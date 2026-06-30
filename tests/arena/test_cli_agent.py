import asyncio
import json
import os
import signal

import pytest

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
    # run_lua is the arbitrary-Lua escape hatch — it MUST be on the denylist too
    # (defense-in-depth; the server-side env disable is the decisive layer, see below)
    assert "mcp__civ6__run_lua" in denied_list


def test_run_lua_disabled_in_child_env(monkeypatch):
    """HOP 1 of layer-4: the `claude` subprocess must be spawned with CIV_MCP_DISABLE_LUA=1.
    This is necessary but NOT sufficient — claude does not auto-propagate it to the civ6 MCP
    server; the .mcp.json relay (HOP 2) is pinned by test_mcp_config_relays_lua_disable below."""
    captured = {}

    class FakeProc:
        pid = 1
        returncode = 0
        async def communicate(self):
            return (b'{"type":"result","result":"ok","usage":{},"total_cost_usd":0}', b"")
        async def wait(self):
            pass

    async def fake_create(*args, **kwargs):
        captured.update(kwargs)
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    pol = CLIAgentPolicy("cli-claude", FakeCost(), project_dir="/x", timeout_s=5)
    asyncio.run(pol(None, player_id=2, turn=3))
    assert captured.get("env", {}).get("CIV_MCP_DISABLE_LUA") == "1"
    # CIV_MCP_NO_WEB disables the civ6 uvicorn dashboard, whose capture_signals() otherwise
    # crashes the stdio MCP server under `claude -p` and leaves the CLI civ with no tools
    assert captured["env"].get("CIV_MCP_NO_WEB") == "1"
    # the rest of the host env is preserved (not replaced wholesale)
    assert "PATH" in captured["env"]

def test_mcp_config_relays_lua_disable():
    """HOP 2 of layer-4: setting CIV_MCP_DISABLE_LUA on the `claude` process is inert unless
    .mcp.json relays it into the civ6 server's own env block — Claude Code forwards only a
    minimal Posix env subset to stdio MCP servers, not arbitrary parent vars. Pin the relay so
    the server-enforced run_lua removal cannot silently regress to a denylist-only no-op."""
    repo_root = os.path.join(os.path.dirname(__file__), "..", "..")
    with open(os.path.join(repo_root, ".mcp.json")) as f:
        cfg = json.load(f)
    civ6_env = cfg["mcpServers"]["civ6"].get("env", {})
    assert "CIV_MCP_DISABLE_LUA" in civ6_env, (
        "civ6 .mcp.json must relay CIV_MCP_DISABLE_LUA into the server env block; "
        "without it the arena's CIV_MCP_DISABLE_LUA=1 never reaches the grandchild server "
        "and layer-4 silently falls back to the client denylist alone")
    # the value must reference the parent env var (e.g. ${CIV_MCP_DISABLE_LUA:-}) so the
    # arena's "1" actually flows through — a hard-coded constant would not pick it up
    assert "CIV_MCP_DISABLE_LUA" in civ6_env["CIV_MCP_DISABLE_LUA"]
    # same two-hop relay for CIV_MCP_NO_WEB (disables the uvicorn dashboard whose
    # capture_signals() crashes the stdio server under `claude -p`)
    assert "CIV_MCP_NO_WEB" in civ6_env
    assert "CIV_MCP_NO_WEB" in civ6_env["CIV_MCP_NO_WEB"]


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

    cost = FakeCost()
    pol = CLIAgentPolicy("cli-claude", cost, project_dir="/x", timeout_s=0.01)
    result = asyncio.run(pol(None, player_id=1, turn=1))

    # The timeout dict must match exactly
    assert result == {"summary": "cli timeout after 0.01s", "actions": [], "usage": {}}
    # subprocess must have been started with start_new_session=True
    assert create_calls and create_calls[0].get("start_new_session") is True
    # process-group kill must have been attempted, not the fallback proc.kill()
    assert getpgid_calls == [12345]
    assert killpg_calls == [(99999, signal.SIGKILL)]
    assert kills_fallback == []  # fallback must NOT have fired
    # the timed-out turn must be recorded (zero cost) — not silently dropped from the log
    assert len(cost.records) == 1
    assert cost.records[0]["turn"] == 1 and cost.records[0]["usd"] == 0.0


def test_cancel_kills_process_group(monkeypatch):
    """A real cancellation (CancelledError, not TimeoutError) must also kill the detached
    process group so the civ6-MCP child cannot orphan and hold port 4318 — then re-raise."""
    killpg_calls = []
    fallback = []

    class FakeProc:
        pid = 222
        async def communicate(self):
            return (b"", b"")
        async def wait(self):
            pass
        def kill(self):
            fallback.append("kill")

    async def fake_create(*args, **kwargs):
        return FakeProc()

    async def fake_wait_for(coro, timeout):
        coro.close()  # avoid "coroutine was never awaited" warning
        raise asyncio.CancelledError()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
    monkeypatch.setattr(os, "getpgid", lambda pid: 7)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: killpg_calls.append((pgid, sig)))

    pol = CLIAgentPolicy("cli-claude", FakeCost(), project_dir="/x", timeout_s=5)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(pol(None, player_id=1, turn=1))
    assert killpg_calls == [(7, signal.SIGKILL)]  # group killed on cancellation
    assert fallback == []


def test_strict_mcp_config_scopes_to_civ6_only():
    """--mcp-config .mcp.json + --strict-mcp-config must be present (layer 3 lockdown).

    This ensures the CLI civ subprocess only sees the civ6 MCP server and cannot
    reach user-scope servers (serena, Gmail, Google Drive, Claude Code Remote, etc.)
    which would otherwise be auto-approved under bypassPermissions.
    """
    pol = CLIAgentPolicy("cli-claude", FakeCost(), project_dir=".", max_turns=5)
    argv = pol._build_argv(player_id=2, turn=5)
    # layer 3: --mcp-config limits which servers are loaded; the path is anchored to
    # project_dir so it cannot silently resolve to a non-existent file off the repo root
    assert "--mcp-config" in argv
    mcp_config_idx = argv.index("--mcp-config")
    assert argv[mcp_config_idx + 1] == os.path.join(pol.project_dir, ".mcp.json")
    # --strict-mcp-config disables auto-discovery / inherited user-scope servers
    assert "--strict-mcp-config" in argv


def test_parse_claude_usage():
    pol = CLIAgentPolicy("cli-claude", FakeCost(), project_dir="/x")
    blob = json.dumps({"type": "result", "subtype": "success", "result": "settled & moved",
                       "total_cost_usd": 0.0123,
                       "usage": {"input_tokens": 1000, "output_tokens": 200}})
    summary, pt, ct, usd = pol._parse_claude(blob)
    assert summary == "settled & moved" and pt == 1000 and ct == 200 and usd == 0.0123

def test_parse_claude_null_fields():
    """Test that present-but-null JSON fields are coerced to safe defaults."""
    pol = CLIAgentPolicy("cli-claude", FakeCost(), project_dir="/x")
    blob = json.dumps({"type": "result", "result": None, "usage": {"input_tokens": None, "output_tokens": None}, "total_cost_usd": None})
    summary, pt, ct, usd = pol._parse_claude(blob)
    assert summary == "" and pt == 0 and ct == 0 and usd == 0.0
