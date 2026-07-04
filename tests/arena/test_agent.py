import pytest
import civ_mcp.arena.agent as agent
from civ_mcp.arena.agent import LLMPolicy
from civ_mcp.arena.backends import Reply
from civ_mcp.arena.config import CivOptions
from civ_mcp.arena.agent import load_playbook


# ---------------------------------------------------------------------------
# Task-H1 — MODEL_FEED_CHAR_CAP constant
# ---------------------------------------------------------------------------

def test_model_feed_char_cap_constant():
    """MODEL_FEED_CHAR_CAP must equal 1500 and be exported from agent module."""
    assert agent.MODEL_FEED_CHAR_CAP == 1500

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


# ---------------------------------------------------------------------------
# Task-2 tests: transcript payload
# ---------------------------------------------------------------------------

class FakeBackendTranscript:
    """First reply: one known + one unknown tool call.  Second reply: text."""
    def __init__(self):
        self.n = 0
        self.last_messages = None

    async def chat(self, messages, tools):
        self.n += 1
        self.last_messages = messages
        if self.n == 1:
            return Reply(
                text=None,
                tool_calls=[
                    {"id": "tc1", "name": "fortify_unit", "arguments": '{"unit_index": 0}'},
                    {"id": "tc2", "name": "bogus_tool",   "arguments": '{}'},
                ],
                prompt_tokens=50,
                completion_tokens=5,
            )
        return Reply(text="all done", tool_calls=[], prompt_tokens=10, completion_tokens=2)


@pytest.mark.asyncio
async def test_transcript_payload():
    gs, be, cost = FakeGS(), FakeBackendTranscript(), FakeCost()
    pol = LLMPolicy(be, cost, max_steps=4)
    out = await pol(gs, player_id=1, turn=3)

    # --- behavior-neutral: existing action/message lines unchanged ---
    # actions list truncated to [:300] as before
    assert out["actions"][0]["result"] == str("FORTIFIED")[:300]
    # dispatch for unknown tool still returns ERROR string
    assert out["actions"][1]["result"].startswith("ERROR:")

    # tool messages sent to model were [:1500] (verified via messages captured
    # in the SECOND backend.chat call, which receives the tool replies)
    tool_msgs = [m for m in be.last_messages if m["role"] == "tool"]
    assert tool_msgs[0]["content"] == str("FORTIFIED")[:1500]

    # --- transcript key present and structured ---
    assert "transcript" in out
    t = out["transcript"]

    # two tool calls → two steps
    assert len(t["steps"]) == 2

    step0 = t["steps"][0]
    assert step0["tool_name"] == "fortify_unit"
    assert step0["tool_result_full"] == "FORTIFIED"
    assert step0["result_total_chars"] == len("FORTIFIED")
    assert step0["result_chars_fed_to_model"] == min(len("FORTIFIED"), 1500)
    assert step0["truncated"] is False

    # --- unknown tool classified in invalid_tool_calls ---
    assert any(
        ic["tool_name"] == "bogus_tool" and ic["reason"] == "unknown_tool"
        for ic in t["invalid_tool_calls"]
    )

    # --- aggregate token sums ---
    assert t["prompt_tokens"] == 60       # 50 + 10
    assert t["completion_tokens"] == 7    # 5 + 2

    # --- control fields ---
    assert t["max_steps_reached"] is False
    assert t["final_summary"] == "all done"
    assert isinstance(t["wall_clock_s"], float) and t["wall_clock_s"] >= 0


@pytest.mark.asyncio
async def test_transcript_max_steps_reached():
    """When the loop exhausts max_steps, max_steps_reached=True in transcript."""
    class AlwaysToolBackend:
        async def chat(self, messages, tools):
            return Reply(text=None, tool_calls=[
                {"id": "x", "name": "fortify_unit", "arguments": '{"unit_index": 0}'}
            ], prompt_tokens=5, completion_tokens=1)

    gs, cost = FakeGS(), FakeCost()
    pol = LLMPolicy(AlwaysToolBackend(), cost, max_steps=2)
    out = await pol(gs, player_id=1, turn=1)
    assert out["summary"] == "max_steps reached"
    assert "transcript" in out
    assert out["transcript"]["max_steps_reached"] is True
    assert len(out["transcript"]["steps"]) == 2  # one step per loop iteration


# ---------------------------------------------------------------------------
# Fix 1: truncation invariant — tool result > 1500 chars
# ---------------------------------------------------------------------------

class FakeGSLong:
    """GS whose get_game_overview returns a 2000-char string."""
    async def get_game_overview(self): return "X" * 2000
    async def get_units(self): return []
    async def get_cities(self): return []


class FakeBackendTruncation:
    """First reply: get_overview call.  Second reply: text (no tool calls)."""
    def __init__(self):
        self.n = 0
        self.last_messages = None

    async def chat(self, messages, tools):
        self.n += 1
        self.last_messages = messages
        if self.n == 1:
            return Reply(
                text=None,
                tool_calls=[{"id": "tr1", "name": "get_overview", "arguments": "{}"}],
                prompt_tokens=20,
                completion_tokens=3,
            )
        return Reply(text="done", tool_calls=[], prompt_tokens=10, completion_tokens=2)


@pytest.mark.asyncio
async def test_transcript_truncation_invariant():
    """tool_result_full is untruncated; model receives only [:1500]."""
    gs = FakeGSLong()
    be = FakeBackendTruncation()
    cost = FakeCost()
    pol = LLMPolicy(be, cost, max_steps=4)
    out = await pol(gs, player_id=1, turn=5)

    t = out["transcript"]
    assert len(t["steps"]) == 1
    step = t["steps"][0]

    # Core invariant: full capture is untruncated
    assert step["tool_result_full"] == "X" * 2000
    assert step["result_total_chars"] == 2000
    assert step["result_chars_fed_to_model"] == 1500
    assert step["truncated"] is True

    # The tool message actually fed to the model is exactly 1500 chars
    tool_msgs = [m for m in be.last_messages if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["content"] == "X" * 1500


# ---------------------------------------------------------------------------
# Fix 2: bad_arguments classification
# ---------------------------------------------------------------------------

class FakeBackendBadArgs:
    """First reply: fortify_unit with malformed JSON arguments.  Second: text."""
    def __init__(self):
        self.n = 0

    async def chat(self, messages, tools):
        self.n += 1
        if self.n == 1:
            return Reply(
                text=None,
                tool_calls=[
                    {"id": "ba1", "name": "fortify_unit",
                     "arguments": '{"unit_index": not valid json'}
                ],
                prompt_tokens=10,
                completion_tokens=2,
            )
        return Reply(text="done", tool_calls=[], prompt_tokens=5, completion_tokens=1)


@pytest.mark.asyncio
async def test_transcript_bad_arguments():
    """Known tool name with malformed JSON args → classified as bad_arguments; run does not crash."""
    gs = FakeGS()
    be = FakeBackendBadArgs()
    cost = FakeCost()
    pol = LLMPolicy(be, cost, max_steps=4)
    # Must not raise
    out = await pol(gs, player_id=1, turn=7)

    t = out["transcript"]

    # bad_arguments entry must appear in invalid_tool_calls
    assert any(
        ic["tool_name"] == "fortify_unit" and ic["reason"] == "bad_arguments"
        for ic in t["invalid_tool_calls"]
    ), f"Expected bad_arguments entry, got: {t['invalid_tool_calls']}"

    # The run still completed (dispatch caught the exception, returned ERROR string)
    assert len(t["steps"]) == 1
    assert t["steps"][0]["tool_result_full"].startswith("ERROR:")
    assert t["max_steps_reached"] is False


def _no_tool_reply(text="done"):
    return Reply(text=text, tool_calls=[], prompt_tokens=10, completion_tokens=5)


class SpyBackend:
    """Records the kwargs of every chat() call; returns queued replies."""

    model = "fake"

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    async def chat(self, messages, tools):
        self.calls.append({"messages": messages, "tools": tools})
        return self.replies.pop(0)


@pytest.mark.asyncio
async def test_options_select_toolset_and_playbook():
    be = SpyBackend([_no_tool_reply()])
    opts = CivOptions(tools="standard", playbook="condensed", max_steps=3)
    pol = LLMPolicy(be, FakeCost(), options=opts)
    await pol(gs=None, player_id=3, turn=5)
    call = be.calls[0]
    names = {t["function"]["name"] for t in call["tools"]}
    assert "get_map_area" in names and "attack_unit" in names
    assert load_playbook() in call["messages"][0]["content"]


@pytest.mark.asyncio
async def test_options_cap_and_steps():
    tool_reply = Reply(
        text=None,
        tool_calls=[{"id": "1", "name": "get_units", "arguments": "{}"}],
        prompt_tokens=10,
        completion_tokens=5,
    )
    be = SpyBackend([tool_reply, _no_tool_reply()])

    class FakeGSLocal:
        async def get_units(self):
            return "U" * 10_000

    opts = CivOptions(result_char_cap=2000, max_steps=2)
    pol = LLMPolicy(be, FakeCost(), options=opts)
    out = await pol(FakeGSLocal(), 3, 5)
    tool_msg = [m for m in be.calls[1]["messages"] if m["role"] == "tool"][0]
    assert len(tool_msg["content"]) == 2000
    step = out["transcript"]["steps"][0]
    assert step["result_chars_fed_to_model"] == 2000 and step["truncated"]


@pytest.mark.asyncio
async def test_out_of_tier_tool_never_executes():
    """A minimal-tier civ calling an in-registry but out-of-tier tool gets an
    ERROR result; the GameState method must NOT run (A/B control integrity)."""

    tool_reply = Reply(
        text=None,
        tool_calls=[{"id": "1", "name": "get_map_area", "arguments": '{"x": 1, "y": 1}'}],
        prompt_tokens=10,
        completion_tokens=5,
    )
    be = SpyBackend([tool_reply, _no_tool_reply()])

    class FakeGSLocal:
        async def get_map_area(self, *a, **kw):
            raise AssertionError("out-of-tier tool must never execute")

    pol = LLMPolicy(be, FakeCost(), options=CivOptions(tools="minimal"))
    out = await pol(FakeGSLocal(), 3, 5)
    tool_msg = [m for m in be.calls[1]["messages"] if m["role"] == "tool"][0]
    assert tool_msg["content"].startswith("ERROR")
    assert any(
        c["tool_name"] == "get_map_area"
        for c in out["transcript"]["invalid_tool_calls"]
    )


@pytest.mark.asyncio
async def test_transcript_carries_options_fingerprint():
    be = SpyBackend([_no_tool_reply()])
    opts = CivOptions(tools="standard")
    pol = LLMPolicy(be, FakeCost(), options=opts)
    out = await pol(None, 3, 5)
    assert out["transcript"]["civ_options"]["tools"] == "standard"


def test_playbook_loads_and_is_reasonably_sized():
    text = load_playbook()
    assert 2000 < len(text) < 20000
    assert "settler" in text.lower()
