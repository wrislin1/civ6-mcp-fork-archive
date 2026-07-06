import asyncio
import json
import os
import signal

import pytest

from civ_mcp.arena.cli_agent import CLIAgentPolicy
from civ_mcp.arena.config import CivOptions, MemoryOptions, TaskTrackerOptions

def _prompt(pid=2, turn=3):
    """Build a representative prompt string for _build_argv tests that don't care
    about the exact prompt contents, only that it round-trips through argv."""
    return f"You are playing player {pid}; it is turn {turn}. Do NOT end the turn."


class FakeCost:
    def __init__(self): self.records = []
    def record(self, **kw): self.records.append(kw)

# ---------------------------------------------------------------------------
# Fixtures for stream-json parsers.
# Modeled on REAL captured stdout (live Task 9 shake-out): claude --output-format
# stream-json (tool_use/tool_result blocks) and codex exec --json (item.completed
# with mcp_tool_call / agent_message items). Values trimmed for size.
# ---------------------------------------------------------------------------

_CLAUDE_STREAM_FIXTURE = "\n".join([
    json.dumps({"type": "system", "subtype": "init", "session_id": "s1"}),
    json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "toolu_abc",
         "name": "mcp__civ6__get_game_overview", "input": {"turn": 3}}
    ]}}),
    json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "toolu_abc",
         "content": "Turn 3 overview data"}
    ]}}),
    json.dumps({"type": "result", "subtype": "success",
                "result": "Moved scout north",
                "usage": {"input_tokens": 500, "output_tokens": 100},
                "total_cost_usd": 0.005}),
])

# Real codex schema: item.completed carries mcp_tool_call (tool/arguments/result) and
# agent_message (text); result is structured {"content":[{"type":"text","text":...}]}.
_CODEX_STREAM_FIXTURE = "\n".join([
    json.dumps({"type": "thread.started", "thread_id": "t1"}),
    json.dumps({"type": "turn.started"}),
    json.dumps({"type": "item.completed", "item": {
        "id": "item_0", "type": "agent_message",
        "text": "I'll inspect player 2's start and make early setup moves."}}),
    json.dumps({"type": "item.completed", "item": {
        "id": "item_1", "type": "mcp_tool_call", "server": "civ6",
        "tool": "get_units", "arguments": {}, "status": "completed", "error": None,
        "result": {"content": [{"type": "text",
                   "text": "1 units:\n  Warrior (UNIT_WARRIOR) at (26,23) — moves 2/2"}],
                   "structured_content": {"result": "1 units: Warrior ..."}}}}),
    json.dumps({"type": "item.completed", "item": {
        "id": "item_2", "type": "mcp_tool_call", "server": "civ6",
        "tool": "unit_action",
        "arguments": {"action": "move", "unit_id": 131073, "target_x": 26, "target_y": 22},
        "status": "completed", "error": None,
        "result": {"content": [{"type": "text", "text": "MOVING_TO|26,22|from:26,23"}]}}}),
    json.dumps({"type": "item.completed", "item": {
        "id": "item_3", "type": "agent_message", "text": "Moved scout north"}}),
    json.dumps({"type": "turn.completed",
                "usage": {"input_tokens": 300, "output_tokens": 50}}),
])

# A codex tool call that the civ6 MCP rejected (bad args) — same "Error executing tool"
# framing the server uses; modeled on the real claude error shape since codex shares the MCP.
_CODEX_ERROR_FIXTURE = "\n".join([
    json.dumps({"type": "item.completed", "item": {
        "id": "item_0", "type": "mcp_tool_call", "server": "civ6",
        "tool": "get_map_area", "arguments": {}, "status": "completed", "error": None,
        "result": {"content": [{"type": "text",
                   "text": "Error executing tool get_map_area: 2 validation errors for "
                           "get_map_areaArguments\ncenter_x\n  Field required"}]}}}),
    json.dumps({"type": "turn.completed",
                "usage": {"input_tokens": 100, "output_tokens": 10}}),
])


# ---------------------------------------------------------------------------
# Argv tests
# ---------------------------------------------------------------------------

def test_claude_argv_contains_mcp_and_safety():
    pol = CLIAgentPolicy("cli-claude", FakeCost(), project_dir="/x", max_turns=20)
    argv = pol._build_argv(_prompt(2, 3))
    assert argv[0] == "claude"
    # stream-json replaces the old plain-json flag (Task 3)
    assert "-p" in argv and "--output-format" in argv and "stream-json" in argv
    assert "--verbose" in argv
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


def test_claude_argv_stream_json_explicit():
    """The --output-format value must be exactly 'stream-json', not 'json'."""
    pol = CLIAgentPolicy("cli-claude", FakeCost(), project_dir="/x", max_turns=20)
    argv = pol._build_argv(_prompt(1, 1))
    fmt_idx = argv.index("--output-format")
    assert argv[fmt_idx + 1] == "stream-json", (
        f"Expected 'stream-json' after --output-format, got {argv[fmt_idx + 1]!r}"
    )
    # The bare "json" string must NOT appear at the format value position
    assert argv[fmt_idx + 1] != "json"
    assert "--verbose" in argv


def test_codex_argv_contains_inline_civ6_mcp_and_safety():
    pol = CLIAgentPolicy("cli-codex", FakeCost(), project_dir="/x", model="gpt-5.5", max_turns=20)
    argv = pol._build_argv(_prompt(2, 3))
    assert argv[:2] == ["codex", "exec"]
    assert "--json" in argv
    assert "--ignore-user-config" in argv
    assert "--skip-git-repo-check" in argv
    assert "-C" in argv and argv[argv.index("-C") + 1] == "/x"
    assert "-m" in argv and argv[argv.index("-m") + 1] == "gpt-5.5"
    assert "-s" in argv and argv[argv.index("-s") + 1] == "danger-full-access"
    joined = " ".join(argv)
    assert 'mcp_servers.civ6.command="uv"' in joined
    assert 'mcp_servers.civ6.args=["run","--directory",".","civ-mcp"]' in joined
    assert 'CIV_MCP_ARENA_PUPPET="1"' in joined
    assert 'CIV_MCP_DISABLE_LUA="1"' in joined
    assert 'CIV_MCP_NO_WEB="1"' in joined
    # Codex has no per-invocation MCP denylist; arena puppet mode removes lifecycle tools server-side.
    assert "--disallowedTools" not in argv

def test_host_tools_and_destructive_civ6_tools_denied_without_tools_flag():
    pol = CLIAgentPolicy("cli-claude", FakeCost(), project_dir=".", max_turns=5)
    argv = pol._build_argv(_prompt(2, 5))
    # `--tools ""` disables the civ6 stdio MCP tools under headless `claude -p`.
    # Keep MCP tools available and deny host built-ins through the explicit denylist instead.
    assert "--tools" not in argv
    assert "--disallowedTools" in argv
    denied_idx = argv.index("--disallowedTools")
    denied_list = argv[denied_idx + 1]
    assert "Bash" in denied_list
    assert "Read" in denied_list
    assert "Write" in denied_list
    assert "Edit" in denied_list
    assert "NotebookEdit" in denied_list
    # Stale names that no longer exist in Claude Code (2.1.196) must NOT be re-added — each
    # emits a "deny rule matches no known tool" warning on stderr and protects nothing.
    for stale in ("MultiEdit", "NotebookRead", "LS"):
        assert stale not in denied_list.split()
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
    assert captured["env"].get("CIV_MCP_ARENA_PUPPET") == "1"
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
    # Claude's stdio MCP env relay must also carry arena puppet mode so lifecycle tools
    # are removed server-side, matching the Codex inline MCP config path.
    assert "CIV_MCP_ARENA_PUPPET" in civ6_env
    assert "CIV_MCP_ARENA_PUPPET" in civ6_env["CIV_MCP_ARENA_PUPPET"]


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

    # Core return keys must remain unchanged — transcript is additive
    assert result["summary"] == "cli timeout after 0.01s"
    assert result["actions"] == []
    assert result["usage"] == {}
    # Transcript must be present on timeout branch
    assert "transcript" in result
    tr = result["transcript"]
    assert tr["steps"] == []
    assert tr["reason"] == "timeout"
    assert isinstance(tr["wall_clock_s"], float) and tr["wall_clock_s"] >= 0
    assert tr["final_summary"] == "cli timeout after 0.01s"
    assert tr["invalid_tool_calls"] == []
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


def test_project_auto_discovery_scoped_to_project_settings():
    """Use project auto-discovery, but exclude user-scope settings.

    Live headless testing on the gaming PC showed that passing `--mcp-config` for the
    civ6 stdio server does not expose its tools to `claude -p`, while project
    auto-discovery does. `--setting-sources project,local` preserves that working
    path and keeps user-scope MCP servers out of the bypassPermissions subprocess.
    """
    pol = CLIAgentPolicy("cli-claude", FakeCost(), project_dir=".", max_turns=5)
    argv = pol._build_argv(_prompt(2, 5))
    assert "--mcp-config" not in argv
    assert "--strict-mcp-config" not in argv
    assert "--setting-sources" in argv
    setting_sources_idx = argv.index("--setting-sources")
    assert argv[setting_sources_idx + 1] == "project,local"


# ---------------------------------------------------------------------------
# _parse_claude / _parse_codex — parse tuple must be byte-identical to today's
# ---------------------------------------------------------------------------

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


def test_parse_claude_stream_fixture_tuple_unchanged():
    """_parse_claude must return the same (summary, in, out, usd) on stream-json fixture
    as it would from a plain result object — the line-by-line scan finds the terminal
    {"type":"result",...} and the tuple logic is unchanged."""
    summary, pt, ct, usd = CLIAgentPolicy._parse_claude(_CLAUDE_STREAM_FIXTURE)
    assert summary == "Moved scout north"
    assert pt == 500
    assert ct == 100
    assert abs(usd - 0.005) < 1e-9


def test_parse_codex_json_events():
    pol = CLIAgentPolicy("cli-codex", FakeCost(), project_dir="/x")
    blob = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "t"}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Turn 4"}}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 123, "output_tokens": 45}}),
    ])
    summary, pt, ct, usd = pol._parse_codex(blob)
    assert summary == "Turn 4"
    assert pt == 123
    assert ct == 45
    assert usd == 0.0


def test_parse_codex_stream_fixture_tuple_unchanged():
    """_parse_codex returns correct (summary, in, out, usd) from synthetic codex fixture."""
    summary, pt, ct, usd = CLIAgentPolicy._parse_codex(_CODEX_STREAM_FIXTURE)
    assert summary == "Moved scout north"
    assert pt == 300
    assert ct == 50
    assert usd == 0.0


# ---------------------------------------------------------------------------
# _stream_steps_claude — defensive parser
# ---------------------------------------------------------------------------

def test_stream_steps_claude_recovers_tool_step():
    """_stream_steps_claude must recover ≥1 tool step with paired tool_result from fixture."""
    steps = CLIAgentPolicy._stream_steps_claude(_CLAUDE_STREAM_FIXTURE)
    assert isinstance(steps, list)
    assert len(steps) >= 1
    tool_steps = [s for s in steps if s.get("tool_name")]
    assert len(tool_steps) >= 1, f"No tool step found in: {steps}"
    ts = tool_steps[0]
    assert ts["tool_name"] == "mcp__civ6__get_game_overview"
    assert ts["tool_args"] == {"turn": 3}
    # tool_result should be paired back from the user/tool_result block
    assert ts["tool_result_full"] is not None
    assert "Turn 3 overview" in ts["tool_result_full"]


def test_stream_steps_claude_step_schema():
    """Every step dict must have the required schema keys."""
    steps = CLIAgentPolicy._stream_steps_claude(_CLAUDE_STREAM_FIXTURE)
    required = {"idx", "role", "text", "tool_name", "tool_args", "tool_result_full", "ts"}
    for s in steps:
        missing = required - set(s.keys())
        assert not missing, f"Step {s} missing keys: {missing}"


def test_stream_steps_claude_defensive_on_garbage():
    """_stream_steps_claude must not raise on unparseable / empty input."""
    assert CLIAgentPolicy._stream_steps_claude("") == []
    assert CLIAgentPolicy._stream_steps_claude("not json\n{bad json}\n") == []
    assert isinstance(CLIAgentPolicy._stream_steps_claude("null\n\n"), list)


def test_stream_steps_claude_defensive_on_missing_fields():
    """Partially malformed lines are skipped; well-formed lines still parsed."""
    malformed_mix = "\n".join([
        "not json at all",
        json.dumps({"type": "assistant"}),  # missing message
        json.dumps({"type": "assistant", "message": {"content": "not a list"}}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "mcp__civ6__get_units", "input": {}}
        ]}}),
        json.dumps({"type": "result", "result": "ok", "usage": {}, "total_cost_usd": 0}),
    ])
    steps = CLIAgentPolicy._stream_steps_claude(malformed_mix)
    assert isinstance(steps, list)
    # The valid tool_use must still be captured
    assert any(s.get("tool_name") == "mcp__civ6__get_units" for s in steps)


# ---------------------------------------------------------------------------
# _stream_steps_codex — defensive parser
# ---------------------------------------------------------------------------

def test_stream_steps_codex_recovers_tool_step():
    """_stream_steps_codex must extract mcp_tool_call items with name, args, and the
    structured result text (the real-schema fix — old parser left every step tool=None)."""
    steps = CLIAgentPolicy._stream_steps_codex(_CODEX_STREAM_FIXTURE)
    assert isinstance(steps, list)
    tool_steps = [s for s in steps if s.get("tool_name")]
    assert len(tool_steps) == 2, f"Expected 2 mcp_tool_call steps, got: {steps}"
    by_name = {s["tool_name"]: s for s in tool_steps}
    assert "get_units" in by_name and "unit_action" in by_name
    # args extracted from item['arguments']
    assert by_name["unit_action"]["tool_args"]["action"] == "move"
    # result text extracted from the structured result.content[].text
    assert "MOVING_TO|26,22" in by_name["unit_action"]["tool_result_full"]
    assert "Warrior" in by_name["get_units"]["tool_result_full"]
    # agent_message items become text steps (no tool_name)
    assert any(s.get("text") == "Moved scout north" and not s.get("tool_name") for s in steps)


def test_stream_steps_codex_error_result_detected_as_invalid():
    """A codex mcp_tool_call rejected by the MCP surfaces 'Error executing tool ...' and is
    caught by _detect_invalid_tool_calls (Finding 3 generalizes to codex)."""
    steps = CLIAgentPolicy._stream_steps_codex(_CODEX_ERROR_FIXTURE)
    tool_steps = [s for s in steps if s.get("tool_name")]
    assert len(tool_steps) == 1 and tool_steps[0]["tool_name"] == "get_map_area"
    assert tool_steps[0]["tool_result_full"].startswith("Error executing tool")
    inv = CLIAgentPolicy._detect_invalid_tool_calls(steps)
    assert len(inv) == 1 and inv[0]["reason"] == "bad_arguments"


def test_stream_steps_codex_step_schema():
    """Every step dict must have the required schema keys."""
    steps = CLIAgentPolicy._stream_steps_codex(_CODEX_STREAM_FIXTURE)
    required = {"idx", "role", "text", "tool_name", "tool_args", "tool_result_full", "ts"}
    for s in steps:
        missing = required - set(s.keys())
        assert not missing, f"Step {s} missing keys: {missing}"


def test_stream_steps_codex_defensive_on_garbage():
    """_stream_steps_codex must not raise on unparseable input."""
    assert CLIAgentPolicy._stream_steps_codex("") == []
    assert CLIAgentPolicy._stream_steps_codex("garbage\n{}\n") == []
    assert isinstance(CLIAgentPolicy._stream_steps_codex("null\n"), list)


# ---------------------------------------------------------------------------
# __call__ — transcript attachment (success and timeout branches)
# ---------------------------------------------------------------------------

def test_call_success_attaches_transcript(monkeypatch):
    """On success, __call__ must return result with transcript containing steps and metadata."""
    class FakeProc:
        pid = 1
        returncode = 0
        async def communicate(self):
            return (
                _CLAUDE_STREAM_FIXTURE.encode(),
                b"some stderr output",
            )
        async def wait(self):
            pass

    async def fake_create(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    pol = CLIAgentPolicy("cli-claude", FakeCost(), project_dir="/x", timeout_s=5)
    result = asyncio.run(pol(None, player_id=1, turn=1))

    # Core keys unchanged
    assert result["summary"] == "Moved scout north"
    assert result["actions"] == []
    assert result["usage"]["prompt_tokens"] == 500
    assert result["usage"]["completion_tokens"] == 100
    # Transcript attached
    assert "transcript" in result
    tr = result["transcript"]
    assert isinstance(tr["steps"], list)
    assert len(tr["steps"]) >= 1
    assert isinstance(tr["wall_clock_s"], float) and tr["wall_clock_s"] >= 0
    assert tr["final_summary"] == "Moved scout north"
    assert tr["cli_exit"] == 0
    assert "some stderr" in tr["cli_stderr_tail"]
    assert tr["invalid_tool_calls"] == []


def test_call_success_attaches_transcript_codex(monkeypatch):
    """Codex path also attaches transcript on success."""
    class FakeProc:
        pid = 2
        returncode = 0
        async def communicate(self):
            return (_CODEX_STREAM_FIXTURE.encode(), b"")
        async def wait(self):
            pass

    async def fake_create(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    pol = CLIAgentPolicy("cli-codex", FakeCost(), project_dir="/x", timeout_s=5)
    result = asyncio.run(pol(None, player_id=1, turn=1))

    assert result["summary"] == "Moved scout north"
    assert "transcript" in result
    tr = result["transcript"]
    assert isinstance(tr["steps"], list)
    assert len(tr["steps"]) >= 1


def test_call_success_transcript_carries_token_counts(monkeypatch):
    """SUCCESS path: transcript must include prompt_tokens and completion_tokens (Finding 2)."""
    class FakeProc:
        pid = 1
        returncode = 0
        async def communicate(self):
            return (_CLAUDE_STREAM_FIXTURE.encode(), b"")
        async def wait(self):
            pass

    async def fake_create(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    pol = CLIAgentPolicy("cli-claude", FakeCost(), project_dir="/x", timeout_s=5)
    result = asyncio.run(pol(None, player_id=1, turn=1))

    tr = result["transcript"]
    assert tr["prompt_tokens"] == 500
    assert tr["completion_tokens"] == 100


def test_call_timeout_transcript_carries_zero_token_counts(monkeypatch):
    """TIMEOUT path: transcript must include prompt_tokens=0 and completion_tokens=0 (Finding 2)."""
    class FakeProc:
        pid = 55555
        returncode = -9

        async def communicate(self):
            await asyncio.sleep(10)

        async def wait(self):
            pass

        def kill(self):
            pass

    async def fake_create(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    monkeypatch.setattr(os, "getpgid", lambda pid: 99998)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: None)

    pol = CLIAgentPolicy("cli-claude", FakeCost(), project_dir="/x", timeout_s=0.01)
    result = asyncio.run(pol(None, player_id=1, turn=1))

    tr = result["transcript"]
    assert tr["prompt_tokens"] == 0
    assert tr["completion_tokens"] == 0


def test_call_parser_exception_yields_empty_steps(monkeypatch):
    """If _stream_steps_claude raises unexpectedly, steps=[]; summary/usage must be unchanged."""
    class FakeProc:
        pid = 3
        returncode = 0
        async def communicate(self):
            return (b'{"type":"result","result":"ok","usage":{"input_tokens":10,"output_tokens":5},"total_cost_usd":0.001}', b"")
        async def wait(self):
            pass

    async def fake_create(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    def boom(stdout):
        raise RuntimeError("simulated parser crash")

    monkeypatch.setattr(CLIAgentPolicy, "_stream_steps_claude", staticmethod(boom))
    pol = CLIAgentPolicy("cli-claude", FakeCost(), project_dir="/x", timeout_s=5)
    result = asyncio.run(pol(None, player_id=1, turn=1))

    assert result["summary"] == "ok"
    assert result["usage"]["prompt_tokens"] == 10
    # Parser crash must NOT propagate — steps is empty list
    assert result["transcript"]["steps"] == []


# ---------------------------------------------------------------------------
# _detect_invalid_tool_calls — Finding 3 CLI invalid-call detection (real signal)
# ---------------------------------------------------------------------------

def test_detect_invalid_tool_calls_classifies():
    """The civ6 MCP frames a malformed call as 'Error executing tool <name>: ...'.
    Game-rejected VALID actions ('...|BLOCKED', 'Error: CANNOT_START|...') are NOT
    invalid calls — they belong to the analyze rubric, not here."""
    steps = [
        {"tool_name": "mcp__civ6__get_units", "tool_result_full": "2 units: Settler ..."},
        {"tool_name": "mcp__civ6__get_map_area",
         "tool_result_full": "Error executing tool get_map_area: 2 validation errors for "
                             "get_map_areaArguments\ncenter_x\n  Field required"},
        {"tool_name": "mcp__civ6__bogus_tool",
         "tool_result_full": "Error executing tool bogus_tool: tool not found"},
        {"tool_name": None, "tool_result_full": "Error executing tool x: whatever"},
        {"tool_name": "mcp__civ6__move_unit", "tool_result_full": "MOVING_TO|3,3|BLOCKED"},
        {"tool_name": "mcp__civ6__set_city_production",
         "tool_result_full": "Error: CANNOT_START|UNIT_WARRIOR cannot start."},
    ]
    inv = CLIAgentPolicy._detect_invalid_tool_calls(steps)
    pairs = {(i["tool_name"], i["reason"]) for i in inv}
    assert ("mcp__civ6__get_map_area", "bad_arguments") in pairs
    assert ("mcp__civ6__bogus_tool", "unknown_tool") in pairs
    # exactly two — the no-tool-name step and the two game-rejected valid actions are excluded
    assert len(inv) == 2
    assert all("result_head" in i for i in inv)


def test_detect_invalid_tool_calls_empty():
    assert CLIAgentPolicy._detect_invalid_tool_calls([]) == []
    assert CLIAgentPolicy._detect_invalid_tool_calls(
        [{"tool_name": "mcp__civ6__get_units", "tool_result_full": "ok"}]) == []


_CLAUDE_INVALID_FIXTURE = "\n".join([
    json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "mcp__civ6__get_map_area", "input": {}}
    ]}}),
    json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1",
         "content": "Error executing tool get_map_area: 2 validation errors for "
                    "get_map_areaArguments\ncenter_x\n  Field required"}
    ]}}),
    json.dumps({"type": "result", "subtype": "success", "result": "done",
                "usage": {"input_tokens": 10, "output_tokens": 5}, "total_cost_usd": 0.0}),
])


def test_call_success_detects_invalid_from_error_result(monkeypatch):
    """End-to-end: a real 'Error executing tool ...' result populates invalid_tool_calls."""
    class FakeProc:
        pid = 9
        returncode = 0
        async def communicate(self):
            return (_CLAUDE_INVALID_FIXTURE.encode(), b"")
        async def wait(self):
            pass

    async def fake_create(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    pol = CLIAgentPolicy("cli-claude", FakeCost(), project_dir="/x", timeout_s=5)
    result = asyncio.run(pol(None, player_id=1, turn=1))
    inv = result["transcript"]["invalid_tool_calls"]
    assert len(inv) == 1
    assert inv[0]["tool_name"] == "mcp__civ6__get_map_area"
    assert inv[0]["reason"] == "bad_arguments"


# ---------------------------------------------------------------------------
# Raw stdout capture (env-gated) — enables codex-parser fixtures + debugging
# ---------------------------------------------------------------------------

def test_call_raw_capture_writes_when_env_set(monkeypatch, tmp_path):
    raw_dir = tmp_path / "raw"

    class FakeProc:
        pid = 1
        returncode = 0
        async def communicate(self):
            return (_CLAUDE_STREAM_FIXTURE.encode(), b"err text")
        async def wait(self):
            pass

    async def fake_create(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    monkeypatch.setenv("CIV_MCP_ARENA_RAW_DIR", str(raw_dir))
    pol = CLIAgentPolicy("cli-claude", FakeCost(), project_dir="/x", timeout_s=5)
    asyncio.run(pol(None, player_id=2, turn=7))

    out_f = raw_dir / "cli-claude-p2-t7.stdout"
    err_f = raw_dir / "cli-claude-p2-t7.stderr"
    assert out_f.exists() and "Moved scout north" in out_f.read_text()
    assert err_f.read_text() == "err text"


def test_call_raw_capture_absent_when_env_unset(monkeypatch, tmp_path):
    raw_dir = tmp_path / "raw"

    class FakeProc:
        pid = 1
        returncode = 0
        async def communicate(self):
            return (_CLAUDE_STREAM_FIXTURE.encode(), b"")
        async def wait(self):
            pass

    async def fake_create(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    monkeypatch.delenv("CIV_MCP_ARENA_RAW_DIR", raising=False)
    pol = CLIAgentPolicy("cli-claude", FakeCost(), project_dir="/x", timeout_s=5)
    asyncio.run(pol(None, player_id=2, turn=7))

    assert not raw_dir.exists()


# ---------------------------------------------------------------------------
# Step 3c — STANDING PLAN block must survive the summary clamp when memory or
# task tracking is enabled. Regression guard for the old hardcoded [:500] clamp,
# which would truncate a final message before a STANDING PLAN block appearing
# past char 500 — silently breaking Task 5's extract_standing_plan on CLI puppets.
# ---------------------------------------------------------------------------

_STANDING_PLAN_TAIL = (
    "STANDING PLAN:\n- do X\n- do Y\nTASK settle unit_id=42 target=10,12\n"
)
# Preamble long enough that the STANDING PLAN region starts well past char 500 —
# the old clamp would cut it off entirely.
_LONG_PREAMBLE = "A" * 550
_LONG_FINAL_MESSAGE = f"{_LONG_PREAMBLE}\n\n{_STANDING_PLAN_TAIL}"
assert len(_LONG_FINAL_MESSAGE) > 500
assert _LONG_FINAL_MESSAGE[:500].find("STANDING PLAN:") == -1  # confirms the old clamp cuts it


def test_call_claude_standing_plan_survives_clamp_when_memory_enabled(monkeypatch):
    class FakeProc:
        pid = 1
        returncode = 0

        async def communicate(self):
            blob = json.dumps({
                "type": "result", "subtype": "success",
                "result": _LONG_FINAL_MESSAGE,
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "total_cost_usd": 0.0,
            })
            return (blob.encode(), b"")

        async def wait(self):
            pass

    async def fake_create(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    pol = CLIAgentPolicy(
        "cli-claude", FakeCost(), project_dir="/x", timeout_s=5,
        options=CivOptions(memory=MemoryOptions(enabled=True)),
    )
    result = asyncio.run(pol(None, player_id=1, turn=1))

    assert "STANDING PLAN:" in result["summary"]
    assert "TASK settle unit_id=42 target=10,12" in result["summary"]
    assert "STANDING PLAN:" in result["transcript"]["final_summary"]
    assert result["transcript"]["prompt_injections"]["standing_plan_instruction"] is True


def test_call_claude_summary_still_clamped_when_memory_disabled(monkeypatch):
    """Control: with memory/task tracking both disabled, the legacy [:500] clamp still
    applies — this must NOT regress to unbounded transcript growth."""
    class FakeProc:
        pid = 1
        returncode = 0

        async def communicate(self):
            blob = json.dumps({
                "type": "result", "subtype": "success",
                "result": _LONG_FINAL_MESSAGE,
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "total_cost_usd": 0.0,
            })
            return (blob.encode(), b"")

        async def wait(self):
            pass

    async def fake_create(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    pol = CLIAgentPolicy("cli-claude", FakeCost(), project_dir="/x", timeout_s=5)
    result = asyncio.run(pol(None, player_id=1, turn=1))

    assert len(result["summary"]) == 500
    assert "STANDING PLAN:" not in result["summary"]
    assert result["transcript"]["prompt_injections"]["standing_plan_instruction"] is False


def test_call_claude_summary_caps_large_memory_capture_at_configured_budget(monkeypatch):
    class FakeProc:
        pid = 1
        returncode = 0

        async def communicate(self):
            blob = json.dumps({
                "type": "result",
                "subtype": "success",
                "result": "x" * 6500,
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "total_cost_usd": 0.0,
            })
            return (blob.encode(), b"")

        async def wait(self):
            pass

    async def fake_create(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    pol = CLIAgentPolicy(
        "cli-claude",
        FakeCost(),
        project_dir="/x",
        timeout_s=5,
        options=CivOptions(memory=MemoryOptions(enabled=True, max_chars=6000)),
    )
    result = asyncio.run(pol(None, player_id=1, turn=1))

    assert len(result["summary"]) == 6000
    assert len(result["transcript"]["final_summary"]) == 6000


def test_call_codex_standing_plan_survives_clamp_when_task_tracker_enabled(monkeypatch):
    class FakeProc:
        pid = 2
        returncode = 0

        async def communicate(self):
            blob = "\n".join([
                json.dumps({"type": "thread.started", "thread_id": "t1"}),
                json.dumps({"type": "item.completed", "item": {
                    "id": "item_0", "type": "agent_message", "text": _LONG_FINAL_MESSAGE}}),
                json.dumps({"type": "turn.completed",
                            "usage": {"input_tokens": 10, "output_tokens": 5}}),
            ])
            return (blob.encode(), b"")

        async def wait(self):
            pass

    async def fake_create(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    pol = CLIAgentPolicy(
        "cli-codex", FakeCost(), project_dir="/x", timeout_s=5,
        options=CivOptions(task_tracker=TaskTrackerOptions(enabled=True)),
    )
    result = asyncio.run(pol(None, player_id=1, turn=1))

    assert "STANDING PLAN:" in result["summary"]
    assert "TASK settle unit_id=42 target=10,12" in result["transcript"]["final_summary"]


# ---------------------------------------------------------------------------
# __call__ prompt construction — playbook prefix, "Do NOT end the turn" clause,
# and standing-plan tail vs one-line-summary tail
# ---------------------------------------------------------------------------

def test_call_builds_argv_with_prompt_containing_load_bearing_clause(monkeypatch):
    """The 'Do NOT end the turn' clause must survive into the spawned argv verbatim."""
    captured = {}

    class FakeProc:
        pid = 1
        returncode = 0
        async def communicate(self):
            return (b'{"type":"result","result":"ok","usage":{},"total_cost_usd":0}', b"")
        async def wait(self):
            pass

    async def fake_create(*args, **kwargs):
        captured["argv"] = args
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    pol = CLIAgentPolicy("cli-claude", FakeCost(), project_dir="/x", timeout_s=5)
    asyncio.run(pol(None, player_id=3, turn=9))

    joined = " ".join(captured["argv"])
    assert "Do NOT end the turn — the host ends it for you." in joined
    assert "player 3" in joined and "turn 9" in joined
    assert "give a one-line summary" in joined
    assert "STANDING PLAN" not in joined


def test_call_uses_standing_plan_tail_instead_of_one_line_summary_when_memory_enabled(monkeypatch):
    captured = {}

    class FakeProc:
        pid = 1
        returncode = 0
        async def communicate(self):
            return (b'{"type":"result","result":"ok","usage":{},"total_cost_usd":0}', b"")
        async def wait(self):
            pass

    async def fake_create(*args, **kwargs):
        captured["argv"] = args
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    pol = CLIAgentPolicy(
        "cli-claude", FakeCost(), project_dir="/x", timeout_s=5,
        options=CivOptions(memory=MemoryOptions(enabled=True)),
    )
    asyncio.run(pol(None, player_id=3, turn=9))

    joined = " ".join(captured["argv"])
    assert "Do NOT end the turn — the host ends it for you." in joined
    assert "STANDING PLAN:" in joined
    assert "give a one-line summary" not in joined


def test_call_prepends_condensed_playbook_when_enabled(monkeypatch):
    captured = {}

    class FakeProc:
        pid = 1
        returncode = 0
        async def communicate(self):
            return (b'{"type":"result","result":"ok","usage":{},"total_cost_usd":0}', b"")
        async def wait(self):
            pass

    async def fake_create(*args, **kwargs):
        captured["argv"] = args
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    pol = CLIAgentPolicy(
        "cli-claude", FakeCost(), project_dir="/x", timeout_s=5,
        options=CivOptions(playbook="condensed"),
    )
    asyncio.run(pol(None, player_id=3, turn=9))

    from civ_mcp.arena.agent import load_playbook
    joined = " ".join(captured["argv"])
    assert load_playbook() in joined
    # playbook text must precede the core turn instruction
    assert joined.index(load_playbook()) < joined.index("Do NOT end the turn")


def test_call_uses_prebuilt_briefing_without_building_from_gs(monkeypatch):
    from civ_mcp.arena import cli_agent as cli_mod
    from civ_mcp.arena.briefing import Briefing
    from civ_mcp.arena.config import BriefingOptions

    captured = {}

    async def forbidden_build(gs, opts, budget):
        raise AssertionError("CLI policy must not build briefing when one is supplied")

    class FakeProc:
        pid = 1
        returncode = 0

        async def communicate(self):
            return (b'{"type":"result","result":"ok","usage":{},"total_cost_usd":0}', b"")

        async def wait(self):
            pass

    async def fake_create(*args, **kwargs):
        captured["argv"] = args
        return FakeProc()

    monkeypatch.setattr(
        "civ_mcp.arena.prompt_context.build_briefing",
        forbidden_build,
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    pol = CLIAgentPolicy(
        "cli-claude",
        FakeCost(),
        project_dir="/x",
        timeout_s=5,
        options=CivOptions(briefing=BriefingOptions(enabled=True)),
    )

    result = asyncio.run(
        pol(
            None,
            player_id=3,
            turn=9,
            briefing=Briefing(text="PREBUILT BRIEFING", tokens=4, sections=["overview"]),
        )
    )

    assert result["summary"] == "ok"
    assert "PREBUILT BRIEFING" in " ".join(captured["argv"])
    assert result["transcript"]["briefing_tokens"] == 4
    assert result["transcript"]["briefing_sections"] == ["overview"]


def test_call_uses_supplied_empty_briefing_without_rebuilding(monkeypatch):
    from civ_mcp.arena import cli_agent as cli_mod
    from civ_mcp.arena.briefing import Briefing
    from civ_mcp.arena.config import BriefingOptions

    async def forbidden_build(gs, opts, budget):
        raise AssertionError("CLI policy must not rebuild a supplied briefing")

    class FakeProc:
        pid = 1
        returncode = 0

        async def communicate(self):
            return (b'{"type":"result","result":"ok","usage":{},"total_cost_usd":0}', b"")

        async def wait(self):
            pass

    async def fake_create(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(
        "civ_mcp.arena.prompt_context.build_briefing",
        forbidden_build,
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    pol = CLIAgentPolicy(
        "cli-claude",
        FakeCost(),
        project_dir="/x",
        timeout_s=5,
        options=CivOptions(briefing=BriefingOptions(enabled=True)),
    )

    result = asyncio.run(
        pol(
            None,
            player_id=3,
            turn=9,
            briefing=Briefing(
                text="",
                tokens=0,
                sections=[],
                errors=["empty prebuild"],
            ),
        )
    )

    assert result["summary"] == "ok"
    assert result["transcript"]["briefing_errors"] == ["empty prebuild"]
