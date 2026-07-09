from __future__ import annotations
import json
import time
from functools import lru_cache
from pathlib import Path

from civ_mcp.arena.briefing import Briefing
from civ_mcp.arena.budget import DEFAULT_N_CTX, resolve_n_ctx
from civ_mcp.arena.config import CivOptions
from civ_mcp.arena.prompt_context import maybe_build_briefing
from civ_mcp.arena.prompting import build_opening_prompt
from civ_mcp.arena.registry import (
    TOOL_REGISTRY,
    dispatch as _registry_dispatch,
    filter_tools,
    openai_tools,
    resolve_tools,
)


MODEL_FEED_CHAR_CAP = 1500  # max chars of a tool result fed to the model

_MINIMAL_NAMES = resolve_tools("minimal")
TOOLS = openai_tools(_MINIMAL_NAMES)


async def _dispatch(gs, name, args, allowed=_MINIMAL_NAMES):
    a = json.loads(args or "{}")
    return await _registry_dispatch(gs, name, a, allowed=allowed)


_UNAVAILABLE_REASON = {
    "gated": "not available this turn — it is era/state-gated and unlocks when you meet "
             "its requirement (research/civic/unit). Do not retry it now.",
    "out_of_tier": "not part of your toolset for this run.",
    "unknown_tool": "not a real tool.",
}


def _unavailable_result(name: str, reason: str) -> str:
    return f"UNAVAILABLE: {name} is {_UNAVAILABLE_REASON.get(reason, 'not available.')}"


@lru_cache(maxsize=1)
def load_playbook() -> str:
    return (Path(__file__).parent / "playbook.md").read_text()

SYSTEM = ("You are playing one civ in Civilization VI on its turn. Use tools to observe, then "
          "take a few sensible early-game actions (scout, move/settle, set production and "
          "research). When you are finished for this turn, reply with a short summary and NO "
          "tool calls. Keep it brief.")


# Cap how many times we re-probe /props when it keeps returning the default.
# A cold llama-swap backend needs a couple of turns to warm up and expose a real
# n_ctx; a backend with no /props at all never will, so stop after a few tries
# instead of paying an HTTP round-trip every turn for the whole game.
_N_CTX_MAX_RESOLVES = 3


def _should_resolve_n_ctx(
    current: int | None, source: str, context_budget: int | str, resolves: int
) -> bool:
    if current is None:
        return True
    return (
        context_budget == "auto"
        and source == "default"
        and resolves < _N_CTX_MAX_RESOLVES
    )


class LLMPolicy:
    def __init__(self, backend, cost, max_steps: int = 6, options: CivOptions | None = None):
        self.backend, self.cost = backend, cost
        self.options = options or CivOptions(max_steps=max_steps)
        self.max_steps = self.options.max_steps
        self._tool_names = resolve_tools(self.options.tools)
        self._tools = openai_tools(self._tool_names)
        self._char_cap = self.options.result_char_cap
        self._system = SYSTEM
        if self.options.playbook == "condensed":
            self._system = SYSTEM + "\n\n" + load_playbook()
        self._n_ctx: int | None = None
        self._n_ctx_source = ""
        self._n_ctx_resolves = 0

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
        caps: dict | None = None,
    ) -> dict:
        # One visible list feeds schema, invalid-call classification, AND the
        # dispatch allowlist (spec §1): filtering only the schema would leave
        # gated tools silently callable. caps=None fails open to the full tier.
        visible_names = filter_tools(self._tool_names, caps)
        tools_schema = openai_tools(visible_names)
        briefing_was_supplied = briefing is not None
        if (
            self.options.briefing.enabled
            and not briefing_was_supplied
            and _should_resolve_n_ctx(
                self._n_ctx,
                self._n_ctx_source,
                self.options.context_budget,
                self._n_ctx_resolves,
            )
        ):
            self._n_ctx, self._n_ctx_source = await resolve_n_ctx(
                getattr(self.backend, "base_url", ""),
                getattr(self.backend, "model", ""),
                self.options.context_budget,
            )
            self._n_ctx_resolves += 1
        playbook_chars = 0
        tool_schema_chars = 0
        if self.options.briefing.enabled and not briefing_was_supplied:
            playbook_chars = len(self._system) - len(SYSTEM)
            tool_schema_chars = len(json.dumps(tools_schema))
        n_ctx = self._n_ctx if self._n_ctx is not None else DEFAULT_N_CTX
        briefing = await maybe_build_briefing(
            gs,
            self.options,
            n_ctx=n_ctx,
            playbook_chars=playbook_chars,
            tool_schema_chars=tool_schema_chars,
            supplied=briefing,
        )
        include_standing_plan_instruction = self.options.standing_plan_enabled
        include_attention_instruction = self.options.attention_directives_enabled
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
        prompt_injections = {
            "memory": bool(memory_block),
            "task_tracker": bool(task_block),
            "standing_plan_instruction": include_standing_plan_instruction,
            "digest": bool(digest_block),
            "attention_instruction": include_attention_instruction,
        }
        messages = [{"role": "system", "content": self._system},
                    {"role": "user", "content": opening}]
        actions = []
        steps: list[dict] = []
        invalid_tool_calls: list[dict] = []
        last_assistant_text = ""
        total_prompt_tokens = 0
        total_completion_tokens = 0
        wall_clock_start = time.time()
        for _ in range(self.max_steps):
            ts_start = time.time()
            reply = await self.backend.chat(messages, tools_schema)
            self.cost.record(player_id=player_id, model=getattr(self.backend, "model", "?"),
                             provider="local", prompt_tokens=reply.prompt_tokens,
                             completion_tokens=reply.completion_tokens, turn=turn)
            total_prompt_tokens += reply.prompt_tokens
            total_completion_tokens += reply.completion_tokens
            if not reply.tool_calls:
                return {"summary": reply.text or "", "actions": actions, "transcript": {
                    "steps": steps,
                    "invalid_tool_calls": invalid_tool_calls,
                    "civ_options": self.options.fingerprint(),
                    "briefing_tokens": briefing.tokens,
                    "briefing_sections": briefing.sections,
                    "briefing_radius": briefing.radius,
                    "briefing_errors": briefing.errors,
                    "n_ctx": self._n_ctx,
                    "n_ctx_source": self._n_ctx_source,
                    "wall_clock_s": time.time() - wall_clock_start,
                    "max_steps_reached": False,
                    "final_summary": reply.text or "",
                    "prompt_tokens": total_prompt_tokens,
                    "completion_tokens": total_completion_tokens,
                    "prompt_injections": prompt_injections,
                }}
            if reply.text:
                # Keep the newest assistant prose: on step exhaustion it is the
                # only place a STANDING PLAN / TASK block can survive to the
                # coordinator's capture pass.
                last_assistant_text = reply.text
            messages.append({"role": "assistant", "content": reply.text or "",
                             "tool_calls": [{"id": tc["id"], "type": "function",
                              "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                             for tc in reply.tool_calls]})
            for tc in reply.tool_calls:
                if tc["name"] not in visible_names:
                    if tc["name"] in self._tool_names:
                        reason = "gated"
                    elif tc["name"] in TOOL_REGISTRY:
                        reason = "out_of_tier"
                    else:
                        reason = "unknown_tool"
                    invalid_tool_calls.append({"tool_name": tc["name"],
                                               "arguments": tc["arguments"],
                                               "reason": reason})
                    result = _unavailable_result(tc["name"], reason)
                else:
                    try:
                        json.loads(tc["arguments"] or "{}")
                    except (json.JSONDecodeError, ValueError):
                        invalid_tool_calls.append({"tool_name": tc["name"], "arguments": tc["arguments"],
                                                   "reason": "bad_arguments"})
                    try:
                        result = await _dispatch(gs, tc["name"], tc["arguments"], visible_names)
                    except Exception as e:
                        result = f"ERROR: {e!r}"
                # transcript step (uses same result object, before truncation)
                _s = str(result)
                _l = len(_s)
                ts_end = time.time()
                try:
                    _tool_args = json.loads(tc["arguments"] or "{}")
                    if not isinstance(_tool_args, dict):
                        _tool_args = {}
                except (json.JSONDecodeError, ValueError):
                    _tool_args = {}
                steps.append({
                    "idx": len(steps),
                    "role": "tool",
                    "ts_start": ts_start,
                    "ts_end": ts_end,
                    "tool_name": tc["name"],
                    "tool_args": _tool_args,
                    "tool_result_full": _s,
                    "result_total_chars": _l,
                    "result_chars_fed_to_model": min(_l, self._char_cap),
                    "truncated": _l > self._char_cap,
                    "prompt_tokens": reply.prompt_tokens,
                    "completion_tokens": reply.completion_tokens,
                })
                actions.append({"tool": tc["name"], "result": str(result)[:300]})
                messages.append({"role": "tool", "tool_call_id": tc["id"],
                                 "content": str(result)[:self._char_cap]})
        return {"summary": "max_steps reached", "actions": actions, "transcript": {
            "steps": steps,
            "invalid_tool_calls": invalid_tool_calls,
            "civ_options": self.options.fingerprint(),
            "briefing_tokens": briefing.tokens,
            "briefing_sections": briefing.sections,
            "briefing_radius": briefing.radius,
            "briefing_errors": briefing.errors,
            "n_ctx": self._n_ctx,
            "n_ctx_source": self._n_ctx_source,
            "wall_clock_s": time.time() - wall_clock_start,
            "max_steps_reached": True,
            "final_summary": last_assistant_text,
            "prompt_tokens": total_prompt_tokens,
            "completion_tokens": total_completion_tokens,
            "prompt_injections": prompt_injections,
        }}
