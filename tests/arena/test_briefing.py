import pytest

from civ_mcp import lua as lq
from civ_mcp.arena.briefing import Briefing, build_briefing
from civ_mcp.arena.config import BriefingOptions


def _unit(x, y):
    return lq.UnitInfo(
        unit_id=65537,
        unit_index=1,
        name="Warrior",
        unit_type="UNIT_WARRIOR",
        x=x,
        y=y,
        moves_remaining=2,
        max_moves=2,
        health=100,
        max_health=100,
    )


def _city(x, y):
    return lq.CityInfo(
        city_id=65536,
        name="Nidaros",
        x=x,
        y=y,
        population=1,
        food=3.0,
        production=2.0,
        gold=1.0,
        science=1.0,
        culture=1.0,
        faith=0.0,
        housing=4.0,
        amenities=1,
        turns_to_grow=10,
    )


def _tile(x, y):
    return lq.TileInfo(
        x,
        y,
        "TERRAIN_GRASS",
        None,
        None,
        False,
        False,
        False,
        None,
        -1,
    )


def _overview():
    return lq.GameOverview(
        turn=5,
        player_id=3,
        civ_name="CIVILIZATION_NORWAY",
        leader_name="LEADER_HARDRADA",
        gold=10.0,
        gold_per_turn=1.5,
        science_yield=2.0,
        culture_yield=1.0,
        faith=0.0,
        current_research="TECH_MINING",
        current_civic="CIVIC_CODE_OF_LAWS",
        num_cities=1,
        num_units=2,
    )


def _resource_stockpile():
    return lq.ResourceStockpile(
        name="IRON",
        amount=10,
        cap=50,
        per_turn=2,
        demand=0,
        imported=0,
    )


def _victory_progress():
    return lq.VictoryProgress(
        players=[
            lq.VictoryPlayerProgress(
                player_id=3,
                name="Norway",
                score=42,
                science_vp=0,
                science_vp_needed=50,
                diplomatic_vp=1,
                tourism=0,
                military_strength=85,
                techs_researched=4,
                civics_completed=3,
                religion_cities=0,
            )
        ]
    )


class FakeGS:
    def __init__(self, city_xy=(12, 10)):
        self.map_calls = []
        self._city_xy = city_xy
        self.cities_calls = 0

    async def get_game_overview(self):
        return _overview()

    async def get_units(self):
        return [_unit(10, 10)]

    async def get_cities(self):
        self.cities_calls += 1
        return ([_city(*self._city_xy)], ["warn"])

    async def list_city_production(self, city_id):
        return f"PRODUCTION OPTIONS city {city_id}: UNIT_WARRIOR"

    async def get_map_area(self, x, y, radius):
        self.map_calls.append((x, y, radius))
        return [_tile(x + dx, y) for dx in range(-radius, radius + 1)]

    async def get_tech_civics(self):
        return "TECHS: pottery 3t"

    async def get_empire_resources(self):
        return ([_resource_stockpile()], [], [], {"SILK": 2})

    async def get_victory_progress(self):
        return _victory_progress()


ALL = ("overview", "units", "cities", "map", "research", "production_options")


class NoCallGS:
    def __getattr__(self, name):
        raise AssertionError(f"GameState method {name} must not be accessed")


@pytest.mark.asyncio
async def test_disabled_briefing_returns_empty_without_calling_game_state():
    b = await build_briefing(
        NoCallGS(),
        BriefingOptions(enabled=False, sections=ALL),
        100_000,
    )

    assert b == Briefing()


@pytest.mark.asyncio
async def test_sections_in_priority_order_and_meta():
    gs = FakeGS()

    b = await build_briefing(gs, BriefingOptions(enabled=True, sections=ALL), 100_000)

    assert isinstance(b, Briefing)
    assert b.sections == [
        "overview",
        "units",
        "cities",
        "production_options",
        "map",
        "research",
    ]
    for marker in (
        "== OVERVIEW ==",
        "at (10,10)",
        "Nidaros",
        "PRODUCTION OPTIONS city 65536",
        "(10,10):",
        "TECHS: pottery 3t",
    ):
        assert marker in b.text
    assert "UnitInfo(" not in b.text and "CityInfo(" not in b.text
    assert b.tokens == len(b.text) // 3
    assert b.errors == []


@pytest.mark.asyncio
async def test_production_options_fetches_cities_when_city_section_absent():
    gs = FakeGS()

    b = await build_briefing(
        gs,
        BriefingOptions(enabled=True, sections=("production_options",)),
        100_000,
    )

    assert b.sections == ["production_options"]
    assert gs.cities_calls == 1
    assert "PRODUCTION OPTIONS city 65536" in b.text


@pytest.mark.asyncio
async def test_map_radius_expands_with_budget():
    gs = FakeGS()

    b = await build_briefing(
        gs,
        BriefingOptions(enabled=True, map_radius=2, sections=ALL),
        100_000,
    )

    assert b.radius == 5
    assert gs.map_calls[0][2] == 2
    assert {c[2] for c in gs.map_calls} == {2, 5}
    assert len(gs.map_calls) == 4


@pytest.mark.asyncio
async def test_map_tiles_deduplicated():
    gs = FakeGS(city_xy=(12, 10))

    b = await build_briefing(
        gs,
        BriefingOptions(enabled=True, map_radius=2, sections=("map",)),
        100_000,
    )

    assert b.text.count("(10,10):") == 1


@pytest.mark.asyncio
async def test_map_radius_capped_at_five():
    gs = FakeGS()

    b = await build_briefing(
        gs,
        BriefingOptions(enabled=True, map_radius=9, sections=("map",)),
        100_000,
    )

    assert max(c[2] for c in gs.map_calls) <= 5
    assert b.radius == 5


@pytest.mark.asyncio
async def test_map_radius_stays_zero_when_map_not_included():
    gs = FakeGS()

    b = await build_briefing(
        gs,
        BriefingOptions(enabled=True, map_radius=2, sections=("map",)),
        10,
    )

    assert b.text == ""
    assert b.sections == []
    assert b.radius == 0
    assert gs.map_calls


@pytest.mark.asyncio
async def test_partial_map_section_does_not_report_radius():
    gs = FakeGS()

    b = await build_briefing(
        gs,
        BriefingOptions(enabled=True, map_radius=5, sections=("map",)),
        70,
    )

    assert "map" in b.sections
    assert "== MAP ==" in b.text
    assert len(b.text) <= 70 * 3
    assert b.radius == 0


@pytest.mark.asyncio
async def test_map_skipped_when_radius_nonpositive():
    gs = FakeGS()

    b = await build_briefing(
        gs,
        BriefingOptions(enabled=True, map_radius=0, sections=("map",)),
        100_000,
    )

    assert b.text == ""
    assert b.sections == []
    assert b.radius == 0
    assert gs.map_calls == []


@pytest.mark.asyncio
async def test_extended_sections_render_real_dataclasses():
    gs = FakeGS()

    b = await build_briefing(
        gs,
        BriefingOptions(enabled=True, sections=("empire_resources", "victory")),
        100_000,
    )

    assert b.errors == []
    assert b.sections == ["empire_resources", "victory"]
    assert "Empire Resources:" in b.text
    assert "IRON: 10/50 (+2/turn)" in b.text
    assert "=== VICTORY PROGRESS ===" in b.text
    assert "Norway: 0/50 VP" in b.text
    assert "ResourceStockpile(" not in b.text
    assert "VictoryProgress(" not in b.text


@pytest.mark.asyncio
async def test_rivals_and_threats_render_real_dataclasses():
    gs = FakeGS()

    async def rivals():
        return [
            lq.RivalSnapshot(
                id=1,
                name="Rome",
                score=50,
                cities=2,
                pop=6,
                sci=4.0,
                cul=3.0,
                gold=20.0,
                mil=120,
                techs=5,
                civics=3,
                faith=0.0,
                sci_vp=0,
                diplo_vp=0,
            )
        ]

    async def threats():
        return [
            lq.ThreatInfo(
                unit_type="UNIT_BARBARIAN_WARRIOR",
                x=9,
                y=9,
                hp=100,
                max_hp=100,
                combat_strength=20,
                ranged_strength=0,
                distance=3,
            )
        ]

    gs.get_rival_snapshot = rivals
    gs.get_threat_scan = threats

    b = await build_briefing(
        gs,
        BriefingOptions(enabled=True, sections=("rivals", "threats")),
        100_000,
    )

    assert b.errors == []
    assert "Rome: score 50, 2 cities, pop 6, mil 120" in b.text
    assert "Barbarian UNIT_BARBARIAN_WARRIOR at (9,9) CS 20" in b.text
    assert "RivalSnapshot(" not in b.text and "ThreatInfo(" not in b.text


@pytest.mark.asyncio
async def test_hard_truncation_at_budget():
    gs = FakeGS()

    b = await build_briefing(gs, BriefingOptions(enabled=True, sections=ALL), 50)

    assert len(b.text) <= 50 * 3
    assert b.tokens == len(b.text) // 3


@pytest.mark.asyncio
async def test_failing_section_skipped_and_logged():
    gs = FakeGS()

    async def boom():
        raise RuntimeError("no tuner")

    gs.get_tech_civics = boom

    b = await build_briefing(gs, BriefingOptions(enabled=True, sections=ALL), 100_000)

    assert "research" not in b.sections
    assert any("research" in e and "no tuner" in e for e in b.errors)
    assert "== OVERVIEW ==" in b.text
