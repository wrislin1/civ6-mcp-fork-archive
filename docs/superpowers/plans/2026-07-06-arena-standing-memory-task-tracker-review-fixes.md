# Arena Standing Memory Task Tracker Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the 10 reviewed correctness, efficiency, and reuse issues in the arena standing-memory/task-tracker slice before the 8-civ live behavior run.

**Architecture:** Keep the Slice 3 feature boundary intact: no new judgment automation, no raw Lua arena tools, and no cross-run memory. Centralize standing-plan gating in `CivOptions`, keep CLI briefing reads on the coordinator's connected tuner, make the task tracker war-aware without blocking on peaceful units, and extract shared JSON file helpers for the duplicated atomic load/write pattern.

**Tech Stack:** Python 3.12, `pytest`, `uv`, existing `civ_mcp.arena` coordinator/policy stack, existing `GameState` methods and narrator models.

---

## Verification Context

This plan applies to the existing worktree:

```bash
cd /home/riz/.config/superpowers/worktrees/civ6-mcp/arena-standing-memory-task-tracker-slice3
git status --short --branch
```

Expected:

```text
## arena-standing-memory-task-tracker-slice3
```

The reviewed code is at branch tip `c30b00d`. The main checkout at `/home/riz/dev/civ6-mcp` does not contain `src/civ_mcp/arena/memory.py` or `src/civ_mcp/arena/task_tracker.py`, so do not execute this plan there.

## File Structure

- Create: `src/civ_mcp/json_io.py`
- Create: `tests/test_json_io.py`
- Modify: `src/civ_mcp/arena/config.py`
- Modify: `src/civ_mcp/arena/agent.py`
- Modify: `src/civ_mcp/arena/cli_agent.py`
- Modify: `src/civ_mcp/arena/coordinator.py`
- Modify: `src/civ_mcp/arena/memory.py`
- Modify: `src/civ_mcp/arena/task_tracker.py`
- Modify: `src/civ_mcp/heartbeat.py`
- Modify: `tests/arena/test_config.py`
- Modify: `tests/arena/test_cli_agent.py`
- Modify: `tests/arena/test_coordinator.py`
- Modify: `tests/arena/test_memory.py`
- Modify: `tests/arena/test_prompting.py`
- Modify: `tests/arena/test_task_tracker.py`

---

### Task 1: Centralize Standing-Plan Options

**Files:**
- Modify: `src/civ_mcp/arena/config.py`
- Modify: `tests/arena/test_config.py`

- [ ] **Step 1: Write config tests for the shared predicate and capture budget**

Append these tests to `tests/arena/test_config.py`:

```python
def test_civ_options_standing_plan_enabled_property():
    assert CivOptions().standing_plan_enabled is False
    assert CivOptions(memory=MemoryOptions(enabled=True)).standing_plan_enabled is True
    assert CivOptions(task_tracker=TaskTrackerOptions(enabled=True)).standing_plan_enabled is True
    assert CivOptions(
        memory=MemoryOptions(enabled=True),
        task_tracker=TaskTrackerOptions(enabled=True),
    ).standing_plan_enabled is True


def test_civ_options_standing_plan_capture_chars():
    assert CivOptions().standing_plan_capture_chars == 0
    assert CivOptions(memory=MemoryOptions(enabled=True, max_chars=900)).standing_plan_capture_chars == 900
    assert CivOptions(task_tracker=TaskTrackerOptions(enabled=True)).standing_plan_capture_chars == 4000
    assert CivOptions(
        memory=MemoryOptions(enabled=True, max_chars=1200),
        task_tracker=TaskTrackerOptions(enabled=True),
    ).standing_plan_capture_chars == 4000
    assert CivOptions(
        memory=MemoryOptions(enabled=True, max_chars=6000),
        task_tracker=TaskTrackerOptions(enabled=True),
    ).standing_plan_capture_chars == 6000
```

- [ ] **Step 2: Run config tests to verify they fail**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_config.py::test_civ_options_standing_plan_enabled_property tests/arena/test_config.py::test_civ_options_standing_plan_capture_chars -q
```

Expected: both tests fail because `CivOptions.standing_plan_enabled` and `CivOptions.standing_plan_capture_chars` do not exist.

- [ ] **Step 3: Add the shared properties**

In `src/civ_mcp/arena/config.py`, add this module constant below `VALID_PLAYBOOKS`:

```python
STANDING_PLAN_CAPTURE_CHARS = 4000
```

Inside `CivOptions`, after `fingerprint()`, add:

```python
    @property
    def standing_plan_enabled(self) -> bool:
        return self.memory.enabled or self.task_tracker.enabled

    @property
    def standing_plan_capture_chars(self) -> int:
        if not self.standing_plan_enabled:
            return 0
        capture_chars = self.memory.max_chars if self.memory.enabled else 0
        if self.task_tracker.enabled:
            capture_chars = max(capture_chars, STANDING_PLAN_CAPTURE_CHARS)
        return capture_chars
```

- [ ] **Step 4: Run config tests to verify they pass**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_config.py::test_civ_options_standing_plan_enabled_property tests/arena/test_config.py::test_civ_options_standing_plan_capture_chars -q
```

Expected: both tests pass.

- [ ] **Step 5: Commit Task 1**

Run:

```bash
git add src/civ_mcp/arena/config.py tests/arena/test_config.py
git commit -m "fix(arena): centralize standing-plan option gates"
```

Expected: commit succeeds.

---

### Task 2: Extract Shared JSON File Helpers

**Files:**
- Create: `src/civ_mcp/json_io.py`
- Create: `tests/test_json_io.py`
- Modify: `src/civ_mcp/arena/memory.py`
- Modify: `src/civ_mcp/arena/task_tracker.py`
- Modify: `src/civ_mcp/heartbeat.py`

- [ ] **Step 1: Write tests for shared JSON helpers**

Create `tests/test_json_io.py`:

```python
import json

from civ_mcp.json_io import read_json_file, write_json_file_atomic


def test_read_json_file_missing_returns_none(tmp_path):
    assert read_json_file(tmp_path / "missing.json") is None


def test_read_json_file_malformed_returns_none(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    assert read_json_file(path) is None


def test_write_json_file_atomic_creates_parent_and_round_trips(tmp_path):
    path = tmp_path / "nested" / "data.json"

    write_json_file_atomic(path, {"a": 1, "b": [2, 3]})

    assert json.loads(path.read_text()) == {"a": 1, "b": [2, 3]}
    assert read_json_file(path) == {"a": 1, "b": [2, 3]}
    assert not path.with_name(path.name + ".tmp").exists()
```

- [ ] **Step 2: Run helper tests to verify they fail**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/test_json_io.py -q
```

Expected: import failure for `civ_mcp.json_io`.

- [ ] **Step 3: Add shared helper module**

Create `src/civ_mcp/json_io.py`:

```python
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_json_file(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        return None


def write_json_file_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps(payload))
    tmp_path.replace(path)
```

- [ ] **Step 4: Refactor memory load/write to use helper**

In `src/civ_mcp/arena/memory.py`, add:

```python
from civ_mcp.json_io import read_json_file, write_json_file_atomic
```

Replace the body of `load_memory()` with:

```python
    data = read_json_file(memory_path(transcript_dir, run_id, player_id))
    if not isinstance(data, dict):
        return None
    try:
        return StandingMemory(
            schema_version=data["schema_version"],
            run_id=data["run_id"],
            player_id=data["player_id"],
            updated_turn=data["updated_turn"],
            text=data["text"],
        )
    except (KeyError, TypeError):
        return None
```

In `save_memory()`, replace:

```python
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": memory.schema_version,
        "run_id": memory.run_id,
        "player_id": memory.player_id,
        "updated_turn": memory.updated_turn,
        "text": memory.text,
    }
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps(payload))
    tmp_path.replace(path)
```

with:

```python
    payload = {
        "schema_version": memory.schema_version,
        "run_id": memory.run_id,
        "player_id": memory.player_id,
        "updated_turn": memory.updated_turn,
        "text": memory.text,
    }
    write_json_file_atomic(path, payload)
```

Remove the now-unused `import json`.

- [ ] **Step 5: Refactor task tracker load/write to use helper**

In `src/civ_mcp/arena/task_tracker.py`, add:

```python
from civ_mcp.json_io import read_json_file, write_json_file_atomic
```

Replace the body of `load_task_state()` with:

```python
    data = read_json_file(task_path(transcript_dir, run_id, player_id))
    if not isinstance(data, dict):
        return _empty_state(run_id, player_id)
    try:
        tasks = tuple(_task_from_dict(t) for t in data["tasks"])
        return TaskState(
            schema_version=data["schema_version"],
            run_id=data["run_id"],
            player_id=data["player_id"],
            tasks=tasks,
        )
    except (ValueError, KeyError, TypeError):
        return _empty_state(run_id, player_id)
```

In `save_task_state()`, replace:

```python
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": state.schema_version,
        "run_id": state.run_id,
        "player_id": state.player_id,
        "tasks": [_task_to_dict(t) for t in state.tasks],
    }
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps(payload))
    tmp_path.replace(path)
```

with:

```python
    payload = {
        "schema_version": state.schema_version,
        "run_id": state.run_id,
        "player_id": state.player_id,
        "tasks": [_task_to_dict(t) for t in state.tasks],
    }
    write_json_file_atomic(path, payload)
```

Remove the now-unused `import json`.

- [ ] **Step 6: Refactor heartbeat atomic write to use helper**

In `src/civ_mcp/heartbeat.py`, add:

```python
from civ_mcp.json_io import write_json_file_atomic
```

In `write()`, replace:

```python
        HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
```

and:

```python
        tmp = HEARTBEAT_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(HEARTBEAT_PATH)
```

with:

```python
        write_json_file_atomic(HEARTBEAT_PATH, data)
```

Remove the now-unused `import json`.

- [ ] **Step 7: Run helper and persistence tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/test_json_io.py tests/arena/test_memory.py tests/arena/test_task_tracker.py -q
```

Expected: all tests pass.

- [ ] **Step 8: Commit Task 2**

Run:

```bash
git add src/civ_mcp/json_io.py src/civ_mcp/arena/memory.py src/civ_mcp/arena/task_tracker.py src/civ_mcp/heartbeat.py tests/test_json_io.py
git commit -m "refactor: share atomic json file helpers"
```

Expected: commit succeeds.

---

### Task 3: Fix Standing Memory Extraction And Aging

**Files:**
- Modify: `src/civ_mcp/arena/memory.py`
- Modify: `src/civ_mcp/arena/coordinator.py`
- Modify: `tests/arena/test_memory.py`
- Modify: `tests/arena/test_coordinator.py`

- [ ] **Step 1: Add extraction and age-label tests**

Append these tests to `tests/arena/test_memory.py`:

```python
def test_extract_standing_plan_keeps_all_caps_bullet_ending_colon():
    summary = (
        "STANDING PLAN:\n"
        "- BUILD CAMPUS:\n"
        "- TASK builder_improve unit_id=456 target=12,19 improvement=IMPROVEMENT_MINE\n"
        "TACTICAL:\n"
        "- unrelated next section\n"
    )

    result = extract_standing_plan(summary, max_chars=1200)

    assert result == (
        "BUILD CAMPUS:\n"
        "TASK builder_improve unit_id=456 target=12,19 improvement=IMPROVEMENT_MINE"
    )


def test_format_memory_block_surfaces_turn_age():
    memory = StandingMemory(
        schema_version=1,
        run_id="run1",
        player_id=0,
        updated_turn=5,
        text="Keep marching.",
    )

    result = format_memory_block(memory, current_turn=8)

    assert result == "== STANDING PLAN (captured turn 5, 3 turns old) ==\nKeep marching."


def test_format_memory_block_surfaces_one_turn_old():
    memory = StandingMemory(
        schema_version=1,
        run_id="run1",
        player_id=0,
        updated_turn=7,
        text="Keep marching.",
    )

    result = format_memory_block(memory, current_turn=8)

    assert result == "== STANDING PLAN (captured turn 7, 1 turn old) ==\nKeep marching."
```

Update the existing `test_format_memory_block_exact_heading()` assertion to:

```python
    assert result == "== STANDING PLAN (captured turn 5) ==\nKeep marching."
```

- [ ] **Step 2: Run memory tests to verify they fail**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_memory.py::test_extract_standing_plan_keeps_all_caps_bullet_ending_colon tests/arena/test_memory.py::test_format_memory_block_surfaces_turn_age tests/arena/test_memory.py::test_format_memory_block_surfaces_one_turn_old tests/arena/test_memory.py::test_format_memory_block_exact_heading -q
```

Expected: failures from early section termination and old `"FROM LAST TURN"` heading.

- [ ] **Step 3: Fix section-header detection**

In `src/civ_mcp/arena/memory.py`, replace `_is_section_header()` with:

```python
def _is_section_header(line: str) -> bool:
    stripped = line.strip()
    if _BULLET_PREFIX_RE.match(stripped):
        return False
    if not stripped.endswith(":"):
        return False
    body = stripped[:-1].strip()
    return bool(body) and body.isupper()
```

- [ ] **Step 4: Change memory block formatting to surface age**

In `src/civ_mcp/arena/memory.py`, replace `format_memory_block()` with:

```python
def format_memory_block(
    memory: StandingMemory | None, *, current_turn: int | None = None
) -> str:
    """Render standing memory as a prompt-ready block, or "" if empty/absent."""
    if memory is None or not memory.text:
        return ""
    suffix = f"captured turn {memory.updated_turn}"
    if current_turn is not None:
        age = max(0, current_turn - memory.updated_turn)
        if age == 1:
            suffix += ", 1 turn old"
        elif age != 0:
            suffix += f", {age} turns old"
    return f"== STANDING PLAN ({suffix}) ==\n{memory.text}"
```

- [ ] **Step 5: Pass current turn from the coordinator**

In `src/civ_mcp/arena/coordinator.py`, replace:

```python
                memory_block = format_memory_block(memory)
```

with:

```python
                memory_block = format_memory_block(memory, current_turn=st.turn)
```

Update `tests/arena/test_coordinator.py` by replacing assertions that expect `"== STANDING PLAN FROM LAST TURN =="` with:

```python
    assert pol2.calls[0]["memory_block"].startswith("== STANDING PLAN (captured turn 2")
```

and:

```python
    assert pol.calls[0]["memory_block"].startswith("== STANDING PLAN (captured turn 1, 1 turn old) ==")
```

- [ ] **Step 6: Run memory and coordinator tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_memory.py tests/arena/test_coordinator.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit Task 3**

Run:

```bash
git add src/civ_mcp/arena/memory.py src/civ_mcp/arena/coordinator.py tests/arena/test_memory.py tests/arena/test_coordinator.py
git commit -m "fix(arena): surface standing memory age"
```

Expected: commit succeeds.

---

### Task 4: Fix Task Tracker Correctness And Per-Turn I/O

**Files:**
- Modify: `src/civ_mcp/arena/task_tracker.py`
- Modify: `src/civ_mcp/arena/coordinator.py`
- Modify: `tests/arena/test_task_tracker.py`

- [ ] **Step 1: Add task tracker regression tests**

In `tests/arena/test_task_tracker.py`, add this import near the top:

```python
from types import SimpleNamespace
```

Extend `FakeGS.__init__()` with:

```python
        diplomacy=None,
        units_calls=0,
```

Inside `FakeGS.__init__()`, add:

```python
        self.diplomacy = diplomacy if diplomacy is not None else []
        self.units_calls = units_calls
```

Replace `FakeGS.get_units()` with:

```python
    async def get_units(self):
        self.units_calls += 1
        return self.units
```

Add this method to `FakeGS`:

```python
    async def get_diplomacy(self):
        return self.diplomacy
```

Append these tests:

```python
@pytest.mark.asyncio
async def test_peaceful_foreign_unit_does_not_block_settler_movement():
    unit = _unit(unit_id=65537, unit_index=1, x=1, y=1)
    gs = FakeGS(
        units=[unit],
        map_tiles={(18, 24): [_tile(18, 24, units=["Rome WARRIOR"])]},
        diplomacy=[SimpleNamespace(civ_name="Rome", is_at_war=False)],
    )
    task = _task(task_id="settle:65537", unit_id=65537, target_x=18, target_y=24)

    updated, results = await run_pre_model_tasks(gs, [task])

    assert gs.move_unit_calls == [(1, 18, 24)]
    assert updated[0].status == "active"
    assert results[0]["action"] == "move"


@pytest.mark.asyncio
async def test_at_war_foreign_unit_blocks_settler_movement():
    unit = _unit(unit_id=65537, unit_index=1, x=1, y=1)
    gs = FakeGS(
        units=[unit],
        map_tiles={(18, 24): [_tile(18, 24, units=["Rome WARRIOR"])]},
        diplomacy=[SimpleNamespace(civ_name="Rome", is_at_war=True)],
    )
    task = _task(task_id="settle:65537", unit_id=65537, target_x=18, target_y=24)

    updated, results = await run_pre_model_tasks(gs, [task])

    assert gs.move_unit_calls == []
    assert updated[0].last_result == "blocked_visible_hostile"
    assert results[0]["action"] == "block"


@pytest.mark.asyncio
async def test_run_pre_model_tasks_skips_units_query_when_no_executable_tasks():
    gs = FakeGS(units=[_unit(unit_id=1, unit_index=1, x=1, y=1)])

    updated, results = await run_pre_model_tasks(gs, [])

    assert updated == ()
    assert results == []
    assert gs.units_calls == 0


@pytest.mark.asyncio
async def test_builder_improve_invalid_at_target_marks_task_failed():
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
    assert updated[0].status == "failed"
    assert updated[0].last_result == "blocked_improvement_not_valid"
    assert results[0]["status"] == "failed"
    assert results[0]["action"] == "block"


def test_format_task_block_honors_configured_max_tasks():
    tasks = tuple(_task(task_id=f"settle:{i}", unit_id=i) for i in range(12))
    results = [
        {
            "task_id": f"settle:{i}",
            "kind": "settle",
            "unit_id": i,
            "target": [18, 24],
            "status": "active",
            "action": "move",
            "result": "MOVING_TO|18,24",
        }
        for i in range(12)
    ]

    block = format_task_block(tasks, results, max_tasks=12)

    assert block.count("settle:") == 24
```

Update the existing `test_builder_improve_blocks_when_improvement_not_valid()` expected status from `"active"` to `"failed"` and result status from `"active"` to `"failed"`.

- [ ] **Step 2: Run task tracker tests to verify they fail**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_task_tracker.py::test_peaceful_foreign_unit_does_not_block_settler_movement tests/arena/test_task_tracker.py::test_at_war_foreign_unit_blocks_settler_movement tests/arena/test_task_tracker.py::test_run_pre_model_tasks_skips_units_query_when_no_executable_tasks tests/arena/test_task_tracker.py::test_builder_improve_invalid_at_target_marks_task_failed tests/arena/test_task_tracker.py::test_format_task_block_honors_configured_max_tasks -q
```

Expected: failures from peaceful unit blocking, unconditional `get_units()`, invalid improvement remaining active, and `format_task_block()` missing `max_tasks`.

- [ ] **Step 3: Add war-aware hostile owner helpers**

In `src/civ_mcp/arena/task_tracker.py`, add:

```python
import asyncio
```

Add these helpers above `_visible_hostile_nearby()`:

```python
async def _hostile_owner_prefixes(gs: Any) -> tuple[str, ...]:
    prefixes = {"Barbarian"}
    try:
        civs = await gs.get_diplomacy()
    except Exception:
        return tuple(sorted(prefixes))
    for civ in civs:
        if getattr(civ, "is_at_war", False):
            name = str(getattr(civ, "civ_name", "") or "").strip()
            if name:
                prefixes.add(name)
    return tuple(sorted(prefixes, key=len, reverse=True))


def _label_matches_owner(label: str, owner: str) -> bool:
    return label == owner or label.startswith(owner + " ")


def _tile_has_hostile_unit(tile: Any, hostile_owners: Sequence[str]) -> bool:
    for label in tile.units or []:
        label_text = str(label)
        if any(_label_matches_owner(label_text, owner) for owner in hostile_owners):
            return True
    return False
```

Replace `_visible_hostile_nearby()` with:

```python
async def _visible_hostile_nearby(
    gs: Any,
    cur_x: int,
    cur_y: int,
    target_x: int,
    target_y: int,
    hostile_owners: Sequence[str],
) -> bool:
    current_tiles, target_tiles = await asyncio.gather(
        gs.get_map_area(cur_x, cur_y, 2),
        gs.get_map_area(target_x, target_y, 2),
    )
    return any(_tile_has_hostile_unit(tile, hostile_owners) for tile in current_tiles) or any(
        _tile_has_hostile_unit(tile, hostile_owners) for tile in target_tiles
    )
```

- [ ] **Step 4: Pass hostile owners through task execution**

Change `_run_single_task()` signature to:

```python
async def _run_single_task(
    gs: Any,
    task: UnitTask,
    units_by_id: dict[int, Any],
    hostile_owners: Sequence[str],
) -> tuple[UnitTask, dict[str, Any]]:
```

Replace both calls to `_visible_hostile_nearby(...)` with:

```python
        if await _visible_hostile_nearby(
            gs, unit.x, unit.y, task.target_x, task.target_y, hostile_owners
        ):
```

In `run_pre_model_tasks()`, replace the beginning through `units_by_id` with:

```python
    executable = [
        task for task in tasks if task.status == "active" and task.kind in TASK_KINDS
    ]
    if not executable:
        return tuple(tasks), []

    try:
        units = await gs.get_units()
    except Exception:  # pragma: no cover - defensive, mirrors per-task guard
        units = []
    units_by_id = {unit.unit_id: unit for unit in units}
    hostile_owners = await _hostile_owner_prefixes(gs)
```

Inside the per-task loop, replace:

```python
            new_task, result = await _run_single_task(gs, task, units_by_id)
```

with:

```python
            new_task, result = await _run_single_task(
                gs, task, units_by_id, hostile_owners
            )
```

- [ ] **Step 5: Mark invalid builder improvement as failed**

In `src/civ_mcp/arena/task_tracker.py`, replace:

```python
        new_task = replace(task, last_result="blocked_improvement_not_valid")
        return new_task, _result_dict(
            task,
            status="active",
            action="block",
            result="blocked_improvement_not_valid",
        )
```

with:

```python
        new_task = replace(
            task, status="failed", last_result="blocked_improvement_not_valid"
        )
        return new_task, _result_dict(
            task,
            status="failed",
            action="block",
            result="blocked_improvement_not_valid",
        )
```

- [ ] **Step 6: Let task block formatting honor max_tasks**

Replace `format_task_block()` signature and first two lines with:

```python
def format_task_block(
    tasks: Sequence[UnitTask],
    results: Sequence[dict[str, Any]],
    *,
    max_tasks: int = 8,
) -> str:
    """Render active tasks and this turn's results as a prompt-ready block.

    Returns "" when there are no active tasks and no results.
    """
    active = [task for task in tasks if task.status == "active"][:max_tasks]
    limited_results = list(results)[:max_tasks]
```

In `src/civ_mcp/arena/coordinator.py`, replace:

```python
                    task_block = format_task_block(updated_tasks, task_results)
```

with:

```python
                    task_block = format_task_block(
                        updated_tasks,
                        task_results,
                        max_tasks=opts.task_tracker.max_tasks,
                    )
```

- [ ] **Step 7: Run task tracker tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_task_tracker.py -q
```

Expected: all task tracker tests pass.

- [ ] **Step 8: Commit Task 4**

Run:

```bash
git add src/civ_mcp/arena/task_tracker.py src/civ_mcp/arena/coordinator.py tests/arena/test_task_tracker.py
git commit -m "fix(arena): make deterministic tasks war-aware and bounded"
```

Expected: commit succeeds.

---

### Task 5: Build CLI Briefing Before Exclusive Disconnect

**Files:**
- Modify: `src/civ_mcp/arena/agent.py`
- Modify: `src/civ_mcp/arena/cli_agent.py`
- Modify: `src/civ_mcp/arena/coordinator.py`
- Modify: `tests/arena/test_cli_agent.py`
- Modify: `tests/arena/test_coordinator.py`

- [ ] **Step 1: Add CLI prebuilt briefing test**

Append this test to `tests/arena/test_cli_agent.py`:

```python
def test_call_uses_prebuilt_briefing_without_building_from_gs(monkeypatch):
    from civ_mcp.arena import cli_agent as cli_mod
    from civ_mcp.arena.briefing import Briefing
    from civ_mcp.arena.config import BriefingOptions

    captured = {}

    async def forbidden_build(gs, opts, budget):
        raise AssertionError("CLI policy must not build briefing when one is supplied")

    class FakeProc:
        pid = 1
        returncode = 0

        async def communicate(self):
            return (b'{"type":"result","result":"ok","usage":{},"total_cost_usd":0}', b"")

        async def wait(self):
            pass

    async def fake_create(*args, **kwargs):
        captured["argv"] = args
        return FakeProc()

    monkeypatch.setattr(cli_mod, "build_briefing", forbidden_build)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    pol = CLIAgentPolicy(
        "cli-claude",
        FakeCost(),
        project_dir="/x",
        timeout_s=5,
        options=CivOptions(briefing=BriefingOptions(enabled=True)),
    )

    result = asyncio.run(
        pol(
            None,
            player_id=3,
            turn=9,
            briefing=Briefing(text="PREBUILT BRIEFING", tokens=4, sections=["overview"]),
        )
    )

    assert result["summary"] == "ok"
    assert "PREBUILT BRIEFING" in " ".join(captured["argv"])
    assert result["transcript"]["briefing_tokens"] == 4
    assert result["transcript"]["briefing_sections"] == ["overview"]
```

- [ ] **Step 2: Add coordinator connected-tuner briefing test**

Append this test to `tests/arena/test_coordinator.py`:

```python
@pytest.mark.asyncio
async def test_exclusive_cli_briefing_built_before_disconnect(monkeypatch):
    from civ_mcp.arena.briefing import Briefing
    from civ_mcp.arena.config import BriefingOptions
    import civ_mcp.arena.coordinator as coord_mod

    built_connected = []

    async def fake_build_briefing(gs, opts, budget):
        built_connected.append(conn.is_connected)
        return Briefing(text="PREBUILT BRIEFING", tokens=4, sections=["overview"])

    class ExclusiveBriefingPolicy(RecordingPolicy):
        needs_exclusive_tuner = True

        async def __call__(self, gs, player_id, turn, **kwargs):
            assert conn.is_connected is False
            assert kwargs["briefing"].text == "PREBUILT BRIEFING"
            return await super().__call__(gs, player_id, turn, **kwargs)

    monkeypatch.setattr(coord_mod, "build_briefing", fake_build_briefing)
    conn = FakeConn()
    gs = FakeGS()
    opts = CivOptions(briefing=BriefingOptions(enabled=True))
    cfg = ArenaConfig(
        players=[PlayerSpec(7, "cli-claude", "")],
        max_puppet_turns=1,
        puppet_ids=[7],
    )
    conn._polls = iter([[ "LOCAL|7", "TURN|2", "ACTIVE|true", "LAST|1" ]])
    pol = ExclusiveBriefingPolicy({"summary": "cli ran"}, options=opts, needs_exclusive_tuner=True)

    result = await run_arena(conn, gs, cfg, policy=pol)

    assert result["puppet_turns_played"] == 1
    assert built_connected == [True]
```

- [ ] **Step 3: Run new briefing tests to verify they fail**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_cli_agent.py::test_call_uses_prebuilt_briefing_without_building_from_gs tests/arena/test_coordinator.py::test_exclusive_cli_briefing_built_before_disconnect -q
```

Expected: failures because neither policy accepts `briefing=` and the coordinator does not prebuild it.

- [ ] **Step 4: Allow local policy to accept a prebuilt briefing**

In `src/civ_mcp/arena/agent.py`, change `LLMPolicy.__call__()` signature to include:

```python
        briefing: Briefing | None = None,
```

Replace:

```python
        briefing = Briefing()
        if self.options.briefing.enabled:
```

with:

```python
        briefing = briefing or Briefing()
        if self.options.briefing.enabled and not briefing.text:
```

Replace:

```python
        include_standing_plan_instruction = (
            self.options.memory.enabled or self.options.task_tracker.enabled
        )
```

with:

```python
        include_standing_plan_instruction = self.options.standing_plan_enabled
```

- [ ] **Step 5: Allow CLI policy to accept a prebuilt briefing and use shared predicate**

In `src/civ_mcp/arena/cli_agent.py`, change `CLIAgentPolicy.__call__()` signature to include:

```python
        briefing: Briefing | None = None,
```

Replace:

```python
        include_standing_plan_instruction = (
            self.options.memory.enabled or self.options.task_tracker.enabled
        )
        briefing = Briefing()
        if self.options.briefing.enabled:
```

with:

```python
        include_standing_plan_instruction = self.options.standing_plan_enabled
        briefing = briefing or Briefing()
        if self.options.briefing.enabled and not briefing.text:
```

Replace:

```python
        if include_standing_plan_instruction:
            max_summary_chars = min(4000, max(1200, self.options.memory.max_chars))
```

with:

```python
        if include_standing_plan_instruction:
            max_summary_chars = max(1200, self.options.standing_plan_capture_chars)
```

In the success transcript payload, add these keys:

```python
            "briefing_tokens": briefing.tokens,
            "briefing_sections": briefing.sections,
            "briefing_radius": briefing.radius,
            "briefing_errors": briefing.errors,
```

In the timeout transcript payload, add:

```python
                "briefing_tokens": briefing.tokens,
                "briefing_sections": briefing.sections,
                "briefing_radius": briefing.radius,
                "briefing_errors": briefing.errors,
```

- [ ] **Step 6: Prebuild exclusive briefing in the coordinator**

In `src/civ_mcp/arena/coordinator.py`, add imports:

```python
from civ_mcp.arena.briefing import Briefing, build_briefing
from civ_mcp.arena.budget import DEFAULT_N_CTX, briefing_budget
from civ_mcp.arena.agent import load_playbook
```

Before the exclusive disconnect block, after task-block construction, add:

```python
                briefing = Briefing()
                if exclusive and opts.briefing.enabled:
                    playbook_chars = (
                        len(load_playbook()) if opts.playbook == "condensed" else 0
                    )
                    budget = briefing_budget(DEFAULT_N_CTX, opts, playbook_chars, 0)
                    briefing = await build_briefing(gs, opts.briefing, budget)
```

Replace the policy call with:

```python
                result = await pol(
                    gs,
                    st.local,
                    st.turn,
                    memory_block=memory_block,
                    task_block=task_block,
                    briefing=briefing,
                )
```

`ScriptedPolicy` and `RecordingPolicy` already accept `**kwargs`; `LLMPolicy` and `CLIAgentPolicy` now accept `briefing=`.

- [ ] **Step 7: Run briefing tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_cli_agent.py::test_call_uses_prebuilt_briefing_without_building_from_gs tests/arena/test_coordinator.py::test_exclusive_cli_briefing_built_before_disconnect tests/arena/test_agent.py tests/arena/test_prompting.py -q
```

Expected: all selected tests pass.

- [ ] **Step 8: Commit Task 5**

Run:

```bash
git add src/civ_mcp/arena/agent.py src/civ_mcp/arena/cli_agent.py src/civ_mcp/arena/coordinator.py tests/arena/test_cli_agent.py tests/arena/test_coordinator.py
git commit -m "fix(arena): prebuild cli briefing before tuner release"
```

Expected: commit succeeds.

---

### Task 6: Wire Standing-Plan Capture Budget Everywhere

**Files:**
- Modify: `src/civ_mcp/arena/agent.py`
- Modify: `src/civ_mcp/arena/cli_agent.py`
- Modify: `src/civ_mcp/arena/coordinator.py`
- Modify: `tests/arena/test_coordinator.py`
- Modify: `tests/arena/test_prompting.py`
- Modify: `tests/arena/test_cli_agent.py`

- [ ] **Step 1: Add task-tracker-only long TASK-line capture test**

Append this test to `tests/arena/test_coordinator.py`:

```python
@pytest.mark.asyncio
async def test_task_tracker_only_uses_task_capture_budget_not_memory_default(tmp_path):
    opts = CivOptions(task_tracker=TaskTrackerOptions(enabled=True, max_tasks=8))
    run_id, player_id = "task-capture-budget", 8
    long_plan = (
        "STANDING PLAN:\n"
        + ("- filler line to push task below memory default\n" * 80)
        + "TASK settle unit_id=42 target=10,12\n"
    )
    cfg = ArenaConfig(
        players=[PlayerSpec(player_id, "local", "m")],
        max_puppet_turns=1,
        dry_run=True,
        puppet_ids=[player_id],
        run_id=run_id,
        transcript_dir=str(tmp_path),
    )
    pol = RecordingPolicy(
        {"summary": "ignored", "transcript": {"final_summary": long_plan}},
        options=opts,
    )
    conn = FakeConn()
    conn._polls = iter([[f"LOCAL|{player_id}", "TURN|2", "ACTIVE|true", "LAST|1"]])

    await run_arena(conn, FakeGS(), cfg, policy=pol)

    path = task_path(str(tmp_path), run_id, player_id)
    assert '"unit_id": 42' in path.read_text()
```

- [ ] **Step 2: Run the capture-budget test to verify it fails**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_coordinator.py::test_task_tracker_only_uses_task_capture_budget_not_memory_default -q
```

Expected: failure because `extract_standing_plan()` is still capped by `opts.memory.max_chars`.

- [ ] **Step 3: Use centralized predicate in local and CLI policies**

In `src/civ_mcp/arena/agent.py`, confirm the standing-plan line is:

```python
        include_standing_plan_instruction = self.options.standing_plan_enabled
```

In `src/civ_mcp/arena/cli_agent.py`, confirm the standing-plan line is:

```python
        include_standing_plan_instruction = self.options.standing_plan_enabled
```

In tests, replace direct expectations built from `memory.enabled or task_tracker.enabled` with `options.standing_plan_enabled` only if tests construct that predicate locally.

- [ ] **Step 4: Use centralized capture budget in coordinator**

In `src/civ_mcp/arena/coordinator.py`, replace:

```python
                if opts.memory.enabled or opts.task_tracker.enabled:
```

with:

```python
                if opts.standing_plan_enabled:
```

Replace:

```python
                    captured_plan = extract_standing_plan(final_summary, opts.memory.max_chars)
```

with:

```python
                    captured_plan = extract_standing_plan(
                        final_summary,
                        opts.standing_plan_capture_chars,
                    )
```

- [ ] **Step 5: Use centralized capture budget in CLI summary parsing**

In `src/civ_mcp/arena/cli_agent.py`, confirm this block exists from Task 5:

```python
        if include_standing_plan_instruction:
            max_summary_chars = max(1200, self.options.standing_plan_capture_chars)
```

If the old memory-only expression remains, replace it with the block above.

- [ ] **Step 6: Run standing-plan wiring tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_coordinator.py::test_task_tracker_only_uses_task_capture_budget_not_memory_default tests/arena/test_prompting.py tests/arena/test_cli_agent.py::test_call_codex_standing_plan_survives_clamp_when_task_tracker_enabled -q
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit Task 6**

Run:

```bash
git add src/civ_mcp/arena/agent.py src/civ_mcp/arena/cli_agent.py src/civ_mcp/arena/coordinator.py tests/arena/test_coordinator.py tests/arena/test_prompting.py tests/arena/test_cli_agent.py
git commit -m "fix(arena): use task-aware standing-plan capture budget"
```

Expected: commit succeeds.

---

### Task 7: Full Regression Verification

**Files:**
- No source changes unless verification exposes a failure.

- [ ] **Step 1: Run focused Slice 3 tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest \
  tests/test_json_io.py \
  tests/arena/test_config.py \
  tests/arena/test_memory.py \
  tests/arena/test_task_tracker.py \
  tests/arena/test_prompting.py \
  tests/arena/test_cli_agent.py \
  tests/arena/test_coordinator.py \
  -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Run full arena tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena -q
```

Expected: all arena tests pass.

- [ ] **Step 3: Run full repository tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests -q
```

Expected: all tests pass.

- [ ] **Step 4: Run whitespace check**

Run:

```bash
git diff --check
```

Expected: no output.

- [ ] **Step 5: Inspect final diff**

Run:

```bash
git diff --stat HEAD~6..HEAD
git diff -- src/civ_mcp/arena/task_tracker.py src/civ_mcp/arena/memory.py src/civ_mcp/arena/coordinator.py src/civ_mcp/arena/cli_agent.py src/civ_mcp/arena/config.py
```

Expected:
- `task_tracker.py` no longer blocks on peaceful foreign units.
- `run_pre_model_tasks()` returns before `get_units()` when no executable tasks exist.
- invalid `builder_improve` tasks become non-active.
- `format_task_block()` receives `max_tasks`.
- CLI briefing is built before the exclusive disconnect.
- standing-plan capture uses `standing_plan_capture_chars`.
- memory heading includes captured turn and age.

- [ ] **Step 6: Commit any verification-only fixes**

If Step 1 through Step 5 required edits, commit them:

```bash
git add src tests
git commit -m "test(arena): cover standing memory review fixes"
```

Expected: commit only if edits were made during verification.

---

## Self-Review

Spec coverage:
- Finding 1 is covered by Task 4 peaceful and at-war unit tests plus war-aware hostile owner filtering.
- Finding 2 is covered by Task 5 prebuilt CLI briefing tests and coordinator wiring before disconnect.
- Finding 3 is covered by Task 1 capture budget properties and Task 6 task-tracker-only long plan test.
- Finding 4 is covered by Task 3 all-caps markdown bullet test and `_is_section_header()` fix.
- Finding 5 is covered by Task 4 invalid builder improvement failure semantics.
- Finding 6 is covered by Task 4 `format_task_block(..., max_tasks=...)` test and coordinator call.
- Finding 7 is covered by Task 3 memory block age labels.
- Finding 8 is covered by Task 4 early return before `get_units()` and parallel map-area reads.
- Finding 9 is covered by Task 2 shared JSON helper and refactors in memory, task tracker, and heartbeat.
- Finding 10 is covered by Task 1 `CivOptions.standing_plan_enabled` and Tasks 5-6 policy/coordinator usage.

Placeholder scan:
- The plan contains concrete code edits and named checks for every step.
- Each code-edit step includes concrete code.

Type consistency:
- `CivOptions.standing_plan_enabled` and `CivOptions.standing_plan_capture_chars` are defined once in Task 1 and used by later tasks.
- `format_task_block(..., max_tasks=...)` is defined in Task 4 and called with the same keyword from the coordinator.
- `briefing: Briefing | None = None` is added to both local and CLI policies before the coordinator passes it.
