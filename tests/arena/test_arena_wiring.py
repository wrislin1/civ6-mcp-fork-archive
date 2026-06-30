# tests/arena/test_arena_wiring.py
import asyncio
import os.path
import shutil
import pytest

from civ_mcp.arena.arena import build_policies, _run
from civ_mcp.arena.config import PlayerSpec, ArenaConfig
from civ_mcp.arena.agent import LLMPolicy
from civ_mcp.arena.cli_agent import CLIAgentPolicy

class FakeCost:
    def record(self, **kw): pass

def test_build_policies_routes_by_provider():
    specs = [
        PlayerSpec(1, "local", "qwen3-coder:30b"),
        PlayerSpec(2, "cli-claude", ""),
        PlayerSpec(3, "cli-codex", "gpt-5.5"),
    ]
    cfg = ArenaConfig(players=specs)
    policies, backend = build_policies(specs, FakeCost(), cfg)
    assert isinstance(policies[1], LLMPolicy)        # local → in-process LLM
    assert isinstance(policies[2], CLIAgentPolicy)   # cli-claude → CLI subprocess
    assert isinstance(policies[3], CLIAgentPolicy)   # cli-codex → CLI subprocess
    assert backend is not None                       # an in-process backend was constructed


def test_build_args_accepts_idle_poll_limit():
    from civ_mcp.arena.arena import build_args

    args = build_args(["--player", "1:cli-codex:gpt-5.5", "--idle-poll-limit", "12"])
    assert args.idle_poll_limit == 12


def test_cli_preflight_raises_when_claude_not_on_path(monkeypatch):
    """_run raises SystemExit before driving any turns if cli spec present but claude missing."""
    monkeypatch.setattr(shutil, "which", lambda name: None)

    class Args:
        player = ["1:cli-claude:"]
        max_puppet_turns = 1
        gateway_url = "http://localhost:11430/v1"
        api_key_env = "LITELLM_OPENAI_API_KEY"
        cost_path = "/tmp/test_arena_preflight.jsonl"
        max_agent_steps = 6
        dry_run = False

    with pytest.raises(SystemExit, match="claude"):
        asyncio.run(_run(Args()))


def test_cli_preflight_raises_when_codex_not_on_path(monkeypatch):
    """_run raises SystemExit before driving turns if a cli-codex spec is present but codex is missing."""
    monkeypatch.setattr(shutil, "which", lambda name: None)

    class Args:
        player = ["1:cli-codex:gpt-5.5"]
        max_puppet_turns = 1
        gateway_url = "http://localhost:11430/v1"
        api_key_env = "LITELLM_OPENAI_API_KEY"
        cost_path = "/tmp/test_arena_preflight.jsonl"
        max_agent_steps = 6
        dry_run = False

    with pytest.raises(SystemExit, match="codex"):
        asyncio.run(_run(Args()))


def test_cli_preflight_raises_when_mcp_config_missing(monkeypatch):
    """_run fails loudly if a cli spec is present but .mcp.json is not in CWD.

    The CLI civ uses project auto-discovery; without the project config, the headless
    subprocess silently starts without the civ6 MCP server.
    """
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(os.path, "isfile", lambda p: False)

    class Args:
        player = ["1:cli-claude:"]
        max_puppet_turns = 1
        gateway_url = "http://localhost:11430/v1"
        api_key_env = "LITELLM_OPENAI_API_KEY"
        cost_path = "/tmp/test_arena_preflight.jsonl"
        max_agent_steps = 6
        dry_run = False

    with pytest.raises(SystemExit, match=".mcp.json"):
        asyncio.run(_run(Args()))
