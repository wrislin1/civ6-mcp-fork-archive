import pytest
from civ_mcp.lua._helpers import _safe_enum, _one_of, _lua_escape, _lua_get_city


class NoExecConn:
    """A GameConnection double whose Lua execution fails if reached — proves
    validation raised BEFORE any Lua ran. Reused by the GameState-entry tests."""
    async def execute_read(self, lua, timeout=5.0):
        raise AssertionError("Lua executed — validation should have raised first")
    async def execute_write(self, lua, timeout=5.0):
        raise AssertionError("Lua executed — validation should have raised first")


class CannedConn:
    """Returns an empty result without raising — for happy-path calls."""
    def __init__(self):
        self.calls = []
    async def execute_read(self, lua, timeout=5.0):
        self.calls.append(lua); return []
    async def execute_write(self, lua, timeout=5.0):
        self.calls.append(lua); return []


def test_safe_enum_accepts_civ_tokens():
    assert _safe_enum("IMPROVEMENT_FARM", "improvement") == "IMPROVEMENT_FARM"
    assert _safe_enum("TECH_POTTERY") == "TECH_POTTERY"

@pytest.mark.parametrize("bad", ['X" .. evil() .. "', "A]B", "A B", "A.B", "", "A;B", "A\nB", "IMPROVEMENT_FARM\n"])
def test_safe_enum_rejects_breakout(bad):
    with pytest.raises(ValueError):
        _safe_enum(bad, "field")

def test_one_of_accepts_and_upcases():
    assert _one_of("military", frozenset({"MILITARY"}), "alliance") == "MILITARY"

@pytest.mark.parametrize("bad", ['UNIT" --', "BOGUS", "", "OPEN BORDERS"])
def test_one_of_rejects_nonmembers(bad):
    with pytest.raises(ValueError):
        _one_of(bad, frozenset({"UNIT", "BUILDING"}), "item_type")

def test_lua_escape_neutralizes_and_preserves_display_names():
    assert _lua_escape("Ancient Walls") == "Ancient Walls"          # legit name unchanged
    out = _lua_escape('x" .. os.exit() .. "')
    assert '"' not in out.replace('\\"', "")                        # no UNescaped quote
    assert "\n" not in _lua_escape("a\nb")

def test_lua_get_city_rejects_nonnumeric():
    with pytest.raises((ValueError, TypeError)):
        _lua_get_city("1) print(1) --")

def test_lua_get_city_accepts_numeric():
    assert "% 65536" in _lua_get_city(65792)
