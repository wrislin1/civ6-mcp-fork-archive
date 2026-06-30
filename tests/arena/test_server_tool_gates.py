from civ_mcp.server import _tools_removed_for_env


def test_disable_lua_env_removes_only_run_lua():
    assert _tools_removed_for_env({"CIV_MCP_DISABLE_LUA": "1"}) == ("run_lua",)


def test_arena_puppet_env_removes_lifecycle_and_lua_tools():
    removed = set(_tools_removed_for_env({"CIV_MCP_ARENA_PUPPET": "1"}))
    assert {
        "end_turn",
        "kill_game",
        "load_game_save",
        "restart_and_load",
        "load_save",
        "load_save_from_menu",
        "launch_game",
        "run_lua",
    } <= removed
