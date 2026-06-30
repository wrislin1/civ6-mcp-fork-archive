from __future__ import annotations
import json
import time

def _tool(name, desc, props=None, required=None):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props or {}, "required": required or []}}}

TOOLS = [
    _tool("get_overview", "Empire/turn overview for your civ"),
    _tool("get_units", "List your units (with their unit_index)"),
    _tool("get_cities", "List your cities"),
    _tool("move_unit", "Move a unit toward (x,y)",
          {"unit_index": {"type": "integer"}, "x": {"type": "integer"}, "y": {"type": "integer"}},
          ["unit_index", "x", "y"]),
    _tool("found_city", "Found a city with a settler",
          {"unit_index": {"type": "integer"}}, ["unit_index"]),
    _tool("set_city_production", "Set a city's production",
          {"city_id": {"type": "integer"},
           "item_type": {"type": "string", "description": "UNIT | BUILDING | DISTRICT | PROJECT"},
           "item_name": {"type": "string", "description": "e.g. UNIT_WARRIOR, BUILDING_MONUMENT"}},
          ["city_id", "item_type", "item_name"]),
    _tool("set_research", "Set the research tech (TECH_*)",
          {"tech": {"type": "string"}}, ["tech"]),
    _tool("fortify_unit", "Fortify a unit", {"unit_index": {"type": "integer"}}, ["unit_index"]),
    _tool("skip_unit", "Skip a unit this turn", {"unit_index": {"type": "integer"}}, ["unit_index"]),
]

# known tool names for invalid-call classification
_KNOWN_TOOLS = frozenset({
    "get_overview", "get_units", "get_cities", "move_unit", "found_city",
    "set_city_production", "set_research", "fortify_unit", "skip_unit",
})

# tool name -> (GameState method name, arg-mapping function)
def _dispatch(gs, name, args):
    a = json.loads(args or "{}")
    table = {
        "get_overview": lambda: gs.get_game_overview(),
        "get_units": lambda: gs.get_units(),
        "get_cities": lambda: gs.get_cities(),
        "move_unit": lambda: gs.move_unit(a["unit_index"], a["x"], a["y"]),
        "found_city": lambda: gs.found_city(a["unit_index"]),
        "set_city_production": lambda: gs.set_city_production(a["city_id"], a["item_type"], a["item_name"]),
        "set_research": lambda: gs.set_research(a["tech"]),
        "fortify_unit": lambda: gs.fortify_unit(a["unit_index"]),
        "skip_unit": lambda: gs.skip_unit(a["unit_index"]),
    }
    return table[name]()

SYSTEM = ("You are playing one civ in Civilization VI on its turn. Use tools to observe, then "
          "take a few sensible early-game actions (scout, move/settle, set production and "
          "research). When you are finished for this turn, reply with a short summary and NO "
          "tool calls. Keep it brief.")

class LLMPolicy:
    def __init__(self, backend, cost, max_steps: int = 6):
        self.backend, self.cost, self.max_steps = backend, cost, max_steps

    async def __call__(self, gs, player_id: int, turn: int) -> dict:
        messages = [{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": f"It is turn {turn}. You control player {player_id}. Begin."}]
        actions = []
        steps: list[dict] = []
        invalid_tool_calls: list[dict] = []
        total_prompt_tokens = 0
        total_completion_tokens = 0
        wall_clock_start = time.time()
        for _ in range(self.max_steps):
            ts_start = time.time()
            reply = await self.backend.chat(messages, TOOLS)
            self.cost.record(player_id=player_id, model=getattr(self.backend, "model", "?"),
                             provider="local", prompt_tokens=reply.prompt_tokens,
                             completion_tokens=reply.completion_tokens, turn=turn)
            total_prompt_tokens += reply.prompt_tokens
            total_completion_tokens += reply.completion_tokens
            if not reply.tool_calls:
                return {"summary": reply.text or "", "actions": actions, "transcript": {
                    "steps": steps,
                    "invalid_tool_calls": invalid_tool_calls,
                    "wall_clock_s": time.time() - wall_clock_start,
                    "max_steps_reached": False,
                    "final_summary": reply.text or "",
                    "prompt_tokens": total_prompt_tokens,
                    "completion_tokens": total_completion_tokens,
                }}
            messages.append({"role": "assistant", "content": reply.text or "",
                             "tool_calls": [{"id": tc["id"], "type": "function",
                              "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                             for tc in reply.tool_calls]})
            for tc in reply.tool_calls:
                # classify (observation only — dispatch below stays untouched)
                if tc["name"] not in _KNOWN_TOOLS:
                    invalid_tool_calls.append({"tool_name": tc["name"], "arguments": tc["arguments"],
                                               "reason": "unknown_tool"})
                else:
                    try:
                        json.loads(tc["arguments"] or "{}")
                    except (json.JSONDecodeError, ValueError):
                        invalid_tool_calls.append({"tool_name": tc["name"], "arguments": tc["arguments"],
                                                   "reason": "bad_arguments"})
                try:
                    result = await _dispatch(gs, tc["name"], tc["arguments"])
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
                    "result_chars_fed_to_model": min(_l, 1500),
                    "truncated": _l > 1500,
                    "prompt_tokens": reply.prompt_tokens,
                    "completion_tokens": reply.completion_tokens,
                })
                actions.append({"tool": tc["name"], "result": str(result)[:300]})
                messages.append({"role": "tool", "tool_call_id": tc["id"],
                                 "content": str(result)[:1500]})
        return {"summary": "max_steps reached", "actions": actions, "transcript": {
            "steps": steps,
            "invalid_tool_calls": invalid_tool_calls,
            "wall_clock_s": time.time() - wall_clock_start,
            "max_steps_reached": True,
            "final_summary": "",
            "prompt_tokens": total_prompt_tokens,
            "completion_tokens": total_completion_tokens,
        }}
