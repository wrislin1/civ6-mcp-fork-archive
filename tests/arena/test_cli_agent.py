import json
from civ_mcp.arena.cli_agent import CLIAgentPolicy

class FakeCost:
    def __init__(self): self.records = []
    def record(self, **kw): self.records.append(kw)

def test_claude_argv_contains_mcp_and_safety():
    pol = CLIAgentPolicy("cli-claude", FakeCost(), project_dir="/x", max_turns=20)
    argv = pol._build_argv(player_id=2, turn=3)
    assert argv[0] == "claude"
    assert "-p" in argv and "--output-format" in argv and "json" in argv
    # restrict to civ6 tools and forbid ending the turn (host ends it)
    assert "--allowedTools" in argv and "mcp__civ6" in " ".join(argv)
    assert "--disallowedTools" in argv and "mcp__civ6__end_turn" in " ".join(argv)
    # deny destructive game-lifecycle tools — these must never be callable by the CLI agent
    assert "mcp__civ6__kill_game" in " ".join(argv)
    assert "mcp__civ6__load_game_save" in " ".join(argv)
    assert "mcp__civ6__restart_and_load" in " ".join(argv)
    assert "mcp__civ6__load_save" in " ".join(argv)
    assert "mcp__civ6__load_save_from_menu" in " ".join(argv)
    assert "mcp__civ6__launch_game" in " ".join(argv)
    # the prompt names the seat
    assert any("player 2" in a for a in argv)

def test_parse_claude_usage():
    pol = CLIAgentPolicy("cli-claude", FakeCost(), project_dir="/x")
    blob = json.dumps({"type": "result", "subtype": "success", "result": "settled & moved",
                       "total_cost_usd": 0.0123,
                       "usage": {"input_tokens": 1000, "output_tokens": 200}})
    summary, pt, ct, usd = pol._parse_claude(blob)
    assert summary == "settled & moved" and pt == 1000 and ct == 200 and usd == 0.0123
