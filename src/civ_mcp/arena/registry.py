from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Sequence

from civ_mcp import narrate as nr


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    params: dict[str, dict[str, Any]]
    required: tuple[str, ...]
    call: Callable[[Any, dict[str, Any]], Awaitable[str]]
    # Analysis verb for action tools (e.g. "move" for move_unit); "" for query
    # tools. The registry is the single source of truth for the tool->verb map;
    # arena.vocab.LOCAL_TOOL_VERBS mirrors it (test-enforced) to stay import-light.
    verb: str = ""


def _int_param(
    description: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> dict[str, Any]:
    param: dict[str, Any] = {"type": "integer", "description": description}
    if minimum is not None:
        param["minimum"] = minimum
    if maximum is not None:
        param["maximum"] = maximum
    return param


def _str_param(description: str) -> dict[str, str]:
    return {"type": "string", "description": description}


def _bool_param(description: str) -> dict[str, str]:
    return {"type": "boolean", "description": description}


def _object_param(description: str) -> dict[str, Any]:
    return {
        "type": "object",
        "description": description,
        "additionalProperties": {"type": "string"},
    }


async def _cities_text(gs: Any, args: dict[str, Any]) -> str:
    del args
    cities, distances = await gs.get_cities()
    return nr.narrate_cities(cities, distances)


async def _empire_resources_text(gs: Any, args: dict[str, Any]) -> str:
    del args
    stockpiles, owned, nearby, luxuries = await gs.get_empire_resources()
    return nr.narrate_empire_resources(stockpiles, owned, nearby, luxuries)


async def _builder_tasks_text(gs: Any, args: dict[str, Any]) -> str:
    del args
    tasks, builders = await gs.get_builder_tasks()
    return nr.narrate_builder_tasks(tasks, builders)


async def _pending_diplomacy_text(gs: Any, args: dict[str, Any]) -> str:
    del args
    return _render(await gs.get_diplomacy_sessions(), nr.narrate_diplomacy_sessions)


async def _pending_trades_text(gs: Any, args: dict[str, Any]) -> str:
    del args
    return _render(await gs.get_pending_deals(), nr.narrate_pending_deals)


async def _trade_options_text(gs: Any, args: dict[str, Any]) -> str:
    return _render(
        await gs.get_deal_options(args["other_player_id"]),
        nr.narrate_deal_options,
    )


async def _district_advisor_text(gs: Any, args: dict[str, Any]) -> str:
    result = await gs.get_district_advisor(args["city_id"], args["district_type"])
    if isinstance(result, str):
        return f"Error: {result}"
    narrated = nr.narrate_district_advisor(result, args["district_type"])
    warning = getattr(gs, "_advisor_budget_warning", None)
    if warning:
        setattr(gs, "_advisor_budget_warning", None)
        return f"!! {warning}\n\n{narrated}"
    return narrated


async def _wonder_advisor_text(gs: Any, args: dict[str, Any]) -> str:
    result = await gs.get_wonder_advisor(args["city_id"], args["wonder_name"])
    if isinstance(result, str):
        return f"Error: {result}"
    narrated = nr.narrate_wonder_advisor(result, args["wonder_name"])
    warning = getattr(gs, "_advisor_budget_warning", None)
    if warning:
        setattr(gs, "_advisor_budget_warning", None)
        return f"!! {warning}\n\n{narrated}"
    return narrated


async def _city_production_text(gs: Any, args: dict[str, Any]) -> str:
    return _render(
        await gs.list_city_production(args["city_id"]),
        nr.narrate_city_production,
    )


async def _global_settle_advisor_text(gs: Any, args: dict[str, Any]) -> str:
    del args
    return _render(await gs.get_global_settle_scan(), nr.narrate_settle_candidates)


async def _governors_text(gs: Any, args: dict[str, Any]) -> str:
    del args
    return _render(await gs.get_governors(), nr.narrate_governors)


async def _dedications_text(gs: Any, args: dict[str, Any]) -> str:
    del args
    return _render(await gs.get_dedications(), nr.narrate_dedications)


async def _religion_founding_status_text(gs: Any, args: dict[str, Any]) -> str:
    del args
    return _render(
        await gs.get_religion_founding_status(),
        nr.narrate_religion_founding_status,
    )


async def _religion_status_text(gs: Any, args: dict[str, Any]) -> str:
    del args
    return _render(await gs.get_religion_status(), nr.narrate_religion_status)


async def _trade_routes_text(gs: Any, args: dict[str, Any]) -> str:
    del args
    return _render(await gs.get_trade_routes(), nr.narrate_trade_routes)


async def _spies_text(gs: Any, args: dict[str, Any]) -> str:
    del args
    return _render(await gs.get_spies(), nr.narrate_spies)


async def _strategic_map_text(gs: Any, args: dict[str, Any]) -> str:
    del args
    return _render(await gs.get_strategic_map(), nr.narrate_strategic_map)


async def _notifications_text(gs: Any, args: dict[str, Any]) -> str:
    del args
    return _render(await gs.get_notifications(), nr.narrate_notifications)


async def _spy_action_text(gs: Any, args: dict[str, Any]) -> str:
    unit_index = _unit_index(args["unit_id"])
    action = str(args["action"])
    if action == "travel":
        return await gs.spy_travel(unit_index, args["target_x"], args["target_y"])
    return await gs.spy_mission(unit_index, action, args["target_x"], args["target_y"])


async def _change_government_text(gs: Any, args: dict[str, Any]) -> str:
    return await gs.change_government(args["government_type"])


async def _spread_religion_text(gs: Any, args: dict[str, Any]) -> str:
    return await gs.spread_religion(_unit_index(args["unit_id"]))


async def _activate_great_person_text(gs: Any, args: dict[str, Any]) -> str:
    return await gs.activate_great_person(_unit_index(args["unit_id"]))


def _unit_index(unit_id: Any) -> int:
    """Composite unit_id -> unit_index, mirroring GameState's own convention."""
    return int(unit_id) % 65536


async def _trade_destinations_text(gs: Any, args: dict[str, Any]) -> str:
    unit_index = _unit_index(args["unit_id"])
    return _render(
        await gs.get_trade_destinations(unit_index),
        nr.narrate_trade_destinations,
    )


async def _gp_advisor_text(gs: Any, args: dict[str, Any]) -> str:
    unit_index = _unit_index(args["unit_id"])
    result = await gs.get_gp_advisor(unit_index)
    if result is None:
        return "Error: no Great Person advisor data for that unit."
    if isinstance(result, str):
        return result
    return nr.narrate_gp_advisor(result)


async def _world_congress_text(gs: Any, args: dict[str, Any]) -> str:
    del args
    return _render(await gs.get_world_congress(), nr.narrate_world_congress)


async def _start_trade_route_text(gs: Any, args: dict[str, Any]) -> str:
    unit_index = _unit_index(args["unit_id"])
    return await gs.make_trade_route(unit_index, args["target_x"], args["target_y"])


async def _teleport_trader_text(gs: Any, args: dict[str, Any]) -> str:
    unit_index = _unit_index(args["unit_id"])
    return await gs.teleport_to_city(unit_index, args["target_x"], args["target_y"])


async def _queue_wc_votes_text(gs: Any, args: dict[str, Any]) -> str:
    raw = args["votes"]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return "Error: votes must be valid JSON."
    else:
        parsed = raw
    if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
        return 'Error: votes must be a JSON list of vote objects, e.g. [{"hash": H, "option": 1, "target": 0, "votes": N}].'
    # Coerce every field to a real int before it reaches the Lua generator:
    # build_register_wc_voter interpolates these values bare into a Lua table
    # literal, so a stray string would splice into the Lua source (or, e.g.
    # option="B", silently evaluate to nil and fall back to option A).
    coerced = []
    for item in parsed:
        clean: dict[str, int] = {}
        for field, aliases, default in (
            ("hash", ("hash", "resolution_hash"), None),
            ("option", ("option",), 1),
            ("target", ("target", "target_index"), 0),
            ("votes", ("votes", "num_votes"), 5),
        ):
            value = next((item[a] for a in aliases if a in item), default)
            if value is None:
                return f'Error: each vote object needs an integer "{field}".'
            if isinstance(value, bool):
                return f'Error: vote field "{field}" must be an integer, got {value!r}.'
            try:
                int_value = int(value)
            except (TypeError, ValueError):
                return f'Error: vote field "{field}" must be an integer, got {value!r}.'
            if isinstance(value, float) and value != int_value:
                return f'Error: vote field "{field}" must be an integer, got {value!r}.'
            clean[field] = int_value
        coerced.append(clean)
    return await gs.queue_wc_votes(coerced)


# Must match the actions server.py city_action and lua/cities.py accept --
# "reject" (free a disloyal loyalty-flip city) was missing, so an arena puppet
# could never resolve a loyalty flip.
_CITY_CAPTURE_ACTIONS = ("keep", "reject", "raze", "liberate_founder", "liberate_previous")


async def _resolve_city_capture_text(gs: Any, args: dict[str, Any]) -> str:
    action = str(args["action"]).lower()
    if action not in _CITY_CAPTURE_ACTIONS:
        return f"Error: action must be one of {', '.join(_CITY_CAPTURE_ACTIONS)}."
    return await gs.resolve_city_capture(action)


def _tool(
    name: str,
    description: str,
    params: dict[str, dict[str, Any]] | None,
    required: Sequence[str],
    call: Callable[[Any, dict[str, Any]], Awaitable[str]],
    *,
    verb: str = "",
) -> ToolDef:
    return ToolDef(
        name=name,
        description=description,
        params=params or {},
        required=tuple(required),
        call=call,
        verb=verb,
    )


def _render(data: Any, narrator: Callable[[Any], str]) -> str:
    if isinstance(data, str):
        return data
    return narrator(data)


def _coerce_policy_assignments(assignments: dict[Any, str]) -> dict[int, str]:
    return {int(slot): policy for slot, policy in assignments.items()}


def _strict_bool(value: Any, name: str) -> tuple[bool, str | None]:
    if isinstance(value, bool):
        return value, None
    return False, f"Error: {name} must be boolean"


async def _respond_to_trade_text(gs: Any, args: dict[str, Any]) -> str:
    accept, error = _strict_bool(args["accept"], "accept")
    if error:
        return error
    return await gs.respond_to_deal(args["other_player_id"], accept)


def _positive_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(parsed, 0)


def _resource_items(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    return [
        {"type": "RESOURCE", "name": res, "amount": 1, "duration": 30}
        for res in (part.strip() for part in str(raw).split(","))
        if res
    ]


def _optional_bool_arg(args: dict[str, Any], name: str) -> tuple[bool, str | None]:
    if name not in args:
        return False, None
    return _strict_bool(args[name], name)


def _build_trade_items(
    args: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    offer_items: list[dict[str, Any]] = []
    request_items: list[dict[str, Any]] = []

    offer_gold = _positive_int(args.get("offer_gold", 0))
    if offer_gold:
        offer_items.append({"type": "GOLD", "amount": offer_gold, "duration": 0})

    offer_gpt = _positive_int(args.get("offer_gold_per_turn", 0))
    if offer_gpt:
        offer_items.append({"type": "GOLD", "amount": offer_gpt, "duration": 30})

    offer_items.extend(_resource_items(args.get("offer_resources", "")))

    offer_favor = _positive_int(args.get("offer_favor", 0))
    if offer_favor:
        offer_items.append({"type": "FAVOR", "amount": offer_favor})

    offer_open_borders, error = _optional_bool_arg(args, "offer_open_borders")
    if error:
        return [], [], error
    if offer_open_borders:
        offer_items.append({"type": "AGREEMENT", "subtype": "OPEN_BORDERS"})

    request_gold = _positive_int(args.get("request_gold", 0))
    if request_gold:
        request_items.append({"type": "GOLD", "amount": request_gold, "duration": 0})

    request_gpt = _positive_int(args.get("request_gold_per_turn", 0))
    if request_gpt:
        request_items.append({"type": "GOLD", "amount": request_gpt, "duration": 30})

    request_items.extend(_resource_items(args.get("request_resources", "")))

    request_favor = _positive_int(args.get("request_favor", 0))
    if request_favor:
        request_items.append({"type": "FAVOR", "amount": request_favor})

    request_open_borders, error = _optional_bool_arg(args, "request_open_borders")
    if error:
        return [], [], error
    if request_open_borders:
        request_items.append({"type": "AGREEMENT", "subtype": "OPEN_BORDERS"})

    if _positive_int(args.get("joint_war_target", 0)):
        offer_items.append({"type": "AGREEMENT", "subtype": "JOINT_WAR"})
        request_items.append({"type": "AGREEMENT", "subtype": "JOINT_WAR"})

    return offer_items, request_items, None


async def _propose_trade_text(gs: Any, args: dict[str, Any]) -> str:
    offer_items, request_items, error = _build_trade_items(args)
    if error:
        return error
    if not offer_items and not request_items:
        return "Error: must specify at least one offer or request item"

    mode = str(args.get("mode", "test")).lower()
    if mode == "test":
        return await gs.test_trade(args["other_player_id"], offer_items, request_items)
    if mode == "send":
        return await gs.propose_trade(args["other_player_id"], offer_items, request_items)
    # Hardening over the MCP wrapper: typos must not accidentally commit a deal.
    return 'Error: mode must be "test" or "send"'


_MAP_RADIUS_DEFAULT = 2
_MAP_RADIUS_MIN = 0
_MAP_RADIUS_MAX = 5


def _clamp_map_radius(value: Any) -> int:
    # Models can emit radius:null or a non-numeric value; fall back to the default
    # instead of raising (which would surface as an ERROR tool result upstream).
    try:
        radius = int(value)
    except (TypeError, ValueError):
        radius = _MAP_RADIUS_DEFAULT
    return max(_MAP_RADIUS_MIN, min(radius, _MAP_RADIUS_MAX))


TOOL_REGISTRY: dict[str, ToolDef] = {
    "get_overview": _tool(
        "get_overview",
        "Empire and turn overview for your civilization.",
        None,
        (),
        lambda gs, args: _narrate_overview(gs, args),
    ),
    "get_units": _tool(
        "get_units",
        "List your units with positions and movement.",
        None,
        (),
        lambda gs, args: _narrate_units(gs, args),
    ),
    "get_cities": _tool(
        "get_cities",
        "List your cities with yields and production state.",
        None,
        (),
        _cities_text,
    ),
    "move_unit": _tool(
        "move_unit",
        "Move a unit toward a target tile.",
        {
            "unit_index": _int_param("Unit index from get_units."),
            "x": _int_param("Target X coordinate."),
            "y": _int_param("Target Y coordinate."),
        },
        ("unit_index", "x", "y"),
        lambda gs, args: gs.move_unit(args["unit_index"], args["x"], args["y"]),
        verb="move",
    ),
    "found_city": _tool(
        "found_city",
        "Found a city with a settler.",
        {"unit_index": _int_param("Settler unit_index from get_units.")},
        ("unit_index",),
        lambda gs, args: gs.found_city(args["unit_index"]),
        verb="found_city",
    ),
    "set_city_production": _tool(
        "set_city_production",
        "Set a city's production queue item.",
        {
            "city_id": _int_param("City ID from get_cities."),
            "item_type": _str_param("UNIT, BUILDING, DISTRICT, or PROJECT."),
            "item_name": _str_param("Production item type name."),
            "target_x": _int_param("Optional placement X coordinate."),
            "target_y": _int_param("Optional placement Y coordinate."),
        },
        ("city_id", "item_type", "item_name"),
        lambda gs, args: gs.set_city_production(
            args["city_id"],
            args["item_type"],
            args["item_name"],
            args.get("target_x"),
            args.get("target_y"),
        ),
    ),
    "set_research": _tool(
        "set_research",
        "Choose the current technology research.",
        {"tech": _str_param("Technology type, for example TECH_MINING.")},
        ("tech",),
        lambda gs, args: gs.set_research(args["tech"]),
    ),
    "fortify_unit": _tool(
        "fortify_unit",
        "Fortify a military unit.",
        {"unit_index": _int_param("Unit index from get_units.")},
        ("unit_index",),
        lambda gs, args: gs.fortify_unit(args["unit_index"]),
        verb="fortify",
    ),
    "skip_unit": _tool(
        "skip_unit",
        "Skip a unit for this turn.",
        {"unit_index": _int_param("Unit index from get_units.")},
        ("unit_index",),
        lambda gs, args: gs.skip_unit(args["unit_index"]),
        verb="skip",
    ),
    "get_map_area": _tool(
        "get_map_area",
        "Inspect terrain and units around a map coordinate.",
        {
            "x": _int_param("Center X coordinate."),
            "y": _int_param("Center Y coordinate."),
            "radius": _int_param(
                f"Search radius; defaults to {_MAP_RADIUS_DEFAULT} and is clamped "
                f"to {_MAP_RADIUS_MIN}..{_MAP_RADIUS_MAX}.",
                minimum=_MAP_RADIUS_MIN,
                maximum=_MAP_RADIUS_MAX,
            ),
        },
        ("x", "y"),
        lambda gs, args: _narrate_map(gs, args),
    ),
    "get_tech_civics": _tool(
        "get_tech_civics",
        "Show current technology and civic options.",
        None,
        (),
        lambda gs, args: _narrate_tech_civics(gs, args),
    ),
    "attack_unit": _tool(
        "attack_unit",
        "Attack a target tile with a unit.",
        {
            "unit_index": _int_param("Attacking unit index."),
            "x": _int_param("Target X coordinate."),
            "y": _int_param("Target Y coordinate."),
        },
        ("unit_index", "x", "y"),
        lambda gs, args: gs.attack_unit(args["unit_index"], args["x"], args["y"]),
        verb="attack",
    ),
    "improve_tile": _tool(
        "improve_tile",
        "Build an improvement on the current tile.",
        {
            "unit_index": _int_param("Builder unit index."),
            "improvement_name": _str_param("Improvement type to build."),
        },
        ("unit_index", "improvement_name"),
        lambda gs, args: gs.improve_tile(args["unit_index"], args["improvement_name"]),
        verb="improve",
    ),
    "remove_feature": _tool(
        "remove_feature",
        "Remove a feature from the current tile.",
        {"unit_index": _int_param("Builder unit index.")},
        ("unit_index",),
        lambda gs, args: gs.remove_feature(args["unit_index"]),
        verb="remove_feature",
    ),
    "purchase_item": _tool(
        "purchase_item",
        "Purchase a unit or building with gold or faith.",
        {
            "city_id": _int_param("City ID from get_cities."),
            "item_type": _str_param("UNIT or BUILDING."),
            "item_name": _str_param("Item type name."),
            "yield_type": _str_param("YIELD_GOLD or YIELD_FAITH."),
        },
        ("city_id", "item_type", "item_name"),
        lambda gs, args: gs.purchase_item(
            args["city_id"],
            args["item_type"],
            args["item_name"],
            args.get("yield_type", "YIELD_GOLD"),
        ),
        verb="purchase",
    ),
    "heal_unit": _tool(
        "heal_unit",
        "Heal a unit until fully recovered.",
        {"unit_index": _int_param("Unit index from get_units.")},
        ("unit_index",),
        lambda gs, args: gs.heal_unit(args["unit_index"]),
        verb="heal",
    ),
    "alert_unit": _tool(
        "alert_unit",
        "Put a unit on alert.",
        {"unit_index": _int_param("Unit index from get_units.")},
        ("unit_index",),
        lambda gs, args: gs.alert_unit(args["unit_index"]),
        verb="alert",
    ),
    "set_civic": _tool(
        "set_civic",
        "Choose the current civic research.",
        {"civic_name": _str_param("Civic type, for example CIVIC_FOREIGN_TRADE.")},
        ("civic_name",),
        lambda gs, args: gs.set_civic(args["civic_name"]),
        verb="set_civic",
    ),
    "get_settle_advisor": _tool(
        "get_settle_advisor",
        "Recommend nearby settle locations for a settler.",
        {"unit_index": _int_param("Settler unit index.")},
        ("unit_index",),
        lambda gs, args: gs.get_settle_advisor(args["unit_index"]),
    ),
    "get_district_advisor": _tool(
        "get_district_advisor",
        "Rank district placement tiles for a city.",
        {
            "city_id": _int_param("City ID from get_cities."),
            "district_type": _str_param("District type, for example DISTRICT_CAMPUS."),
        },
        ("city_id", "district_type"),
        _district_advisor_text,
    ),
    "get_wonder_advisor": _tool(
        "get_wonder_advisor",
        "Rank wonder placement tiles for a city.",
        {
            "city_id": _int_param("City ID from get_cities."),
            "wonder_name": _str_param("Wonder building type."),
        },
        ("city_id", "wonder_name"),
        _wonder_advisor_text,
    ),
    "get_builder_tasks": _tool(
        "get_builder_tasks",
        "Show prioritized builder work across the empire.",
        None,
        (),
        _builder_tasks_text,
    ),
    "get_diplomacy": _tool(
        "get_diplomacy",
        "Show diplomatic relationships with known civilizations.",
        None,
        (),
        lambda gs, args: _narrate_diplomacy(gs, args),
    ),
    "get_pending_diplomacy": _tool(
        "get_pending_diplomacy",
        "Check for pending diplomacy encounters that can block turn progression.",
        None,
        (),
        _pending_diplomacy_text,
    ),
    "get_pending_trades": _tool(
        "get_pending_trades",
        "Check for pending incoming trade deal offers.",
        None,
        (),
        _pending_trades_text,
    ),
    "get_trade_options": _tool(
        "get_trade_options",
        "See what both sides can trade with another civilization.",
        {"other_player_id": _int_param("Player ID from get_diplomacy.")},
        ("other_player_id",),
        _trade_options_text,
    ),
    "get_city_states": _tool(
        "get_city_states",
        "Show city-state envoy and suzerainty status.",
        None,
        (),
        lambda gs, args: _narrate_city_states(gs, args),
    ),
    "get_great_people": _tool(
        "get_great_people",
        "Show current Great People and recruitment progress.",
        None,
        (),
        lambda gs, args: _narrate_great_people(gs, args),
    ),
    "get_empire_resources": _tool(
        "get_empire_resources",
        "Summarize owned and nearby empire resources.",
        None,
        (),
        _empire_resources_text,
    ),
    "get_victory_progress": _tool(
        "get_victory_progress",
        "Show progress toward all victory conditions.",
        None,
        (),
        lambda gs, args: _narrate_victory_progress(gs, args),
    ),
    "get_pathing_estimate": _tool(
        "get_pathing_estimate",
        "Estimate the turns needed to reach a destination.",
        {
            "unit_index": _int_param("Unit index from get_units."),
            "x": _int_param("Target X coordinate."),
            "y": _int_param("Target Y coordinate."),
        },
        ("unit_index", "x", "y"),
        lambda gs, args: _narrate_pathing_estimate(gs, args),
    ),
    "send_envoy": _tool(
        "send_envoy",
        "Send an envoy to a city-state.",
        {"city_state_player_id": _int_param("City-state player ID.")},
        ("city_state_player_id",),
        lambda gs, args: gs.send_envoy(args["city_state_player_id"]),
        verb="send_envoy",
    ),
    "get_policies": _tool(
        "get_policies",
        "Show the current government and policy cards.",
        None,
        (),
        lambda gs, args: _narrate_policies(gs, args),
    ),
    "set_policies": _tool(
        "set_policies",
        "Assign policy cards to policy slots.",
        {"assignments": _object_param("Mapping from slot index to policy type.")},
        ("assignments",),
        lambda gs, args: gs.set_policies(
            _coerce_policy_assignments(args["assignments"])
        ),
        verb="set_policies",
    ),
    "appoint_governor": _tool(
        "appoint_governor",
        "Appoint a new governor.",
        {"governor_type": _str_param("Governor type name.")},
        ("governor_type",),
        lambda gs, args: gs.appoint_governor(args["governor_type"]),
        verb="appoint_governor",
    ),
    "assign_governor": _tool(
        "assign_governor",
        "Assign a governor to a city.",
        {
            "governor_type": _str_param("Governor type name."),
            "city_id": _int_param("City ID from get_cities."),
        },
        ("governor_type", "city_id"),
        lambda gs, args: gs.assign_governor(args["governor_type"], args["city_id"]),
        verb="assign_governor",
    ),
    "choose_pantheon": _tool(
        "choose_pantheon",
        "Choose a pantheon belief.",
        {"belief_type": _str_param("Pantheon belief type.")},
        ("belief_type",),
        lambda gs, args: gs.choose_pantheon(args["belief_type"]),
        verb="choose_pantheon",
    ),
    "get_pantheon_status": _tool(
        "get_pantheon_status",
        "Show current pantheon status and available beliefs.",
        None,
        (),
        lambda gs, args: _narrate_pantheon_status(gs, args),
    ),
    "upgrade_unit": _tool(
        "upgrade_unit",
        "Upgrade a unit to its next type.",
        {"unit_id": _int_param("Composite unit ID from get_units.")},
        ("unit_id",),
        lambda gs, args: gs.upgrade_unit(args["unit_id"]),
        verb="upgrade",
    ),
    "promote_unit": _tool(
        "promote_unit",
        "Apply a unit promotion.",
        {
            "unit_id": _int_param("Composite unit ID from get_units."),
            "promotion_type": _str_param("Promotion type name."),
        },
        ("unit_id", "promotion_type"),
        lambda gs, args: gs.promote_unit(args["unit_id"], args["promotion_type"]),
        verb="promote",
    ),
    "get_unit_promotions": _tool(
        "get_unit_promotions",
        "Show available promotions for a unit.",
        {"unit_id": _int_param("Composite unit ID from get_units.")},
        ("unit_id",),
        lambda gs, args: _narrate_unit_promotions(gs, args),
    ),
    "automate_explore": _tool(
        "automate_explore",
        "Set a scout to automated exploration.",
        {"unit_index": _int_param("Scout unit index.")},
        ("unit_index",),
        lambda gs, args: gs.automate_explore(args["unit_index"]),
        verb="automate",
    ),
    "skip_remaining_units": _tool(
        "skip_remaining_units",
        "Skip all units that still have movement.",
        None,
        (),
        lambda gs, args: _skip_remaining_units(gs, args),
        verb="skip",
    ),
    "purchase_tile": _tool(
        "purchase_tile",
        "Purchase a tile for a city with gold.",
        {
            "city_id": _int_param("City ID from get_cities."),
            "x": _int_param("Tile X coordinate."),
            "y": _int_param("Tile Y coordinate."),
        },
        ("city_id", "x", "y"),
        lambda gs, args: gs.purchase_tile(args["city_id"], args["x"], args["y"]),
        verb="purchase_tile",
    ),
    "get_purchasable_tiles": _tool(
        "get_purchasable_tiles",
        "Show tiles a city can purchase.",
        {"city_id": _int_param("City ID from get_cities.")},
        ("city_id",),
        lambda gs, args: _narrate_purchasable_tiles(gs, args),
    ),
    "set_city_focus": _tool(
        "set_city_focus",
        "Change a city's yield focus.",
        {
            "city_id": _int_param("City ID from get_cities."),
            "focus": _str_param("City focus name, for example FOOD."),
        },
        ("city_id", "focus"),
        lambda gs, args: gs.set_city_focus(args["city_id"], args["focus"]),
        verb="set_city_focus",
    ),
    "respond_to_diplomacy": _tool(
        "respond_to_diplomacy",
        "Respond to a pending diplomacy encounter with POSITIVE or NEGATIVE.",
        {
            "other_player_id": _int_param("Player ID from get_pending_diplomacy."),
            "response": _str_param("POSITIVE or NEGATIVE."),
        },
        ("other_player_id", "response"),
        lambda gs, args: gs.diplomacy_respond(args["other_player_id"], args["response"]),
        verb="respond_to_diplomacy",
    ),
    "respond_to_trade": _tool(
        "respond_to_trade",
        "Accept or reject a pending incoming trade deal.",
        {
            "other_player_id": _int_param("Player ID from get_pending_trades."),
            "accept": _bool_param("True to accept; false to reject."),
        },
        ("other_player_id", "accept"),
        _respond_to_trade_text,
        verb="respond_to_trade",
    ),
    "propose_trade": _tool(
        "propose_trade",
        "Propose or preview a trade deal with another civilization.",
        {
            "other_player_id": _int_param("Player ID from get_diplomacy."),
            "offer_gold": _int_param("Lump-sum gold to offer."),
            "offer_gold_per_turn": _int_param("Gold per turn to offer for 30 turns."),
            "offer_resources": _str_param("Comma-separated resource types to offer, for example RESOURCE_SILK."),
            "offer_favor": _int_param("Diplomatic Favor to offer."),
            "offer_open_borders": _bool_param("Offer our open borders."),
            "request_gold": _int_param("Lump-sum gold to request."),
            "request_gold_per_turn": _int_param("Gold per turn to request for 30 turns."),
            "request_resources": _str_param("Comma-separated resource types to request."),
            "request_favor": _int_param("Diplomatic Favor to request."),
            "request_open_borders": _bool_param("Request their open borders."),
            "joint_war_target": _int_param("Third-party player ID for joint war; 0 for none."),
            "mode": _str_param('"test" previews AI counter-offer; "send" commits the deal.'),
        },
        ("other_player_id",),
        _propose_trade_text,
        verb="propose_trade",
    ),
    "propose_peace": _tool(
        "propose_peace",
        "Propose white peace to a civilization you are at war with.",
        {"other_player_id": _int_param("Player ID from get_diplomacy.")},
        ("other_player_id",),
        lambda gs, args: gs.propose_peace(args["other_player_id"]),
        verb="propose_peace",
    ),
    "send_diplomatic_action": _tool(
        "send_diplomatic_action",
        "Send a proactive diplomatic action such as delegation, friendship, embassy, denouncement, open borders, or war declaration.",
        {
            "other_player_id": _int_param("Player ID from get_diplomacy."),
            "action": _str_param(
                "One of: DIPLOMATIC_DELEGATION, DECLARE_FRIENDSHIP, DENOUNCE, "
                "RESIDENT_EMBASSY, OPEN_BORDERS, DECLARE_SURPRISE_WAR, "
                "DECLARE_FORMAL_WAR, DECLARE_HOLY_WAR, DECLARE_LIBERATION_WAR, "
                "DECLARE_RECONQUEST_WAR, DECLARE_PROTECTORATE_WAR, "
                "DECLARE_COLONIAL_WAR, DECLARE_TERRITORIAL_WAR. "
                "OPEN_BORDERS is routed through the trade API as mutual open borders."
            ),
        },
        ("other_player_id", "action"),
        lambda gs, args: gs.send_diplomatic_action(args["other_player_id"], args["action"].upper()),
        verb="send_diplomatic_action",
    ),
    "form_alliance": _tool(
        "form_alliance",
        "Form an alliance with another civilization after friendship and Diplomatic Service.",
        {
            "other_player_id": _int_param("Player ID from get_diplomacy."),
            "alliance_type": _str_param("MILITARY, RESEARCH, CULTURAL, ECONOMIC, or RELIGIOUS."),
        },
        ("other_player_id", "alliance_type"),
        lambda gs, args: gs.form_alliance(args["other_player_id"], args["alliance_type"].upper()),
        verb="form_alliance",
    ),
    "get_city_production": _tool(
        "get_city_production",
        "List producible units, buildings, districts, and projects for a city.",
        {"city_id": _int_param("City ID from get_cities.")},
        ("city_id",),
        _city_production_text,
    ),
    "get_global_settle_advisor": _tool(
        "get_global_settle_advisor",
        "Recommend the best remaining settle locations across the revealed map.",
        None,
        (),
        _global_settle_advisor_text,
    ),
    "get_governors": _tool(
        "get_governors",
        "Show governor appointment, assignment, and promotion status.",
        None,
        (),
        _governors_text,
    ),
    "get_dedications": _tool(
        "get_dedications",
        "Show available era dedications.",
        None,
        (),
        _dedications_text,
    ),
    "get_religion_beliefs": _tool(
        "get_religion_beliefs",
        "Show religion founding status and available beliefs.",
        None,
        (),
        _religion_founding_status_text,
    ),
    "get_religion_spread": _tool(
        "get_religion_spread",
        "Show religion spread and majority status across civilizations.",
        None,
        (),
        _religion_status_text,
    ),
    "get_trade_routes": _tool(
        "get_trade_routes",
        "Show active trade routes and idle traders.",
        None,
        (),
        _trade_routes_text,
    ),
    "get_trade_destinations": _tool(
        "get_trade_destinations",
        "Show available trade route destinations for a trader unit.",
        {"unit_id": _int_param("Composite unit ID from get_units.")},
        ("unit_id",),
        _trade_destinations_text,
    ),
    "get_gp_advisor": _tool(
        "get_gp_advisor",
        "Rank cities to activate a recruited Great Person unit.",
        {"unit_id": _int_param("Composite unit ID from get_units.")},
        ("unit_id",),
        _gp_advisor_text,
    ),
    "get_world_congress": _tool(
        "get_world_congress",
        "Show pending World Congress resolutions and voting status.",
        None,
        (),
        _world_congress_text,
    ),
    "promote_governor": _tool(
        "promote_governor",
        "Apply a promotion to an appointed governor.",
        {
            "governor_type": _str_param("Governor type name."),
            "promotion_type": _str_param("Promotion type name."),
        },
        ("governor_type", "promotion_type"),
        lambda gs, args: gs.promote_governor(args["governor_type"], args["promotion_type"]),
        verb="promote_governor",
    ),
    "choose_dedication": _tool(
        "choose_dedication",
        "Choose an era dedication.",
        {"dedication_index": _int_param("Dedication index from get_dedications.")},
        ("dedication_index",),
        lambda gs, args: gs.choose_dedication(args["dedication_index"]),
        verb="choose_dedication",
    ),
    "found_religion": _tool(
        "found_religion",
        "Found a religion with a follower and founder belief.",
        {
            "religion_name": _str_param("Religion type, for example RELIGION_BUDDHISM."),
            "follower_belief": _str_param("Follower belief type."),
            "founder_belief": _str_param("Founder belief type."),
        },
        ("religion_name", "follower_belief", "founder_belief"),
        lambda gs, args: gs.found_religion(
            args["religion_name"], args["follower_belief"], args["founder_belief"]
        ),
        verb="found_religion",
    ),
    "recruit_great_person": _tool(
        "recruit_great_person",
        "Recruit a Great Person candidate using accumulated points.",
        {"individual_id": _int_param("Great Person individual ID from get_great_people.")},
        ("individual_id",),
        lambda gs, args: gs.recruit_great_person(args["individual_id"]),
        verb="recruit_great_person",
    ),
    "patronize_great_person": _tool(
        "patronize_great_person",
        "Instantly buy a Great Person with gold or faith.",
        {
            "individual_id": _int_param("Great Person individual ID from get_great_people."),
            "yield_type": _str_param("YIELD_GOLD or YIELD_FAITH."),
        },
        ("individual_id",),
        lambda gs, args: gs.patronize_great_person(
            args["individual_id"], args.get("yield_type", "YIELD_GOLD")
        ),
        verb="patronize_great_person",
    ),
    "reject_great_person": _tool(
        "reject_great_person",
        "Pass on a Great Person candidate, advancing to the next in that class.",
        {"individual_id": _int_param("Great Person individual ID from get_great_people.")},
        ("individual_id",),
        lambda gs, args: gs.reject_great_person(args["individual_id"]),
        verb="reject_great_person",
    ),
    "start_trade_route": _tool(
        "start_trade_route",
        "Start a trade route from a trader to a destination city.",
        {
            "unit_id": _int_param("Composite trader unit ID from get_units."),
            "target_x": _int_param("Destination city X coordinate."),
            "target_y": _int_param("Destination city Y coordinate."),
        },
        ("unit_id", "target_x", "target_y"),
        _start_trade_route_text,
        verb="start_trade_route",
    ),
    "teleport_trader": _tool(
        "teleport_trader",
        "Teleport an idle trader to a city.",
        {
            "unit_id": _int_param("Composite trader unit ID from get_units."),
            "target_x": _int_param("Destination city X coordinate."),
            "target_y": _int_param("Destination city Y coordinate."),
        },
        ("unit_id", "target_x", "target_y"),
        _teleport_trader_text,
        verb="teleport_trader",
    ),
    "queue_wc_votes": _tool(
        "queue_wc_votes",
        "Register World Congress vote preferences before ending the turn the Congress fires.",
        {
            "votes": _str_param(
                'JSON array of vote objects: [{"hash": H, "option": 1, "target": 0, "votes": N}].'
            )
        },
        ("votes",),
        _queue_wc_votes_text,
        verb="queue_wc_votes",
    ),
    "city_attack": _tool(
        "city_attack",
        "Attack a target tile with a city's ranged attack.",
        {
            "city_id": _int_param("City ID from get_cities."),
            "target_x": _int_param("Target X coordinate."),
            "target_y": _int_param("Target Y coordinate."),
        },
        ("city_id", "target_x", "target_y"),
        lambda gs, args: gs.city_attack(args["city_id"], args["target_x"], args["target_y"]),
        verb="city_attack",
    ),
    "resolve_city_capture": _tool(
        "resolve_city_capture",
        "Resolve a captured or disloyal city: keep, raze, liberate, or reject.",
        {"action": _str_param(
            "One of: keep, raze, liberate_founder, liberate_previous, reject. "
            "Use reject to decline a city gained via loyalty flip (frees it) "
            "instead of keeping it."
        )},
        ("action",),
        _resolve_city_capture_text,
        verb="resolve_city_capture",
    ),
    "get_spies": _tool(
        "get_spies",
        "List your spy units: composite id, position, rank, city, and which "
        "operations are available where they stand. Offensive missions need the "
        "spy physically in the target city (spy_action travel first).",
        None,
        (),
        _spies_text,
    ),
    "get_strategic_map": _tool(
        "get_strategic_map",
        "Empire-level map summary: fog coverage per city and unclaimed nearby "
        "resources. Use every ~30 turns to spot expansion gaps.",
        None,
        (),
        _strategic_map_text,
    ),
    "get_notifications": _tool(
        "get_notifications",
        "Current game notifications (the bell items a human sees): what needs "
        "attention this turn.",
        None,
        (),
        _notifications_text,
    ),
    "spy_action": _tool(
        "spy_action",
        "Send a spy to a city (action='travel') or launch a mission (action = "
        "COUNTERSPY, GAIN_SOURCES, SIPHON_FUNDS, STEAL_TECH_BOOST, "
        "SABOTAGE_PRODUCTION, GREAT_WORK_HEIST, RECRUIT_PARTISANS, "
        "NEUTRALIZE_GOVERNOR, FABRICATE_SCANDAL). The spy must already be IN the "
        "target city for missions: travel first, end turn, then launch.",
        {
            "unit_id": _int_param("Spy composite id from get_spies"),
            "action": _str_param("'travel' or a mission type"),
            "target_x": _int_param("Target city tile X"),
            "target_y": _int_param("Target city tile Y"),
        },
        ("unit_id", "action", "target_x", "target_y"),
        _spy_action_text,
        verb="spy_action",
    ),
    "change_government": _tool(
        "change_government",
        "Switch to a new government (e.g. GOVERNMENT_OLIGARCHY). The first switch "
        "after unlocking a tier is free.",
        {"government_type": _str_param("GOVERNMENT_* type id")},
        ("government_type",),
        _change_government_text,
        verb="change_government",
    ),
    "spread_religion": _tool(
        "spread_religion",
        "Spend a missionary/apostle charge to spread its religion to the city it "
        "stands in or adjacent to.",
        {"unit_id": _int_param("Religious unit composite id from get_units")},
        ("unit_id",),
        _spread_religion_text,
        verb="spread_religion",
    ),
    "activate_great_person": _tool(
        "activate_great_person",
        "Activate a Great Person standing on its matching completed district. "
        "The error message lists requirements if activation fails.",
        {"unit_id": _int_param("Great Person composite id from get_units")},
        ("unit_id",),
        _activate_great_person_text,
        verb="activate_great_person",
    ),
}


TIERS: dict[str, tuple[str, ...]] = {
    "minimal": (
        "get_overview",
        "get_units",
        "get_cities",
        "move_unit",
        "found_city",
        "set_city_production",
        "set_research",
        "fortify_unit",
        "skip_unit",
    ),
    "standard": (
        "get_overview",
        "get_units",
        "get_cities",
        "move_unit",
        "found_city",
        "set_city_production",
        "set_research",
        "fortify_unit",
        "skip_unit",
        "get_map_area",
        "get_tech_civics",
        "attack_unit",
        "improve_tile",
        "remove_feature",
        "purchase_item",
        "heal_unit",
        "alert_unit",
        "set_civic",
    ),
    "full": tuple(TOOL_REGISTRY),
}


def resolve_tools(selector: str | Sequence[str]) -> tuple[str, ...]:
    if isinstance(selector, str):
        if selector == "full":
            return tuple(TOOL_REGISTRY)
        if selector in TIERS:
            return TIERS[selector]
        if selector in TOOL_REGISTRY:
            return (selector,)
        raise ValueError(f"Unknown tool tier or tool: {selector}")

    names = tuple(selector)
    unknown = [name for name in names if name not in TOOL_REGISTRY]
    if unknown:
        raise ValueError(f"Unknown tools: {unknown}")
    return names


def openai_tools(names: Sequence[str]) -> list[dict[str, Any]]:
    resolved = resolve_tools(names)
    tools = []
    for name in resolved:
        tool = TOOL_REGISTRY[name]
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": {
                        "type": "object",
                        "properties": tool.params,
                        "required": list(tool.required),
                    },
                },
            }
        )
    return tools


async def dispatch(
    gs: Any,
    name: str,
    args: dict[str, Any],
    allowed: Sequence[str] | None = None,
) -> str:
    if allowed is not None and name not in allowed:
        raise KeyError(name)
    tool = TOOL_REGISTRY[name]
    return await tool.call(gs, args)


async def _narrate_overview(gs: Any, args: dict[str, Any]) -> str:
    del args
    return _render(await gs.get_game_overview(), nr.narrate_overview)


async def _narrate_units(gs: Any, args: dict[str, Any]) -> str:
    del args
    return _render(await gs.get_units(), nr.narrate_units)


async def _narrate_map(gs: Any, args: dict[str, Any]) -> str:
    radius = _clamp_map_radius(args.get("radius", _MAP_RADIUS_DEFAULT))
    return _render(
        await gs.get_map_area(args["x"], args["y"], radius),
        nr.narrate_map,
    )


async def _narrate_tech_civics(gs: Any, args: dict[str, Any]) -> str:
    del args
    return _render(await gs.get_tech_civics(), nr.narrate_tech_civics)


async def _narrate_diplomacy(gs: Any, args: dict[str, Any]) -> str:
    del args
    return _render(await gs.get_diplomacy(), nr.narrate_diplomacy)


async def _narrate_city_states(gs: Any, args: dict[str, Any]) -> str:
    del args
    return _render(await gs.get_city_states(), nr.narrate_city_states)


async def _narrate_great_people(gs: Any, args: dict[str, Any]) -> str:
    del args
    return _render(await gs.get_great_people(), nr.narrate_great_people)


async def _narrate_victory_progress(gs: Any, args: dict[str, Any]) -> str:
    del args
    return _render(await gs.get_victory_progress(), nr.narrate_victory_progress)


async def _narrate_pathing_estimate(gs: Any, args: dict[str, Any]) -> str:
    est = await gs.get_pathing_estimate(args["unit_index"], args["x"], args["y"])
    return _render(est, nr.narrate_pathing_estimate)


async def _narrate_policies(gs: Any, args: dict[str, Any]) -> str:
    del args
    return _render(await gs.get_policies(), nr.narrate_policies)


async def _narrate_pantheon_status(gs: Any, args: dict[str, Any]) -> str:
    del args
    return _render(await gs.get_pantheon_status(), nr.narrate_pantheon_status)


async def _narrate_unit_promotions(gs: Any, args: dict[str, Any]) -> str:
    return _render(
        await gs.get_unit_promotions(args["unit_id"]),
        nr.narrate_unit_promotions,
    )


async def _skip_remaining_units(gs: Any, args: dict[str, Any]) -> str:
    del args
    return await gs.skip_remaining_units()


async def _narrate_purchasable_tiles(gs: Any, args: dict[str, Any]) -> str:
    return _render(
        await gs.get_purchasable_tiles(args["city_id"]),
        nr.narrate_purchasable_tiles,
    )
