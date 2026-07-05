# tests/arena/test_arena_wiring.py
import asyncio
import os
import os.path
import shutil
import pytest

from civ_mcp.arena.arena import build_args, build_policies, resolve_config, _run
from civ_mcp.arena.config import (
    PlayerSpec,
    ArenaConfig,
    CivOptions,
    DEFAULT_GATEWAY_URL,
    parse_player_spec,
)
from civ_mcp.arena.agent import LLMPolicy
from civ_mcp.arena.cli_agent import CLIAgentPolicy
from civ_mcp.arena.cost import CostLog

class FakeCost:
    def record(self, **kw): pass

def test_build_policies_routes_by_provider():
    specs = [
        PlayerSpec(1, "local", "qwen3-coder:30b"),
        PlayerSpec(2, "cli-claude", ""),
        PlayerSpec(3, "cli-codex", "gpt-5.5"),
    ]
    cfg = ArenaConfig(players=specs)
    policies, local_backends = build_policies(specs, FakeCost(), cfg)
    assert isinstance(policies[1], LLMPolicy)        # local → in-process LLM
    assert isinstance(policies[2], CLIAgentPolicy)   # cli-claude → CLI subprocess
    assert isinstance(policies[3], CLIAgentPolicy)   # cli-codex → CLI subprocess
    assert len(local_backends) == 1                  # one local spec → one backend


def test_build_policies_two_local_specs_two_backends():
    """Two local players must each get their own backend (old code silently dropped the first)."""
    specs = [
        PlayerSpec(1, "local", "model-a"),
        PlayerSpec(2, "local", "model-b"),
    ]
    cfg = ArenaConfig(players=specs)
    policies, local_backends = build_policies(specs, FakeCost(), cfg)
    assert isinstance(policies[1], LLMPolicy)
    assert isinstance(policies[2], LLMPolicy)
    assert len(local_backends) == 2


def test_build_policies_cli_only_empty_local_backends():
    specs = [PlayerSpec(1, "cli-claude", ""), PlayerSpec(2, "cli-codex", "gpt-5.5")]
    cfg = ArenaConfig(players=specs)
    policies, local_backends = build_policies(specs, FakeCost(), cfg)
    assert local_backends == []


def test_build_policies_per_civ_gateway_pins_backend():
    """Each local civ's backend targets its own gateway when the spec pins one;
    civs without a pin fall back to the global cfg.gateway_url."""
    specs = [
        PlayerSpec(3, "local", "gemma4-26b", "http://192.168.20.196:11440/v1"),
        PlayerSpec(4, "local", "qwen3.6-27b", "http://192.168.20.196:11441/v1"),
        PlayerSpec(5, "local", "gemma4-26b"),  # no pin → global default
    ]
    cfg = ArenaConfig(players=specs, gateway_url="http://192.168.20.196:11444/v1")
    _policies, local_backends = build_policies(specs, FakeCost(), cfg)
    by_model_gw = {(b.model, b.base_url) for b in local_backends}
    assert ("gemma4-26b", "http://192.168.20.196:11440/v1") in by_model_gw
    assert ("qwen3.6-27b", "http://192.168.20.196:11441/v1") in by_model_gw
    # the un-pinned civ uses the global gateway
    assert ("gemma4-26b", "http://192.168.20.196:11444/v1") in by_model_gw


def test_build_args_accepts_idle_poll_limit():
    args = build_args(["--idle-poll-limit", "12"])
    assert args.idle_poll_limit == 12


def test_resolve_config_non_config_uses_arena_defaults():
    cfg = resolve_config(build_args(["--player", "3:local:m"]))
    assert cfg.max_puppet_turns == 1
    assert cfg.idle_poll_limit == 600
    assert cfg.gateway_url == DEFAULT_GATEWAY_URL
    assert cfg.max_agent_steps == 6


def test_build_args_accepts_config():
    a = build_args(["--config", "experiments/x.yaml"])
    assert a.config == "experiments/x.yaml"


def test_config_and_player_are_mutually_exclusive(tmp_path, capsys):
    with pytest.raises(SystemExit):
        resolve_config(build_args(["--config", "x.yaml", "--player", "1:local:m"]))


def test_resolve_config_from_file(tmp_path):
    p = tmp_path / "e.yaml"
    p.write_text(
        "max_puppet_turns: 12\ncivs:\n  - {player: 3, provider: local, model: m, max_steps: 9}\n"
    )
    cfg = resolve_config(build_args(["--config", str(p)]))
    assert cfg.max_puppet_turns == 12
    assert cfg.players[0].options.max_steps == 9


@pytest.mark.parametrize(
    ("argv_tail", "flag"),
    [
        (["--max-puppet-turns", "2"], "--max-puppet-turns"),
        (["--gateway-url", "http://example.invalid/v1"], "--gateway-url"),
        (["--idle-poll-limit", "601"], "--idle-poll-limit"),
        (["--max-agent-steps", "7"], "--max-agent-steps"),
    ],
)
def test_resolve_config_rejects_non_default_config_owned_flags(tmp_path, argv_tail, flag):
    p = tmp_path / "e.yaml"
    p.write_text("civs:\n  - {player: 3, provider: local, model: m}\n")
    with pytest.raises(SystemExit, match=flag):
        resolve_config(build_args(["--config", str(p), *argv_tail]))


@pytest.mark.parametrize(
    ("argv_tail", "flag"),
    [
        (["--max-puppet-turns", "1"], "--max-puppet-turns"),
        (["--gateway-url", DEFAULT_GATEWAY_URL], "--gateway-url"),
        (["--idle-poll-limit", "600"], "--idle-poll-limit"),
        (["--max-agent-steps", "6"], "--max-agent-steps"),
    ],
)
def test_resolve_config_rejects_config_owned_flags_even_when_default_value_passed(
    tmp_path, argv_tail, flag
):
    p = tmp_path / "e.yaml"
    p.write_text("civs:\n  - {player: 3, provider: local, model: m}\n")
    with pytest.raises(SystemExit, match=flag):
        resolve_config(build_args(["--config", str(p), *argv_tail]))


def test_build_policies_threads_options(tmp_path):
    spec = parse_player_spec("3:local:m")
    object.__setattr__(spec, "options", CivOptions(max_steps=11, tools="standard"))
    cfg = ArenaConfig(players=[spec])
    cost = CostLog(str(tmp_path / "c.jsonl"))
    policies, backends = build_policies([spec], cost, cfg)
    pol = policies[3]
    assert pol.max_steps == 11
    assert any(t["function"]["name"] == "get_map_area" for t in pol._tools)


def test_player_shorthand_honors_max_agent_steps():
    cfg = resolve_config(build_args(["--player", "3:local:m", "--max-agent-steps", "12"]))
    assert cfg.players[0].options.max_steps == 12


def test_run_uses_file_run_id_for_config(tmp_path, monkeypatch):
    cfg_path = tmp_path / "e.yaml"
    cfg_path.write_text(
        "run_id: file-run\nmax_puppet_turns: 1\ncivs:\n  - {player: 3, provider: local, model: m}\n"
    )
    run_root = tmp_path / "runs"
    captured = {}

    class FakeConn:
        async def connect(self):
            captured["connected"] = True

    def fake_game_state(conn):
        captured["gs_conn"] = conn
        return {"conn": conn}

    async def fake_run_arena(conn, gs, cfg, policy_for, transcript):
        captured["conn"] = conn
        captured["gs"] = gs
        captured["cfg"] = cfg
        captured["transcript"] = transcript
        captured["policy"] = policy_for(3)
        return {"ok": True}

    monkeypatch.setattr("civ_mcp.arena.arena.GameConnection", FakeConn)
    monkeypatch.setattr("civ_mcp.arena.arena.GameState", fake_game_state)
    monkeypatch.setattr("civ_mcp.arena.arena.run_arena", fake_run_arena)

    asyncio.run(
        _run(build_args(["--config", str(cfg_path), "--dry-run", "--transcript-dir", str(run_root)]))
    )

    cfg = captured["cfg"]
    assert cfg.run_id == "file-run"
    assert cfg.cost_path == str(run_root / "file-run" / "arena_cost.jsonl")
    assert cfg.transcript_dir == str(run_root)
    assert captured["transcript"].path == str(run_root / "file-run" / "transcript.jsonl")
    assert os.path.isdir(run_root / "file-run")


def test_config_yaml_run_id_survives_when_cli_run_id_absent(tmp_path):
    p = tmp_path / "e.yaml"
    p.write_text("run_id: file-run\ncivs:\n  - {player: 3, provider: local, model: m}\n")

    args = build_args(["--config", str(p)])
    cfg = resolve_config(args)

    assert args.run_id is None
    assert cfg.run_id == "file-run"


def test_config_rejects_cli_run_id_when_yaml_run_id_present(tmp_path):
    p = tmp_path / "e.yaml"
    p.write_text("run_id: file-run\ncivs:\n  - {player: 3, provider: local, model: m}\n")

    with pytest.raises(SystemExit, match="--run-id"):
        resolve_config(build_args(["--config", str(p), "--run-id", "cli-run"]))


def test_config_rejects_empty_cli_run_id_when_yaml_run_id_present(tmp_path):
    p = tmp_path / "e.yaml"
    p.write_text("run_id: file-run\ncivs:\n  - {player: 3, provider: local, model: m}\n")

    with pytest.raises(SystemExit, match="--run-id"):
        resolve_config(build_args(["--config", str(p), "--run-id", ""]))


def test_cli_preflight_raises_when_claude_not_on_path(monkeypatch, tmp_path):
    """_run raises SystemExit before driving any turns if cli spec present but claude missing."""
    monkeypatch.setattr(shutil, "which", lambda name: None)

    class Args:
        player = ["1:cli-claude:"]
        max_puppet_turns = 1
        gateway_url = "http://localhost:11430/v1"
        api_key_env = "LITELLM_OPENAI_API_KEY"
        cost_path = str(tmp_path / "cost.jsonl")
        max_agent_steps = 6
        dry_run = False
        run_id = ""
        transcript_dir = str(tmp_path / "runs")
        no_transcript = True

    with pytest.raises(SystemExit, match="claude"):
        asyncio.run(_run(Args()))


def test_cli_preflight_raises_when_codex_not_on_path(monkeypatch, tmp_path):
    """_run raises SystemExit before driving turns if a cli-codex spec is present but codex is missing."""
    monkeypatch.setattr(shutil, "which", lambda name: None)

    class Args:
        player = ["1:cli-codex:gpt-5.5"]
        max_puppet_turns = 1
        gateway_url = "http://localhost:11430/v1"
        api_key_env = "LITELLM_OPENAI_API_KEY"
        cost_path = str(tmp_path / "cost.jsonl")
        max_agent_steps = 6
        dry_run = False
        run_id = ""
        transcript_dir = str(tmp_path / "runs")
        no_transcript = True

    with pytest.raises(SystemExit, match="codex"):
        asyncio.run(_run(Args()))


def test_cli_preflight_raises_when_mcp_config_missing(monkeypatch, tmp_path):
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
        cost_path = str(tmp_path / "cost.jsonl")
        max_agent_steps = 6
        dry_run = False
        run_id = ""
        transcript_dir = str(tmp_path / "runs")
        no_transcript = True

    with pytest.raises(SystemExit, match=".mcp.json"):
        asyncio.run(_run(Args()))


def test_run_rejects_path_traversal_run_id(tmp_path):
    """A CLI --run-id must not escape the transcript dir (the YAML loader already
    guards this; _run applies the same check at the single choke point)."""
    class Args:
        player = ["1:local:m"]
        max_puppet_turns = 1
        gateway_url = "http://localhost:11430/v1"
        api_key_env = "LITELLM_OPENAI_API_KEY"
        cost_path = str(tmp_path / "cost.jsonl")
        max_agent_steps = 6
        dry_run = True
        run_id = "../../evil"
        transcript_dir = str(tmp_path / "runs")
        no_transcript = True

    with pytest.raises(SystemExit, match="invalid run_id"):
        asyncio.run(_run(Args()))
    # Nothing should have been created outside the transcript dir.
    assert not (tmp_path.parent / "evil").exists()
