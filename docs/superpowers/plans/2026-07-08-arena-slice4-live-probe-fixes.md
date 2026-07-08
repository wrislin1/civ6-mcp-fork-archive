# Arena Slice-4 Live-Probe Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **NOTE — live-game dependency.** Tasks 1–3 capture/verify against a **live** turn-380 Future-era Gathering-Storm game on the Gaming PC via the FireTuner SSH tunnel (see [[reference-firetuner-tunnel-gaming-pc]]). FireTuner is single-client: run every probe through the **one persistent-connection wrapper defined in Task 0 Step 4** (`try/finally: await conn.disconnect()`), never overlapping clients. If no live game is available, the code + unit-test steps still stand and the suite still goes green; only the "re-capture real fixture" sub-steps block.
>
> **STATUS 2026-07-08 — executed + live-re-verified; the `- [ ]` "Step 5 re-capture" boxes are intentionally NOT owed.** All code/fix work landed (`b770626`/`8706814`/`7cf085c`) and the real turn-380 fixtures are pinned (`951423d`, `b3540d8`). A separate **turn-381 live re-verification** (via the freed FireTuner slot) then re-confirmed all five probe behaviors directly and exercised the excavate full success path (real dig at (98,56), site consumed) — see the checklist `2026-07-07-arena-slice4-live-probes.md` and the spec §"Live-probe outcomes". The optional "re-capture the real fixture (live) + re-pin" sub-steps (Task 1/2/3 Step 5–6) were **deliberately not run**: the pinned turn-380 captures remain authoritative and behavior is unchanged at 381, so no test file was modified. Those boxes stay `- [ ]` to reflect that the *re-pin* action was skipped by design, not that verification is owed.

**Goal:** Fix the three real defects the 2026-07-08 live-probe run found in the already-merged slice-4 greenfield tools (gossip prints table pointers; excavate's op enum is nil; great-works move calls a nonexistent `UI.MoveGreatWork`), record the two API degrades (sea-level, loyalty-breakdown), and replace synthetic parser fixtures with the real captured game output.

**Architecture:** Small, surgical edits to the Lua builders in `src/civ_mcp/lua/` (gossip in `diplomacy.py`, unit-operation hashes in `units.py`, move degrade in `great_works.py`) plus parser-test fixtures in `tests/`. Mirror existing codebase idioms: the excavate fix copies `espionage.py`'s hardcoded-hash pattern; the move cut copies slice-4's "informative UNAVAILABLE readout" pattern. No new modules.

**Tech Stack:** Python 3.12 + `uv`; pytest + pytest-asyncio; Lua over FireTuner via `conn.execute_read`/`execute_write`. Live probes: `GameConnection` (127.0.0.1:4318 through the tunnel).

## Global Constraints

- **Branch:** all work on `arena-slice4-live-probe-fixes` off `main` (currently `48c7de1`). Implementation **ends at a green, unmerged branch** with a summary, ready for riz's separate-session review (standing Rule 11). The usual-route merge (push to `origin`/.141, ff-merge its `main`, `git fetch origin`, `git push github main`) is documented in Task 6 as a **post-review step gated on explicit go-ahead** — do NOT auto-merge as part of execution.
- **Test command:** `uv run pytest tests/ -q` (never bare `pytest`). Baseline is **851 passed**; every task keeps the suite green.
- **Single-client FireTuner:** one persistent `GameConnection` per probe script; disconnect cleanly. Never open overlapping clients (wedges the tuner with orphan sockets).
- **Delimiter:** parser lines are `|`-split; the gossip parser rejoins `parts[3:]`, so embedded `|` in gossip text is preserved — do NOT strip it.
- **Fail-open / degrade idiom:** a missing game API leaves its field at the sentinel (`-1`, or no line) inside a `pcall`; never let it raise. Keep this for sea-level and loyalty-breakdown.
- **Gossip cap:** newest-first, `_GOSSIP_MAX_PER_CIV = 15` per met civ (riz decision, 2026-07-08 — 13,493 uncapped lines/turn is unusable).

---

## File Structure

- `src/civ_mcp/lua/diplomacy.py` — `_GOSSIP_LUA` / `build_gossip_query`: extract `entry[1]` text, `entry[2]` turn, cap newest-first.
- `src/civ_mcp/lua/units.py` — add `_UNIT_OP_HASHES`; `build_unit_operation` uses hardcoded op hash instead of nil-prone `UnitOperationTypes.{op}`.
- `src/civ_mcp/lua/great_works.py` — `build_move_great_work` / `_GW_MOVE_LUA`: guard the nonexistent `UI.MoveGreatWork` and emit an informative `UNAVAILABLE:` readout (cut, not crash).
- `src/civ_mcp/arena/registry.py` — `move_great_work` tool description (~1275): mark it unavailable in this build so the arena LLM stops being told it works.
- `src/civ_mcp/arena/playbook.md` — the `move_great_work` recommendation (~162): drop / mark unavailable.
- `tests/test_parsers.py` — builder-shape regression tests (gossip extracts `entry[1]`/caps; great-works move degrades) + real captured fixtures for gossip (fixed), climate (`sea=-1` degrade), great-works query; formation OK-line shape.
- `tests/arena/test_capabilities.py` — real captured `CAPS|` fixture.
- `tests/test_live_probe_fixtures.py` (new file) — parse real captured loyalty / great-works / gossip / caps lines end-to-end.
- `docs/superpowers/plans/2026-07-07-arena-slice4-live-probes.md` — tick ✅ / annotate DEGRADED / CUT per probe with the real evidence.
- `docs/superpowers/specs/2026-07-07-arena-slice4-full-toolset-design.md` — record the two degrades + one cut as §3 decisions.

---

### Task 0: Branch setup

**Files:** none (git only)

- [ ] **Step 1: Create the fix branch off main**

```bash
cd /home/riz/dev/civ6-mcp
git fetch origin && git checkout main && git rev-parse --short HEAD   # expect 48c7de1
git worktree add /home/riz/dev/civ6-mcp/.claude/worktrees/arena-slice4-live-probe-fixes -b arena-slice4-live-probe-fixes main
cd /home/riz/dev/civ6-mcp/.claude/worktrees/arena-slice4-live-probe-fixes
```

- [ ] **Step 2: Copy this plan into the branch worktree and commit**

The plan currently lives **untracked** in the main checkout (`/home/riz/dev/civ6-mcp/docs/…`), so `git mv` would fail and a relative `../../docs` from the new worktree resolves under `.claude/`, not the checkout. Copy by absolute path:

```bash
cp /home/riz/dev/civ6-mcp/docs/superpowers/plans/2026-07-08-arena-slice4-live-probe-fixes.md \
   docs/superpowers/plans/2026-07-08-arena-slice4-live-probe-fixes.md
git add docs/superpowers/plans/2026-07-08-arena-slice4-live-probe-fixes.md
git commit -m "docs(arena): live-probe-fixes plan"
# tidy the now-orphaned untracked copy in the main checkout
rm -f /home/riz/dev/civ6-mcp/docs/superpowers/plans/2026-07-08-arena-slice4-live-probe-fixes.md
```

- [ ] **Step 3: Confirm the tunnel is up**

```bash
pgrep -f 'ssh -f -N.*-L 4318' || ssh -f -N -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L 4318:127.0.0.1:4318 riz@192.168.20.141
```

- [ ] **Step 4: Define the single-client probe wrapper (used by every live step)**

Every live step below runs through this exact shape — **one** `GameConnection`, and `disconnect()` in a `finally` so a mid-probe exception can't leave an orphan socket that wedges the single-client tuner. Save it as `scratch/probe.py` in the worktree:

```python
# scratch/probe.py — run: uv run python scratch/probe.py
import asyncio
from civ_mcp.connection import GameConnection

# Replace LUA with the builder output under test, e.g.:
#   from civ_mcp.lua.diplomacy import build_gossip_query; LUA = build_gossip_query()
LUA = 'print("SANITY|" .. tostring(Game.GetCurrentGameTurn())); print("__S__")'

async def main():
    conn = GameConnection()
    await conn.connect()
    try:
        for line in await conn.execute_write(LUA, timeout=15.0):
            print(line)
    finally:
        await conn.disconnect()

asyncio.run(main())
```

Run it once as a sanity read: `uv run python scratch/probe.py` → expect a `SANITY|380` line (turn 380 / ERA_FUTURE). If the game changed, re-derive the unit IDs / work IDs used below. `scratch/` is git-ignored; it never gets committed.

---

### Task 1: Gossip — extract text + cap newest-first

**Files:**
- Modify: `src/civ_mcp/lua/diplomacy.py` (`_GOSSIP_LUA` ~1377–1410, `build_gossip_query` ~1413)
- Test: `tests/test_parsers.py::TestParseGossip` (`test_grievances_and_gossip` ~389)

**Interfaces:**
- Consumes: `SENTINEL` (already imported).
- Produces: `build_gossip_query() -> str` (unchanged signature); output lines `GRIEV|pid|name|theirs|mine` and `GOSSIP|pid|turn|text` where `text` is the real gossip string.

**Live finding:** `gm:GetRecentVisibleGossipStrings(me, pid)` returns a list of **tables**; `entry[1]` = gossip text (string), `entry[2]` = turn (number). Old code printed `tostring(entry)` → `table: 0x…`. At turn 380 it emitted 13,493 lines.

**Why the regression test targets the builder, not the parser.** The bug is entirely in the emitted **Lua** (`_GOSSIP_LUA` prints `tostring(entry)`); `parse_gossip_response` already parses well-formed `GOSSIP|pid|turn|text` lines correctly and always did. A parser-only fixture would pass against the *current, broken* code and prove nothing. So the failing test must assert on `build_gossip_query()`'s **output string** — that it extracts `entry[1]`/`entry[2]`, substitutes the cap, and no longer contains `tostring(entry)`.

- [ ] **Step 1: Write the failing builder-shape test (red)**

In `tests/test_parsers.py::TestParseGossip`, add:

```python
def test_build_gossip_query_extracts_text_and_caps(self):
    from civ_mcp.lua.diplomacy import build_gossip_query, _GOSSIP_MAX_PER_CIV
    lua = build_gossip_query()
    assert "entry[1]" in lua                 # emits the text field, not the table
    assert "entry[2]" in lua                 # emits the turn field
    assert "tostring(entry)" not in lua      # regression: old code printed the table pointer
    assert "__GOSSIP_MAX__" not in lua       # cap placeholder was substituted
    assert f"shown >= {_GOSSIP_MAX_PER_CIV}" in lua   # newest-first cap wired in
```

- [ ] **Step 2: Run the test to verify it fails (red)**

Run: `uv run pytest tests/test_parsers.py::TestParseGossip::test_build_gossip_query_extracts_text_and_caps -q`
Expected: FAIL — current builder contains `tostring(entry)` and lacks `entry[1]` / the `_GOSSIP_MAX_PER_CIV` constant (an `ImportError` on the constant is also acceptable red).

- [ ] **Step 3: Add the cap constant + rewrite the gossip-log block (green)**

In `src/civ_mcp/lua/diplomacy.py`, add near the top of the module (with other module constants):

```python
_GOSSIP_MAX_PER_CIV = 15  # newest-first cap per met civ (live probe 2026-07-08: 13k uncapped)
```

Replace the gossip-log `pcall` block inside `_GOSSIP_LUA` (the `for _, entry in ipairs(...)` loop) with:

```lua
        pcall(function()
            -- entry is a table: entry[1]=text (string), entry[2]=turn (number).
            -- Cap newest-first per civ; 13k+ lines/turn is unusable (live probe 2026-07-08).
            local gm = Game.GetGossipManager()
            local entries = gm:GetRecentVisibleGossipStrings(me, pid)
            table.sort(entries, function(a, b) return (a[2] or 0) > (b[2] or 0) end)
            local shown = 0
            for _, entry in ipairs(entries) do
                if shown >= __GOSSIP_MAX__ then break end
                local text = tostring(entry[1] or "")
                local eturn = tostring(entry[2] or Game.GetCurrentGameTurn())
                print("GOSSIP|" .. pid .. "|" .. eturn .. "|" .. text)
                shown = shown + 1
            end
        end)
```

Update `build_gossip_query`:

```python
def build_gossip_query() -> str:
    """InGame context: grievances both directions per met major + capped gossip log."""
    return (_GOSSIP_LUA
            .replace("__GOSSIP_MAX__", str(_GOSSIP_MAX_PER_CIV))
            .replace("{SENTINEL}", SENTINEL))
```

- [ ] **Step 4: Run the builder-shape test + parser suite (green)**

Run: `uv run pytest tests/test_parsers.py -q`
Expected: PASS (the builder-shape test now green; existing parser tests unaffected).

- [ ] **Step 5: Re-capture the real fixture (live) + pin it in the parser test**

Using the Task 0 probe wrapper (`scratch/probe.py` with `from civ_mcp.lua.diplomacy import build_gossip_query; LUA = build_gossip_query()`), run `uv run python scratch/probe.py`. Expect `GRIEV|…` lines plus ≤15 `GOSSIP|pid|turn|<real text>` lines per civ (no `table:` refs, total well under a few hundred). Copy 1 `GRIEV|` and 1 real `GOSSIP|` line into `test_grievances_and_gossip`:

```python
def test_grievances_and_gossip(self):
    lines = [
        "GRIEV|1|Elizabeth I|82|-82",
        "GOSSIP|1|379|Your delegate, Frideswide, learned that Sweden completed research on Guidance Systems.",
    ]
    grievances, gossip = parse_gossip_response(lines)
    assert grievances[0].player_id == 1 and grievances[0].they_hold_against_me == 82
    assert gossip[0].about_player == 1 and gossip[0].turn == 379
    assert "Guidance Systems" in gossip[0].text
    assert "table:" not in gossip[0].text  # regression: old bug printed the table pointer
```

Re-run `uv run pytest tests/test_parsers.py -q` → PASS. (If no live game is available, keep the fixture text shown above — it is a real turn-380 capture from the probe run — and skip only the re-capture.)

- [ ] **Step 6: Commit**

```bash
git add src/civ_mcp/lua/diplomacy.py tests/test_parsers.py
git commit -m "fix(arena): gossip emits real text (entry[1]) capped newest-first per civ"
```

---

### Task 2: Excavate — hardcode op hashes (enum is nil)

**Files:**
- Modify: `src/civ_mcp/lua/units.py` (`build_unit_operation` ~1399, add `_UNIT_OP_HASHES`)
- Test: `tests/test_parsers.py` (new `test_build_unit_operation_uses_hash`)

**Interfaces:**
- Produces: `build_unit_operation(unit_index: int, operation: str, x: int, y: int) -> str` (signature unchanged); `_UNIT_OP_HASHES: dict[str,int]`.

**Live finding:** `UnitOperationTypes.EXCAVATE` is **nil**, so the request silently fails. `DB.MakeHash("UNITOPERATION_EXCAVATE")` = `1548958412` (also present in `GameInfo.UnitOperations`). `UnitOperationTypes.REBASE` resolves to `-1054550409` (its hash). Mirror `espionage.py`'s hardcoded-hash pattern.

- [ ] **Step 1: Write the failing substitution test (red)**

In `tests/test_parsers.py`, add:

```python
def test_build_unit_operation_uses_hash():
    from civ_mcp.lua.units import build_unit_operation, _UNIT_OP_HASHES
    lua = build_unit_operation(42, "EXCAVATE", 5, 6)
    assert str(_UNIT_OP_HASHES["EXCAVATE"]) in lua
    assert "UnitOperationTypes.EXCAVATE" not in lua  # no nil enum reference
    assert "PARAM_X" in lua and "PARAM_Y" in lua
```

- [ ] **Step 2: Run the test to verify it fails (red)**

Run: `uv run pytest tests/test_parsers.py::test_build_unit_operation_uses_hash -q`
Expected: FAIL — `_UNIT_OP_HASHES` does not exist yet (ImportError), and the current builder still emits `UnitOperationTypes.EXCAVATE`.

- [ ] **Step 3: Add the hash table + use it in the builder (green)**

In `src/civ_mcp/lua/units.py`, near `_UNIT_OPERATIONS`:

```python
# Operation hashes — UnitOperationTypes.EXCAVATE is nil in the tuner Lua
# context (live probe 2026-07-08); REBASE resolves but we pin both for
# stability, mirroring espionage.py's _SPY_OP_HASHES.
_UNIT_OP_HASHES: dict[str, int] = {
    "REBASE": -1054550409,
    "EXCAVATE": 1548958412,
}
```

In `build_unit_operation`, replace `local op = UnitOperationTypes.{operation}` with the hardcoded hash:

```python
    x, y = int(x), int(y)
    op_hash = _UNIT_OP_HASHES[operation]
    return f"""
{_lua_get_unit(unit_index)}
local ok, err = pcall(function()
    local op = {op_hash}
    local tParameters = {{}}
    tParameters[UnitOperationTypes.PARAM_X] = {x}
    tParameters[UnitOperationTypes.PARAM_Y] = {y}
    if not UnitManager.CanStartOperation(unit, op, nil, tParameters) then
        print("ERR:cannot {operation} at ({x},{y}) - check range/target")
        return
    end
    UnitManager.RequestOperation(unit, op, tParameters)
    print("OK:{operation} requested to ({x},{y})")
end)
if not ok then print("ERR:" .. tostring(err)) end
print("{SENTINEL}")
"""
```

(The `_UNIT_OPERATIONS` membership guard at the top of the function stays; `_UNIT_OP_HASHES` keys must equal `_UNIT_OPERATIONS`.)

**Live guard on the hash values (optional, needs the game):** before trusting the two constants, run the Task 0 probe wrapper with `LUA = 'print(DB.MakeHash("UNITOPERATION_REBASE"), DB.MakeHash("UNITOPERATION_EXCAVATE")); print("__S__")'`. Expect `-1054550409  1548958412`; if the ruleset differs, use the printed values instead.

- [ ] **Step 4: Run the test (green)**

Run: `uv run pytest tests/test_parsers.py::test_build_unit_operation_uses_hash -q`
Expected: PASS

- [ ] **Step 5: Live-verify rebase still OK + excavate degrades cleanly**

Via the Task 0 probe wrapper: rebase Jet Bomber → a valid city tile (`LUA = build_unit_operation(<jet_idx>, "REBASE", <cx>, <cy>)`): expect `OK:REBASE requested to (x,y)`. Excavate on the charge-0 archaeologist (`LUA = build_unit_operation(<arch_idx>, "EXCAVATE", <x>, <y>)`): expect clean `ERR:cannot EXCAVATE …` (the op hash now resolves; failure is charge/target, not a nil enum). Record both lines as the probe fixtures.

- [ ] **Step 6: Run parser suite + commit**

Run: `uv run pytest tests/test_parsers.py -q` → PASS
```bash
git add src/civ_mcp/lua/units.py tests/test_parsers.py
git commit -m "fix(arena): unit-operation ops use hardcoded hashes (EXCAVATE enum nil)"
```

---

### Task 3: Great-works move — cut to informative UNAVAILABLE

**Files:**
- Modify: `src/civ_mcp/lua/great_works.py` (`_GW_MOVE_LUA`, ~move request line)
- Modify: `src/civ_mcp/arena/registry.py` (`move_great_work` tool description ~1275)
- Modify: `src/civ_mcp/arena/playbook.md` (great-works line ~162)
- Test: `tests/test_parsers.py::test_build_move_great_work_substitutes_args` (augment) + new unavailable-degrade assertion

**Interfaces:**
- Produces: `build_move_great_work(work_index, target_city_id, building, slot) -> str` (signature unchanged); output is now an informative `UNAVAILABLE:` line instead of a raw Lua crash.

**Live finding:** `UI.MoveGreatWork`, `Game.GetGreatWorks`, `GreatWorksManager` are all **nil** in the tuner context; the old builder crashed with `function expected instead of nil`. `CityManager.RequestCommand` exists but the `CityCommandTypes.MOVE_GREAT_WORK` param constants don't resolve under the guessed names — reverse-engineering them is out of scope for this pass (the checklist flagged this API as the least-certain and permits a cut). The **query** works fully; only the **move** is cut. Because the tool stays registered (returning UNAVAILABLE), the arena surfaces that *recommend* it must also be corrected — otherwise the LLM keeps being told a dead action works (Step 5).

- [ ] **Step 1: Write the failing degrade test (red)**

In `tests/test_parsers.py`, add (keep `test_build_move_great_work_substitutes_args` / `_rejects_suspicious_building` as-is — they check substitution + the building-id guard, both unchanged):

```python
def test_build_move_great_work_degrades_when_ui_absent():
    lua = build_move_great_work(33, 851980, "BUILDING_AMPHITHEATER", 0)
    assert "UNAVAILABLE:great-work move" in lua      # cut path present
    assert 'type(UI.MoveGreatWork) ~= "function"' in lua
```

- [ ] **Step 2: Run the test to verify it fails (red)**

Run: `uv run pytest tests/test_parsers.py::test_build_move_great_work_degrades_when_ui_absent -q`
Expected: FAIL — the current builder calls `UI.MoveGreatWork(...)` unguarded, so neither the `UNAVAILABLE` string nor the type-guard is present.

- [ ] **Step 3: Guard the move call — degrade, don't crash (green)**

In `_GW_MOVE_LUA`, replace the `UI.MoveGreatWork(...)` request (and its success `print`) with an availability guard so the whole builder returns a clean, parseable readout:

```lua
    -- PROBE(live 2026-07-08): UI.MoveGreatWork / Game.GetGreatWorks /
    -- GreatWorksManager are all nil in the tuner Lua context; no working
    -- move API found. CUT to an informative readout (query still works).
    if type(UI) ~= "table" or type(UI.MoveGreatWork) ~= "function" then
        print("UNAVAILABLE:great-work move — no move API in this game build "
              .. "(work " .. workIndex .. " -> " .. targetBuilding .. " slot " .. targetSlot .. ")")
        return
    end
    UI.MoveGreatWork(fromCity:GetID(), fromBuildingIdx, fromSlot,
                     toCity:GetID(), toRow.Index, targetSlot)
    print("OK:requested move of work " .. workIndex .. " to " .. targetBuilding
          .. " slot " .. targetSlot)
```

- [ ] **Step 4: Run the test (green)**

Run: `uv run pytest tests/test_parsers.py::test_build_move_great_work_degrades_when_ui_absent -q`
Expected: PASS

- [ ] **Step 5: Correct the arena surfaces that advertise the cut action**

The tool remains registered but now returns UNAVAILABLE. Update the two prompt surfaces so the arena LLM is not told it works.

In `src/civ_mcp/arena/registry.py`, the `move_great_work` `_tool(...)` description — replace:

```python
        "Move a great work (index from get_great_works) to another building "
        "slot, e.g. to group matching works for a theming bonus.",
```

with:

```python
        "UNAVAILABLE in this game build: no working move API in the tuner "
        "context, so this reports UNAVAILABLE rather than moving anything. "
        "Read slots with get_great_works; great works can't be rearranged here.",
```

In `src/civ_mcp/arena/playbook.md` (~line 162), replace:

```
slots; group matching great works in one building (move_great_work) for theming.
```

with:

```
slots. (Rearranging works for a theming bonus via move_great_work is
unavailable in this build — the tuner context exposes no move API.)
```

- [ ] **Step 6: Live-verify graceful degrade (no crash)**

Via the Task 0 probe wrapper (`LUA = build_move_great_work(33, 851980, "BUILDING_AMPHITHEATER", 0)`), run on the live game: expect one `UNAVAILABLE:great-work move …` line and no `Runtime Error`. Record it as the probe fixture.

- [ ] **Step 7: Run parser suite + commit**

Run: `uv run pytest tests/test_parsers.py -q` → PASS
```bash
git add src/civ_mcp/lua/great_works.py src/civ_mcp/arena/registry.py src/civ_mcp/arena/playbook.md tests/test_parsers.py
git commit -m "fix(arena): great-works move degrades to UNAVAILABLE + mark tool/playbook unavailable"
```

---

### Task 4: Real-fixture parser tests for the validated probes

**Files:**
- Create: `tests/test_live_probe_fixtures.py`
- Modify: `tests/arena/test_capabilities.py` (add the real CAPS line)

**Interfaces:**
- Consumes: `parse_loyalty_response`, `parse_climate_response`, `parse_great_works_response`, `parse_caps`, `parse_gossip_response` (all existing).

Captured real lines to pin (turn-380 game). These are regression anchors proving the parsers handle actual game output — not just synthetic fixtures.

- [ ] **Step 1: Write the end-to-end fixture test**

```python
from civ_mcp.lua.diplomacy import parse_gossip_response
from civ_mcp.lua.cities import parse_loyalty_response
from civ_mcp.lua.climate import parse_climate_response
from civ_mcp.lua.great_works import parse_great_works_response


def test_real_loyalty_lines():
    rows = parse_loyalty_response([
        "LOYAL|65536|Gyeongju|100.00|100.00|38.00",
        "LOYAL|2228255|London|79.98|100.00|3.89",
    ])
    assert rows[0].city_id == 65536 and rows[0].loyalty == 100.0
    assert rows[1].name == "London" and rows[1].per_turn == 3.89
    # LOYSRC degrades live (GetLoyaltyBreakdown nil) -> no sources
    assert rows[0].sources == []


def test_real_climate_line_sea_level_degrades():
    status = parse_climate_response(["CLIMATE|11|-1|17376"])
    assert status.phase == 11 and status.co2_total == 17376
    assert status.sea_level == -1   # GetSeaLevel + alts all nil (documented degrade)
    assert status.disasters == []


def test_real_great_works_slots():
    slots = parse_great_works_response([
        "GWSLOT|65536|Gyeongju|BUILDING_OXFORD_UNIVERSITY|0|GREATWORKSLOT_WRITING|3|",
        "GWSLOT|393221|Busan|BUILDING_AMPHITHEATER|1|GREATWORKSLOT_WRITING|-1|",
    ])
    assert slots[0].work_index == 3 and slots[0].slot_type == "GREATWORKSLOT_WRITING"
    assert slots[1].work_index == -1  # empty slot sentinel


def test_real_gossip_fixed_text():
    _, gossip = parse_gossip_response([
        "GOSSIP|1|379|Your delegate learned that Sweden completed research on Guidance Systems.",
    ])
    assert gossip[0].turn == 379 and "table:" not in gossip[0].text
```

- [ ] **Step 2: Pin the real CAPS line in the caps test**

In `tests/arena/test_capabilities.py`, add:

```python
def test_parse_caps_real_capture():
    from civ_mcp.arena.capabilities import parse_caps
    flags = parse_caps([
        "CAPS|spies=1|government=1|religious_unit=0|gp_unit=0|corps=1|army=1|air=1|archaeology=0|great_works=1"
    ])
    assert flags["corps"] and flags["army"] and flags["air"] and flags["great_works"]
    assert flags["archaeology"] is False  # charge-0 archaeologist -> flag flips correctly
```

- [ ] **Step 3: Run tests + commit**

Run: `uv run pytest tests/test_live_probe_fixtures.py tests/arena/test_capabilities.py -q` → PASS
```bash
git add tests/test_live_probe_fixtures.py tests/arena/test_capabilities.py
git commit -m "test(arena): pin real turn-380 fixtures for caps/loyalty/climate/great-works/gossip"
```

---

### Task 5: Record probe outcomes in the checklist + spec

**Files:**
- Modify: `docs/superpowers/plans/2026-07-07-arena-slice4-live-probes.md`
- Modify: `docs/superpowers/specs/2026-07-07-arena-slice4-full-toolset-design.md`

- [ ] **Step 1: Tick / annotate every probe box** in the live-probes checklist with the real result:
  - caps `[x]` — real CAPS line; flags flip (archaeology=0 with an archaeologist proves gating).
  - gossip `[x]` — GRIEV ok; GOSSIP fixed (`entry[1]`) + capped 15/civ (commit ref).
  - loyalty `[x] DEGRADED` — LOYAL×32 ok; LOYSRC omitted (`GetLoyaltyBreakdown` nil).
  - climate `[x] DEGRADED` — phase+co2 ok; `sea=-1` (`GetSeaLevel` + 3 alts nil).
  - great works query `[x]` — 145 GWSLOT lines.
  - great works move `[x] CUT` — `UI.MoveGreatWork`/`Game.GetGreatWorks`/`GreatWorksManager` nil; degraded to UNAVAILABLE (commit ref).
  - form corps `[x]` — OK + verified mf=1.
  - form army `[x]` — command path validated (`CanStartCommand`); no adjacent trio for a live merge on this save (armies already present).
  - rebase `[x]` — OK.
  - excavate `[x] DEGRADED` — enum nil → hash `1548958412` (commit ref); needs a charged archaeologist to fully exercise.
  - review-probes: formation enums `[x]` (0/1/2); naval fleet gating `[x]`; GameClimate numeric `[x]` (int; sea degrades).

- [ ] **Step 2: Record the degrade/cut decisions in the spec** (§3): sea-level and loyalty-breakdown degrade to explicit sentinels; great-works **move** is CUT to a readout with UI.MoveGreatWork nil in the tuner context (query retained). One line each.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/plans/2026-07-07-arena-slice4-live-probes.md docs/superpowers/specs/2026-07-07-arena-slice4-full-toolset-design.md
git commit -m "docs(arena): record live-probe outcomes (ticks + 2 degrades + 1 cut)"
```

---

### Task 6: Full suite + branch summary, then STOP for review

**Files:** none (git only)

Implementation ends here at a green, unmerged branch (standing Rule 11). Do **not** merge or push in this step — that is the separate gated section below.

- [ ] **Step 1: Full suite green**

Run: `uv run pytest tests/ -q`
Expected: PASS (≥ 851 + the new builder-shape / fixture tests).

- [ ] **Step 2: Write the branch summary and stop**

Produce a short summary for riz's separate-session review:
- the 3 defect fixes (gossip `entry[1]`+cap, excavate hashes, great-works move→UNAVAILABLE) with commit refs;
- the 2 degrades (sea-level, loyalty-breakdown) + 1 cut (great-works move), each with the live evidence;
- final `git log --oneline main..HEAD` and the pass count from Step 1;
- which live re-captures actually ran vs. which used the pinned turn-380 fixtures.

Then **stop.** Report the branch as ready for review; do not proceed to merge without explicit go-ahead.

---

### Post-review merge — GATED on explicit go-ahead (do NOT run as part of execution)

Only after riz reviews the branch in a separate session **and says to merge**, run the usual route:

```bash
cd /home/riz/dev/civ6-mcp/.claude/worktrees/arena-slice4-live-probe-fixes
BR=arena-slice4-live-probe-fixes
git push origin HEAD:refs/heads/$BR
ssh -o BatchMode=yes riz@192.168.20.141 "cd ~/projects/civ6-mcp && git merge --ff-only $BR"   # if .141 main isn't ff-able, rebase the branch on origin/main first
# fold branch into local main:
cd /home/riz/dev/civ6-mcp && git checkout main && git merge --ff-only $BR
git fetch origin && git push github main
git push origin :refs/heads/$BR   # delete temp branch
# verify all three refs aligned:
echo "local=$(git rev-parse --short main) origin=$(git rev-parse --short origin/main) github=$(git ls-remote github refs/heads/main | cut -c1-7)"
# worktree cleanup:
git worktree remove /home/riz/dev/civ6-mcp/.claude/worktrees/arena-slice4-live-probe-fixes
git branch -d arena-slice4-live-probe-fixes
# re-run the full suite on merged main:
uv run pytest tests/ -q   # expect PASS
```

---

## Self-Review

**Spec coverage:** Every probe in the live-probe checklist maps to a task — the 3 defects to Tasks 1–3, the real fixtures for the 8 working probes to Task 4, the degrade/cut records to Task 5. The two degrades (sea-level, loyalty-breakdown) are recorded, not "fixed", because no working API exists (verified live: `GetSeaLevel` + 3 alternatives nil; `GetLoyaltyBreakdown` nil).

**Placeholder scan:** All code steps carry the exact edit + the real captured fixtures (turn-380 game). The only bounded-judgment steps are the live re-captures (Task 1 Step 5, Task 2 Step 5, Task 3 Step 6), which are real capture actions through the Task 0 probe wrapper, not TODOs.

**Type consistency:** `_UNIT_OP_HASHES` keys equal `_UNIT_OPERATIONS` (`REBASE`,`EXCAVATE`); `build_gossip_query` still returns `str`; parser signatures (`parse_gossip_response`, `parse_loyalty_response`, `parse_climate_response`, `parse_great_works_response`, `parse_caps`) are consumed exactly as defined in the source.

## Execution Handoff

Recommended: **Inline execution** (superpowers:executing-plans) in this session — the fixes are small and interleave with live FireTuner captures that need the persistent connection already established here; a fresh subagent can't easily share it. Subagent-driven would force each subagent to re-establish the tunnel and re-derive unit/work IDs.
