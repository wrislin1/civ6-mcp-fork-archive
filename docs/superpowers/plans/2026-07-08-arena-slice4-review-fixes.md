# Arena Slice 4 — Review-Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve the 10 confirmed findings from the high-effort whole-branch review of `arena-slice4-full-toolset`, plus close the standing pre-existing coordinate-injection OPEN ITEM, without changing the slice's architecture.

**Architecture:** Localized fixes at the layer where each defect lives — Lua builders/parsers (`src/civ_mcp/lua/`), the arena registry wrappers (`src/civ_mcp/arena/registry.py`), the capability snapshot (`src/civ_mcp/arena/capabilities.py`), the agent dispatch loop (`src/civ_mcp/arena/agent.py`), the narrator (`src/civ_mcp/narrate.py`), and the metrics module (`src/civ_mcp/arena/analyze.py`). No systemic `_dispatch` schema-coercion refactor (explicitly ruled out this session); the spec's dispatch-allowlist hard-block is preserved and only its error message is improved.

**Tech Stack:** Python 3.12 + `uv`; pytest + pytest-asyncio; the game-facing layer emits Lua strings executed over FireTuner (never evaluated in-process).

## Global Constraints

Every task's requirements implicitly include these (copied from the slice-4 spec + project memory; they bind here too):

- `run_lua` stays removed from the puppet/CLI toolsets — never register it in `TOOL_REGISTRY`, never add it to any tier.
- **Fail-open gating everywhere:** `caps=None` or a missing/erroring flag must leave the tool exposed. Every fix that touches gating must keep failing open — an over-closed gate silently removes an ability.
- **Localized fixes only.** Do NOT add a schema-driven coercion pass to `_dispatch`. Do NOT soften the dispatch allowlist to let gated tools through — keep the hard-block, improve only the message.
- **LLM-controlled arguments that reach a bare Lua-value context must be int-cast or validated** before interpolation. This is the security theme of the slice (the reason `run_lua` was removed): a crafted string arg must never be able to break out of a Lua string literal.
- Composite unit IDs convert via `_unit_index()` (`int(unit_id) % 65536`); coordinates convert via `int()`.
- Lua conventions: pipe-delimited `print()` lines terminated by `SENTINEL` (`---END---`); `pcall` every uncertain engine API; `-- PROBE(live):` comments tie unverified APIs to the Task-15 live-probe merge gate.
- New or changed **behavior** tools must be reflected in `analyze.py`'s behavior frozensets and (for action tools) `vocab.LOCAL_TOOL_VERBS` — both are test-enforced.
- End-state is an **UNMERGED** branch. Do not merge or push. The merge gate remains the live-probe checklist (`docs/superpowers/plans/2026-07-07-arena-slice4-live-probes.md`) plus riz's separate-session review.
- **Test command:** `uv run pytest tests/ -q` for the full suite (NEVER bare `uv run pytest` — `scripts/` breaks collection). Focused: `uv run pytest tests/path::test_name -q`.

---

### Task 1: Sanitize `spy_action`'s unknown-mission echo (Finding #1 — security, reachable)

`spy_action`'s `action` reaches `build_spy_mission`'s unknown-mission branch raw (via `_spy_action_text` → `str(args["action"])`). The current escape `mission_type.replace('"', '\\"')` only escapes quotes, not backslashes, so an `action` beginning with a backslash combines with the added escape to close the Lua string early and execute arbitrary Lua. This is a real, reachable injection. Replace the fragile escape with a whitelist sanitizer — the value is only echoed into an error message, so a display-safe subset is sufficient and unambiguously injection-proof.

**Files:**
- Modify: `src/civ_mcp/lua/espionage.py:183-194` (the `op_hash is None` branch)
- Test: `tests/test_parsers.py` (new test near the espionage builders)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_parsers.py`:

```python
def test_build_spy_mission_unknown_type_cannot_inject_lua():
    from civ_mcp.lua.espionage import build_spy_mission
    # An unknown mission whose name is a Lua-breakout payload must be neutralized:
    # the echoed name contributes ZERO quote characters, so the only quotes in the
    # generated Lua are the four from the two wrapping print() literals.
    lua = build_spy_mission(5, 'EVIL") do print("pwned") end --', 10, 12)
    assert "UNKNOWN_MISSION" in lua
    assert lua.count('"') == 4
    assert '") ' not in lua

def test_build_spy_mission_unknown_type_still_names_valid_missions():
    from civ_mcp.lua.espionage import build_spy_mission
    lua = build_spy_mission(5, "NOPE", 10, 12)
    assert "NOPE" in lua and "SIPHON_FUNDS" in lua
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest "tests/test_parsers.py::test_build_spy_mission_unknown_type_cannot_inject_lua" -q`
Expected: FAIL — with the current `replace('"', '\\"')`, the payload's `"` survives as `\"`, so `lua.count('"')` is greater than 4.

- [ ] **Step 3: Write minimal implementation**

In `src/civ_mcp/lua/espionage.py`, add `import re` at the top (after `from __future__ import annotations`), and replace the escape line in the `op_hash is None` branch:

```python
    if op_hash is None:
        valid = ", ".join(k for k in _SPY_OP_HASHES if k != "TRAVEL")
        # This is only echoed into an error message. Whitelist to a display-safe
        # subset so a crafted action string cannot break out of the Lua literal
        # (backslash+quote escaping is too fragile — see review finding #1).
        safe_mission = re.sub(r"[^A-Za-z0-9_ ]", "", mission_type)[:40] or "?"
        return " ".join(
            [
                f'print("ERR:UNKNOWN_MISSION|Unknown mission type {safe_mission}. Valid missions: {valid}")',
                f'print("{sentinel}")',
            ]
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest "tests/test_parsers.py::test_build_spy_mission_unknown_type_cannot_inject_lua" "tests/test_parsers.py::test_build_spy_mission_unknown_type_still_names_valid_missions" -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/lua/espionage.py tests/test_parsers.py
git commit -m "fix(arena): sanitize spy_action unknown-mission echo to block Lua injection"
```

---

### Task 2: Coerce numeric args reaching bare Lua (Finding #6 + standing OPEN ITEM — security)

Two related gaps of the same class:
- `build_form_formation` interpolates `unit_index`/`merge_unit_index` into InGame Lua with no `int()` cast (Finding #6). Its sibling `build_unit_operation` was deliberately hardened with `x, y = int(x), int(y)`; match it. (Not reachable today because the registry wrapper's `_unit_index()` already `int()`s, but the builder must self-defend like its sibling.)
- The pre-existing arena coordinate wrappers pass `x`/`y`/`target_x`/`target_y` (and `unit_index`) raw into builders that interpolate them into bare Lua (`build_move_unit`, `build_city_attack`, `build_attack_unit`, `build_purchase_tile`, `build_make_trade_route`, `build_teleport`, `build_map_area`, `build_pathing_estimate` — verified: none int-cast internally). The server/FastMCP path is already type-coerced; only the arena path is exposed. Close it at the registry choke point (the standing OPEN ITEM).

Scope note: this task coerces the coordinate + unit-index numeric args flagged by the review. `city_id`/`player_id` args that also reach Lua raw are the logical next increment — record that in Task 8's checklist, do not expand this task.

**Files:**
- Modify: `src/civ_mcp/lua/units.py:1356-1373` (`build_form_formation`)
- Modify: `src/civ_mcp/arena/registry.py` — wrappers at lines 231, 236, 269, 274, 515, 600, 875, 1154, 1465, 1496
- Test: `tests/test_parsers.py` (builder), `tests/arena/test_registry.py` (wrappers)

**Interfaces:**
- Consumes: `dispatch(gs, name, args, allowed=None)` from `civ_mcp.arena.registry` — with `allowed=None`, any registered tool dispatches; a wrapper that raises `ValueError` on a bad arg surfaces as that exception out of `dispatch`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_parsers.py`:

```python
def test_build_form_formation_coerces_indices_and_rejects_injection():
    from civ_mcp.lua.units import build_form_formation
    # numeric strings are accepted (coerced)
    lua = build_form_formation("3", "7", "FORM_CORPS")
    assert "GetUnit(me, 7)" in lua
    # a crafted string index cannot reach Lua — it raises at the builder
    with pytest.raises((ValueError, TypeError)):
        build_form_formation(3, "7} print(1) --", "FORM_CORPS")
```

Add to `tests/arena/test_registry.py`:

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("name,args", [
    ("move_unit", {"unit_index": 1, "x": "0} print(1) --", "y": 5}),
    ("attack_unit", {"unit_index": 1, "x": 3, "y": "9)--"}),
    ("purchase_tile", {"city_id": 1, "x": "z", "y": 2}),
    ("city_attack", {"city_id": 1, "target_x": "q", "target_y": 2}),
    ("start_trade_route", {"unit_id": 65537, "target_x": 1, "target_y": "b"}),
    ("teleport_trader", {"unit_id": 65537, "target_x": "a", "target_y": 2}),
    ("rebase_unit", {"unit_id": 65537, "target_x": "a", "target_y": 2}),
    ("excavate_artifact", {"unit_id": 65537, "target_x": 1, "target_y": "b"}),
    ("get_map_area", {"x": "z", "y": 2}),
    ("get_pathing_estimate", {"unit_index": 1, "x": "q", "y": 2}),
])
async def test_dispatch_coerces_coordinates_and_rejects_injection(name, args):
    """A non-numeric coordinate must raise before any Lua is built, so an
    in-process LLM civ cannot inject Lua through a coordinate string."""
    class FakeGS:
        async def move_unit(self, *a): raise AssertionError("must not reach GS")
        async def attack_unit(self, *a): raise AssertionError("must not reach GS")
        async def purchase_tile(self, *a): raise AssertionError("must not reach GS")
        async def city_attack(self, *a): raise AssertionError("must not reach GS")
        async def make_trade_route(self, *a): raise AssertionError("must not reach GS")
        async def teleport_to_city(self, *a): raise AssertionError("must not reach GS")
        async def rebase_unit(self, *a): raise AssertionError("must not reach GS")
        async def excavate_artifact(self, *a): raise AssertionError("must not reach GS")
        async def get_map_area(self, *a): raise AssertionError("must not reach GS")
        async def get_pathing_estimate(self, *a): raise AssertionError("must not reach GS")
    with pytest.raises((ValueError, TypeError)):
        await dispatch(FakeGS(), name, args)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest "tests/test_parsers.py::test_build_form_formation_coerces_indices_and_rejects_injection" "tests/arena/test_registry.py::test_dispatch_coerces_coordinates_and_rejects_injection" -q`
Expected: FAIL — the crafted string coordinates currently pass through into the returned Lua / to the (asserting) GS method rather than raising `ValueError`.

- [ ] **Step 3a: Harden the builder (`units.py`)**

In `build_form_formation`, immediately after the `command` validation, add the cast (mirrors `build_unit_operation`'s `x, y = int(x), int(y)`):

```python
    if command not in _FORMATION_COMMANDS:
        raise ValueError(f"unknown formation command: {command!r}")
    unit_index, merge_unit_index = int(unit_index), int(merge_unit_index)
```

- [ ] **Step 3b: Harden the registry wrappers (`registry.py`)**

Wrap each flagged coordinate/index arg in `int(...)`. Exact replacements:

`_rebase_unit_text` (231) and `_excavate_artifact_text` (236):
```python
    return await gs.rebase_unit(
        _unit_index(args["unit_id"]), int(args["target_x"]), int(args["target_y"]))
```
```python
    return await gs.excavate_artifact(
        _unit_index(args["unit_id"]), int(args["target_x"]), int(args["target_y"]))
```

`_start_trade_route_text` (269) and `_teleport_trader_text` (274):
```python
    return await gs.make_trade_route(unit_index, int(args["target_x"]), int(args["target_y"]))
```
```python
    return await gs.teleport_to_city(unit_index, int(args["target_x"]), int(args["target_y"]))
```

The four inline lambdas (515, 600, 875, 1154):
```python
        lambda gs, args: gs.move_unit(int(args["unit_index"]), int(args["x"]), int(args["y"])),
```
```python
        lambda gs, args: gs.attack_unit(int(args["unit_index"]), int(args["x"]), int(args["y"])),
```
```python
        lambda gs, args: gs.purchase_tile(args["city_id"], int(args["x"]), int(args["y"])),
```
```python
        lambda gs, args: gs.city_attack(args["city_id"], int(args["target_x"]), int(args["target_y"])),
```

`_narrate_map` (1465) and `_narrate_pathing_estimate` (1496):
```python
        await gs.get_map_area(int(args["x"]), int(args["y"]), radius),
```
```python
    est = await gs.get_pathing_estimate(int(args["unit_index"]), int(args["x"]), int(args["y"]))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest "tests/test_parsers.py::test_build_form_formation_coerces_indices_and_rejects_injection" "tests/arena/test_registry.py::test_dispatch_coerces_coordinates_and_rejects_injection" -q`
Expected: PASS

Then run the existing registry suite to confirm no regression in the coordinate paths:
Run: `uv run pytest tests/arena/test_registry.py -q`
Expected: PASS (existing `test_dispatch_maps_args` etc. still green — legitimate int coordinates are unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/lua/units.py src/civ_mcp/arena/registry.py tests/test_parsers.py tests/arena/test_registry.py
git commit -m "fix(arena): int-coerce coordinate/index args reaching bare Lua (finding #6 + open item)"
```

---

### Task 3: Capability snapshot — naval formations + fail-open enum guard (Findings #2, #3)

Two issues in the formation block of `_CAPS_LUA`:
- **#2:** the mergeable-pair count only considers `FORMATION_CLASS_LAND_COMBAT`, so a naval-only roster with Nationalism can never satisfy `corps` and `form_corps` (Fleet) stays hidden. Also count `FORMATION_CLASS_NAVAL`.
- **#3:** `MilitaryFormationTypes.CORPS_FORMATION`/`STANDARD_FORMATION` are PROBE-live/unverified. If a name is wrong, the index is `nil` (no raise), so `pcall` cannot rescue it and `corps`/`army` fail **closed** — violating the module's fail-open doctrine. Guard: only override the fail-open defaults for `corps`/`army` when both enum constants resolve.

**Files:**
- Modify: `src/civ_mcp/arena/capabilities.py:54-55` (naval) and `:61-66` (enum guard)
- Test: `tests/arena/test_capabilities.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/arena/test_capabilities.py`:

```python
def test_caps_query_counts_naval_formations_and_guards_enum():
    lua = build_caps_query(3)
    # #2: Fleets (naval) count toward the corps mergeable-pair check
    assert "FORMATION_CLASS_NAVAL" in lua
    # #3: corps/army only override their fail-open default when the enum resolves
    assert "MilitaryFormationTypes.CORPS_FORMATION ~= nil" in lua
    assert "MilitaryFormationTypes.STANDARD_FORMATION ~= nil" in lua
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest "tests/arena/test_capabilities.py::test_caps_query_counts_naval_formations_and_guards_enum" -q`
Expected: FAIL — neither `FORMATION_CLASS_NAVAL` nor the `~= nil` guards are present yet.

- [ ] **Step 3: Write the implementation**

In `src/civ_mcp/arena/capabilities.py`, change the pair-count formation-class test (currently lines 54-55):

```lua
            if info and (info.FormationClass == "FORMATION_CLASS_LAND_COMBAT"
                    or info.FormationClass == "FORMATION_CLASS_NAVAL")
                    and mf == MilitaryFormationTypes.STANDARD_FORMATION then
```

And replace the two unconditional corps/army assignments (currently lines 65-66):

```lua
    -- Only override the fail-open defaults when the formation enums resolved:
    -- a wrong constant name yields nil (no raise) and would fail these CLOSED.
    if MilitaryFormationTypes.CORPS_FORMATION ~= nil
            and MilitaryFormationTypes.STANDARD_FORMATION ~= nil then
        flags.corps = natl and pair
        flags.army = mob and corpsOwned
    end
```

(Leave the `if mf == MilitaryFormationTypes.CORPS_FORMATION then corpsOwned = true end` line as-is — a nil comparison there is a harmless `false`, and the guard above prevents it from mattering when the enum is missing.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/arena/test_capabilities.py -q`
Expected: PASS (new test passes; existing `test_build_caps_query_shape` and parse tests unaffected).

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/capabilities.py tests/arena/test_capabilities.py
git commit -m "fix(arena): count naval formations + fail-open guard on formation enums (findings #2,#3)"
```

---

### Task 4: Climate parser robustness (Finding #4)

`parse_climate_response` uses bare `int()` on Lua-concatenated `GameClimate` values that may print as floats (e.g. `2.0`), which raises `ValueError`; because it mutates `status` field-by-field inside a `try/except: continue`, a mid-line failure leaves a half-updated status. Use the repo's `_int()` (`int(float(s))`, used by every other parser) and assign the three CLIMATE fields atomically.

**Files:**
- Modify: `src/civ_mcp/lua/climate.py:10` (import) and `:36-51` (parser)
- Test: `tests/test_parsers.py` (class `TestParseClimate`)

- [ ] **Step 1: Write the failing tests**

Add to `TestParseClimate` in `tests/test_parsers.py`:

```python
    def test_float_formatted_values_parse(self):
        st = parse_climate_response(["CLIMATE|2.0|1.0|317.0", "---END---"])
        assert (st.phase, st.sea_level, st.co2_total) == (2, 1, 317)

    def test_bad_value_does_not_half_update(self):
        # a non-numeric field must leave the whole status at its unavailable default,
        # not a partially-written mix.
        st = parse_climate_response(["CLIMATE|2|1|notanumber"])
        assert (st.phase, st.sea_level, st.co2_total) == (-1, -1, -1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest "tests/test_parsers.py::TestParseClimate::test_float_formatted_values_parse" "tests/test_parsers.py::TestParseClimate::test_bad_value_does_not_half_update" -q`
Expected: FAIL — `int("2.0")` raises, and the half-update test currently sees `phase == 2` (written before the bad co2 field raised).

- [ ] **Step 3: Write the implementation**

In `src/civ_mcp/lua/climate.py`, change the import (line 10):

```python
from civ_mcp.lua._helpers import SENTINEL, _int
```

Replace the parser body (lines 36-51):

```python
def parse_climate_response(lines: list[str]) -> ClimateStatus:
    status = ClimateStatus(phase=-1, sea_level=-1, co2_total=-1)
    for line in lines:
        parts = line.split("|")
        try:
            if parts[0] == "CLIMATE" and len(parts) >= 4:
                # parse all three before assigning any — a bad field must not
                # leave a half-updated status.
                phase, sea, co2 = _int(parts[1]), _int(parts[2]), _int(parts[3])
                status.phase, status.sea_level, status.co2_total = phase, sea, co2
            elif parts[0] == "DISASTER" and len(parts) >= 5:
                status.disasters.append(DisasterEvent(
                    kind=parts[1], x=_int(parts[2]), y=_int(parts[3]),
                    turn=_int(parts[4])))
        except (ValueError, IndexError):
            continue
    return status
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest "tests/test_parsers.py::TestParseClimate" -q`
Expected: PASS (new tests + existing `test_full_status`/`test_unavailable_climate_system`/`test_empty_is_unavailable`).

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/lua/climate.py tests/test_parsers.py
git commit -m "fix(arena): use _int and atomic assignment in climate parser (finding #4)"
```

---

### Task 5: Informative result for gated tool calls (Findings #7, #5-message)

When the model calls a tool absent from `visible_names`, the agent classifies it (`gated`/`out_of_tier`/`unknown_tool`) for the transcript but then still calls `_dispatch`, which raises `KeyError(name)` — surfaced to the model as `ERROR: KeyError('spy_action')`, an uninformative result that invites retry churn (the very churn gating exists to reduce). Keep the spec's dispatch-allowlist hard-block (do NOT let the tool through); replace the raw error with an informative message and skip the doomed `_dispatch` call. This resolves #7 and the message half of #5 (the per-turn snapshot staleness itself is accepted per the per-turn design).

**Files:**
- Modify: `src/civ_mcp/arena/agent.py:181-201` (the per-tool-call block in `LLMPolicy.__call__`) and a small module-level helper
- Test: `tests/arena/test_agent.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/arena/test_agent.py` (reuses `FakeBackendCapturesTools`/`FakeGSSpies` defined above the existing gating tests):

```python
@pytest.mark.asyncio
async def test_gated_call_returns_informative_result_not_keyerror():
    gs, be, cost = FakeGSSpies(), FakeBackendCapturesTools(), FakeCost()
    opts = CivOptions(max_steps=3, tools=["fortify_unit", "get_spies"])
    pol = LLMPolicy(be, cost, options=opts)
    out = await pol(gs, player_id=1, turn=2, caps={"spies": False})
    step = out["transcript"]["steps"][0]
    assert step["tool_name"] == "get_spies"
    assert step["tool_result_full"].startswith("UNAVAILABLE")
    assert "KeyError" not in step["tool_result_full"]
    assert gs.spy_calls == 0            # still hard-blocked, never dispatched
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest "tests/arena/test_agent.py::test_gated_call_returns_informative_result_not_keyerror" -q`
Expected: FAIL — the current result is `ERROR: KeyError('get_spies')`.

- [ ] **Step 3: Write the implementation**

In `src/civ_mcp/arena/agent.py`, add a module-level helper (near the top, after `_dispatch`):

```python
_UNAVAILABLE_REASON = {
    "gated": "not available this turn — it is era/state-gated and unlocks when you meet "
             "its requirement (research/civic/unit). Do not retry it now.",
    "out_of_tier": "not part of your toolset for this run.",
    "unknown_tool": "not a real tool.",
}

def _unavailable_result(name: str, reason: str) -> str:
    return f"UNAVAILABLE: {name} is {_UNAVAILABLE_REASON.get(reason, 'not available.')}"
```

Restructure the per-tool-call block so a not-visible name yields the informative result and skips `_dispatch`, while the visible path is unchanged. Replace the current block (lines 181-201):

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
                    result = _unavailable_result(tc["name"], reason)
                else:
                    try:
                        json.loads(tc["arguments"] or "{}")
                    except (json.JSONDecodeError, ValueError):
                        invalid_tool_calls.append({"tool_name": tc["name"], "arguments": tc["arguments"],
                                                   "reason": "bad_arguments"})
                    try:
                        result = await _dispatch(gs, tc["name"], tc["arguments"], visible_names)
                    except Exception as e:
                        result = f"ERROR: {e!r}"
```

(Only structural change: the `_dispatch` try/except moves inside the `else`; the not-visible branch now assigns `result`. Everything after — the `_s`/`steps.append`/tool-message append — is unchanged and runs for both branches.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/arena/test_agent.py -q`
Expected: PASS — the new test passes; the existing `test_caps_gate_schema_classification_and_dispatch` (asserts classification + `spy_calls == 0`) still passes; `test_out_of_tier_tool_never_executes` still passes. Note `tests/arena/test_registry.py::test_dispatch_rejects_out_of_allowed` is unaffected (it calls `dispatch()` directly, which still raises `KeyError`).

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/agent.py tests/arena/test_agent.py
git commit -m "fix(arena): informative UNAVAILABLE result for gated tool calls instead of raw KeyError (findings #5,#7)"
```

---

### Task 6: Great-works readout surfaces `city_id` (Finding #8)

`narrate_great_works` prints `city_name` but not `city_id`, yet its closing hint tells the agent to call `move_great_work` — whose `target_city_id` is numeric. `GreatWorkSlot` already carries `city_id: int`; include it so the readout provides the id it instructs the agent to use.

**Files:**
- Modify: `src/civ_mcp/narrate.py:2159-2171` (`narrate_great_works`)
- Test: `tests/test_parsers.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_parsers.py`:

```python
def test_narrate_great_works_surfaces_city_id():
    from civ_mcp import narrate as nr
    from civ_mcp.lua.models import GreatWorkSlot
    slot = GreatWorkSlot(city_id=65792, city_name="Lahore",
                         building="BUILDING_AMPHITHEATER", slot_index=0,
                         slot_type="GREATWORKSLOT_WRITING", work_index=17,
                         work_name="Ramayana")
    text = nr.narrate_great_works([slot])
    assert "65792" in text          # the numeric id move_great_work needs
    assert "Lahore" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest "tests/test_parsers.py::test_narrate_great_works_surfaces_city_id" -q`
Expected: FAIL — `65792` is not in the current output.

- [ ] **Step 3: Write the implementation**

In `src/civ_mcp/narrate.py`, change the per-slot line (currently 2166-2167) to include the id, and sharpen the hint (2169-2170):

```python
        out.append(f"{s.city_name} (city_id {s.city_id}) {s.building} slot {s.slot_index} "
                   f"({s.slot_type}): {content}")
    empty = sum(1 for s in slots if s.work_index < 0)
    out.append(f"{empty} empty slot(s). Theming bonus needs matching works in "
               f"one building; use move_great_work(target_city_id=...) to group them.")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest "tests/test_parsers.py::test_narrate_great_works_surfaces_city_id" -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/narrate.py tests/test_parsers.py
git commit -m "fix(arena): surface city_id in great-works readout for move_great_work (finding #8)"
```

---

### Task 7: Analyze metrics — exclude `gated`, cover new behavior tools (Findings #9, #10)

- **#9:** `_counted_invalid_calls` excludes `reason == "out_of_tier"` from `invalid_call_rate` but not `reason == "gated"`. Gated tools are deliberately hidden (same as out-of-tier), so counting them penalizes early-era/low-tech civs for tools the operator hid. Exclude both.
- **#10:** the behavior frozensets `_GREAT_PEOPLE_TOOLS`/`_RELIGION_WC_TOOLS` were not extended for the newly-exposed `activate_great_person`/`spread_religion`, so those behaviors are undercounted.

**Files:**
- Modify: `src/civ_mcp/arena/analyze.py:69-78` (`_counted_invalid_calls`), `:103-106` (`_GREAT_PEOPLE_TOOLS`), `:113-116` (`_RELIGION_WC_TOOLS`)
- Test: `tests/arena/test_analyze.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/arena/test_analyze.py`:

```python
def test_gated_calls_do_not_count_as_invalid_rate():
    from civ_mcp.arena.analyze import analyze
    records = [{
        "player_id": 1, "model": "m", "provider": "local", "driver": "in_process",
        "turn": 1, "step_count": 2, "steps": [],
        "invalid_tool_calls": [{"tool_name": "get_spies", "reason": "gated"}],
    }]
    report = analyze(records, [])
    assert report["by_player"][1]["rates"]["invalid_call_rate"] == 0.0

def test_behavior_counters_cover_new_slice4_tools():
    from civ_mcp.arena.analyze import (
        _count_tool_calls, _GREAT_PEOPLE_TOOLS, _RELIGION_WC_TOOLS)
    gp = [{"tool_name": "activate_great_person", "tool_args": {}}]
    rel = [{"tool_name": "spread_religion", "tool_args": {}}]
    assert _count_tool_calls(gp, _GREAT_PEOPLE_TOOLS) == 1
    assert _count_tool_calls(rel, _RELIGION_WC_TOOLS) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest "tests/arena/test_analyze.py::test_gated_calls_do_not_count_as_invalid_rate" "tests/arena/test_analyze.py::test_behavior_counters_cover_new_slice4_tools" -q`
Expected: FAIL — `gated` currently counts (rate > 0), and neither new tool is in its frozenset (count 0).

- [ ] **Step 3: Write the implementation**

In `src/civ_mcp/arena/analyze.py`, change the exclusion in `_counted_invalid_calls` (line 75):

```python
        if item.get("reason") in ("out_of_tier", "gated"):
            continue
```

Add `activate_great_person` to `_GREAT_PEOPLE_TOOLS`:

```python
_GREAT_PEOPLE_TOOLS: frozenset[str] = frozenset({
    "recruit_great_person", "patronize_great_person", "reject_great_person",
    "get_great_people", "get_gp_advisor", "activate_great_person",
})
```

Add `spread_religion` to `_RELIGION_WC_TOOLS`:

```python
_RELIGION_WC_TOOLS: frozenset[str] = frozenset({
    "found_religion", "get_religion_beliefs", "get_religion_spread",
    "queue_wc_votes", "get_world_congress", "spread_religion",
})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/arena/test_analyze.py -q`
Expected: PASS (new tests + existing `test_out_of_tier_calls_do_not_count_as_invalid_rate_or_hallucination`).

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/analyze.py tests/arena/test_analyze.py
git commit -m "fix(arena): exclude gated from invalid-call rate; count new GP/religion behaviors (findings #9,#10)"
```

---

### Task 8: Extend the live-probe checklist (documents the residual live-only risks)

Several fixes above depend on engine facts only verifiable in a live game. Record them in the merge-gate checklist so they are probed before this branch reaches a live run. This is a docs-only task.

**Files:**
- Modify: `docs/superpowers/plans/2026-07-07-arena-slice4-live-probes.md`

- [ ] **Step 1: Append the checklist items**

Add a new section to the live-probes doc:

```markdown
## Review-fix probes (2026-07-08)

- [ ] **Formation enum constants** (capabilities.py, finding #3): in a live InGame
      context, confirm `MilitaryFormationTypes.CORPS_FORMATION` and
      `MilitaryFormationTypes.STANDARD_FORMATION` are non-nil (the live-verified
      spelling elsewhere in the repo is the longer `STANDARD_MILITARY_FORMATION`).
      If either is nil, the fail-open guard keeps `corps`/`army` exposed but the
      detection is inert — fix the constant name and capture the correct enum.
- [ ] **Naval Fleet gating** (capabilities.py, finding #2): with a Nationalism-era
      naval-only roster, confirm `corps` reports 1 (a Fleet-eligible pair is
      detected), i.e. `form_corps` is exposed for naval civs.
- [ ] **GameClimate numeric format** (climate.py, finding #4): capture a real
      `CLIMATE|` line in a Gathering-Storm game and confirm the parser handles the
      actual formatting (integer vs float) of `GetClimateChangeLevel` /
      `GetSeaLevel` / `GetTotalCO2Footprint`.
- [ ] **Residual id-arg coercion** (registry.py, follow-up to finding #6 sweep):
      the coordinate/unit-index sweep did not cover `city_id`/`player_id` args that
      also interpolate into Lua (e.g. via `_lua_get_city`'s `{city_id} % 65536`).
      Decide whether to extend the int-coercion sweep to those id args or accept
      them as validated upstream.
```

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/plans/2026-07-07-arena-slice4-live-probes.md
git commit -m "docs(arena): add review-fix live-probe items (findings #2,#3,#4 + id-arg follow-up)"
```

---

## Self-Review

- **Coverage:** Findings #1 (T1), #2/#3 (T3), #4 (T4), #5 (T5, message half; snapshot staleness accepted per design), #6 + OPEN ITEM (T2), #7 (T5), #8 (T6), #9/#10 (T7); live-only residuals (T8). The refuted backends item is intentionally untouched.
- **Placeholder scan:** every code step carries verbatim code; no TODO/TBD.
- **Type consistency:** `_int` returns `int`; `_unavailable_result(name, reason) -> str`; `build_form_formation`/coordinate wrappers raise `ValueError`/`TypeError` on non-numeric input; frozenset additions are plain strings matching `_step_verb` tool_base names.
- **Constraint check:** no `_dispatch` schema refactor; dispatch allowlist hard-block preserved (only the message changed); all gating changes fail open; `run_lua` untouched; branch stays unmerged.

## Execution Handoff

Plan complete. Recommended execution: **Subagent-Driven Development** (fresh implementer per task + spec/quality review between tasks), matching how the rest of this slice was built. Tasks 1 and 2 (security) first; the remainder are independent and may run in any order. Task 8 is docs-only and can be batched last.
