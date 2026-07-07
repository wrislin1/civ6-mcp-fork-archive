import asyncio
import types

import pytest

from civ_mcp.arena.backends import (
    MAX_COMPLETION_TOKENS,
    REQUEST_TIMEOUT_S,
    OpenAICompatBackend,
    Reply,
)


class _FakeUsage:  prompt_tokens = 11; completion_tokens = 3
class _FakeMsg:    content = "ok"; tool_calls = None
class _FakeChoice: message = _FakeMsg()
class _FakeResp:   choices = [_FakeChoice()]; usage = _FakeUsage()


@pytest.mark.asyncio
async def test_chat_parses_reply(monkeypatch):
    b = OpenAICompatBackend("http://x/v1", "k", "m")
    async def fake_create(**kw): return _FakeResp()
    monkeypatch.setattr(b._client.chat.completions, "create", fake_create)
    r = await b.chat([{"role": "user", "content": "hi"}], tools=[])
    assert isinstance(r, Reply)
    assert r.text == "ok" and r.prompt_tokens == 11 and r.completion_tokens == 3


class _CapturingCompletions:
    def __init__(self): self.kwargs = None
    async def create(self, **kw):
        self.kwargs = kw
        return _FakeResp()


def _backend_with_capture():
    b = OpenAICompatBackend.__new__(OpenAICompatBackend)
    b.model = "gemma4-26b"
    b.base_url = "http://x/v1"
    cap = _CapturingCompletions()
    b._client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=cap))
    return b, cap


def test_chat_sends_max_tokens_and_timeout():
    # Without these, a degenerate generation runs until it exhausts context and
    # stalls the whole game — the cap + timeout bound each turn-step.
    b, cap = _backend_with_capture()
    asyncio.run(b.chat([{"role": "user", "content": "hi"}], tools=[]))
    assert cap.kwargs["max_tokens"] == MAX_COMPLETION_TOKENS
    assert cap.kwargs["timeout"] == REQUEST_TIMEOUT_S
    assert "tools" not in cap.kwargs


def test_chat_passes_tools_with_cap():
    b, cap = _backend_with_capture()
    asyncio.run(b.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function"}]))
    assert cap.kwargs["tool_choice"] == "auto"
    assert cap.kwargs["max_tokens"] == MAX_COMPLETION_TOKENS


def test_caps_are_bounded():
    # guard against someone loosening the cap back into runaway territory
    assert 256 <= MAX_COMPLETION_TOKENS <= 4096
    assert 30 <= REQUEST_TIMEOUT_S <= 300


def _backend_with_create(create_fn):
    b = OpenAICompatBackend.__new__(OpenAICompatBackend)
    b.model = "gemma4-26b"
    b.base_url = "http://x/v1"
    b._client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create_fn))
    )
    return b


async def _noop(*_a, **_k):
    return None


@pytest.mark.asyncio
async def test_chat_retries_transient_error_then_succeeds(monkeypatch):
    """A transient gateway failure (e.g. a 500 on a malformed tool call -- which at
    temp>0 usually differs when resampled, or a model-swap 503) is retried, and a
    later success returns normally instead of bubbling up and costing the whole turn."""
    monkeypatch.setattr(asyncio, "sleep", _noop)
    calls = {"n": 0}

    async def flaky(**kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("Error code: 500 - Failed to parse tool call arguments as JSON")
        return _FakeResp()

    b = _backend_with_create(flaky)
    r = await b.chat([{"role": "user", "content": "hi"}], tools=[])
    assert r.text == "ok"
    assert calls["n"] == 3   # two failures retried, third succeeded


@pytest.mark.asyncio
async def test_chat_raises_after_bounded_retries(monkeypatch):
    """A persistently-failing call is retried a bounded number of times, then the
    error is re-raised so the coordinator's degrade-not-abort guard skips the turn."""
    monkeypatch.setattr(asyncio, "sleep", _noop)
    calls = {"n": 0}

    async def always_fail(**kw):
        calls["n"] += 1
        raise RuntimeError("Error code: 500 - persistent")

    b = _backend_with_create(always_fail)
    with pytest.raises(RuntimeError):
        await b.chat([{"role": "user", "content": "hi"}], tools=[])
    assert calls["n"] == 3   # bounded: exactly three attempts, no unbounded loop
