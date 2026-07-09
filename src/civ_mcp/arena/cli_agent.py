from __future__ import annotations
import asyncio, json, os, signal, time

from civ_mcp.arena.agent import load_playbook
from civ_mcp.arena.briefing import Briefing
from civ_mcp.arena.budget import explicit_n_ctx
from civ_mcp.arena.config import CivOptions
from civ_mcp.arena.memory import find_standing_plan_start
from civ_mcp.arena.prompt_context import maybe_build_briefing
from civ_mcp.arena.prompting import build_opening_prompt


# Four-layer lockdown for CLI civ security:
# 1. --setting-sources project,local keeps project .mcp.json auto-discovery (the only headless
#    `claude -p` path observed to expose civ6 stdio tools) while excluding user-scope MCP
#    servers such as serena/Gmail/Drive/Calendar/Code Remote.
# 2. _DENIED_HOST_TOOLS blocks built-in host tools (Bash/Read/Write/Edit/etc.). Do not use
#    --tools ""; live testing showed it suppresses the civ6 stdio MCP tools too.
# 3. _DENIED_CIV6_TOOLS blocks destructive MCP civ6 tools — the host ends turns and manages
#    the game lifecycle, so the CLI civ must never end_turn, kill/reload, or load saves.
# 4. CIV_MCP_ARENA_PUPPET=1 makes the civ6 MCP SERVER remove lifecycle tools and run_lua.
#    This is the decisive layer: run_lua is an arbitrary-Lua escape hatch reaching execute_write
#    with no seat/caller gating, and Codex has no per-invocation MCP denylist. Server-enforced
#    removal is strictly stronger; the Claude denylist is belt-and-suspenders for clients that
#    still surface the tool names.
#    TWO-HOP FORWARDING (critical): Claude Code does NOT pass arbitrary parent env vars through to
#    the stdio MCP servers it spawns — it forwards only a minimal Posix subset.  Setting the var on
#    the `claude` subprocess (below) is necessary but NOT sufficient; the var only reaches the
#    civ6 grandchild because .mcp.json relays it via an `env` block. Codex gets the same server
#    env through inline `mcp_servers.civ6.env` config.
_SERVER_ENV = {
    "CIV_MCP_DISABLE_LUA": "1",
    "CIV_MCP_NO_WEB": "1",
    "CIV_MCP_ARENA_PUPPET": "1",
}

# Names must match the installed Claude Code tool registry, else each emits a harmless
# "deny rule matches no known tool" warning on stderr. Verified against Claude Code 2.1.196:
# MultiEdit / NotebookRead / LS no longer exist (Edit/Read/Glob subsume them) and were dropped.
# Security does NOT rest on this list — `--allowedTools mcp__civ6` is what gates host built-ins;
# the denylist is defense-in-depth for clients where the allowlist is advisory.
_DENIED_HOST_TOOLS = [
    "Bash",
    "BashOutput",
    "KillBash",
    "Read",
    "Write",
    "Edit",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
    "Task",
    "TodoWrite",
    "Glob",
    "Grep",
    "ExitPlanMode",
]

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

_DENIED_TOOLS = _DENIED_HOST_TOOLS + _DENIED_CIV6_TOOLS

_CODEX_MCP_ENV_CONFIG = (
    'mcp_servers.civ6.env={'
    'CIV_MCP_DISABLE_LUA="1",'
    'CIV_MCP_NO_WEB="1",'
    'CIV_MCP_ARENA_PUPPET="1"'
    '}'
)

# Core observe/act instruction. The "Do NOT end the turn" clause is load-bearing (verbatim) —
# the host ends turns; if the CLI agent ends its own turn the coordinator handoff breaks.
# The trailing wrap-up instruction is appended separately in __call__: a plain one-line-summary
# ask when memory/task tracking are both disabled, or STANDING_PLAN_INSTRUCTION (shared with the
# local policy via prompting.py) when either is enabled, so the CLI's final message carries a
# machine-parseable STANDING PLAN block just like the local puppet's.
_PROMPT = (
    "You are playing player {pid} (an AI civ) in the running Civilization VI game; it is "
    "turn {turn} and YOU are currently the active player. Use the civ6 tools to observe your "
    "situation and take a few sensible early-game actions (scout, move/settle a settler, set "
    "city production and research). Do NOT end the turn — the host ends it for you."
)
_PROMPT_SUMMARY_TAIL = " When done, give a one-line summary."
# Attention civs are told they may END the summary with SKIP:/WAKE IF: lines
# (ATTENTION_INSTRUCTION); demanding "one-line" contradicts that and suppresses
# directive emission (final-review Important 1).
_PROMPT_SUMMARY_TAIL_ATTENTION = " When done, give a short summary."

def _clamp_final_summary(text: str, max_summary_chars: int) -> str:
    # memory.find_standing_plan_start shares extraction's exact matcher, so a
    # plan this clamp preserves is always one extract_standing_plan can find.
    if len(text) <= max_summary_chars:
        return text
    plan_start = find_standing_plan_start(text)
    if plan_start >= 0:
        return text[plan_start : plan_start + max_summary_chars].strip()
    return text[:max_summary_chars]


class CLIAgentPolicy:
    needs_exclusive_tuner = True   # the CLI's civ6 MCP needs the single tuner slot

    def __init__(self, provider, cost, project_dir, model="", timeout_s=900, max_turns=40, options=None):
        self.provider, self.cost, self.project_dir = provider, cost, project_dir
        self.model, self.timeout_s, self.max_turns = model, timeout_s, max_turns
        self.options = options if options is not None else CivOptions()
        # CLI mode has no separate system message (unlike LLMPolicy's self._system), so the
        # condensed playbook text — when requested — goes at the very top of the single prompt
        # string built in __call__.
        self._system_prefix = load_playbook() if self.options.playbook == "condensed" else ""

    def _build_argv(self, prompt: str) -> list[str]:
        if self.provider == "cli-claude":
            # Let Claude auto-discover the project .mcp.json. Live headless tests showed that
            # explicit --mcp-config does not expose the civ6 stdio server's tools to `claude -p`,
            # while project auto-discovery does. Scope settings to project/local so user-scope
            # MCP servers are not inherited into the bypassPermissions subprocess.
            # --output-format stream-json with --verbose emits a stream of NDJSON events so the
            # defensive parsers can pair tool_use with tool_result and record per-step telemetry.
            argv = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
                    "--permission-mode", "bypassPermissions",
                    "--allowedTools", "mcp__civ6",
                    "--disallowedTools", " ".join(_DENIED_TOOLS),
                    "--setting-sources", "project,local",
                    "--max-turns", str(self.max_turns)]
            if self.model:
                argv += ["--model", self.model]
            return argv
        if self.provider == "cli-codex":
            argv = [
                "codex", "exec",
                "--json",
                "--ignore-user-config",
                "--skip-git-repo-check",
                "-C", self.project_dir,
                "-s", "danger-full-access",
                "-c", 'approval_policy="never"',
                "-c", 'mcp_servers.civ6.command="uv"',
                "-c", 'mcp_servers.civ6.args=["run","--directory",".","civ-mcp"]',
                "-c", _CODEX_MCP_ENV_CONFIG,
            ]
            if self.model:
                argv += ["-m", self.model]
            argv.append(prompt)
            return argv
        raise ValueError(f"unknown CLI provider {self.provider!r}")

    @staticmethod
    def _parse_claude_raw(stdout: str):
        """Extract the raw (unclamped) final text plus usage from claude output.

        The raw text feeds the transcript's final_summary — the coordinator
        parses TASK/CANCEL lines from it, so it must never be clamped here.
        """
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
            if not isinstance(obj, dict):
                return ("(unparseable CLI output)", 0, 0, 0.0)
        u = obj.get("usage", {}) or {}
        text = str(obj.get("result") or "")
        return (text,
                int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0),
                float(obj.get("total_cost_usd") or 0.0))

    @staticmethod
    def _parse_claude(
        stdout: str,
        max_summary_chars: int = 500,
        *,
        preserve_standing_plan: bool = True,
    ):
        text, pt, ct, usd = CLIAgentPolicy._parse_claude_raw(stdout)
        summary = (
            _clamp_final_summary(text, max_summary_chars)
            if preserve_standing_plan
            else text[:max_summary_chars]
        )
        return (summary, pt, ct, usd)

    @staticmethod
    def _parse_codex_raw(stdout: str):
        """Extract the raw (unclamped) final agent message plus usage from codex
        output. See _parse_claude_raw for why the text must stay unclamped."""
        text = ""
        prompt_tokens = 0
        completion_tokens = 0
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("type") == "item.completed":
                item = obj.get("item", {}) or {}
                if item.get("type") == "agent_message":
                    text = str(item.get("text") or "")
            elif obj.get("type") == "turn.completed":
                usage = obj.get("usage", {}) or {}
                prompt_tokens = int(usage.get("input_tokens") or 0)
                completion_tokens = int(usage.get("output_tokens") or 0)
        return (text, prompt_tokens, completion_tokens, 0.0)

    @staticmethod
    def _parse_codex(
        stdout: str,
        max_summary_chars: int = 500,
        *,
        preserve_standing_plan: bool = True,
    ):
        text, pt, ct, usd = CLIAgentPolicy._parse_codex_raw(stdout)
        if not text:
            return ("", pt, ct, usd)
        summary = (
            _clamp_final_summary(text, max_summary_chars)
            if preserve_standing_plan
            else text[:max_summary_chars]
        )
        return (summary, pt, ct, usd)

    @staticmethod
    def _stream_steps_claude(stdout: str) -> list:
        """Parse claude --output-format stream-json NDJSON into step dicts.

        Pairs tool_use.id <-> tool_result.tool_use_id so each tool step carries both
        the call and its result. Defensive: skips unparseable lines and never raises;
        a bug here must not crash a turn or alter summary/usage.

        NOTE: Fixtures are synthetic/provisional — Task 9 pins against real captured stdout.
        """
        steps: list[dict] = []
        pending: dict[str, dict] = {}  # tool_use id -> step dict
        idx = 0
        try:
            for line in stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                obj_type = obj.get("type")
                if obj_type == "assistant":
                    msg = obj.get("message") or {}
                    content = msg.get("content", [])
                    if not isinstance(content, list):
                        continue
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "tool_use":
                            step: dict = {
                                "idx": idx,
                                "role": "assistant",
                                "text": "",
                                "tool_name": block.get("name"),
                                "tool_args": block.get("input"),
                                "tool_result_full": None,
                                "ts": 0.0,
                            }
                            uid = block.get("id") or ""
                            if uid:
                                pending[uid] = step
                            steps.append(step)
                            idx += 1
                        elif btype == "text":
                            steps.append({
                                "idx": idx,
                                "role": "assistant",
                                "text": str(block.get("text") or ""),
                                "tool_name": None,
                                "tool_args": None,
                                "tool_result_full": None,
                                "ts": 0.0,
                            })
                            idx += 1
                elif obj_type == "user":
                    msg = obj.get("message") or {}
                    content = msg.get("content", [])
                    if not isinstance(content, list):
                        continue
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "tool_result":
                            uid = block.get("tool_use_id") or ""
                            rc = block.get("content", "")
                            if isinstance(rc, list):
                                rc = json.dumps(rc)
                            else:
                                rc = str(rc) if rc is not None else ""
                            if uid and uid in pending:
                                # Pair the result back into the tool_use step
                                pending[uid]["tool_result_full"] = rc
                            else:
                                # Orphan result — record as its own step
                                steps.append({
                                    "idx": idx,
                                    "role": "user",
                                    "text": rc,
                                    "tool_name": None,
                                    "tool_args": None,
                                    "tool_result_full": rc,
                                    "ts": 0.0,
                                })
                                idx += 1
        except Exception:
            # Belt-and-suspenders: if any outer logic raises, return what we have so far
            pass
        return steps

    @staticmethod
    def _codex_result_text(result) -> str:
        """Extract the textual tool result from a codex mcp_tool_call.result.

        Real shape (codex-cli 0.142.x): ``{"content": [{"type":"text","text": ...}],
        "structured_content": {"result": ...}}``. Falls back to structured_content.result,
        then a JSON dump, so a schema tweak degrades gracefully instead of dropping data.
        """
        if result is None:
            return ""
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            parts = [str(c.get("text", "")) for c in (result.get("content") or [])
                     if isinstance(c, dict) and c.get("type") == "text"]
            if parts:
                return "\n".join(parts)
            sc = result.get("structured_content")
            if isinstance(sc, dict) and "result" in sc:
                return str(sc["result"])
            return json.dumps(result)
        return str(result)

    @staticmethod
    def _stream_steps_codex(stdout: str) -> list:
        """Parse codex `exec --json` NDJSON into step dicts.

        Pinned against REAL captured codex-cli 0.142.x stdout (live Task 9 shake-out).
        item.completed item shapes that matter:
          - {"type":"mcp_tool_call","tool":<name>,"server":"civ6","arguments":{...},
             "result":{"content":[{"type":"text","text":...}],...},"status":...,"error":...}
          - {"type":"agent_message","text":...}
        The legacy guess (item.name / item.output) never matched, so every codex step was
        tool=None — that is the bug this fix closes. Other item types (reasoning,
        command_execution, …) carry no civ6 decision signal and are skipped.
        Defensive: skips unparseable/unknown lines, never raises.
        """
        steps: list[dict] = []
        idx = 0
        try:
            for line in stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if not isinstance(obj, dict) or obj.get("type") != "item.completed":
                    continue
                item = obj.get("item") or {}
                if not isinstance(item, dict):
                    continue
                ity = item.get("type")
                if ity == "mcp_tool_call":
                    text = CLIAgentPolicy._codex_result_text(item.get("result"))
                    err = item.get("error")
                    status = item.get("status")
                    # Surface failures with the same "Error executing tool" framing the MCP
                    # server uses, so _detect_invalid_tool_calls catches codex errors too.
                    if (status not in (None, "completed", "success") or err) and \
                            not text.startswith("Error executing tool"):
                        text = f"Error executing tool {item.get('tool')}: {err or status}"
                    args = item.get("arguments")
                    steps.append({
                        "idx": idx,
                        "role": "tool",
                        "text": "",
                        "tool_name": item.get("tool"),
                        "tool_args": args if isinstance(args, dict) else {},
                        "tool_result_full": text,
                        "ts": 0.0,
                    })
                    idx += 1
                elif ity == "agent_message":
                    steps.append({
                        "idx": idx,
                        "role": "assistant",
                        "text": str(item.get("text") or ""),
                        "tool_name": None,
                        "tool_args": None,
                        "tool_result_full": None,
                        "ts": 0.0,
                    })
                    idx += 1
        except Exception:
            pass
        return steps

    @staticmethod
    def _kill_group(proc) -> None:
        """SIGKILL the whole process group so the civ6-MCP grandchild (holding tuner port
        4318) dies too, not just `claude`.  Falls back to killing the direct child."""
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()

    @staticmethod
    def _detect_invalid_tool_calls(steps: list) -> list:
        """Detect malformed / hallucinated CLI tool calls from parsed steps.

        The civ6 MCP server frames a rejected call as ``Error executing tool <name>: ...``
        (e.g. pydantic validation errors for bad/missing args, or an unknown tool). That
        framing is distinct from a VALID call the game rejected on rules grounds
        (``Error: CANNOT_START|...``, ``...|BLOCKED``), which the analyze rubric scores —
        NOT here. Both CLI providers share the same civ6 MCP, so this generalizes across
        claude and codex once their steps carry tool_name + tool_result_full.
        """
        invalid: list[dict] = []
        for s in steps:
            if not isinstance(s, dict) or not s.get("tool_name"):
                continue
            res = str(s.get("tool_result_full") or "")
            if not res.startswith("Error executing tool"):
                continue
            low = res.lower()
            if "validation error" in low:
                reason = "bad_arguments"
            elif "not found" in low or "unknown tool" in low or "no such tool" in low:
                reason = "unknown_tool"
            else:
                reason = "tool_error"
            invalid.append({"tool_name": s.get("tool_name"), "reason": reason,
                            "result_head": res[:160]})
        return invalid

    def _dump_raw(self, raw_dir: str, player_id: int, turn: int, stdout: str, stderr: str) -> None:
        """Best-effort persist of raw CLI stdout/stderr for fixture-pinning + debugging.

        Env-gated via CIV_MCP_ARENA_RAW_DIR (off by default — codex stdout is large).
        Never raises into the turn; a capture failure must not affect the run.
        """
        try:
            os.makedirs(raw_dir, exist_ok=True)
            base = os.path.join(raw_dir, f"{self.provider}-p{player_id}-t{turn}")
            with open(base + ".stdout", "w") as fh:
                fh.write(stdout)
            with open(base + ".stderr", "w") as fh:
                fh.write(stderr)
        except Exception:
            pass

    async def __call__(
        self,
        gs,
        player_id: int,
        turn: int,
        *,
        memory_block: str = "",
        task_block: str = "",
        digest_block: str = "",
        briefing: Briefing | None = None,
    ) -> dict:
        include_standing_plan_instruction = self.options.standing_plan_enabled
        include_attention_instruction = self.options.attention_directives_enabled
        playbook_chars = len(self._system_prefix)
        briefing = await maybe_build_briefing(
            gs,
            self.options,
            n_ctx=explicit_n_ctx(self.options.context_budget),
            playbook_chars=playbook_chars,
            tool_schema_chars=0,
            supplied=briefing,
        )
        opening = build_opening_prompt(
            player_id=player_id,
            turn=turn,
            briefing_text=briefing.text,
            memory_block=memory_block,
            task_block=task_block,
            digest_block=digest_block,
            include_standing_plan_instruction=include_standing_plan_instruction,
            include_attention_instruction=include_attention_instruction,
        )
        if not include_standing_plan_instruction:
            opening += (
                _PROMPT_SUMMARY_TAIL_ATTENTION
                if include_attention_instruction
                else _PROMPT_SUMMARY_TAIL
            )
        core = _PROMPT.format(pid=player_id, turn=turn)
        prompt = f"{core}\n\n{opening}"
        if self._system_prefix:
            prompt = f"{self._system_prefix}\n\n{prompt}"
        prompt_injections = {
            "memory": bool(memory_block),
            "task_tracker": bool(task_block),
            "standing_plan_instruction": include_standing_plan_instruction,
            "digest": bool(digest_block),
            "attention_instruction": include_attention_instruction,
        }
        # STANDING PLAN blocks (1-3 bullets + optional TASK lines) can exceed the plain
        # one-line-summary clamp; widen it whenever memory/task tracking are in play so the
        # block survives truncation for later extraction (extract_standing_plan).
        max_summary_chars = self.options.standing_plan_summary_chars
        argv = self._build_argv(prompt)
        # Layer-4 lockdown: disable run_lua/lifecycle tools server-side. Claude relays this
        # through .mcp.json; Codex receives the same values through inline mcp_servers.civ6.env.
        # The parent env is still set so direct project auto-discovery paths inherit it when they can.
        env = {**os.environ, **_SERVER_ENV}
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=self.project_dir, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True)
        t0 = time.monotonic()
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_s)
        except asyncio.TimeoutError:
            wall_s = time.monotonic() - t0
            self._kill_group(proc)
            await proc.wait()
            # Record the timed-out turn (zero usable work) so it is not silently missing
            # from the cost log.
            self.cost.record(player_id=player_id, model=(self.model or self.provider),
                             provider=self.provider, prompt_tokens=0, completion_tokens=0,
                             turn=turn, usd=0.0)
            timeout_summary = f"cli timeout after {self.timeout_s}s"
            result: dict = {"summary": timeout_summary, "actions": [], "usage": {}}
            result["transcript"] = {
                "steps": [],
                "reason": "timeout",
                "wall_clock_s": wall_s,
                "final_summary": timeout_summary,
                "cli_exit": None,
                "cli_stderr_tail": "",
                "invalid_tool_calls": [],  # timeout: no parsed steps to inspect
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "prompt_injections": prompt_injections,
                "briefing_tokens": briefing.tokens,
                "briefing_sections": briefing.sections,
                "briefing_radius": briefing.radius,
                "briefing_errors": briefing.errors,
            }
            return result
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
        wall_s = time.monotonic() - t0
        stdout = out.decode("utf-8", "replace")
        stderr_full = err.decode("utf-8", "replace")
        stderr_tail = stderr_full[-400:]
        raw_dir = os.environ.get("CIV_MCP_ARENA_RAW_DIR")
        if raw_dir:
            self._dump_raw(raw_dir, player_id, turn, stdout, stderr_full)
        if self.provider == "cli-codex":
            raw_summary, pt, ct, usd = self._parse_codex_raw(stdout)
            try:
                steps = self._stream_steps_codex(stdout)
            except Exception:
                steps = []
        else:
            raw_summary, pt, ct, usd = self._parse_claude_raw(stdout)
            try:
                steps = self._stream_steps_claude(stdout)
            except Exception:
                steps = []
        # The compact summary clamps (and jumps to the STANDING PLAN header when
        # over budget); the transcript's final_summary stays raw because the
        # coordinator parses TASK/CANCEL lines and the standing plan from it.
        summary = (
            _clamp_final_summary(raw_summary, max_summary_chars)
            if include_standing_plan_instruction
            else raw_summary[:max_summary_chars]
        )
        self.cost.record(player_id=player_id, model=(self.model or self.provider),
                         provider=self.provider, prompt_tokens=pt, completion_tokens=ct,
                         turn=turn, usd=usd)
        result = {"summary": summary, "actions": [],
                  "usage": {"prompt_tokens": pt, "completion_tokens": ct, "usd": usd,
                            "exit": proc.returncode, "stderr": stderr_tail}}
        result["transcript"] = {
            "steps": steps,
            "wall_clock_s": wall_s,
            "final_summary": raw_summary,
            "cli_exit": proc.returncode,
            "cli_stderr_tail": stderr_tail,
            "invalid_tool_calls": self._detect_invalid_tool_calls(steps),
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "prompt_injections": prompt_injections,
            "briefing_tokens": briefing.tokens,
            "briefing_sections": briefing.sections,
            "briefing_radius": briefing.radius,
            "briefing_errors": briefing.errors,
        }
        return result
