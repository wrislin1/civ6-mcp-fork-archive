# tests/arena/test_arena_wiring.py
from civ_mcp.arena.arena import build_policies
from civ_mcp.arena.config import PlayerSpec, ArenaConfig
from civ_mcp.arena.agent import LLMPolicy
from civ_mcp.arena.cli_agent import CLIAgentPolicy

class FakeCost:
    def record(self, **kw): pass

def test_build_policies_routes_by_provider():
    specs = [PlayerSpec(1, "local", "qwen3-coder:30b"), PlayerSpec(2, "cli-claude", "")]
    cfg = ArenaConfig(players=specs)
    policies, backend = build_policies(specs, FakeCost(), cfg)
    assert isinstance(policies[1], LLMPolicy)        # local → in-process LLM
    assert isinstance(policies[2], CLIAgentPolicy)   # cli-claude → CLI subprocess
    assert backend is not None                       # an in-process backend was constructed
