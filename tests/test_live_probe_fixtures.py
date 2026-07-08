"""Regression anchors from the 2026-07-08 live-probe run (turn-380 Future-era
Gathering-Storm game). These pin the parsers against *actual* captured game
output — not synthetic fixtures — including the two documented API degrades
(climate sea-level, loyalty breakdown)."""

from civ_mcp.lua.diplomacy import parse_gossip_response
from civ_mcp.lua.cities import parse_loyalty_response
from civ_mcp.lua.climate import parse_climate_response
from civ_mcp.lua.great_works import parse_great_works_response


def test_real_loyalty_lines():
    rows = parse_loyalty_response([
        "LOYAL|65536|Gyeongju|100.00|100.00|38.00",
        "LOYAL|2228255|London|79.98|100.00|3.89",
    ])
    assert rows[0].city_id == 65536 and rows[0].loyalty == 100.0
    assert rows[1].name == "London" and rows[1].per_turn == 3.89
    # LOYSRC degrades live (GetLoyaltyBreakdown nil) -> no sources
    assert rows[0].sources == []


def test_real_climate_line_sea_level_degrades():
    status = parse_climate_response(["CLIMATE|11|-1|17376"])
    assert status.phase == 11 and status.co2_total == 17376
    assert status.sea_level == -1   # GetSeaLevel + alts all nil (documented degrade)
    assert status.disasters == []


def test_real_great_works_slots():
    slots = parse_great_works_response([
        "GWSLOT|65536|Gyeongju|BUILDING_OXFORD_UNIVERSITY|0|GREATWORKSLOT_WRITING|3|",
        "GWSLOT|393221|Busan|BUILDING_AMPHITHEATER|1|GREATWORKSLOT_WRITING|-1|",
    ])
    assert slots[0].work_index == 3 and slots[0].slot_type == "GREATWORKSLOT_WRITING"
    assert slots[1].work_index == -1  # empty slot sentinel


def test_real_gossip_fixed_text():
    _, gossip = parse_gossip_response([
        "GOSSIP|1|379|Your delegate learned that Sweden completed research on Guidance Systems.",
    ])
    assert gossip[0].turn == 379 and "table:" not in gossip[0].text
