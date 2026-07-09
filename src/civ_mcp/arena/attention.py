"""Attention & turn-skipping for arena LLM puppets (quiet-turn fast path).

Owns: SKIP:/WAKE IF: directive parsing, the per-turn trigger scan, the
persisted per-civ attention state, and the wake-digest accumulator/renderer.
Spec: docs/superpowers/specs/2026-07-09-arena-attention-turn-skipping-design.md

Philosophy: every failure here degrades toward MORE model turns, never more
blind skips. Directive unparseable -> no directive. State corrupt -> reset +
wake. Scan error or partial -> wake.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from civ_mcp.json_io import read_json_file, write_json_file_atomic
from civ_mcp.lua._helpers import SENTINEL

SOFT_TRIGGERS: tuple[str, ...] = (
    "GREAT_PERSON_AVAILABLE",
    "CITY_GREW",
    "TRADE_ROUTE_IDLE",
    "GOLD_STOCKPILE_HIGH",
)

# Tolerant line matchers, the STANDING_PLAN_RE lesson (memory.py): models
# reformat instructed plain markers into bullets/headings/emphasis, and a
# silent miss is a directive that never takes effect. The keyword must open
# the line (after markdown prefixes) so prose like "I will skip: nothing"
# never matches. Also imported by memory.extract_standing_plan as standing-
# plan TERMINATORS so a directive after the plan block is never persisted
# as plan text (external-review catch: the plan collector's header test
# requires a trailing colon, so "SKIP: 3" would otherwise be swallowed).
_DIRECTIVE_PREFIX = r"^\s*(?:[-*•]+\s+)?(?:#{1,6}\s*)?(?:[*_]{1,3})?\s*"
SKIP_LINE_RE = re.compile(
    _DIRECTIVE_PREFIX + r"skip\s*(?:[*_]{1,3})?\s*:\s*(?P<body>.*)$", re.IGNORECASE
)
WAKE_IF_LINE_RE = re.compile(
    _DIRECTIVE_PREFIX + r"wake\s+if\s*(?:[*_]{1,3})?\s*:\s*(?P<body>.*)$", re.IGNORECASE
)


@dataclass(frozen=True)
class Directive:
    skip: int
    wake_if: tuple[str, ...] = ()
    unknown_tokens: tuple[str, ...] = ()
    clamped: bool = False


def has_directive_lines(summary: str) -> bool:
    """True if any line looks like a directive attempt (valid or not)."""
    return any(
        SKIP_LINE_RE.match(line) or WAKE_IF_LINE_RE.match(line)
        for line in summary.splitlines()
    )


def parse_directive(summary: str, max_skip: int) -> Directive | None:
    """Extract a SKIP/WAKE IF directive from a final summary, or None.

    First parsed SKIP line wins; later SKIP lines never override it, and a
    SKIP line with no integer does not block a later parseable one. WAKE IF
    without SKIP is inert (spec: sleep must be freshly and explicitly
    chosen). Unknown WAKE IF tokens are collected, not fatal. SKIP body must
    START with an integer ("SKIP: 3 turns" tolerated;
    digit-bearing prose like "SKIP: hold until turn 340" does not parse).
    """
    skip: int | None = None
    clamped = False
    wake_if: list[str] = []
    unknown: list[str] = []
    for line in summary.splitlines():
        m = SKIP_LINE_RE.match(line)
        if m and skip is None:
            # The integer must LEAD the body (markdown decoration tolerated):
            # "SKIP: 3" / "SKIP: 3 turns" / "SKIP: **3**" parse; digit-bearing
            # prose like "SKIP: hold until turn 340" must NOT become a
            # max-clamped blind skip (review-2 f6) -- no directive -> wake.
            num = re.match(r"[\s*_`~'\"(\[]*(-?\d+)", m.group("body"))
            if num:
                n = int(num.group(1))
                skip = min(max(n, 1), max_skip)
                clamped = skip != n
            continue
        m = WAKE_IF_LINE_RE.match(line)
        if m:
            for tok in re.split(r"[,\s]+", m.group("body")):
                token = tok.strip("`*_.").upper()
                if not token:
                    continue
                if token in SOFT_TRIGGERS:
                    if token not in wake_if:
                        wake_if.append(token)
                else:
                    unknown.append(token)
    if skip is None:
        return None
    return Directive(
        skip=skip, wake_if=tuple(wake_if), unknown_tokens=tuple(unknown), clamped=clamped
    )


SCHEMA_VERSION = 1
DIGEST_MAX_CHARS = 1200
DIGEST_MAX_NOTIFICATIONS = 10  # the gossip lesson: never an unbounded feed


@dataclass(frozen=True)
class AttentionState:
    """Per-civ persisted skip state. Corrupt file -> fresh state -> wake."""
    schema_version: int = SCHEMA_VERSION
    run_id: str = ""
    player_id: int = -1
    directive: dict | None = None      # {"skip": int, "wake_if": [...]} as issued
    skips_remaining: int = 0
    streak: int = 0                    # consecutive sleeps since last model turn
    last_wake_turn: int = -1
    last_snapshot: dict | None = None  # overview snapshot at previous captured turn
    last_scan: dict | None = None      # stored scalars: at_war_with/era_index/total_population
    slept: list = field(default_factory=list)   # digest accumulator, one dict per slept turn
    directive_ack: str = ""            # reported in the next wake digest


def attention_path(transcript_dir: str, run_id: str, player_id: int) -> Path:
    return Path(transcript_dir) / run_id / "attention" / f"player_{player_id}.json"


def load_attention_state(transcript_dir: str, run_id: str, player_id: int) -> AttentionState:
    data = read_json_file(attention_path(transcript_dir, run_id, player_id))
    fresh = AttentionState(run_id=run_id, player_id=player_id)
    if not isinstance(data, dict):
        return fresh
    try:
        for key in ("directive", "last_snapshot", "last_scan"):
            value = data.get(key)
            if value is not None and not isinstance(value, dict):
                raise TypeError(f"{key} must be a dict or null")
        directive = data.get("directive")
        if directive is not None:
            # Value-type validation (review-3 f1): a dict-shaped directive
            # with wake_if as a str tuple()s into per-character tokens
            # WITHOUT raising -- the one corruption class that produces a
            # masked skip instead of a loud failure. Reject it here so the
            # established contract (corrupt file -> fresh state -> wake ->
            # save self-heals) covers it.
            if not isinstance(directive.get("skip"), int):
                raise TypeError("directive.skip must be an int")
            wake_if_val = directive.get("wake_if", [])
            if not isinstance(wake_if_val, list) or not all(
                isinstance(t, str) for t in wake_if_val
            ):
                raise TypeError("directive.wake_if must be a list of str")
        slept = data.get("slept", [])
        if not isinstance(slept, list) or not all(isinstance(r, dict) for r in slept):
            raise TypeError("slept must be a list of dicts")
        st = AttentionState(
            schema_version=int(data["schema_version"]),
            run_id=str(data["run_id"]),
            player_id=int(data["player_id"]),
            directive=data.get("directive"),
            skips_remaining=int(data.get("skips_remaining", 0)),
            streak=int(data.get("streak", 0)),
            last_wake_turn=int(data.get("last_wake_turn", -1)),
            last_snapshot=data.get("last_snapshot"),
            last_scan=data.get("last_scan"),
            slept=list(slept),
            directive_ack=str(data.get("directive_ack", "")),
        )
    except (KeyError, TypeError, ValueError):
        return fresh
    if st.run_id != run_id or st.player_id != player_id:
        return fresh
    return st


def save_attention_state(
    transcript_dir: str, run_id: str, player_id: int, state: AttentionState
) -> None:
    payload = {
        "schema_version": state.schema_version,
        "run_id": state.run_id,
        "player_id": state.player_id,
        "directive": state.directive,
        "skips_remaining": state.skips_remaining,
        "streak": state.streak,
        "last_wake_turn": state.last_wake_turn,
        "last_snapshot": state.last_snapshot,
        "last_scan": state.last_scan,
        "slept": state.slept,
        "directive_ack": state.directive_ack,
    }
    write_json_file_atomic(attention_path(transcript_dir, run_id, player_id), payload)


def note_sleep(
    state: AttentionState, *, turn: int, snapshot: dict | None,
    scan_scalars: dict | None, task_notes: list, notifications: list,
) -> AttentionState:
    record = {
        "turn": turn,
        "snapshot": snapshot,
        "task_notes": list(task_notes),
        "notifications": [list(n) for n in notifications][:DIGEST_MAX_NOTIFICATIONS],
    }
    return replace(
        state,
        skips_remaining=max(0, state.skips_remaining - 1),
        streak=state.streak + 1,
        last_snapshot=snapshot if snapshot is not None else state.last_snapshot,
        last_scan=scan_scalars if scan_scalars is not None else state.last_scan,
        slept=[*state.slept, record],
    )


def note_wake(
    state: AttentionState, *, turn: int, wake_cause: str, directive: "Directive | None",
    directive_ack: str, snapshot: dict | None, scan_scalars: dict | None,
) -> AttentionState:
    # Any wake cancels the remainder (spec: sleep is always freshly chosen).
    new_directive = None
    remaining = 0
    if directive is not None:
        new_directive = {"skip": directive.skip, "wake_if": list(directive.wake_if)}
        remaining = directive.skip
    return replace(
        state,
        directive=new_directive,
        skips_remaining=remaining,
        streak=0,
        last_wake_turn=turn,
        last_snapshot=snapshot if snapshot is not None else state.last_snapshot,
        last_scan=scan_scalars if scan_scalars is not None else state.last_scan,
        slept=[],
        directive_ack=directive_ack,
    )


def cancel_remainder(state: AttentionState) -> AttentionState:
    """Cancel an active directive's remaining sleeps without the full wake
    bookkeeping.

    For the failed-policy-turn seam (final-review Important 2): a wake whose
    LLM call failed never reaches ``note_wake``, and spec section 3 says any
    wake cancels the remainder -- otherwise the seat resumes a stale sleep
    right after the system misbehaved. Keeps the ``slept`` accumulator (the
    digest must survive to the eventual successful wake) and the streak (its
    cap keeps bounding the run of model-free turns).
    """
    return replace(state, skips_remaining=0)


def render_digest(
    state: AttentionState, *, wake_turn: int, wake_cause: str, wake_detail: str
) -> str:
    """Priority order (spec section 4): wake cause, directive ack, accumulated
    deltas, tracker progress, notifications (newest first, capped).

    Must be called on the pre-wake state (while ``slept`` is still populated)
    — ``note_wake`` clears the accumulator.
    """
    if not state.slept:
        return ""
    first = state.slept[0]["turn"]
    last = state.slept[-1]["turn"]
    n = len(state.slept)
    lines = [f"== WHILE YOU SLEPT (turns {first}–{last}, {n} skipped) =="]
    cause = wake_cause + (f" — {wake_detail}" if wake_detail else "")
    lines.append(f"Woke because: {cause}")
    if state.directive_ack:
        lines.append(f"Your directive: {state.directive_ack}")
    snaps = [r["snapshot"] for r in state.slept if r.get("snapshot")]
    if snaps:
        first_s, last_s = snaps[0], snaps[-1]
        lines.append("Empire while asleep:")
        deltas = []
        for key in ("score", "gold", "science", "culture", "cities", "units"):
            a, b = first_s.get(key), last_s.get(key)
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                deltas.append(f"{key} {a}→{b}")
        if deltas:
            lines.append("- " + ", ".join(deltas))
    notes = [note for r in state.slept for note in r.get("task_notes", [])]
    if notes:
        lines.append("Tracker: " + "; ".join(notes[-5:]))
    tagged = [
        (rec["turn"], pair[1] if len(pair) > 1 else str(pair))
        for rec in state.slept
        for pair in rec.get("notifications", [])
    ]
    if tagged:
        lines.append(
            f"Notifications during sleep (newest first, max {DIGEST_MAX_NOTIFICATIONS}):"
        )
        for turn_no, msg in list(reversed(tagged))[:DIGEST_MAX_NOTIFICATIONS]:
            lines.append(f"- [T{turn_no}] {msg}")
    text = "\n".join(lines)
    return text[:DIGEST_MAX_CHARS].rstrip()


# --- Trigger scan (Task 7): batched read-only Lua build + parse ---------------
#
# Line protocol (spec section 3):
#   ATTN|<FAMILY>|key=value|...     one line per family (NOTIFY repeats, max 10)
#   ATTN_ERR|<FAMILY>               a family whose pcall failed
#   ---END---                       sentinel, always last
#
# Every family runs in its own pcall so a single bad API name degrades that
# family to ATTN_ERR (-> failed_families -> wake), never a crash or a blind
# skip of the whole scan.

_SCAN_FAMILIES = (
    "THREAT", "CITYHP", "WAR", "LOYALTY", "WC", "ERA",
    "POP", "GP", "TRADE", "DIPLO", "BLOCKERS",
)
BLOCKER_IGNORE = frozenset({"ENDTURN_BLOCKING_UNIT_PROMOTION"})
NOTIFICATION_WAKE_LIST = frozenset({
    "NOTIFICATION_CITY_UNDER_ATTACK",
    "NOTIFICATION_CITY_LOW_LOYALTY",
    "NOTIFICATION_REBELLION",
    "NOTIFICATION_SPY_CAUGHT",
})


@dataclass(frozen=True)
class AttentionScan:
    hostile_count: int = 0
    nearest_hostile: str = ""
    damaged_city_ids: tuple[int, ...] = ()
    at_war_with: tuple[int, ...] = ()
    negative_loyalty_city_ids: tuple[int, ...] = ()
    wc_turns_until_next: int = -1
    era_index: int = -1
    total_population: int = 0
    great_person_available: bool = False
    trade_route_idle: bool = False
    pending_diplomacy: bool = False
    blocker_types: tuple[str, ...] = ()
    notifications: tuple[tuple[str, str], ...] = ()
    failed_families: tuple[str, ...] = ()
    failure_details: tuple[str, ...] = ()


def _ids(value: str) -> tuple[int, ...]:
    out = []
    for part in value.split(","):
        part = part.strip()
        if part:
            try:
                out.append(int(part))
            except ValueError:
                continue
    return tuple(out)


def parse_attention_scan(lines: "list[str] | None") -> AttentionScan | None:
    if not lines:
        return None
    fields: dict = {}
    seen: set[str] = set()
    failed: list[str] = []
    failure_details: list[str] = []
    notifications: list[tuple[str, str]] = []
    for line in lines:
        if line.startswith("ATTN_ERR|"):
            parts = line.split("|", 2)
            fam_name = parts[1].strip() if len(parts) > 1 else ""
            detail = parts[2].strip() if len(parts) > 2 else ""
            failed.append(fam_name)
            if detail:
                failure_details.append(f"{fam_name}: {detail}")
            continue
        if not line.startswith("ATTN|"):
            continue
        parts = line.split("|")
        family = parts[1] if len(parts) > 1 else ""
        kv = {}
        for part in parts[2:]:
            key, sep, val = part.partition("=")
            if sep:
                kv[key] = val
        try:
            if family == "THREAT":
                fields["hostile_count"] = int(kv.get("count", "0"))
                fields["nearest_hostile"] = kv.get("nearest", "")
            elif family == "CITYHP":
                fields["damaged_city_ids"] = _ids(kv.get("damaged", ""))
            elif family == "WAR":
                fields["at_war_with"] = _ids(kv.get("with", ""))
            elif family == "LOYALTY":
                fields["negative_loyalty_city_ids"] = _ids(kv.get("negative", ""))
            elif family == "WC":
                fields["wc_turns_until_next"] = int(kv.get("turns", "-1"))
            elif family == "ERA":
                fields["era_index"] = int(kv.get("index", "-1"))
            elif family == "POP":
                fields["total_population"] = int(kv.get("total", "0"))
            elif family == "GP":
                fields["great_person_available"] = kv.get("available", "0") == "1"
            elif family == "TRADE":
                fields["trade_route_idle"] = kv.get("idle", "0") == "1"
            elif family == "DIPLO":
                fields["pending_diplomacy"] = kv.get("pending", "0") == "1"
            elif family == "BLOCKERS":
                types = tuple(
                    t for t in kv.get("types", "").split(",")
                    if t and t not in BLOCKER_IGNORE
                )
                fields["blocker_types"] = types
            elif family == "NOTIFY":
                notifications.append((kv.get("type", ""), kv.get("msg", "")))
                continue  # repeated family; not part of `seen` accounting
            else:
                continue
        except ValueError:
            failed.append(family)
            continue
        seen.add(family)
    if not seen and not failed:
        return None
    for family in _SCAN_FAMILIES:
        if family not in seen and family not in failed:
            failed.append(family)  # a missing family narrows attention -> treat as failed
    return AttentionScan(
        notifications=tuple(notifications), failed_families=tuple(failed),
        failure_details=tuple(failure_details), **fields
    )


def scan_scalars(scan: AttentionScan) -> dict:
    return {
        "at_war_with": list(scan.at_war_with),
        "era_index": scan.era_index,
        "total_population": scan.total_population,
    }


# --- Lua query build -----------------------------------------------------
#
# Idiom provenance (each family's body is copied from the cited accessor
# idiom, not invented):
#   THREAT   - overview.py:37-46 (city/unit asset plots, GameInfo.Units[u:GetType()]),
#              units.py:200 (civilian FormationClass check), map.py:151-161
#              (PlayersVisibility[me]:IsVisible(plotIdx)), diplomacy.py:43
#              (alive-player loop shape; barbarians via Players[i]:IsBarbarian())
#   CITYHP   - cities.py:48-61 (city-center district GetMaxDamage/GetDamage
#              for DISTRICT_GARRISON and DISTRICT_OUTER)
#   WAR      - diplomacy.py:43 (alive-major loop + pDiplo:IsAtWarWith(i))
#   LOYALTY  - cities.py:852-878 (_LOYALTY_LUA accessor names; unlike that
#              read tool, failures here PROPAGATE to fam() -> ATTN_ERR --
#              a hard-trigger family must wake on failure, not degrade)
#   WC       - congress.py build_world_congress_query (GetWorldCongress() +
#              GetMeetingStatus().TurnsLeft)
#   ERA      - tech.py:93 (Game.GetEras():GetCurrentEra())
#   POP      - overview.py:35 (sum c:GetPopulation())
#   GP       - great_people.py:147-156 (gp:CanRecruitPerson(me, entry.Individual)
#              inside the GetTimeline() candidate loop)
#   TRADE    - economy.py build_trade_capacity_check (GetOutgoingRouteCapacity()
#              vs GetOutgoingRoutes() count) + build_trade_routes_query's
#              uInfo.MakeTradeRoute trader-unit check
#   DIPLO    - diplomacy.py:328-334 (build_close_orphan_sessions session-
#              iteration idiom: DiplomacyManager.FindOpenSessionID both ways)
#   BLOCKERS - notifications.py:20-39 (build_end_turn_blocking_query;
#              EndTurnBlockingTypes reverse lookup), :33 sanitize idiom
#   NOTIFY   - notifications.py:53-102 (build_notifications_query;
#              entry:GetTypeName(), entry:GetMessage():gsub("|", "/"))

_ATTENTION_LUA = """
local me = __PID__
local radius = __RADIUS__
local wakeTypes = {__WAKELIST__}
local p = Players[me]
local pDiplo = p:GetDiplomacy()
local function fam(name, fn)
    local ok, err = pcall(fn)
    if not ok then
        -- Carry the engine error text (pipe-sanitized, capped) so a
        -- SCAN_PARTIAL wake is diagnosable post-run: "API absent on this
        -- build" vs a one-off miss (review-3 f3).
        print("ATTN_ERR|" .. name .. "|" .. tostring(err):gsub("|", "/"):sub(1, 120))
    end
end
fam("ERA", function()
    print("ATTN|ERA|index=" .. tostring(Game.GetEras():GetCurrentEra()))
end)
fam("WAR", function()
    -- diplomacy.py:43 loop shape. PlayerManager.GetAliveMajors() has no
    -- codebase precedent; if that API were missing, this family would emit
    -- ATTN_ERR|WAR on every scan -> every turn wakes -> feature silently inert.
    local ids = {}
    for i = 0, 62 do
        if i ~= me and Players[i] and Players[i]:IsAlive() and Players[i]:IsMajor() then
            if pDiplo:IsAtWarWith(i) then ids[#ids + 1] = tostring(i) end
        end
    end
    print("ATTN|WAR|with=" .. table.concat(ids, ","))
end)
fam("POP", function()
    local total = 0
    for _, c in p:GetCities():Members() do total = total + c:GetPopulation() end
    print("ATTN|POP|total=" .. tostring(total))
end)
fam("THREAT", function()
    -- my assets = my city plots + my civilian-unit plots (overview.py:37-46 shape)
    local myAssets = {}
    for _, c in p:GetCities():Members() do
        table.insert(myAssets, {x = c:GetX(), y = c:GetY(), label = Locale.Lookup(c:GetName())})
    end
    for _, u in p:GetUnits():Members() do
        local entry = GameInfo.Units[u:GetType()]
        if entry and entry.FormationClass == "FORMATION_CLASS_CIVILIAN" then
            table.insert(myAssets, {x = u:GetX(), y = u:GetY(), label = Locale.Lookup(entry.Name)})
        end
    end
    local count = 0
    local bestDist = nil
    local bestLabel = ""
    for i = 0, 63 do
        if i ~= me and Players[i] ~= nil and Players[i]:IsAlive() then
            local hostile = Players[i]:IsBarbarian() or pDiplo:IsAtWarWith(i)
            if hostile then
                for _, u in Players[i]:GetUnits():Members() do
                    local ux, uy = u:GetX(), u:GetY()
                    local plot = Map.GetPlot(ux, uy)
                    if plot then
                        local plotIdx = plot:GetIndex()
                        if PlayersVisibility[me]:IsVisible(plotIdx) then
                            local minD = nil
                            local minLabel = ""
                            for _, asset in ipairs(myAssets) do
                                local d = Map.GetPlotDistance(ux, uy, asset.x, asset.y)
                                if minD == nil or d < minD then
                                    minD = d
                                    minLabel = asset.label
                                end
                            end
                            if minD ~= nil and minD <= radius then
                                count = count + 1
                                if bestDist == nil or minD < bestDist then
                                    local uEntry = GameInfo.Units[u:GetType()]
                                    local uName = uEntry and Locale.Lookup(uEntry.Name) or "Unknown"
                                    bestDist = minD
                                    bestLabel = (uName .. " d" .. minD .. " near " .. minLabel):gsub("|", "/")
                                end
                            end
                        end
                    end
                end
            end
        end
    end
    print("ATTN|THREAT|count=" .. count .. "|nearest=" .. bestLabel)
end)
fam("CITYHP", function()
    local damaged = {}
    local ccIdx = GameInfo.Districts["DISTRICT_CITY_CENTER"].Index
    for _, c in p:GetCities():Members() do
        local garDmg, wallDmg = 0, 0
        for _, d in c:GetDistricts():Members() do
            if d:GetType() == ccIdx then
                -- No inner protected-call wrapper: a GetDamage/DefenseTypes
                -- failure must reach fam()'s error boundary -> ATTN_ERR|CITYHP
                -- -> SCAN_PARTIAL wake, never an empty damaged= that
                -- blind-skips a siege (review-2 f3).
                garDmg = d:GetDamage(DefenseTypes.DISTRICT_GARRISON) or 0
                wallDmg = d:GetDamage(DefenseTypes.DISTRICT_OUTER) or 0
                break
            end
        end
        if garDmg > 0 or wallDmg > 0 then
            table.insert(damaged, tostring(c:GetID()))
        end
    end
    print("ATTN|CITYHP|damaged=" .. table.concat(damaged, ","))
end)
fam("LOYALTY", function()
    local negative = {}
    for _, c in p:GetCities():Members() do
        -- No inner protected-call wrappers: a GetCulturalIdentity/
        -- GetLoyaltyPerTurn failure must reach fam()'s error boundary ->
        -- ATTN_ERR|LOYALTY -> SCAN_PARTIAL wake, never an empty negative=
        -- that blind-skips a loyalty flip (review-2 f4).
        local pt = c:GetCulturalIdentity():GetLoyaltyPerTurn()
        if pt < 0 then
            table.insert(negative, tostring(c:GetID()))
        end
    end
    print("ATTN|LOYALTY|negative=" .. table.concat(negative, ","))
end)
fam("WC", function()
    local turns = -1
    local wc = Game.GetWorldCongress()
    if wc then
        local meeting = wc:GetMeetingStatus()
        turns = (meeting and meeting.TurnsLeft) or -1
    end
    print("ATTN|WC|turns=" .. tostring(turns))
end)
fam("GP", function()
    local available = false
    local gp = Game.GetGreatPeople()
    if gp then
        local timeline = gp:GetTimeline()
        if timeline then
            for _, entry in ipairs(timeline) do
                if entry.Class ~= nil and entry.Individual ~= nil then
                    local canRecruit = false
                    -- Soft-trigger tier: swallowing is deliberate here and in
                    -- TRADE (a loud failure would wake every turn for an
                    -- opt-in signal). Hard families must NOT copy this.
                    pcall(function()
                        canRecruit = gp:CanRecruitPerson(me, entry.Individual)
                    end)
                    if canRecruit then available = true end
                end
            end
        end
    end
    print("ATTN|GP|available=" .. (available and "1" or "0"))
end)
fam("TRADE", function()
    local tr = p:GetTrade()
    local cap = tr:GetOutgoingRouteCapacity()
    local active = 0
    for _, city in p:GetCities():Members() do
        pcall(function()
            local routes = city:GetTrade():GetOutgoingRoutes()
            if routes then active = active + #routes end
        end)
    end
    local hasTrader = false
    for _, u in p:GetUnits():Members() do
        local uType = u:GetType()
        if uType then
            local uInfo = GameInfo.Units[uType]
            if uInfo and uInfo.MakeTradeRoute then hasTrader = true end
        end
    end
    local idle = (cap > active and hasTrader) and "1" or "0"
    print("ATTN|TRADE|idle=" .. idle)
end)
fam("DIPLO", function()
    local pending = false
    for i = 0, 63 do
        if i ~= me and Players[i] ~= nil and Players[i]:IsAlive() then
            local a = DiplomacyManager.FindOpenSessionID(me, i)
            if a and a >= 0 then pending = true end
            local b = DiplomacyManager.FindOpenSessionID(i, me)
            if b and b >= 0 then pending = true end
        end
    end
    print("ATTN|DIPLO|pending=" .. (pending and "1" or "0"))
end)
fam("BLOCKERS", function()
    local list = NotificationManager.GetList(me)
    local seen = {}
    local types = {}
    if list then
        for _, nid in ipairs(list) do
            local entry = NotificationManager.Find(me, nid)
            if entry and not entry:IsDismissed() then
                local bt = entry:GetEndTurnBlocking()
                if bt and bt ~= 0 then
                    local typeName = "UNKNOWN"
                    for k, v in pairs(EndTurnBlockingTypes) do
                        if v == bt then typeName = k; break end
                    end
                    if not seen[typeName] then
                        seen[typeName] = true
                        table.insert(types, typeName)
                    end
                end
            end
        end
    end
    print("ATTN|BLOCKERS|types=" .. table.concat(types, ","))
end)
fam("NOTIFY", function()
    local list = NotificationManager.GetList(me)
    if not list then return end
    local emitted = 0
    local function tryEmit(nid, wantWake)
        -- per-entry pcall (notifications.py:53-102 idiom): one malformed
        -- notification skips itself, not the rest of the list
        pcall(function()
            local entry = NotificationManager.Find(me, nid)
            if entry and not entry:IsDismissed() then
                local typeName = entry:GetTypeName() or "UNKNOWN"
                if (wakeTypes[typeName] == true) == wantWake then
                    local msg = (entry:GetMessage() or ""):gsub("|", "/")
                    print("ATTN|NOTIFY|type=" .. typeName .. "|msg=" .. msg)
                    emitted = emitted + 1
                end
            end
        end)
    end
    -- pass 1: wake-list types always make the cut, whatever their list
    -- position (review-2 f5: SPY_CAUGHT has no redundant trigger family)
    for _, nid in ipairs(list) do
        if emitted >= 10 then break end
        tryEmit(nid, true)
    end
    -- pass 2: fill the remaining slots with everything else, list order
    for _, nid in ipairs(list) do
        if emitted >= 10 then break end
        tryEmit(nid, false)
    end
end)
print("{SENTINEL}")
""".replace("{SENTINEL}", SENTINEL)


def build_attention_query(player_id: int, threat_radius: int) -> str:
    # sorted() so the query text is deterministic (test + cache friendliness)
    wake_entries = ", ".join(
        f'["{name}"]=true' for name in sorted(NOTIFICATION_WAKE_LIST)
    )
    return (
        _ATTENTION_LUA
        .replace("__PID__", str(int(player_id)))
        .replace("__RADIUS__", str(int(threat_radius)))
        .replace("__WAKELIST__", wake_entries)
    )


# --- Decision & evaluation (Task 8) -----------------------------------------------

GOLD_STOCKPILE_THRESHOLD = 500


@dataclass(frozen=True)
class Decision:
    action: str                    # "sleep" | "wake"
    wake_cause: str | None = None
    wake_detail: str = ""
    hard: tuple[str, ...] = ()
    soft: tuple[str, ...] = ()


def _hard_triggers(
    state: AttentionState, scan: AttentionScan, snapshot: dict, task_event: bool
) -> "tuple[list[str], str]":
    prev = state.last_snapshot or {}
    prev_scan = state.last_scan or {}
    hard: list[str] = []
    detail = ""
    if task_event:
        hard.append("TASK_EVENT")
    if snapshot.get("units", 0) < prev.get("units", 0):
        hard.append("UNITS_LOST")
    if snapshot.get("cities", 0) != prev.get("cities", 0):
        hard.append("CITY_COUNT_CHANGED")
    gold, prev_gold = snapshot.get("gold"), prev.get("gold")
    if (
        isinstance(gold, (int, float)) and isinstance(prev_gold, (int, float))
        and gold < prev_gold and gold + 5 * (gold - prev_gold) < 0
    ):
        hard.append("GOLD_CRASH")
    if scan.blocker_types:
        hard.append(f"BLOCKER_{scan.blocker_types[0]}")
    if scan.hostile_count > 0:
        hard.append("ENEMY_NEAR")
        detail = detail or scan.nearest_hostile
    if scan.damaged_city_ids:
        hard.append("CITY_DAMAGED")
    if set(scan.at_war_with) != set(prev_scan.get("at_war_with", [])):
        hard.append("WAR_PEACE_CHANGED")
    if scan.negative_loyalty_city_ids:
        hard.append("LOYALTY_NEGATIVE")
    if scan.wc_turns_until_next == 0:
        hard.append("WC_SESSION")
    prev_era = prev_scan.get("era_index", -1)
    if scan.era_index >= 0 and prev_era >= 0 and scan.era_index != prev_era:
        hard.append("ERA_CHANGED")
    if any(ntype in NOTIFICATION_WAKE_LIST for ntype, _ in scan.notifications):
        hard.append("NOTIFICATION_WAKE")
    if scan.pending_diplomacy:
        hard.append("BLOCKER_DIPLOMACY_SESSION")
    return hard, detail


def evaluate(
    mode: str, state: AttentionState, scan: AttentionScan | None,
    snapshot: dict | None, *, max_streak: int, task_event: bool,
) -> Decision:
    if scan is None or snapshot is None:
        return Decision("wake", "SCAN_ERROR")
    if scan.failed_families:
        detail = ",".join(scan.failed_families)
        if scan.failure_details:
            detail = (detail + " -- " + "; ".join(scan.failure_details))[:300]
        return Decision("wake", "SCAN_PARTIAL", detail)
    if state.last_snapshot is None or state.last_scan is None:
        return Decision("wake", "NO_BASELINE")
    hard, detail = _hard_triggers(state, scan, snapshot, task_event)
    if hard:
        # detail describes ENEMY_NEAR; never attach it to a different winning
        # cause (review catch)
        return Decision(
            "wake", hard[0], detail if hard[0] == "ENEMY_NEAR" else "", hard=tuple(hard)
        )
    if state.streak >= max_streak:
        return Decision("wake", "STREAK_CAP")
    directive_active = state.skips_remaining > 0
    # Subscriptions key on the directive EXISTING, not on skips remaining:
    # in hybrid the seat keeps auto-sleeping after the directive is spent,
    # and the model's explicit WAKE IF must keep being honored for that
    # whole streak (review-2 f10). note_wake clears/replaces the directive
    # on every wake, so a subscription never outlives its sleep streak.
    wake_if_raw = (state.directive or {}).get("wake_if", ())
    if not isinstance(wake_if_raw, (list, tuple)):
        # A non-list wake_if would tuple() into per-character tokens and
        # silently drop the model's subscription -- a masked skip, the
        # unsafe direction. Raise instead: the coordinator's STATE_CORRUPT
        # guard resets + wakes and note_wake's save self-heals the file
        # (review-3 f1 backstop; load_attention_state validates first).
        raise TypeError(
            f"directive.wake_if must be a list, got {type(wake_if_raw).__name__}"
        )
    subscribed = tuple(wake_if_raw)
    if mode in ("model", "hybrid") and subscribed:
        soft: list[str] = []
        if "GREAT_PERSON_AVAILABLE" in subscribed and scan.great_person_available:
            soft.append("GREAT_PERSON_AVAILABLE")
        if (
            "CITY_GREW" in subscribed
            and scan.total_population > state.last_scan.get("total_population", 0)
        ):
            soft.append("CITY_GREW")
        if "TRADE_ROUTE_IDLE" in subscribed and scan.trade_route_idle:
            soft.append("TRADE_ROUTE_IDLE")
        if (
            "GOLD_STOCKPILE_HIGH" in subscribed
            and snapshot.get("gold", 0) >= GOLD_STOCKPILE_THRESHOLD
        ):
            soft.append("GOLD_STOCKPILE_HIGH")
        if soft:
            return Decision("wake", soft[0], soft=tuple(soft))
    if mode == "auto":
        return Decision("sleep")
    if mode == "model":
        return Decision("sleep") if directive_active else Decision("wake", "NO_DIRECTIVE")
    if mode == "hybrid":
        return Decision("sleep")
    return Decision("wake", "SCAN_ERROR")  # unknown mode: defensive fail-open
