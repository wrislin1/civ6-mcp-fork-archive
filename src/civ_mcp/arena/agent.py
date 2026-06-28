from __future__ import annotations
import json

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
          {"city_id": {"type": "integer"}, "item": {"type": "string"}}, ["city_id", "item"]),
    _tool("set_research", "Set the research tech (TECH_*)",
          {"tech": {"type": "string"}}, ["tech"]),
    _tool("fortify_unit", "Fortify a unit", {"unit_index": {"type": "integer"}}, ["unit_index"]),
    _tool("skip_unit", "Skip a unit this turn", {"unit_index": {"type": "integer"}}, ["unit_index"]),
]

# tool name -> (GameState method name, arg-mapping function)
def _dispatch(gs, name, args):
    a = json.loads(args or "{}")
    table = {
        "get_overview": lambda: gs.get_game_overview(),
        "get_units": lambda: gs.get_units(),
        "get_cities": lambda: gs.get_cities(),
        "move_unit": lambda: gs.move_unit(a["unit_index"], a["x"], a["y"]),
        "found_city": lambda: gs.found_city(a["unit_index"]),
        "set_city_production": lambda: gs.set_city_production(a["city_id"], a["item"]),
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
        for _ in range(self.max_steps):
            reply = await self.backend.chat(messages, TOOLS)
            self.cost.record(player_id=player_id, model=getattr(self.backend, "model", "?"),
                             provider="local", prompt_tokens=reply.prompt_tokens,
                             completion_tokens=reply.completion_tokens, turn=turn)
            if not reply.tool_calls:
                return {"summary": reply.text or "", "actions": actions}
            messages.append({"role": "assistant", "content": reply.text or "",
                             "tool_calls": [{"id": tc["id"], "type": "function",
                              "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                             for tc in reply.tool_calls]})
            for tc in reply.tool_calls:
                try:
                    result = await _dispatch(gs, tc["name"], tc["arguments"])
                except Exception as e:
                    result = f"ERROR: {e!r}"
                actions.append({"tool": tc["name"], "result": str(result)[:300]})
                messages.append({"role": "tool", "tool_call_id": tc["id"],
                                 "content": str(result)[:1500]})
        return {"summary": "max_steps reached", "actions": actions}
