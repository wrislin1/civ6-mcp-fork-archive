"""Deterministic low-risk unit task tracker for arena LLM puppets.

An LLM puppet can hand off a small set of explicit, low-risk civilian
follow-through actions ("settle here", "improve this tile") via short
``TASK``/``CANCEL`` lines in its turn output. This module persists those
tasks per-run/per-player and executes them deterministically at the start
of the *next* turn, before the model is invoked -- so a settler or builder
keeps marching toward its destination across turns without needing the
model to re-decide it every time and without ever taking a risky action
(attack, purchase, diplomacy, etc.) on the model's behalf.

Deliberately independent of ``civ_mcp.arena.memory``: standing memory is a
free-text plan blob for the model to read; the task tracker is a strictly
bounded, deterministic executor that never calls the model at all.

Storage: ``<transcript_dir>/<run_id>/tasks/player_<player_id>.json``
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from pathlib import Path
from re import IGNORECASE, compile as re_compile
from typing import Any, Sequence

from civ_mcp.json_io import read_json_file, write_json_file_atomic

SCHEMA_VERSION = 1

TASK_KINDS = {"settle", "builder_improve"}

ACTION_NO_RESPONSE = "Action completed (no response)."
FOUND_CITY_RETRY_LIMIT = "found_city_failed_retry_limit"
SETTLE_NO_RESPONSE = "settle_no_response"
SETTLE_NO_RESPONSE_RETRY_LIMIT = "settle_no_response_retry_limit"
IMPROVE_NO_RESPONSE = "improve_no_response"
IMPROVE_NO_RESPONSE_RETRY_LIMIT = "improve_no_response_retry_limit"
IMPROVE_ERROR_RETRY_LIMIT = "improve_error_retry_limit"
BLOCKED_IMPROVEMENT_NOT_VALID = "blocked_improvement_not_valid"
BLOCKED_IMPROVEMENT_NOT_VALID_RETRY_LIMIT = "blocked_improvement_not_valid_retry_limit"

# Consecutive at-target failures (of any kind) a task may accumulate before it
# is marked failed. 3 leaves room for a transient blocker that persists across
# two turns (e.g. a popup blocking the async found-city op) to clear.
MAX_TASK_FAILURES = 3

# Public: memory.py's standing-plan terminator lookahead reuses these so
# "does a real TASK/CANCEL line follow" and "what actually parses as a task"
# can never disagree. Bullet-tolerant because they run on raw model summaries.
TASK_LINE_RE = re_compile(
    r"^\s*(?:[-*•]+\s+)?TASK\s+(?P<kind>settle|builder_improve)\s+"
    r"unit_id=(?P<unit_id>-?\d+)\s+"
    r"target=(?P<tx>-?\d+)\s*,\s*(?P<ty>-?\d+)"
    r"(?:\s+improvement=(?P<improvement>\S+))?\s*$",
    IGNORECASE,
)
CANCEL_LINE_RE = re_compile(
    r"^\s*(?:[-*•]+\s+)?CANCEL\s+unit_id=(?P<unit_id>-?\d+)\s*$", IGNORECASE
)


@dataclass(frozen=True)
class UnitTask:
    task_id: str
    kind: str
    unit_id: int
    target_x: int
    target_y: int
    created_turn: int
    updated_turn: int
    improvement: str = ""
    status: str = "active"
    last_result: str = ""
    failure_count: int = 0


@dataclass(frozen=True)
class TaskState:
    schema_version: int
    run_id: str
    player_id: int
    tasks: tuple[UnitTask, ...]


@dataclass(frozen=True)
class _HostileOwnerContext:
    hostile_prefixes: tuple[str, ...]
    peaceful_prefixes: tuple[str, ...]
    hostile_coords: frozenset[tuple[int, int]]
    block_unknown: bool


def _empty_state(run_id: str, player_id: int) -> TaskState:
    return TaskState(
        schema_version=SCHEMA_VERSION, run_id=run_id, player_id=player_id, tasks=()
    )


def tasks_dir(transcript_dir: str, run_id: str) -> Path:
    return Path(transcript_dir) / run_id / "tasks"


def task_path(transcript_dir: str, run_id: str, player_id: int) -> Path:
    return tasks_dir(transcript_dir, run_id) / f"player_{player_id}.json"


def load_task_state(transcript_dir: str, run_id: str, player_id: int) -> TaskState:
    """Load task state for a player. Returns an empty state if absent/malformed."""
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


def save_task_state(
    transcript_dir: str, run_id: str, player_id: int, tasks: Sequence[UnitTask]
) -> TaskState:
    """Persist only the active tasks for a player and return the saved state.

    Write is atomic best-effort: a sibling temp file is written first, then
    swapped into place via Path.replace().
    """
    active = tuple(t for t in tasks if t.status == "active")
    state = TaskState(
        schema_version=SCHEMA_VERSION, run_id=run_id, player_id=player_id, tasks=active
    )
    path = task_path(transcript_dir, run_id, player_id)
    payload = {
        "schema_version": state.schema_version,
        "run_id": state.run_id,
        "player_id": state.player_id,
        "tasks": [_task_to_dict(t) for t in state.tasks],
    }
    write_json_file_atomic(path, payload)
    return state


def _task_to_dict(task: UnitTask) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "kind": task.kind,
        "unit_id": task.unit_id,
        "target_x": task.target_x,
        "target_y": task.target_y,
        "created_turn": task.created_turn,
        "updated_turn": task.updated_turn,
        "improvement": task.improvement,
        "status": task.status,
        "last_result": task.last_result,
        "failure_count": task.failure_count,
    }


def _task_from_dict(data: dict[str, Any]) -> UnitTask:
    return UnitTask(
        task_id=data["task_id"],
        kind=data["kind"],
        unit_id=data["unit_id"],
        target_x=data["target_x"],
        target_y=data["target_y"],
        created_turn=data["created_turn"],
        updated_turn=data["updated_turn"],
        improvement=data.get("improvement", ""),
        status=data.get("status", "active"),
        last_result=data.get("last_result", ""),
        failure_count=data.get("failure_count", 0),
    )


def parse_task_lines(plan_text: str, turn: int) -> list[UnitTask]:
    """Parse explicit ``TASK``/``CANCEL`` lines out of a puppet's plan text.

    Invalid lines (missing fields, unknown kind, missing ``improvement`` for
    ``builder_improve``) are silently ignored rather than raising.

    ``CANCEL`` lines produce a placeholder ``UnitTask`` with ``kind="cancel"``
    and ``status="cancelled"`` carrying only the ``unit_id`` to cancel --
    ``merge_tasks`` resolves it against whichever existing task (of any kind)
    currently owns that ``unit_id``.
    """
    parsed: list[UnitTask] = []
    for line in plan_text.splitlines():
        match = TASK_LINE_RE.match(line)
        if match:
            kind = match.group("kind").lower()
            unit_id = int(match.group("unit_id"))
            improvement = match.group("improvement") or ""
            if kind == "builder_improve" and not improvement:
                continue
            parsed.append(
                UnitTask(
                    task_id=f"{kind}:{unit_id}",
                    kind=kind,
                    unit_id=unit_id,
                    target_x=int(match.group("tx")),
                    target_y=int(match.group("ty")),
                    created_turn=turn,
                    updated_turn=turn,
                    improvement=improvement,
                    status="active",
                    last_result="",
                )
            )
            continue

        match = CANCEL_LINE_RE.match(line)
        if match:
            unit_id = int(match.group("unit_id"))
            parsed.append(
                UnitTask(
                    task_id=f"cancel:{unit_id}",
                    kind="cancel",
                    unit_id=unit_id,
                    target_x=0,
                    target_y=0,
                    created_turn=turn,
                    updated_turn=turn,
                    improvement="",
                    status="cancelled",
                    last_result="",
                )
            )
    return parsed


def _task_unit_ids_equivalent(left: int, right: int) -> bool:
    if left == right:
        return True
    # A bare unit index (< 65536) aliases its composite owner*65536+index form.
    # Two distinct composite ids are different units even when congruent mod
    # 65536 -- their high bits encode different owners.
    if left >= 65536 and right >= 65536:
        return False
    return left % 65536 == right % 65536


def merge_tasks(
    existing: Sequence[UnitTask], updates: Sequence[UnitTask], max_tasks: int
) -> tuple[UnitTask, ...]:
    """Upsert parsed ``updates`` onto ``existing`` tasks.

    - ``TASK`` updates (status != "cancelled") replace any existing task with
      the same ``task_id``. A restatement (same kind/unit/target/improvement)
      carries over the existing task's failure strikes instead of resetting
      them, and cannot resurrect a task that already hit its failure budget --
      only a changed target starts fresh.
    - ``CANCEL`` updates remove whichever existing task currently owns that
      ``unit_id`` (regardless of kind).
    - Tasks absent from ``updates`` persist unchanged.
    - Only ``active`` tasks appear in the output; the newest ``max_tasks`` of
      them (by ``updated_turn``) are kept. A non-active task (completed/lost/
      cancelled) can never occupy a cap slot and evict an active one.
    """
    merged: dict[str, UnitTask] = {task.task_id: task for task in existing}

    for update in updates:
        if update.status == "cancelled":
            for task_id in [
                tid
                for tid, task in merged.items()
                if _task_unit_ids_equivalent(task.unit_id, update.unit_id)
            ]:
                del merged[task_id]
            continue
        restated: UnitTask | None = None
        for task_id in [
            tid
            for tid, task in merged.items()
            if task.kind == update.kind
            and _task_unit_ids_equivalent(task.unit_id, update.unit_id)
        ]:
            task = merged.pop(task_id)
            if (task.target_x, task.target_y, task.improvement) == (
                update.target_x,
                update.target_y,
                update.improvement,
            ):
                restated = task
        if restated is not None:
            if restated.status == "failed":
                # A verbatim restatement can't revive a task that exhausted its
                # failure budget -- the model must change the target to retry.
                continue
            update = replace(
                update,
                created_turn=restated.created_turn,
                last_result=restated.last_result,
                failure_count=restated.failure_count,
            )
        merged[update.task_id] = update

    # Cap ACTIVE tasks only: a freshly completed/lost task (which
    # run_pre_model_tasks returns in `existing`, carrying its original
    # updated_turn) must never occupy a cap slot and evict an in-progress
    # active task. Non-active tasks are dropped from the output entirely --
    # save_task_state persists active-only anyway, and the brief specifies
    # keeping active tasks (cancelled/completed/lost do not survive merge).
    ordered = sorted(merged.values(), key=lambda task: task.updated_turn)
    active = [task for task in ordered if task.status == "active"]
    if max_tasks > 0 and len(active) > max_tasks:
        active = active[-max_tasks:]
    return tuple(active)


def _result_dict(task: UnitTask, *, status: str, action: str, result: str) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "kind": task.kind,
        "unit_id": task.unit_id,
        "target": [task.target_x, task.target_y],
        "status": status,
        "action": action,
        "result": result,
    }


def _sorted_prefixes(names: set[str]) -> tuple[str, ...]:
    return tuple(sorted(names, key=len, reverse=True))


async def _call_context_source(gs: Any, method_name: str) -> Any:
    method = getattr(gs, method_name)
    return await method()


async def _hostile_owner_context(gs: Any) -> _HostileOwnerContext:
    hostile = {"Barbarian"}
    peaceful: set[str] = set()
    hostile_coords: set[tuple[int, int]] = set()
    block_unknown = False

    civ_result, threat_result = await asyncio.gather(
        _call_context_source(gs, "get_diplomacy"),
        _call_context_source(gs, "get_threat_scan"),
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


def _label_matches_owner(label: str, owner: str) -> bool:
    return label == owner or label.startswith(owner + " ")


def _tile_has_hostile_unit(tile: Any, owner_context: _HostileOwnerContext) -> bool:
    labels = [str(label).strip() for label in (tile.units or []) if str(label).strip()]
    if labels and (tile.x, tile.y) in owner_context.hostile_coords and any(
        not any(
            _label_matches_owner(label_text, owner)
            for owner in owner_context.peaceful_prefixes
        )
        for label_text in labels
    ):
        return True

    for label_text in labels:
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


async def _visible_hostile_nearby(
    gs: Any,
    cur_x: int,
    cur_y: int,
    target_x: int,
    target_y: int,
    owner_context: _HostileOwnerContext,
) -> bool:
    current_tiles, target_tiles = await asyncio.gather(
        gs.get_map_area(cur_x, cur_y, 2),
        gs.get_map_area(target_x, target_y, 2),
    )
    return any(_tile_has_hostile_unit(tile, owner_context) for tile in current_tiles) or any(
        _tile_has_hostile_unit(tile, owner_context) for tile in target_tiles
    )


def _touch_task(task: UnitTask, turn: int | None) -> UnitTask:
    if turn is None:
        return task
    return replace(task, updated_turn=turn)


def _fail_or_retry(
    task: UnitTask,
    *,
    action: str,
    result_str: str,
    limit_result: str,
    turn: int | None,
) -> tuple[UnitTask, dict[str, Any]]:
    """Record a failed at-target attempt, escalating at the failure budget.

    Consecutive at-target failures of any kind count against MAX_TASK_FAILURES
    on the task itself (so the count survives merge_tasks restatements and does
    not depend on error strings repeating verbatim); the attempt that reaches
    the budget marks the task failed with ``limit_result``.
    """
    failures = task.failure_count + 1
    if failures >= MAX_TASK_FAILURES:
        new_task = replace(
            task, status="failed", last_result=limit_result, failure_count=failures
        )
        return new_task, _result_dict(
            task, status="failed", action=action, result=limit_result
        )
    new_task = _touch_task(
        replace(task, last_result=result_str, failure_count=failures), turn
    )
    return new_task, _result_dict(
        task, status="active", action=action, result=result_str
    )


def _unit_lookup_maps(units: Sequence[Any]) -> tuple[dict[int, Any], dict[int, Any]]:
    by_id = {unit.unit_id: unit for unit in units}
    by_index: dict[int, Any] = {}
    for unit in units:
        by_index.setdefault(unit.unit_index, unit)
        by_index.setdefault(unit.unit_id % 65536, unit)
    return by_id, by_index


def _resolve_task_unit(
    task: UnitTask, units_by_id: dict[int, Any], units_by_index: dict[int, Any]
) -> Any | None:
    return units_by_id.get(task.unit_id) or units_by_index.get(task.unit_id)


async def _run_single_task(
    gs: Any,
    task: UnitTask,
    units_by_id: dict[int, Any],
    units_by_index: dict[int, Any],
    owner_context: _HostileOwnerContext,
    turn: int | None,
) -> tuple[UnitTask, dict[str, Any]]:
    unit = _resolve_task_unit(task, units_by_id, units_by_index)
    if unit is None:
        new_task = replace(task, status="lost", last_result="unit_missing")
        return new_task, _result_dict(
            task, status="lost", action="skip", result="unit_missing"
        )

    if unit.moves_remaining <= 0:
        new_task = _touch_task(replace(task, last_result="skipped_no_moves"), turn)
        return new_task, _result_dict(
            task, status="active", action="skip", result="skipped_no_moves"
        )

    at_target = (unit.x, unit.y) == (task.target_x, task.target_y)

    if task.kind == "settle":
        if at_target:
            result_str = await gs.found_city(unit.unit_index)
            if result_str.startswith("Error:"):
                return _fail_or_retry(
                    task,
                    action="found_city",
                    result_str=result_str,
                    limit_result=FOUND_CITY_RETRY_LIMIT,
                    turn=turn,
                )
            if result_str == ACTION_NO_RESPONSE:
                # No output lines from the tuner is ambiguous, not success: a
                # city may or may not exist. Retry (a founded city completes
                # via unit_missing/lost next turn) rather than report complete.
                return _fail_or_retry(
                    task,
                    action="found_city",
                    result_str=SETTLE_NO_RESPONSE,
                    limit_result=SETTLE_NO_RESPONSE_RETRY_LIMIT,
                    turn=turn,
                )
            new_task = replace(task, status="complete", last_result=result_str)
            return new_task, _result_dict(
                task, status="complete", action="found_city", result=result_str
            )

        if await _visible_hostile_nearby(
            gs, unit.x, unit.y, task.target_x, task.target_y, owner_context
        ):
            new_task = _touch_task(
                replace(task, last_result="blocked_visible_hostile"), turn
            )
            return new_task, _result_dict(
                task, status="active", action="block", result="blocked_visible_hostile"
            )

        result_str = await gs.move_unit(unit.unit_index, task.target_x, task.target_y)
        new_task = _touch_task(replace(task, last_result=result_str), turn)
        return new_task, _result_dict(task, status="active", action="move", result=result_str)

    # task.kind == "builder_improve"
    if at_target:
        if task.improvement in unit.valid_improvements:
            result_str = await gs.improve_tile(unit.unit_index, task.improvement)
            if result_str.startswith("Error:"):
                return _fail_or_retry(
                    task,
                    action="improve",
                    result_str=result_str,
                    limit_result=IMPROVE_ERROR_RETRY_LIMIT,
                    turn=turn,
                )
            if result_str == ACTION_NO_RESPONSE:
                return _fail_or_retry(
                    task,
                    action="improve",
                    result_str=IMPROVE_NO_RESPONSE,
                    limit_result=IMPROVE_NO_RESPONSE_RETRY_LIMIT,
                    turn=turn,
                )
            new_task = replace(task, status="complete", last_result=result_str)
            return new_task, _result_dict(
                task, status="complete", action="improve", result=result_str
            )

        return _fail_or_retry(
            task,
            action="block",
            result_str=BLOCKED_IMPROVEMENT_NOT_VALID,
            limit_result=BLOCKED_IMPROVEMENT_NOT_VALID_RETRY_LIMIT,
            turn=turn,
        )

    if await _visible_hostile_nearby(
        gs, unit.x, unit.y, task.target_x, task.target_y, owner_context
    ):
        new_task = _touch_task(replace(task, last_result="blocked_visible_hostile"), turn)
        return new_task, _result_dict(
            task, status="active", action="block", result="blocked_visible_hostile"
        )

    result_str = await gs.move_unit(unit.unit_index, task.target_x, task.target_y)
    new_task = _touch_task(replace(task, last_result=result_str), turn)
    return new_task, _result_dict(task, status="active", action="move", result=result_str)


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


async def run_pre_model_tasks(
    gs: Any, tasks: Sequence[UnitTask], *, turn: int | None = None
) -> tuple[tuple[UnitTask, ...], list[dict[str, Any]]]:
    """Execute active, low-risk tasks before the model turn.

    Only ``settle`` and ``builder_improve`` tasks with ``status == "active"``
    are ever executed -- this function never attacks, fortifies, escorts,
    purchases, chops, recruits, votes, trades, or makes diplomacy choices.
    A per-task exception is caught and recorded as ``error:<repr>`` without
    aborting the remaining tasks.
    """
    executable = [
        task for task in tasks if task.status == "active" and task.kind in TASK_KINDS
    ]
    if not executable:
        return tuple(tasks), []

    try:
        units = await gs.get_units()
    except Exception:  # pragma: no cover - defensive, mirrors per-task guard
        units = []
    units_by_id, units_by_index = _unit_lookup_maps(units)
    if any(
        _task_needs_hostile_context(
            task, _resolve_task_unit(task, units_by_id, units_by_index)
        )
        for task in executable
    ):
        owner_context = await _hostile_owner_context(gs)
    else:
        owner_context = _empty_hostile_owner_context()

    updated: list[UnitTask] = []
    results: list[dict[str, Any]] = []

    for task in tasks:
        if task.status != "active" or task.kind not in TASK_KINDS:
            updated.append(task)
            continue

        try:
            new_task, result = await _run_single_task(
                gs, task, units_by_id, units_by_index, owner_context, turn
            )
        except Exception as exc:
            error_msg = f"error:{exc!r}"
            new_task = replace(task, last_result=error_msg)
            result = _result_dict(task, status="active", action="error", result=error_msg)

        updated.append(new_task)
        results.append(result)

    return tuple(updated), results


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

    if not active and not limited_results:
        return ""

    lines = ["== DETERMINISTIC TASK TRACKER =="]

    if active:
        lines.append("ACTIVE TASKS:")
        for task in active:
            detail = f"- {task.task_id} unit_id={task.unit_id} target=({task.target_x},{task.target_y})"
            if task.improvement:
                detail += f" improvement={task.improvement}"
            if task.last_result:
                detail += f" last_result={task.last_result}"
            lines.append(detail)

    if limited_results:
        lines.append("RESULTS THIS TURN:")
        for result in limited_results:
            lines.append(
                f"- {result['task_id']} status={result['status']} "
                f"action={result['action']} result={result['result']}"
            )

    return "\n".join(lines)
