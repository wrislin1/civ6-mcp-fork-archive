from __future__ import annotations
import asyncio, json, os, signal

# Four-layer lockdown for CLI civ security:
# 1. --tools "" disables all host built-in tools (Bash/Write/Edit/Read)
# 2. _DENIED_CIV6_TOOLS blocks destructive MCP civ6 tools — the host ends turns and manages
#    the game lifecycle, so the CLI civ must never end_turn, kill/reload, or load saves.
# 3. --mcp-config .mcp.json --strict-mcp-config scopes the subprocess to ONLY the civ6 MCP
#    server defined in the project's .mcp.json.  Without this, the CLI civ inherits all
#    user-scope MCP servers (serena, Gmail, Google Drive, Google Calendar, Claude Code Remote,
#    Empower, …) and under bypassPermissions every one of those tools is auto-approved —
#    allowing arbitrary host-file mutation (serena/write_memory/replace_content) or persistent
#    scheduled agents (Claude Code Remote/create_trigger).  --strict-mcp-config disables
#    auto-discovery and inherited user-scope servers entirely; only the civ6 server loads.
# 4. CIV_MCP_DISABLE_LUA=1 makes the civ6 MCP SERVER remove its run_lua tool (server.py honours
#    this by remove_tool("run_lua")).  This is the decisive layer: run_lua is an arbitrary-Lua
#    escape hatch reaching execute_write with no seat/caller gating, so a client-side denylist
#    alone cannot contain it — run_lua(code="UI.RequestAction(...ACTION_ENDTURN)") would end the
#    turn / kill / load despite layers 1-3.  Server-enforced removal is strictly stronger; see
#    evals/civbench.py for the same policy.  The denylist entry below is belt-and-suspenders for
#    clients that still surface the tool name.
#    TWO-HOP FORWARDING (critical): Claude Code does NOT pass arbitrary parent env vars through to
#    the stdio MCP servers it spawns — it forwards only a minimal Posix subset.  Setting the var on
#    the `claude` subprocess (below) is necessary but NOT sufficient; the var only reaches the
#    civ6 grandchild because .mcp.json relays it via an `env` block ("${CIV_MCP_DISABLE_LUA:-}").
#    Both halves are load-bearing — drop either and layer-4 silently no-ops back to the denylist.
_DENIED_CIV6_TOOLS = [
    "mcp__civ6__end_turn",
    "mcp__civ6__kill_game",
    "mcp__civ6__load_game_save",
    "mcp__civ6__restart_and_load",
    "mcp__civ6__load_save",
    "mcp__civ6__load_save_from_menu",
    "mcp__civ6__launch_game",
    "mcp__civ6__run_lua",
]

_PROMPT = (
    "You are playing player {pid} (an AI civ) in the running Civilization VI game; it is "
    "turn {turn} and YOU are currently the active player. Use the civ6 tools to observe your "
    "situation and take a few sensible early-game actions (scout, move/settle a settler, set "
    "city production and research). Do NOT end the turn — the host ends it for you. When done, "
    "give a one-line summary."
)

class CLIAgentPolicy:
    needs_exclusive_tuner = True   # the CLI's civ6 MCP needs the single tuner slot

    def __init__(self, provider, cost, project_dir, model="", timeout_s=900, max_turns=40):
        self.provider, self.cost, self.project_dir = provider, cost, project_dir
        self.model, self.timeout_s, self.max_turns = model, timeout_s, max_turns

    def _build_argv(self, player_id: int, turn: int) -> list[str]:
        prompt = _PROMPT.format(pid=player_id, turn=turn)
        if self.provider == "cli-claude":
            # Anchor .mcp.json to project_dir (absolute) so layer-3 scoping cannot silently
            # become a no-op if the subprocess CWD is not the repo root — with
            # --strict-mcp-config a missing config loads ZERO servers (civ6 included).
            mcp_config = os.path.join(self.project_dir, ".mcp.json")
            argv = ["claude", "-p", prompt, "--output-format", "json",
                    "--permission-mode", "bypassPermissions",
                    "--tools", "",
                    "--allowedTools", "mcp__civ6",
                    "--disallowedTools", " ".join(_DENIED_CIV6_TOOLS),
                    "--mcp-config", mcp_config, "--strict-mcp-config",
                    "--max-turns", str(self.max_turns)]
            if self.model:
                argv += ["--model", self.model]
            return argv
        raise ValueError(f"unknown CLI provider {self.provider!r}")

    @staticmethod
    def _parse_claude(stdout: str):
        obj = None
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                cand = json.loads(line)
            except ValueError:
                continue
            if isinstance(cand, dict) and cand.get("type") == "result":
                obj = cand
        if obj is None:                       # --output-format json may emit one object
            try:
                obj = json.loads(stdout)
            except ValueError:
                return ("(unparseable CLI output)", 0, 0, 0.0)
        u = obj.get("usage", {}) or {}
        return (str(obj.get("result") or "")[:500],
                int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0),
                float(obj.get("total_cost_usd") or 0.0))

    @staticmethod
    def _kill_group(proc) -> None:
        """SIGKILL the whole process group so the civ6-MCP grandchild (holding tuner port
        4318) dies too, not just `claude`.  Falls back to killing the direct child."""
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()

    async def __call__(self, gs, player_id: int, turn: int) -> dict:
        argv = self._build_argv(player_id, turn)
        # Layer-4 lockdown: disable the run_lua escape hatch server-side. This sets the var on the
        # `claude` process; .mcp.json's civ6 `env` block relays it on to the grandchild server (see
        # the TWO-HOP FORWARDING note above — claude does not auto-propagate it).
        env = {**os.environ, "CIV_MCP_DISABLE_LUA": "1"}
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=self.project_dir, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_s)
        except asyncio.TimeoutError:
            self._kill_group(proc)
            await proc.wait()
            # Record the timed-out turn (zero usable work) so it is not silently missing
            # from the cost log.
            self.cost.record(player_id=player_id, model=(self.model or self.provider),
                             provider=self.provider, prompt_tokens=0, completion_tokens=0,
                             turn=turn, usd=0.0)
            return {"summary": f"cli timeout after {self.timeout_s}s", "actions": [], "usage": {}}
        except BaseException:
            # Real cancellation (Ctrl-C raises CancelledError, a BaseException the TimeoutError
            # branch misses) — start_new_session detached the group from the parent's SIGINT,
            # so without this the civ6-MCP child is orphaned and keeps port 4318, blocking the
            # coordinator's reclaim and stranding the human.  Kill the group, then re-raise.
            self._kill_group(proc)
            try:
                await proc.wait()
            except BaseException:
                pass
            raise
        stdout = out.decode("utf-8", "replace")
        summary, pt, ct, usd = self._parse_claude(stdout)
        self.cost.record(player_id=player_id, model=(self.model or self.provider),
                         provider=self.provider, prompt_tokens=pt, completion_tokens=ct,
                         turn=turn, usd=usd)
        return {"summary": summary, "actions": [],
                "usage": {"prompt_tokens": pt, "completion_tokens": ct, "usd": usd,
                          "exit": proc.returncode, "stderr": err.decode("utf-8","replace")[-400:]}}
