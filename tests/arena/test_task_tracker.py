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
          created_turn=1, updated_turn=1, improvement="", status="active", last_result=""):
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
    ):
        self.units = units
        self.map_tiles = map_tiles or {}
        self.found_city_result = found_city_result
        self.move_unit_result = move_unit_result
        self.improve_tile_result = improve_tile_result
        self.found_city_calls = []
        self.move_unit_calls = []
        self.improve_tile_calls = []
        self.map_area_calls = []

    async def get_units(self):
        return self.units

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


def test_merge_cancel_removes_matching_unit_id_task():
    existing = (_task(task_id="settle:1", unit_id=1),)
    cancel = _task(task_id="cancel:1", kind="cancel", unit_id=1, status="cancelled")

    merged = merge_tasks(existing, [cancel], max_tasks=10)

    assert merged == ()


def test_merge_respects_max_tasks_keeping_newest():
    existing = (
        _task(task_id="settle:1", unit_id=1, updated_turn=1),
        _task(task_id="settle:2", unit_id=2, updated_turn=2),
        _task(task_id="settle:3", unit_id=3, updated_turn=3),
    )

    merged = merge_tasks(existing, [], max_tasks=2)

    assert [t.task_id for t in merged] == ["settle:2", "settle:3"]


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
async def test_builder_improve_blocks_when_improvement_not_valid():
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
    assert results[0]["action"] == "block"
    assert results[0]["result"] == "blocked_improvement_not_valid"


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


def test_format_task_block_omits_non_active_tasks():
    tasks = (_task(task_id="settle:1", unit_id=1, status="complete"),)

    block = format_task_block(tasks, [])

    assert block == ""
