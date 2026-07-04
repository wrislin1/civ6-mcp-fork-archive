# src/civ_mcp/arena/arena.py
from __future__ import annotations
import argparse, asyncio, json, os, shutil
from dataclasses import replace
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
    """Pure: specs -> ({player_id: policy}, local_backends). No network on construct."""
    from civ_mcp.arena.cli_agent import CLIAgentPolicy
    from civ_mcp.arena.backends import OpenAICompatBackend
    from civ_mcp.arena.agent import LLMPolicy
    policies, local_backends = {}, []
    for spec in specs:
        if spec.driver_kind() == "cli":
            policies[spec.player_id] = CLIAgentPolicy(
                spec.provider, cost, project_dir=os.getcwd(), model=spec.model)
        else:  # in_process local
            backend = OpenAICompatBackend(
                spec.gateway or cfg.gateway_url,   # per-civ gateway override, else the global default
                os.environ.get(cfg.api_key_env, "x"), spec.model)
            local_backends.append(backend)
            policies[spec.player_id] = LLMPolicy(
                backend, cost, max_steps=cfg.max_agent_steps, options=spec.options)
    return policies, local_backends

def build_args(argv=None):
    ap = argparse.ArgumentParser(prog="civ-arena")
    ap.add_argument("--player", action="append", default=[],
                    help="'<id>:<provider>:<model>[@<gateway>]' (local civ may pin its own gateway)")
    ap.add_argument("--config", default="",
                    help="YAML experiment file (mutually exclusive with --player)")
    ap.add_argument("--max-puppet-turns", type=int, default=1)
    ap.add_argument("--gateway-url", default=DEFAULT_GATEWAY_URL)
    ap.add_argument("--api-key-env", default="LITELLM_OPENAI_API_KEY")
    ap.add_argument("--cost-path", default="", help="path for cost log (default: auto under run dir)")
    ap.add_argument("--run-id", default="", help="run ID (generated if empty)")
    ap.add_argument("--transcript-dir", default="arena_runs", help="base directory for run dirs")
    ap.add_argument("--no-transcript", action="store_true", help="disable transcript writing")
    ap.add_argument("--max-agent-steps", type=int, default=6)
    ap.add_argument("--idle-poll-limit", type=int, default=600,
                    help="number of 1s polls to wait for puppet turns before exiting")
    ap.add_argument("--dry-run", action="store_true", help="scripted policy, no LLM")
    return ap.parse_args(argv)

def resolve_config(args) -> ArenaConfig:
    from civ_mcp.arena.experiment import load_experiment

    config_path = getattr(args, "config", "")
    if config_path and args.player:
        raise SystemExit("--config and --player are mutually exclusive")
    if config_path:
        rejected = []
        if args.max_puppet_turns != 1:
            rejected.append("--max-puppet-turns")
        if args.gateway_url != DEFAULT_GATEWAY_URL:
            rejected.append("--gateway-url")
        if getattr(args, "idle_poll_limit", 600) != 600:
            rejected.append("--idle-poll-limit")
        if args.max_agent_steps != 6:
            rejected.append("--max-agent-steps")
        if rejected:
            flags = ", ".join(rejected)
            raise SystemExit(f"--config does not allow overriding config-owned flags: {flags}")
        cfg = load_experiment(config_path)
        cfg.dry_run = args.dry_run
        cfg.api_key_env = args.api_key_env
        return cfg

    specs = [parse_player_spec(s) for s in args.player]
    if args.max_agent_steps != 6:
        updated = []
        for spec in specs:
            if spec.provider == "local":
                opts = replace(spec.options, max_steps=args.max_agent_steps)
                spec = replace(spec, options=opts)
            updated.append(spec)
        specs = updated
    return ArenaConfig(players=specs, max_puppet_turns=args.max_puppet_turns,
                       gateway_url=args.gateway_url, api_key_env=args.api_key_env,
                       dry_run=args.dry_run, max_agent_steps=args.max_agent_steps,
                       idle_poll_limit=getattr(args, "idle_poll_limit", 600),
                       puppet_ids=[s.player_id for s in specs])

async def _run(args):
    from pathlib import Path
    from civ_mcp.run_id import generate_run_id
    from civ_mcp.arena.transcript import TranscriptSink, NullSink
    cfg = resolve_config(args)
    specs = cfg.players
    run_id = args.run_id or cfg.run_id or generate_run_id()
    run_dir = Path(args.transcript_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)            # BEFORE CostLog (cost.py opens path directly)
    cost_path = args.cost_path or str(run_dir / "arena_cost.jsonl")
    cost = CostLog(cost_path)
    transcript = (TranscriptSink(str(run_dir / "transcript.jsonl"))
                  if not args.no_transcript else NullSink())
    cfg.cost_path = cost_path
    cfg.run_id = run_id
    cfg.transcript_dir = args.transcript_dir
    policies, local_backends = build_policies(specs, cost, cfg)
    if args.dry_run:
        sp = ScriptedPolicy()
        policy_for = lambda pid: sp
    else:
        for b in local_backends:                              # check EVERY local model
            if not await b.reachable():
                raise SystemExit(f"local backend not reachable at {b.base_url} (model {b.model})")
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
    result = await run_arena(conn, gs, cfg, policy_for=policy_for, transcript=transcript)
    print(json.dumps({"result": result, "cost": cost.summary()}, indent=2))

def main():
    asyncio.run(_run(build_args()))
