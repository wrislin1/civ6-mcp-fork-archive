"""Tests for run_id generation — hostname differentiation + hour bucket."""

import asyncio
import shutil

import pytest

from civ_mcp.run_id import generate_run_id


class TestRunId:
    def test_deterministic_same_inputs(self):
        a = generate_run_id(
            model_id="opus",
            scenario_id="ground_control",
            timestamp=1775915000,
            hostname="solomon",
        )
        b = generate_run_id(
            model_id="opus",
            scenario_id="ground_control",
            timestamp=1775915000,
            hostname="solomon",
        )
        assert a == b

    def test_hostname_differentiates(self):
        kwargs = dict(
            model_id="gpt-5.4",
            scenario_id="ground_control",
            timestamp=1775915000,
        )
        sol = generate_run_id(hostname="solomon", **kwargs)
        ste = generate_run_id(hostname="steed", **kwargs)
        mae = generate_run_id(hostname="maeve", **kwargs)
        assert sol != ste
        assert ste != mae
        assert sol != mae

    def test_hour_bucket_stable_within_hour(self):
        # 5 minutes apart, same hour bucket
        a = generate_run_id(
            model_id="opus",
            scenario_id="snowflake",
            timestamp=1775915000,
            hostname="test",
        )
        b = generate_run_id(
            model_id="opus",
            scenario_id="snowflake",
            timestamp=1775915300,
            hostname="test",
        )
        assert a == b

    def test_hour_bucket_changes_across_hours(self):
        a = generate_run_id(
            model_id="opus",
            scenario_id="snowflake",
            timestamp=1775915000,
            hostname="test",
        )
        b = generate_run_id(
            model_id="opus",
            scenario_id="snowflake",
            timestamp=1775918700,  # > 1 hour later
            hostname="test",
        )
        assert a != b

    def test_regression_gemini_pro_collision_fixed(self):
        """The specific case that caused the production collision: 3 machines
        launching gemini-3.1-pro + ground_control at the same moment."""
        kwargs = dict(
            model_id="gemini-3.1-pro-preview",
            scenario_id="ground_control",
            timestamp=1775915000,
        )
        ids = {
            generate_run_id(hostname=h, **kwargs) for h in ("solomon", "steed", "maeve")
        }
        assert len(ids) == 3

    def test_format(self):
        rid = generate_run_id(
            model_id="opus",
            scenario_id="ground_control",
            timestamp=1775915000,
            hostname="test",
        )
        parts = rid.split("-")
        assert len(parts) == 4
        assert parts[3].isdigit()
        assert len(parts[3]) == 2

    def test_no_context_is_random(self):
        """generate_run_id() with no args uses the random branch — two calls differ."""
        a = generate_run_id()
        b = generate_run_id()
        assert a != b, "random branch must produce distinct IDs on successive calls"

    def test_model_id_arena_is_deterministic(self):
        """Documents the bug: model_id='arena' is deterministic within an hour.

        Two bare 'civ-arena' launches on the same host in the same hour produce
        the same run_id → transcript and cost files append-merge across games.
        """
        a = generate_run_id(model_id="arena", timestamp=1775915000, hostname="riz-llm")
        b = generate_run_id(model_id="arena", timestamp=1775915000, hostname="riz-llm")
        assert a == b, "model_id='arena' must be deterministic (documents the collision)"


# --- Arena wiring: default run_id must not carry model_id ---

def test_arena_default_run_id_no_model_id(monkeypatch, tmp_path):
    """_run default path must call generate_run_id() with no model_id.

    generate_run_id(model_id=...) is deterministic per (host, model, hour) —
    two same-hour bare 'civ-arena' launches collide and merge transcripts.
    The fix: call generate_run_id() with no context so the random branch fires.
    """
    import civ_mcp.run_id as run_id_mod
    from civ_mcp.arena.arena import _run

    captured: dict = {}

    def spy(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return "stub-amber-falcon-00"

    monkeypatch.setattr(run_id_mod, "generate_run_id", spy)
    monkeypatch.setattr(shutil, "which", lambda name: None)  # trigger early SystemExit

    class Args:
        player = ["1:cli-claude:"]
        max_puppet_turns = 1
        gateway_url = "http://localhost:11430/v1"
        api_key_env = "LITELLM_OPENAI_API_KEY"
        cost_path = ""
        max_agent_steps = 6
        dry_run = False
        run_id = ""  # empty → triggers generate_run_id call
        transcript_dir = str(tmp_path / "runs")
        no_transcript = True
        idle_poll_limit = 600

    with pytest.raises(SystemExit):
        asyncio.run(_run(Args()))

    assert captured, "generate_run_id was never called — test wiring broken"
    assert "model_id" not in captured["kwargs"], (
        "generate_run_id must be called without model_id; "
        f"got kwargs={captured['kwargs']!r}"
    )


def test_is_safe_run_id_accepts_normal_and_generated_ids():
    from civ_mcp.run_id import generate_run_id, is_safe_run_id

    assert is_safe_run_id("crimson-amber-falcon-47")
    assert is_safe_run_id("hybrid-4civ-20260705T101112Z")
    assert is_safe_run_id("run_1.2")
    # Generated ids must always pass their own path-safety guard.
    assert is_safe_run_id(generate_run_id("model", "scenario"))
    assert is_safe_run_id(generate_run_id())


def test_is_safe_run_id_rejects_traversal_and_junk():
    from civ_mcp.run_id import is_safe_run_id

    for bad in ["../../tmp/evil", "a/b", ".", "..", "", ".hidden", "has space", None, 5]:
        assert not is_safe_run_id(bad), f"{bad!r} should be rejected"
