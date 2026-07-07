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
    # NOTE: queue_wc_votes was forbidden pre-Task-7 (no validated wrapper existed).
    # Task 7 adds it as a discrete tool with JSON/list validation (see
    # test_queue_wc_votes_* below), so it is intentionally absent from this list.
    for name in (
        "end_turn",
        "execute_lua",
        "load_game_save",
        "kill_game",
        "diplomacy_respond",
        # Raw/lifecycle tools that must remain absent (Task 7 constraint).
        "unit_action",
        "city_action",
        "run_lua",
        "load_save",
        "restart_and_load",
        "launch_game",
        "list_saves",
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


# ---------------------------------------------------------------------------
# Task 7 — behavior-critical tools (Great People, trade routes, religion,
# World Congress, city ranged attack/capture, governor/dedication, global
# settling, city production).
# ---------------------------------------------------------------------------

BEHAVIOR_CRITICAL_TOOL_NAMES = {
    "get_city_production",
    "get_global_settle_advisor",
    "get_governors",
    "get_dedications",
    "get_religion_beliefs",
    "get_religion_spread",
    "get_trade_routes",
    "get_trade_destinations",
    "get_gp_advisor",
    "get_world_congress",
    "promote_governor",
    "choose_dedication",
    "found_religion",
    "recruit_great_person",
    "patronize_great_person",
    "reject_great_person",
    "start_trade_route",
    "teleport_trader",
    "queue_wc_votes",
    "city_attack",
    "resolve_city_capture",
}

BEHAVIOR_CRITICAL_ACTION_VERBS = (
    "promote_governor",
    "choose_dedication",
    "found_religion",
    "recruit_great_person",
    "patronize_great_person",
    "reject_great_person",
    "start_trade_route",
    "teleport_trader",
    "queue_wc_votes",
    "city_attack",
    "resolve_city_capture",
)


def test_behavior_critical_tools_registered_full_only():
    assert BEHAVIOR_CRITICAL_TOOL_NAMES <= set(TOOL_REGISTRY)
    assert BEHAVIOR_CRITICAL_TOOL_NAMES <= set(resolve_tools("full"))
    assert BEHAVIOR_CRITICAL_TOOL_NAMES.isdisjoint(set(resolve_tools("minimal")))
    assert BEHAVIOR_CRITICAL_TOOL_NAMES.isdisjoint(set(resolve_tools("standard")))


def test_behavior_critical_raw_and_lifecycle_tools_absent():
    for name in (
        "unit_action",
        "city_action",
        "run_lua",
        "load_game_save",
        "load_save",
        "restart_and_load",
        "kill_game",
        "launch_game",
        "list_saves",
        "end_turn",
    ):
        assert name not in TOOL_REGISTRY


def test_behavior_critical_action_verbs_set():
    for name in BEHAVIOR_CRITICAL_ACTION_VERBS:
        assert TOOL_REGISTRY[name].verb == name


@pytest.mark.asyncio
async def test_dispatch_behavior_critical_read_tools_are_narrated():
    from civ_mcp import lua as lq

    class FakeGS:
        async def list_city_production(self, city_id):
            assert city_id == 7
            return [
                lq.ProductionOption(
                    category="UNIT",
                    item_name="UNIT_WARRIOR",
                    cost=100,
                    turns=2,
                )
            ]

        async def get_global_settle_scan(self):
            return []

        async def get_governors(self):
            return lq.GovernorStatus(
                points_available=0,
                points_spent=0,
                can_appoint=False,
                appointed=[],
                available_to_appoint=[],
            )

        async def get_dedications(self):
            return lq.DedicationStatus(
                age_type="Normal",
                era=1,
                era_score=0,
                dark_threshold=0,
                golden_threshold=0,
                selections_allowed=1,
                active=[],
                choices=[],
            )

        async def get_religion_founding_status(self):
            return lq.ReligionFoundingStatus(
                has_religion=False,
                religion_type=None,
                religion_name=None,
                pantheon_index=-1,
                faith_balance=0.0,
                available_religions=[],
                beliefs_by_class={},
            )

        async def get_religion_status(self):
            return lq.ReligionStatus(cities=[], summary=[])

        async def get_trade_routes(self):
            return lq.TradeRouteStatus(capacity=1, active_count=0, traders=[])

        async def get_world_congress(self):
            return lq.WorldCongressStatus(
                is_in_session=False,
                turns_until_next=0,
                favor=0,
                max_votes=0,
                favor_costs=[],
                resolutions=[],
                proposals=[],
            )

    production = await dispatch(FakeGS(), "get_city_production", {"city_id": 7})
    settle = await dispatch(FakeGS(), "get_global_settle_advisor", {})
    governors = await dispatch(FakeGS(), "get_governors", {})
    dedications = await dispatch(FakeGS(), "get_dedications", {})
    beliefs = await dispatch(FakeGS(), "get_religion_beliefs", {})
    spread = await dispatch(FakeGS(), "get_religion_spread", {})
    routes = await dispatch(FakeGS(), "get_trade_routes", {})
    congress = await dispatch(FakeGS(), "get_world_congress", {})

    for text in (production, settle, governors, dedications, beliefs, spread, routes, congress):
        assert isinstance(text, str)
        assert "ProductionOption(" not in text
        assert "GovernorStatus(" not in text


@pytest.mark.asyncio
async def test_dispatch_gp_advisor_returns_error_string_on_none():
    class FakeGS:
        async def get_gp_advisor(self, unit_index):
            assert unit_index == 5
            return None

    text = await dispatch(FakeGS(), "get_gp_advisor", {"unit_id": 5})
    assert text.startswith("Error:")


@pytest.mark.asyncio
async def test_dispatch_gp_advisor_narrates_result():
    from civ_mcp import lua as lq

    class FakeGS:
        async def get_gp_advisor(self, unit_index):
            assert unit_index == 5
            return lq.GPAdvisorResult(
                gp_name="Confucius",
                gp_class="GREAT_PERSON_CLASS_GREAT_PROPHET",
                target_district="DISTRICT_HOLY_SITE",
                gp_x=0,
                gp_y=0,
                charges=1,
                cities=[],
            )

    text = await dispatch(FakeGS(), "get_gp_advisor", {"unit_id": 5})
    assert "Confucius" in text
    assert "GPAdvisorResult(" not in text


@pytest.mark.asyncio
async def test_dispatch_unit_id_tools_resolve_to_unit_index():
    """unit_id (composite) -> unit_index (unit_id % 65536) for the four affected tools."""
    calls = []

    class FakeGS:
        async def get_trade_destinations(self, unit_index):
            calls.append(("get_trade_destinations", unit_index))
            return []

        async def get_gp_advisor(self, unit_index):
            calls.append(("get_gp_advisor", unit_index))
            return None

        async def make_trade_route(self, unit_index, target_x, target_y):
            calls.append(("make_trade_route", unit_index, target_x, target_y))
            return "OK:ROUTE_STARTED"

        async def teleport_to_city(self, unit_index, target_x, target_y):
            calls.append(("teleport_to_city", unit_index, target_x, target_y))
            return "OK:TELEPORTED"

    composite_unit_id = 3 * 65536 + 42  # unit_index 42, distinct composite prefix

    await dispatch(FakeGS(), "get_trade_destinations", {"unit_id": composite_unit_id})
    await dispatch(FakeGS(), "get_gp_advisor", {"unit_id": composite_unit_id})
    await dispatch(
        FakeGS(),
        "start_trade_route",
        {"unit_id": composite_unit_id, "target_x": 10, "target_y": 11},
    )
    await dispatch(
        FakeGS(),
        "teleport_trader",
        {"unit_id": composite_unit_id, "target_x": 10, "target_y": 11},
    )

    assert calls == [
        ("get_trade_destinations", 42),
        ("get_gp_advisor", 42),
        ("make_trade_route", 42, 10, 11),
        ("teleport_to_city", 42, 10, 11),
    ]


@pytest.mark.asyncio
async def test_dispatch_gp_and_trade_action_tools_call_gamestate_methods():
    calls = []

    class FakeGS:
        async def promote_governor(self, governor_type, promotion_type):
            calls.append(("promote_governor", governor_type, promotion_type))
            return "OK:PROMOTED"

        async def choose_dedication(self, dedication_index):
            calls.append(("choose_dedication", dedication_index))
            return "OK:DEDICATION_CHOSEN"

        async def found_religion(self, religion_type, follower_belief, founder_belief):
            calls.append(("found_religion", religion_type, follower_belief, founder_belief))
            return "OK:RELIGION_FOUNDED"

        async def recruit_great_person(self, individual_id):
            calls.append(("recruit_great_person", individual_id))
            return "OK:RECRUITED"

        async def patronize_great_person(self, individual_id, yield_type):
            calls.append(("patronize_great_person", individual_id, yield_type))
            return "OK:PATRONIZED"

        async def reject_great_person(self, individual_id):
            calls.append(("reject_great_person", individual_id))
            return "OK:REJECTED"

        async def city_attack(self, city_id, target_x, target_y):
            calls.append(("city_attack", city_id, target_x, target_y))
            return "CITY_RANGE_ATTACK|..."

    assert (
        await dispatch(
            FakeGS(),
            "promote_governor",
            {"governor_type": "GOVERNOR_AMANI", "promotion_type": "PROMOTION_AFFLUENCE"},
        )
        == "OK:PROMOTED"
    )
    assert await dispatch(FakeGS(), "choose_dedication", {"dedication_index": 1}) == "OK:DEDICATION_CHOSEN"
    assert (
        await dispatch(
            FakeGS(),
            "found_religion",
            {
                "religion_name": "RELIGION_BUDDHISM",
                "follower_belief": "BELIEF_FOLLOWER",
                "founder_belief": "BELIEF_FOUNDER",
            },
        )
        == "OK:RELIGION_FOUNDED"
    )
    assert await dispatch(FakeGS(), "recruit_great_person", {"individual_id": 9}) == "OK:RECRUITED"
    assert (
        await dispatch(
            FakeGS(),
            "patronize_great_person",
            {"individual_id": 9},
        )
        == "OK:PATRONIZED"
    )
    assert await dispatch(FakeGS(), "reject_great_person", {"individual_id": 9}) == "OK:REJECTED"
    assert (
        await dispatch(
            FakeGS(),
            "city_attack",
            {"city_id": 1, "target_x": 3, "target_y": 4},
        )
        == "CITY_RANGE_ATTACK|..."
    )

    assert calls == [
        ("promote_governor", "GOVERNOR_AMANI", "PROMOTION_AFFLUENCE"),
        ("choose_dedication", 1),
        ("found_religion", "RELIGION_BUDDHISM", "BELIEF_FOLLOWER", "BELIEF_FOUNDER"),
        ("recruit_great_person", 9),
        ("patronize_great_person", 9, "YIELD_GOLD"),
        ("reject_great_person", 9),
        ("city_attack", 1, 3, 4),
    ]


@pytest.mark.asyncio
async def test_dispatch_queue_wc_votes_accepts_json_string_and_list():
    calls = []

    class FakeGS:
        async def queue_wc_votes(self, votes):
            calls.append(votes)
            return "OK:VOTES_QUEUED"

    from_json_str = await dispatch(
        FakeGS(),
        "queue_wc_votes",
        {"votes": '[{"hash": 123, "option": 1, "target": 0, "votes": 5}]'},
    )
    from_list = await dispatch(
        FakeGS(),
        "queue_wc_votes",
        {"votes": [{"hash": 456, "option": 2, "target": 1, "votes": 3}]},
    )

    assert from_json_str == "OK:VOTES_QUEUED"
    assert from_list == "OK:VOTES_QUEUED"
    assert calls == [
        [{"hash": 123, "option": 1, "target": 0, "votes": 5}],
        [{"hash": 456, "option": 2, "target": 1, "votes": 3}],
    ]


@pytest.mark.asyncio
async def test_dispatch_queue_wc_votes_rejects_malformed_json():
    class FakeGS:
        async def queue_wc_votes(self, votes):
            raise AssertionError("malformed votes must not reach GameState")

    text = await dispatch(FakeGS(), "queue_wc_votes", {"votes": "not json"})
    assert text.startswith("Error:")


@pytest.mark.asyncio
async def test_dispatch_queue_wc_votes_rejects_non_list_payload():
    class FakeGS:
        async def queue_wc_votes(self, votes):
            raise AssertionError("non-list votes must not reach GameState")

    dict_payload = await dispatch(FakeGS(), "queue_wc_votes", {"votes": '{"hash": 1}'})
    non_dict_items = await dispatch(FakeGS(), "queue_wc_votes", {"votes": "[1, 2, 3]"})

    assert dict_payload.startswith("Error:")
    assert non_dict_items.startswith("Error:")


@pytest.mark.asyncio
async def test_dispatch_resolve_city_capture_accepts_valid_actions():
    calls = []

    class FakeGS:
        async def resolve_city_capture(self, action):
            calls.append(action)
            return f"OK:{action.upper()}"

    # "reject" (free a disloyal loyalty-flip city) must be accepted too: it is a
    # valid server.py/lua directive, and omitting it left arena puppets unable
    # to resolve a loyalty flip.
    for action in ("keep", "reject", "raze", "liberate_founder", "liberate_previous"):
        text = await dispatch(FakeGS(), "resolve_city_capture", {"action": action})
        assert text == f"OK:{action.upper()}"

    assert calls == ["keep", "reject", "raze", "liberate_founder", "liberate_previous"]


@pytest.mark.asyncio
async def test_dispatch_resolve_city_capture_rejects_unknown_action():
    class FakeGS:
        async def resolve_city_capture(self, action):
            raise AssertionError("unknown action must not reach GameState")

    text = await dispatch(FakeGS(), "resolve_city_capture", {"action": "destroy"})
    assert text.startswith("Error:")


@pytest.mark.asyncio
async def test_dispatch_queue_wc_votes_rejects_non_integer_fields():
    class FakeGS:
        async def queue_wc_votes(self, votes):
            raise AssertionError("non-integer vote fields must not reach GameState")

    string_option = await dispatch(
        FakeGS(),
        "queue_wc_votes",
        {"votes": '[{"hash": 1, "option": "B", "target": 0, "votes": 1}]'},
    )
    lua_injection = await dispatch(
        FakeGS(),
        "queue_wc_votes",
        {"votes": '[{"hash": 1, "option": "1}; DoEvil() --", "target": 0, "votes": 1}]'},
    )
    bool_votes = await dispatch(
        FakeGS(),
        "queue_wc_votes",
        {"votes": [{"hash": 1, "option": 1, "target": 0, "votes": True}]},
    )
    fractional = await dispatch(
        FakeGS(),
        "queue_wc_votes",
        {"votes": [{"hash": 1, "option": 1.5, "target": 0, "votes": 1}]},
    )
    missing_hash = await dispatch(
        FakeGS(),
        "queue_wc_votes",
        {"votes": [{"option": 1, "target": 0, "votes": 1}]},
    )

    for text in (string_option, lua_injection, bool_votes, fractional, missing_hash):
        assert text.startswith("Error:")


@pytest.mark.asyncio
async def test_dispatch_queue_wc_votes_coerces_aliases_and_numeric_strings():
    calls = []

    class FakeGS:
        async def queue_wc_votes(self, votes):
            calls.append(votes)
            return "OK:VOTES_QUEUED"

    text = await dispatch(
        FakeGS(),
        "queue_wc_votes",
        {"votes": '[{"resolution_hash": "123", "option": "2", "target_index": 1}]'},
    )

    assert text == "OK:VOTES_QUEUED"
    assert calls == [[{"hash": 123, "option": 2, "target": 1, "votes": 5}]]


PARITY_READOUTS = ("get_spies", "get_strategic_map", "get_notifications")


def test_parity_readouts_registered():
    for name in PARITY_READOUTS:
        assert name in TOOL_REGISTRY, name
        assert TOOL_REGISTRY[name].verb == ""      # query tools carry no verb
        assert name in resolve_tools("full")


@pytest.mark.asyncio
async def test_parity_readouts_dispatch_to_gamestate():
    class GS:
        def __init__(self):
            self.called = []
        async def get_spies(self):
            self.called.append("spies"); return "2 spies"      # str passthrough
        async def get_strategic_map(self):
            self.called.append("smap"); return "fog report"
        async def get_notifications(self):
            self.called.append("notif"); return "3 notifications"

    gs = GS()
    for name in PARITY_READOUTS:
        out = await dispatch(gs, name, {})
        assert isinstance(out, str) and out
    assert gs.called == ["spies", "smap", "notif"]


@pytest.mark.asyncio
async def test_spy_action_routes_travel_vs_mission():
    class GS:
        def __init__(self):
            self.calls = []
        async def spy_travel(self, unit_index, x, y):
            self.calls.append(("travel", unit_index, x, y)); return "OK travel"
        async def spy_mission(self, unit_index, mission, x, y):
            self.calls.append(("mission", unit_index, mission, x, y)); return "OK mission"

    gs = GS()
    # composite id 65539 = player 1, unit index 3
    await dispatch(gs, "spy_action",
                   {"unit_id": 65539, "action": "travel", "target_x": 5, "target_y": 6})
    await dispatch(gs, "spy_action",
                   {"unit_id": 65539, "action": "SIPHON_FUNDS", "target_x": 5, "target_y": 6})
    assert gs.calls == [("travel", 3, 5, 6), ("mission", 3, "SIPHON_FUNDS", 5, 6)]


@pytest.mark.asyncio
async def test_parity_actions_dispatch_with_composite_ids():
    class GS:
        def __init__(self):
            self.calls = []
        async def change_government(self, government_type):
            self.calls.append(("gov", government_type)); return "OK"
        async def spread_religion(self, unit_index):
            self.calls.append(("spread", unit_index)); return "OK"
        async def activate_great_person(self, unit_index):
            self.calls.append(("gp", unit_index)); return "OK"

    gs = GS()
    await dispatch(gs, "change_government", {"government_type": "GOVERNMENT_OLIGARCHY"})
    await dispatch(gs, "spread_religion", {"unit_id": 131074})       # p2 idx2
    await dispatch(gs, "activate_great_person", {"unit_id": 131075})  # p2 idx3
    assert gs.calls == [("gov", "GOVERNMENT_OLIGARCHY"), ("spread", 2), ("gp", 3)]


def test_parity_actions_have_mirrored_verbs():
    from civ_mcp.arena.vocab import LOCAL_TOOL_VERBS
    for name in ("spy_action", "change_government", "spread_religion",
                 "activate_great_person"):
        assert TOOL_REGISTRY[name].verb == name
        assert LOCAL_TOOL_VERBS[name] == name
