import asyncio
import json
import os
import signal

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

def test_host_tools_disabled_and_civ6_denylist():
    pol = CLIAgentPolicy("cli-claude", FakeCost(), project_dir=".", max_turns=5)
    argv = pol._build_argv(player_id=2, turn=5)
    # verify --tools "" disables host built-in tools (Bash/Write/Edit/Read)
    assert "--tools" in argv
    tools_idx = argv.index("--tools")
    assert argv[tools_idx + 1] == ""
    # verify --disallowedTools still present with the denylist
    assert "--disallowedTools" in argv
    denied_idx = argv.index("--disallowedTools")
    denied_list = argv[denied_idx + 1]
    assert "mcp__civ6__end_turn" in denied_list
    assert "mcp__civ6__kill_game" in denied_list

def test_timeout_kills_process_group(monkeypatch):
    """Timeout must kill the whole process group to free port 4318 (MCP grandchild)."""
    kills_fallback = []

    class FakeProc:
        pid = 12345
        returncode = -9

        async def communicate(self):
            await asyncio.sleep(10)  # blocks long enough for wait_for to time out

        async def wait(self):
            pass  # async no-op — proc already dead

        def kill(self):
            kills_fallback.append("kill")

    create_calls = []

    async def fake_create(*args, **kwargs):
        create_calls.append(kwargs)
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    getpgid_calls = []
    killpg_calls = []

    monkeypatch.setattr(os, "getpgid", lambda pid: (getpgid_calls.append(pid) or 99999))
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: killpg_calls.append((pgid, sig)))

    pol = CLIAgentPolicy("cli-claude", FakeCost(), project_dir="/x", timeout_s=0.01)
    result = asyncio.run(pol(None, player_id=1, turn=1))

    # The timeout dict must match exactly
    assert result == {"summary": "cli timeout after 0.01s", "actions": [], "usage": {}}
    # subprocess must have been started with start_new_session=True
    assert create_calls and create_calls[0].get("start_new_session") is True
    # process-group kill must have been attempted, not the fallback proc.kill()
    assert getpgid_calls == [12345]
    assert killpg_calls == [(99999, signal.SIGKILL)]
    assert kills_fallback == []  # fallback must NOT have fired


def test_parse_claude_usage():
    pol = CLIAgentPolicy("cli-claude", FakeCost(), project_dir="/x")
    blob = json.dumps({"type": "result", "subtype": "success", "result": "settled & moved",
                       "total_cost_usd": 0.0123,
                       "usage": {"input_tokens": 1000, "output_tokens": 200}})
    summary, pt, ct, usd = pol._parse_claude(blob)
    assert summary == "settled & moved" and pt == 1000 and ct == 200 and usd == 0.0123
