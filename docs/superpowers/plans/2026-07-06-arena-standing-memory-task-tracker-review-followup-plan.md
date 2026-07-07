# Arena Standing Memory Task Tracker Review Follow-Up Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve the valid post-review regressions in Slice 3 while preserving the already-fixed city-state/major-civ threat behavior.

**Architecture:** Keep the fixes local to arena standing-memory/task-tracker code. Restore fail-closed civilian movement when owner/threat context is incomplete, keep transient builder-invalid retries bounded, remove dead task-tracker state, preserve configured standing-plan summary budgets, make plan extraction less brittle around reserved bullet labels, and avoid needless briefing serialization work.

**Tech Stack:** Python 3, pytest, asyncio, Civ 6 arena test fakes, existing `civ_mcp.arena` modules.

---

## Review Assessment

Accepted findings to fix:

- `src/civ_mcp/arena/task_tracker.py`: civilian safety should fail closed when diplomacy or threat-scan context is incomplete and an unknown visible unit is near a civilian path.
- `src/civ_mcp/arena/config.py`: `standing_plan_summary_chars` must not cap below `memory.max_chars`.
- `src/civ_mcp/arena/task_tracker.py`: repeated invalid builder improvements need a bounded retry path so permanently invalid tasks do not stay active forever.
- `src/civ_mcp/arena/memory.py`: a reserved-label plan subheading such as `- Planning:` should not discard following `TASK` or `CANCEL` lines.
- `src/civ_mcp/arena/agent.py`: `json.dumps(self._tools)` and playbook sizing should run only when a briefing will actually be built.
- `src/civ_mcp/arena/task_tracker.py`: hostile-context lookup should not be fetched when all executable tasks resolve without movement.
- `src/civ_mcp/arena/task_tracker.py`: `peaceful_prefixes` should be used again for fail-closed unknown-label decisions, not left dead.
- `src/civ_mcp/arena/task_tracker.py`: replace duplicated safe loader wrappers with the codebase's `asyncio.gather(..., return_exceptions=True)` style.

Adjusted finding:

- The current `get_units()` then `_hostile_owner_context()` sequencing is real, but literal parallelization conflicts with skipping hostile-context network calls for at-target tasks. This plan implements lazy, one-shot hostile-context loading after unit positions prove movement is needed. Do not add speculative parallel context fetches that recreate the unconditional two-network-call path.

## File Map

- `src/civ_mcp/arena/task_tracker.py`: task execution, civilian hostile detection, builder retry policy.
- `tests/arena/test_task_tracker.py`: regression tests for fail-closed movement, bounded builder retry, and skipped hostile-context calls.
- `src/civ_mcp/arena/config.py`: standing-plan capture and CLI final-summary character budget properties.
- `tests/arena/test_config.py`: budget expectations for memory-only and memory-plus-task-tracker configs.
- `src/civ_mcp/arena/memory.py`: standing-plan block extraction and section-header detection.
- `tests/arena/test_memory.py`: parser regression tests for reserved bullet labels and existing reflection-header termination.
- `src/civ_mcp/arena/agent.py`: in-process LLM briefing setup and tool-schema sizing.
- `tests/arena/test_agent.py`: regression tests proving tool schema serialization is skipped when briefing is disabled or supplied.

## Task 1: Restore Fail-Closed Civilian Safety And Bound Builder Retries

**Files:**
- Modify: `src/civ_mcp/arena/task_tracker.py`
- Test: `tests/arena/test_task_tracker.py`

- [ ] **Step 1: Add failing task-tracker regression tests**

In `tests/arena/test_task_tracker.py`, extend `FakeGS` with call counters for diplomacy and threat scan:

```python
class FakeGS:
    """Minimal async fake mirroring the GameState methods the tracker calls."""

    def __init__(
        self,
        units,
        map_tiles=None,
        found_city_result="FOUNDED|18,24",
        move_unit_result="MOVING_TO|18,24",
        improve_tile_result="IMPROVED",
        diplomacy=None,
        units_calls=0,
        threat_scan=None,
        diplomacy_error=None,
        threat_scan_error=None,
    ):
        self.units = units
        self.map_tiles = map_tiles or {}
        self.found_city_result = found_city_result
        self.move_unit_result = move_unit_result
        self.improve_tile_result = improve_tile_result
        self.diplomacy = diplomacy if diplomacy is not None else []
        self.units_calls = units_calls
        self.diplomacy_calls = 0
        self.threat_scan = threat_scan if threat_scan is not None else []
        self.threat_scan_calls = 0
        self.diplomacy_error = diplomacy_error
        self.threat_scan_error = threat_scan_error
        self.found_city_calls = []
        self.move_unit_calls = []
        self.improve_tile_calls = []
        self.map_area_calls = []

    async def get_units(self):
        self.units_calls += 1
        return self.units

    async def get_diplomacy(self):
        self.diplomacy_calls += 1
        if self.diplomacy_error is not None:
            raise self.diplomacy_error
        return self.diplomacy

    async def get_threat_scan(self):
        self.threat_scan_calls += 1
        if self.threat_scan_error is not None:
            raise self.threat_scan_error
        return self.threat_scan
```

Replace `test_diplomacy_failure_does_not_block_unconfirmed_foreign_unit` with:

```python
@pytest.mark.asyncio
async def test_diplomacy_failure_blocks_unknown_unit_label():
    unit = _unit(unit_id=65537, unit_index=1, x=1, y=1)
    gs = FakeGS(
        units=[unit],
        map_tiles={(18, 24): [_tile(18, 24, units=["Unidentified WARRIOR"])]},
        diplomacy_error=RuntimeError("diplomacy unavailable"),
    )
    task = _task(task_id="settle:65537", unit_id=65537, target_x=18, target_y=24)

    updated, results = await run_pre_model_tasks(gs, [task])

    assert gs.move_unit_calls == []
    assert updated[0].status == "active"
    assert updated[0].last_result == "blocked_visible_hostile"
    assert results[0]["action"] == "block"
    assert results[0]["result"] == "blocked_visible_hostile"
```

Replace `test_threat_scan_failure_does_not_block_unknown_city_state_unit` with two tests:

```python
@pytest.mark.asyncio
async def test_threat_scan_failure_blocks_unknown_city_state_unit():
    unit = _unit(unit_id=65537, unit_index=1, x=1, y=1)
    gs = FakeGS(
        units=[unit],
        map_tiles={(18, 24): [_tile(18, 24, units=["Vatican City WARRIOR"])]},
        diplomacy=[SimpleNamespace(civ_name="Rome", is_at_war=False)],
        threat_scan_error=RuntimeError("threat scan unavailable"),
    )
    task = _task(task_id="settle:65537", unit_id=65537, target_x=18, target_y=24)

    updated, results = await run_pre_model_tasks(gs, [task])

    assert gs.move_unit_calls == []
    assert updated[0].last_result == "blocked_visible_hostile"
    assert results[0]["action"] == "block"


@pytest.mark.asyncio
async def test_threat_scan_failure_keeps_known_peaceful_major_unit_unblocked():
    unit = _unit(unit_id=65537, unit_index=1, x=1, y=1)
    gs = FakeGS(
        units=[unit],
        map_tiles={(18, 24): [_tile(18, 24, units=["Rome WARRIOR"])]},
        diplomacy=[SimpleNamespace(civ_name="Rome", is_at_war=False)],
        threat_scan_error=RuntimeError("threat scan unavailable"),
    )
    task = _task(task_id="settle:65537", unit_id=65537, target_x=18, target_y=24)

    updated, results = await run_pre_model_tasks(gs, [task])

    assert gs.move_unit_calls == [(1, 18, 24)]
    assert updated[0].status == "active"
    assert results[0]["action"] == "move"
```

Add a bounded retry test after `test_builder_improve_retries_after_transient_invalid_improvement_becomes_valid`:

```python
@pytest.mark.asyncio
async def test_builder_improve_fails_after_repeated_invalid_improvement():
    unit = _unit(unit_id=65538, unit_index=2, x=12, y=19, valid_improvements=[])
    gs = FakeGS(units=[unit])
    task = _task(
        task_id="builder_improve:65538",
        kind="builder_improve",
        unit_id=65538,
        target_x=12,
        target_y=19,
        improvement="IMPROVEMENT_FARM",
        last_result="blocked_improvement_not_valid",
    )

    updated, results = await run_pre_model_tasks(gs, [task])

    assert gs.improve_tile_calls == []
    assert updated[0].status == "failed"
    assert updated[0].last_result == "blocked_improvement_not_valid_retry_limit"
    assert results[0]["status"] == "failed"
    assert results[0]["action"] == "block"
    assert results[0]["result"] == "blocked_improvement_not_valid_retry_limit"
```

Add an at-target no-hostile-context test near the no-executable task tests:

```python
@pytest.mark.asyncio
async def test_run_pre_model_tasks_skips_hostile_context_when_task_is_at_target():
    unit = _unit(unit_id=65537, unit_index=1, x=18, y=24)
    gs = FakeGS(units=[unit])
    task = _task(task_id="settle:65537", unit_id=65537, target_x=18, target_y=24)

    updated, results = await run_pre_model_tasks(gs, [task])

    assert updated[0].status == "complete"
    assert results[0]["action"] == "found_city"
    assert gs.diplomacy_calls == 0
    assert gs.threat_scan_calls == 0
```

- [ ] **Step 2: Run the new task-tracker tests and verify they fail**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest \
  tests/arena/test_task_tracker.py::test_diplomacy_failure_blocks_unknown_unit_label \
  tests/arena/test_task_tracker.py::test_threat_scan_failure_blocks_unknown_city_state_unit \
  tests/arena/test_task_tracker.py::test_threat_scan_failure_keeps_known_peaceful_major_unit_unblocked \
  tests/arena/test_task_tracker.py::test_builder_improve_fails_after_repeated_invalid_improvement \
  tests/arena/test_task_tracker.py::test_run_pre_model_tasks_skips_hostile_context_when_task_is_at_target -q
```

Expected: FAIL before implementation. The unknown-unit and repeated-invalid tests should fail on behavior; the at-target test should show diplomacy/threat calls were made.

- [ ] **Step 3: Implement fail-closed context loading, lazy hostile context, and bounded builder retry**

In `src/civ_mcp/arena/task_tracker.py`, change `_HostileOwnerContext` to carry `block_unknown` again while retaining exact threat coordinates:

```python
@dataclass(frozen=True)
class _HostileOwnerContext:
    hostile_prefixes: tuple[str, ...]
    peaceful_prefixes: tuple[str, ...]
    hostile_coords: frozenset[tuple[int, int]]
    block_unknown: bool
```

Remove `_load_diplomacy_safely()` and `_load_threat_scan_safely()`. Add `_sorted_prefixes()` and rewrite `_hostile_owner_context()`:

```python
def _sorted_prefixes(names: set[str]) -> tuple[str, ...]:
    return tuple(sorted(names, key=len, reverse=True))


async def _hostile_owner_context(gs: Any) -> _HostileOwnerContext:
    hostile = {"Barbarian"}
    peaceful: set[str] = set()
    hostile_coords: set[tuple[int, int]] = set()
    block_unknown = False

    civ_result, threat_result = await asyncio.gather(
        gs.get_diplomacy(),
        gs.get_threat_scan(),
        return_exceptions=True,
    )

    civs: tuple[Any, ...]
    if isinstance(civ_result, Exception):
        civs = ()
        block_unknown = True
    else:
        civs = tuple(civ_result)

    threats: tuple[Any, ...]
    if isinstance(threat_result, Exception):
        threats = ()
        block_unknown = True
    else:
        threats = tuple(threat_result)

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
        if name and getattr(threat, "is_city_state", False):
            hostile.add(name)

    return _HostileOwnerContext(
        hostile_prefixes=_sorted_prefixes(hostile),
        peaceful_prefixes=_sorted_prefixes(peaceful),
        hostile_coords=frozenset(hostile_coords),
        block_unknown=block_unknown,
    )
```

Update `_tile_has_hostile_unit()`:

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
        if owner_context.block_unknown and not any(
            _label_matches_owner(label_text, owner)
            for owner in owner_context.peaceful_prefixes
        ):
            return True
    return False
```

Add helpers before `run_pre_model_tasks()`:

```python
def _empty_hostile_owner_context() -> _HostileOwnerContext:
    return _HostileOwnerContext(
        hostile_prefixes=("Barbarian",),
        peaceful_prefixes=(),
        hostile_coords=frozenset(),
        block_unknown=False,
    )


def _task_needs_hostile_context(task: UnitTask, unit: Any | None) -> bool:
    if unit is None or unit.moves_remaining <= 0:
        return False
    return (unit.x, unit.y) != (task.target_x, task.target_y)
```

In the invalid builder-improve branch inside `_run_single_task()`, replace the current always-active block with:

```python
        if task.last_result == "blocked_improvement_not_valid":
            new_task = replace(
                task,
                status="failed",
                last_result="blocked_improvement_not_valid_retry_limit",
            )
            return new_task, _result_dict(
                task,
                status="failed",
                action="block",
                result="blocked_improvement_not_valid_retry_limit",
            )

        new_task = replace(task, last_result="blocked_improvement_not_valid")
        return new_task, _result_dict(
            task,
            status="active",
            action="block",
            result="blocked_improvement_not_valid",
        )
```

In `run_pre_model_tasks()`, replace unconditional owner-context loading with lazy loading after `units_by_id` exists:

```python
    units_by_id = {unit.unit_id: unit for unit in units}
    if any(
        _task_needs_hostile_context(task, units_by_id.get(task.unit_id))
        for task in executable
    ):
        owner_context = await _hostile_owner_context(gs)
    else:
        owner_context = _empty_hostile_owner_context()
```

- [ ] **Step 4: Run the task-tracker focused tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_task_tracker.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 1**

```bash
git add src/civ_mcp/arena/task_tracker.py tests/arena/test_task_tracker.py
git commit -m "fix(arena): restore fail-closed task safety"
```

## Task 2: Preserve Configured CLI Standing-Plan Summary Budgets

**Files:**
- Modify: `src/civ_mcp/arena/config.py`
- Test: `tests/arena/test_config.py`

- [ ] **Step 1: Update failing config expectations**

In `tests/arena/test_config.py`, replace `test_civ_options_standing_plan_summary_chars_is_bounded_for_cli` with:

```python
def test_civ_options_standing_plan_summary_chars_matches_enabled_capture_budget():
    assert CivOptions().standing_plan_summary_chars == 500
    assert CivOptions(
        memory=MemoryOptions(enabled=True, max_chars=900),
    ).standing_plan_summary_chars == 1200
    assert CivOptions(
        memory=MemoryOptions(enabled=True, max_chars=6000),
    ).standing_plan_summary_chars == 6000
    assert CivOptions(task_tracker=TaskTrackerOptions(enabled=True)).standing_plan_summary_chars == 4000
    assert CivOptions(
        task_tracker=TaskTrackerOptions(enabled=True, max_tasks=12),
    ).standing_plan_summary_chars == 4480
    assert CivOptions(
        memory=MemoryOptions(enabled=True, max_chars=6000),
        task_tracker=TaskTrackerOptions(enabled=True, max_tasks=12),
    ).standing_plan_summary_chars == 6000
```

- [ ] **Step 2: Run the config test and verify it fails**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_config.py::test_civ_options_standing_plan_summary_chars_matches_enabled_capture_budget -q
```

Expected: FAIL before implementation because memory `max_chars=6000` is currently clamped to `4000` or `4480`.

- [ ] **Step 3: Simplify `standing_plan_summary_chars`**

In `src/civ_mcp/arena/config.py`, replace the property body with:

```python
    @property
    def standing_plan_summary_chars(self) -> int:
        if not self.standing_plan_enabled:
            return 500
        return max(1200, self.standing_plan_capture_chars)
```

- [ ] **Step 4: Run config tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_config.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 2**

```bash
git add src/civ_mcp/arena/config.py tests/arena/test_config.py
git commit -m "fix(arena): preserve standing plan summary budgets"
```

## Task 3: Keep Reserved Plan Subheadings When They Carry Task Lines

**Files:**
- Modify: `src/civ_mcp/arena/memory.py`
- Test: `tests/arena/test_memory.py`

- [ ] **Step 1: Add a failing parser regression test**

In `tests/arena/test_memory.py`, add this test after the existing bulleted reflection-header tests:

```python
def test_extract_standing_plan_keeps_reserved_bullet_heading_when_block_contains_task():
    summary = (
        "STANDING PLAN:\n"
        "- Planning:\n"
        "- TASK settle unit_id=123 target=18,24\n"
        "- Keep escort near the target.\n"
        "TACTICAL:\n"
        "- unrelated next section\n"
    )

    result = extract_standing_plan(summary, max_chars=1200)

    assert result == (
        "Planning:\n"
        "TASK settle unit_id=123 target=18,24\n"
        "Keep escort near the target."
    )
```

- [ ] **Step 2: Run the parser test and verify it fails**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_memory.py::test_extract_standing_plan_keeps_reserved_bullet_heading_when_block_contains_task -q
```

Expected: FAIL before implementation because `- Planning:` is treated as a terminator.

- [ ] **Step 3: Add task-aware bulleted section detection**

In `src/civ_mcp/arena/memory.py`, add the import and regex:

```python
from collections.abc import Sequence
```

```python
_TASK_OR_CANCEL_LINE_RE = re.compile(r"^\s*[-*•]*\s*(?:TASK\s+|CANCEL\s+)", re.IGNORECASE)
```

Change the collection loop in `extract_standing_plan()` to pass following lines:

```python
    following = lines[start_idx + 1 :]
    for offset, line in enumerate(following):
        if _is_section_header(line, following[offset + 1 :]):
            break
        if line.strip() == "":
            # Blank lines are not a terminator per the brief; skip them so a
            # plan split across a blank gap keeps all its content.
            continue
        collected.append(_strip_bullet(line.strip()))
```

Replace `_is_section_header()` with:

```python
def _has_task_line_before_next_header(lines: Sequence[str]) -> bool:
    for line in lines:
        if line.strip() == "":
            continue
        if _TASK_OR_CANCEL_LINE_RE.match(line):
            return True
        if _is_section_header(line):
            return False
    return False


def _is_section_header(line: str, following_lines: Sequence[str] = ()) -> bool:
    stripped = line.strip()
    if not stripped.endswith(":"):
        return False

    bullet = _BULLET_PREFIX_RE.match(stripped)
    candidate = _strip_bullet(stripped) if bullet else stripped
    body = candidate[:-1].strip()
    if not body:
        return False

    if bullet:
        return (
            body.upper() in _BULLETED_SECTION_HEADERS
            and not _has_task_line_before_next_header(following_lines)
        )
    return body.isupper()
```

- [ ] **Step 4: Run memory tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_memory.py -q
```

Expected: PASS, including the existing tests that still stop at `- TACTICAL:`, `- Tactical:`, and `- tactical:` when no task line follows inside that block.

- [ ] **Step 5: Commit Task 3**

```bash
git add src/civ_mcp/arena/memory.py tests/arena/test_memory.py
git commit -m "fix(arena): preserve task lines under reserved plan headings"
```

## Task 4: Avoid Unused In-Process Briefing Serialization

**Files:**
- Modify: `src/civ_mcp/arena/agent.py`
- Test: `tests/arena/test_agent.py`

- [ ] **Step 1: Add failing serialization regression tests**

In `tests/arena/test_agent.py`, add these tests after `test_briefing_disabled_is_todays_message`:

```python
@pytest.mark.asyncio
async def test_policy_skips_tool_schema_serialization_when_briefing_disabled(monkeypatch):
    def fail_dumps(value):
        raise AssertionError("tool schema should not be serialized when briefing is disabled")

    monkeypatch.setattr(agent.json, "dumps", fail_dumps)
    be = SpyBackend([_no_tool_reply()])
    pol = LLMPolicy(be, FakeCost(), options=CivOptions(briefing=BriefingOptions(enabled=False)))

    await pol(None, 3, 7)

    user_msg = [m for m in be.calls[0]["messages"] if m["role"] == "user"][0]
    assert user_msg["content"] == "It is turn 7. You control player 3. Begin."


@pytest.mark.asyncio
async def test_policy_skips_tool_schema_serialization_when_briefing_supplied(monkeypatch):
    from civ_mcp.arena.briefing import Briefing

    def fail_dumps(value):
        raise AssertionError("tool schema should not be serialized for supplied briefing")

    monkeypatch.setattr(agent.json, "dumps", fail_dumps)
    be = SpyBackend([_no_tool_reply()])
    pol = LLMPolicy(
        be,
        FakeCost(),
        options=CivOptions(briefing=BriefingOptions(enabled=True)),
    )

    out = await pol(
        None,
        3,
        7,
        briefing=Briefing(text="PREBUILT", tokens=1, sections=["overview"]),
    )

    assert out["transcript"]["briefing_tokens"] == 1
    assert out["transcript"]["briefing_sections"] == ["overview"]
```

- [ ] **Step 2: Run the agent tests and verify they fail**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest \
  tests/arena/test_agent.py::test_policy_skips_tool_schema_serialization_when_briefing_disabled \
  tests/arena/test_agent.py::test_policy_skips_tool_schema_serialization_when_briefing_supplied -q
```

Expected: FAIL before implementation because `agent.json.dumps(self._tools)` runs before `maybe_build_briefing()`.

- [ ] **Step 3: Guard briefing size computation**

In `src/civ_mcp/arena/agent.py`, replace the current unconditional sizing block:

```python
        playbook_chars = len(self._system) - len(SYSTEM)
        tool_schema_chars = len(json.dumps(self._tools))
        n_ctx = self._n_ctx if self._n_ctx is not None else DEFAULT_N_CTX
        briefing = await maybe_build_briefing(
```

with:

```python
        playbook_chars = 0
        tool_schema_chars = 0
        if self.options.briefing.enabled and not briefing_was_supplied:
            playbook_chars = len(self._system) - len(SYSTEM)
            tool_schema_chars = len(json.dumps(self._tools))
        n_ctx = self._n_ctx if self._n_ctx is not None else DEFAULT_N_CTX
        briefing = await maybe_build_briefing(
```

- [ ] **Step 4: Run agent tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_agent.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 4**

```bash
git add src/civ_mcp/arena/agent.py tests/arena/test_agent.py
git commit -m "fix(arena): skip unused briefing serialization"
```

## Task 5: Final Verification And Review Prep

**Files:**
- No code changes expected.
- Verify: task tracker, memory, config, CLI, coordinator, agent, prompt-context, full arena tests.

- [ ] **Step 1: Run the focused Slice 3 suite**

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
  tests/arena/test_agent.py \
  tests/arena/test_prompt_context.py -q
```

Expected: PASS.

- [ ] **Step 2: Run full arena tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena -q
```

Expected: PASS.

- [ ] **Step 3: Run full repository tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests -q
```

Expected: PASS.

- [ ] **Step 4: Check whitespace and branch status**

Run:

```bash
git diff --check
git status --short --branch
```

Expected: `git diff --check` prints nothing. Status shows only intentional commits plus pre-existing untracked `.serena/` and plan docs unless the implementation added a new plan file.

- [ ] **Step 5: Request code review**

Use `superpowers:requesting-code-review` against the full branch diff from `404e1a7` through the new HEAD. Ask the reviewer to focus on:

- fail-closed civilian movement when diplomacy/threat context is partial,
- no global blocking of peaceful major civ labels,
- bounded builder-improve retry semantics,
- CLI standing-plan summary budget parity with memory capture budget,
- reserved bullet heading extraction with task lines,
- no needless briefing serialization when disabled or supplied.
