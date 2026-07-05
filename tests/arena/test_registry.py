import pytest

from civ_mcp.arena.registry import (
    TOOL_REGISTRY,
    TIERS,
    dispatch,
    openai_tools,
    resolve_tools,
)


MINIMAL_9 = {
    "get_overview",
    "get_units",
    "get_cities",
    "move_unit",
    "found_city",
    "set_city_production",
    "set_research",
    "fortify_unit",
    "skip_unit",
}


def test_minimal_tier_is_todays_nine():
    assert set(TIERS["minimal"]) == MINIMAL_9


def test_tiers_nest():
    assert set(TIERS["minimal"]) < set(TIERS["standard"]) < set(TIERS["full"])


def test_standard_adds_map_and_combat():
    extra = set(TIERS["standard"]) - set(TIERS["minimal"])
    assert {
        "get_map_area",
        "get_tech_civics",
        "attack_unit",
        "improve_tile",
        "purchase_item",
    } <= extra


def test_forbidden_tools_never_defined():
    for name in (
        "end_turn",
        "execute_lua",
        "load_game_save",
        "kill_game",
        "queue_wc_votes",
        "diplomacy_respond",
    ):
        assert name not in TOOL_REGISTRY


def test_resolve_tools_tier_and_explicit_list():
    assert resolve_tools("minimal") == TIERS["minimal"]
    assert resolve_tools(["get_units", "move_unit"]) == ("get_units", "move_unit")


def test_resolve_tools_rejects_unknown():
    with pytest.raises(ValueError):
        resolve_tools("mega")
    with pytest.raises(ValueError):
        resolve_tools(["get_units", "launch_nuke"])


def test_openai_tools_schema_shape():
    (t,) = openai_tools(["move_unit"])
    fn = t["function"]
    assert t["type"] == "function" and fn["name"] == "move_unit"
    assert set(fn["parameters"]["required"]) == {"unit_index", "x", "y"}


@pytest.mark.asyncio
async def test_dispatch_maps_args():
    calls = []

    class FakeGS:
        async def move_unit(self, unit_index, target_x, target_y):
            calls.append((unit_index, target_x, target_y))
            return "MOVING_TO|4,5"

        async def attack_unit(self, unit_index, target_x, target_y):
            calls.append(("atk", unit_index))
            return "ATTACKED"

    assert (
        await dispatch(FakeGS(), "move_unit", {"unit_index": 1, "x": 4, "y": 5})
        == "MOVING_TO|4,5"
    )
    assert (
        await dispatch(FakeGS(), "attack_unit", {"unit_index": 2, "x": 9, "y": 9})
        == "ATTACKED"
    )
    assert calls == [(1, 4, 5), ("atk", 2)]


@pytest.mark.asyncio
async def test_dispatch_set_policies_coerces_assignment_keys_to_int():
    calls = []

    class FakeGS:
        async def set_policies(self, assignments):
            calls.append(assignments)
            return "OK"

    assert (
        await dispatch(
            FakeGS(),
            "set_policies",
            {"assignments": {"0": "POLICY_AGOGE"}},
        )
        == "OK"
    )
    assert calls == [{0: "POLICY_AGOGE"}]


@pytest.mark.asyncio
async def test_dispatch_rejects_out_of_allowed():
    """An in-registry name outside the allowed set must never reach GameState."""

    class FakeGS:
        async def get_map_area(self, x, y, radius):
            raise AssertionError("out-of-tier tool must never execute")

    with pytest.raises(KeyError):
        await dispatch(
            FakeGS(),
            "get_map_area",
            {"x": 1, "y": 1},
            allowed=("get_units", "move_unit"),
        )


@pytest.mark.asyncio
async def test_read_tools_narrate_not_repr():
    from civ_mcp import lua as lq

    class FakeGS:
        async def get_units(self):
            return [
                lq.UnitInfo(
                    unit_id=65537,
                    unit_index=1,
                    name="Warrior",
                    unit_type="UNIT_WARRIOR",
                    x=10,
                    y=10,
                    moves_remaining=2,
                    max_moves=2,
                    health=100,
                    max_health=100,
                )
            ]

    out = await dispatch(FakeGS(), "get_units", {})
    assert "UnitInfo(" not in out
    assert "at (10,10)" in out


def test_agent_module_still_exposes_tools():
    from civ_mcp.arena.agent import TOOLS

    names = {t["function"]["name"] for t in TOOLS}
    assert names == MINIMAL_9


def test_get_map_area_radius_schema_is_bounded():
    (tool,) = openai_tools(["get_map_area"])
    radius = tool["function"]["parameters"]["properties"]["radius"]

    assert radius["type"] == "integer"
    assert radius["minimum"] == 0
    assert radius["maximum"] == 5


@pytest.mark.asyncio
async def test_get_map_area_radius_clamped_before_game_state():
    calls = []

    class FakeGS:
        async def get_map_area(self, x, y, radius):
            calls.append((x, y, radius))
            return []

    await dispatch(FakeGS(), "get_map_area", {"x": 1, "y": 2, "radius": 99})
    await dispatch(FakeGS(), "get_map_area", {"x": 1, "y": 2, "radius": -3})
    await dispatch(FakeGS(), "get_map_area", {"x": 1, "y": 2})

    assert calls == [(1, 2, 5), (1, 2, 0), (1, 2, 2)]


@pytest.mark.asyncio
async def test_get_map_area_radius_tolerates_null_and_non_numeric():
    """radius:null / non-numeric must fall back to the default, never raise."""
    calls = []

    class FakeGS:
        async def get_map_area(self, x, y, radius):
            calls.append(radius)
            return []

    await dispatch(FakeGS(), "get_map_area", {"x": 1, "y": 2, "radius": None})
    await dispatch(FakeGS(), "get_map_area", {"x": 1, "y": 2, "radius": "far"})
    await dispatch(FakeGS(), "get_map_area", {"x": 1, "y": 2, "radius": 2.9})

    assert calls == [2, 2, 2]


def test_apply_param_bounds_clamps_any_declared_integer_param():
    """Schema minimum/maximum is enforced generically at dispatch, not per-tool."""
    from civ_mcp.arena.registry import _apply_param_bounds, ToolDef, _int_param

    async def _noop(gs, args):
        return ""

    tool = ToolDef(
        name="t",
        description="",
        params={
            "depth": _int_param("bounded", minimum=1, maximum=3),
            "free": _int_param("unbounded"),
        },
        required=(),
        call=_noop,
    )

    # Bounded param clamps both ends; unbounded param is untouched.
    assert _apply_param_bounds(tool, {"depth": 9, "free": 100}) == {"depth": 3, "free": 100}
    assert _apply_param_bounds(tool, {"depth": -5})["depth"] == 1
    # Malformed / absent values are left for the tool to handle.
    assert _apply_param_bounds(tool, {"depth": None}) == {"depth": None}
    assert _apply_param_bounds(tool, {}) == {}
