"""Unit tests for Lua response parsers.

Each parser takes list[str] (pipe-delimited lines from Lua print()) and returns
typed dataclasses. These tests verify the parsing logic with realistic fixtures.
"""

import pytest

from civ_mcp.lua.overview import parse_gameover_response, parse_overview_response
from civ_mcp.lua.units import (
    parse_combat_estimate,
    parse_threat_scan_response,
    parse_units_response,
)
from civ_mcp.lua.cities import parse_cities_response, parse_loyalty_response
from civ_mcp.lua.map import parse_map_response
from civ_mcp.lua.notifications import parse_end_turn_blocking
from civ_mcp.lua.diplomacy import parse_gossip_response
from civ_mcp.lua.climate import parse_climate_response
from civ_mcp.lua.great_works import (
    parse_great_works_response,
    build_move_great_work,
)


# ---------------------------------------------------------------------------
# parse_gameover_response
# ---------------------------------------------------------------------------


class TestParseGameover:
    def test_game_active(self):
        assert parse_gameover_response(["GAME_ACTIVE"]) is None

    def test_victory(self):
        lines = ["GAME_OVER|VICTORY|Gandhi|SCIENCE|alive|Gandhi"]
        result = parse_gameover_response(lines)
        assert result is not None
        assert result.is_game_over is True
        assert result.is_defeat is False
        assert result.winner_name == "Gandhi"
        assert result.victory_type == "SCIENCE"
        assert result.player_alive is True
        assert result.winner_leader == "Gandhi"

    def test_defeat(self):
        lines = ["GAME_OVER|DEFEAT|Gilgamesh|DOMINATION|dead|Gilgamesh"]
        result = parse_gameover_response(lines)
        assert result is not None
        assert result.is_defeat is True
        assert result.victory_type == "DOMINATION"
        assert result.player_alive is False

    def test_empty_lines(self):
        assert parse_gameover_response([]) is None

    def test_minimal_fields(self):
        """Only 2 fields — optional fields should get defaults."""
        lines = ["GAME_OVER|VICTORY"]
        result = parse_gameover_response(lines)
        assert result is not None
        assert result.winner_name == "Unknown"
        assert result.victory_type == "Unknown"


# ---------------------------------------------------------------------------
# parse_overview_response
# ---------------------------------------------------------------------------


class TestParseOverview:
    # Minimal 19-field main line: turn|pid|civ|leader|gold|gpt|sci|cul|faith|
    #   research|civic|cities|units|score|favor|fpt|pop|gold_income|maintenance
    MAIN_LINE = (
        "42|0|CIVILIZATION_INDIA|Gandhi|500.0|10.5|25.0|18.0|12.0|"
        "TECH_POTTERY|CIVIC_CODE_OF_LAWS|3|5|120|10|2|15|35.0|24.5"
    )

    def test_basic_fields(self):
        result = parse_overview_response([self.MAIN_LINE])
        assert result.turn == 42
        assert result.player_id == 0
        assert result.civ_name == "CIVILIZATION_INDIA"
        assert result.leader_name == "Gandhi"
        assert result.gold == 500.0
        assert result.gold_per_turn == 10.5
        assert result.science_yield == 25.0
        assert result.culture_yield == 18.0
        assert result.faith == 12.0
        assert result.current_research == "TECH_POTTERY"
        assert result.current_civic == "CIVIC_CODE_OF_LAWS"
        assert result.num_cities == 3
        assert result.num_units == 5
        assert result.score == 120

    def test_rankings(self):
        lines = [
            self.MAIN_LINE,
            "RANK|0|India|120",
            "RANK|1|Sumeria|95",
        ]
        result = parse_overview_response(lines)
        assert result.rankings is not None
        assert len(result.rankings) == 2
        assert result.rankings[0].civ_name == "India"
        assert result.rankings[1].score == 95

    def test_era_info(self):
        lines = [self.MAIN_LINE, "ERA|Classical|15|12|24"]
        result = parse_overview_response(lines)
        assert result.era_name == "Classical"
        assert result.era_score == 15
        assert result.era_dark_threshold == 12
        assert result.era_golden_threshold == 24

    def test_exploration(self):
        lines = [self.MAIN_LINE, "EXPLORE|200|1000"]
        result = parse_overview_response(lines)
        assert result.explored_land == 200
        assert result.total_land == 1000

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="Empty overview response"):
            parse_overview_response([])

    def test_too_few_fields_raises(self):
        with pytest.raises(ValueError, match="expected >=14"):
            parse_overview_response(["1|2|3"])


# ---------------------------------------------------------------------------
# parse_units_response
# ---------------------------------------------------------------------------


class TestParseUnits:
    # Fields: uid|index|name|type|x,y|moves/max|hp/max|cs|rs|charges|targets|promo|upgrade|upgrade_target|upgrade_cost|valid_imps|religion
    WARRIOR = "0|0|Warrior|UNIT_WARRIOR|10,24|2.0/2.0|100/100|20|0|0||0|0|||"
    BUILDER = "1|1|Builder|UNIT_BUILDER|12,22|2.0/2.0|100/100|0|0|3||0|0|||IMPROVEMENT_FARM;IMPROVEMENT_MINE|"

    def test_basic_warrior(self):
        units = parse_units_response([self.WARRIOR])
        assert len(units) == 1
        u = units[0]
        assert u.unit_id == 0
        assert u.name == "Warrior"
        assert u.unit_type == "UNIT_WARRIOR"
        assert u.x == 10
        assert u.y == 24
        assert u.moves_remaining == 2.0
        assert u.health == 100
        assert u.combat_strength == 20
        assert u.ranged_strength == 0
        assert u.build_charges == 0

    def test_builder_with_improvements(self):
        units = parse_units_response([self.BUILDER])
        u = units[0]
        assert u.build_charges == 3
        assert "IMPROVEMENT_FARM" in u.valid_improvements
        assert "IMPROVEMENT_MINE" in u.valid_improvements

    def test_multiple_units(self):
        units = parse_units_response([self.WARRIOR, self.BUILDER])
        assert len(units) == 2

    def test_short_line_skipped(self):
        units = parse_units_response(["too|few|fields"])
        assert len(units) == 0

    def test_targets(self):
        line = "2|2|Archer|UNIT_ARCHER|5,5|2.0/2.0|100/100|25|25|0|14,6;15,7|0|0|||"
        units = parse_units_response([line])
        assert units[0].targets == ["14,6", "15,7"]


# ---------------------------------------------------------------------------
# parse_combat_estimate
# ---------------------------------------------------------------------------


class TestParseCombat:
    def test_melee_combat(self):
        # ESTIMATE|att_type|def_type|eff_att_cs|eff_def_cs|is_ranged|modifiers|my_hp|enemy_hp
        line = "ESTIMATE|UNIT_WARRIOR|UNIT_WARRIOR|20|20|0|Flanking +2;Fortified -4|100|100"
        result = parse_combat_estimate([line], att_cs=20, def_cs=20)
        assert result is not None
        assert result.attacker_type == "UNIT_WARRIOR"
        assert result.defender_type == "UNIT_WARRIOR"
        assert result.attacker_cs == 20
        assert result.defender_cs == 20
        assert result.is_ranged is False
        assert "Flanking +2" in result.modifiers
        assert "Fortified -4" in result.modifiers
        # Equal CS: damage should be base (24) for both sides
        assert result.est_damage_to_defender == 24
        assert result.est_damage_to_attacker == 24

    def test_ranged_no_counter(self):
        line = "ESTIMATE|UNIT_ARCHER|UNIT_WARRIOR|25|20|1||100|100"
        result = parse_combat_estimate([line], att_cs=25, def_cs=20)
        assert result is not None
        assert result.is_ranged is True
        assert result.est_damage_to_attacker == 0  # ranged = no counter
        assert result.est_damage_to_defender > 24  # attacker stronger

    def test_no_estimate_line(self):
        assert parse_combat_estimate(["some other line"], att_cs=20, def_cs=20) is None


# ---------------------------------------------------------------------------
# parse_threat_scan_response
# ---------------------------------------------------------------------------


class TestParseThreatScan:
    def test_standard_threat(self):
        line = "THREAT|63|Barbarian|UNIT_WARRIOR|15,30|100/100|CS:20|RS:0|dist:3|cs:0|uid:42"
        threats = parse_threat_scan_response([line])
        assert len(threats) == 1
        t = threats[0]
        assert t.owner_id == 63
        assert t.owner_name == "Barbarian"
        assert t.unit_type == "UNIT_WARRIOR"
        assert t.x == 15
        assert t.y == 30
        assert t.hp == 100
        assert t.combat_strength == 20
        assert t.distance == 3
        assert t.unit_id == 42

    def test_city_state_threat(self):
        line = (
            "THREAT|10|Zanzibar|UNIT_ARCHER|8,12|80/100|CS:25|RS:25|dist:2|cs:1|uid:5"
        )
        threats = parse_threat_scan_response([line])
        assert threats[0].is_city_state is True

    def test_non_threat_lines_skipped(self):
        threats = parse_threat_scan_response(["SOME_OTHER_LINE", "ALSO_NOT_THREAT"])
        assert len(threats) == 0

    def test_legacy_format(self):
        """Older format without owner_id/owner_name."""
        line = "THREAT|UNIT_WARRIOR|15,30|100/100|CS:20|RS:0|dist:3"
        threats = parse_threat_scan_response([line])
        assert len(threats) == 1
        assert threats[0].unit_type == "UNIT_WARRIOR"
        assert threats[0].x == 15


# ---------------------------------------------------------------------------
# parse_cities_response
# ---------------------------------------------------------------------------


class TestParseCities:
    # 30 pipe-separated fields: id|name|x,y|pop|food|prod|gold|sci|cul|faith|
    #   housing|amenities|turns_grow|building|prod_turns|defense|gar_hp|wall_hp|
    #   attack_targets|pillaged_districts|districts|loyalty|loyalty_max|loyalty_pt|
    #   turns_flip|food_surplus|food_stored|growth_threshold|pillaged_buildings|garrison
    CITY_LINE = (
        "0|Delhi|10,24|4|8.0|5.0|3.0|2.0|1.5|0.0|"
        "6.0|3|12|BUILDING_GRANARY|5|"
        "15|200/200|0/0|"
        "||DISTRICT_CITY_CENTER;DISTRICT_CAMPUS|"
        "100.0|100.0|5.0|0|3.5|20.0|36||Warrior"
    )

    def test_basic_city(self):
        cities, distances = parse_cities_response([self.CITY_LINE])
        assert len(cities) == 1
        c = cities[0]
        assert c.city_id == 0
        assert c.name == "Delhi"
        assert c.x == 10
        assert c.y == 24
        assert c.population == 4
        assert c.food == 8.0
        assert c.production == 5.0
        assert c.currently_building == "BUILDING_GRANARY"
        assert c.production_turns_left == 5

    def test_districts_parsed(self):
        cities, _ = parse_cities_response([self.CITY_LINE])
        assert "DISTRICT_CITY_CENTER" in cities[0].districts
        assert "DISTRICT_CAMPUS" in cities[0].districts

    def test_distances(self):
        lines = [self.CITY_LINE, "DIST|Delhi|Agra|8"]
        cities, distances = parse_cities_response(lines)
        assert len(cities) == 1
        assert len(distances) == 1
        assert "8 tiles" in distances[0]

    def test_short_line_skipped(self):
        cities, _ = parse_cities_response(["too|short"])
        assert len(cities) == 0


# ---------------------------------------------------------------------------
# parse_map_response
# ---------------------------------------------------------------------------


class TestParseMap:
    # Fields: x,y|terrain|feature|resource|hills|river|coastal|improvement|owner|units|
    #   visibility|fresh_water|yields|district|owner_name|own_units|route|move_cost
    PLAINS_TILE = "10,24|TERRAIN_PLAINS|none|none|0|1|0|none|-1|none|visible|1|2,1,0,0,0,0|none||||-1|1"
    HILLS_WITH_MINE = (
        "12,22|TERRAIN_PLAINS|none|RESOURCE_IRON:RESOURCECLASS_STRATEGIC|1|0|0|"
        "IMPROVEMENT_MINE|0|none|visible|0|1,3,0,0,0,0|none|India|none|-1|2"
    )

    def test_basic_tile(self):
        tiles = parse_map_response([self.PLAINS_TILE])
        assert len(tiles) == 1
        t = tiles[0]
        assert t.x == 10
        assert t.y == 24
        assert t.terrain == "TERRAIN_PLAINS"
        assert t.feature is None
        assert t.resource is None
        assert t.is_hills is False
        assert t.is_river is True
        assert t.owner_id == -1
        assert t.visibility == "visible"
        assert t.is_fresh_water is True

    def test_resource_with_class(self):
        tiles = parse_map_response([self.HILLS_WITH_MINE])
        t = tiles[0]
        assert t.resource == "RESOURCE_IRON"
        assert t.resource_class == "strategic"
        assert t.is_hills is True
        assert t.improvement == "IMPROVEMENT_MINE"
        assert t.owner_name == "India"
        assert t.movement_cost == 2

    def test_pillaged_improvement(self):
        line = "5,5|TERRAIN_GRASSLAND|none|none|0|0|0|IMPROVEMENT_FARM:PILLAGED|0|none|visible|0|0,0,0,0,0,0|none||||-1|1"
        tiles = parse_map_response([line])
        assert tiles[0].improvement == "IMPROVEMENT_FARM"
        assert tiles[0].is_pillaged is True

    def test_yields_parsing(self):
        tiles = parse_map_response([self.PLAINS_TILE])
        assert tiles[0].yields == (2, 1, 0, 0, 0, 0)

    def test_short_line_skipped(self):
        tiles = parse_map_response(["too|few|fields"])
        assert len(tiles) == 0


# ---------------------------------------------------------------------------
# parse_end_turn_blocking
# ---------------------------------------------------------------------------


class TestParseEndTurnBlocking:
    def test_none(self):
        assert parse_end_turn_blocking(["NONE"]) == []

    def test_single_blocker(self):
        blockers = parse_end_turn_blocking(
            ["BLOCKING|UNIT_NEEDS_ORDERS|Warrior at 10,24"]
        )
        assert len(blockers) == 1
        assert blockers[0] == ("UNIT_NEEDS_ORDERS", "Warrior at 10,24")

    def test_multiple_blockers(self):
        lines = [
            "BLOCKING|UNIT_NEEDS_ORDERS|Warrior at 10,24",
            "BLOCKING|CHOOSE_PRODUCTION|Delhi needs production",
        ]
        blockers = parse_end_turn_blocking(lines)
        assert len(blockers) == 2

    def test_empty_lines(self):
        assert parse_end_turn_blocking([]) == []


# ---------------------------------------------------------------------------
# parse_gossip_response
# ---------------------------------------------------------------------------


class TestParseGossip:
    def test_grievances_and_gossip(self):
        lines = [
            "GRIEV|1|Gilgamesh|30|0",
            "GRIEV|3|Gandhi|0|15",
            "GOSSIP|1|41|Gilgamesh started building the Pyramids.",
            "---END---",
        ]
        grievances, gossip = parse_gossip_response(lines)
        assert len(grievances) == 2
        assert grievances[0].player_id == 1
        assert grievances[0].name == "Gilgamesh"
        assert grievances[0].they_hold_against_me == 30
        assert grievances[1].i_hold_against_them == 15
        assert len(gossip) == 1
        assert gossip[0].about_player == 1
        assert gossip[0].turn == 41
        assert "Pyramids" in gossip[0].text

    def test_gossip_lines_optional(self):
        """The gossip-log API is a live-probe candidate; grievances alone parse."""
        grievances, gossip = parse_gossip_response(["GRIEV|1|Gilgamesh|5|5"])
        assert len(grievances) == 1 and gossip == []

    def test_malformed_rows_skipped(self):
        grievances, gossip = parse_gossip_response(
            ["GRIEV|x|bad|row", "GOSSIP|notanint|q|t", "junk"])
        assert grievances == [] and gossip == []


# ---------------------------------------------------------------------------
# parse_loyalty_response
# ---------------------------------------------------------------------------


class TestParseLoyalty:
    def test_cities_with_sources(self):
        lines = [
            "LOYAL|65792|Lahore|72.5|100.0|-3.25",
            "LOYSRC|65792|Pressure from other civs|-5.5",
            "LOYSRC|65792|Governor|2.25",
            "LOYAL|65793|Multan|100.0|100.0|1.00",
            "---END---",
        ]
        rows = parse_loyalty_response(lines)
        assert len(rows) == 2
        assert rows[0].name == "Lahore"
        assert rows[0].loyalty == 72.5
        assert rows[0].per_turn == -3.25
        assert rows[0].sources == [("Pressure from other civs", -5.5),
                                   ("Governor", 2.25)]
        assert rows[1].sources == []

    def test_orphan_source_and_junk_skipped(self):
        rows = parse_loyalty_response(
            ["LOYSRC|999|orphan|1.0", "LOYAL|bad|X|a|b|c", "noise"])
        assert rows == []


# ---------------------------------------------------------------------------
# parse_climate_response
# ---------------------------------------------------------------------------


class TestParseClimate:
    def test_full_status(self):
        lines = [
            "CLIMATE|2|1|317",
            "DISASTER|STORM_HURRICANE|14|22|43",
            "DISASTER|RANDOM_EVENT_VOLCANO_ERUPTION|9|8|40",
            "---END---",
        ]
        st = parse_climate_response(lines)
        assert st.phase == 2 and st.sea_level == 1 and st.co2_total == 317
        assert len(st.disasters) == 2
        assert st.disasters[0].kind == "STORM_HURRICANE"
        assert (st.disasters[0].x, st.disasters[0].y) == (14, 22)
        assert st.disasters[1].turn == 40

    def test_unavailable_climate_system(self):
        st = parse_climate_response(["CLIMATE|-1|-1|-1"])
        assert st.phase == -1 and st.disasters == []

    def test_empty_is_unavailable(self):
        st = parse_climate_response([])
        assert st.phase == -1


# ---------------------------------------------------------------------------
# parse_great_works_response
# ---------------------------------------------------------------------------


class TestParseGreatWorks:
    def test_slots_and_works(self):
        lines = [
            "GWSLOT|65792|Lahore|BUILDING_AMPHITHEATER|0|GREATWORKSLOT_WRITING|17|Ramayana",
            "GWSLOT|65792|Lahore|BUILDING_AMPHITHEATER|1|GREATWORKSLOT_WRITING|-1|",
            "---END---",
        ]
        slots = parse_great_works_response(lines)
        assert len(slots) == 2
        assert slots[0].work_index == 17 and slots[0].work_name == "Ramayana"
        assert slots[1].work_index == -1 and slots[1].work_name == ""
        assert slots[1].slot_type == "GREATWORKSLOT_WRITING"

    def test_junk_skipped(self):
        assert parse_great_works_response(["GWSLOT|x|y", "noise"]) == []


def test_build_move_great_work_substitutes_args():
    lua = build_move_great_work(17, 65793, "BUILDING_MUSEUM_ART", 2)
    assert "17" in lua and "65793" in lua and "BUILDING_MUSEUM_ART" in lua
    assert "OK:" in lua and "ERR:" in lua


def test_build_move_great_work_rejects_suspicious_building():
    for bad in ('BUILDING_X"; print(1) --', "BUILDING X", "BUILDING_X'y",
                "b]u[ilding"):
        with pytest.raises(ValueError):
            build_move_great_work(17, 65793, bad, 2)


def test_build_form_formation_shape():
    from civ_mcp.lua.units import build_form_formation
    lua = build_form_formation(3, 7, "FORM_CORPS")
    assert "FORM_CORPS" in lua
    assert "PARAM_UNIT_ID" in lua           # merge target passed by id
    assert "CanStartCommand" in lua         # precheck before request
    assert "OK:" in lua and "ERR:" in lua

    with pytest.raises(ValueError):
        build_form_formation(3, 7, "FORM_VOLTRON")


def test_build_unit_operation_shape():
    from civ_mcp.lua.units import build_unit_operation
    lua = build_unit_operation(3, "REBASE", 10, 12)
    assert "REBASE" in lua and "PARAM_X" in lua and "PARAM_Y" in lua
    assert "CanStartOperation" in lua
    assert "OK:" in lua and "ERR:" in lua
    # Unit-not-found bail must print ERR AND the sentinel before returning
    # (via _lua_get_unit/_bail) — a bare return would hang the reader.
    assert 'print("ERR:UNIT_NOT_FOUND"); print("---END---"); return' in lua
    assert lua.rstrip().endswith('print("---END---")')
    with pytest.raises(ValueError):
        build_unit_operation(3, "MAKE_TEA", 0, 0)


def test_build_form_formation_coerces_indices_and_rejects_injection():
    from civ_mcp.lua.units import build_form_formation
    # numeric strings are accepted (coerced)
    lua = build_form_formation("3", "7", "FORM_CORPS")
    assert "GetUnit(me, 7)" in lua
    # a crafted string index cannot reach Lua — it raises at the builder
    with pytest.raises((ValueError, TypeError)):
        build_form_formation(3, "7} print(1) --", "FORM_CORPS")


# ---------------------------------------------------------------------------
# build_spy_mission (espionage builders)
# ---------------------------------------------------------------------------


def test_build_spy_mission_unknown_type_cannot_inject_lua():
    from civ_mcp.lua.espionage import build_spy_mission
    # An unknown mission whose name is a Lua-breakout payload must be neutralized:
    # the echoed name contributes ZERO quote characters, so the only quotes in the
    # generated Lua are the four from the two wrapping print() literals.
    lua = build_spy_mission(5, 'EVIL") do print("pwned") end --', 10, 12)
    assert "UNKNOWN_MISSION" in lua
    assert lua.count('"') == 4
    assert '") ' not in lua


def test_build_spy_mission_unknown_type_still_names_valid_missions():
    from civ_mcp.lua.espionage import build_spy_mission
    lua = build_spy_mission(5, "NOPE", 10, 12)
    assert "NOPE" in lua and "SIPHON_FUNDS" in lua
