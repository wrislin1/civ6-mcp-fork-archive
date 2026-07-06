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


def test_full_tier_initially_matches_registry_order():
    assert TIERS["full"] == tuple(TOOL_REGISTRY)


def test_resolve_tools_full_tracks_registry_additions(monkeypatch):
    from civ_mcp.arena.registry import ToolDef

    async def _noop(gs, args):
        return ""

    monkeypatch.setitem(
        TOOL_REGISTRY,
        "__probe_tool__",
        ToolDef(
            name="__probe_tool__",
            description="probe",
            params={},
            required=(),
            call=_noop,
        ),
    )

    assert "__probe_tool__" in resolve_tools("full")


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


DIPLOMACY_TOOL_NAMES = {
    "get_pending_diplomacy",
    "respond_to_diplomacy",
    "get_pending_trades",
    "respond_to_trade",
    "get_trade_options",
    "propose_trade",
    "propose_peace",
    "send_diplomatic_action",
    "form_alliance",
}


def test_diplomacy_tools_registered_full_only():
    assert DIPLOMACY_TOOL_NAMES <= set(TOOL_REGISTRY)
    assert DIPLOMACY_TOOL_NAMES <= set(resolve_tools("full"))
    assert DIPLOMACY_TOOL_NAMES.isdisjoint(set(resolve_tools("minimal")))
    assert DIPLOMACY_TOOL_NAMES.isdisjoint(set(resolve_tools("standard")))

    for internal_name in (
        "diplomacy_respond",
        "get_deal_options",
        "get_pending_deals",
        "respond_to_deal",
    ):
        assert internal_name not in TOOL_REGISTRY


def test_diplomacy_tool_schema_shape():
    by_name = {tool["function"]["name"]: tool["function"] for tool in openai_tools(sorted(DIPLOMACY_TOOL_NAMES))}

    assert by_name["respond_to_trade"]["parameters"]["properties"]["accept"]["type"] == "boolean"
    assert set(by_name["respond_to_trade"]["parameters"]["required"]) == {"other_player_id", "accept"}
    assert set(by_name["get_trade_options"]["parameters"]["required"]) == {"other_player_id"}
    assert set(by_name["respond_to_diplomacy"]["parameters"]["required"]) == {"other_player_id", "response"}
    assert by_name["propose_trade"]["parameters"]["properties"]["mode"]["type"] == "string"
    assert by_name["form_alliance"]["parameters"]["properties"]["alliance_type"]["type"] == "string"

    action_desc = by_name["send_diplomatic_action"]["parameters"]["properties"]["action"]["description"]
    for token in (
        "DIPLOMATIC_DELEGATION",
        "DECLARE_FRIENDSHIP",
        "DENOUNCE",
        "RESIDENT_EMBASSY",
        "OPEN_BORDERS",
        "DECLARE_SURPRISE_WAR",
        "DECLARE_FORMAL_WAR",
        "DECLARE_HOLY_WAR",
        "DECLARE_LIBERATION_WAR",
        "DECLARE_RECONQUEST_WAR",
        "DECLARE_PROTECTORATE_WAR",
        "DECLARE_COLONIAL_WAR",
        "DECLARE_TERRITORIAL_WAR",
    ):
        assert token in action_desc


@pytest.mark.asyncio
async def test_dispatch_pending_diplomacy_and_trades_are_narrated():
    class FakeGS:
        async def get_diplomacy_sessions(self):
            from civ_mcp import lua as lq

            return [
                lq.DiplomacySession(
                    session_id=12,
                    other_player_id=3,
                    other_civ_name="Rome",
                    other_leader_name="Trajan",
                    choices=[],
                    dialogue_text="Welcome.",
                    buttons="POSITIVE;NEGATIVE",
                )
            ]

        async def get_pending_deals(self):
            from civ_mcp import lua as lq

            return [
                lq.PendingDeal(
                    other_player_id=4,
                    other_player_name="Egypt",
                    other_leader_name="Cleopatra",
                    items_from_them=[
                        lq.DealItem(
                            from_player_id=4,
                            from_player_name="Egypt",
                            item_type="GOLD",
                            name="Gold",
                            amount=50,
                            duration=0,
                            is_from_us=False,
                        )
                    ],
                    items_from_us=[],
                )
            ]

    diplo = await dispatch(FakeGS(), "get_pending_diplomacy", {})
    deals = await dispatch(FakeGS(), "get_pending_trades", {})

    assert "Rome" in diplo and "Respond with: POSITIVE" in diplo
    assert "Egypt" in deals and "respond_to_trade(other_player_id=4" in deals


@pytest.mark.asyncio
async def test_dispatch_trade_options_are_narrated():
    class FakeGS:
        async def get_deal_options(self, other_player_id):
            from civ_mcp import lua as lq

            assert other_player_id == 3
            return lq.DealOptions(
                other_player_id=3,
                other_civ_name="Rome",
                our_gold=120,
                our_gpt=8,
                their_gold=40,
                their_gpt=3,
                our_luxuries=["Silk x2"],
                alliance_eligible=True,
            )

    text = await dispatch(FakeGS(), "get_trade_options", {"other_player_id": 3})

    assert "Trade options with Rome (player 3)" in text
    assert "Silk x2" in text
    assert "Alliance: eligible" in text


@pytest.mark.asyncio
async def test_dispatch_reactive_action_tools_call_gamestate_methods():
    calls = []

    class FakeGS:
        async def diplomacy_respond(self, other_player_id, response):
            calls.append(("diplomacy_respond", other_player_id, response))
            return "OK:RESPONDED|POSITIVE|SESSION_CLOSED"

        async def respond_to_deal(self, other_player_id, accept):
            calls.append(("respond_to_deal", other_player_id, accept))
            return "OK:DEAL_ACCEPTED|Rome"

    diplo = await dispatch(
        FakeGS(),
        "respond_to_diplomacy",
        {"other_player_id": 3, "response": "POSITIVE"},
    )
    trade = await dispatch(
        FakeGS(),
        "respond_to_trade",
        {"other_player_id": 4, "accept": True},
    )

    assert diplo == "OK:RESPONDED|POSITIVE|SESSION_CLOSED"
    assert trade == "OK:DEAL_ACCEPTED|Rome"
    assert calls == [
        ("diplomacy_respond", 3, "POSITIVE"),
        ("respond_to_deal", 4, True),
    ]


@pytest.mark.asyncio
async def test_dispatch_respond_to_trade_rejects_non_boolean_accept():
    class FakeGS:
        async def respond_to_deal(self, other_player_id, accept):
            raise AssertionError("malformed accept must not reach GameState")

    text = await dispatch(
        FakeGS(),
        "respond_to_trade",
        {"other_player_id": 4, "accept": "false"},
    )

    assert text == "Error: accept must be boolean"


@pytest.mark.asyncio
async def test_dispatch_proactive_diplomacy_tools_call_gamestate_methods():
    calls = []

    class FakeGS:
        async def propose_peace(self, other_player_id):
            calls.append(("propose_peace", other_player_id))
            return "ACCEPTED|Peace established with Rome"

        async def send_diplomatic_action(self, other_player_id, action):
            calls.append(("send_diplomatic_action", other_player_id, action))
            return "OK:DIPLOMATIC_DELEGATION|Rome"

        async def form_alliance(self, other_player_id, alliance_type):
            calls.append(("form_alliance", other_player_id, alliance_type))
            return "OK:ALLIANCE_FORMED|Rome|RESEARCH"

    peace = await dispatch(FakeGS(), "propose_peace", {"other_player_id": 3})
    delegation = await dispatch(
        FakeGS(),
        "send_diplomatic_action",
        {"other_player_id": 3, "action": "diplomatic_delegation"},
    )
    alliance = await dispatch(
        FakeGS(),
        "form_alliance",
        {"other_player_id": 3, "alliance_type": "research"},
    )

    assert peace.startswith("ACCEPTED|")
    assert delegation.startswith("OK:DIPLOMATIC_DELEGATION")
    assert alliance.startswith("OK:ALLIANCE_FORMED")
    assert calls == [
        ("propose_peace", 3),
        ("send_diplomatic_action", 3, "DIPLOMATIC_DELEGATION"),
        ("form_alliance", 3, "RESEARCH"),
    ]


@pytest.mark.asyncio
async def test_dispatch_propose_trade_builds_items_for_send_and_test_modes():
    calls = []

    class FakeGS:
        async def test_trade(self, other_player_id, offer_items, request_items):
            calls.append(("test_trade", other_player_id, offer_items, request_items))
            return "AI counter-offer: Rome will accept"

        async def propose_trade(self, other_player_id, offer_items, request_items):
            calls.append(("propose_trade", other_player_id, offer_items, request_items))
            return "OK:ACCEPTED|Trade accepted with Rome"

    test_text = await dispatch(
        FakeGS(),
        "propose_trade",
        {
            "other_player_id": 3,
            "offer_resources": "RESOURCE_SILK, RESOURCE_TEA",
            "request_gold_per_turn": 5,
            "request_open_borders": True,
            "mode": "test",
        },
    )
    send_text = await dispatch(
        FakeGS(),
        "propose_trade",
        {
            "other_player_id": 3,
            "offer_favor": 20,
            "request_gold": 80,
            "mode": "send",
        },
    )

    assert test_text.startswith("AI counter-offer")
    assert send_text.startswith("OK:ACCEPTED")
    assert calls[0] == (
        "test_trade",
        3,
        [
            {"type": "RESOURCE", "name": "RESOURCE_SILK", "amount": 1, "duration": 30},
            {"type": "RESOURCE", "name": "RESOURCE_TEA", "amount": 1, "duration": 30},
        ],
        [
            {"type": "GOLD", "amount": 5, "duration": 30},
            {"type": "AGREEMENT", "subtype": "OPEN_BORDERS"},
        ],
    )
    assert calls[1] == (
        "propose_trade",
        3,
        [{"type": "FAVOR", "amount": 20}],
        [{"type": "GOLD", "amount": 80, "duration": 0}],
    )


@pytest.mark.asyncio
async def test_dispatch_propose_trade_defaults_missing_mode_to_test():
    calls = []

    class FakeGS:
        async def test_trade(self, other_player_id, offer_items, request_items):
            calls.append(("test_trade", other_player_id, offer_items, request_items))
            return "AI counter-offer: Rome will accept"

        async def propose_trade(self, other_player_id, offer_items, request_items):
            raise AssertionError("missing mode must not commit a trade")

    text = await dispatch(
        FakeGS(),
        "propose_trade",
        {"other_player_id": 3, "offer_gold": 10},
    )

    assert text.startswith("AI counter-offer")
    assert calls == [
        (
            "test_trade",
            3,
            [{"type": "GOLD", "amount": 10, "duration": 0}],
            [],
        )
    ]


@pytest.mark.asyncio
async def test_dispatch_propose_trade_rejects_non_boolean_open_borders_flags():
    class FakeGS:
        async def test_trade(self, other_player_id, offer_items, request_items):
            raise AssertionError("malformed open-borders flag must not reach GameState")

        async def propose_trade(self, other_player_id, offer_items, request_items):
            raise AssertionError("malformed open-borders flag must not reach GameState")

    offer_text = await dispatch(
        FakeGS(),
        "propose_trade",
        {
            "other_player_id": 3,
            "offer_gold": 10,
            "offer_open_borders": "false",
            "mode": "test",
        },
    )
    request_text = await dispatch(
        FakeGS(),
        "propose_trade",
        {
            "other_player_id": 3,
            "request_gold": 10,
            "request_open_borders": "false",
            "mode": "test",
        },
    )

    assert offer_text == "Error: offer_open_borders must be boolean"
    assert request_text == "Error: request_open_borders must be boolean"


@pytest.mark.asyncio
async def test_dispatch_propose_trade_rejects_empty_or_bad_mode():
    class FakeGS:
        async def test_trade(self, other_player_id, offer_items, request_items):
            raise AssertionError("invalid trade must not reach GameState")

        async def propose_trade(self, other_player_id, offer_items, request_items):
            raise AssertionError("invalid trade must not reach GameState")

    empty = await dispatch(FakeGS(), "propose_trade", {"other_player_id": 3})
    bool_gold = await dispatch(
        FakeGS(),
        "propose_trade",
        {"other_player_id": 3, "offer_gold": True, "mode": "test"},
    )
    bad_mode = await dispatch(
        FakeGS(),
        "propose_trade",
        {"other_player_id": 3, "offer_gold": 10, "mode": "preview"},
    )

    assert empty == "Error: must specify at least one offer or request item"
    assert bool_gold == "Error: must specify at least one offer or request item"
    assert bad_mode == 'Error: mode must be "test" or "send"'


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


def test_registry_has_no_generic_param_bounds_layer():
    import civ_mcp.arena.registry as registry_mod

    assert not hasattr(registry_mod, "_apply_param_bounds")
