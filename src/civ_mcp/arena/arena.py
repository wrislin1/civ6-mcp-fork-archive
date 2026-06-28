# src/civ_mcp/arena/arena.py
from __future__ import annotations
import argparse, asyncio, json, os
from civ_mcp.connection import GameConnection
from civ_mcp.game_state import GameState
from civ_mcp.arena.config import ArenaConfig, parse_player_spec
from civ_mcp.arena.cost import CostLog
from civ_mcp.arena.coordinator import run_arena, ScriptedPolicy

def build_args(argv=None):
    ap = argparse.ArgumentParser(prog="civ-arena")
    ap.add_argument("--player", action="append", default=[], help="'<id>:<provider>:<model>'")
    ap.add_argument("--max-puppet-turns", type=int, default=1)
    ap.add_argument("--gateway-url", default="http://192.168.20.146:4000/v1")
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
    conn = GameConnection(); await conn.connect()
    gs = GameState(conn)
    cost = CostLog(cfg.cost_path)
    if args.dry_run:
        policy = ScriptedPolicy()
    else:
        from civ_mcp.arena.backends import OpenAICompatBackend
        from civ_mcp.arena.agent import LLMPolicy
        spec = specs[0]
        backend = OpenAICompatBackend(cfg.gateway_url, os.environ.get(cfg.api_key_env, "x"), spec.model)
        if not await backend.reachable():
            raise SystemExit(f"gateway not reachable at {cfg.gateway_url}")
        policy = LLMPolicy(backend, cost, max_steps=cfg.max_agent_steps)
    result = await run_arena(conn, gs, cfg, policy=policy)
    print(json.dumps({"result": result, "cost": cost.summary()}, indent=2))

def main():
    asyncio.run(_run(build_args()))
