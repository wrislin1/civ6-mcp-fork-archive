# Arena Standing Memory Task Tracker Follow-Up Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct the second-review regressions in standing memory, deterministic task tracking, and CLI summary capture before the live arena run.

**Architecture:** Keep the existing Slice 3 structure. Fix task execution in `task_tracker.py`, standing-plan parsing and freshness in `memory.py`, shared option math in `config.py`, and policy/coordinator call sites where those helpers are consumed. Do not introduce a new task lifecycle unless tests prove the current active-task model cannot express the behavior.

**Tech Stack:** Python 3, asyncio, pytest/pytest-asyncio, Civ 6 MCP arena modules.

---

## Verification Notes

Review findings checked against branch `arena-standing-memory-task-tracker-slice3` at `404e1a7`.

- Confirmed: invalid `builder_improve` currently changes status to `failed`, so `save_task_state()` drops a recoverable task.
- Confirmed: `_is_section_header()` currently returns `False` for every markdown bullet, including `- TACTICAL:`.
- Confirmed: `_hostile_owner_context()` awaits diplomacy then threat scan serially and uses `block_unknown=True` on helper failures.
- Confirmed: standing memory reports age but has no max-age gate.
- Confirmed: CLI summary clamp uses `max(1200, standing_plan_capture_chars)`, so `memory.max_chars > 4000` can exceed the intended CLI cap.
- Confirmed: task capture budget is a fixed 4000 chars for all task-tracker configs.
- Confirmed: briefing budget/build logic remains repeated across `agent.py`, `cli_agent.py`, and `coordinator.py`.

## File Structure

- Modify `src/civ_mcp/arena/task_tracker.py`
  - Keep transiently invalid builder-improve tasks active.
  - Fetch diplomacy and threat scan concurrently.
  - Track exact hostile coordinates from threat scan and stop blocking unknown labels on helper failures.
- Modify `tests/arena/test_task_tracker.py`
  - Cover recoverable builder retry.
  - Cover non-stalling helper failures and exact threat-scan coordinate blocking.
  - Cover concurrent diplomacy/threat-scan fetching.
- Modify `src/civ_mcp/arena/memory.py`
  - Treat known bulleted reflection headers as section terminators.
  - Add optional max-age gating to memory formatting.
- Modify `tests/arena/test_memory.py`
  - Cover `- TACTICAL:` termination without regressing `- BUILD CAMPUS:`.
  - Cover memory TTL omission and max-age boundary.
- Modify `src/civ_mcp/arena/config.py`
  - Add `MemoryOptions.max_age_turns`.
  - Add dynamic task capture math and bounded CLI summary math.
- Modify `src/civ_mcp/arena/experiment.py`
  - Parse `memory.max_age_turns` from YAML.
- Modify `tests/arena/test_config.py` and `tests/arena/test_experiment.py`
  - Cover new memory option and summary/capture properties.
- Modify `src/civ_mcp/arena/cli_agent.py`
  - Use the shared bounded summary property.
- Create `src/civ_mcp/arena/prompt_context.py`
  - Centralize "use supplied briefing or build one with budget" behavior.
- Create `tests/arena/test_prompt_context.py`
  - Cover supplied empty briefing passthrough and budgeted build.
- Modify `src/civ_mcp/arena/agent.py`, `src/civ_mcp/arena/cli_agent.py`, and `src/civ_mcp/arena/coordinator.py`
  - Consume `maybe_build_briefing()`.

---

### Task 1: Keep Recoverable Builder Improve Tasks Active

**Files:**
- Modify: `src/civ_mcp/arena/task_tracker.py`
- Test: `tests/arena/test_task_tracker.py`

- [ ] **Step 1: Replace the invalid-improvement test expectation**

In `tests/arena/test_task_tracker.py`, replace `test_builder_improve_blocks_when_improvement_not_valid` with:

```python
@pytest.mark.asyncio
async def test_builder_improve_keeps_task_active_when_improvement_not_currently_valid():
    unit = _unit(unit_id=65538, unit_index=2, x=12, y=19, valid_improvements=[])
    gs = FakeGS(units=[unit])
    task = _task(
        task_id="builder_improve:65538",
        kind="builder_improve",
        unit_id=65538,
        target_x=12,
        target_y=19,
        improvement="IMPROVEMENT_FARM",
    )

    updated, results = await run_pre_model_tasks(gs, [task])

    assert gs.improve_tile_calls == []
    assert updated[0].status == "active"
    assert updated[0].last_result == "blocked_improvement_not_valid"
    assert results[0]["status"] == "active"
    assert results[0]["action"] == "block"
    assert results[0]["result"] == "blocked_improvement_not_valid"
```

- [ ] **Step 2: Add a retry regression**

Add this test immediately after the previous test:

```python
@pytest.mark.asyncio
async def test_builder_improve_retries_after_transient_invalid_improvement_becomes_valid():
    invalid_unit = _unit(
        unit_id=65538,
        unit_index=2,
        x=12,
        y=19,
        valid_improvements=[],
    )
    task = _task(
        task_id="builder_improve:65538",
        kind="builder_improve",
        unit_id=65538,
        target_x=12,
        target_y=19,
        improvement="IMPROVEMENT_FARM",
    )

    first_gs = FakeGS(units=[invalid_unit])
    updated, first_results = await run_pre_model_tasks(first_gs, [task])

    valid_unit = _unit(
        unit_id=65538,
        unit_index=2,
        x=12,
        y=19,
        valid_improvements=["IMPROVEMENT_FARM"],
    )
    second_gs = FakeGS(units=[valid_unit])
    retried, retry_results = await run_pre_model_tasks(second_gs, updated)

    assert first_results[0]["result"] == "blocked_improvement_not_valid"
    assert second_gs.improve_tile_calls == [(2, "IMPROVEMENT_FARM")]
    assert retried[0].status == "complete"
    assert retry_results[0]["action"] == "improve"
```

- [ ] **Step 3: Run the focused tests and verify failure**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_task_tracker.py::test_builder_improve_keeps_task_active_when_improvement_not_currently_valid tests/arena/test_task_tracker.py::test_builder_improve_retries_after_transient_invalid_improvement_becomes_valid -q
```

Expected: FAIL because the current implementation returns status `failed`.

- [ ] **Step 4: Keep invalid improvements active**

In `src/civ_mcp/arena/task_tracker.py`, replace the invalid-improvement branch inside `_run_single_task()` with:

```python
        new_task = replace(task, last_result="blocked_improvement_not_valid")
        return new_task, _result_dict(
            task,
            status="active",
            action="block",
            result="blocked_improvement_not_valid",
        )
```

This leaves the builder available for the model to chop, research toward the tech, move, or emit `CANCEL unit_id=...`, while preserving the deterministic retry path.

- [ ] **Step 5: Run focused task-tracker tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_task_tracker.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/civ_mcp/arena/task_tracker.py tests/arena/test_task_tracker.py
git commit -m "fix(arena): keep recoverable builder tasks active"
```

---

### Task 2: Stop Standing Plan Extraction At Bulleted Reflection Headers

**Files:**
- Modify: `src/civ_mcp/arena/memory.py`
- Test: `tests/arena/test_memory.py`

- [ ] **Step 1: Add the bulleted reflection-header regression**

In `tests/arena/test_memory.py`, add this test after `test_extract_standing_plan_keeps_all_caps_bullet_ending_colon`:

```python
def test_extract_standing_plan_stops_at_bulleted_reflection_header():
    summary = (
        "STANDING PLAN:\n"
        "- Keep settler 123 marching to (18,24).\n"
        "- TASK settle unit_id=123 target=18,24\n"
        "- TACTICAL:\n"
        "- Settler moved one tile this turn.\n"
    )

    result = extract_standing_plan(summary, max_chars=1200)

    assert result == (
        "Keep settler 123 marching to (18,24).\n"
        "TASK settle unit_id=123 target=18,24"
    )
```

- [ ] **Step 2: Run the focused extraction tests and verify failure**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_memory.py::test_extract_standing_plan_keeps_all_caps_bullet_ending_colon tests/arena/test_memory.py::test_extract_standing_plan_stops_at_bulleted_reflection_header -q
```

Expected: FAIL because the extracted plan includes `TACTICAL:` and the reflection text.

- [ ] **Step 3: Add known bulleted section terminators**

In `src/civ_mcp/arena/memory.py`, add this constant near `_BULLET_PREFIX_RE`:

```python
_BULLETED_SECTION_HEADERS = frozenset(
    {
        "TACTICAL",
        "STRATEGIC",
        "TOOLING",
        "PLANNING",
        "HYPOTHESIS",
        "STRATEGIC NOTES",
    }
)
```

- [ ] **Step 4: Replace `_is_section_header()`**

Replace the function body with:

```python
def _is_section_header(line: str) -> bool:
    stripped = line.strip()
    if not stripped.endswith(":"):
        return False

    bullet = _BULLET_PREFIX_RE.match(stripped)
    candidate = _strip_bullet(stripped) if bullet else stripped
    body = candidate[:-1].strip()
    if not body or not body.isupper():
        return False

    if bullet:
        return body in _BULLETED_SECTION_HEADERS
    return True
```

This keeps unbulleted all-caps headers such as `STRATEGIC NOTES:` as terminators, while treating `- BUILD CAMPUS:` as plan content unless the bullet body is a known reflection header.

- [ ] **Step 5: Run focused memory tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_memory.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/civ_mcp/arena/memory.py tests/arena/test_memory.py
git commit -m "fix(arena): stop plan capture at bulleted reflection headers"
```

---

### Task 3: Make Hostility Context Concurrent And Non-Stalling On Helper Failures

**Files:**
- Modify: `src/civ_mcp/arena/task_tracker.py`
- Test: `tests/arena/test_task_tracker.py`

- [ ] **Step 1: Replace helper-failure expectations**

In `tests/arena/test_task_tracker.py`, replace `test_diplomacy_failure_blocks_visible_foreign_unit` with:

```python
@pytest.mark.asyncio
async def test_diplomacy_failure_does_not_block_unconfirmed_foreign_unit():
    unit = _unit(unit_id=65537, unit_index=1, x=1, y=1)
    gs = FakeGS(
        units=[unit],
        map_tiles={(18, 24): [_tile(18, 24, units=["Rome WARRIOR"])]},
        diplomacy_error=RuntimeError("diplomacy unavailable"),
    )
    task = _task(task_id="settle:65537", unit_id=65537, target_x=18, target_y=24)

    updated, results = await run_pre_model_tasks(gs, [task])

    assert gs.move_unit_calls == [(1, 18, 24)]
    assert updated[0].status == "active"
    assert results[0]["action"] == "move"
```

Replace `test_threat_scan_failure_blocks_unknown_foreign_unit` with:

```python
@pytest.mark.asyncio
async def test_threat_scan_failure_does_not_block_unknown_city_state_unit():
    unit = _unit(unit_id=65537, unit_index=1, x=1, y=1)
    gs = FakeGS(
        units=[unit],
        map_tiles={(18, 24): [_tile(18, 24, units=["Vatican City WARRIOR"])]},
        diplomacy=[SimpleNamespace(civ_name="Rome", is_at_war=False)],
        threat_scan_error=RuntimeError("threat scan unavailable"),
    )
    task = _task(task_id="settle:65537", unit_id=65537, target_x=18, target_y=24)

    updated, results = await run_pre_model_tasks(gs, [task])

    assert gs.move_unit_calls == [(1, 18, 24)]
    assert updated[0].status == "active"
    assert results[0]["action"] == "move"
```

- [ ] **Step 2: Add exact threat-scan coordinate coverage**

Add this test after the helper-failure tests:

```python
@pytest.mark.asyncio
async def test_diplomacy_failure_still_blocks_exact_threat_scan_coordinate():
    unit = _unit(unit_id=65537, unit_index=1, x=1, y=1)
    gs = FakeGS(
        units=[unit],
        map_tiles={(18, 24): [_tile(18, 24, units=["Rome WARRIOR"])]},
        diplomacy_error=RuntimeError("diplomacy unavailable"),
        threat_scan=[
            SimpleNamespace(
                x=18,
                y=24,
                owner_name="Rome",
                is_city_state=False,
            )
        ],
    )
    task = _task(task_id="settle:65537", unit_id=65537, target_x=18, target_y=24)

    updated, results = await run_pre_model_tasks(gs, [task])

    assert gs.move_unit_calls == []
    assert updated[0].last_result == "blocked_visible_hostile"
    assert results[0]["action"] == "block"
```

- [ ] **Step 3: Add concurrent fetch coverage**

Add this test near the other hostile-context tests:

```python
@pytest.mark.asyncio
async def test_hostile_context_fetches_diplomacy_and_threat_scan_concurrently():
    class ConcurrentGS(FakeGS):
        def __init__(self):
            super().__init__(
                units=[_unit(unit_id=65537, unit_index=1, x=1, y=1)],
                map_tiles={(18, 24): []},
            )
            self.threat_started = False
            self.diplomacy_resumed_after_threat_started = False

        async def get_diplomacy(self):
            await asyncio.sleep(0)
            self.diplomacy_resumed_after_threat_started = self.threat_started
            return self.diplomacy

        async def get_threat_scan(self):
            self.threat_started = True
            await asyncio.sleep(0)
            return self.threat_scan

    gs = ConcurrentGS()
    task = _task(task_id="settle:65537", unit_id=65537, target_x=18, target_y=24)

    await run_pre_model_tasks(gs, [task])

    assert gs.diplomacy_resumed_after_threat_started is True
```

Add the missing import at the top of the test file:

```python
import asyncio
from types import SimpleNamespace
```

Replace the existing `from types import SimpleNamespace` line rather than adding a duplicate import.

- [ ] **Step 4: Run focused tests and verify failure**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_task_tracker.py::test_diplomacy_failure_does_not_block_unconfirmed_foreign_unit tests/arena/test_task_tracker.py::test_threat_scan_failure_does_not_block_unknown_city_state_unit tests/arena/test_task_tracker.py::test_diplomacy_failure_still_blocks_exact_threat_scan_coordinate tests/arena/test_task_tracker.py::test_hostile_context_fetches_diplomacy_and_threat_scan_concurrently -q
```

Expected: FAIL because the current code blocks unknown labels and fetches helper data serially.

- [ ] **Step 5: Update `_HostileOwnerContext`**

In `src/civ_mcp/arena/task_tracker.py`, replace `_HostileOwnerContext` with:

```python
@dataclass(frozen=True)
class _HostileOwnerContext:
    hostile_prefixes: tuple[str, ...]
    peaceful_prefixes: tuple[str, ...]
    hostile_coords: frozenset[tuple[int, int]]
```

Delete `_sorted_prefixes()` entirely.

- [ ] **Step 6: Add safe helper loaders**

Add these helpers above `_hostile_owner_context()`:

```python
async def _load_diplomacy_safely(gs: Any) -> tuple[Any, ...]:
    try:
        return tuple(await gs.get_diplomacy())
    except Exception:
        return ()


async def _load_threat_scan_safely(gs: Any) -> tuple[Any, ...]:
    try:
        return tuple(await gs.get_threat_scan())
    except Exception:
        return ()
```

- [ ] **Step 7: Replace `_hostile_owner_context()`**

Use this implementation:

```python
async def _hostile_owner_context(gs: Any) -> _HostileOwnerContext:
    hostile = {"Barbarian"}
    peaceful: set[str] = set()
    hostile_coords: set[tuple[int, int]] = set()

    civs, threats = await asyncio.gather(
        _load_diplomacy_safely(gs),
        _load_threat_scan_safely(gs),
    )

    for civ in civs:
        name = str(getattr(civ, "civ_name", "") or "").strip()
        if not name:
            continue
        if getattr(civ, "is_at_war", False):
            hostile.add(name)
        else:
            peaceful.add(name)

    for threat in threats:
        tx = getattr(threat, "x", None)
        ty = getattr(threat, "y", None)
        if type(tx) is int and type(ty) is int:
            hostile_coords.add((tx, ty))
        name = str(getattr(threat, "owner_name", "") or "").strip()
        if name:
            hostile.add(name)

    return _HostileOwnerContext(
        hostile_prefixes=tuple(hostile),
        peaceful_prefixes=tuple(peaceful),
        hostile_coords=frozenset(hostile_coords),
    )
```

- [ ] **Step 8: Replace `_tile_has_hostile_unit()`**

Use this implementation:

```python
def _tile_has_hostile_unit(tile: Any, owner_context: _HostileOwnerContext) -> bool:
    labels = tile.units or []
    if labels and (tile.x, tile.y) in owner_context.hostile_coords:
        return True

    for label in labels:
        label_text = str(label).strip()
        if not label_text:
            continue
        if any(
            _label_matches_owner(label_text, owner)
            for owner in owner_context.hostile_prefixes
        ):
            return True
    return False
```

No unknown-label fallback remains. A transient failure now only removes unconfirmed knowledge for that turn; confirmed barbarian prefixes, at-war major prefixes, and exact threat-scan coordinates still block.

- [ ] **Step 9: Run focused task-tracker tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_task_tracker.py -q
```

Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add src/civ_mcp/arena/task_tracker.py tests/arena/test_task_tracker.py
git commit -m "fix(arena): avoid stalling tasks on transient threat lookup failures"
```

---

### Task 4: Add Standing Memory TTL

**Files:**
- Modify: `src/civ_mcp/arena/config.py`
- Modify: `src/civ_mcp/arena/experiment.py`
- Modify: `src/civ_mcp/arena/memory.py`
- Modify: `src/civ_mcp/arena/coordinator.py`
- Test: `tests/arena/test_config.py`
- Test: `tests/arena/test_experiment.py`
- Test: `tests/arena/test_memory.py`

- [ ] **Step 1: Add memory TTL tests**

In `tests/arena/test_memory.py`, add these tests after `test_format_memory_block_surfaces_one_turn_old`:

```python
def test_format_memory_block_omits_stale_memory_when_max_age_exceeded():
    memory = StandingMemory(
        schema_version=1,
        run_id="run1",
        player_id=0,
        updated_turn=5,
        text="Keep marching.",
    )

    result = format_memory_block(memory, current_turn=16, max_age_turns=10)

    assert result == ""


def test_format_memory_block_includes_memory_at_max_age_boundary():
    memory = StandingMemory(
        schema_version=1,
        run_id="run1",
        player_id=0,
        updated_turn=5,
        text="Keep marching.",
    )

    result = format_memory_block(memory, current_turn=15, max_age_turns=10)

    assert result == "== STANDING PLAN (captured turn 5, 10 turns old) ==\nKeep marching."
```

- [ ] **Step 2: Add config and experiment tests**

In `tests/arena/test_config.py`, update memory fingerprint expectations by adding:

```python
def test_civ_options_memory_fingerprint_includes_max_age_turns():
    opts = CivOptions(memory=MemoryOptions(enabled=True, max_chars=900, max_age_turns=6))

    assert opts.fingerprint()["memory"] == {
        "enabled": True,
        "max_chars": 900,
        "max_age_turns": 6,
    }
```

In `tests/arena/test_experiment.py`, update `test_local_civ_parses_memory_and_task_tracker()` so the replacement YAML memory line is:

```python
        "    memory: {enabled: true, max_chars: 800, max_age_turns: 6}\n"
```

and the assertion is:

```python
    assert local.options.memory == MemoryOptions(
        enabled=True,
        max_chars=800,
        max_age_turns=6,
    )
```

Add this invalid-config test near the other memory validation tests:

```python
def test_memory_max_age_turns_must_be_positive(tmp_path):
    text = """
civs:
  - player: 1
    provider: cli-claude
    memory: {enabled: true, max_age_turns: 0}
"""

    with pytest.raises(ValueError, match="memory.max_age_turns must be a positive integer"):
        load_experiment(_write(tmp_path, text))
```

Also update the existing `test_civ_options_fingerprint_contains_memory_and_task_tracker()` in `tests/arena/test_experiment.py` (its memory assertion currently expects a two-key dict and will fail once `fingerprint()` emits `max_age_turns`). Replace:

```python
    assert fp["memory"] == {"enabled": True, "max_chars": 900}
```

with:

```python
    assert fp["memory"] == {"enabled": True, "max_chars": 900, "max_age_turns": 10}
```

- [ ] **Step 3: Run focused tests and verify failure**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_memory.py::test_format_memory_block_omits_stale_memory_when_max_age_exceeded tests/arena/test_memory.py::test_format_memory_block_includes_memory_at_max_age_boundary tests/arena/test_config.py::test_civ_options_memory_fingerprint_includes_max_age_turns tests/arena/test_experiment.py::test_local_civ_parses_memory_and_task_tracker tests/arena/test_experiment.py::test_memory_max_age_turns_must_be_positive -q
```

Expected: FAIL because `max_age_turns` does not exist and `format_memory_block()` does not accept it.

- [ ] **Step 4: Add `MemoryOptions.max_age_turns` and fingerprint output**

In `src/civ_mcp/arena/config.py`, replace `MemoryOptions` with:

```python
@dataclass(frozen=True)
class MemoryOptions:
    enabled: bool = False
    max_chars: int = 1200
    max_age_turns: int = 10
```

In `CivOptions.fingerprint()`, replace the memory entry with:

```python
            "memory": {
                "enabled": self.memory.enabled,
                "max_chars": self.memory.max_chars,
                "max_age_turns": self.memory.max_age_turns,
            },
```

- [ ] **Step 5: Parse memory TTL from experiment YAML**

In `src/civ_mcp/arena/experiment.py`, replace `_parse_memory()` with:

```python
def _parse_memory(civ_label: str, raw: object) -> MemoryOptions:
    if not isinstance(raw, dict):
        raise _err(civ_label, f"memory must be a mapping, got {raw!r}")
    _validate_mapping_keys(
        civ_label,
        raw,
        {"enabled", "max_chars", "max_age_turns"},
        "memory",
    )
    enabled = raw.get("enabled", _MEMORY_DEFAULTS.enabled)
    if not isinstance(enabled, bool):
        raise _err(civ_label, f"memory.enabled must be a boolean, got {enabled!r}")
    max_chars = _positive_int(
        civ_label,
        "memory.max_chars",
        raw.get("max_chars", _MEMORY_DEFAULTS.max_chars),
    )
    max_age_turns = _positive_int(
        civ_label,
        "memory.max_age_turns",
        raw.get("max_age_turns", _MEMORY_DEFAULTS.max_age_turns),
    )
    return MemoryOptions(
        enabled=enabled,
        max_chars=max_chars,
        max_age_turns=max_age_turns,
    )
```

- [ ] **Step 6: Add max-age gating to memory formatting**

In `src/civ_mcp/arena/memory.py`, replace the `format_memory_block()` signature and body with:

```python
def format_memory_block(
    memory: StandingMemory | None,
    *,
    current_turn: int | None = None,
    max_age_turns: int | None = None,
) -> str:
    """Render standing memory as a prompt-ready block, or "" if empty/absent/stale."""
    if memory is None or not memory.text:
        return ""
    age: int | None = None
    if current_turn is not None:
        age = max(0, current_turn - memory.updated_turn)
        if max_age_turns is not None and age > max_age_turns:
            return ""

    suffix = f"captured turn {memory.updated_turn}"
    if age is not None:
        if age == 1:
            suffix += ", 1 turn old"
        elif age != 0:
            suffix += f", {age} turns old"
    return f"== STANDING PLAN ({suffix}) ==\n{memory.text}"
```

- [ ] **Step 7: Thread TTL through coordinator injection**

In `src/civ_mcp/arena/coordinator.py`, replace:

```python
                memory_block = format_memory_block(memory, current_turn=st.turn)
```

with:

```python
                memory_block = format_memory_block(
                    memory,
                    current_turn=st.turn,
                    max_age_turns=opts.memory.max_age_turns,
                )
```

- [ ] **Step 8: Run focused arena memory/config tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_memory.py tests/arena/test_config.py tests/arena/test_experiment.py -q
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/civ_mcp/arena/config.py src/civ_mcp/arena/experiment.py src/civ_mcp/arena/memory.py src/civ_mcp/arena/coordinator.py tests/arena/test_config.py tests/arena/test_experiment.py tests/arena/test_memory.py
git commit -m "fix(arena): expire stale standing memory"
```

---

### Task 5: Restore Bounded CLI Summary Capture And Scale Task Budget

**Files:**
- Modify: `src/civ_mcp/arena/config.py`
- Modify: `src/civ_mcp/arena/cli_agent.py`
- Test: `tests/arena/test_config.py`
- Test: `tests/arena/test_cli_agent.py`

- [ ] **Step 1: Add option math tests**

In `tests/arena/test_config.py`, extend `test_civ_options_standing_plan_capture_chars()` with:

```python
    assert CivOptions(
        task_tracker=TaskTrackerOptions(enabled=True, max_tasks=12),
    ).standing_plan_capture_chars == 4480
```

Add this new test after it:

```python
def test_civ_options_standing_plan_summary_chars_is_bounded_for_cli():
    assert CivOptions().standing_plan_summary_chars == 500
    assert CivOptions(memory=MemoryOptions(enabled=True, max_chars=900)).standing_plan_summary_chars == 1200
    assert CivOptions(memory=MemoryOptions(enabled=True, max_chars=6000)).standing_plan_summary_chars == 4000
    assert CivOptions(task_tracker=TaskTrackerOptions(enabled=True)).standing_plan_summary_chars == 4000
    assert CivOptions(
        task_tracker=TaskTrackerOptions(enabled=True, max_tasks=12),
    ).standing_plan_summary_chars == 4480
```

- [ ] **Step 2: Add CLI clamp regression**

In `tests/arena/test_cli_agent.py`, add this test after `test_call_claude_summary_still_clamped_when_memory_disabled`:

```python
def test_call_claude_summary_caps_large_memory_capture_at_cli_bound(monkeypatch):
    class FakeProc:
        pid = 1
        returncode = 0

        async def communicate(self):
            blob = json.dumps({
                "type": "result",
                "subtype": "success",
                "result": "x" * 4500,
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "total_cost_usd": 0.0,
            })
            return (blob.encode(), b"")

        async def wait(self):
            pass

    async def fake_create(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    pol = CLIAgentPolicy(
        "cli-claude",
        FakeCost(),
        project_dir="/x",
        timeout_s=5,
        options=CivOptions(memory=MemoryOptions(enabled=True, max_chars=6000)),
    )
    result = asyncio.run(pol(None, player_id=1, turn=1))

    assert len(result["summary"]) == 4000
    assert len(result["transcript"]["final_summary"]) == 4000
```

- [ ] **Step 3: Run focused tests and verify failure**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_config.py::test_civ_options_standing_plan_capture_chars tests/arena/test_config.py::test_civ_options_standing_plan_summary_chars_is_bounded_for_cli tests/arena/test_cli_agent.py::test_call_claude_summary_caps_large_memory_capture_at_cli_bound -q
```

Expected: FAIL because `standing_plan_summary_chars` does not exist and the CLI policy uses the unbounded capture property.

- [ ] **Step 4: Add dynamic capture constants and properties**

In `src/civ_mcp/arena/config.py`, replace:

```python
STANDING_PLAN_CAPTURE_CHARS = 4000
```

with:

```python
STANDING_PLAN_CAPTURE_CHARS = 4000
STANDING_PLAN_BASE_TASK_CAP = 8
STANDING_PLAN_CHARS_PER_EXTRA_TASK = 120
```

In `CivOptions`, add this private helper property above `standing_plan_capture_chars`:

```python
    @property
    def _standing_plan_task_capture_chars(self) -> int:
        if not self.task_tracker.enabled:
            return 0
        extra_tasks = max(0, self.task_tracker.max_tasks - STANDING_PLAN_BASE_TASK_CAP)
        return STANDING_PLAN_CAPTURE_CHARS + (
            extra_tasks * STANDING_PLAN_CHARS_PER_EXTRA_TASK
        )
```

Replace `standing_plan_capture_chars` with:

```python
    @property
    def standing_plan_capture_chars(self) -> int:
        if not self.standing_plan_enabled:
            return 0
        capture_chars = self.memory.max_chars if self.memory.enabled else 0
        if self.task_tracker.enabled:
            capture_chars = max(capture_chars, self._standing_plan_task_capture_chars)
        return capture_chars
```

Add this public property below it:

```python
    @property
    def standing_plan_summary_chars(self) -> int:
        if not self.standing_plan_enabled:
            return 500
        desired_chars = max(1200, self.standing_plan_capture_chars)
        summary_cap = max(
            STANDING_PLAN_CAPTURE_CHARS,
            self._standing_plan_task_capture_chars,
        )
        return min(desired_chars, summary_cap)
```

- [ ] **Step 5: Use shared summary clamp in CLI policy**

In `src/civ_mcp/arena/cli_agent.py`, replace:

```python
        max_summary_chars = 500
        if include_standing_plan_instruction:
            max_summary_chars = max(1200, self.options.standing_plan_capture_chars)
```

with:

```python
        max_summary_chars = self.options.standing_plan_summary_chars
```

- [ ] **Step 6: Run focused config and CLI tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_config.py tests/arena/test_cli_agent.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/civ_mcp/arena/config.py src/civ_mcp/arena/cli_agent.py tests/arena/test_config.py tests/arena/test_cli_agent.py
git commit -m "fix(arena): bound cli standing-plan summaries"
```

---

### Task 6: Centralize Briefing Budget And Build Flow

**Files:**
- Create: `src/civ_mcp/arena/prompt_context.py`
- Create: `tests/arena/test_prompt_context.py`
- Modify: `src/civ_mcp/arena/agent.py`
- Modify: `src/civ_mcp/arena/cli_agent.py`
- Modify: `src/civ_mcp/arena/coordinator.py`

- [ ] **Step 1: Add tests for the shared helper**

Create `tests/arena/test_prompt_context.py`:

```python
import pytest

from civ_mcp.arena.briefing import Briefing
from civ_mcp.arena.budget import briefing_budget
from civ_mcp.arena.config import BriefingOptions, CivOptions
from civ_mcp.arena.prompt_context import maybe_build_briefing


@pytest.mark.asyncio
async def test_maybe_build_briefing_returns_supplied_empty_briefing(monkeypatch):
    async def fail_build_briefing(*args, **kwargs):
        raise AssertionError("supplied briefing must be authoritative")

    monkeypatch.setattr(
        "civ_mcp.arena.prompt_context.build_briefing",
        fail_build_briefing,
    )
    supplied = Briefing()
    options = CivOptions(briefing=BriefingOptions(enabled=True))

    result = await maybe_build_briefing(
        object(),
        options,
        n_ctx=8192,
        playbook_chars=0,
        tool_schema_chars=0,
        supplied=supplied,
    )

    assert result is supplied


@pytest.mark.asyncio
async def test_maybe_build_briefing_builds_with_shared_budget(monkeypatch):
    captured = {}

    async def fake_build_briefing(gs, opts, budget):
        captured["gs"] = gs
        captured["opts"] = opts
        captured["budget"] = budget
        return Briefing(text="brief", tokens=1, sections=("overview",), radius=3)

    monkeypatch.setattr(
        "civ_mcp.arena.prompt_context.build_briefing",
        fake_build_briefing,
    )
    gs = object()
    options = CivOptions(
        max_steps=2,
        result_char_cap=600,
        briefing=BriefingOptions(enabled=True, sections=("overview",)),
    )

    result = await maybe_build_briefing(
        gs,
        options,
        n_ctx=8192,
        playbook_chars=400,
        tool_schema_chars=800,
    )

    assert result.text == "brief"
    assert captured == {
        "gs": gs,
        "opts": options.briefing,
        "budget": briefing_budget(
            8192,
            options,
            playbook_chars=400,
            tool_schema_chars=800,
        ),
    }


@pytest.mark.asyncio
async def test_maybe_build_briefing_returns_empty_when_disabled(monkeypatch):
    async def fail_build_briefing(*args, **kwargs):
        raise AssertionError("disabled briefing must not be built")

    monkeypatch.setattr(
        "civ_mcp.arena.prompt_context.build_briefing",
        fail_build_briefing,
    )

    result = await maybe_build_briefing(
        object(),
        CivOptions(),
        n_ctx=8192,
        playbook_chars=0,
        tool_schema_chars=0,
    )

    assert result == Briefing()
```

- [ ] **Step 2: Run tests and verify import failure**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_prompt_context.py -q
```

Expected: FAIL because `civ_mcp.arena.prompt_context` does not exist.

- [ ] **Step 3: Create the shared helper**

Create `src/civ_mcp/arena/prompt_context.py`:

```python
from __future__ import annotations

from typing import Any

from civ_mcp.arena.briefing import Briefing, build_briefing
from civ_mcp.arena.budget import briefing_budget


async def maybe_build_briefing(
    gs: Any,
    options: Any,
    *,
    n_ctx: int,
    playbook_chars: int,
    tool_schema_chars: int,
    supplied: Briefing | None = None,
) -> Briefing:
    if supplied is not None:
        return supplied
    if not options.briefing.enabled:
        return Briefing()
    budget = briefing_budget(
        n_ctx,
        options,
        playbook_chars,
        tool_schema_chars,
    )
    return await build_briefing(gs, options.briefing, budget)
```

- [ ] **Step 4: Use helper in `LLMPolicy.__call__()`**

In `src/civ_mcp/arena/agent.py`, add:

```python
from civ_mcp.arena.prompt_context import maybe_build_briefing
```

Then narrow the two existing import lines so only the still-used names remain (`resolve_n_ctx` and `Briefing` are still referenced; `briefing_budget` and `build_briefing` are not). Replace:

```python
from civ_mcp.arena.briefing import Briefing, build_briefing
from civ_mcp.arena.budget import briefing_budget, resolve_n_ctx
```

with:

```python
from civ_mcp.arena.briefing import Briefing
from civ_mcp.arena.budget import resolve_n_ctx
```

Replace the entire briefing build block. The current block is:

```python
        briefing_was_supplied = briefing is not None
        briefing = briefing or Briefing()
        if self.options.briefing.enabled and not briefing_was_supplied:
            if _should_resolve_n_ctx(
                self._n_ctx,
                self._n_ctx_source,
                self.options.context_budget,
                self._n_ctx_resolves,
            ):
                self._n_ctx, self._n_ctx_source = await resolve_n_ctx(
                    getattr(self.backend, "base_url", ""),
                    getattr(self.backend, "model", ""),
                    self.options.context_budget,
                )
                self._n_ctx_resolves += 1
            playbook_chars = len(self._system) - len(SYSTEM)
            tool_schema_chars = len(json.dumps(self._tools))
            budget = briefing_budget(
                self._n_ctx,
                self.options,
                playbook_chars,
                tool_schema_chars,
            )
            briefing = await build_briefing(gs, self.options.briefing, budget)
```

Replace it with:

```python
        briefing_was_supplied = briefing is not None
        if (
            self.options.briefing.enabled
            and not briefing_was_supplied
            and _should_resolve_n_ctx(
                self._n_ctx,
                self._n_ctx_source,
                self.options.context_budget,
                self._n_ctx_resolves,
            )
        ):
            self._n_ctx, self._n_ctx_source = await resolve_n_ctx(
                getattr(self.backend, "base_url", ""),
                getattr(self.backend, "model", ""),
                self.options.context_budget,
            )
            self._n_ctx_resolves += 1
        playbook_chars = len(self._system) - len(SYSTEM)
        tool_schema_chars = len(json.dumps(self._tools))
        briefing = await maybe_build_briefing(
            gs,
            self.options,
            n_ctx=self._n_ctx,
            playbook_chars=playbook_chars,
            tool_schema_chars=tool_schema_chars,
            supplied=briefing,
        )
```

Two things this preserves that the naive rewrite breaks: the `briefing = briefing or Briefing()` line is **dropped** (so `supplied=briefing` stays `None` when nothing was supplied and `maybe_build_briefing` actually builds), and `_should_resolve_n_ctx` keeps its real name, full four-argument signature, and the `not briefing_was_supplied` guard (so a supplied briefing never triggers a wasted `resolve_n_ctx` network call). `maybe_build_briefing` returns the supplied briefing unchanged when one was passed, `Briefing()` when disabled, and a freshly built briefing otherwise — so `briefing` is always a real `Briefing` for the `briefing.text` access below.

- [ ] **Step 5: Use helper in `CLIAgentPolicy.__call__()`**

In `src/civ_mcp/arena/cli_agent.py`, add:

```python
from civ_mcp.arena.prompt_context import maybe_build_briefing
```

Drop `build_briefing` from `from civ_mcp.arena.briefing import Briefing, build_briefing` (keep `Briefing` — it is still used for the annotation) and drop `briefing_budget` from `from civ_mcp.arena.budget import DEFAULT_N_CTX, briefing_budget` (keep `DEFAULT_N_CTX` — it is still passed as `n_ctx` below).

Replace:

```python
        briefing_was_supplied = briefing is not None
        briefing = briefing or Briefing()
        if self.options.briefing.enabled and not briefing_was_supplied:
            playbook_chars = len(self._system_prefix)
            budget = briefing_budget(DEFAULT_N_CTX, self.options, playbook_chars, 0)
            briefing = await build_briefing(gs, self.options.briefing, budget)
```

with:

```python
        playbook_chars = len(self._system_prefix)
        briefing = await maybe_build_briefing(
            gs,
            self.options,
            n_ctx=DEFAULT_N_CTX,
            playbook_chars=playbook_chars,
            tool_schema_chars=0,
            supplied=briefing,
        )
```

- [ ] **Step 6: Use helper in coordinator exclusive briefing prebuild**

In `src/civ_mcp/arena/coordinator.py`, add:

```python
from civ_mcp.arena.prompt_context import maybe_build_briefing
```

Remove the now-unused `from civ_mcp.arena.briefing import build_briefing` line, and drop `briefing_budget` from `from civ_mcp.arena.budget import DEFAULT_N_CTX, briefing_budget` (keep `DEFAULT_N_CTX` — it is still passed as `n_ctx` below).

Replace:

```python
                    budget = briefing_budget(DEFAULT_N_CTX, opts, playbook_chars, 0)
                    policy_kwargs["briefing"] = await build_briefing(
                        gs, opts.briefing, budget
                    )
```

with:

```python
                    policy_kwargs["briefing"] = await maybe_build_briefing(
                        gs,
                        opts,
                        n_ctx=DEFAULT_N_CTX,
                        playbook_chars=playbook_chars,
                        tool_schema_chars=0,
                    )
```

- [ ] **Step 7: Run briefing/policy/coordinator tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_prompt_context.py tests/arena/test_agent.py tests/arena/test_cli_agent.py tests/arena/test_coordinator.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/civ_mcp/arena/prompt_context.py src/civ_mcp/arena/agent.py src/civ_mcp/arena/cli_agent.py src/civ_mcp/arena/coordinator.py tests/arena/test_prompt_context.py
git commit -m "refactor(arena): share briefing build context"
```

---

### Task 7: Full Verification

**Files:**
- No source changes.

- [ ] **Step 1: Run the focused Slice 3 suite**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/test_json_io.py tests/arena/test_config.py tests/arena/test_memory.py tests/arena/test_task_tracker.py tests/arena/test_prompting.py tests/arena/test_cli_agent.py tests/arena/test_coordinator.py tests/arena/test_agent.py tests/arena/test_prompt_context.py -q
```

Expected: PASS.

- [ ] **Step 2: Run the full arena suite**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena -q
```

Expected: PASS.

- [ ] **Step 3: Run the full repo suite**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests -q
```

Expected: PASS.

- [ ] **Step 4: Check whitespace**

Run:

```bash
git diff --check
```

Expected: no output.

- [ ] **Step 5: Inspect final branch state**

Run:

```bash
git status --short --branch
git log --oneline -12
```

Expected: branch is `arena-standing-memory-task-tracker-slice3`; tracked files are clean except intentional untracked plan docs or `.serena/`.

---

## Self-Review

- Spec coverage: all ten findings are covered. Tasks 1-3 cover the highlighted blocker regressions. Task 4 covers stale-memory TTL. Task 5 covers CLI cap and task capture scaling. Task 6 covers briefing build duplication. Task 3 also covers serial helper I/O, exact threat coordinates, and the no-op sorted-prefix cleanup.
- Placeholder scan: no banned placeholder tokens or unspecified edge handling remains.
- Type consistency: new `MemoryOptions.max_age_turns`, `CivOptions.standing_plan_summary_chars`, and `maybe_build_briefing()` signatures are used consistently across tests and call sites.
- Regression guards verified against current source: Task 4 updates the existing `test_civ_options_fingerprint_contains_memory_and_task_tracker()` assertion (test_experiment.py) alongside the new fingerprint key, so adding `max_age_turns` does not silently fail that suite. Task 6 preserves `_should_resolve_n_ctx`'s real name and four-argument signature, drops `briefing = briefing or Briefing()` so `maybe_build_briefing(supplied=...)` still builds for the in-process policy, and trims only the unused names from shared import lines (`resolve_n_ctx`, `DEFAULT_N_CTX`, and `Briefing` stay).
