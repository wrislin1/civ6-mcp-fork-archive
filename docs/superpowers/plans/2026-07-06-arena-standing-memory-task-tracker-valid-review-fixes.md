# Arena Standing Memory Task Tracker Valid Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the validated review issues that still undermine standing-memory continuity, deterministic task follow-through, and behavior reporting parity for Slice 3.

**Architecture:** Keep fixes local to the existing arena modules. Preserve the current public concepts (`STANDING PLAN`, `TASK`, `CANCEL unit_id=...`, `CivOptions`) while making parsing, execution, capture, and metrics semantics consistent across in-process and CLI puppets. Each task adds a focused failing regression first, implements the smallest code change, and commits independently.

**Tech Stack:** Python 3, pytest, asyncio, existing `civ_mcp.arena` modules and arena test fakes.

---

## Review Assessment

All 10 findings in the supplied review are valid against current HEAD `c19f85638564530922687c7fe1220b93e4fa0f56`.

Accepted fixes:

- Preserve trailing `STANDING PLAN:` blocks in CLI final summaries even when long preamble text exceeds the configured summary budget.
- Normalize common markdown/emphasis heading forms for the `STANDING PLAN:` marker, and match known reflection headers case-insensitively for both bulleted (`- Tactical:`) and unbulleted (`Tactical:`) forms. Arbitrary all-caps plan bullets such as `- BUILD CAMPUS:` remain plan content by design (see the retained `test_extract_standing_plan_keeps_all_caps_bullet_ending_colon`); only whitelisted reflection headers terminate the block. This narrows finding #2b: an unlisted bulleted header like `- NEXT STEPS:` is intentionally kept rather than treated as a terminator, since nothing distinguishes it from a legitimate imperative bullet.
- Refresh deterministic task `updated_turn` when pre-model execution touches an active task, so long-running tasks are not evicted first.
- Add bounded retry/failure semantics for permanently failing settle tasks.
- Treat builder-improve engine no-response results as unconfirmed, not complete.
- Resolve task tracker unit references by composite `unit_id` and visible `unit_index` aliases.
- Isolate memory/task tracker load/save/capture failures so filesystem errors do not abort the arena loop.
- Count `task_tracker_turns` only when task tracker had meaningful state/results.
- Count CLI `unit_action(action="trade_route")` and `unit_action(action="teleport")` as trade-route behavior calls.
- Make explicit CLI `context_budget` values affect `CLIAgentPolicy` briefing budgets instead of being accepted as a no-op.

Rejected or deferred:

- No supplied finding is rejected.
- The review's cleanup notes are intentionally out of scope for this plan.

## File Map

- `src/civ_mcp/arena/cli_agent.py`: CLI summary parsing, CLI briefing budget construction.
- `tests/arena/test_cli_agent.py`: CLI parser/call regressions, CLI context budget regression.
- `src/civ_mcp/arena/memory.py`: standing-plan marker and section-header parsing.
- `tests/arena/test_memory.py`: parser regressions for markdown headings and generic bulleted all-caps terminators.
- `src/civ_mcp/arena/task_tracker.py`: task merge/execution semantics, unit lookup aliases, bounded retries.
- `tests/arena/test_task_tracker.py`: deterministic task regressions.
- `src/civ_mcp/arena/coordinator.py`: guarded memory/task tracker capture path.
- `tests/arena/test_coordinator.py`: arena-loop resilience to memory/task filesystem failures.
- `src/civ_mcp/arena/analyze.py`: behavior metric counting for task tracker and CLI trade-route calls.
- `tests/arena/test_analyze.py`: behavior metric regressions.
- `src/civ_mcp/arena/experiment.py`: no production change expected unless Task 7 decides to add config validation tests only.

---

## Task 1: Preserve CLI Standing Plans At The Tail Of Long Summaries

**Files:**
- Modify: `src/civ_mcp/arena/cli_agent.py`
- Test: `tests/arena/test_cli_agent.py`

- [ ] **Step 1: Add failing CLI parser/call regressions**

In `tests/arena/test_cli_agent.py`, add these tests near the existing `STANDING PLAN block must survive the summary clamp` tests:

```python
def test_parse_claude_preserves_trailing_standing_plan_past_memory_budget():
    text = (
        "A" * 1500
        + "\n\nSTANDING PLAN:\n"
        + "- keep settler moving east\n"
        + "TASK settle unit_id=42 target=10,12\n"
    )
    blob = json.dumps({
        "type": "result",
        "subtype": "success",
        "result": text,
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "total_cost_usd": 0.0,
    })

    summary, pt, ct, usd = CLIAgentPolicy._parse_claude(blob, max_summary_chars=1200)

    assert len(summary) <= 1200
    assert summary.startswith("STANDING PLAN:")
    assert "TASK settle unit_id=42 target=10,12" in summary
    assert pt == 10
    assert ct == 5
    assert usd == 0.0


def test_parse_codex_preserves_trailing_standing_plan_past_task_budget():
    text = (
        "A" * 4300
        + "\n\nSTANDING PLAN:\n"
        + "- improve the copper first\n"
        + "TASK builder_improve unit_id=65538 target=12,19 improvement=IMPROVEMENT_MINE\n"
    )
    stdout = "\n".join([
        json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": text},
        }),
        json.dumps({
            "type": "turn.completed",
            "usage": {"input_tokens": 11, "output_tokens": 7},
        }),
    ])

    summary, pt, ct, usd = CLIAgentPolicy._parse_codex(stdout, max_summary_chars=4000)

    assert len(summary) <= 4000
    assert summary.startswith("STANDING PLAN:")
    assert "TASK builder_improve unit_id=65538" in summary
    assert pt == 11
    assert ct == 7
    assert usd == 0.0
```

- [ ] **Step 2: Run the new CLI parser tests and verify they fail**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest \
  tests/arena/test_cli_agent.py::test_parse_claude_preserves_trailing_standing_plan_past_memory_budget \
  tests/arena/test_cli_agent.py::test_parse_codex_preserves_trailing_standing_plan_past_task_budget -q
```

Expected: FAIL. Both summaries will start with `"A"` and omit the trailing `STANDING PLAN:` block because the current parsers use `text[:max_summary_chars]`.

- [ ] **Step 3: Add a shared CLI final-summary clamp helper**

In `src/civ_mcp/arena/cli_agent.py`, add these imports and helpers near `_PROMPT_SUMMARY_TAIL`:

```python
import re
```

If `re` is already imported after this task is started, do not duplicate it.

```python
_CLI_STANDING_PLAN_RE = re.compile(
    r"^\s*(?:[-*]+\s*)?(?:#{1,6}\s*)?(?:[*_]{1,3})?\s*standing plan\s*:\s*(?:[*_]{1,3})?",
    re.IGNORECASE,
)


def _find_standing_plan_start(text: str) -> int:
    offset = 0
    for line in text.splitlines(keepends=True):
        if _CLI_STANDING_PLAN_RE.match(line):
            return offset
        offset += len(line)
    return -1


def _clamp_final_summary(text: str, max_summary_chars: int) -> str:
    if len(text) <= max_summary_chars:
        return text
    plan_start = _find_standing_plan_start(text)
    if plan_start >= 0:
        return text[plan_start : plan_start + max_summary_chars].strip()
    return text[:max_summary_chars]
```

Replace the two head slices in `_parse_claude()` and `_parse_codex()`:

```python
return (_clamp_final_summary(str(obj.get("result") or ""), max_summary_chars),
        int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0),
        float(obj.get("total_cost_usd") or 0.0))
```

```python
summary = _clamp_final_summary(str(item.get("text") or ""), max_summary_chars)
```

- [ ] **Step 4: Run CLI parser tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest \
  tests/arena/test_cli_agent.py::test_parse_claude_preserves_trailing_standing_plan_past_memory_budget \
  tests/arena/test_cli_agent.py::test_parse_codex_preserves_trailing_standing_plan_past_task_budget \
  tests/arena/test_cli_agent.py::test_call_claude_summary_still_clamped_when_memory_disabled \
  tests/arena/test_cli_agent.py::test_call_claude_summary_caps_large_memory_capture_at_configured_budget -q
```

Expected: PASS.

- [ ] **Step 5: Run all CLI agent tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_cli_agent.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 1**

```bash
git add src/civ_mcp/arena/cli_agent.py tests/arena/test_cli_agent.py
git commit -m "fix(arena): preserve cli standing plan tails"
```

---

## Task 2: Make Standing-Plan Header Parsing Format-Tolerant

**Files:**
- Modify: `src/civ_mcp/arena/memory.py`
- Test: `tests/arena/test_memory.py`

- [ ] **Step 1: Add failing parser regressions**

In `tests/arena/test_memory.py`, add:

```python
def test_extract_standing_plan_accepts_markdown_heading_forms():
    cases = [
        "**STANDING PLAN:**\n- keep scout moving\n",
        "- STANDING PLAN:\n- keep scout moving\n",
        "## STANDING PLAN:\n- keep scout moving\n",
    ]

    for summary in cases:
        assert extract_standing_plan(summary, max_chars=1200) == "keep scout moving"


def test_extract_standing_plan_stops_at_titlecase_known_unbulleted_header():
    summary = (
        "STANDING PLAN:\n"
        "- keep builder near copper\n"
        "Tactical:\n"
        "- unrelated reflection content\n"
    )

    result = extract_standing_plan(summary, max_chars=1200)

    assert result == "keep builder near copper"
```

Note: the existing `test_extract_standing_plan_keeps_all_caps_bullet_ending_colon`
(which asserts `- BUILD CAMPUS:` is *kept* as plan content) must continue to pass
unchanged — do not delete or modify it. Arbitrary all-caps bulleted lines are plan
content by design; only whitelisted reflection headers terminate the block.

- [ ] **Step 2: Run the new memory parser tests and verify they fail**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest \
  tests/arena/test_memory.py::test_extract_standing_plan_accepts_markdown_heading_forms \
  tests/arena/test_memory.py::test_extract_standing_plan_stops_at_titlecase_known_unbulleted_header -q
```

Expected: FAIL. The current marker regex misses markdown heading forms, and unbulleted `Tactical:` (title-case) is swallowed because the current unbulleted rule only matches all-caps via `body.isupper()`.

- [ ] **Step 3: Replace literal marker and section-header checks with normalized helpers**

In `src/civ_mcp/arena/memory.py`, replace `_STANDING_PLAN_RE` and `_BULLET_PREFIX_RE` with:

```python
_STANDING_PLAN_RE = re.compile(
    r"^\s*(?:[-*\u2022]+\s*)?(?:#{1,6}\s*)?(?:[*_]{1,3})?\s*standing plan\s*:\s*(?:[*_]{1,3})?\s*(.*)$",
    re.IGNORECASE,
)
_BULLET_PREFIX_RE = re.compile(r"^\s*[-*\u2022]+\s*")
_HEADING_PREFIX_RE = re.compile(r"^\s*(?:[-*\u2022]+\s*)?(?:#{1,6}\s*)?")
_HEADING_EMPHASIS_RE = re.compile(r"^(?:[*_]{1,3})?(.*?)(?:[*_]{1,3})?$")
```

Add these helpers before `_is_section_header()`:

```python
def _header_body(line: str) -> tuple[str, bool]:
    stripped = line.strip()
    bullet = _BULLET_PREFIX_RE.match(stripped) is not None
    candidate = _HEADING_PREFIX_RE.sub("", stripped, count=1).strip()
    if not candidate.endswith(":"):
        return "", bullet
    body = candidate[:-1].strip()
    emphasis = _HEADING_EMPHASIS_RE.match(body)
    if emphasis:
        body = emphasis.group(1).strip()
    return body, bullet
```

Replace `_is_section_header()` with:

```python
def _is_section_header(line: str, following_lines: Sequence[str] = ()) -> bool:
    body, bullet = _header_body(line)
    if not body:
        return False

    header = body.upper()
    if bullet:
        # Bulleted lines terminate ONLY on a known reflection header. An arbitrary
        # all-caps imperative bullet like "- BUILD CAMPUS:" is legitimate plan
        # content and must be kept (test_extract_standing_plan_keeps_all_caps_
        # bullet_ending_colon). PLANNING is task-aware: it is only a terminator when
        # no TASK/CANCEL line follows before the next header.
        if header in _TASK_AWARE_BULLETED_PLAN_SUBHEADINGS:
            return not _has_task_line_before_next_header(following_lines)
        return header in _BULLETED_SECTION_HEADERS

    # Unbulleted: a known reflection header (matched case-insensitively, so title-case
    # "Tactical:" terminates) or any all-caps line ("STRATEGIC NOTES:") terminates.
    return header in _BULLETED_SECTION_HEADERS or body.isupper()
```

Keep `_TASK_AWARE_BULLETED_PLAN_SUBHEADINGS = frozenset({"PLANNING"})` unchanged.

This change is deliberately minimal versus current behavior: the bulleted branch is
logically identical to today's whitelist rule (arbitrary bulleted all-caps stays
plan content); only the marker regex broadening and the unbulleted case-insensitive
`header in _BULLETED_SECTION_HEADERS` match are new. Every existing `test_memory.py`
case therefore continues to pass.

- [ ] **Step 4: Run memory parser tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_memory.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 2**

```bash
git add src/civ_mcp/arena/memory.py tests/arena/test_memory.py
git commit -m "fix(arena): tolerate standing plan heading formats"
```

---

## Task 3: Refresh Executed Tasks And Resolve Unit Index Aliases

**Files:**
- Modify: `src/civ_mcp/arena/task_tracker.py`
- Modify: `src/civ_mcp/arena/coordinator.py`
- Test: `tests/arena/test_task_tracker.py`
- Test: `tests/arena/test_coordinator.py`

- [ ] **Step 1: Add failing task tracker regressions**

In `tests/arena/test_task_tracker.py`, add:

```python
@pytest.mark.asyncio
async def test_run_pre_model_tasks_bumps_updated_turn_for_active_followthrough():
    unit = _unit(unit_id=65537, unit_index=1, x=1, y=1)
    gs = FakeGS(units=[unit])
    task = _task(
        task_id="settle:65537",
        unit_id=65537,
        target_x=18,
        target_y=24,
        created_turn=5,
        updated_turn=5,
    )

    updated, results = await run_pre_model_tasks(gs, [task], turn=12)

    assert updated[0].status == "active"
    assert updated[0].updated_turn == 12
    assert results[0]["action"] == "move"


@pytest.mark.asyncio
async def test_run_pre_model_tasks_resolves_unit_index_alias_from_plan():
    unit = _unit(unit_id=65537, unit_index=1, x=18, y=24)
    gs = FakeGS(units=[unit])
    task = _task(task_id="settle:1", unit_id=1, target_x=18, target_y=24)

    updated, results = await run_pre_model_tasks(gs, [task])

    assert gs.found_city_calls == [1]
    assert updated[0].status == "complete"
    assert results[0]["action"] == "found_city"
```

- [ ] **Step 2: Add a failing coordinator regression for passing the turn**

In `tests/arena/test_coordinator.py`, add after `test_pre_model_task_results_appear_in_log_and_transcript`:

```python
@pytest.mark.asyncio
async def test_pre_model_task_execution_refreshes_updated_turn(tmp_path):
    opts = CivOptions(task_tracker=TaskTrackerOptions(enabled=True, max_tasks=8))
    run_id, player_id = "task-refresh", 6
    existing_task = UnitTask(
        task_id="settle:65537",
        kind="settle",
        unit_id=65537,
        target_x=10,
        target_y=10,
        created_turn=2,
        updated_turn=2,
    )
    save_task_state(str(tmp_path), run_id, player_id, [existing_task])
    cfg = ArenaConfig(
        players=[PlayerSpec(player_id, "local", "m")],
        max_puppet_turns=1,
        dry_run=True,
        puppet_ids=[player_id],
        run_id=run_id,
        transcript_dir=str(tmp_path),
    )
    pol = RecordingPolicy(
        {"summary": "no new task", "transcript": {"final_summary": "TACTICAL: none"}},
        options=opts,
    )
    conn = FakeConn()
    conn._polls = iter([
        ["LOCAL|0", "TURN|1", "ACTIVE|false", "LAST|nil"],
        [f"LOCAL|{player_id}", "TURN|9", "ACTIVE|true", "LAST|1"],
    ])
    gs = FakeGSWithUnit(unit_id=65537, unit_index=1, x=1, y=1)

    await run_arena(conn, gs, cfg, policy=pol)

    saved = task_path(str(tmp_path), run_id, player_id).read_text()
    assert '"updated_turn": 9' in saved
```

- [ ] **Step 3: Run the new task aging and alias tests and verify they fail**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest \
  tests/arena/test_task_tracker.py::test_run_pre_model_tasks_bumps_updated_turn_for_active_followthrough \
  tests/arena/test_task_tracker.py::test_run_pre_model_tasks_resolves_unit_index_alias_from_plan \
  tests/arena/test_coordinator.py::test_pre_model_task_execution_refreshes_updated_turn -q
```

Expected: FAIL. `run_pre_model_tasks()` currently has no `turn` keyword, unit-index alias tasks are marked lost, and the coordinator does not refresh `updated_turn`.

- [ ] **Step 4: Add unit lookup and touch helpers**

In `src/civ_mcp/arena/task_tracker.py`, add these helpers before `_run_single_task()`:

```python
def _touch_task(task: UnitTask, turn: int | None) -> UnitTask:
    if turn is None:
        return task
    return replace(task, updated_turn=turn)


def _unit_lookup_maps(units: Sequence[Any]) -> tuple[dict[int, Any], dict[int, Any]]:
    by_id = {unit.unit_id: unit for unit in units}
    by_index: dict[int, Any] = {}
    for unit in units:
        by_index.setdefault(unit.unit_index, unit)
        by_index.setdefault(unit.unit_id % 65536, unit)
    return by_id, by_index


def _resolve_task_unit(task: UnitTask, units_by_id: dict[int, Any], units_by_index: dict[int, Any]) -> Any | None:
    return units_by_id.get(task.unit_id) or units_by_index.get(task.unit_id)
```

Change `_run_single_task()` signature to:

```python
async def _run_single_task(
    gs: Any,
    task: UnitTask,
    units_by_id: dict[int, Any],
    units_by_index: dict[int, Any],
    owner_context: _HostileOwnerContext,
    turn: int | None,
) -> tuple[UnitTask, dict[str, Any]]:
```

Replace its first line with:

```python
    unit = _resolve_task_unit(task, units_by_id, units_by_index)
```

Then wrap **every** `new_task` that `_run_single_task()` returns with `status="active"` in `_touch_task(..., turn)` so an executed active task's `updated_turn` advances (the regression checks the *move* branch specifically, so wrapping only the skip example is not enough). Leave `status` values `"lost"`, `"complete"`, and `"failed"` unwrapped — those tasks are dropped from persisted state.

There are six active-status returns to wrap. Apply each edit:

Skip (no moves):
```python
new_task = _touch_task(replace(task, last_result="skipped_no_moves"), turn)
```

Settle blocked by visible hostile:
```python
new_task = _touch_task(replace(task, last_result="blocked_visible_hostile"), turn)
```

Settle move:
```python
result_str = await gs.move_unit(unit.unit_index, task.target_x, task.target_y)
new_task = _touch_task(replace(task, last_result=result_str), turn)
return new_task, _result_dict(task, status="active", action="move", result=result_str)
```

Builder-improve at-target `Error:` (still active for retry):
```python
new_task = _touch_task(replace(task, last_result=result_str), turn)
return new_task, _result_dict(
    task, status="active", action="improve", result=result_str
)
```

Builder-improve `blocked_improvement_not_valid` (first strike, still active):
```python
new_task = _touch_task(replace(task, last_result="blocked_improvement_not_valid"), turn)
```

Builder blocked by visible hostile:
```python
new_task = _touch_task(replace(task, last_result="blocked_visible_hostile"), turn)
```

Builder move:
```python
result_str = await gs.move_unit(unit.unit_index, task.target_x, task.target_y)
new_task = _touch_task(replace(task, last_result=result_str), turn)
return new_task, _result_dict(task, status="active", action="move", result=result_str)
```

(The settle at-target `found_city` `Error:` active branch is rewritten in Task 4 Step 4, which already wraps it in `_touch_task`; do not double-wrap it here. The per-task exception fallback in `run_pre_model_tasks()` — `replace(task, last_result=error_msg)` — is left unwrapped; it is a rare defensive path with no regression and touching it is out of scope.)

- [ ] **Step 5: Thread `turn` through `run_pre_model_tasks()` and coordinator**

Change `run_pre_model_tasks()` signature in `src/civ_mcp/arena/task_tracker.py`:

```python
async def run_pre_model_tasks(
    gs: Any, tasks: Sequence[UnitTask], *, turn: int | None = None
) -> tuple[tuple[UnitTask, ...], list[dict[str, Any]]]:
```

Inside `run_pre_model_tasks()`, replace:

```python
units_by_id = {unit.unit_id: unit for unit in units}
```

with:

```python
units_by_id, units_by_index = _unit_lookup_maps(units)
```

Update `_task_needs_hostile_context()` calls to use the resolved unit:

```python
if any(
    _task_needs_hostile_context(
        task, _resolve_task_unit(task, units_by_id, units_by_index)
    )
    for task in executable
):
```

Update `_run_single_task()` invocation:

```python
new_task, result = await _run_single_task(
    gs, task, units_by_id, units_by_index, owner_context, turn
)
```

In `src/civ_mcp/arena/coordinator.py`, change:

```python
updated_tasks, task_results = await run_pre_model_tasks(
    gs, active_tasks_before
)
```

to:

```python
updated_tasks, task_results = await run_pre_model_tasks(
    gs, active_tasks_before, turn=st.turn
)
```

- [ ] **Step 6: Run task tracker and coordinator focused tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_task_tracker.py tests/arena/test_coordinator.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit Task 3**

```bash
git add src/civ_mcp/arena/task_tracker.py src/civ_mcp/arena/coordinator.py tests/arena/test_task_tracker.py tests/arena/test_coordinator.py
git commit -m "fix(arena): refresh deterministic task followthrough"
```

---

## Task 4: Bound Settle Retries And Keep Builder No-Responses Active

**Files:**
- Modify: `src/civ_mcp/arena/task_tracker.py`
- Test: `tests/arena/test_task_tracker.py`

- [ ] **Step 1: Add failing task execution regressions**

In `tests/arena/test_task_tracker.py`, add:

```python
@pytest.mark.asyncio
async def test_settle_fails_after_repeated_found_city_error():
    unit = _unit(unit_id=65537, unit_index=1, x=18, y=24)
    gs = FakeGS(units=[unit], found_city_result="Error: FOUND_FAILED")
    task = _task(
        task_id="settle:65537",
        unit_id=65537,
        target_x=18,
        target_y=24,
        last_result="Error: FOUND_FAILED",
    )

    updated, results = await run_pre_model_tasks(gs, [task])

    assert updated[0].status == "failed"
    assert updated[0].last_result == "found_city_failed_retry_limit"
    assert results[0]["status"] == "failed"
    assert results[0]["action"] == "found_city"
    assert results[0]["result"] == "found_city_failed_retry_limit"


@pytest.mark.asyncio
async def test_builder_improve_no_response_stays_active_for_retry():
    unit = _unit(
        unit_id=65538,
        unit_index=2,
        x=12,
        y=19,
        valid_improvements=["IMPROVEMENT_MINE"],
    )
    gs = FakeGS(units=[unit], improve_tile_result="Action completed (no response).")
    task = _task(
        task_id="builder_improve:65538",
        kind="builder_improve",
        unit_id=65538,
        target_x=12,
        target_y=19,
        improvement="IMPROVEMENT_MINE",
    )

    updated, results = await run_pre_model_tasks(gs, [task])

    assert updated[0].status == "active"
    assert updated[0].last_result == "improve_no_response"
    assert results[0]["status"] == "active"
    assert results[0]["action"] == "improve"
    assert results[0]["result"] == "improve_no_response"


@pytest.mark.asyncio
async def test_builder_improve_no_response_fails_after_retry():
    unit = _unit(
        unit_id=65538,
        unit_index=2,
        x=12,
        y=19,
        valid_improvements=["IMPROVEMENT_MINE"],
    )
    gs = FakeGS(units=[unit], improve_tile_result="Action completed (no response).")
    task = _task(
        task_id="builder_improve:65538",
        kind="builder_improve",
        unit_id=65538,
        target_x=12,
        target_y=19,
        improvement="IMPROVEMENT_MINE",
        last_result="improve_no_response",
    )

    updated, results = await run_pre_model_tasks(gs, [task])

    assert updated[0].status == "failed"
    assert updated[0].last_result == "improve_no_response_retry_limit"
    assert results[0]["status"] == "failed"
    assert results[0]["result"] == "improve_no_response_retry_limit"
```

- [ ] **Step 2: Run the new execution tests and verify they fail**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest \
  tests/arena/test_task_tracker.py::test_settle_fails_after_repeated_found_city_error \
  tests/arena/test_task_tracker.py::test_builder_improve_no_response_stays_active_for_retry \
  tests/arena/test_task_tracker.py::test_builder_improve_no_response_fails_after_retry -q
```

Expected: FAIL. Settle remains active forever and builder no-response is currently marked complete.

- [ ] **Step 3: Add shared result constants**

In `src/civ_mcp/arena/task_tracker.py`, add near `TASK_KINDS`:

```python
ACTION_NO_RESPONSE = "Action completed (no response)."
FOUND_CITY_RETRY_LIMIT = "found_city_failed_retry_limit"
IMPROVE_NO_RESPONSE = "improve_no_response"
IMPROVE_NO_RESPONSE_RETRY_LIMIT = "improve_no_response_retry_limit"
```

- [ ] **Step 4: Bound repeated found-city errors**

In the settle at-target branch, replace the `result_str.startswith("Error:")` block with:

```python
if result_str.startswith("Error:"):
    if task.last_result == result_str:
        new_task = replace(
            task,
            status="failed",
            last_result=FOUND_CITY_RETRY_LIMIT,
        )
        return new_task, _result_dict(
            task,
            status="failed",
            action="found_city",
            result=FOUND_CITY_RETRY_LIMIT,
        )
    new_task = _touch_task(replace(task, last_result=result_str), turn)
    return new_task, _result_dict(
        task, status="active", action="found_city", result=result_str
    )
```

If Task 3 has not introduced `_touch_task()` yet, complete Task 3 first.

- [ ] **Step 5: Treat builder no-response as unconfirmed**

In the builder-improve at-target branch, after the `Error:` block and before marking complete, add:

```python
if result_str == ACTION_NO_RESPONSE:
    if task.last_result == IMPROVE_NO_RESPONSE:
        new_task = replace(
            task,
            status="failed",
            last_result=IMPROVE_NO_RESPONSE_RETRY_LIMIT,
        )
        return new_task, _result_dict(
            task,
            status="failed",
            action="improve",
            result=IMPROVE_NO_RESPONSE_RETRY_LIMIT,
        )
    new_task = _touch_task(replace(task, last_result=IMPROVE_NO_RESPONSE), turn)
    return new_task, _result_dict(
        task,
        status="active",
        action="improve",
        result=IMPROVE_NO_RESPONSE,
    )
```

- [ ] **Step 6: Run task tracker tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_task_tracker.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit Task 4**

```bash
git add src/civ_mcp/arena/task_tracker.py tests/arena/test_task_tracker.py
git commit -m "fix(arena): bound deterministic task retries"
```

---

## Task 5: Guard Memory And Task Tracker Capture In The Coordinator

**Files:**
- Modify: `src/civ_mcp/arena/coordinator.py`
- Test: `tests/arena/test_coordinator.py`

- [ ] **Step 1: Add failing coordinator resilience regressions**

In `tests/arena/test_coordinator.py`, add:

```python
@pytest.mark.asyncio
async def test_memory_save_failure_does_not_abort_arena_turn(monkeypatch, tmp_path):
    from civ_mcp.arena import coordinator

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(coordinator, "save_memory", boom)
    opts = CivOptions(memory=MemoryOptions(enabled=True, max_chars=1200))
    cfg = ArenaConfig(
        players=[PlayerSpec(4, "local", "m")],
        max_puppet_turns=1,
        dry_run=True,
        puppet_ids=[4],
        run_id="mem-save-failure",
        transcript_dir=str(tmp_path),
    )
    pol = RecordingPolicy(
        {
            "summary": "ignored",
            "transcript": {"final_summary": "STANDING PLAN:\n- keep exploring\n"},
        },
        options=opts,
    )
    conn = FakeConn()
    conn._polls = iter([
        ["LOCAL|0", "TURN|1", "ACTIVE|false", "LAST|nil"],
        ["LOCAL|4", "TURN|2", "ACTIVE|true", "LAST|1"],
    ])
    sink = FakeSink()

    result = await run_arena(conn, FakeGS(), cfg, policy=pol, transcript=sink)

    assert result["puppet_turns_played"] == 1
    assert result["log"][0]["standing_memory"]["error"] == "OSError('disk full')"
    assert sink.records[0]["standing_memory"]["error"] == "OSError('disk full')"


@pytest.mark.asyncio
async def test_task_state_save_failure_does_not_abort_arena_turn(monkeypatch, tmp_path):
    from civ_mcp.arena import coordinator

    def boom(*args, **kwargs):
        raise OSError("read only")

    monkeypatch.setattr(coordinator, "save_task_state", boom)
    opts = CivOptions(task_tracker=TaskTrackerOptions(enabled=True, max_tasks=8))
    cfg = ArenaConfig(
        players=[PlayerSpec(5, "local", "m")],
        max_puppet_turns=1,
        dry_run=True,
        puppet_ids=[5],
        run_id="task-save-failure",
        transcript_dir=str(tmp_path),
    )
    pol = RecordingPolicy(
        {
            "summary": "ignored",
            "transcript": {
                "final_summary": "STANDING PLAN:\nTASK settle unit_id=42 target=10,12\n"
            },
        },
        options=opts,
    )
    conn = FakeConn()
    conn._polls = iter([
        ["LOCAL|0", "TURN|1", "ACTIVE|false", "LAST|nil"],
        ["LOCAL|5", "TURN|2", "ACTIVE|true", "LAST|1"],
    ])
    sink = FakeSink()

    result = await run_arena(conn, FakeGS(), cfg, policy=pol, transcript=sink)

    assert result["puppet_turns_played"] == 1
    assert result["log"][0]["task_tracker"]["error"] == "OSError('read only')"
    assert sink.records[0]["task_tracker"]["error"] == "OSError('read only')"
```

- [ ] **Step 2: Run the new coordinator tests and verify they fail**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest \
  tests/arena/test_coordinator.py::test_memory_save_failure_does_not_abort_arena_turn \
  tests/arena/test_coordinator.py::test_task_state_save_failure_does_not_abort_arena_turn -q
```

Expected: FAIL. Current bare save calls propagate `OSError`.

- [ ] **Step 3: Add error fields and guarded memory/task tracker blocks**

In `src/civ_mcp/arena/coordinator.py`, before loading memory, initialize:

```python
memory_error = ""
task_tracker_error = ""
```

Wrap memory load/format:

```python
try:
    memory = load_memory(transcript_dir, run_id, st.local) if opts.memory.enabled else None
    memory_block = format_memory_block(
        memory,
        current_turn=st.turn,
        max_age_turns=opts.memory.max_age_turns,
    )
except Exception as e:
    memory = None
    memory_block = ""
    memory_error = repr(e)
    print(f"[arena] standing memory load failed: {e!r}", file=sys.stderr)
```

Wrap task state load/run/pre-model save:

```python
if opts.task_tracker.enabled:
    try:
        task_state = load_task_state(transcript_dir, run_id, st.local)
        active_tasks_before = tuple(
            t for t in task_state.tasks if t.status == "active"
        )
        updated_tasks, task_results = await run_pre_model_tasks(
            gs, active_tasks_before, turn=st.turn
        )
        pre_model_state = save_task_state(
            transcript_dir, run_id, st.local, updated_tasks
        )
        active_tasks_after = pre_model_state.tasks
        task_block = format_task_block(
            updated_tasks,
            task_results,
            max_tasks=opts.task_tracker.max_tasks,
        )
    except Exception as e:
        active_tasks_before = ()
        updated_tasks = ()
        task_results = []
        active_tasks_after = ()
        task_block = ""
        task_tracker_error = repr(e)
        print(f"[arena] task tracker pre-model failed: {e!r}", file=sys.stderr)
```

Wrap memory capture save:

```python
if opts.memory.enabled and captured_plan and not memory_error:
    try:
        save_memory(
            transcript_dir, run_id, st.local, st.turn, captured_plan,
            opts.memory.max_chars,
        )
    except Exception as e:
        memory_error = repr(e)
        print(f"[arena] standing memory save failed: {e!r}", file=sys.stderr)
```

Wrap task capture parse/merge/save:

```python
if opts.task_tracker.enabled and not task_tracker_error:
    try:
        new_tasks = parse_task_lines(captured_plan, st.turn)
        merged = merge_tasks(updated_tasks, new_tasks, opts.task_tracker.max_tasks)
        captured_state = save_task_state(transcript_dir, run_id, st.local, merged)
        active_tasks_after = captured_state.tasks
    except Exception as e:
        task_tracker_error = repr(e)
        print(f"[arena] task tracker capture failed: {e!r}", file=sys.stderr)
```

Add `"error": memory_error` to `_standing_memory_fields` and `"error": task_tracker_error` to `_task_tracker_fields`.

- [ ] **Step 4: Run coordinator tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_coordinator.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 5**

```bash
git add src/civ_mcp/arena/coordinator.py tests/arena/test_coordinator.py
git commit -m "fix(arena): guard standing state persistence"
```

---

## Task 6: Fix Behavior Metrics For Task Tracker And CLI Trade Routes

**Files:**
- Modify: `src/civ_mcp/arena/analyze.py`
- Test: `tests/arena/test_analyze.py`

- [ ] **Step 1: Add failing behavior metric regressions**

In `tests/arena/test_analyze.py`, add near the existing behavior tests:

```python
def test_behavior_task_tracker_turns_ignore_zero_filled_disabled_records() -> None:
    from civ_mcp.arena.analyze import analyze

    disabled_record = {
        "schema_version": 1,
        "run_id": "behavior-test",
        "player_id": 4,
        "turn": 1,
        "driver": "cli",
        "steps": [],
        "standing_memory": {"loaded": False, "injected_chars": 0, "captured_chars": 0},
        "task_tracker": {
            "active_before": 0,
            "pre_model_results": [],
            "active_after": 0,
        },
    }

    report = analyze([disabled_record], [])

    assert report["behavior"]["task_tracker_turns"] == 0


def test_behavior_counts_cli_unit_action_trade_routes() -> None:
    from civ_mcp.arena.analyze import analyze

    rec = {
        "schema_version": 1,
        "run_id": "behavior-test",
        "player_id": 2,
        "turn": 1,
        "driver": "cli",
        "steps": [
            {
                "tool_name": "mcp__civ6__unit_action",
                "tool_args": {"action": "trade_route", "unit_id": 65538, "x": 10, "y": 12},
                "tool_result_full": "ROUTE_STARTED",
            },
            {
                "tool_name": "mcp__civ6__unit_action",
                "tool_args": {"action": "teleport", "unit_id": 65538, "x": 5, "y": 6},
                "tool_result_full": "TELEPORTED",
            },
        ],
        "invalid_tool_calls": [],
        "standing_memory": {"loaded": False, "injected_chars": 0, "captured_chars": 0},
    }

    report = analyze([rec], [])

    assert report["by_player"][2]["behavior"]["trade_route_tool_calls"] == 2
```

- [ ] **Step 2: Run the new behavior metric tests and verify they fail**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest \
  tests/arena/test_analyze.py::test_behavior_task_tracker_turns_ignore_zero_filled_disabled_records \
  tests/arena/test_analyze.py::test_behavior_counts_cli_unit_action_trade_routes -q
```

Expected: FAIL. The zero-filled task tracker dict is counted and CLI `unit_action` trade verbs are not counted.

- [ ] **Step 3: Add task tracker activity helper**

In `src/civ_mcp/arena/analyze.py`, add:

```python
def _task_tracker_active(rec: dict) -> bool:
    tt = rec.get("task_tracker")
    if not isinstance(tt, dict):
        return False
    if tt.get("active_before") or tt.get("active_after"):
        return True
    return bool(_task_tracker_pre_model_results(rec))
```

Replace:

```python
if rec.get("task_tracker") is not None:
    task_tracker_turns += 1
```

with:

```python
if _task_tracker_active(rec):
    task_tracker_turns += 1
```

- [ ] **Step 4: Count CLI unit_action trade verbs**

In `src/civ_mcp/arena/analyze.py`, add:

```python
_TRADE_ROUTE_UNIT_ACTIONS: frozenset[str] = frozenset({"trade_route", "teleport"})
```

Replace `_count_tool_calls()` with:

```python
def _count_tool_calls(steps: list[dict], tool_bases: "frozenset[str]") -> int:
    """Count behavior tool calls after normalizing local and CLI tool vocabularies."""
    count = 0
    for step in steps:
        tool_base, verb = _step_verb(step)
        if tool_base in tool_bases:
            count += 1
            continue
        if tool_bases is _TRADE_ROUTE_TOOLS and tool_base == "unit_action" and verb in _TRADE_ROUTE_UNIT_ACTIONS:
            count += 1
    return count
```

- [ ] **Step 5: Run analyze tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_analyze.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 6**

```bash
git add src/civ_mcp/arena/analyze.py tests/arena/test_analyze.py
git commit -m "fix(arena): correct behavior metrics"
```

---

## Task 7: Apply CLI Context Budget To Briefing Construction

**Files:**
- Modify: `src/civ_mcp/arena/cli_agent.py`
- Test: `tests/arena/test_cli_agent.py`

- [ ] **Step 1: Add failing CLI context-budget regression**

In `tests/arena/test_cli_agent.py`, the module-level import currently reads
`from civ_mcp.arena.config import CivOptions, MemoryOptions, TaskTrackerOptions`.
Add `BriefingOptions` to it so it reads:

```python
from civ_mcp.arena.config import BriefingOptions, CivOptions, MemoryOptions, TaskTrackerOptions
```

Then add:

```python
@pytest.mark.asyncio
async def test_cli_policy_uses_explicit_context_budget_for_briefing(monkeypatch):
    from civ_mcp.arena import cli_agent
    from civ_mcp.arena.briefing import Briefing

    captured = {}

    async def fake_briefing(gs, options, *, n_ctx, playbook_chars, tool_schema_chars, supplied=None):
        captured["n_ctx"] = n_ctx
        captured["playbook_chars"] = playbook_chars
        captured["tool_schema_chars"] = tool_schema_chars
        captured["supplied"] = supplied
        return Briefing(text="", tokens=0, sections=[])

    class FakeProc:
        pid = 1
        returncode = 0

        async def communicate(self):
            return (
                json.dumps({
                    "type": "result",
                    "subtype": "success",
                    "result": "ok",
                    "usage": {},
                    "total_cost_usd": 0.0,
                }).encode(),
                b"",
            )

        async def wait(self):
            pass

    async def fake_create(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(cli_agent, "maybe_build_briefing", fake_briefing)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    pol = CLIAgentPolicy(
        "cli-claude",
        FakeCost(),
        project_dir="/x",
        timeout_s=5,
        options=CivOptions(
            context_budget=8192,
            briefing=BriefingOptions(enabled=True),
        ),
    )

    await pol(None, player_id=3, turn=7)

    assert captured["n_ctx"] == 8192
    assert captured["tool_schema_chars"] == 0
```

- [ ] **Step 2: Run the new context-budget test and verify it fails**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_cli_agent.py::test_cli_policy_uses_explicit_context_budget_for_briefing -q
```

Expected: FAIL because `CLIAgentPolicy` passes `DEFAULT_N_CTX` to `maybe_build_briefing()`.

- [ ] **Step 3: Use configured explicit CLI context budgets**

In `src/civ_mcp/arena/cli_agent.py`, replace:

```python
briefing = await maybe_build_briefing(
    gs,
    self.options,
    n_ctx=DEFAULT_N_CTX,
    playbook_chars=playbook_chars,
    tool_schema_chars=0,
    supplied=briefing,
)
```

with:

```python
n_ctx = (
    DEFAULT_N_CTX
    if self.options.context_budget == "auto"
    else int(self.options.context_budget)
)
briefing = await maybe_build_briefing(
    gs,
    self.options,
    n_ctx=n_ctx,
    playbook_chars=playbook_chars,
    tool_schema_chars=0,
    supplied=briefing,
)
```

Do not add network context probing for CLI policies in this task. The explicit integer is the operator's requested context budget; `"auto"` keeps the current default behavior.

- [ ] **Step 4: Run CLI tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_cli_agent.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 7**

```bash
git add src/civ_mcp/arena/cli_agent.py tests/arena/test_cli_agent.py
git commit -m "fix(arena): apply cli context budget"
```

---

## Task 8: Final Verification And Review Prep

**Files:**
- No code changes expected.

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
  tests/arena/test_prompt_context.py \
  tests/arena/test_analyze.py -q
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

Expected: `git diff --check` prints nothing. Status shows committed implementation changes plus only pre-existing untracked `.serena/` and plan docs unless the implementation intentionally adds this plan file.

- [ ] **Step 5: Request final code review**

Use `superpowers:requesting-code-review` against the full branch diff from `404e1a7` through the new HEAD. Ask the reviewer to focus on:

- CLI tail preservation for trailing standing plans,
- markdown/emphasis standing-plan marker parsing,
- whitelist-based (case-insensitive) section termination that keeps arbitrary all-caps plan bullets and does not break `Planning:` task subheadings,
- deterministic task age refresh and unit-id alias lookup,
- bounded settle/improve retries,
- coordinator resilience to memory/task state I/O failures,
- task tracker and trade-route behavior metrics,
- CLI context-budget behavior.

Expected: no Critical or Important issues.

---

## Self-Review Notes

- Spec coverage: all 10 validated findings map to Tasks 1 through 7, and Task 8 verifies the combined result. Finding #2b (unlisted bulleted headers swallowed) is intentionally *narrowed*: a general all-caps bulleted terminator would break the existing, deliberate `test_extract_standing_plan_keeps_all_caps_bullet_ending_colon` (imperative bullets like `- BUILD CAMPUS:` must survive). Task 2 instead broadens the marker regex and adds case-insensitive known-header termination; unlisted bulleted headers like `- NEXT STEPS:` are accepted as low-harm plan noise rather than cut.
- Placeholder scan: no `TBD`, `TODO`, or unspecified test/implementation instructions remain. Task 3 Step 4 now enumerates every active-return `_touch_task` site explicitly rather than relying on a single example.
- Type consistency: new `run_pre_model_tasks(..., turn=...)` is optional for existing tests and explicit in coordinator; new helper names are used consistently in the plan.
- Test-scaffolding correctness (verified against source): `run_arena` returns `puppet_turns_played` (not `turns`); coordinator tests that exercise a specific puppet must set `conn._polls` with an active `LOCAL|<player_id>` poll (the default `FakeConn` only yields `LOCAL|1`, and a non-matching `puppet_ids` idles the loop for the full poll budget); Task 7's test requires `BriefingOptions` in the `test_cli_agent.py` import.
