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

import json
from dataclasses import dataclass, replace
from pathlib import Path
from re import IGNORECASE, compile as re_compile
from typing import Any, Sequence

SCHEMA_VERSION = 1

TASK_KINDS = {"settle", "builder_improve"}

_TASK_LINE_RE = re_compile(
    r"^\s*TASK\s+(?P<kind>settle|builder_improve)\s+"
    r"unit_id=(?P<unit_id>-?\d+)\s+"
    r"target=(?P<tx>-?\d+)\s*,\s*(?P<ty>-?\d+)"
    r"(?:\s+improvement=(?P<improvement>\S+))?\s*$",
    IGNORECASE,
)
_CANCEL_LINE_RE = re_compile(r"^\s*CANCEL\s+unit_id=(?P<unit_id>-?\d+)\s*$", IGNORECASE)


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


@dataclass(frozen=True)
class TaskState:
    schema_version: int
    run_id: str
    player_id: int
    tasks: tuple[UnitTask, ...]


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
    path = task_path(transcript_dir, run_id, player_id)
    try:
        data = json.loads(path.read_text())
        tasks = tuple(_task_from_dict(t) for t in data["tasks"])
        return TaskState(
            schema_version=data["schema_version"],
            run_id=data["run_id"],
            player_id=data["player_id"],
            tasks=tasks,
        )
    except (OSError, ValueError, KeyError, TypeError):
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
        match = _TASK_LINE_RE.match(line)
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

        match = _CANCEL_LINE_RE.match(line)
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


def merge_tasks(
    existing: Sequence[UnitTask], updates: Sequence[UnitTask], max_tasks: int
) -> tuple[UnitTask, ...]:
    """Upsert parsed ``updates`` onto ``existing`` tasks.

    - ``TASK`` updates (status != "cancelled") replace any existing task with
      the same ``task_id``.
    - ``CANCEL`` updates remove whichever existing task currently owns that
      ``unit_id`` (regardless of kind).
    - Tasks absent from ``updates`` persist unchanged.
    - The newest ``max_tasks`` active tasks (by ``updated_turn``) are kept.
    """
    merged: dict[str, UnitTask] = {task.task_id: task for task in existing}

    for update in updates:
        if update.status == "cancelled":
            match_id = next(
                (tid for tid, task in merged.items() if task.unit_id == update.unit_id),
                None,
            )
            if match_id is not None:
                del merged[match_id]
            continue
        merged[update.task_id] = update

    ordered = sorted(merged.values(), key=lambda task: task.updated_turn)
    if max_tasks >= 0 and len(ordered) > max_tasks:
        ordered = ordered[-max_tasks:]
    return tuple(ordered)


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


async def _visible_hostile_nearby(
    gs: Any, cur_x: int, cur_y: int, target_x: int, target_y: int
) -> bool:
    current_tiles = await gs.get_map_area(cur_x, cur_y, 2)
    target_tiles = await gs.get_map_area(target_x, target_y, 2)
    return any(tile.units for tile in current_tiles) or any(
        tile.units for tile in target_tiles
    )


async def _run_single_task(
    gs: Any, task: UnitTask, units_by_id: dict[int, Any]
) -> tuple[UnitTask, dict[str, Any]]:
    unit = units_by_id.get(task.unit_id)
    if unit is None:
        new_task = replace(task, status="lost", last_result="unit_missing")
        return new_task, _result_dict(
            task, status="lost", action="skip", result="unit_missing"
        )

    if unit.moves_remaining <= 0:
        new_task = replace(task, last_result="skipped_no_moves")
        return new_task, _result_dict(
            task, status="active", action="skip", result="skipped_no_moves"
        )

    at_target = (unit.x, unit.y) == (task.target_x, task.target_y)

    if task.kind == "settle":
        if at_target:
            result_str = await gs.found_city(unit.unit_index)
            if result_str.startswith("Error:"):
                new_task = replace(task, last_result=result_str)
                return new_task, _result_dict(
                    task, status="active", action="found_city", result=result_str
                )
            new_task = replace(task, status="complete", last_result=result_str)
            return new_task, _result_dict(
                task, status="complete", action="found_city", result=result_str
            )

        if await _visible_hostile_nearby(gs, unit.x, unit.y, task.target_x, task.target_y):
            new_task = replace(task, last_result="blocked_visible_hostile")
            return new_task, _result_dict(
                task, status="active", action="block", result="blocked_visible_hostile"
            )

        result_str = await gs.move_unit(unit.unit_index, task.target_x, task.target_y)
        new_task = replace(task, last_result=result_str)
        return new_task, _result_dict(task, status="active", action="move", result=result_str)

    # task.kind == "builder_improve"
    if at_target:
        if task.improvement in unit.valid_improvements:
            result_str = await gs.improve_tile(unit.unit_index, task.improvement)
            if result_str.startswith("Error:"):
                new_task = replace(task, last_result=result_str)
                return new_task, _result_dict(
                    task, status="active", action="improve", result=result_str
                )
            new_task = replace(task, status="complete", last_result=result_str)
            return new_task, _result_dict(
                task, status="complete", action="improve", result=result_str
            )

        new_task = replace(task, last_result="blocked_improvement_not_valid")
        return new_task, _result_dict(
            task,
            status="active",
            action="block",
            result="blocked_improvement_not_valid",
        )

    if await _visible_hostile_nearby(gs, unit.x, unit.y, task.target_x, task.target_y):
        new_task = replace(task, last_result="blocked_visible_hostile")
        return new_task, _result_dict(
            task, status="active", action="block", result="blocked_visible_hostile"
        )

    result_str = await gs.move_unit(unit.unit_index, task.target_x, task.target_y)
    new_task = replace(task, last_result=result_str)
    return new_task, _result_dict(task, status="active", action="move", result=result_str)


async def run_pre_model_tasks(
    gs: Any, tasks: Sequence[UnitTask]
) -> tuple[tuple[UnitTask, ...], list[dict[str, Any]]]:
    """Execute active, low-risk tasks before the model turn.

    Only ``settle`` and ``builder_improve`` tasks with ``status == "active"``
    are ever executed -- this function never attacks, fortifies, escorts,
    purchases, chops, recruits, votes, trades, or makes diplomacy choices.
    A per-task exception is caught and recorded as ``error:<repr>`` without
    aborting the remaining tasks.
    """
    try:
        units = await gs.get_units()
    except Exception:  # pragma: no cover - defensive, mirrors per-task guard
        units = []
    units_by_id = {unit.unit_id: unit for unit in units}

    updated: list[UnitTask] = []
    results: list[dict[str, Any]] = []

    for task in tasks:
        if task.status != "active" or task.kind not in TASK_KINDS:
            updated.append(task)
            continue

        try:
            new_task, result = await _run_single_task(gs, task, units_by_id)
        except Exception as exc:
            error_msg = f"error:{exc!r}"
            new_task = replace(task, last_result=error_msg)
            result = _result_dict(task, status="active", action="error", result=error_msg)

        updated.append(new_task)
        results.append(result)

    return tuple(updated), results


def format_task_block(tasks: Sequence[UnitTask], results: Sequence[dict[str, Any]]) -> str:
    """Render active tasks and this turn's results as a prompt-ready block.

    Returns "" when there are no active tasks and no results.
    """
    active = [task for task in tasks if task.status == "active"][:8]
    limited_results = list(results)[:8]

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
