from __future__ import annotations

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


def _tool(
    name: str,
    description: str,
    params: dict[str, dict[str, Any]] | None,
    required: Sequence[str],
    call: Callable[[Any, dict[str, Any]], Awaitable[str]],
) -> ToolDef:
    return ToolDef(
        name=name,
        description=description,
        params=params or {},
        required=tuple(required),
        call=call,
    )


def _render(data: Any, narrator: Callable[[Any], str]) -> str:
    if isinstance(data, str):
        return data
    return narrator(data)


def _coerce_policy_assignments(assignments: dict[Any, str]) -> dict[int, str]:
    return {int(slot): policy for slot, policy in assignments.items()}


_MAP_RADIUS_DEFAULT = 2
_MAP_RADIUS_MIN = 0
_MAP_RADIUS_MAX = 5


def _clamp_map_radius(value: Any) -> int:
    radius = int(value)
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
    ),
    "found_city": _tool(
        "found_city",
        "Found a city with a settler.",
        {"unit_index": _int_param("Settler unit_index from get_units.")},
        ("unit_index",),
        lambda gs, args: gs.found_city(args["unit_index"]),
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
    ),
    "skip_unit": _tool(
        "skip_unit",
        "Skip a unit for this turn.",
        {"unit_index": _int_param("Unit index from get_units.")},
        ("unit_index",),
        lambda gs, args: gs.skip_unit(args["unit_index"]),
    ),
    "get_map_area": _tool(
        "get_map_area",
        "Inspect terrain and units around a map coordinate.",
        {
            "x": _int_param("Center X coordinate."),
            "y": _int_param("Center Y coordinate."),
            "radius": _int_param(
                "Search radius; defaults to 2 and is clamped to 0..5.",
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
    ),
    "remove_feature": _tool(
        "remove_feature",
        "Remove a feature from the current tile.",
        {"unit_index": _int_param("Builder unit index.")},
        ("unit_index",),
        lambda gs, args: gs.remove_feature(args["unit_index"]),
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
    ),
    "heal_unit": _tool(
        "heal_unit",
        "Heal a unit until fully recovered.",
        {"unit_index": _int_param("Unit index from get_units.")},
        ("unit_index",),
        lambda gs, args: gs.heal_unit(args["unit_index"]),
    ),
    "alert_unit": _tool(
        "alert_unit",
        "Put a unit on alert.",
        {"unit_index": _int_param("Unit index from get_units.")},
        ("unit_index",),
        lambda gs, args: gs.alert_unit(args["unit_index"]),
    ),
    "set_civic": _tool(
        "set_civic",
        "Choose the current civic research.",
        {"civic_name": _str_param("Civic type, for example CIVIC_FOREIGN_TRADE.")},
        ("civic_name",),
        lambda gs, args: gs.set_civic(args["civic_name"]),
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
    ),
    "appoint_governor": _tool(
        "appoint_governor",
        "Appoint a new governor.",
        {"governor_type": _str_param("Governor type name.")},
        ("governor_type",),
        lambda gs, args: gs.appoint_governor(args["governor_type"]),
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
    ),
    "choose_pantheon": _tool(
        "choose_pantheon",
        "Choose a pantheon belief.",
        {"belief_type": _str_param("Pantheon belief type.")},
        ("belief_type",),
        lambda gs, args: gs.choose_pantheon(args["belief_type"]),
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
    ),
    "skip_remaining_units": _tool(
        "skip_remaining_units",
        "Skip all units that still have movement.",
        None,
        (),
        lambda gs, args: _skip_remaining_units(gs, args),
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
    "full": (
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
        "get_settle_advisor",
        "get_district_advisor",
        "get_wonder_advisor",
        "get_builder_tasks",
        "get_diplomacy",
        "get_city_states",
        "get_great_people",
        "get_empire_resources",
        "get_victory_progress",
        "get_pathing_estimate",
        "send_envoy",
        "get_policies",
        "set_policies",
        "appoint_governor",
        "assign_governor",
        "choose_pantheon",
        "get_pantheon_status",
        "upgrade_unit",
        "promote_unit",
        "get_unit_promotions",
        "automate_explore",
        "skip_remaining_units",
        "purchase_tile",
        "get_purchasable_tiles",
        "set_city_focus",
    ),
}


def resolve_tools(selector: str | Sequence[str]) -> tuple[str, ...]:
    if isinstance(selector, str):
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
