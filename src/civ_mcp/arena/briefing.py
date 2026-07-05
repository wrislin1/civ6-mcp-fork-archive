"""Budgeted game-state briefing for in-process arena civs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from civ_mcp import narrate as nr
from civ_mcp.arena.budget import CHARS_PER_TOKEN
from civ_mcp.arena.config import BriefingOptions

_MAX_RADIUS = 5
_EXPAND_BELOW = 0.75
_PARTIAL_SECTION_MIN_CHARS = 200


@dataclass
class Briefing:
    text: str = ""
    tokens: int = 0
    sections: list[str] = field(default_factory=list)
    radius: int = 0
    errors: list[str] = field(default_factory=list)


def _render(result: Any, renderer: Callable[..., str]) -> str:
    if isinstance(result, str):
        return result
    return renderer(result)


async def _overview(gs: Any, ctx: dict[str, Any]) -> str:
    del ctx
    return _render(await gs.get_game_overview(), nr.narrate_overview)


async def _units(gs: Any, ctx: dict[str, Any]) -> str:
    units = await gs.get_units()
    ctx["units"] = [] if isinstance(units, str) else units
    return _render(units, nr.narrate_units)


async def _cities(gs: Any, ctx: dict[str, Any]) -> str:
    result = await gs.get_cities()
    if isinstance(result, str):
        ctx["cities"] = []
        return result
    cities, distances = result
    ctx["cities"] = cities
    return nr.narrate_cities(cities, distances)


async def _production_options(gs: Any, ctx: dict[str, Any]) -> str:
    cities = ctx.get("cities")
    if cities is None:
        result = await gs.get_cities()
        if isinstance(result, str):
            return result
        cities, _ = result
        ctx["cities"] = cities

    parts = []
    for city in cities:
        result = await gs.list_city_production(city.city_id)
        text = _render(result, nr.narrate_city_production)
        parts.append(f"[city {city.city_id} {city.name}]\n{text}")
    return "\n".join(parts) if parts else "No cities available for production options."


async def _research(gs: Any, ctx: dict[str, Any]) -> str:
    del ctx
    return _render(await gs.get_tech_civics(), nr.narrate_tech_civics)


async def _empire_resources(gs: Any, ctx: dict[str, Any]) -> str:
    del ctx
    result = await gs.get_empire_resources()
    if isinstance(result, str):
        return result
    stockpiles, owned, nearby, luxuries = result
    return nr.narrate_empire_resources(stockpiles, owned, nearby, luxuries)


async def _rivals(gs: Any, ctx: dict[str, Any]) -> str:
    del ctx
    rivals = await gs.get_rival_snapshot()
    if isinstance(rivals, str):
        return rivals
    if not rivals:
        return "No rival snapshot data."
    lines = []
    for r in rivals:
        lines.append(
            f"{r.name}: score {r.score}, {r.cities} cities, pop {r.pop}, "
            f"mil {r.mil}, sci {r.sci:.0f}, cul {r.cul:.0f}, "
            f"gold {r.gold:.0f}, techs {r.techs}, civics {r.civics}, "
            f"sci VP {r.sci_vp}, diplo VP {r.diplo_vp}"
        )
    return "\n".join(lines)


async def _threats(gs: Any, ctx: dict[str, Any]) -> str:
    del ctx
    threats = await gs.get_threat_scan()
    if isinstance(threats, str):
        return threats
    if not threats:
        return "No visible threats."
    lines = []
    for t in threats:
        ranged = f" RS {t.ranged_strength}" if t.ranged_strength else ""
        cs = f"CS {t.combat_strength}{ranged}"
        cs_tag = " [city-state]" if t.is_city_state else ""
        lines.append(
            f"{t.owner_name}{cs_tag} {t.unit_type} at ({t.x},{t.y}) {cs} "
            f"HP {t.hp}/{t.max_hp}, {t.distance} tiles away"
        )
    return "\n".join(lines)


async def _victory(gs: Any, ctx: dict[str, Any]) -> str:
    del ctx
    return _render(await gs.get_victory_progress(), nr.narrate_victory_progress)


_ORDER = (
    "overview",
    "units",
    "cities",
    "production_options",
    "map",
    "research",
    "empire_resources",
    "rivals",
    "threats",
    "victory",
)

_BUILDERS: dict[str, Callable[[Any, dict[str, Any]], Awaitable[str]]] = {
    "overview": _overview,
    "units": _units,
    "cities": _cities,
    "production_options": _production_options,
    "research": _research,
    "empire_resources": _empire_resources,
    "rivals": _rivals,
    "threats": _threats,
    "victory": _victory,
}


def _tile_count(radius: int) -> int:
    return 3 * radius * radius + 3 * radius + 1


async def _fetch_units(gs: Any, ctx: dict[str, Any]) -> list[Any]:
    units = ctx.get("units")
    if units is None:
        result = await gs.get_units()
        units = [] if isinstance(result, str) else result
        ctx["units"] = units
    return units


async def _fetch_cities(gs: Any, ctx: dict[str, Any]) -> list[Any]:
    cities = ctx.get("cities")
    if cities is None:
        result = await gs.get_cities()
        if isinstance(result, str):
            cities = []
        else:
            cities, _ = result
        ctx["cities"] = cities
    return cities


async def _map_text(gs: Any, centers: list[tuple[int, int]], radius: int) -> str:
    tiles = {}
    for x, y in centers:
        for tile in await gs.get_map_area(x, y, radius):
            tiles[(tile.x, tile.y)] = tile
    return nr.narrate_map([tiles[key] for key in sorted(tiles)])


async def _map(gs: Any, ctx: dict[str, Any], opts: BriefingOptions, used: int, budget: int) -> tuple[str, int]:
    if opts.map_radius <= 0:
        return "", 0

    units = await _fetch_units(gs, ctx)
    cities = await _fetch_cities(gs, ctx)
    centers = [(u.x, u.y) for u in units] + [(c.x, c.y) for c in cities]
    if not centers:
        return "", 0

    radius = min(opts.map_radius, _MAX_RADIUS)
    text = await _map_text(gs, centers, radius)
    target = radius
    first_tile_count = _tile_count(radius)

    for candidate in range(radius + 1, _MAX_RADIUS + 1):
        projected = len(text) * _tile_count(candidate) / first_tile_count
        if used + projected >= budget * _EXPAND_BELOW:
            break
        target = candidate

    if target > radius:
        larger = await _map_text(gs, centers, target)
        if used + len(larger) <= budget:
            radius = target
            text = larger

    return text, radius


def _join_with(parts: list[str], block: str) -> str:
    return block if not parts else "\n".join([*parts, block])


def _append_block(
    briefing: Briefing,
    parts: list[str],
    name: str,
    block: str,
    char_budget: int,
) -> bool:
    candidate = _join_with(parts, block)
    if len(candidate) <= char_budget:
        parts.append(block)
        briefing.sections.append(name)
        return True

    current = "\n".join(parts)
    separator = 1 if parts else 0
    remaining = char_budget - len(current) - separator
    if remaining >= _PARTIAL_SECTION_MIN_CHARS:
        prefix = "\n" if parts else ""
        parts.append((prefix + block)[:remaining].lstrip("\n"))
        briefing.sections.append(name)
    return False


def _block_header(name: str) -> str:
    """Section header used for both the budget accounting and the rendered block."""
    return f"== {name.upper()} ==\n"


async def build_briefing(
    gs: Any, opts: BriefingOptions, budget_tokens: int
) -> Briefing:
    briefing = Briefing()
    if not opts.enabled:
        return briefing

    char_budget = max(0, budget_tokens * CHARS_PER_TOKEN)
    if char_budget <= 0:
        return briefing

    ctx: dict[str, Any] = {}
    parts: list[str] = []
    wanted = {section for section in opts.sections}

    for name in _ORDER:
        if name not in wanted:
            continue

        try:
            if name == "map":
                map_prefix_used = len(_join_with(parts, _block_header("map")))
                text, radius = await _map(gs, ctx, opts, map_prefix_used, char_budget)
                if not text:
                    continue
            else:
                text = await _BUILDERS[name](gs, ctx)
        except Exception as exc:
            briefing.errors.append(f"{name}: {exc!r}")
            continue

        block = f"{_block_header(name)}{text}"
        keep_building = _append_block(briefing, parts, name, block, char_budget)
        if name == "map" and keep_building:
            briefing.radius = radius
        if not keep_building:
            break

    briefing.text = "\n".join(parts)[:char_budget]
    briefing.tokens = len(briefing.text) // CHARS_PER_TOKEN
    return briefing
