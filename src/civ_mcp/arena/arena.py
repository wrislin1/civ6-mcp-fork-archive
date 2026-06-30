# src/civ_mcp/arena/arena.py
from __future__ import annotations
import argparse, asyncio, json, os, shutil
from civ_mcp.connection import GameConnection
from civ_mcp.game_state import GameState
from civ_mcp.arena.config import (
    ArenaConfig,
    CLI_PROVIDER_COMMANDS,
    parse_player_spec,
    DEFAULT_GATEWAY_URL,
)
from civ_mcp.arena.cost import CostLog
from civ_mcp.arena.coordinator import run_arena, ScriptedPolicy

def build_policies(specs, cost, cfg):
    """Pure: specs -> ({player_id: policy}, in_proc_backend|None). No network on construct."""
    from civ_mcp.arena.cli_agent import CLIAgentPolicy
    from civ_mcp.arena.backends import OpenAICompatBackend
    from civ_mcp.arena.agent import LLMPolicy
    policies, in_proc_backend = {}, None
    for spec in specs:
        if spec.driver_kind() == "cli":
            policies[spec.player_id] = CLIAgentPolicy(
                spec.provider, cost, project_dir=os.getcwd(), model=spec.model)
        else:  # in_process local
            in_proc_backend = OpenAICompatBackend(
                cfg.gateway_url, os.environ.get(cfg.api_key_env, "x"), spec.model)
            policies[spec.player_id] = LLMPolicy(in_proc_backend, cost, max_steps=cfg.max_agent_steps)
    return policies, in_proc_backend

def build_args(argv=None):
    ap = argparse.ArgumentParser(prog="civ-arena")
    ap.add_argument("--player", action="append", default=[], help="'<id>:<provider>:<model>'")
    ap.add_argument("--max-puppet-turns", type=int, default=1)
    ap.add_argument("--gateway-url", default=DEFAULT_GATEWAY_URL)
    ap.add_argument("--api-key-env", default="LITELLM_OPENAI_API_KEY")
    ap.add_argument("--cost-path", default="arena_cost.jsonl")
    ap.add_argument("--max-agent-steps", type=int, default=6)
    ap.add_argument("--dry-run", action="store_true", help="scripted policy, no LLM")
    return ap.parse_args(argv)

async def _run(args):
    specs = [parse_player_spec(s) for s in args.player]
    cfg = ArenaConfig(players=specs, max_puppet_turns=args.max_puppet_turns,
                      gateway_url=args.gateway_url, api_key_env=args.api_key_env,
                      dry_run=args.dry_run, max_agent_steps=args.max_agent_steps,
                      cost_path=args.cost_path, puppet_ids=[s.player_id for s in specs])
    cost = CostLog(cfg.cost_path)
    policies, in_proc_backend = build_policies(specs, cost, cfg)
    if args.dry_run:
        sp = ScriptedPolicy()
        policy_for = lambda pid: sp
    else:
        if in_proc_backend is not None and not await in_proc_backend.reachable():
            raise SystemExit(f"in-process backend not reachable at {cfg.gateway_url}")
        if any(s.driver_kind() == "cli" for s in specs):
            for cmd in sorted({CLI_PROVIDER_COMMANDS[s.provider] for s in specs if s.driver_kind() == "cli"}):
                if shutil.which(cmd) is None:
                    raise SystemExit(f"cli provider requested but '{cmd}' not found on PATH")
            # cli-claude relies on Claude's project .mcp.json auto-discovery from CWD
            # (== project_dir). A missing config loads no civ6 server - a silent no-op.
            # cli-codex uses inline MCP config and does not need this file.
            if (
                any(s.provider == "cli-claude" for s in specs)
                and not os.path.isfile(os.path.join(os.getcwd(), ".mcp.json"))
            ):
                raise SystemExit(
                    f"cli provider requested but .mcp.json not found in CWD ({os.getcwd()}); "
                    "run the arena from the repo root")
        policy_for = lambda pid: policies[pid]
    conn = GameConnection(); await conn.connect()
    gs = GameState(conn)
    result = await run_arena(conn, gs, cfg, policy_for=policy_for)
    print(json.dumps({"result": result, "cost": cost.summary()}, indent=2))

def main():
    asyncio.run(_run(build_args()))
