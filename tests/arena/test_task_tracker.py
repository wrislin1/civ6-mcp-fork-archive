import asyncio
from types import SimpleNamespace

import pytest

from civ_mcp import lua as lq
from civ_mcp.arena.task_tracker import (
    TaskState,
    UnitTask,
    format_task_block,
    load_task_state,
    merge_tasks,
    parse_task_lines,
    run_pre_model_tasks,
    save_task_state,
    task_path,
)


def _unit(unit_id, unit_index, x, y, moves_remaining=2.0, valid_improvements=None):
    return lq.UnitInfo(
        unit_id=unit_id,
        unit_index=unit_index,
        name="Settler",
        unit_type="UNIT_SETTLER",
        x=x,
        y=y,
        moves_remaining=moves_remaining,
        max_moves=2.0,
        health=100,
        max_health=100,
        valid_improvements=valid_improvements or [],
    )


def _tile(x, y, units=None):
    return lq.TileInfo(
        x=x,
        y=y,
        terrain="TERRAIN_GRASS",
        feature=None,
        resource=None,
        is_hills=False,
        is_river=False,
        is_coastal=False,
        improvement=None,
        owner_id=-1,
        units=units,
    )


def _task(task_id="settle:65537", kind="settle", unit_id=65537, target_x=18, target_y=24,
          created_turn=1, updated_turn=1, improvement="", status="active", last_result="",
          failure_count=0):
    return UnitTask(
        task_id=task_id,
        kind=kind,
        unit_id=unit_id,
        target_x=target_x,
        target_y=target_y,
        created_turn=created_turn,
        updated_turn=updated_turn,
        improvement=improvement,
        status=status,
        last_result=last_result,
        failure_count=failure_count,
    )


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

    async def get_map_area(self, x, y, radius=2):
        self.map_area_calls.append((x, y, radius))
        return self.map_tiles.get((x, y), [])

    async def found_city(self, unit_index):
        self.found_city_calls.append(unit_index)
        return self.found_city_result

    async def move_unit(self, unit_index, target_x, target_y):
        self.move_unit_calls.append((unit_index, target_x, target_y))
        return self.move_unit_result

    async def improve_tile(self, unit_index, improvement):
        self.improve_tile_calls.append((unit_index, improvement))
        return self.improve_tile_result


# ---------------------------------------------------------------------------
# Save/load round trip and malformed JSON
# ---------------------------------------------------------------------------


def test_load_task_state_missing_file_returns_empty_state(tmp_path):
    state = load_task_state(str(tmp_path), "run1", 0)
    assert state == TaskState(schema_version=1, run_id="run1", player_id=0, tasks=())


def test_load_task_state_malformed_json_returns_empty_state(tmp_path):
    path = task_path(str(tmp_path), "run1", 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json")

    state = load_task_state(str(tmp_path), "run1", 0)
    assert state == TaskState(schema_version=1, run_id="run1", player_id=0, tasks=())


def test_load_task_state_malformed_structure_returns_empty_state(tmp_path):
    path = task_path(str(tmp_path), "run1", 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('["unexpected", "list", "shape"]')

    state = load_task_state(str(tmp_path), "run1", 0)
    assert state == TaskState(schema_version=1, run_id="run1", player_id=0, tasks=())


def test_save_then_load_round_trip(tmp_path):
    task = _task()
    saved = save_task_state(str(tmp_path), "run1", 2, [task])

    loaded = load_task_state(str(tmp_path), "run1", 2)

    assert loaded == saved
    assert loaded.tasks == (task,)


def test_save_task_state_writes_expected_path(tmp_path):
    save_task_state(str(tmp_path), "run1", 3, [_task()])

    expected = tmp_path / "run1" / "tasks" / "player_3.json"
    assert expected.exists()


def test_save_task_state_persists_only_active_tasks(tmp_path):
    active = _task(task_id="settle:1", unit_id=1, status="active")
    lost = _task(task_id="settle:2", unit_id=2, status="lost")

    saved = save_task_state(str(tmp_path), "run1", 0, [active, lost])

    assert saved.tasks == (active,)
    loaded = load_task_state(str(tmp_path), "run1", 0)
    assert loaded.tasks == (active,)


# ---------------------------------------------------------------------------
# parse_task_lines
# ---------------------------------------------------------------------------


def test_parse_valid_settle_line():
    tasks = parse_task_lines("TASK settle unit_id=123 target=18,24", turn=5)

    assert tasks == [
        UnitTask(
            task_id="settle:123",
            kind="settle",
            unit_id=123,
            target_x=18,
            target_y=24,
            created_turn=5,
            updated_turn=5,
        )
    ]


def test_parse_valid_builder_improve_line():
    tasks = parse_task_lines(
        "TASK builder_improve unit_id=456 target=12,19 improvement=IMPROVEMENT_FARM", turn=7
    )

    assert tasks == [
        UnitTask(
            task_id="builder_improve:456",
            kind="builder_improve",
            unit_id=456,
            target_x=12,
            target_y=19,
            created_turn=7,
            updated_turn=7,
            improvement="IMPROVEMENT_FARM",
        )
    ]


def test_parse_valid_cancel_line():
    tasks = parse_task_lines("CANCEL unit_id=123", turn=9)

    assert tasks == [
        UnitTask(
            task_id="cancel:123",
            kind="cancel",
            unit_id=123,
            target_x=0,
            target_y=0,
            created_turn=9,
            updated_turn=9,
            status="cancelled",
        )
    ]


@pytest.mark.parametrize(
    "line",
    [
        "TASK settle unit_id=123",  # missing target
        "TASK settle target=18,24",  # missing unit_id
        "TASK builder_improve unit_id=456 target=12,19",  # missing required improvement
        "TASK attack unit_id=1 target=1,1",  # unsupported kind
        "CANCEL",  # missing unit_id
        "just some narrative plan text",
        "",
    ],
)
def test_invalid_lines_are_ignored(line):
    assert parse_task_lines(line, turn=1) == []


def test_parse_task_lines_accepts_bulleted_lines():
    """Raw summaries carry markdown bullets; task parsing runs on the raw
    summary (not the bullet-stripped captured plan), so the parser itself
    must tolerate a leading bullet."""
    tasks = parse_task_lines(
        "- TASK settle unit_id=123 target=18,24\n• CANCEL unit_id=456", turn=5
    )

    assert [(t.kind, t.unit_id) for t in tasks] == [("settle", 123), ("cancel", 456)]


def test_parse_task_lines_ignores_invalid_and_keeps_valid_lines():
    plan = "\n".join(
        [
            "Some narration.",
            "TASK settle unit_id=123 target=18,24",
            "TASK settle unit_id=999",  # invalid, missing target
            "CANCEL unit_id=456",
        ]
    )

    tasks = parse_task_lines(plan, turn=2)

    assert [t.task_id for t in tasks] == ["settle:123", "cancel:456"]


# ---------------------------------------------------------------------------
# merge_tasks
# ---------------------------------------------------------------------------


def test_merge_keeps_existing_active_tasks_when_no_updates():
    existing = (_task(task_id="settle:1", unit_id=1),)

    merged = merge_tasks(existing, [], max_tasks=10)

    assert merged == existing


def test_merge_replaces_by_task_id():
    existing = (_task(task_id="settle:1", unit_id=1, target_x=1, target_y=1, updated_turn=1),)
    update = _task(task_id="settle:1", unit_id=1, target_x=9, target_y=9, updated_turn=2)

    merged = merge_tasks(existing, [update], max_tasks=10)

    assert merged == (update,)


def test_merge_tasks_replaces_existing_composite_task_with_unit_index_alias():
    existing = _task(
        task_id="settle:65537",
        unit_id=65537,
        target_x=10,
        target_y=10,
        created_turn=2,
        updated_turn=2,
    )
    update = _task(
        task_id="settle:1",
        unit_id=1,
        target_x=12,
        target_y=13,
        created_turn=9,
        updated_turn=9,
    )

    merged = merge_tasks([existing], [update], max_tasks=8)

    assert merged == (update,)


def test_merge_cancel_removes_matching_unit_id_task():
    existing = (_task(task_id="settle:1", unit_id=1),)
    cancel = _task(task_id="cancel:1", kind="cancel", unit_id=1, status="cancelled")

    merged = merge_tasks(existing, [cancel], max_tasks=10)

    assert merged == ()


def test_merge_tasks_cancel_matches_unit_index_alias():
    existing = _task(
        task_id="settle:65537",
        unit_id=65537,
        target_x=10,
        target_y=10,
        created_turn=2,
        updated_turn=2,
    )
    cancel = UnitTask(
        task_id="cancel:1",
        kind="cancel",
        unit_id=1,
        target_x=0,
        target_y=0,
        status="cancelled",
        created_turn=9,
        updated_turn=9,
        last_result="cancelled",
    )

    assert merge_tasks([existing], [cancel], max_tasks=8) == ()


def test_merge_cancel_does_not_conflate_distinct_composite_ids():
    # 65537 and 131073 are congruent mod 65536 but carry different owner high
    # bits -- they are different units, not alias forms of one unit.
    existing = (_task(task_id="settle:65537", unit_id=65537),)
    cancel = UnitTask(
        task_id="cancel:131073",
        kind="cancel",
        unit_id=131073,
        target_x=0,
        target_y=0,
        status="cancelled",
        created_turn=9,
        updated_turn=9,
        last_result="cancelled",
    )

    assert merge_tasks(existing, [cancel], max_tasks=8) == existing


def test_merge_restatement_does_not_conflate_distinct_composite_ids():
    existing = (_task(task_id="settle:65537", unit_id=65537, target_x=10, target_y=10),)
    updates = parse_task_lines("TASK settle unit_id=131073 target=12,13", 6)

    merged = merge_tasks(existing, updates, max_tasks=8)

    assert {t.task_id for t in merged} == {"settle:65537", "settle:131073"}


def test_merge_respects_max_tasks_keeping_newest():
    existing = (
        _task(task_id="settle:1", unit_id=1, updated_turn=1),
        _task(task_id="settle:2", unit_id=2, updated_turn=2),
        _task(task_id="settle:3", unit_id=3, updated_turn=3),
    )

    merged = merge_tasks(existing, [], max_tasks=2)

    assert [t.task_id for t in merged] == ["settle:2", "settle:3"]


def test_merge_cap_never_evicts_active_task_for_completed_one():
    # run_pre_model_tasks returns freshly-completed tasks in `existing`,
    # carrying their original updated_turn. A completed task with the newest
    # updated_turn must NOT occupy a cap slot and drop an in-progress active
    # task -- the cap applies to active tasks only.
    existing = (
        _task(task_id="settle:1", unit_id=1, updated_turn=1, status="active"),  # A, oldest
        _task(task_id="settle:2", unit_id=2, updated_turn=2, status="active"),  # B
        _task(task_id="settle:3", unit_id=3, updated_turn=3, status="complete"),  # C, newest
    )

    merged = merge_tasks(existing, [], max_tasks=2)

    # Both active tasks survive; the completed one is dropped, not [B, C].
    assert {t.task_id for t in merged} == {"settle:1", "settle:2"}
    assert all(t.status == "active" for t in merged)


# ---------------------------------------------------------------------------
# run_pre_model_tasks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settle_resolves_unit_index_from_unit_id_and_calls_found_city_at_target():
    unit = _unit(unit_id=65537, unit_index=1, x=18, y=24)
    gs = FakeGS(units=[unit])
    task = _task(task_id="settle:65537", unit_id=65537, target_x=18, target_y=24)

    updated, results = await run_pre_model_tasks(gs, [task])

    assert gs.found_city_calls == [1]  # unit_index, not raw unit_id
    assert updated[0].status == "complete"
    assert results == [
        {
            "task_id": "settle:65537",
            "kind": "settle",
            "unit_id": 65537,
            "target": [18, 24],
            "status": "complete",
            "action": "found_city",
            "result": "FOUNDED|18,24",
        }
    ]


@pytest.mark.asyncio
async def test_settle_moves_toward_target_when_not_there_yet():
    unit = _unit(unit_id=65537, unit_index=1, x=1, y=1)
    gs = FakeGS(units=[unit])
    task = _task(task_id="settle:65537", unit_id=65537, target_x=18, target_y=24)

    updated, results = await run_pre_model_tasks(gs, [task])

    assert gs.move_unit_calls == [(1, 18, 24)]
    assert gs.found_city_calls == []
    assert updated[0].status == "active"
    assert results[0]["action"] == "move"


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


@pytest.mark.asyncio
async def test_builder_improve_calls_improve_tile_only_when_improvement_valid():
    unit = _unit(
        unit_id=65538,
        unit_index=2,
        x=12,
        y=19,
        valid_improvements=["IMPROVEMENT_FARM"],
    )
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

    assert gs.improve_tile_calls == [(2, "IMPROVEMENT_FARM")]
    assert updated[0].status == "complete"
    assert results[0]["action"] == "improve"


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
        failure_count=2,
    )

    updated, results = await run_pre_model_tasks(gs, [task])

    assert gs.improve_tile_calls == []
    assert updated[0].status == "failed"
    assert updated[0].last_result == "blocked_improvement_not_valid_retry_limit"
    assert results[0]["status"] == "failed"
    assert results[0]["action"] == "block"
    assert results[0]["result"] == "blocked_improvement_not_valid_retry_limit"


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
        failure_count=2,
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
        failure_count=2,
    )

    updated, results = await run_pre_model_tasks(gs, [task])

    assert updated[0].status == "failed"
    assert updated[0].last_result == "improve_no_response_retry_limit"
    assert results[0]["status"] == "failed"
    assert results[0]["result"] == "improve_no_response_retry_limit"


def test_merge_restated_task_preserves_failure_state():
    """Restating an identical TASK line must not wipe accumulated failure strikes."""
    existing = [
        _task(
            created_turn=3,
            updated_turn=5,
            last_result="Error: FOUND_FAILED",
            failure_count=1,
        )
    ]
    updates = parse_task_lines("TASK settle unit_id=65537 target=18,24", 6)

    merged = merge_tasks(existing, updates, max_tasks=8)

    assert len(merged) == 1
    assert merged[0].failure_count == 1
    assert merged[0].last_result == "Error: FOUND_FAILED"
    assert merged[0].created_turn == 3
    assert merged[0].updated_turn == 6


def test_merge_restated_task_with_new_target_resets_failure_state():
    existing = [
        _task(last_result="Error: FOUND_FAILED", failure_count=2)
    ]
    updates = parse_task_lines("TASK settle unit_id=65537 target=20,26", 6)

    merged = merge_tasks(existing, updates, max_tasks=8)

    assert len(merged) == 1
    assert merged[0].target_x == 20
    assert merged[0].failure_count == 0
    assert merged[0].last_result == ""


def test_merge_restatement_does_not_resurrect_completed_task():
    """A task that just resolved complete and is verbatim re-emitted in the same
    summary is the model echoing its plan, not a new directive -- it must not
    re-enter the active set (and re-execute on a consumed unit next turn)."""
    existing = [
        _task(status="complete", last_result="Founded city at (18, 24)")
    ]
    updates = parse_task_lines("TASK settle unit_id=65537 target=18,24", 6)

    merged = merge_tasks(existing, updates, max_tasks=8)

    assert merged == ()


def test_merge_restatement_does_not_resurrect_lost_task():
    existing = [
        _task(status="lost", last_result="unit_missing")
    ]
    updates = parse_task_lines("TASK settle unit_id=65537 target=18,24", 6)

    merged = merge_tasks(existing, updates, max_tasks=8)

    assert merged == ()


def test_merge_restated_task_keeps_original_identity():
    """A verbatim restatement keeps the existing task object (all execution
    state by construction), only refreshing updated_turn -- even when the model
    restates with the bare-index alias of the stored composite id."""
    existing = [
        _task(
            task_id="settle:65537",
            unit_id=65537,
            created_turn=3,
            updated_turn=5,
            last_result="Error: FOUND_FAILED",
            failure_count=1,
        )
    ]
    updates = parse_task_lines("TASK settle unit_id=1 target=18,24", 6)

    merged = merge_tasks(existing, updates, max_tasks=8)

    assert len(merged) == 1
    assert merged[0].task_id == "settle:65537"
    assert merged[0].unit_id == 65537
    assert merged[0].failure_count == 1
    assert merged[0].created_turn == 3
    assert merged[0].updated_turn == 6


def test_merge_restatement_does_not_resurrect_failed_task():
    """A verbatim restatement of a task that just hit its failure budget must not
    revive it — only a changed target (a genuinely new instruction) starts over."""
    existing = [
        _task(
            status="failed",
            last_result="found_city_failed_retry_limit",
            failure_count=3,
        )
    ]
    updates = parse_task_lines("TASK settle unit_id=65537 target=18,24", 6)

    merged = merge_tasks(existing, updates, max_tasks=8)

    assert merged == ()


@pytest.mark.asyncio
async def test_settle_no_response_stays_active_for_retry():
    unit = _unit(unit_id=65537, unit_index=1, x=18, y=24)
    gs = FakeGS(units=[unit], found_city_result="Action completed (no response).")
    task = _task(task_id="settle:65537", unit_id=65537, target_x=18, target_y=24)

    updated, results = await run_pre_model_tasks(gs, [task])

    assert updated[0].status == "active"
    assert updated[0].last_result == "settle_no_response"
    assert updated[0].failure_count == 1
    assert results[0]["status"] == "active"
    assert results[0]["action"] == "found_city"
    assert results[0]["result"] == "settle_no_response"


@pytest.mark.asyncio
async def test_settle_no_response_fails_at_failure_budget():
    unit = _unit(unit_id=65537, unit_index=1, x=18, y=24)
    gs = FakeGS(units=[unit], found_city_result="Action completed (no response).")
    task = _task(
        task_id="settle:65537",
        unit_id=65537,
        target_x=18,
        target_y=24,
        last_result="settle_no_response",
        failure_count=2,
    )

    updated, results = await run_pre_model_tasks(gs, [task])

    assert updated[0].status == "failed"
    assert updated[0].last_result == "settle_no_response_retry_limit"
    assert results[0]["status"] == "failed"
    assert results[0]["result"] == "settle_no_response_retry_limit"


@pytest.mark.asyncio
async def test_builder_improve_error_fails_at_failure_budget():
    unit = _unit(
        unit_id=65538,
        unit_index=2,
        x=12,
        y=19,
        valid_improvements=["IMPROVEMENT_MINE"],
    )
    gs = FakeGS(units=[unit], improve_tile_result="Error: improvement blocked")
    task = _task(
        task_id="builder_improve:65538",
        kind="builder_improve",
        unit_id=65538,
        target_x=12,
        target_y=19,
        improvement="IMPROVEMENT_MINE",
        last_result="Error: improvement blocked",
        failure_count=2,
    )

    updated, results = await run_pre_model_tasks(gs, [task])

    assert updated[0].status == "failed"
    assert updated[0].last_result == "improve_error_retry_limit"
    assert results[0]["status"] == "failed"
    assert results[0]["result"] == "improve_error_retry_limit"


@pytest.mark.asyncio
async def test_settle_survives_repeated_identical_transient_errors_within_budget():
    """Two byte-identical consecutive errors (e.g. a popup-blocked found_city) must
    NOT permanently fail the task; the third attempt can still succeed."""
    unit = _unit(unit_id=65537, unit_index=1, x=18, y=24)
    task = _task(task_id="settle:65537", unit_id=65537, target_x=18, target_y=24)
    error = "Error: FOUND_FAILED|Founding at 18,24 was requested but city did not appear."

    first_gs = FakeGS(units=[unit], found_city_result=error)
    updated, _ = await run_pre_model_tasks(first_gs, [task])
    assert updated[0].status == "active"
    assert updated[0].failure_count == 1

    second_gs = FakeGS(units=[unit], found_city_result=error)
    updated, _ = await run_pre_model_tasks(second_gs, updated)
    assert updated[0].status == "active"
    assert updated[0].failure_count == 2

    third_gs = FakeGS(units=[unit], found_city_result="FOUNDED|18,24")
    updated, results = await run_pre_model_tasks(third_gs, updated)
    assert updated[0].status == "complete"
    assert results[0]["result"] == "FOUNDED|18,24"


def test_task_state_round_trip_preserves_failure_count(tmp_path):
    task = _task(last_result="Error: FOUND_FAILED", failure_count=2)
    save_task_state(str(tmp_path), "run1", 4, [task])

    loaded = load_task_state(str(tmp_path), "run1", 4)

    assert loaded.tasks[0].failure_count == 2


@pytest.mark.asyncio
async def test_visible_hostile_at_current_position_blocks_movement():
    unit = _unit(unit_id=65537, unit_index=1, x=1, y=1)
    gs = FakeGS(
        units=[unit],
        map_tiles={(1, 1): [_tile(1, 1, units=["Barbarian WARRIOR"])]},
    )
    task = _task(task_id="settle:65537", unit_id=65537, target_x=18, target_y=24)

    updated, results = await run_pre_model_tasks(gs, [task])

    assert gs.move_unit_calls == []
    assert updated[0].status == "active"
    assert updated[0].last_result == "blocked_visible_hostile"
    assert results[0]["action"] == "block"


@pytest.mark.asyncio
async def test_visible_hostile_at_target_position_blocks_movement():
    unit = _unit(unit_id=65537, unit_index=1, x=1, y=1)
    gs = FakeGS(
        units=[unit],
        map_tiles={(18, 24): [_tile(18, 24, units=["Barbarian WARRIOR"])]},
    )
    task = _task(task_id="settle:65537", unit_id=65537, target_x=18, target_y=24)

    updated, results = await run_pre_model_tasks(gs, [task])

    assert gs.move_unit_calls == []
    assert updated[0].last_result == "blocked_visible_hostile"


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
async def test_at_war_city_state_unit_blocks_settler_movement():
    unit = _unit(unit_id=65537, unit_index=1, x=1, y=1)
    gs = FakeGS(
        units=[unit],
        map_tiles={(18, 24): [_tile(18, 24, units=["Vatican City WARRIOR"])]},
        threat_scan=[
            SimpleNamespace(owner_name="Vatican City", is_city_state=True),
        ],
    )
    task = _task(task_id="settle:65537", unit_id=65537, target_x=18, target_y=24)

    updated, results = await run_pre_model_tasks(gs, [task])

    assert gs.move_unit_calls == []
    assert updated[0].last_result == "blocked_visible_hostile"
    assert results[0]["action"] == "block"
    assert results[0]["result"] == "blocked_visible_hostile"


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
async def test_missing_threat_scan_blocks_unknown_unit_label_without_aborting():
    class MissingThreatScanGS(FakeGS):
        def __init__(self):
            super().__init__(
                units=[_unit(unit_id=65537, unit_index=1, x=1, y=1)],
                map_tiles={(18, 24): [_tile(18, 24, units=["Unidentified WARRIOR"])]},
                diplomacy=[SimpleNamespace(civ_name="Rome", is_at_war=False)],
            )
            self.get_threat_scan = None

    gs = MissingThreatScanGS()
    task = _task(task_id="settle:65537", unit_id=65537, target_x=18, target_y=24)

    updated, results = await run_pre_model_tasks(gs, [task])

    assert gs.move_unit_calls == []
    assert updated[0].status == "active"
    assert updated[0].last_result == "blocked_visible_hostile"
    assert results[0]["action"] == "block"
    assert results[0]["result"] == "blocked_visible_hostile"


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


@pytest.mark.asyncio
async def test_diplomacy_failure_still_blocks_exact_threat_scan_coordinate():
    unit = _unit(unit_id=65537, unit_index=1, x=1, y=1)
    gs = FakeGS(
        units=[unit],
        map_tiles={(18, 24): [_tile(18, 24, units=["Unidentified WARRIOR"])]},
        diplomacy_error=RuntimeError("diplomacy unavailable"),
        threat_scan=[
            SimpleNamespace(
                x=18,
                y=24,
                owner_name="",
                is_city_state=False,
            )
        ],
    )
    task = _task(task_id="settle:65537", unit_id=65537, target_x=18, target_y=24)

    updated, results = await run_pre_model_tasks(gs, [task])

    assert gs.move_unit_calls == []
    assert updated[0].last_result == "blocked_visible_hostile"
    assert results[0]["action"] == "block"


@pytest.mark.asyncio
async def test_threat_scan_major_owner_does_not_globally_block_peaceful_major_labels():
    unit = _unit(unit_id=65537, unit_index=1, x=1, y=1)
    gs = FakeGS(
        units=[unit],
        map_tiles={(18, 24): [_tile(18, 24, units=["Rome WARRIOR"])]},
        diplomacy=[SimpleNamespace(civ_name="Rome", is_at_war=False)],
        threat_scan=[
            SimpleNamespace(
                x=5,
                y=5,
                owner_name="Rome",
                is_city_state=False,
            )
        ],
    )
    task = _task(task_id="settle:65537", unit_id=65537, target_x=18, target_y=24)

    updated, results = await run_pre_model_tasks(gs, [task])

    assert gs.move_unit_calls == [(1, 18, 24)]
    assert updated[0].status == "active"
    assert results[0]["action"] == "move"


@pytest.mark.asyncio
async def test_threat_scan_coordinate_does_not_block_known_peaceful_major_label():
    unit = _unit(unit_id=65537, unit_index=1, x=1, y=1)
    gs = FakeGS(
        units=[unit],
        map_tiles={(18, 24): [_tile(18, 24, units=["Rome WARRIOR"])]},
        diplomacy=[SimpleNamespace(civ_name="Rome", is_at_war=False)],
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

    assert gs.move_unit_calls == [(1, 18, 24)]
    assert updated[0].status == "active"
    assert results[0]["action"] == "move"


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


@pytest.mark.asyncio
async def test_missing_unit_marks_task_lost():
    gs = FakeGS(units=[])
    task = _task(task_id="settle:65537", unit_id=65537, target_x=18, target_y=24)

    updated, results = await run_pre_model_tasks(gs, [task])

    assert updated[0].status == "lost"
    assert updated[0].last_result == "unit_missing"
    assert results == [
        {
            "task_id": "settle:65537",
            "kind": "settle",
            "unit_id": 65537,
            "target": [18, 24],
            "status": "lost",
            "action": "skip",
            "result": "unit_missing",
        }
    ]


@pytest.mark.asyncio
async def test_no_moves_remaining_keeps_task_active_and_skips():
    unit = _unit(unit_id=65537, unit_index=1, x=1, y=1, moves_remaining=0.0)
    gs = FakeGS(units=[unit])
    task = _task(task_id="settle:65537", unit_id=65537, target_x=18, target_y=24)

    updated, results = await run_pre_model_tasks(gs, [task])

    assert gs.move_unit_calls == []
    assert updated[0].status == "active"
    assert updated[0].last_result == "skipped_no_moves"
    assert results[0]["action"] == "skip"


@pytest.mark.asyncio
async def test_non_active_tasks_are_not_executed():
    gs = FakeGS(units=[])
    lost_task = _task(task_id="settle:1", unit_id=1, status="lost", last_result="unit_missing")

    updated, results = await run_pre_model_tasks(gs, [lost_task])

    assert updated == (lost_task,)
    assert results == []


@pytest.mark.asyncio
async def test_run_pre_model_tasks_skips_units_query_when_no_executable_tasks():
    gs = FakeGS(units=[_unit(unit_id=1, unit_index=1, x=1, y=1)])

    updated, results = await run_pre_model_tasks(gs, [])

    assert updated == ()
    assert results == []
    assert gs.units_calls == 0


@pytest.mark.asyncio
async def test_run_pre_model_tasks_skips_units_query_for_non_executable_tasks():
    gs = FakeGS(units=[_unit(unit_id=1, unit_index=1, x=1, y=1)])
    inactive = _task(task_id="settle:1", unit_id=1, status="complete")
    unsupported = _task(task_id="attack:2", kind="attack", unit_id=2)

    updated, results = await run_pre_model_tasks(gs, [inactive, unsupported])

    assert updated == (inactive, unsupported)
    assert results == []
    assert gs.units_calls == 0


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


@pytest.mark.asyncio
async def test_per_task_exception_is_caught_and_recorded():
    class RaisingGS(FakeGS):
        async def get_map_area(self, x, y, radius=2):
            raise RuntimeError("boom")

    unit = _unit(unit_id=65537, unit_index=1, x=1, y=1)
    gs = RaisingGS(units=[unit])
    task = _task(task_id="settle:65537", unit_id=65537, target_x=18, target_y=24)

    updated, results = await run_pre_model_tasks(gs, [task])

    assert updated[0].status == "active"
    assert updated[0].last_result.startswith("error:")
    assert results[0]["action"] == "error"
    assert results[0]["result"].startswith("error:")


@pytest.mark.asyncio
async def test_task_exception_accrues_failure_strike():
    class RaisingGS(FakeGS):
        async def found_city(self, unit_index):
            raise RuntimeError("tuner disconnected")

    unit = _unit(unit_id=65537, unit_index=1, x=18, y=24)
    gs = RaisingGS(units=[unit])
    task = _task(task_id="settle:65537", unit_id=65537, target_x=18, target_y=24)

    updated, results = await run_pre_model_tasks(gs, [task], turn=9)

    assert updated[0].status == "active"
    assert updated[0].failure_count == 1
    assert updated[0].updated_turn == 9


@pytest.mark.asyncio
async def test_task_exception_fails_at_failure_budget():
    """A task whose action raises every turn must hit MAX_TASK_FAILURES like a
    string-error failure would, not retry forever."""
    class RaisingGS(FakeGS):
        async def found_city(self, unit_index):
            raise RuntimeError("tuner disconnected")

    unit = _unit(unit_id=65537, unit_index=1, x=18, y=24)
    gs = RaisingGS(units=[unit])
    task = _task(
        task_id="settle:65537", unit_id=65537, target_x=18, target_y=24,
        failure_count=2,
    )

    updated, results = await run_pre_model_tasks(gs, [task], turn=9)

    assert updated[0].status == "failed"
    assert updated[0].failure_count == 3
    assert results[0]["status"] == "failed"
    assert results[0]["result"] == "task_exception_retry_limit"


# ---------------------------------------------------------------------------
# format_task_block
# ---------------------------------------------------------------------------


def test_format_task_block_empty_returns_empty_string():
    assert format_task_block([], []) == ""


def test_format_task_block_includes_heading_and_bounded_content():
    tasks = tuple(_task(task_id=f"settle:{i}", unit_id=i) for i in range(10))
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
        for i in range(10)
    ]

    block = format_task_block(tasks, results)

    assert block.startswith("== DETERMINISTIC TASK TRACKER ==")
    assert block.count("settle:") == 16  # 8 active task lines + 8 result lines


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


def test_format_task_block_omits_non_active_tasks():
    tasks = (_task(task_id="settle:1", unit_id=1, status="complete"),)

    block = format_task_block(tasks, [])

    assert block == ""
