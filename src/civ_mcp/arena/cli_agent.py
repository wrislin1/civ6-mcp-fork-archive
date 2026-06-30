from __future__ import annotations
import asyncio, json, os, signal

# Two-layer lockdown for CLI civ security:
# 1. --tools "" disables all host built-in tools (Bash/Write/Edit/Read)
# 2. _DENIED_CIV6_TOOLS blocks destructive MCP civ6 tools — the host ends turns and manages
#    the game lifecycle, so the CLI civ must never end_turn, kill/reload, or load saves.
_DENIED_CIV6_TOOLS = [
    "mcp__civ6__end_turn",
    "mcp__civ6__kill_game",
    "mcp__civ6__load_game_save",
    "mcp__civ6__restart_and_load",
    "mcp__civ6__load_save",
    "mcp__civ6__load_save_from_menu",
    "mcp__civ6__launch_game",
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
            argv = ["claude", "-p", prompt, "--output-format", "json",
                    "--permission-mode", "bypassPermissions",
                    "--tools", "",
                    "--allowedTools", "mcp__civ6",
                    "--disallowedTools", " ".join(_DENIED_CIV6_TOOLS),
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
        return (str(obj.get("result", ""))[:500],
                int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0)),
                float(obj.get("total_cost_usd", 0.0)))

    async def __call__(self, gs, player_id: int, turn: int) -> dict:
        argv = self._build_argv(player_id, turn)
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=self.project_dir,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_s)
        except asyncio.TimeoutError:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            await proc.wait()
            return {"summary": f"cli timeout after {self.timeout_s}s", "actions": [], "usage": {}}
        stdout = out.decode("utf-8", "replace")
        summary, pt, ct, usd = self._parse_claude(stdout)
        self.cost.record(player_id=player_id, model=(self.model or self.provider),
                         provider=self.provider, prompt_tokens=pt, completion_tokens=ct,
                         turn=turn, usd=usd)
        return {"summary": summary, "actions": [],
                "usage": {"prompt_tokens": pt, "completion_tokens": ct, "usd": usd,
                          "exit": proc.returncode, "stderr": err.decode("utf-8","replace")[-400:]}}
