import pytest
from civ_mcp.arena.config import parse_player_spec, PlayerSpec, ArenaConfig

def test_parse_player_spec_local():
    assert parse_player_spec("1:local:qwen3-coder-30b") == PlayerSpec(1, "local", "qwen3-coder-30b")

def test_parse_player_spec_rejects_bad():
    with pytest.raises(ValueError):
        parse_player_spec("nope")

def test_local_model_with_colon():
    s = parse_player_spec("1:local:qwen3-coder:30b")
    assert s == PlayerSpec(1, "local", "qwen3-coder:30b")
    assert s.driver_kind() == "in_process"

def test_cli_claude_empty_model():
    s = parse_player_spec("2:cli-claude:")
    assert s == PlayerSpec(2, "cli-claude", "")
    assert s.driver_kind() == "cli"

def test_rejects_unknown_provider():
    with pytest.raises(ValueError):
        parse_player_spec("1:typo:model")

def test_arena_config_gateway_url_default():
    assert ArenaConfig(players=[]).gateway_url == "http://192.168.20.196:11430/v1"
