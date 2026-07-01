import pytest
from civ_mcp.arena.config import parse_player_spec, PlayerSpec, ArenaConfig

def test_parse_player_spec_local():
    assert parse_player_spec("1:local:qwen3-coder-30b") == PlayerSpec(1, "local", "qwen3-coder-30b")
    # no gateway override → empty string (falls back to the global --gateway-url)
    assert parse_player_spec("1:local:qwen3-coder-30b").gateway == ""


def test_parse_player_spec_per_civ_gateway():
    """A trailing '@<url>' pins a local civ to its own gateway (e.g. a per-GPU llama-swap)."""
    s = parse_player_spec("3:local:gemma4-26b@http://192.168.20.196:11440/v1")
    assert s == PlayerSpec(3, "local", "gemma4-26b", "http://192.168.20.196:11440/v1")
    assert s.model == "gemma4-26b"
    assert s.gateway == "http://192.168.20.196:11440/v1"


def test_parse_player_spec_gateway_with_colon_model():
    """Model names may contain ':'; the gateway split is on the last '@' only."""
    s = parse_player_spec("4:local:qwen3.6:27b@http://192.168.20.196:11441/v1")
    assert s.model == "qwen3.6:27b"
    assert s.gateway == "http://192.168.20.196:11441/v1"

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

def test_cli_codex_model_optional():
    s = parse_player_spec("2:cli-codex:gpt-5.5")
    assert s == PlayerSpec(2, "cli-codex", "gpt-5.5")
    assert s.driver_kind() == "cli"

def test_rejects_unknown_provider():
    with pytest.raises(ValueError):
        parse_player_spec("1:typo:model")

def test_arena_config_gateway_url_default():
    assert ArenaConfig(players=[]).gateway_url == "http://192.168.20.196:11444/v1"

def test_arena_config_idle_poll_limit_default():
    assert ArenaConfig(players=[]).idle_poll_limit == 600

def test_arena_config_run_id_default():
    assert ArenaConfig(players=[]).run_id == ""

def test_arena_config_transcript_dir_default():
    assert ArenaConfig(players=[]).transcript_dir == "arena_runs"
