"""Per-turn capability snapshot: CAPS| line -> flags dict, fail-open everywhere."""
from civ_mcp.arena.capabilities import CAP_FLAGS, build_caps_query, parse_caps


CAPS_LINE = ("CAPS|spies=0|government=1|religious_unit=0|gp_unit=1|corps=0"
             "|army=0|air=0|archaeology=0|great_works=1")


def test_cap_flags_inventory():
    assert CAP_FLAGS == ("spies", "government", "religious_unit", "gp_unit",
                         "corps", "army", "air", "archaeology", "great_works")


def test_build_caps_query_shape():
    lua = build_caps_query(3)
    assert "Players[3]" in lua                    # explicit pid, not GetLocalPlayer
    assert "HasCivic" in lua
    assert "MilitaryFormationTypes" in lua
    assert "pcall" in lua                         # per-check fail-open
    assert "---END---" in lua
    assert "CAPS|" in lua


def test_parse_caps_happy_path():
    flags = parse_caps([CAPS_LINE, "---END---"])
    assert flags == {"spies": False, "government": True, "religious_unit": False,
                     "gp_unit": True, "corps": False, "army": False, "air": False,
                     "archaeology": False, "great_works": True}


def test_parse_caps_fail_open_paths():
    assert parse_caps(None) is None
    assert parse_caps([]) is None
    assert parse_caps(["LUA ERROR: nope"]) is None
    # partial line: unknown keys skipped, known keys kept, missing keys absent
    flags = parse_caps(["CAPS|spies=1|bogus=1|government="])
    assert flags == {"spies": True}
