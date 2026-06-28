import pytest
from civ_mcp.arena.agent import LLMPolicy
from civ_mcp.arena.backends import Reply

class FakeGS:
    def __init__(self): self.calls = []
    async def get_game_overview(self): return "OVERVIEW"
    async def get_units(self): return ["settler", "warrior"]
    async def fortify_unit(self, unit_index): self.calls.append(("fortify", unit_index)); return "FORTIFIED"

class FakeBackend:
    def __init__(self): self.n = 0
    async def chat(self, messages, tools):
        self.n += 1
        if self.n == 1:
            return Reply(text=None, tool_calls=[{"id": "1", "name": "fortify_unit",
                         "arguments": '{"unit_index": 0}'}], prompt_tokens=50, completion_tokens=5)
        return Reply(text="done", tool_calls=[], prompt_tokens=10, completion_tokens=2)

class FakeCost:
    def __init__(self): self.total = 0
    def record(self, **kw): self.total += kw["prompt_tokens"]

@pytest.mark.asyncio
async def test_policy_executes_one_tool_then_stops():
    gs, be, cost = FakeGS(), FakeBackend(), FakeCost()
    pol = LLMPolicy(be, cost, max_steps=4)
    out = await pol(gs, player_id=1, turn=2)
    assert gs.calls == [("fortify", 0)]
    assert be.n == 2           # one tool round + one final no-tool round
    assert cost.total == 60    # tokens summed across rounds
