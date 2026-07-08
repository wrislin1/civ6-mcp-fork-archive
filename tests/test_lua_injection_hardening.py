import pytest
from civ_mcp.game_state import GameState
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


# NOTE on the omitted `item_name`-only case: `_lua_escape` (used for item_name)
# is a neutralize-not-reject primitive by design (see
# docs/superpowers/specs/2026-07-08-arena-lua-injection-hardening-design.md —
# "item_name legitimately carries mixed-case, space-containing display names
# ... A crafted item_name is neutralized ... and falls through to the existing
# 'not found' bail"). A call with an otherwise-valid city_id/item_type and only
# a crafted item_name therefore does NOT raise — it proceeds (safely) to
# conn.execute_write, same as any other well-formed call. Confirmed empirically:
# adding that case to the "must raise" parametrize below fails with
# AssertionError (NoExecConn reached), not ValueError/TypeError — the
# unconditional-raise double is the wrong tool for that case. Escaping
# correctness for item_name is covered by test_lua_escape_neutralizes_and_preserves_display_names
# above (primitive-level) and by test_set_city_production_escapes_item_name_at_entry /
# test_purchase_item_escapes_item_name_at_entry below (GameState-entry wiring).
@pytest.mark.asyncio
@pytest.mark.parametrize("method,args,kwargs", [
    ("set_city_production", (), {"city_id": '1) e() --', "item_name": "Scout", "item_type": "UNIT"}),
    ("set_city_production", (), {"city_id": 1, "item_name": "Scout", "item_type": 'UNIT" --'}),
    ("set_city_production", (), {"city_id": 1, "item_name": "Scout", "item_type": "UNIT",
                                 "target_x": '9)--', "target_y": 3}),
    ("purchase_item",       (), {"city_id": 1, "item_type": 'UNIT"--', "item_name": "Scout"}),
    ("purchase_item",       (), {"city_id": 1, "item_type": "UNIT", "item_name": "Scout",
                                 "yield_type": 'YIELD_GOLD" --'}),
    ("set_city_focus",      (), {"city_id": 1, "focus": 'FOOD" .. e() .. "'}),
    ("set_city_focus",      (), {"city_id": '1)--', "focus": "FOOD"}),
    ("list_city_production",(), {"city_id": '1) print(1) --'}),
])
async def test_cities_methods_reject_injection(method, args, kwargs):
    gs = GameState(NoExecConn())
    with pytest.raises((ValueError, TypeError)):
        await getattr(gs, method)(*args, **kwargs)


@pytest.mark.asyncio
async def test_set_city_production_escapes_item_name_at_entry():
    """item_name is neutralized (not rejected) — verify the GameState entry
    actually routes it through _lua_escape before it reaches conn, so no
    unescaped quote from a crafted item_name survives into the built Lua."""
    conn = CannedConn()
    gs = GameState(conn)
    await gs.set_city_production(city_id=1, item_type="UNIT", item_name='x" .. e() .. "')
    assert conn.calls, "execute_write should have been reached (escape, not reject)"
    lua = conn.calls[-1]
    assert 'x\\" .. e() .. \\"' in lua
    assert ' .. e() .. "' not in lua.replace('x\\" .. e() .. \\"', "")


@pytest.mark.asyncio
async def test_purchase_item_escapes_item_name_at_entry():
    conn = CannedConn()
    gs = GameState(conn)
    await gs.purchase_item(city_id=1, item_type="UNIT", item_name='x" .. e() .. "')
    assert conn.calls, "execute_write should have been reached (escape, not reject)"
    lua = conn.calls[-1]
    assert 'x\\" .. e() .. \\"' in lua
    assert ' .. e() .. "' not in lua.replace('x\\" .. e() .. \\"', "")


@pytest.mark.asyncio
@pytest.mark.parametrize("method,kwargs", [
    ("appoint_governor",  {"governor_type": 'GOVERNOR_X" --'}),
    ("assign_governor",   {"governor_type": 'GOVERNOR_X" --', "city_id": 1}),
    ("assign_governor",   {"governor_type": "GOVERNOR_LIANG", "city_id": '1)--'}),
    ("promote_governor",  {"governor_type": "GOVERNOR_LIANG", "promotion_type": 'X" --'}),
    ("promote_unit",      {"unit_id": 1, "promotion_type": 'PROMOTION_X" --'}),
    ("change_government",  {"government_type": 'GOVERNMENT_X" --'}),
])
async def test_governance_methods_reject_injection(method, kwargs):
    gs = GameState(NoExecConn())
    with pytest.raises((ValueError, TypeError)):
        await getattr(gs, method)(**kwargs)


@pytest.mark.asyncio
async def test_set_policies_rejects_injection():
    gs = GameState(NoExecConn())
    with pytest.raises((ValueError, TypeError)):
        await gs.set_policies({0: 'POLICY_X" .. e() .. "'})


@pytest.mark.asyncio
@pytest.mark.parametrize("method,kwargs", [
    ("choose_pantheon", {"belief_type": 'BELIEF_X" --'}),
    ("found_religion",  {"religion_type": 'RELIGION_X" --', "follower_belief": "BELIEF_A", "founder_belief": "BELIEF_B"}),
    ("found_religion",  {"religion_type": "RELIGION_BUDDHISM", "follower_belief": 'X"--', "founder_belief": "BELIEF_B"}),
    ("found_religion",  {"religion_type": "RELIGION_BUDDHISM", "follower_belief": "BELIEF_A", "founder_belief": 'X"--'}),
])
async def test_religion_methods_reject_injection(method, kwargs):
    gs = GameState(NoExecConn())
    with pytest.raises((ValueError, TypeError)):
        await getattr(gs, method)(**kwargs)


# NOTE: method names below are the REAL GameState methods, not the public tool
# names — e.g. respond_to_diplomacy/get_trade_options/respond_to_trade are
# exposed under diplomacy_respond/get_deal_options/respond_to_deal internally.
@pytest.mark.asyncio
@pytest.mark.parametrize("method,kwargs", [
    ("send_diplomatic_action", {"other_player_id": 1, "action": 'DECLARE_FRIENDSHIP" --'}),
    ("send_diplomatic_action", {"other_player_id": '1)--', "action": "DECLARE_FRIENDSHIP"}),
    ("diplomacy_respond",      {"other_player_id": 1, "response": 'POSITIVE" --'}),
    ("form_alliance",          {"other_player_id": 1, "alliance_type": 'MILITARY" --'}),
    ("form_alliance",          {"other_player_id": 1, "alliance_type": "BOGUS"}),
    ("propose_peace",          {"other_player_id": '1) e() --'}),
    ("get_deal_options",       {"other_player_id": '1)--'}),
    ("respond_to_deal",        {"other_player_id": '1)--', "accept": True}),
    ("send_envoy",             {"city_state_player_id": '1) print(1) --'}),
])
async def test_diplomacy_methods_reject_injection(method, kwargs):
    gs = GameState(NoExecConn())
    with pytest.raises((ValueError, TypeError)):
        await getattr(gs, method)(**kwargs)


@pytest.mark.asyncio
async def test_propose_trade_rejects_resource_injection():
    # GameState.propose_trade takes offer_items/request_items (list[dict]), not
    # the offer_resources/mode kwargs the public server.py tool builds from —
    # craft a RESOURCE item so the call actually reaches the _lua_deal_item
    # RESOURCE branch in civ_mcp.lua.diplomacy.
    gs = GameState(NoExecConn())
    with pytest.raises((ValueError, TypeError)):
        await gs.propose_trade(
            other_player_id=1,
            offer_items=[
                {"type": "RESOURCE", "name": 'RESOURCE_IRON,X" .. e() .. "', "amount": 1}
            ],
            request_items=[],
        )


def test_send_diplo_action_unknown_does_not_fall_back_to_raw():
    # An unknown (but charset-safe) action must NOT be spliced into RequestSession.
    from civ_mcp.lua.diplomacy import build_send_diplo_action
    lua = build_send_diplo_action(1, "TOTALLY_UNKNOWN_ACTION")
    assert 'RequestSession(me, target, "TOTALLY_UNKNOWN_ACTION")' not in lua


# NOTE: set_research's real kwarg is tech_name (not tech); improve_tile's real
# signature is (unit_index, improvement_name) — unit_index is already coerced
# to int at the arena registry, so only improvement_name is crafted here.
@pytest.mark.asyncio
@pytest.mark.parametrize("method,kwargs", [
    ("set_research",         {"tech_name": 'TECH_X" .. e() .. "'}),
    ("set_civic",            {"civic_name": 'CIVIC_X" --'}),
    ("improve_tile",         {"unit_index": 1, "improvement_name": 'IMPROVEMENT_X" --'}),
    ("get_district_advisor", {"city_id": 1, "district_type": 'DISTRICT_X" --'}),
    ("get_district_advisor", {"city_id": '1)--', "district_type": "DISTRICT_CAMPUS"}),
    ("get_wonder_advisor",   {"city_id": 1, "wonder_name": 'BUILDING_X" --'}),
    ("get_wonder_advisor",   {"city_id": '1)--', "wonder_name": "BUILDING_STONEHENGE"}),
    ("get_purchasable_tiles",{"city_id": '1) print(1) --'}),
    ("purchase_tile",        {"city_id": '1)--', "x": 1, "y": 1}),
])
async def test_research_map_methods_reject_injection(method, kwargs):
    gs = GameState(NoExecConn())
    with pytest.raises((ValueError, TypeError)):
        await getattr(gs, method)(**kwargs)


# patronize_great_person: individual_id is already int-coerced at the arena
# registry (registry.py ~1096, int(args["individual_id"])); yield_type was
# NOT covered by the earlier hardening pass — it splices raw into a Lua
# string literal inside build_patronize_great_person (great_people.py
# :199/:208) via .replace("YIELD_", "").lower(), which strips no quote or
# backslash.
@pytest.mark.asyncio
@pytest.mark.parametrize("method,kwargs", [
    ("patronize_great_person", {"individual_id": 1, "yield_type": 'GOLD" .. e() .. "'}),
])
async def test_great_people_methods_reject_injection(method, kwargs):
    gs = GameState(NoExecConn())
    with pytest.raises((ValueError, TypeError)):
        await getattr(gs, method)(**kwargs)
