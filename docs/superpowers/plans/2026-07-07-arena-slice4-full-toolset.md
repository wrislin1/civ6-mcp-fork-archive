# Arena Slice 4 — Full Toolset & Era-Gated Abilities Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `local` (in-process LLM) arena civs the full game-system toolset — parity with the MCP server plus seven new game systems — with per-turn era/state gating and the completion-cap raise.

**Architecture:** Approach A, phased value-first: cap → parity → gating → systems. Gating is a per-turn `CAPS|` Lua snapshot parsed into a flags dict; one filtered name list feeds the OpenAI tool schema, invalid-call classification, and the dispatch allowlist. Every new game system follows the uniform pipeline: Lua builder + parser (models) → GameState method → MCP server tool → registry `ToolDef` (+ `requires`) → narrator → fixture tests. Greenfield Lua APIs are pcall-guarded and validated by a live-probe checklist that is the slice's merge gate.

**Tech Stack:** Python 3.12 (`uv`), pytest + pytest-asyncio, FireTuner Lua (two contexts: `execute_read` = GameCore, `execute_write` = InGame UI), openai AsyncOpenAI client.

**Spec:** `docs/superpowers/specs/2026-07-07-arena-slice4-full-toolset-design.md` (read it first).

## Global Constraints

- `run_lua` stays removed from puppet/CLI toolsets — never register it in the arena registry; never add it to any tier.
- End-state is an **unmerged branch** (`arena-slice4-full-toolset`, worktree via `superpowers:using-git-worktrees`) ready for riz's separate-session review. Never merge or push without direction.
- **Merge gate:** no greenfield-backed tool reaches a live run until its live probe (Task 15 checklist) captures a real fixture or the spec records a degrade/cut decision.
- Degrade-not-abort: every new coordinator-loop component is exception-guarded; gating **fails open** (snapshot error ⇒ full toolset, missing flag ⇒ exposed).
- All new unit-taking registry tools accept composite `unit_id` and convert via `_unit_index()` (`unit_id % 65536`).
- New registry entries are appended at the **end of the `TOOL_REGISTRY` dict literal** so `TIERS["full"] = tuple(TOOL_REGISTRY)` auto-includes them (test-enforced). No experiment YAML changes.
- Every new **action** tool sets `verb=` on its `ToolDef` AND adds the same entry to `vocab.LOCAL_TOOL_VERBS` — `tests/arena/test_analyze.py` enforces the mirror and will fail the suite otherwise.
- Run tests as `uv run pytest tests/ -q` (never bare `uv run pytest` — `scripts/` contains live-game scripts that break collection).
- Lua builders: pipe-delimited `print()` lines terminated by `print("---END---")` (`SENTINEL` from `civ_mcp.lua._helpers`); action builders print `OK:...`/`ERR:...` and GameState wraps with `_action_result`. Wrap every uncertain API call in `pcall`. Mark unverified APIs with a `-- PROBE(live):` comment tied to Task 15.
- Baseline suite: 709 passing. Suite must be green at every commit.

## File Structure

See the spec's Section 6 File Map. Summary of new files: `src/civ_mcp/arena/capabilities.py`, `src/civ_mcp/lua/climate.py`, `src/civ_mcp/lua/great_works.py`, `tests/arena/test_capabilities.py`, `docs/superpowers/plans/2026-07-07-arena-slice4-live-probes.md`. Everything else modifies existing files.

---

### Task 1: Completion cap 6144 + 300 s timeout + no-retry-on-timeout

**Files:**
- Modify: `src/civ_mcp/arena/backends.py`
- Test: `tests/arena/test_backends.py`

**Interfaces:**
- Consumes: existing `OpenAICompatBackend.chat`, `MAX_ATTEMPTS = 3`, `RETRY_BACKOFF_S = 1.0`, test helper `_backend_with_create(create_fn)` already in `tests/arena/test_backends.py:69`.
- Produces: `MAX_COMPLETION_TOKENS = 6144`, `REQUEST_TIMEOUT_S = 300.0`; `chat()` re-raises `openai.APITimeoutError` immediately (no retry).

- [ ] **Step 1: Write the failing tests**

In `tests/arena/test_backends.py`, first **edit the existing bounds test** to the new envelope:

```python
def test_caps_are_bounded():
    # guard against someone loosening the cap back into runaway territory
    assert 256 <= MAX_COMPLETION_TOKENS <= 8192
    assert 30 <= REQUEST_TIMEOUT_S <= 600
    assert MAX_COMPLETION_TOKENS == 6144   # slice-4 decision (spec §4)
    assert REQUEST_TIMEOUT_S == 300.0      # token cap, not the clock, bounds a step
```

Then append (add `import httpx` and `import openai` to the test file's imports):

```python
@pytest.mark.asyncio
async def test_timeout_errors_are_not_retried(monkeypatch):
    """A 300 s timeout at a 6144 cap means runaway generation; resampling it
    3x would stall one seat ~15 minutes. Timeouts re-raise immediately so the
    coordinator's degrade guard skips the turn (spec §4)."""
    monkeypatch.setattr(asyncio, "sleep", _noop)
    calls = {"n": 0}

    async def timing_out(**kw):
        calls["n"] += 1
        raise openai.APITimeoutError(request=httpx.Request("POST", "http://x/v1"))

    b = _backend_with_create(timing_out)
    with pytest.raises(openai.APITimeoutError):
        await b.chat([{"role": "user", "content": "hi"}], tools=[])
    assert calls["n"] == 1   # no retry


@pytest.mark.asyncio
async def test_non_timeout_errors_still_retry(monkeypatch):
    """The existing 3-attempt retry stays for gateway 500s / llama-swap 503s."""
    monkeypatch.setattr(asyncio, "sleep", _noop)
    calls = {"n": 0}

    async def flaky_then_ok(**kw):
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("HTTP 500")
        import types as _t
        msg = _t.SimpleNamespace(content="ok", tool_calls=None)
        return _t.SimpleNamespace(
            choices=[_t.SimpleNamespace(message=msg)],
            usage=_t.SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    b = _backend_with_create(flaky_then_ok)
    r = await b.chat([{"role": "user", "content": "hi"}], tools=[])
    assert r.text == "ok"
    assert calls["n"] == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/arena/test_backends.py -q`
Expected: `test_caps_are_bounded` FAILS (`3072 != 6144`), `test_timeout_errors_are_not_retried` FAILS (`calls["n"] == 3`, retried).

- [ ] **Step 3: Implement**

In `src/civ_mcp/arena/backends.py`: add `import openai` below the existing `from openai import AsyncOpenAI`. Replace the two constants and their comment block:

```python
# A turn-step is "reason, then emit one tool call". Observed legit steps reach
# ~1,900 completion tokens; one live step hit the old 3072 cap (truncated tool
# JSON -> gateway 500 -> the 37a48ef crash). 6144 is ~3x observed max. At local
# speeds (~25-35 tok/s on a 3090) a full 6144-token generation runs 3-4 minutes,
# so the timeout rises with it: the token cap, not the clock, bounds a legit
# long step. A timeout at this cap means runaway generation - it is re-raised
# without retry (see chat()) so one seat stalls at most ~5 min before the
# coordinator's degrade guard skips the turn.
MAX_COMPLETION_TOKENS = 6144
REQUEST_TIMEOUT_S = 300.0
```

In `chat()`, change the retry loop's except clause:

```python
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = await self._client.chat.completions.create(**kw)
                break
            except openai.APITimeoutError:
                # Runaway generation, not a transient: resampling would repeat it.
                raise
            except Exception:
                if attempt >= MAX_ATTEMPTS:
                    raise
                await asyncio.sleep(RETRY_BACKOFF_S * attempt)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/arena/test_backends.py -q` then `uv run pytest tests/ -q`
Expected: all PASS (709 + 2 new).

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/backends.py tests/arena/test_backends.py
git commit -m "feat(arena): raise completion cap to 6144, timeout to 300s, no retry on timeout"
```

---

### Task 2: Parity readouts — `get_spies`, `get_strategic_map`, `get_notifications`

**Files:**
- Modify: `src/civ_mcp/arena/registry.py` (wrappers near the other `*_text` helpers ~line 140-240; ToolDefs at the END of `TOOL_REGISTRY`)
- Test: `tests/arena/test_registry.py`

**Interfaces:**
- Consumes: `gs.get_spies() -> list[SpyInfo]`, `gs.get_strategic_map() -> StrategicMapData`, `gs.get_notifications() -> list[GameNotification]` (all exist in `game_state.py`); `nr.narrate_spies`, `nr.narrate_strategic_map`, `nr.narrate_notifications` (all exist in `narrate.py`); registry helpers `_render`, `_tool`.
- Produces: registry names `get_spies`, `get_strategic_map`, `get_notifications` (query tools, `verb=""`).

- [ ] **Step 1: Write the failing test**

Append to `tests/arena/test_registry.py`:

```python
PARITY_READOUTS = ("get_spies", "get_strategic_map", "get_notifications")


def test_parity_readouts_registered():
    for name in PARITY_READOUTS:
        assert name in TOOL_REGISTRY, name
        assert TOOL_REGISTRY[name].verb == ""      # query tools carry no verb
        assert name in resolve_tools("full")


@pytest.mark.asyncio
async def test_parity_readouts_dispatch_to_gamestate():
    class GS:
        def __init__(self):
            self.called = []
        async def get_spies(self):
            self.called.append("spies"); return "2 spies"      # str passthrough
        async def get_strategic_map(self):
            self.called.append("smap"); return "fog report"
        async def get_notifications(self):
            self.called.append("notif"); return "3 notifications"

    gs = GS()
    for name in PARITY_READOUTS:
        out = await dispatch(gs, name, {})
        assert isinstance(out, str) and out
    assert gs.called == ["spies", "smap", "notif"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/arena/test_registry.py -q`
Expected: FAIL with `KeyError`/`assert 'get_spies' in TOOL_REGISTRY`.

- [ ] **Step 3: Implement**

In `registry.py`, near the other text helpers:

```python
async def _spies_text(gs: Any, args: dict[str, Any]) -> str:
    del args
    return _render(await gs.get_spies(), nr.narrate_spies)


async def _strategic_map_text(gs: Any, args: dict[str, Any]) -> str:
    del args
    return _render(await gs.get_strategic_map(), nr.narrate_strategic_map)


async def _notifications_text(gs: Any, args: dict[str, Any]) -> str:
    del args
    return _render(await gs.get_notifications(), nr.narrate_notifications)
```

At the END of the `TOOL_REGISTRY` dict literal (before its closing `}`):

```python
    "get_spies": _tool(
        "get_spies",
        "List your spy units: composite id, position, rank, city, and which "
        "operations are available where they stand. Offensive missions need the "
        "spy physically in the target city (spy_action travel first).",
        None,
        (),
        _spies_text,
    ),
    "get_strategic_map": _tool(
        "get_strategic_map",
        "Empire-level map summary: fog coverage per city and unclaimed nearby "
        "resources. Use every ~30 turns to spot expansion gaps.",
        None,
        (),
        _strategic_map_text,
    ),
    "get_notifications": _tool(
        "get_notifications",
        "Current game notifications (the bell items a human sees): what needs "
        "attention this turn.",
        None,
        (),
        _notifications_text,
    ),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/arena/test_registry.py tests/arena/test_analyze.py -q` then `uv run pytest tests/ -q`
Expected: PASS (readouts have no verbs, so the LOCAL_TOOL_VERBS mirror is untouched).

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/registry.py tests/arena/test_registry.py
git commit -m "feat(arena): expose get_spies/get_strategic_map/get_notifications to local civs"
```

---

### Task 3: Parity actions — `spy_action`, `change_government`, `spread_religion`, `activate_great_person`

**Files:**
- Modify: `src/civ_mcp/arena/registry.py`, `src/civ_mcp/arena/vocab.py`
- Test: `tests/arena/test_registry.py`

**Interfaces:**
- Consumes: `gs.spy_travel(unit_index, x, y)`, `gs.spy_mission(unit_index, mission_type, x, y)`, `gs.change_government(government_type)`, `gs.spread_religion(unit_index)`, `gs.activate_great_person(unit_index)` — all existing GameState methods returning `str`; `_unit_index` at `registry.py:152`.
- Produces: registry names `spy_action`, `change_government`, `spread_religion`, `activate_great_person` with verbs `"spy_action"`, `"change_government"`, `"spread_religion"`, `"activate_great_person"`; matching `LOCAL_TOOL_VERBS` entries.

- [ ] **Step 1: Write the failing test**

Append to `tests/arena/test_registry.py`:

```python
@pytest.mark.asyncio
async def test_spy_action_routes_travel_vs_mission():
    class GS:
        def __init__(self):
            self.calls = []
        async def spy_travel(self, unit_index, x, y):
            self.calls.append(("travel", unit_index, x, y)); return "OK travel"
        async def spy_mission(self, unit_index, mission, x, y):
            self.calls.append(("mission", unit_index, mission, x, y)); return "OK mission"

    gs = GS()
    # composite id 65539 = player 1, unit index 3
    await dispatch(gs, "spy_action",
                   {"unit_id": 65539, "action": "travel", "target_x": 5, "target_y": 6})
    await dispatch(gs, "spy_action",
                   {"unit_id": 65539, "action": "SIPHON_FUNDS", "target_x": 5, "target_y": 6})
    assert gs.calls == [("travel", 3, 5, 6), ("mission", 3, "SIPHON_FUNDS", 5, 6)]


@pytest.mark.asyncio
async def test_parity_actions_dispatch_with_composite_ids():
    class GS:
        def __init__(self):
            self.calls = []
        async def change_government(self, government_type):
            self.calls.append(("gov", government_type)); return "OK"
        async def spread_religion(self, unit_index):
            self.calls.append(("spread", unit_index)); return "OK"
        async def activate_great_person(self, unit_index):
            self.calls.append(("gp", unit_index)); return "OK"

    gs = GS()
    await dispatch(gs, "change_government", {"government_type": "GOVERNMENT_OLIGARCHY"})
    await dispatch(gs, "spread_religion", {"unit_id": 131074})       # p2 idx2
    await dispatch(gs, "activate_great_person", {"unit_id": 131075})  # p2 idx3
    assert gs.calls == [("gov", "GOVERNMENT_OLIGARCHY"), ("spread", 2), ("gp", 3)]


def test_parity_actions_have_mirrored_verbs():
    from civ_mcp.arena.vocab import LOCAL_TOOL_VERBS
    for name in ("spy_action", "change_government", "spread_religion",
                 "activate_great_person"):
        assert TOOL_REGISTRY[name].verb == name
        assert LOCAL_TOOL_VERBS[name] == name
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/arena/test_registry.py -q`
Expected: FAIL with `KeyError: 'spy_action'`.

- [ ] **Step 3: Implement**

`registry.py` wrappers:

```python
async def _spy_action_text(gs: Any, args: dict[str, Any]) -> str:
    unit_index = _unit_index(args["unit_id"])
    action = str(args["action"])
    if action == "travel":
        return await gs.spy_travel(unit_index, args["target_x"], args["target_y"])
    return await gs.spy_mission(unit_index, action, args["target_x"], args["target_y"])


async def _change_government_text(gs: Any, args: dict[str, Any]) -> str:
    return await gs.change_government(args["government_type"])


async def _spread_religion_text(gs: Any, args: dict[str, Any]) -> str:
    return await gs.spread_religion(_unit_index(args["unit_id"]))


async def _activate_great_person_text(gs: Any, args: dict[str, Any]) -> str:
    return await gs.activate_great_person(_unit_index(args["unit_id"]))
```

ToolDefs at the END of `TOOL_REGISTRY`:

```python
    "spy_action": _tool(
        "spy_action",
        "Send a spy to a city (action='travel') or launch a mission (action = "
        "COUNTERSPY, GAIN_SOURCES, SIPHON_FUNDS, STEAL_TECH_BOOST, "
        "SABOTAGE_PRODUCTION, GREAT_WORK_HEIST, RECRUIT_PARTISANS, "
        "NEUTRALIZE_GOVERNOR, FABRICATE_SCANDAL). The spy must already be IN the "
        "target city for missions: travel first, end turn, then launch.",
        {
            "unit_id": _int_param("Spy composite id from get_spies"),
            "action": _str_param("'travel' or a mission type"),
            "target_x": _int_param("Target city tile X"),
            "target_y": _int_param("Target city tile Y"),
        },
        ("unit_id", "action", "target_x", "target_y"),
        _spy_action_text,
        verb="spy_action",
    ),
    "change_government": _tool(
        "change_government",
        "Switch to a new government (e.g. GOVERNMENT_OLIGARCHY). The first switch "
        "after unlocking a tier is free.",
        {"government_type": _str_param("GOVERNMENT_* type id")},
        ("government_type",),
        _change_government_text,
        verb="change_government",
    ),
    "spread_religion": _tool(
        "spread_religion",
        "Spend a missionary/apostle charge to spread its religion to the city it "
        "stands in or adjacent to.",
        {"unit_id": _int_param("Religious unit composite id from get_units")},
        ("unit_id",),
        _spread_religion_text,
        verb="spread_religion",
    ),
    "activate_great_person": _tool(
        "activate_great_person",
        "Activate a Great Person standing on its matching completed district. "
        "The error message lists requirements if activation fails.",
        {"unit_id": _int_param("Great Person composite id from get_units")},
        ("unit_id",),
        _activate_great_person_text,
        verb="activate_great_person",
    ),
```

`vocab.py` — append inside `LOCAL_TOOL_VERBS`:

```python
    "spy_action": "spy_action",
    "change_government": "change_government",
    "spread_religion": "spread_religion",
    "activate_great_person": "activate_great_person",
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/arena/test_registry.py tests/arena/test_analyze.py -q` then `uv run pytest tests/ -q`
Expected: PASS. If `test_analyze` mirror test fails, a verb/vocab entry is missing — fix, don't skip.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/registry.py src/civ_mcp/arena/vocab.py tests/arena/test_registry.py
git commit -m "feat(arena): parity action tools spy_action/change_government/spread_religion/activate_great_person"
```

---

### Task 4: Capability snapshot — `arena/capabilities.py`

**Files:**
- Create: `src/civ_mcp/arena/capabilities.py`
- Test: `tests/arena/test_capabilities.py` (new)

**Interfaces:**
- Consumes: nothing (pure module: Lua string + parser).
- Produces: `CAP_FLAGS: tuple[str, ...]`, `build_caps_query(player_id: int) -> str`, `parse_caps(lines: list[str] | None) -> dict[str, bool] | None`. Consumed by Task 5 (`filter_tools`) and Task 7 (coordinator).

- [ ] **Step 1: Write the failing tests**

Create `tests/arena/test_capabilities.py`:

```python
"""Per-turn capability snapshot: CAPS| line -> flags dict, fail-open everywhere."""
from civ_mcp.arena.capabilities import CAP_FLAGS, build_caps_query, parse_caps


CAPS_LINE = ("CAPS|spies=0|government=1|religious_unit=0|gp_unit=1|corps=0"
             "|army=0|air=0|archaeology=0|great_works=1")


def test_cap_flags_inventory():
    assert CAP_FLAGS == ("spies", "government", "religious_unit", "gp_unit",
                         "corps", "army", "air", "archaeology", "great_works")


def test_build_caps_query_shape():
    lua = build_caps_query(3)
    assert "Players[3]" in lua                    # explicit pid, not GetLocalPlayer
    assert "HasCivic" in lua
    assert "MilitaryFormationTypes" in lua
    assert "pcall" in lua                         # per-check fail-open
    assert "---END---" in lua
    assert "CAPS|" in lua


def test_parse_caps_happy_path():
    flags = parse_caps([CAPS_LINE, "---END---"])
    assert flags == {"spies": False, "government": True, "religious_unit": False,
                     "gp_unit": True, "corps": False, "army": False, "air": False,
                     "archaeology": False, "great_works": True}


def test_parse_caps_fail_open_paths():
    assert parse_caps(None) is None
    assert parse_caps([]) is None
    assert parse_caps(["LUA ERROR: nope"]) is None
    # partial line: unknown keys skipped, known keys kept, missing keys absent
    flags = parse_caps(["CAPS|spies=1|bogus=1|government="])
    assert flags == {"spies": True}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/arena/test_capabilities.py -q`
Expected: FAIL with `ModuleNotFoundError: civ_mcp.arena.capabilities`.

- [ ] **Step 3: Implement**

Create `src/civ_mcp/arena/capabilities.py`:

```python
"""Per-turn capability snapshot for era/state-gated tool exposure (spec §1).

One cheap GameCore (execute_read) query per puppet turn emits one CAPS| line
of flag=0/1 fields; parse_caps() turns it into the dict consumed by
registry.filter_tools(). Action tools gate on *executable-now* state (unlock
AND required game objects). Every failure path fails OPEN (flag stays true /
parse returns None -> full toolset): an ungated tool costs invalid-call
churn; an over-closed gate silently removes an ability.
"""
from __future__ import annotations

CAP_FLAGS: tuple[str, ...] = (
    "spies", "government", "religious_unit", "gp_unit",
    "corps", "army", "air", "archaeology", "great_works",
)

# Plain string (lua braces break f-strings), __PID__ substituted at build time.
_CAPS_LUA = """
local me = __PID__
local p = Players[me]
-- fail-open defaults: a check that errors leaves its flag exposed
local flags = {spies=true, government=true, religious_unit=true, gp_unit=true,
               corps=true, army=true, air=true, archaeology=true, great_works=true}
local function civ(name)
    local row = GameInfo.Civics[name]
    if row == nil then return true end
    return p:GetCulture():HasCivic(row.Index)
end
pcall(function() flags.spies = civ("CIVIC_DIPLOMATIC_SERVICE") end)
pcall(function() flags.government = civ("CIVIC_CODE_OF_LAWS") end)
local natl, mob = true, true
pcall(function() natl = civ("CIVIC_NATIONALISM") end)
pcall(function() mob = civ("CIVIC_MOBILIZATION") end)
pcall(function()
    local rel, gpu, air, arch, corpsOwned, pair = false, false, false, false, false, false
    local counts = {}
    for i, u in p:GetUnits():Members() do
        local info = GameInfo.Units[u:GetType()]
        local okC, charges = pcall(function() return u:GetSpreadCharges() end)
        if okC and charges and charges > 0 then rel = true end
        local okG, isGP = pcall(function() return u:GetGreatPerson():IsGreatPerson() end)
        if okG and isGP then gpu = true end
        if info then
            if info.Domain == "DOMAIN_AIR" then air = true end
            local okB, bc = pcall(function() return u:GetBuildCharges() end)
            if info.ExtractsArtifacts and okB and bc and bc > 0 then arch = true end
        end
        local okF, mf = pcall(function() return u:GetMilitaryFormation() end)
        if okF and mf then
            if mf == MilitaryFormationTypes.CORPS_FORMATION then corpsOwned = true end
            if info and info.FormationClass == "FORMATION_CLASS_LAND_COMBAT"
                    and mf == MilitaryFormationTypes.STANDARD_FORMATION then
                counts[info.UnitType] = (counts[info.UnitType] or 0) + 1
                if counts[info.UnitType] >= 2 then pair = true end
            end
        end
    end
    flags.religious_unit = rel
    flags.gp_unit = gpu
    flags.air = air
    flags.archaeology = arch
    flags.corps = natl and pair
    flags.army = mob and corpsOwned
end)
pcall(function()
    -- PROBE(live): great-work count API (Task 15). On error flag stays true.
    local own = false
    for _, c in p:GetCities():Members() do
        local b = c:GetBuildings()
        for row in GameInfo.Buildings() do
            if b:HasBuilding(row.Index) and b:GetNumGreatWorksInBuilding(row.Index) > 0 then
                own = true
            end
        end
    end
    flags.great_works = own
end)
local function b2i(v) if v then return 1 end return 0 end
print(string.format(
    "CAPS|spies=%d|government=%d|religious_unit=%d|gp_unit=%d|corps=%d|army=%d|air=%d|archaeology=%d|great_works=%d",
    b2i(flags.spies), b2i(flags.government), b2i(flags.religious_unit),
    b2i(flags.gp_unit), b2i(flags.corps), b2i(flags.army), b2i(flags.air),
    b2i(flags.archaeology), b2i(flags.great_works)))
print("---END---")
"""


def build_caps_query(player_id: int) -> str:
    return _CAPS_LUA.replace("__PID__", str(int(player_id)))


def parse_caps(lines: list[str] | None) -> dict[str, bool] | None:
    """CAPS| line -> {flag: bool}. Any malformed input returns None (fail open)."""
    if not lines:
        return None
    for line in lines:
        if not line.startswith("CAPS|"):
            continue
        flags: dict[str, bool] = {}
        for field in line[5:].split("|"):
            key, sep, val = field.partition("=")
            if sep and key in CAP_FLAGS and val.strip() in ("0", "1"):
                flags[key] = val.strip() == "1"
        return flags or None
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/arena/test_capabilities.py -q` then `uv run pytest tests/ -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/capabilities.py tests/arena/test_capabilities.py
git commit -m "feat(arena): per-turn capability snapshot (CAPS query + fail-open parser)"
```

---

### Task 5: Registry gating — `ToolDef.requires` + `filter_tools`

**Files:**
- Modify: `src/civ_mcp/arena/registry.py`
- Test: `tests/arena/test_registry.py`

**Interfaces:**
- Consumes: `CAP_FLAGS` from Task 4 (test-side cross-check only — the registry must NOT import capabilities; keep it import-light).
- Produces: `ToolDef.requires: str | None = None`; `_tool(..., requires: str | None = None)` passthrough; `filter_tools(names: Sequence[str], caps: Mapping[str, bool] | None) -> tuple[str, ...]`; `requires` set on the four gated parity tools.

- [ ] **Step 1: Write the failing tests**

Append to `tests/arena/test_registry.py` (add `from civ_mcp.arena.registry import filter_tools` to its imports once implemented):

```python
def test_filter_tools_fail_open_and_gating():
    from civ_mcp.arena.registry import filter_tools
    names = ("get_overview", "get_spies", "spy_action", "change_government")
    # caps=None (snapshot failed): everything exposed
    assert filter_tools(names, None) == names
    # flag false: gated tools dropped, unflagged tools kept
    caps = {"spies": False, "government": True}
    assert filter_tools(names, caps) == ("get_overview", "change_government")
    # missing flag: fail open (exposed)
    assert filter_tools(names, {}) == names


def test_gated_parity_tools_declare_requires():
    assert TOOL_REGISTRY["get_spies"].requires == "spies"
    assert TOOL_REGISTRY["spy_action"].requires == "spies"
    assert TOOL_REGISTRY["change_government"].requires == "government"
    assert TOOL_REGISTRY["spread_religion"].requires == "religious_unit"
    assert TOOL_REGISTRY["activate_great_person"].requires == "gp_unit"
    # readouts stay ungated
    assert TOOL_REGISTRY["get_strategic_map"].requires is None
    assert TOOL_REGISTRY["get_notifications"].requires is None


def test_every_requires_flag_exists_in_snapshot():
    """A ToolDef gating on a flag the snapshot never emits would fail open
    forever (silently ungated). Pin the mirror."""
    from civ_mcp.arena.capabilities import CAP_FLAGS
    used = {t.requires for t in TOOL_REGISTRY.values() if t.requires is not None}
    assert used <= set(CAP_FLAGS), used - set(CAP_FLAGS)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/arena/test_registry.py -q`
Expected: FAIL with `ImportError: cannot import name 'filter_tools'`.

- [ ] **Step 3: Implement**

In `registry.py`: extend the dataclass and `_tool`:

```python
@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    params: dict[str, dict[str, Any]]
    required: tuple[str, ...]
    call: Callable[[Any, dict[str, Any]], Awaitable[str]]
    # Analysis verb for action tools (e.g. "move" for move_unit); "" for query
    # tools. The registry is the single source of truth for the tool->verb map;
    # arena.vocab.LOCAL_TOOL_VERBS mirrors it (test-enforced) to stay import-light.
    verb: str = ""
    # Capability-snapshot flag gating this tool's exposure (spec §1);
    # None = always exposed. Flag names live in arena.capabilities.CAP_FLAGS.
    requires: str | None = None
```

```python
def _tool(
    name: str,
    description: str,
    params: dict[str, dict[str, Any]] | None,
    required: Sequence[str],
    call: Callable[[Any, dict[str, Any]], Awaitable[str]],
    *,
    verb: str = "",
    requires: str | None = None,
) -> ToolDef:
    return ToolDef(
        name=name,
        description=description,
        params=params or {},
        required=tuple(required),
        call=call,
        verb=verb,
        requires=requires,
    )
```

Add `Mapping` to the `typing` import line. Below `openai_tools`:

```python
def filter_tools(
    names: Sequence[str], caps: "Mapping[str, bool] | None"
) -> tuple[str, ...]:
    """Drop tools whose `requires` flag is false in the capability snapshot.

    caps=None (snapshot failed/absent) and missing flags both FAIL OPEN:
    over-exposing costs invalid-call churn; over-closing silently removes an
    ability (spec §1).
    """
    resolved = resolve_tools(names)
    if caps is None:
        return resolved
    return tuple(
        n for n in resolved
        if TOOL_REGISTRY[n].requires is None
        or caps.get(TOOL_REGISTRY[n].requires, True)
    )
```

Add `requires=` to the four Task-2/3 ToolDefs: `get_spies` and `spy_action` get `requires="spies"`, `change_government` gets `requires="government"`, `spread_religion` gets `requires="religious_unit"`, `activate_great_person` gets `requires="gp_unit"`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/arena/test_registry.py -q` then `uv run pytest tests/ -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/registry.py tests/arena/test_registry.py
git commit -m "feat(arena): ToolDef.requires + filter_tools capability gating"
```

---

### Task 6: Agent — one visible list for schema, classification, and dispatch

**Files:**
- Modify: `src/civ_mcp/arena/agent.py`
- Test: `tests/arena/test_agent.py`

**Interfaces:**
- Consumes: `filter_tools` (Task 5).
- Produces: `LLMPolicy.__call__(..., caps: dict | None = None)`; new invalid-call reason `"gated"`. Consumed by Task 7 (coordinator passes `caps=`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/arena/test_agent.py`:

```python
class FakeBackendCapturesTools:
    """Records the tools schema passed to chat; calls a gated tool once."""
    def __init__(self):
        self.n = 0
        self.seen_tools = None

    async def chat(self, messages, tools):
        self.n += 1
        self.seen_tools = tools
        if self.n == 1:
            return Reply(text=None, tool_calls=[
                {"id": "1", "name": "get_spies", "arguments": "{}"}],
                prompt_tokens=5, completion_tokens=5)
        return Reply(text="done", tool_calls=[], prompt_tokens=1, completion_tokens=1)


class FakeGSSpies(FakeGS):
    def __init__(self):
        super().__init__()
        self.spy_calls = 0
    async def get_spies(self):
        self.spy_calls += 1
        return "1 spy"


@pytest.mark.asyncio
async def test_caps_gate_schema_classification_and_dispatch():
    gs, be, cost = FakeGSSpies(), FakeBackendCapturesTools(), FakeCost()
    opts = CivOptions(max_steps=3, tools=["fortify_unit", "get_spies"])
    pol = LLMPolicy(be, cost, options=opts)
    out = await pol(gs, player_id=1, turn=2, caps={"spies": False})
    # schema: gated tool absent
    names = {t["function"]["name"] for t in be.seen_tools}
    assert names == {"fortify_unit"}
    # dispatch: gated tool never executed
    assert gs.spy_calls == 0
    # classification: recorded as gated, not unknown/out_of_tier
    invalid = out["transcript"]["invalid_tool_calls"]
    assert invalid == [{"tool_name": "get_spies", "arguments": "{}", "reason": "gated"}]


@pytest.mark.asyncio
async def test_no_caps_means_full_tier_unchanged():
    gs, be, cost = FakeGSSpies(), FakeBackendCapturesTools(), FakeCost()
    opts = CivOptions(max_steps=3, tools=["fortify_unit", "get_spies"])
    pol = LLMPolicy(be, cost, options=opts)
    await pol(gs, player_id=1, turn=2)          # caps omitted -> fail open
    names = {t["function"]["name"] for t in be.seen_tools}
    assert names == {"fortify_unit", "get_spies"}
    assert gs.spy_calls == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/arena/test_agent.py -q`
Expected: first test FAILS (`TypeError: unexpected keyword argument 'caps'`).

- [ ] **Step 3: Implement**

In `agent.py`: add `filter_tools` to the registry import list. Change `__call__`'s signature and body:

```python
    async def __call__(
        self,
        gs,
        player_id: int,
        turn: int,
        *,
        memory_block: str = "",
        task_block: str = "",
        briefing: Briefing | None = None,
        caps: dict | None = None,
    ) -> dict:
        # One visible list feeds schema, invalid-call classification, AND the
        # dispatch allowlist (spec §1): filtering only the schema would leave
        # gated tools silently callable. caps=None fails open to the full tier.
        visible_names = filter_tools(self._tool_names, caps)
        tools_schema = openai_tools(visible_names)
```

Then, inside the existing body, three mechanical replacements:

1. `tool_schema_chars = len(json.dumps(self._tools))` → `tool_schema_chars = len(json.dumps(tools_schema))`
2. `reply = await self.backend.chat(messages, self._tools)` → `reply = await self.backend.chat(messages, tools_schema)`
3. The classification block becomes:

```python
            for tc in reply.tool_calls:
                if tc["name"] not in visible_names:
                    if tc["name"] in self._tool_names:
                        reason = "gated"
                    elif tc["name"] in TOOL_REGISTRY:
                        reason = "out_of_tier"
                    else:
                        reason = "unknown_tool"
                    invalid_tool_calls.append({"tool_name": tc["name"],
                                               "arguments": tc["arguments"],
                                               "reason": reason})
```

4. The dispatch call: `result = await _dispatch(gs, tc["name"], tc["arguments"], self._tool_names)` → `result = await _dispatch(gs, tc["name"], tc["arguments"], visible_names)`

(`self._tool_names` / `self._tools` stay as the unfiltered baseline built in `__init__` — do not remove them.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/arena/test_agent.py -q` then `uv run pytest tests/ -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/agent.py tests/arena/test_agent.py
git commit -m "feat(arena): agent gates schema, classification, and dispatch on capability snapshot"
```

---

### Task 7: Coordinator — fire the snapshot each puppet turn, fail open

**Files:**
- Modify: `src/civ_mcp/arena/coordinator.py` (adjacent to the `policy_kwargs` construction at ~line 237)
- Test: `tests/arena/test_capabilities.py`

**Interfaces:**
- Consumes: `build_caps_query`, `parse_caps` (Task 4); existing `_policy_accepts_kwarg(pol, name)`; `conn.execute_read`.
- Produces: `policy_kwargs["caps"]` (dict) when the snapshot succeeds AND the policy accepts the kwarg; nothing otherwise. Pre-slice-4 policies keep working (signature gate, the briefing precedent).

- [ ] **Step 1: Write the failing test**

Append to `tests/arena/test_capabilities.py`:

```python
import asyncio as _aio

import pytest

from civ_mcp.arena.config import ArenaConfig, PlayerSpec
from civ_mcp.arena.coordinator import run_arena

from .test_coordinator import FakeConn, FakeGS   # same pattern as test_orphan_sweep


CAPTURE_POLLS = [
    ["LOCAL|1", "TURN|2", "ACTIVE|true", "LAST|1"],
    ["LOCAL|0", "TURN|2", "ACTIVE|false", "LAST|1"],
]


class CapsConn(FakeConn):
    def __init__(self, caps_lines=None, raise_on_caps=False):
        super().__init__()
        self.caps_lines = caps_lines or [CAPS_LINE, "---END---"]
        self.raise_on_caps = raise_on_caps

    async def execute_read(self, lua, timeout=5.0):
        if "CAPS|" in lua:
            if self.raise_on_caps:
                raise ConnectionError("read context dead")
            return self.caps_lines
        return await super().execute_read(lua, timeout=timeout)


class CapsRecordingPolicy:
    def __init__(self):
        self.received = "NOT_CALLED"

    async def __call__(self, gs, player_id, turn, *, caps=None, **kw):
        self.received = caps
        return {"summary": "ok", "actions": []}


def _cfg():
    return ArenaConfig(players=[PlayerSpec(1, "local", "m")], max_puppet_turns=1,
                       dry_run=True, puppet_ids=[1], idle_poll_limit=10)


@pytest.mark.asyncio
async def test_coordinator_passes_parsed_caps_to_policy(monkeypatch):
    async def noop(_d): pass
    monkeypatch.setattr(_aio, "sleep", noop)
    conn = CapsConn()
    conn._polls = iter(CAPTURE_POLLS)
    pol = CapsRecordingPolicy()
    await run_arena(conn, FakeGS(), _cfg(), policy=pol)
    assert pol.received == parse_caps([CAPS_LINE])


@pytest.mark.asyncio
async def test_snapshot_failure_fails_open_and_run_continues(monkeypatch):
    async def noop(_d): pass
    monkeypatch.setattr(_aio, "sleep", noop)
    conn = CapsConn(raise_on_caps=True)
    conn._polls = iter(CAPTURE_POLLS)
    pol = CapsRecordingPolicy()
    result = await run_arena(conn, FakeGS(), _cfg(), policy=pol)
    assert pol.received is None            # kwarg default: full toolset
    assert result["puppet_turns_played"] == 1
```

If `FakeConn` has no `execute_read`, add a base stub to it in `tests/arena/test_coordinator.py`: `async def execute_read(self, lua, timeout=5.0): return []` (keep existing behavior for all other tests).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/arena/test_capabilities.py -q`
Expected: both new tests FAIL — the policy is called but `caps` stays at its `None` default because the coordinator never fetches, so the first test's `pol.received == parse_caps(...)` assertion fails (and the second passes only by accident until the first is green — implement before trusting it).

- [ ] **Step 3: Implement**

In `coordinator.py`: add the import near the other arena imports:

```python
from civ_mcp.arena.capabilities import build_caps_query, parse_caps
```

Immediately AFTER the existing `policy_kwargs = {...}` comprehension (coordinator.py:237-245) and BEFORE the briefing block, insert:

```python
                # Capability snapshot (spec §1): once per puppet turn, cheap
                # GameCore read. Signature-gated like every injected kwarg;
                # ANY failure fails open (no kwarg -> agent uses full tier).
                if _policy_accepts_kwarg(pol, "caps"):
                    caps = None
                    try:
                        cap_lines = await conn.execute_read(
                            build_caps_query(st.local)
                        )
                        caps = parse_caps(cap_lines)
                        if caps is None:
                            print(
                                "[arena] capability snapshot unparseable; "
                                "fail-open full toolset",
                                file=sys.stderr,
                            )
                    except Exception as e:
                        print(
                            f"[arena] capability snapshot failed; "
                            f"fail-open full toolset: {e!r}",
                            file=sys.stderr,
                        )
                    if caps is not None:
                        policy_kwargs["caps"] = caps
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/arena/test_capabilities.py tests/arena/test_coordinator.py tests/arena/test_orphan_sweep.py -q` then `uv run pytest tests/ -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/coordinator.py tests/arena/test_capabilities.py tests/arena/test_coordinator.py
git commit -m "feat(arena): coordinator fires capability snapshot per puppet turn, fail-open"
```

---

### Task 8: System — gossip & grievances (`get_gossip`, always-on)

**Files:**
- Modify: `src/civ_mcp/lua/models.py`, `src/civ_mcp/lua/diplomacy.py`, `src/civ_mcp/lua/__init__.py`, `src/civ_mcp/game_state.py`, `src/civ_mcp/narrate.py`, `src/civ_mcp/server.py`, `src/civ_mcp/arena/registry.py`
- Test: `tests/test_parsers.py`, `tests/arena/test_registry.py`

**Interfaces:**
- Consumes: existing grievance access pattern (`GetDiplomaticAI():GetGrievancesAgainst`, already parsed in `diplomacy.py:40`); `_render`, `_tool`.
- Produces: `GossipEntry(about_player: int, turn: int, text: str)`, `GrievanceRow(player_id: int, name: str, they_hold_against_me: int, i_hold_against_them: int)` in models; `build_gossip_query() -> str`, `parse_gossip_response(lines) -> tuple[list[GrievanceRow], list[GossipEntry]]`; `gs.get_gossip()`; `nr.narrate_gossip(data)`; MCP tool + registry tool `get_gossip` (no `requires`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_parsers.py` (add `from civ_mcp.lua.diplomacy import parse_gossip_response` to imports):

```python
class TestParseGossip:
    def test_grievances_and_gossip(self):
        lines = [
            "GRIEV|1|Gilgamesh|30|0",
            "GRIEV|3|Gandhi|0|15",
            "GOSSIP|1|41|Gilgamesh started building the Pyramids.",
            "---END---",
        ]
        grievances, gossip = parse_gossip_response(lines)
        assert len(grievances) == 2
        assert grievances[0].player_id == 1
        assert grievances[0].name == "Gilgamesh"
        assert grievances[0].they_hold_against_me == 30
        assert grievances[1].i_hold_against_them == 15
        assert len(gossip) == 1
        assert gossip[0].about_player == 1
        assert gossip[0].turn == 41
        assert "Pyramids" in gossip[0].text

    def test_gossip_lines_optional(self):
        """The gossip-log API is a live-probe candidate; grievances alone parse."""
        grievances, gossip = parse_gossip_response(["GRIEV|1|Gilgamesh|5|5"])
        assert len(grievances) == 1 and gossip == []

    def test_malformed_rows_skipped(self):
        grievances, gossip = parse_gossip_response(
            ["GRIEV|x|bad|row", "GOSSIP|notanint|q|t", "junk"])
        assert grievances == [] and gossip == []
```

Append to `tests/arena/test_registry.py`:

```python
@pytest.mark.asyncio
async def test_get_gossip_registered_ungated():
    assert TOOL_REGISTRY["get_gossip"].requires is None

    class GS:
        async def get_gossip(self):
            return "Gilgamesh holds 30 grievances against you"
    assert "grievances" in await dispatch(GS(), "get_gossip", {})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_parsers.py tests/arena/test_registry.py -q`
Expected: FAIL with `ImportError: parse_gossip_response` / `KeyError: 'get_gossip'`.

- [ ] **Step 3: Implement**

`models.py` — append:

```python
@dataclass
class GrievanceRow:
    player_id: int
    name: str
    they_hold_against_me: int
    i_hold_against_them: int


@dataclass
class GossipEntry:
    about_player: int
    turn: int
    text: str
```

`diplomacy.py` — append:

```python
_GOSSIP_LUA = """
local me = Game.GetLocalPlayer()
local myDiplo = Players[me]:GetDiplomacy()
for pid = 0, 63 do
    local pl = Players[pid]
    if pid ~= me and pl and pl:IsAlive() and pl:IsMajor()
            and myDiplo:HasMet(pid) then
        local name = "Unknown"
        pcall(function()
            name = Locale.Lookup(PlayerConfigurations[pid]:GetLeaderName())
        end)
        local theirs, mine = 0, 0
        pcall(function()
            theirs = Players[pid]:GetDiplomaticAI():GetGrievancesAgainst(me)
        end)
        pcall(function()
            mine = Players[me]:GetDiplomaticAI():GetGrievancesAgainst(pid)
        end)
        print("GRIEV|" .. pid .. "|" .. name .. "|" .. theirs .. "|" .. mine)
        pcall(function()
            -- PROBE(live): gossip-log query API (Task 15). Gossip normally
            -- arrives as push events; if no retroactive query exists this
            -- block prints nothing and the tool degrades to grievances-only
            -- (degrade decision recorded in the spec).
            local gm = Game.GetGossipManager()
            local turn = Game.GetCurrentGameTurn()
            for _, entry in ipairs(gm:GetRecentVisibleGossipStrings(me, pid)) do
                print("GOSSIP|" .. pid .. "|" .. turn .. "|" .. tostring(entry))
            end
        end)
    end
end
print("{SENTINEL}")
"""


def build_gossip_query() -> str:
    """InGame context: grievances both directions per met major + gossip log."""
    return _GOSSIP_LUA.replace("{SENTINEL}", SENTINEL)


def parse_gossip_response(
    lines: list[str],
) -> tuple[list[GrievanceRow], list[GossipEntry]]:
    grievances: list[GrievanceRow] = []
    gossip: list[GossipEntry] = []
    for line in lines:
        parts = line.split("|")
        try:
            if parts[0] == "GRIEV" and len(parts) >= 5:
                grievances.append(GrievanceRow(
                    player_id=int(parts[1]), name=parts[2],
                    they_hold_against_me=int(parts[3]),
                    i_hold_against_them=int(parts[4])))
            elif parts[0] == "GOSSIP" and len(parts) >= 4:
                gossip.append(GossipEntry(
                    about_player=int(parts[1]), turn=int(parts[2]),
                    text="|".join(parts[3:])))
        except (ValueError, IndexError):
            continue
    return grievances, gossip
```

Add `GrievanceRow, GossipEntry` to the `models` import at the top of `diplomacy.py` (it already imports from `civ_mcp.lua.models`), and add `build_gossip_query`, `parse_gossip_response` to the diplomacy block and `GossipEntry`, `GrievanceRow` to the models block in `lua/__init__.py`.

`game_state.py` — append near the other diplomacy methods:

```python
    async def get_gossip(self) -> tuple[list[lq.GrievanceRow], list[lq.GossipEntry]]:
        # InGame context: DiplomaticAI/gossip access
        lines = await self.conn.execute_write(lq.build_gossip_query())
        return lq.parse_gossip_response(lines)
```

`narrate.py` — append:

```python
def narrate_gossip(
    data: tuple[list[lq.GrievanceRow], list[lq.GossipEntry]],
) -> str:
    grievances, gossip = data
    if not grievances and not gossip:
        return "No grievances and no gossip: you have met no other majors yet."
    out = ["=== GRIEVANCES ==="]
    for g in grievances:
        out.append(
            f"{g.name} (player {g.player_id}): holds {g.they_hold_against_me} "
            f"against you; you hold {g.i_hold_against_them} against them"
        )
    if gossip:
        out.append("=== RECENT GOSSIP ===")
        for e in gossip[-20:]:
            out.append(f"[T{e.turn}] (about player {e.about_player}) {e.text}")
    return "\n".join(out)
```

`server.py` — add after `get_diplomacy`'s tool:

```python
@mcp.tool(annotations={"readOnlyHint": True})
async def get_gossip(ctx: Context) -> str:
    """Grievances (both directions) per met civ, plus recent gossip about them.

    Grievances predict AI hostility; gossip reveals what rivals are building
    and doing. Check when planning war, peace, or World Congress votes.
    """
    gs = _get_game(ctx)

    async def _run():
        return nr.narrate_gossip(await gs.get_gossip())

    return await _logged(ctx, "get_gossip", {}, _run)
```

`registry.py` — wrapper + ToolDef at END of `TOOL_REGISTRY`:

```python
async def _gossip_text(gs: Any, args: dict[str, Any]) -> str:
    del args
    return _render(await gs.get_gossip(), nr.narrate_gossip)
```

```python
    "get_gossip": _tool(
        "get_gossip",
        "Grievances both directions per met civ plus recent gossip. Grievances "
        "predict AI hostility; check before wars and World Congress votes.",
        None,
        (),
        _gossip_text,
    ),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_parsers.py tests/arena/test_registry.py -q` then `uv run pytest tests/ -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/lua/models.py src/civ_mcp/lua/diplomacy.py src/civ_mcp/lua/__init__.py \
        src/civ_mcp/game_state.py src/civ_mcp/narrate.py src/civ_mcp/server.py \
        src/civ_mcp/arena/registry.py tests/test_parsers.py tests/arena/test_registry.py
git commit -m "feat: get_gossip tool (grievances + gossip log) in both tool layers"
```

---

### Task 9: System — loyalty detail (`get_loyalty`, always-on)

**Files:**
- Modify: `src/civ_mcp/lua/models.py`, `src/civ_mcp/lua/cities.py`, `src/civ_mcp/lua/__init__.py`, `src/civ_mcp/game_state.py`, `src/civ_mcp/narrate.py`, `src/civ_mcp/server.py`, `src/civ_mcp/arena/registry.py`
- Test: `tests/test_parsers.py`, `tests/arena/test_registry.py`

**Interfaces:**
- Produces: `CityLoyalty(city_id: int, name: str, loyalty: float, max_loyalty: float, per_turn: float, sources: list[tuple[str, float]])`; `build_loyalty_query()`, `parse_loyalty_response(lines) -> list[CityLoyalty]`; `gs.get_loyalty()`; `nr.narrate_loyalty(rows)`; tool `get_loyalty` in both layers (no `requires`).

- [ ] **Step 1: Write the failing tests**

`tests/test_parsers.py` (import `parse_loyalty_response` from `civ_mcp.lua.cities`):

```python
class TestParseLoyalty:
    def test_cities_with_sources(self):
        lines = [
            "LOYAL|65792|Lahore|72.5|100.0|-3.25",
            "LOYSRC|65792|Pressure from other civs|-5.5",
            "LOYSRC|65792|Governor|2.25",
            "LOYAL|65793|Multan|100.0|100.0|1.00",
            "---END---",
        ]
        rows = parse_loyalty_response(lines)
        assert len(rows) == 2
        assert rows[0].name == "Lahore"
        assert rows[0].loyalty == 72.5
        assert rows[0].per_turn == -3.25
        assert rows[0].sources == [("Pressure from other civs", -5.5),
                                   ("Governor", 2.25)]
        assert rows[1].sources == []

    def test_orphan_source_and_junk_skipped(self):
        rows = parse_loyalty_response(
            ["LOYSRC|999|orphan|1.0", "LOYAL|bad|X|a|b|c", "noise"])
        assert rows == []
```

`tests/arena/test_registry.py`:

```python
@pytest.mark.asyncio
async def test_get_loyalty_registered_ungated():
    assert TOOL_REGISTRY["get_loyalty"].requires is None

    class GS:
        async def get_loyalty(self):
            return "Lahore: 72.5/100 (-3.25/turn)"
    assert "Lahore" in await dispatch(GS(), "get_loyalty", {})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_parsers.py tests/arena/test_registry.py -q`
Expected: FAIL with ImportError / KeyError.

- [ ] **Step 3: Implement**

`models.py`:

```python
@dataclass
class CityLoyalty:
    city_id: int
    name: str
    loyalty: float
    max_loyalty: float
    per_turn: float
    sources: list[tuple[str, float]] = field(default_factory=list)
```

(`models.py` already imports `field`; verify, add if missing.)

`cities.py`:

```python
_LOYALTY_LUA = """
local me = Game.GetLocalPlayer()
for _, c in Players[me]:GetCities():Members() do
    local ok = pcall(function()
        local ci = c:GetCulturalIdentity()
        local cur, mx, pt = 0, 100, 0
        pcall(function() cur = ci:GetLoyalty() end)
        pcall(function() mx = ci:GetMaxLoyalty() end)
        pcall(function() pt = ci:GetLoyaltyPerTurn() end)
        print(string.format("LOYAL|%d|%s|%.2f|%.2f|%.2f",
            c:GetID(), Locale.Lookup(c:GetName()), cur, mx, pt))
        pcall(function()
            -- PROBE(live): per-source breakdown API (Task 15). Base numbers
            -- above are solid; if this errors the city simply has no LOYSRC
            -- lines and the tool degrades to totals-only.
            local breakdown = ci:GetLoyaltyBreakdown()
            for _, src in ipairs(breakdown) do
                print(string.format("LOYSRC|%d|%s|%.2f",
                    c:GetID(), tostring(src.Name), src.Amount))
            end
        end)
    end)
    if not ok then
        print("LOYAL|" .. c:GetID() .. "|ERROR|0|100|0")
    end
end
print("{SENTINEL}")
"""


def build_loyalty_query() -> str:
    """InGame context: per-city loyalty, per-turn delta, source breakdown."""
    return _LOYALTY_LUA.replace("{SENTINEL}", SENTINEL)


def parse_loyalty_response(lines: list[str]) -> list[CityLoyalty]:
    rows: list[CityLoyalty] = []
    by_id: dict[int, CityLoyalty] = {}
    for line in lines:
        parts = line.split("|")
        try:
            if parts[0] == "LOYAL" and len(parts) >= 6:
                row = CityLoyalty(
                    city_id=int(parts[1]), name=parts[2],
                    loyalty=float(parts[3]), max_loyalty=float(parts[4]),
                    per_turn=float(parts[5]))
                rows.append(row)
                by_id[row.city_id] = row
            elif parts[0] == "LOYSRC" and len(parts) >= 4:
                row = by_id.get(int(parts[1]))
                if row is not None:
                    row.sources.append((parts[2], float(parts[3])))
        except (ValueError, IndexError):
            continue
    return rows
```

Add `CityLoyalty` to `cities.py`'s models import; export `build_loyalty_query`, `parse_loyalty_response`, `CityLoyalty` from `lua/__init__.py`. Check `cities.py` imports `SENTINEL` from `_helpers` (it does for other builders; add if missing).

`game_state.py`:

```python
    async def get_loyalty(self) -> list[lq.CityLoyalty]:
        lines = await self.conn.execute_write(lq.build_loyalty_query())
        return lq.parse_loyalty_response(lines)
```

`narrate.py`:

```python
def narrate_loyalty(rows: list[lq.CityLoyalty]) -> str:
    if not rows:
        return "No cities."
    out = ["=== LOYALTY ==="]
    for r in rows:
        trend = "+" if r.per_turn >= 0 else ""
        flag = ""
        if r.per_turn < 0:
            turns = int(r.loyalty / -r.per_turn) if r.per_turn else 0
            flag = f"  !! LOSING LOYALTY (~{turns} turns to revolt)"
        out.append(f"{r.name} (id {r.city_id}): {r.loyalty:.0f}/{r.max_loyalty:.0f} "
                   f"({trend}{r.per_turn:.2f}/turn){flag}")
        for name, amount in r.sources:
            sign = "+" if amount >= 0 else ""
            out.append(f"    {name}: {sign}{amount:.2f}")
    return "\n".join(out)
```

`server.py`:

```python
@mcp.tool(annotations={"readOnlyHint": True})
async def get_loyalty(ctx: Context) -> str:
    """Per-city loyalty with per-turn trend and pressure sources.

    A city trending negative will revolt and flip; assign a governor or
    fix amenities before it does. get_cities shows only the summary number.
    """
    gs = _get_game(ctx)

    async def _run():
        return nr.narrate_loyalty(await gs.get_loyalty())

    return await _logged(ctx, "get_loyalty", {}, _run)
```

`registry.py`:

```python
async def _loyalty_text(gs: Any, args: dict[str, Any]) -> str:
    del args
    return _render(await gs.get_loyalty(), nr.narrate_loyalty)
```

```python
    "get_loyalty": _tool(
        "get_loyalty",
        "Per-city loyalty: current/max, per-turn trend, and pressure sources. "
        "A negative trend means the city will eventually revolt and flip.",
        None,
        (),
        _loyalty_text,
    ),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_parsers.py tests/arena/test_registry.py -q` then `uv run pytest tests/ -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/lua/models.py src/civ_mcp/lua/cities.py src/civ_mcp/lua/__init__.py \
        src/civ_mcp/game_state.py src/civ_mcp/narrate.py src/civ_mcp/server.py \
        src/civ_mcp/arena/registry.py tests/test_parsers.py tests/arena/test_registry.py
git commit -m "feat: get_loyalty tool (per-city loyalty detail) in both tool layers"
```

---

### Task 10: System — climate & disasters (`get_climate`, always-on)

**Files:**
- Create: `src/civ_mcp/lua/climate.py`
- Modify: `src/civ_mcp/lua/models.py`, `src/civ_mcp/lua/__init__.py`, `src/civ_mcp/game_state.py`, `src/civ_mcp/narrate.py`, `src/civ_mcp/server.py`, `src/civ_mcp/arena/registry.py`
- Test: `tests/test_parsers.py`, `tests/arena/test_registry.py`

**Interfaces:**
- Produces: `ClimateStatus(phase: int, sea_level: int, co2_total: int, disasters: list[DisasterEvent])`, `DisasterEvent(kind: str, x: int, y: int, turn: int)`; `build_climate_query()`, `parse_climate_response(lines) -> ClimateStatus`; `gs.get_climate()`; `nr.narrate_climate(status)`; tool `get_climate` in both layers (no `requires`). `phase == -1` means "climate system unavailable" (base game / API missing) — an explicit degrade value, not an error.

- [ ] **Step 1: Write the failing tests**

`tests/test_parsers.py` (import `parse_climate_response` from `civ_mcp.lua.climate`):

```python
class TestParseClimate:
    def test_full_status(self):
        lines = [
            "CLIMATE|2|1|317",
            "DISASTER|STORM_HURRICANE|14|22|43",
            "DISASTER|RANDOM_EVENT_VOLCANO_ERUPTION|9|8|40",
            "---END---",
        ]
        st = parse_climate_response(lines)
        assert st.phase == 2 and st.sea_level == 1 and st.co2_total == 317
        assert len(st.disasters) == 2
        assert st.disasters[0].kind == "STORM_HURRICANE"
        assert (st.disasters[0].x, st.disasters[0].y) == (14, 22)
        assert st.disasters[1].turn == 40

    def test_unavailable_climate_system(self):
        st = parse_climate_response(["CLIMATE|-1|-1|-1"])
        assert st.phase == -1 and st.disasters == []

    def test_empty_is_unavailable(self):
        st = parse_climate_response([])
        assert st.phase == -1
```

`tests/arena/test_registry.py`:

```python
@pytest.mark.asyncio
async def test_get_climate_registered_ungated():
    assert TOOL_REGISTRY["get_climate"].requires is None

    class GS:
        async def get_climate(self):
            return "Climate phase 2, sea level +1"
    assert "phase" in await dispatch(GS(), "get_climate", {})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_parsers.py tests/arena/test_registry.py -q`
Expected: FAIL with `ModuleNotFoundError: civ_mcp.lua.climate` / KeyError.

- [ ] **Step 3: Implement**

`models.py`:

```python
@dataclass
class DisasterEvent:
    kind: str
    x: int
    y: int
    turn: int


@dataclass
class ClimateStatus:
    phase: int          # -1 = climate system unavailable (base game / API missing)
    sea_level: int
    co2_total: int
    disasters: list[DisasterEvent] = field(default_factory=list)
```

Create `src/civ_mcp/lua/climate.py`:

```python
"""Climate & disasters domain (Gathering Storm systems) — greenfield Lua.

Every API call is pcall-guarded; a base game or missing API prints
CLIMATE|-1|-1|-1 so the tool degrades to an explicit "unavailable" readout.
-- PROBE(live): all APIs in this module are validated by the Task 15
live-probe checklist before the tool reaches a live run.
"""
from __future__ import annotations

from civ_mcp.lua._helpers import SENTINEL
from civ_mcp.lua.models import ClimateStatus, DisasterEvent

_CLIMATE_LUA = """
local phase, sea, co2 = -1, -1, -1
pcall(function() phase = GameClimate.GetClimateChangeLevel() end)
pcall(function() sea = GameClimate.GetSeaLevel() end)
pcall(function() co2 = GameClimate.GetTotalCO2Footprint() end)
print("CLIMATE|" .. phase .. "|" .. sea .. "|" .. co2)
pcall(function()
    -- PROBE(live): recent random events / active storms query (Task 15)
    local events = Game.GetRandomEventsManager():GetActiveEvents()
    for _, ev in ipairs(events) do
        print("DISASTER|" .. tostring(ev.Type) .. "|" .. ev.X .. "|" .. ev.Y
              .. "|" .. Game.GetCurrentGameTurn())
    end
end)
print("{SENTINEL}")
"""


def build_climate_query() -> str:
    """InGame context: climate phase, sea level, CO2, active disasters."""
    return _CLIMATE_LUA.replace("{SENTINEL}", SENTINEL)


def parse_climate_response(lines: list[str]) -> ClimateStatus:
    status = ClimateStatus(phase=-1, sea_level=-1, co2_total=-1)
    for line in lines:
        parts = line.split("|")
        try:
            if parts[0] == "CLIMATE" and len(parts) >= 4:
                status.phase = int(parts[1])
                status.sea_level = int(parts[2])
                status.co2_total = int(parts[3])
            elif parts[0] == "DISASTER" and len(parts) >= 5:
                status.disasters.append(DisasterEvent(
                    kind=parts[1], x=int(parts[2]), y=int(parts[3]),
                    turn=int(parts[4])))
        except (ValueError, IndexError):
            continue
    return status
```

`lua/__init__.py` — add a climate block:

```python
from civ_mcp.lua.climate import (  # noqa: F401
    build_climate_query,
    parse_climate_response,
)
```

and `ClimateStatus`, `DisasterEvent` to the models re-export list.

`game_state.py`:

```python
    async def get_climate(self) -> lq.ClimateStatus:
        lines = await self.conn.execute_write(lq.build_climate_query())
        return lq.parse_climate_response(lines)
```

`narrate.py`:

```python
def narrate_climate(status: lq.ClimateStatus) -> str:
    if status.phase < 0:
        return ("Climate system unavailable (base-game ruleset or API not "
                "present). No climate pressure to manage.")
    out = [f"Climate change phase {status.phase}, sea level +{status.sea_level}, "
           f"total CO2 {status.co2_total}"]
    if status.disasters:
        out.append("Active/recent disasters:")
        for d in status.disasters:
            out.append(f"  {d.kind} at ({d.x},{d.y}) turn {d.turn}")
    else:
        out.append("No active disasters.")
    return "\n".join(out)
```

`server.py`:

```python
@mcp.tool(annotations={"readOnlyHint": True})
async def get_climate(ctx: Context) -> str:
    """Climate phase, sea level, CO2, and active disasters (Gathering Storm).

    Rising phases flood coastal tiles and power disasters; check before
    settling coasts or river floodplains.
    """
    gs = _get_game(ctx)

    async def _run():
        return nr.narrate_climate(await gs.get_climate())

    return await _logged(ctx, "get_climate", {}, _run)
```

`registry.py`:

```python
async def _climate_text(gs: Any, args: dict[str, Any]) -> str:
    del args
    return _render(await gs.get_climate(), nr.narrate_climate)
```

```python
    "get_climate": _tool(
        "get_climate",
        "Climate phase, sea level, CO2, active disasters. Rising sea levels "
        "flood coastal tiles; storms pillage improvements.",
        None,
        (),
        _climate_text,
    ),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_parsers.py tests/arena/test_registry.py -q` then `uv run pytest tests/ -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/lua/climate.py src/civ_mcp/lua/models.py src/civ_mcp/lua/__init__.py \
        src/civ_mcp/game_state.py src/civ_mcp/narrate.py src/civ_mcp/server.py \
        src/civ_mcp/arena/registry.py tests/test_parsers.py tests/arena/test_registry.py
git commit -m "feat: get_climate tool (climate/disasters) in both tool layers"
```

---

### Task 11: System — Great Works (`get_great_works` always-on, `move_great_work` gated)

**Files:**
- Create: `src/civ_mcp/lua/great_works.py`
- Modify: `src/civ_mcp/lua/models.py`, `src/civ_mcp/lua/__init__.py`, `src/civ_mcp/game_state.py`, `src/civ_mcp/narrate.py`, `src/civ_mcp/server.py`, `src/civ_mcp/arena/registry.py`, `src/civ_mcp/arena/vocab.py`
- Test: `tests/test_parsers.py`, `tests/arena/test_registry.py`

**Interfaces:**
- Produces: `GreatWorkSlot(city_id: int, city_name: str, building: str, slot_index: int, slot_type: str, work_index: int, work_name: str)` (`work_index == -1` = empty slot); `build_great_works_query()`, `parse_great_works_response(lines) -> list[GreatWorkSlot]`, `build_move_great_work(work_index, target_city_id, building, slot) -> str`; `gs.get_great_works()`, `gs.move_great_work(work_index, target_city_id, building, slot) -> str`; `nr.narrate_great_works(slots)`; tools `get_great_works` (no gate) + `move_great_work` (`requires="great_works"`, verb `"move_great_work"`).

- [ ] **Step 1: Write the failing tests**

`tests/test_parsers.py` (import from `civ_mcp.lua.great_works`):

```python
class TestParseGreatWorks:
    def test_slots_and_works(self):
        lines = [
            "GWSLOT|65792|Lahore|BUILDING_AMPHITHEATER|0|GREATWORKSLOT_WRITING|17|Ramayana",
            "GWSLOT|65792|Lahore|BUILDING_AMPHITHEATER|1|GREATWORKSLOT_WRITING|-1|",
            "---END---",
        ]
        slots = parse_great_works_response(lines)
        assert len(slots) == 2
        assert slots[0].work_index == 17 and slots[0].work_name == "Ramayana"
        assert slots[1].work_index == -1 and slots[1].work_name == ""
        assert slots[1].slot_type == "GREATWORKSLOT_WRITING"

    def test_junk_skipped(self):
        assert parse_great_works_response(["GWSLOT|x|y", "noise"]) == []


def test_build_move_great_work_substitutes_args():
    from civ_mcp.lua.great_works import build_move_great_work
    lua = build_move_great_work(17, 65793, "BUILDING_MUSEUM_ART", 2)
    assert "17" in lua and "65793" in lua and "BUILDING_MUSEUM_ART" in lua
    assert "OK:" in lua and "ERR:" in lua
```

`tests/arena/test_registry.py`:

```python
@pytest.mark.asyncio
async def test_great_works_tools_registered_and_gated():
    assert TOOL_REGISTRY["get_great_works"].requires is None
    assert TOOL_REGISTRY["move_great_work"].requires == "great_works"
    assert TOOL_REGISTRY["move_great_work"].verb == "move_great_work"

    class GS:
        def __init__(self):
            self.calls = []
        async def get_great_works(self):
            return "1 empty writing slot"
        async def move_great_work(self, work_index, target_city_id, building, slot):
            self.calls.append((work_index, target_city_id, building, slot))
            return "OK"

    gs = GS()
    assert "slot" in await dispatch(gs, "get_great_works", {})
    await dispatch(gs, "move_great_work",
                   {"work_id": 17, "target_city_id": 65793,
                    "building": "BUILDING_MUSEUM_ART", "slot": 2})
    assert gs.calls == [(17, 65793, "BUILDING_MUSEUM_ART", 2)]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_parsers.py tests/arena/test_registry.py -q`
Expected: FAIL (module/keys missing).

- [ ] **Step 3: Implement**

`models.py`:

```python
@dataclass
class GreatWorkSlot:
    city_id: int
    city_name: str
    building: str
    slot_index: int
    slot_type: str
    work_index: int     # -1 = empty slot
    work_name: str      # "" when empty
```

Create `src/civ_mcp/lua/great_works.py`:

```python
"""Great Works domain — slots, contents, and moves.

-- PROBE(live): slot/query APIs follow GreatWorksOverview.lua conventions
(GetNumGreatWorkSlots / GetGreatWorkSlotType / GetGreatWorkInSlot /
Game.GetGreatWorkDataFromIndex) and the move API is a best-guess; both are
validated by the Task 15 checklist before the tools reach a live run.
"""
from __future__ import annotations

from civ_mcp.lua._helpers import SENTINEL
from civ_mcp.lua.models import GreatWorkSlot

_GW_QUERY_LUA = """
local me = Game.GetLocalPlayer()
for _, c in Players[me]:GetCities():Members() do
    pcall(function()
        local b = c:GetBuildings()
        local cname = Locale.Lookup(c:GetName())
        for row in GameInfo.Buildings() do
            if b:HasBuilding(row.Index) then
                local nSlots = 0
                pcall(function() nSlots = b:GetNumGreatWorkSlots(row.Index) end)
                for s = 0, nSlots - 1 do
                    pcall(function()
                        local slotType = "UNKNOWN"
                        pcall(function()
                            local st = b:GetGreatWorkSlotType(row.Index, s)
                            slotType = GameInfo.GreatWorkSlotTypes[st].GreatWorkSlotType
                        end)
                        local gwIndex = b:GetGreatWorkInSlot(row.Index, s)
                        local gwName = ""
                        if gwIndex and gwIndex >= 0 then
                            pcall(function()
                                local data = Game.GetGreatWorkDataFromIndex(gwIndex)
                                gwName = Locale.Lookup(data.Name)
                            end)
                        else
                            gwIndex = -1
                        end
                        print("GWSLOT|" .. c:GetID() .. "|" .. cname .. "|"
                              .. row.BuildingType .. "|" .. s .. "|" .. slotType
                              .. "|" .. gwIndex .. "|" .. gwName)
                    end)
                end
            end
        end
    end)
end
print("{SENTINEL}")
"""

_GW_MOVE_LUA = """
local me = Game.GetLocalPlayer()
local workIndex = __WORK__
local targetCityId = __CITY__
local targetBuilding = "__BUILDING__"
local targetSlot = __SLOT__
local ok, err = pcall(function()
    -- locate the work's current slot
    local fromCity, fromBuildingIdx, fromSlot = nil, nil, nil
    for _, c in Players[me]:GetCities():Members() do
        local b = c:GetBuildings()
        for row in GameInfo.Buildings() do
            if b:HasBuilding(row.Index) then
                local n = 0
                pcall(function() n = b:GetNumGreatWorkSlots(row.Index) end)
                for s = 0, n - 1 do
                    if b:GetGreatWorkInSlot(row.Index, s) == workIndex then
                        fromCity, fromBuildingIdx, fromSlot = c, row.Index, s
                    end
                end
            end
        end
    end
    if fromCity == nil then
        print("ERR:great work " .. workIndex .. " not found in any of your slots")
        return
    end
    local toCity = CityManager.GetCity(me, targetCityId % 65536)
    local toRow = GameInfo.Buildings[targetBuilding]
    if toCity == nil or toRow == nil then
        print("ERR:target city or building not found")
        return
    end
    -- PROBE(live): move request API (Task 15)
    UI.MoveGreatWork(fromCity:GetID(), fromBuildingIdx, fromSlot,
                     toCity:GetID(), toRow.Index, targetSlot)
    print("OK:requested move of work " .. workIndex .. " to " .. targetBuilding
          .. " slot " .. targetSlot)
end)
if not ok then print("ERR:" .. tostring(err)) end
print("{SENTINEL}")
"""


def build_great_works_query() -> str:
    """InGame context: every great-work slot you own, with contents."""
    return _GW_QUERY_LUA.replace("{SENTINEL}", SENTINEL)


def build_move_great_work(
    work_index: int, target_city_id: int, building: str, slot: int
) -> str:
    """InGame context: move a great work into a target building slot."""
    if not building.replace("_", "").isalnum():
        raise ValueError(f"suspicious building id: {building!r}")
    return (_GW_MOVE_LUA
            .replace("__WORK__", str(int(work_index)))
            .replace("__CITY__", str(int(target_city_id)))
            .replace("__BUILDING__", building)
            .replace("__SLOT__", str(int(slot)))
            .replace("{SENTINEL}", SENTINEL))


def parse_great_works_response(lines: list[str]) -> list[GreatWorkSlot]:
    slots: list[GreatWorkSlot] = []
    for line in lines:
        parts = line.split("|")
        try:
            if parts[0] == "GWSLOT" and len(parts) >= 8:
                slots.append(GreatWorkSlot(
                    city_id=int(parts[1]), city_name=parts[2], building=parts[3],
                    slot_index=int(parts[4]), slot_type=parts[5],
                    work_index=int(parts[6]), work_name=parts[7]))
        except (ValueError, IndexError):
            continue
    return slots
```

`lua/__init__.py` — new block + models:

```python
from civ_mcp.lua.great_works import (  # noqa: F401
    build_great_works_query,
    build_move_great_work,
    parse_great_works_response,
)
```

`game_state.py`:

```python
    async def get_great_works(self) -> list[lq.GreatWorkSlot]:
        lines = await self.conn.execute_write(lq.build_great_works_query())
        return lq.parse_great_works_response(lines)

    async def move_great_work(
        self, work_index: int, target_city_id: int, building: str, slot: int
    ) -> str:
        lines = await self.conn.execute_write(
            lq.build_move_great_work(work_index, target_city_id, building, slot)
        )
        return _action_result(lines)
```

`narrate.py`:

```python
def narrate_great_works(slots: list[lq.GreatWorkSlot]) -> str:
    if not slots:
        return "No great-work slots yet (build Amphitheaters, Museums, Wonders)."
    out = ["=== GREAT WORK SLOTS ==="]
    for s in slots:
        content = (f"[{s.work_index}] {s.work_name}" if s.work_index >= 0
                   else "(empty)")
        out.append(f"{s.city_name} {s.building} slot {s.slot_index} "
                   f"({s.slot_type}): {content}")
    empty = sum(1 for s in slots if s.work_index < 0)
    out.append(f"{empty} empty slot(s). Theming bonus needs matching works in "
               f"one building; use move_great_work to group them.")
    return "\n".join(out)
```

`server.py` — two tools:

```python
@mcp.tool(annotations={"readOnlyHint": True})
async def get_great_works(ctx: Context) -> str:
    """Every great-work slot you own: city, building, slot type, contents.

    Empty slots waste tourism; mismatched works forgo theming bonuses.
    """
    gs = _get_game(ctx)

    async def _run():
        return nr.narrate_great_works(await gs.get_great_works())

    return await _logged(ctx, "get_great_works", {}, _run)


@mcp.tool()
async def move_great_work(
    ctx: Context, work_id: int, target_city_id: int, building: str, slot: int
) -> str:
    """Move a great work (index from get_great_works) into another slot.

    Args:
        work_id: The work's index from get_great_works output
        target_city_id: Destination city id
        building: Destination building type, e.g. BUILDING_MUSEUM_ART
        slot: Destination slot index (0-based)
    """
    gs = _get_game(ctx)
    params = {"work_id": work_id, "target_city_id": target_city_id,
              "building": building, "slot": slot}
    return await _logged(
        ctx, "move_great_work", params,
        lambda: gs.move_great_work(work_id, target_city_id, building, slot))
```

`registry.py`:

```python
async def _great_works_text(gs: Any, args: dict[str, Any]) -> str:
    del args
    return _render(await gs.get_great_works(), nr.narrate_great_works)


async def _move_great_work_text(gs: Any, args: dict[str, Any]) -> str:
    return await gs.move_great_work(
        args["work_id"], args["target_city_id"], args["building"], args["slot"])
```

```python
    "get_great_works": _tool(
        "get_great_works",
        "All your great-work slots: city, building, slot type, contents. Empty "
        "slots waste tourism; matching works in one building earn theming.",
        None,
        (),
        _great_works_text,
    ),
    "move_great_work": _tool(
        "move_great_work",
        "Move a great work (index from get_great_works) to another building "
        "slot, e.g. to group matching works for a theming bonus.",
        {
            "work_id": _int_param("Work index from get_great_works"),
            "target_city_id": _int_param("Destination city id"),
            "building": _str_param("Destination BUILDING_* type"),
            "slot": _int_param("Destination slot index (0-based)", minimum=0),
        },
        ("work_id", "target_city_id", "building", "slot"),
        _move_great_work_text,
        verb="move_great_work",
        requires="great_works",
    ),
```

`vocab.py` — append `"move_great_work": "move_great_work",` to `LOCAL_TOOL_VERBS`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_parsers.py tests/arena/test_registry.py tests/arena/test_analyze.py -q` then `uv run pytest tests/ -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/lua/great_works.py src/civ_mcp/lua/models.py src/civ_mcp/lua/__init__.py \
        src/civ_mcp/game_state.py src/civ_mcp/narrate.py src/civ_mcp/server.py \
        src/civ_mcp/arena/registry.py src/civ_mcp/arena/vocab.py \
        tests/test_parsers.py tests/arena/test_registry.py
git commit -m "feat: great works tools (get + gated move) in both tool layers"
```

---

### Task 12: System — formations (`form_corps`, `form_army`, gated)

**Files:**
- Modify: `src/civ_mcp/lua/units.py`, `src/civ_mcp/lua/__init__.py`, `src/civ_mcp/game_state.py`, `src/civ_mcp/server.py`, `src/civ_mcp/arena/registry.py`, `src/civ_mcp/arena/vocab.py`
- Test: `tests/test_parsers.py`, `tests/arena/test_registry.py`

**Interfaces:**
- Consumes: `_lua_get_unit(unit_index)` helper from `lua/_helpers.py` (InGame-context unit lookup, used by existing unit action builders — read one existing caller, e.g. `build_fortify_unit`, and match its exact usage/result idiom).
- Produces: `build_form_formation(unit_index: int, merge_unit_index: int, command: str) -> str` where `command` is `"FORM_CORPS"` or `"FORM_ARMY"`; `gs.form_corps(unit_index, merge_unit_index) -> str`, `gs.form_army(unit_index, merge_unit_index) -> str`; MCP tools + registry tools `form_corps`/`form_army` (verbs = names; `requires="corps"`/`"army"`).

- [ ] **Step 1: Write the failing tests**

`tests/test_parsers.py`:

```python
def test_build_form_formation_shape():
    from civ_mcp.lua.units import build_form_formation
    lua = build_form_formation(3, 7, "FORM_CORPS")
    assert "FORM_CORPS" in lua
    assert "PARAM_UNIT_ID" in lua           # merge target passed by id
    assert "CanStartCommand" in lua         # precheck before request
    assert "OK:" in lua and "ERR:" in lua

    with pytest.raises(ValueError):
        build_form_formation(3, 7, "FORM_VOLTRON")
```

`tests/arena/test_registry.py`:

```python
@pytest.mark.asyncio
async def test_formation_tools_registered_gated_composite_ids():
    assert TOOL_REGISTRY["form_corps"].requires == "corps"
    assert TOOL_REGISTRY["form_army"].requires == "army"

    class GS:
        def __init__(self):
            self.calls = []
        async def form_corps(self, unit_index, merge_unit_index):
            self.calls.append(("corps", unit_index, merge_unit_index)); return "OK"
        async def form_army(self, unit_index, merge_unit_index):
            self.calls.append(("army", unit_index, merge_unit_index)); return "OK"

    gs = GS()
    await dispatch(gs, "form_corps", {"unit_id": 65539, "merge_unit_id": 65540})
    await dispatch(gs, "form_army", {"unit_id": 65539, "merge_unit_id": 65541})
    assert gs.calls == [("corps", 3, 4), ("army", 3, 5)]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_parsers.py tests/arena/test_registry.py -q`
Expected: FAIL (ImportError / KeyError).

- [ ] **Step 3: Implement**

`units.py` (this follows the game's own UnitPanel.lua request pattern for FORM_CORPS/FORM_ARMY):

```python
_FORM_FORMATION_LUA = """
local me = Game.GetLocalPlayer()
local u = UnitManager.GetUnit(me, __UNIT__)
local m = UnitManager.GetUnit(me, __MERGE__)
if u == nil then print("ERR:unit __UNIT__ not found") return end
if m == nil then print("ERR:merge unit __MERGE__ not found") return end
local ok, err = pcall(function()
    local cmd = UnitCommandTypes.__COMMAND__
    local tParameters = {}
    tParameters[UnitCommandTypes.PARAM_UNIT_PLAYER] = m:GetOwner()
    tParameters[UnitCommandTypes.PARAM_UNIT_ID] = m:GetID()
    if not UnitManager.CanStartCommand(u, cmd, tParameters) then
        print("ERR:cannot __COMMAND__ here - units must be same type, "
              .. "on/adjacent tiles, with the required civic")
        return
    end
    UnitManager.RequestCommand(u, cmd, tParameters)
    print("OK:__COMMAND__ requested for unit __UNIT__ merging __MERGE__")
end)
if not ok then print("ERR:" .. tostring(err)) end
print("{SENTINEL}")
"""

_FORMATION_COMMANDS = ("FORM_CORPS", "FORM_ARMY")


def build_form_formation(
    unit_index: int, merge_unit_index: int, command: str
) -> str:
    """InGame context: merge two same-type units into a corps or army."""
    if command not in _FORMATION_COMMANDS:
        raise ValueError(f"unknown formation command: {command!r}")
    return (_FORM_FORMATION_LUA
            .replace("__UNIT__", str(int(unit_index)))
            .replace("__MERGE__", str(int(merge_unit_index)))
            .replace("__COMMAND__", command)
            .replace("{SENTINEL}", SENTINEL))
```

(If `units.py`'s existing action builders use `_lua_get_unit(unit_index)` instead of `UnitManager.GetUnit(me, idx)`, match the existing helper — read `build_fortify_unit` first and copy its unit-lookup idiom exactly.)

Export `build_form_formation` from `lua/__init__.py` (units block).

`game_state.py`:

```python
    async def form_corps(self, unit_index: int, merge_unit_index: int) -> str:
        lines = await self.conn.execute_write(
            lq.build_form_formation(unit_index, merge_unit_index, "FORM_CORPS"))
        return _action_result(lines)

    async def form_army(self, unit_index: int, merge_unit_index: int) -> str:
        lines = await self.conn.execute_write(
            lq.build_form_formation(unit_index, merge_unit_index, "FORM_ARMY"))
        return _action_result(lines)
```

`server.py`:

```python
@mcp.tool()
async def form_corps(ctx: Context, unit_id: int, merge_unit_id: int) -> str:
    """Merge two same-type military units into a corps (+10 CS, one unit).

    Requires the Nationalism civic. Units must be the same type and adjacent
    or stacked.
    """
    gs = _get_game(ctx)
    params = {"unit_id": unit_id, "merge_unit_id": merge_unit_id}
    return await _logged(ctx, "form_corps", params,
                         lambda: gs.form_corps(unit_id % 65536, merge_unit_id % 65536))


@mcp.tool()
async def form_army(ctx: Context, unit_id: int, merge_unit_id: int) -> str:
    """Merge a corps with a same-type unit into an army (+17 CS total).

    Requires the Mobilization civic. unit_id must already be a corps.
    """
    gs = _get_game(ctx)
    params = {"unit_id": unit_id, "merge_unit_id": merge_unit_id}
    return await _logged(ctx, "form_army", params,
                         lambda: gs.form_army(unit_id % 65536, merge_unit_id % 65536))
```

`registry.py`:

```python
async def _form_corps_text(gs: Any, args: dict[str, Any]) -> str:
    return await gs.form_corps(
        _unit_index(args["unit_id"]), _unit_index(args["merge_unit_id"]))


async def _form_army_text(gs: Any, args: dict[str, Any]) -> str:
    return await gs.form_army(
        _unit_index(args["unit_id"]), _unit_index(args["merge_unit_id"]))
```

```python
    "form_corps": _tool(
        "form_corps",
        "Merge two same-type military units into a corps (+10 CS as one unit). "
        "Units must be adjacent or stacked.",
        {
            "unit_id": _int_param("Composite id of the unit to keep"),
            "merge_unit_id": _int_param("Composite id of the same-type unit to merge in"),
        },
        ("unit_id", "merge_unit_id"),
        _form_corps_text,
        verb="form_corps",
        requires="corps",
    ),
    "form_army": _tool(
        "form_army",
        "Merge a corps with a same-type unit into an army. unit_id must already "
        "be a corps.",
        {
            "unit_id": _int_param("Composite id of the corps"),
            "merge_unit_id": _int_param("Composite id of the same-type unit to merge in"),
        },
        ("unit_id", "merge_unit_id"),
        _form_army_text,
        verb="form_army",
        requires="army",
    ),
```

`vocab.py` — append `"form_corps": "form_corps",` and `"form_army": "form_army",`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_parsers.py tests/arena/test_registry.py tests/arena/test_analyze.py -q` then `uv run pytest tests/ -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/lua/units.py src/civ_mcp/lua/__init__.py src/civ_mcp/game_state.py \
        src/civ_mcp/server.py src/civ_mcp/arena/registry.py src/civ_mcp/arena/vocab.py \
        tests/test_parsers.py tests/arena/test_registry.py
git commit -m "feat: form_corps/form_army tools (gated) in both tool layers"
```

---

### Task 13: Systems — air rebase (`rebase_unit`) + archaeology (`excavate_artifact`), gated

**Files:**
- Modify: `src/civ_mcp/lua/units.py`, `src/civ_mcp/lua/__init__.py`, `src/civ_mcp/game_state.py`, `src/civ_mcp/server.py`, `src/civ_mcp/arena/registry.py`, `src/civ_mcp/arena/vocab.py`
- Test: `tests/test_parsers.py`, `tests/arena/test_registry.py`

**Interfaces:**
- Produces: `build_unit_operation(unit_index: int, operation: str, x: int, y: int) -> str` where `operation` is `"REBASE"` or `"EXCAVATE"`; `gs.rebase_unit(unit_index, x, y) -> str`, `gs.excavate_artifact(unit_index, x, y) -> str`; tools in both layers (`requires="air"` / `"archaeology"`, verbs = names).

- [ ] **Step 1: Write the failing tests**

`tests/test_parsers.py`:

```python
def test_build_unit_operation_shape():
    from civ_mcp.lua.units import build_unit_operation
    lua = build_unit_operation(3, "REBASE", 10, 12)
    assert "REBASE" in lua and "PARAM_X" in lua and "PARAM_Y" in lua
    assert "CanStartOperation" in lua
    assert "OK:" in lua and "ERR:" in lua
    with pytest.raises(ValueError):
        build_unit_operation(3, "MAKE_TEA", 0, 0)
```

`tests/arena/test_registry.py`:

```python
@pytest.mark.asyncio
async def test_air_and_archaeology_tools_registered_gated():
    assert TOOL_REGISTRY["rebase_unit"].requires == "air"
    assert TOOL_REGISTRY["excavate_artifact"].requires == "archaeology"

    class GS:
        def __init__(self):
            self.calls = []
        async def rebase_unit(self, unit_index, x, y):
            self.calls.append(("rebase", unit_index, x, y)); return "OK"
        async def excavate_artifact(self, unit_index, x, y):
            self.calls.append(("dig", unit_index, x, y)); return "OK"

    gs = GS()
    await dispatch(gs, "rebase_unit", {"unit_id": 65539, "target_x": 4, "target_y": 5})
    await dispatch(gs, "excavate_artifact", {"unit_id": 65540, "target_x": 6, "target_y": 7})
    assert gs.calls == [("rebase", 3, 4, 5), ("dig", 4, 6, 7)]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_parsers.py tests/arena/test_registry.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

`units.py`:

```python
_UNIT_OPERATION_LUA = """
local me = Game.GetLocalPlayer()
local u = UnitManager.GetUnit(me, __UNIT__)
if u == nil then print("ERR:unit __UNIT__ not found") return end
local ok, err = pcall(function()
    -- PROBE(live): operation enum availability (Task 15); spy ops needed
    -- hardcoded hashes, these two may as well.
    local op = UnitOperationTypes.__OPERATION__
    local tParameters = {}
    tParameters[UnitOperationTypes.PARAM_X] = __X__
    tParameters[UnitOperationTypes.PARAM_Y] = __Y__
    if not UnitManager.CanStartOperation(u, op, nil, tParameters) then
        print("ERR:cannot __OPERATION__ at (__X__,__Y__) - check range/target")
        return
    end
    UnitManager.RequestOperation(u, op, tParameters)
    print("OK:__OPERATION__ requested to (__X__,__Y__)")
end)
if not ok then print("ERR:" .. tostring(err)) end
print("{SENTINEL}")
"""

_UNIT_OPERATIONS = ("REBASE", "EXCAVATE")


def build_unit_operation(unit_index: int, operation: str, x: int, y: int) -> str:
    """InGame context: targeted unit operation (air rebase, artifact dig)."""
    if operation not in _UNIT_OPERATIONS:
        raise ValueError(f"unknown unit operation: {operation!r}")
    return (_UNIT_OPERATION_LUA
            .replace("__UNIT__", str(int(unit_index)))
            .replace("__OPERATION__", operation)
            .replace("__X__", str(int(x)))
            .replace("__Y__", str(int(y)))
            .replace("{SENTINEL}", SENTINEL))
```

(Same note as Task 12: match the existing unit-lookup idiom in `units.py` action builders.)

Export `build_unit_operation` from `lua/__init__.py`.

`game_state.py`:

```python
    async def rebase_unit(self, unit_index: int, x: int, y: int) -> str:
        lines = await self.conn.execute_write(
            lq.build_unit_operation(unit_index, "REBASE", x, y))
        return _action_result(lines)

    async def excavate_artifact(self, unit_index: int, x: int, y: int) -> str:
        lines = await self.conn.execute_write(
            lq.build_unit_operation(unit_index, "EXCAVATE", x, y))
        return _action_result(lines)
```

`server.py`:

```python
@mcp.tool()
async def rebase_unit(ctx: Context, unit_id: int, target_x: int, target_y: int) -> str:
    """Rebase an air unit to another of your cities/airstrips within range."""
    gs = _get_game(ctx)
    params = {"unit_id": unit_id, "target_x": target_x, "target_y": target_y}
    return await _logged(ctx, "rebase_unit", params,
                         lambda: gs.rebase_unit(unit_id % 65536, target_x, target_y))


@mcp.tool()
async def excavate_artifact(ctx: Context, unit_id: int, target_x: int, target_y: int) -> str:
    """Send an archaeologist to dig an antiquity site (consumes a charge).

    Sites appear after the Natural History civic; artifacts fill museum slots.
    """
    gs = _get_game(ctx)
    params = {"unit_id": unit_id, "target_x": target_x, "target_y": target_y}
    return await _logged(ctx, "excavate_artifact", params,
                         lambda: gs.excavate_artifact(unit_id % 65536, target_x, target_y))
```

`registry.py`:

```python
async def _rebase_unit_text(gs: Any, args: dict[str, Any]) -> str:
    return await gs.rebase_unit(
        _unit_index(args["unit_id"]), args["target_x"], args["target_y"])


async def _excavate_artifact_text(gs: Any, args: dict[str, Any]) -> str:
    return await gs.excavate_artifact(
        _unit_index(args["unit_id"]), args["target_x"], args["target_y"])
```

```python
    "rebase_unit": _tool(
        "rebase_unit",
        "Rebase an air unit to another of your cities or airstrips in range.",
        {
            "unit_id": _int_param("Air unit composite id"),
            "target_x": _int_param("Destination tile X"),
            "target_y": _int_param("Destination tile Y"),
        },
        ("unit_id", "target_x", "target_y"),
        _rebase_unit_text,
        verb="rebase_unit",
        requires="air",
    ),
    "excavate_artifact": _tool(
        "excavate_artifact",
        "Send an archaeologist to dig the antiquity site at the target tile "
        "(consumes a charge; artifact fills a museum slot).",
        {
            "unit_id": _int_param("Archaeologist composite id"),
            "target_x": _int_param("Site tile X"),
            "target_y": _int_param("Site tile Y"),
        },
        ("unit_id", "target_x", "target_y"),
        _excavate_artifact_text,
        verb="excavate_artifact",
        requires="archaeology",
    ),
```

`vocab.py` — append `"rebase_unit": "rebase_unit",` and `"excavate_artifact": "excavate_artifact",`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_parsers.py tests/arena/test_registry.py tests/arena/test_analyze.py -q` then `uv run pytest tests/ -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/lua/units.py src/civ_mcp/lua/__init__.py src/civ_mcp/game_state.py \
        src/civ_mcp/server.py src/civ_mcp/arena/registry.py src/civ_mcp/arena/vocab.py \
        tests/test_parsers.py tests/arena/test_registry.py
git commit -m "feat: rebase_unit + excavate_artifact tools (gated) in both tool layers"
```

---

### Task 14: Analyze mirror hardening + playbook lines

**Files:**
- Modify: `src/civ_mcp/arena/playbook.md`
- Test: `tests/arena/test_analyze.py`

**Interfaces:**
- Consumes: `_GREAT_PEOPLE_TOOLS`, `_TRADE_ROUTE_TOOLS`, `_RELIGION_WC_TOOLS` frozensets in `analyze.py` (~line 103-118); `TOOL_REGISTRY`.
- Produces: a mirror-consistency test pinning that every local-tool name inside analyze's counter frozensets exists in the registry (closes the "unenforced tool mirrors" review nit); playbook guidance for the new systems.

- [ ] **Step 1: Write the failing test**

Append to `tests/arena/test_analyze.py`:

```python
def test_counter_frozensets_only_name_real_tools():
    """analyze.py's category counters hold literal tool-name frozensets; a
    renamed/removed registry tool would silently drop out of its counter.
    Server-only names (unit_action verbs, mcp__civ6__ tools) are exempt."""
    from civ_mcp.arena import analyze
    from civ_mcp.arena.registry import TOOL_REGISTRY

    SERVER_ONLY = {"unit_action"}  # CLI civs route these via the MCP server
    for setname in ("_GREAT_PEOPLE_TOOLS", "_TRADE_ROUTE_TOOLS",
                    "_RELIGION_WC_TOOLS"):
        names = getattr(analyze, setname)
        local_names = {n for n in names
                       if not n.startswith(analyze.MCP_CIV6_PREFIX)} - SERVER_ONLY
        unknown = local_names - set(TOOL_REGISTRY)
        assert not unknown, f"{setname} names not in registry: {unknown}"
```

Before finalizing, `Read` the three frozensets at `analyze.py:103-118`; if they contain other server-side aliases (e.g. bare unit-action verbs from `_TRADE_ROUTE_UNIT_ACTIONS`), extend `SERVER_ONLY` with exactly those literals so the test pins reality, not aspiration.

- [ ] **Step 2: Run test to verify it fails or passes honestly**

Run: `uv run pytest tests/arena/test_analyze.py -q`
Expected: PASS if the frozensets are already clean (the test then pins them), FAIL listing any stale name — fix the frozenset, not the test.

- [ ] **Step 3: Add playbook lines**

Append to `src/civ_mcp/arena/playbook.md`:

```markdown
## Espionage and government
Once spies unlock, get_spies shows missions; travel first, end turn, then launch.
Change government when a new tier unlocks (first switch per tier is free).

## Grievances, loyalty, climate
get_gossip shows grievances - high grievances against you predict war. get_loyalty
flags cities trending to revolt: assign a governor or fix amenities the same turn.
get_climate warns of floods/storms - avoid settling low coast in late game.

## Formations, air, archaeology, great works
Two same-type units adjacent -> form_corps (+10 CS) once Nationalism unlocks; a
corps + one more -> form_army after Mobilization. Rebase air units forward as the
front moves. Archaeologists dig antiquity sites (excavate_artifact) to fill museum
slots; group matching great works in one building (move_great_work) for theming.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/arena/test_analyze.py tests/arena/test_agent.py -q` then `uv run pytest tests/ -q`
Expected: PASS (playbook is loaded via `load_playbook()`; test_agent has playbook-related tests — confirm none pin exact playbook byte length).

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/playbook.md tests/arena/test_analyze.py
git commit -m "feat(arena): playbook lines for slice-4 systems + analyze counter mirror test"
```

---

### Task 15: Live-probe checklist (merge gate)

**Files:**
- Create: `docs/superpowers/plans/2026-07-07-arena-slice4-live-probes.md`

**Interfaces:**
- Consumes: every `-- PROBE(live):` comment landed in Tasks 4, 8–13.
- Produces: the runnable checklist that gates merging this branch. **This task only writes the checklist document** — executing it requires the FireTuner connection, which the current 50-turn run owns; execution happens later with riz.

- [ ] **Step 1: Write the checklist document**

Create `docs/superpowers/plans/2026-07-07-arena-slice4-live-probes.md`:

```markdown
# Slice 4 live-probe checklist (MERGE GATE)

No greenfield-backed tool reaches a live run until its probe below captures a
real fixture, or the spec records a degrade/cut decision
(docs/superpowers/specs/2026-07-07-arena-slice4-full-toolset-design.md §3).

**Preconditions:** the 50-turn run has ended (no watcher owns FireTuner);
a game is loaded past the relevant era where noted. Run each probe from the
branch checkout with a direct connection:

    uv run python - <<'EOF'
    import asyncio
    from civ_mcp.connection import GameConnection
    from civ_mcp import lua as lq

    async def main():
        conn = GameConnection()
        await conn.connect()
        lines = await conn.execute_write(lq.build_gossip_query())  # <- swap per probe
        print("\n".join(lines))

    asyncio.run(main())
    EOF

For each probe: paste the real output lines into the matching parser test as a
fixture (replacing/augmenting the synthetic one), re-run the suite, and tick
the box. If an API errors, either fix the Lua from the live error, or record
the degrade/cut in the spec and tick with "DEGRADED"/"CUT".

- [ ] **caps snapshot** — `build_caps_query(<pid>)` via execute_read. Verify all
      9 flags emit and flip correctly (check a civ with/without Diplomatic
      Service; verify great_works building scan and formation enums).
- [ ] **gossip** — `build_gossip_query()` via execute_write. GRIEV lines are
      expected to work; GOSSIP lines depend on Game.GetGossipManager existing.
      Likely outcome if absent: degrade to grievances-only (pre-approved in
      spec §3.1).
- [ ] **loyalty** — `build_loyalty_query()` via execute_write. LOYAL lines
      expected solid; LOYSRC breakdown is the probe target.
- [ ] **climate** — `build_climate_query()` via execute_write on a Gathering
      Storm game. Verify phase/sea/CO2 and DISASTER lines; on a base-game
      ruleset confirm the -1 degrade path.
- [ ] **great works query** — `build_great_works_query()` via execute_write on
      a save owning >=1 work + >=1 empty slot.
- [ ] **great works move** — `build_move_great_work(...)` between two owned
      slots; verify with a follow-up query that the work moved. UI.MoveGreatWork
      is the least certain API in the slice.
- [ ] **form corps/army** — on a save with Nationalism + two same-type units:
      `build_form_formation(...)`; verify via get_units that one unit remains
      with corps formation.
- [ ] **rebase** — with any air unit: `build_unit_operation(idx,"REBASE",x,y)`.
      If UnitOperationTypes.REBASE is nil, capture the operation hash the way
      espionage.py documents its _SPY_OP_HASHES and hardcode it.
- [ ] **excavate** — with an archaeologist + revealed antiquity site:
      `build_unit_operation(idx,"EXCAVATE",x,y)`; same hash fallback note.

Record results inline here (output snippet or "DEGRADED: <reason>" / "CUT:
<reason>") and mirror any degrade/cut into the spec before merge.
```

- [ ] **Step 2: Verify suite still green**

Run: `uv run pytest tests/ -q`
Expected: PASS (doc-only change).

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/plans/2026-07-07-arena-slice4-live-probes.md
git commit -m "docs(arena): slice-4 live-probe checklist (merge gate)"
```

---

## Completion

End-state per Global Constraints: all 15 tasks committed on `arena-slice4-full-toolset`, suite green (`uv run pytest tests/ -q`), branch **unmerged**, live probes pending the current run's end. Summarize for riz: what landed, the probe checklist path, and that merge waits on probes + his separate-session review.
