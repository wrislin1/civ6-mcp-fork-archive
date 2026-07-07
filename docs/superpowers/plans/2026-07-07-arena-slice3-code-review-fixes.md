# Arena Slice-3 Code-Review Fixes Implementation Plan

## Status — 2026-07-07: COMPLETE ✅ (merged to `main`)

Executed via `superpowers:subagent-driven-development` on branch
`arena-standing-memory-task-tracker-slice3`. All three tasks shipped, each
task-reviewed clean; final whole-branch review (opus) over `db72429..1f9914c`:
**Ready to merge — Yes**, zero Critical / zero Important.

- **Task 1 (finding #3 — the one real bug):** `5da4a37` — guard the exclusive-CLI
  briefing pre-build so a build failure degrades that civ to no-briefing instead
  of aborting the whole multi-civ run; matches the memory/task-tracker guards.
  + new test `test_briefing_build_failure_does_not_abort_arena_turn`.
- **Task 2 (findings #6 / #4-const):** `dfb71db` — single-source `RESOLVED_STATUSES`
  (frozenset) and import `BLOCKED_VISIBLE_HOSTILE` in analyze. Behavior-preserving,
  zero test edits; review confirmed no metric-bucket drift.
- **Task 3 (finding #8-doc):** `1f9914c` — clarify the standing-plan docstring
  (inline content needs a colon); regex `STANDING_PLAN_RE` unchanged.

Tests: `tests/arena/` 591/591, full `tests/` 701/701 green (was 700 at `db72429`;
+1 is Task 1's test). Findings #1, #2, #5, #7, #9, #10, #8-regex, and #4-semantics
were **retracted** as intentional / test-protected (see the "findings NOT being
fixed" table below) and deliberately left unchanged.

---

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply the code-review findings that survive scrutiny against the actual test suite: one real robustness fix (guard the exclusive-CLI briefing pre-build) plus two optional, behavior-preserving cleanups.

**Architecture:** The Civ-VI arena harness (`src/civ_mcp/arena/`) runs LLM/CLI puppets through a per-turn coordinator loop. Slice 3 added standing memory, a deterministic task tracker, and behavior metrics. Each per-civ pre-model context step (memory load, task-tracker pre-model run, briefing build) is meant to degrade that one civ on failure and never abort the multi-civ run. This plan closes the one step missing that guard and removes two small duplication hazards.

**Tech Stack:** Python 3.12, `pytest` + `pytest-asyncio`, dataclasses, `asyncio`. Tests live in `tests/arena/`.

## Global Constraints

- **Work on the branch, not the working tree.** The coherent Slice-3 code lives on git branch `arena-standing-memory-task-tracker-slice3` (tip `db72429`, full suite green). The current `main` working tree is a partial half-staged state (4 slice-3 files staged, the rest absent) — do **not** build on it. Check out the branch first: `git checkout arena-standing-memory-task-tracker-slice3`.
- **No behavior change outside the named bug.** Tasks 2–3 must keep the entire existing suite green with zero test edits (they are refactors/doc-only).
- **Run the arena suite after each task:** `python -m pytest tests/arena/ -q`. It is currently 700/700 green; keep it green.
- **Line numbers in this plan are as of branch tip `db72429`.** Re-anchor by the quoted surrounding text, not the raw line number, if the branch has moved.
- **Follow existing style:** async fakes (`FakeGS`, `FakeConn`, `RecordingPolicy`, `FakeSink`) already exist in the test modules; reuse them, do not invent new harnesses.

---

## Context: findings NOT being fixed (retracted after reading the tests)

The original review was produced by a finder/verifier pass that reasoned about the source **without the test suite**. Reading the tests shows most findings target intentional, test-asserted behavior. These are **deliberately excluded** from this plan (do not implement them):

| Finding | Why retracted | Test that pins the intent |
|---|---|---|
| #1 block_unknown strikes/abandons a task | Blocking an *unidentified* unit near a civilian's path when threat intel is unavailable is the designed safety behavior. | `test_diplomacy_failure_blocks_unknown_unit_label`, `test_threat_scan_failure_blocks_unknown_city_state_unit`, `test_missing_threat_scan_blocks_unknown_unit_label_without_aborting`, `test_threat_scan_failure_keeps_known_peaceful_major_unit_unblocked` (tests/arena/test_task_tracker.py:1063–1137) |
| #2 restatement guard suppresses re-issue | Suppressing a verbatim restatement of a resolved task is the whole point of tombstones (stops stale standing-plan echoes from resurrecting tasks). | `test_merge_restatement_does_not_resurrect_completed_task` / `_lost_task` / `_failed_task`, `test_merge_restated_task_preserves_failure_state` (test_task_tracker.py:735–842) |
| #5 `task_lost` counts `dropped_future_dated` | The metric deliberately counts a rollback-dropped task as lost. | `test_classify_task_results_excludes_non_attempt_bookkeeping` asserts `counts["lost"] == 1` for a `dropped_future_dated` entry (test_analyze.py:1655–1678) |
| #8 regex loosening for no-colon inline content | The regex deliberately requires end-of-line for the bare marker so prose never matches. | `test_bare_marker_requires_end_of_line_so_prose_does_not_match` (test_memory.py:521) |
| #9 clamp `final_summary` | It is intentionally raw — the coordinator parses TASK/CANCEL + standing plan from it; clamping would drop tasks. Implicitly bounded by model max-output. | cli_agent.py:594–598, 154–160 comments |
| #7 `behavior_metrics` re-classification | Offline analysis script; the drift risk it cites is already prevented by the single shared `_classify_task_results`. YAGNI. | n/a (both paths call the one classifier) |
| #10 rollback one-turn suppression | Self-heals the next turn when the future tombstone is dropped. YAGNI. | n/a |

If the design intent behind #1, #2, or the #5 metric semantics should *change*, that is a separate decision that requires deliberately rewriting those tests — out of scope here. Raise it with the maintainer before touching them.

---

## Task 1: Guard the exclusive-CLI briefing pre-build (finding #3)

**Why (real bug):** In `run_arena`, the memory-load block (coordinator.py:151–162) and the task-tracker pre-model block (175–207) each wrap their work in `try/except`, degrade to empty, and log — a tested invariant (`test_memory_save_failure_does_not_abort_arena_turn`, `test_task_state_save_failure_does_not_abort_arena_turn`). The briefing pre-build (220–234) is the one sibling with **no** guard. `run_arena`'s outer `try` has only a `finally`, no `except`, so a raise here propagates out and aborts the entire multi-civ run. `load_playbook()` at line 226 is a file read (realistic raise: missing/unreadable `playbook.md`); `explicit_n_ctx` / `briefing_budget` / `build_briefing`'s pre-loop setup can also raise. On failure this civ should degrade to no briefing (the same state a non-exclusive turn already uses), never kill the run.

**Files:**
- Modify: `src/civ_mcp/arena/coordinator.py` (the `if (exclusive and opts.briefing.enabled ...)` block, ~lines 220–234)
- Test: `tests/arena/test_coordinator.py` (new test near the existing briefing tests, ~line 1052)

**Interfaces:**
- Consumes: `run_arena(conn, gs, config, policy=..., transcript=...)`, `maybe_build_briefing` (imported into the `coordinator` module namespace at coordinator.py:17), `_policy_accepts_kwarg`, existing test fakes `FakeConn`, `FakeGS`, and config types `ArenaConfig`, `PlayerSpec`, `CivOptions`, `BriefingOptions`.
- Produces: no signature change. `run_arena` gains the invariant "a briefing-build exception degrades this civ to no briefing and the run continues."

- [ ] **Step 1: Write the failing test**

Add to `tests/arena/test_coordinator.py` (place it right after `test_exclusive_cli_briefing_built_before_disconnect`, ~line 1052). The policy accepts `briefing` (so the kwarg gate fires) and tolerates its absence via a default (so the degraded path runs):

```python
@pytest.mark.asyncio
async def test_briefing_build_failure_does_not_abort_arena_turn(monkeypatch):
    """A briefing-build raise (e.g. a missing playbook file) must degrade this
    civ to no briefing, not abort the whole multi-civ run -- mirroring the
    memory/task-tracker load guards."""
    from civ_mcp.arena import coordinator

    async def boom(*args, **kwargs):
        raise RuntimeError("playbook missing")

    monkeypatch.setattr(coordinator, "maybe_build_briefing", boom)

    seen = {}

    class ExclusiveBriefingPolicy:
        needs_exclusive_tuner = True
        options = CivOptions(briefing=BriefingOptions(enabled=True))
        provider = "cli-claude"

        async def __call__(self, gs, player_id, turn, *, briefing=None):
            seen["briefing"] = briefing
            return {"summary": "ran"}

    conn = FakeConn()
    conn._polls = iter([["LOCAL|7", "TURN|2", "ACTIVE|true", "LAST|1"]])
    cfg = ArenaConfig(
        players=[PlayerSpec(7, "cli-claude", "")],
        max_puppet_turns=1,
        puppet_ids=[7],
    )
    pol = ExclusiveBriefingPolicy()

    result = await run_arena(conn, FakeGS(), cfg, policy=pol)

    assert result["puppet_turns_played"] == 1   # run survived the briefing failure
    assert seen["briefing"] is None              # degraded to no briefing
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/arena/test_coordinator.py::test_briefing_build_failure_does_not_abort_arena_turn -q`
Expected: FAIL — the `RuntimeError("playbook missing")` propagates out of `run_arena` (no `except`), so the `await run_arena(...)` call raises instead of returning a result.

- [ ] **Step 3: Wrap the briefing pre-build in try/except**

In `src/civ_mcp/arena/coordinator.py`, replace the unguarded block:

```python
                if (
                    exclusive
                    and opts.briefing.enabled
                    and _policy_accepts_kwarg(pol, "briefing")
                ):
                    playbook_chars = (
                        len(load_playbook()) if opts.playbook == "condensed" else 0
                    )
                    policy_kwargs["briefing"] = await maybe_build_briefing(
                        gs,
                        opts,
                        n_ctx=explicit_n_ctx(opts.context_budget),
                        playbook_chars=playbook_chars,
                        tool_schema_chars=0,
                    )
```

with:

```python
                if (
                    exclusive
                    and opts.briefing.enabled
                    and _policy_accepts_kwarg(pol, "briefing")
                ):
                    try:
                        playbook_chars = (
                            len(load_playbook()) if opts.playbook == "condensed" else 0
                        )
                        policy_kwargs["briefing"] = await maybe_build_briefing(
                            gs,
                            opts,
                            n_ctx=explicit_n_ctx(opts.context_budget),
                            playbook_chars=playbook_chars,
                            tool_schema_chars=0,
                        )
                    except Exception as e:
                        # A per-civ briefing-build failure (a missing playbook
                        # file, a budget-calc raise) must degrade THIS civ to no
                        # briefing, never abort the whole multi-civ run --
                        # mirroring the memory/task-tracker load guards above and
                        # the promotion-sweep guard below. Omitting the kwarg is
                        # the same state a non-exclusive turn uses, so the policy
                        # already tolerates its absence.
                        print(f"[arena] briefing build failed: {e!r}", file=sys.stderr)
```

- [ ] **Step 4: Run the new test to verify it passes**

Run: `python -m pytest tests/arena/test_coordinator.py::test_briefing_build_failure_does_not_abort_arena_turn -q`
Expected: PASS

- [ ] **Step 5: Run the briefing regression tests to verify no behavior change on the success path**

Run: `python -m pytest tests/arena/test_coordinator.py -k briefing -q`
Expected: PASS (all existing briefing tests, including `test_exclusive_cli_briefing_built_before_disconnect` and `test_exclusive_cli_briefing_prebuild_uses_explicit_context_budget`, still green)

- [ ] **Step 6: Commit**

```bash
git add tests/arena/test_coordinator.py src/civ_mcp/arena/coordinator.py
git commit -m "fix(arena): degrade on briefing-build failure instead of aborting the run

The exclusive-CLI briefing pre-build was the only per-civ context step
without a try/except; a raise (e.g. a missing playbook.md via load_playbook)
propagated out of run_arena's bare try/finally and aborted the whole
multi-civ run. Wrap it to match the memory/task-tracker load guards."
```

---

## Task 2 (OPTIONAL — low value, behavior-preserving): De-duplicate the terminal-status set and the blocked-hostile literal (findings #6, #4-constant only)

**Why (maintainability, not a live bug):** The three terminal statuses `{"failed","complete","lost"}` appear as bare tuple literals at three sites across two files (task_tracker.py:161, task_tracker.py:363, analyze.py:197). Adding a future terminal status and missing one site would silently drop tombstones or mis-bucket a metric. Separately, analyze.py:199 hardcodes the string `"blocked_visible_hostile"` instead of importing the constant `BLOCKED_VISIBLE_HOSTILE` that defines it — a coupling hazard if the constant's value ever changes. Both are pure de-duplication with **no behavior change** (identical values), so the existing suite must stay green with zero test edits.

> Scope note: this task does **not** change any metric semantics (it does not make the `_retry_limit` terminal string count toward `blocked_visible_hostile`, and it does not stop `dropped_future_dated` counting as lost). Those are the retracted findings #4-semantics/#5 and are intentionally left alone.

**Files:**
- Modify: `src/civ_mcp/arena/task_tracker.py` (add constant; use at lines 161, 363)
- Modify: `src/civ_mcp/arena/analyze.py` (extend the task_tracker import; use at lines 197, 199)

**Interfaces:**
- Produces: `task_tracker.RESOLVED_STATUSES: frozenset[str]` = `{"failed", "complete", "lost"}`, importable by analyze.

- [ ] **Step 1: Add the shared constant in task_tracker.py**

In `src/civ_mcp/arena/task_tracker.py`, after `MAX_TASK_FAILURES = 3` (line 63), add:

```python
# The three terminal task statuses. save_task_state persists them as
# tombstones, merge_tasks keeps them outside the active cap so the restatement
# guard keeps recognizing them, and analyze buckets its metrics by the same
# set. Single source of truth so a new terminal status can't be added at one
# site and missed at another.
RESOLVED_STATUSES = frozenset({"failed", "complete", "lost"})
```

- [ ] **Step 2: Use it at the two task_tracker.py sites**

Replace at line 160–162 (`save_task_state`):

```python
    persisted = tuple(
        t for t in tasks if t.status in ("active", "failed", "complete", "lost")
    )
```

with:

```python
    persisted = tuple(
        t for t in tasks if t.status == "active" or t.status in RESOLVED_STATUSES
    )
```

Replace at line 360–364 (end of `merge_tasks`):

```python
    return tuple(
        task
        for task in ordered
        if task.task_id in kept or task.status in ("failed", "complete", "lost")
    )
```

with:

```python
    return tuple(
        task
        for task in ordered
        if task.task_id in kept or task.status in RESOLVED_STATUSES
    )
```

- [ ] **Step 3: Extend the analyze.py import and use both constants**

In `src/civ_mcp/arena/analyze.py`, replace the import (lines 23–27):

```python
from civ_mcp.arena.task_tracker import (
    DROPPED_FUTURE_DATED,
    SKIPPED_NO_MOVES,
    UNITS_FETCH_FAILED,
)
```

with:

```python
from civ_mcp.arena.task_tracker import (
    BLOCKED_VISIBLE_HOSTILE,
    DROPPED_FUTURE_DATED,
    RESOLVED_STATUSES,
    SKIPPED_NO_MOVES,
    UNITS_FETCH_FAILED,
)
```

Then in `_classify_task_results`, replace lines 197–200:

```python
        status = entry.get("status")
        if status in ("complete", "lost", "failed"):
            counts[status] += 1
        if entry.get("result") == "blocked_visible_hostile":
            counts["blocked_visible_hostile"] += 1
```

with:

```python
        status = entry.get("status")
        if status in RESOLVED_STATUSES:
            counts[status] += 1
        if entry.get("result") == BLOCKED_VISIBLE_HOSTILE:
            counts["blocked_visible_hostile"] += 1
```

- [ ] **Step 4: Run the full arena suite to confirm zero behavior change**

Run: `python -m pytest tests/arena/ -q`
Expected: PASS, same count as before (700/700). No test edits.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/task_tracker.py src/civ_mcp/arena/analyze.py
git commit -m "refactor(arena): single source for terminal statuses + blocked-hostile constant

Extract RESOLVED_STATUSES (failed/complete/lost) used by save_task_state,
merge_tasks, and analyze; import BLOCKED_VISIBLE_HOSTILE in analyze instead
of hardcoding the literal. No behavior change."
```

---

## Task 3 (OPTIONAL — trivial, doc-only): Clarify the standing-plan docstring (finding #8, doc part only)

**Why (marginal):** `extract_standing_plan`'s docstring says the marker is recognized "with or without a trailing colon." That is true for the *bare header* form (content on the following lines) but a reader could infer that `STANDING PLAN settle the coast` (no colon, inline content on the same line) also works — it does not (deliberately, per `test_bare_marker_requires_end_of_line_so_prose_does_not_match`). One sentence removes the ambiguity. Do **not** change the regex.

**Files:**
- Modify: `src/civ_mcp/arena/memory.py` (`extract_standing_plan` docstring, ~lines 144–147)

- [ ] **Step 1: Clarify the docstring**

In `src/civ_mcp/arena/memory.py`, in the `extract_standing_plan` docstring, after the sentence ending "...like \"(next 3 turns)\")", add:

```
    Inline content on the marker line requires a colon ("STANDING PLAN:
    settle east"); the colon-less forms are bare headers whose content must
    begin on the following line, so a no-colon marker with same-line prose is
    intentionally not treated as a plan (see
    test_bare_marker_requires_end_of_line_so_prose_does_not_match).
```

- [ ] **Step 2: Confirm nothing else changed**

Run: `python -m pytest tests/arena/test_memory.py -q`
Expected: PASS (docstring-only change)

- [ ] **Step 3: Commit**

```bash
git add src/civ_mcp/arena/memory.py
git commit -m "docs(arena): clarify standing-plan marker requires a colon for inline content"
```

---

## Self-Review

- **Spec coverage:** The "spec" here is the code-review findings. #3 → Task 1 (real fix). #6 + #4-constant → Task 2 (optional cleanup). #8-doc → Task 3 (optional). #1, #2, #5, #7, #9, #10, #8-regex, #4-semantics → intentional/test-protected, documented in the "findings NOT being fixed" table with the exact pinning test. Every finding is accounted for.
- **Type consistency:** `RESOLVED_STATUSES` is defined once in task_tracker.py and imported by analyze.py; the same name is used at all four use-sites. `maybe_build_briefing` is patched on the `coordinator` module (matching `test_exclusive_cli_briefing_prebuild_uses_explicit_context_budget`, which does the same).
- **No placeholders:** every code step shows the exact before/after text and exact pytest command.
- **Ordering:** Task 1 is the only behavior change and is independent; Tasks 2–3 are pure cleanup/doc and can be dropped without affecting Task 1.
