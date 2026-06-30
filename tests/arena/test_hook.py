from civ_mcp.arena.hook import build_inject_lua, POLL_LUA, parse_poll, PuppetState

def test_inject_lua_contains_ids_and_switch():
    lua = build_inject_lua([1, 2])
    assert "SetLocalPlayerAndObserver" in lua
    assert "__pt_puppets" in lua and "[1]=true" in lua and "[2]=true" in lua
    assert "__pt_puppets = { [1]=true, [2]=true }" in lua

def test_parse_poll():
    lines = ["LOCAL|1", "TURN|2", "ACTIVE|true", "LAST|1"]
    st = parse_poll(lines)
    assert st == PuppetState(local=1, turn=2, active=True, last=1)
