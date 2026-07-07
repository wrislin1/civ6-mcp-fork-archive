import pytest
import civ_mcp.arena.agent as agent
from civ_mcp.arena.agent import LLMPolicy
from civ_mcp.arena.backends import Reply
from civ_mcp.arena.config import BriefingOptions, CivOptions
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


class FakeBackendOutOfTier:
    def __init__(self):
        self.n = 0

    async def chat(self, messages, tools):
        self.n += 1
        if self.n == 1:
            return Reply(
                text=None,
                tool_calls=[
                    {"id": "tc1", "name": "get_map_area", "arguments": '{"x": 1, "y": 2}'},
                    {"id": "tc2", "name": "bogus_tool", "arguments": "{}"},
                ],
                prompt_tokens=10,
                completion_tokens=1,
            )
        return Reply(text="done", tool_calls=[], prompt_tokens=10, completion_tokens=1)


@pytest.mark.asyncio
async def test_policy_distinguishes_out_of_tier_from_unknown_tool():
    gs, cost = FakeGS(), FakeCost()
    pol = LLMPolicy(FakeBackendOutOfTier(), cost, options=CivOptions(tools="minimal"))
    out = await pol(gs, player_id=1, turn=3)

    invalid = out["transcript"]["invalid_tool_calls"]
    assert {"tool_name": "get_map_area", "arguments": '{"x": 1, "y": 2}', "reason": "out_of_tier"} in invalid
    assert {"tool_name": "bogus_tool", "arguments": "{}", "reason": "unknown_tool"} in invalid


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


@pytest.mark.asyncio
async def test_briefing_prepended_and_telemetry(monkeypatch):
    from civ_mcp.arena import agent as agent_mod
    from civ_mcp.arena.briefing import Briefing

    async def fake_resolve(base_url, model, budget, http_get=None):
        assert base_url == "http://h:11440/v1"
        assert model == "fake"
        assert budget == "auto"
        return 131072, "upstream_props"

    async def fake_build(gs, opts, budget_tokens):
        assert budget_tokens > 100_000
        return Briefing(
            text="BRIEFING BODY",
            tokens=3,
            sections=["overview"],
            radius=4,
            errors=[],
        )

    monkeypatch.setattr(agent_mod, "resolve_n_ctx", fake_resolve)
    monkeypatch.setattr("civ_mcp.arena.prompt_context.build_briefing", fake_build)

    be = SpyBackend([_no_tool_reply()])
    be.base_url = "http://h:11440/v1"
    opts = CivOptions(briefing=BriefingOptions(enabled=True))
    pol = LLMPolicy(be, FakeCost(), options=opts)
    out = await pol(None, 3, 7)

    user_msg = [m for m in be.calls[0]["messages"] if m["role"] == "user"][0]
    assert user_msg["content"].startswith("BRIEFING BODY")
    assert "It is turn 7. You control player 3. Begin." in user_msg["content"]
    tr = out["transcript"]
    assert tr["briefing_tokens"] == 3
    assert tr["briefing_sections"] == ["overview"]
    assert tr["briefing_radius"] == 4
    assert tr["briefing_errors"] == []
    assert tr["n_ctx"] == 131072
    assert tr["n_ctx_source"] == "upstream_props"


@pytest.mark.asyncio
async def test_policy_uses_supplied_empty_briefing_without_rebuilding(monkeypatch):
    from civ_mcp.arena import agent as agent_mod
    from civ_mcp.arena.briefing import Briefing

    async def forbidden_build(gs, opts, budget):
        raise AssertionError("LLM policy must not rebuild a supplied briefing")

    monkeypatch.setattr(
        "civ_mcp.arena.prompt_context.build_briefing",
        forbidden_build,
    )

    be = SpyBackend([_no_tool_reply()])
    pol = LLMPolicy(
        be,
        FakeCost(),
        options=CivOptions(briefing=BriefingOptions(enabled=True)),
    )

    out = await pol(
        None,
        3,
        7,
        briefing=Briefing(text="", tokens=0, sections=[], errors=["empty prebuild"]),
    )

    user_msg = [m for m in be.calls[0]["messages"] if m["role"] == "user"][0]
    assert user_msg["content"] == "It is turn 7. You control player 3. Begin."
    assert out["transcript"]["briefing_errors"] == ["empty prebuild"]


@pytest.mark.asyncio
async def test_n_ctx_resolved_once_across_turns(monkeypatch):
    from civ_mcp.arena import agent as agent_mod
    from civ_mcp.arena.briefing import Briefing

    calls = []

    async def fake_resolve(*args, **kwargs):
        calls.append((args, kwargs))
        return 32768, "props"

    async def fake_build(gs, opts, budget):
        return Briefing(text="B", tokens=1)

    monkeypatch.setattr(agent_mod, "resolve_n_ctx", fake_resolve)
    monkeypatch.setattr("civ_mcp.arena.prompt_context.build_briefing", fake_build)

    be = SpyBackend([_no_tool_reply(), _no_tool_reply()])
    be.base_url = "http://h:1/v1"
    pol = LLMPolicy(
        be,
        FakeCost(),
        options=CivOptions(briefing=BriefingOptions(enabled=True)),
    )
    await pol(None, 3, 7)
    await pol(None, 3, 8)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_n_ctx_default_fallback_retries_on_next_turn(monkeypatch):
    from civ_mcp.arena import agent as agent_mod
    from civ_mcp.arena.briefing import Briefing

    calls = []

    async def fake_resolve(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) == 1:
            return 16384, "default"
        return 131072, "upstream_props"

    async def fake_build(gs, opts, budget):
        return Briefing(text="B", tokens=1)

    monkeypatch.setattr(agent_mod, "resolve_n_ctx", fake_resolve)
    monkeypatch.setattr("civ_mcp.arena.prompt_context.build_briefing", fake_build)

    be = SpyBackend([_no_tool_reply(), _no_tool_reply()])
    be.base_url = "http://h:1/v1"
    pol = LLMPolicy(
        be,
        FakeCost(),
        options=CivOptions(briefing=BriefingOptions(enabled=True)),
    )

    first = await pol(None, 3, 7)
    second = await pol(None, 3, 8)

    assert len(calls) == 2
    assert first["transcript"]["n_ctx_source"] == "default"
    assert second["transcript"]["n_ctx"] == 131072
    assert second["transcript"]["n_ctx_source"] == "upstream_props"


@pytest.mark.asyncio
async def test_briefing_disabled_is_todays_message():
    be = SpyBackend([_no_tool_reply()])
    pol = LLMPolicy(be, FakeCost(), options=CivOptions())
    await pol(None, 3, 7)
    user_msg = [m for m in be.calls[0]["messages"] if m["role"] == "user"][0]
    assert user_msg["content"] == "It is turn 7. You control player 3. Begin."


@pytest.mark.asyncio
async def test_policy_skips_tool_schema_serialization_when_briefing_disabled(monkeypatch):
    def fail_dumps(value):
        raise AssertionError("tool schema should not be serialized when briefing is disabled")

    monkeypatch.setattr(agent.json, "dumps", fail_dumps)
    be = SpyBackend([_no_tool_reply()])
    pol = LLMPolicy(be, FakeCost(), options=CivOptions(briefing=BriefingOptions(enabled=False)))

    await pol(None, 3, 7)

    user_msg = [m for m in be.calls[0]["messages"] if m["role"] == "user"][0]
    assert user_msg["content"] == "It is turn 7. You control player 3. Begin."


@pytest.mark.asyncio
async def test_policy_skips_tool_schema_serialization_when_briefing_supplied(monkeypatch):
    from civ_mcp.arena.briefing import Briefing

    def fail_dumps(value):
        raise AssertionError("tool schema should not be serialized for supplied briefing")

    monkeypatch.setattr(agent.json, "dumps", fail_dumps)
    be = SpyBackend([_no_tool_reply()])
    pol = LLMPolicy(
        be,
        FakeCost(),
        options=CivOptions(briefing=BriefingOptions(enabled=True)),
    )

    out = await pol(
        None,
        3,
        7,
        briefing=Briefing(text="PREBUILT", tokens=1, sections=["overview"]),
    )

    assert out["transcript"]["briefing_tokens"] == 1
    assert out["transcript"]["briefing_sections"] == ["overview"]


def test_should_resolve_n_ctx_caps_default_retries():
    """Re-probe while stuck on 'default', but stop after the cap so a backend with
    no /props endpoint does not incur an HTTP round-trip every single turn."""
    from civ_mcp.arena.agent import _should_resolve_n_ctx, _N_CTX_MAX_RESOLVES

    # First resolve always happens (nothing resolved yet).
    assert _should_resolve_n_ctx(None, "", "auto", 0) is True
    # Auto budget + still 'default' + under the cap → keep retrying (warm-up).
    assert _should_resolve_n_ctx(4096, "default", "auto", 1) is True
    # Hit the cap → give up re-probing.
    assert _should_resolve_n_ctx(4096, "default", "auto", _N_CTX_MAX_RESOLVES) is False
    # A resolved upstream value stops retries immediately.
    assert _should_resolve_n_ctx(131072, "upstream_props", "auto", 1) is False
    # A fixed (non-auto) budget never re-probes.
    assert _should_resolve_n_ctx(4096, "default", 8000, 1) is False


@pytest.mark.asyncio
async def test_max_steps_final_summary_keeps_last_assistant_text():
    """Step exhaustion must not discard plan text emitted alongside tool calls."""
    class ToolWithProseBackend:
        def __init__(self):
            self.n = 0

        async def chat(self, messages, tools):
            self.n += 1
            return Reply(
                text=f"step {self.n} thinking\nSTANDING PLAN\n- TASK settle unit_id=130 target=20,25",
                tool_calls=[
                    {"id": f"t{self.n}", "name": "fortify_unit", "arguments": '{"unit_index": 0}'}
                ],
                prompt_tokens=5,
                completion_tokens=1,
            )

    gs, cost = FakeGS(), FakeCost()
    pol = LLMPolicy(ToolWithProseBackend(), cost, max_steps=2)
    out = await pol(gs, player_id=1, turn=1)

    assert out["summary"] == "max_steps reached"
    assert out["transcript"]["max_steps_reached"] is True
    assert "STANDING PLAN" in out["transcript"]["final_summary"]
    assert "step 2 thinking" in out["transcript"]["final_summary"]
