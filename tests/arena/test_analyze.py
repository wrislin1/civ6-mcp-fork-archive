"""Tests for civ_mcp.arena.analyze — TDD: RED first, then GREEN."""
from __future__ import annotations
import json
from pathlib import Path
import pytest


# ---------------------------------------------------------------------------
# Fixtures helpers
# ---------------------------------------------------------------------------

def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def _make_step(
    idx: int,
    tool_name: str | None = None,
    tool_args: dict | None = None,
    tool_result_full: str = "OK",
    truncated: bool = False,
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
) -> dict:
    return {
        "idx": idx,
        "role": "assistant",
        "tool_name": tool_name,
        "tool_args": tool_args or {},
        "tool_result_full": tool_result_full,
        "truncated": truncated,
        "ts_start": "2026-01-01T00:00:00Z",
        "ts_end": "2026-01-01T00:00:01Z",
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }


def _config_rec(pid: int, steps: int, invalid: int, brief: int, score: int) -> dict:
    return {
        "player_id": pid,
        "model": "gemma4-26b",
        "provider": "local",
        "driver": "in_process",
        "step_count": steps,
        "invalid_tool_calls": [{}] * invalid,
        "civ_options": {"tools": "standard", "max_steps": 10},
        "briefing_tokens": brief,
        "n_ctx": 131072,
        "state_delta": {"score": score},
        "steps": [],
        "prompt_tokens": 100,
        "completion_tokens": 10,
    }


@pytest.fixture()
def run_dir(tmp_path: Path) -> Path:
    """Create a synthetic arena run with 2 models."""
    run_id = "test-run-001"
    d = tmp_path / "arena_runs" / run_id
    d.mkdir(parents=True)

    # --- transcript records ---

    # Model A, Turn 1 (in_process, LOCAL flat vocabulary):
    #   - 3 steps: step 0 move_unit truncated, step 1 skip_unit, step 2 set_research non-ERROR
    #   - 1 invalid_tool_call (unknown_tool)
    #   - state_delta cities=1  => rubric: founded_extra_city (via state_delta path)
    #   - step 0 (move_unit, truncated) + step 1 (skip_unit) => truncation_bad_move
    #   - step 0 (move_unit) => explored_vs_idle
    tr_a1 = {
        "schema_version": 1,
        "run_id": run_id,
        "ts": "2026-01-01T00:00:01Z",
        "player_id": 1,
        "turn": 1,
        "provider": "local",
        "model": "model-a",
        "driver": "in_process",
        "steps": [
            _make_step(0, tool_name="move_unit",
                       tool_args={"unit_index": 1, "x": 5, "y": 5},
                       truncated=True,
                       prompt_tokens=100, completion_tokens=50),
            _make_step(1, tool_name="skip_unit",
                       tool_args={"unit_index": 1},
                       tool_result_full="OK",
                       prompt_tokens=100, completion_tokens=50),
            _make_step(2, tool_name="set_research",
                       tool_args={"tech": "TECH_ANIMAL_HUSBANDRY"},
                       tool_result_full="Research set.",
                       prompt_tokens=100, completion_tokens=50),
        ],
        "invalid_tool_calls": [
            {"step": 0, "tool_name": "fake_tool", "reason": "unknown_tool", "raw_args": {}}
        ],
        "wall_clock_s": 10.5,
        "final_summary": "Settled city.",
        "prompt_tokens": 100,   # top-level (NOT sum of step tokens)
        "completion_tokens": 50,
        "max_steps_reached": False,
        "step_count": 3,
        "usd": 0.0,
        "state_before": {"score": 10, "gold": 50, "science": 2, "culture": 1,
                         "faith": 0, "research": "TECH_POTTERY", "civic": "CIVIC_CODE_OF_LAWS",
                         "cities": 1, "units": 3},
        "state_after":  {"score": 20, "gold": 55, "science": 4, "culture": 2,
                         "faith": 0, "research": "TECH_ANIMAL_HUSBANDRY",
                         "civic": "CIVIC_CODE_OF_LAWS", "cities": 2, "units": 3},
        "state_delta":  {"score": 10, "gold": 5, "science": 2, "culture": 1,
                         "faith": 0, "cities": 1, "units": 0,
                         "research": "TECH_ANIMAL_HUSBANDRY",
                         "civic": "CIVIC_CODE_OF_LAWS"},
    }

    # Model A, Turn 2 (in_process, LOCAL flat vocabulary):
    #   - 2 steps: step 0 move_unit ERROR (wasted_move), step 1 skip_unit
    #   - no truncation, no invalid calls
    tr_a2 = {
        "schema_version": 1,
        "run_id": run_id,
        "ts": "2026-01-01T00:01:00Z",
        "player_id": 1,
        "turn": 2,
        "provider": "local",
        "model": "model-a",
        "driver": "in_process",
        "steps": [
            _make_step(0, tool_name="move_unit",
                       tool_args={"unit_index": 2, "x": 3, "y": 3},
                       tool_result_full="MOVING_TO|3,3|BLOCKED",
                       prompt_tokens=120, completion_tokens=60),
            _make_step(1, tool_name="skip_unit",
                       tool_args={"unit_index": 3},
                       tool_result_full="OK",
                       prompt_tokens=120, completion_tokens=60),
        ],
        "invalid_tool_calls": [],
        "wall_clock_s": 8.2,
        "final_summary": "skipped unit.",
        "prompt_tokens": 120,
        "completion_tokens": 60,
        "max_steps_reached": False,
        "step_count": 2,
        "usd": 0.0,
        "state_before": {"score": 20, "gold": 55, "science": 4, "culture": 2,
                         "faith": 0, "research": "TECH_ANIMAL_HUSBANDRY",
                         "civic": "CIVIC_CODE_OF_LAWS", "cities": 2, "units": 3},
        "state_after":  {"score": 22, "gold": 58, "science": 5, "culture": 3,
                         "faith": 0, "research": "TECH_ANIMAL_HUSBANDRY",
                         "civic": "CIVIC_CODE_OF_LAWS", "cities": 2, "units": 3},
        "state_delta":  {"score": 2, "gold": 3, "science": 1, "culture": 1,
                         "faith": 0, "cities": 0, "units": 0,
                         "research": "TECH_ANIMAL_HUSBANDRY",
                         "civic": "CIVIC_CODE_OF_LAWS"},
    }

    # Model B, Turn 1 (cli driver — no truncation field in steps):
    #   - 2 steps, no truncation tracking (cli)
    #   - 0 invalid calls: the CLI producer hardcodes invalid_tool_calls=[] — real detection
    #     is deferred to live Task 9 (needs real stream-json/codex stdout).
    tr_b1 = {
        "schema_version": 1,
        "run_id": run_id,
        "ts": "2026-01-01T00:02:00Z",
        "player_id": 2,
        "turn": 1,
        "provider": "anthropic",
        "model": "model-b",
        "driver": "cli",
        "steps": [
            {"tool_name": "get_units", "tool_result_full": "[]"},
            {"tool_name": "end_turn", "tool_result_full": "OK"},
        ],
        "invalid_tool_calls": [],  # CLI producer cannot detect these yet (Task 9)
        "wall_clock_s": 15.0,
        "final_summary": "ended turn.",
        "prompt_tokens": 300,
        "completion_tokens": 100,
        "max_steps_reached": False,
        "step_count": 2,
        "usd": 0.001,
        "cli_exit": 0,
        "cli_stderr_tail": "",
        "state_before": None,
        "state_after":  {"score": 5, "gold": 20, "science": 1, "culture": 0,
                         "faith": 0, "research": None, "civic": None, "cities": 1, "units": 2},
        "state_delta":  {"score": 5, "gold": 5, "science": 1, "culture": 0,
                         "faith": 0, "cities": 0, "units": 0},
    }

    # Model B, Turn 2 (cli driver):
    #   - no invalid calls
    tr_b2 = {
        "schema_version": 1,
        "run_id": run_id,
        "ts": "2026-01-01T00:03:00Z",
        "player_id": 2,
        "turn": 2,
        "provider": "anthropic",
        "model": "model-b",
        "driver": "cli",
        "steps": [
            {"tool_name": "get_game_overview", "tool_result_full": "{}"},
        ],
        "invalid_tool_calls": [],
        "wall_clock_s": 12.0,
        "final_summary": "got overview.",
        "prompt_tokens": 250,
        "completion_tokens": 80,
        "max_steps_reached": False,
        "step_count": 1,
        "usd": 0.0,
        "cli_exit": 0,
        "cli_stderr_tail": "",
        "state_before": None,
        "state_after":  {"score": 8, "gold": 22, "science": 2, "culture": 1,
                         "faith": 0, "research": None, "civic": None, "cities": 1, "units": 2},
        "state_delta":  {"score": 3, "gold": 2, "science": 1, "culture": 1,
                         "faith": 0, "cities": 0, "units": 0},
    }

    _write_jsonl(d / "transcript.jsonl", [tr_a1, tr_a2, tr_b1, tr_b2])

    # --- cost records ---
    cost_records = [
        {"turn": 1, "player_id": 1, "provider": "local",
         "model": "model-a", "prompt_tokens": 100, "completion_tokens": 50, "usd": 0.0},
        {"turn": 2, "player_id": 1, "provider": "local",
         "model": "model-a", "prompt_tokens": 120, "completion_tokens": 60, "usd": 0.0},
        {"turn": 1, "player_id": 2, "provider": "anthropic",
         "model": "model-b", "prompt_tokens": 300, "completion_tokens": 100, "usd": 0.001},
        {"turn": 2, "player_id": 2, "provider": "anthropic",
         "model": "model-b", "prompt_tokens": 250, "completion_tokens": 80, "usd": 0.0},
    ]
    _write_jsonl(d / "arena_cost.jsonl", cost_records)

    return d


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_load_records(run_dir: Path) -> None:
    from civ_mcp.arena.analyze import load_records

    transcript = load_records(run_dir / "transcript.jsonl")
    assert len(transcript) == 4

    cost = load_records(run_dir / "arena_cost.jsonl")
    assert len(cost) == 4


def test_load_records_missing_file(tmp_path: Path) -> None:
    from civ_mcp.arena.analyze import load_records

    result = load_records(tmp_path / "nonexistent.jsonl")
    assert result == []


def test_per_model_series_present_and_ordered(run_dir: Path) -> None:
    from civ_mcp.arena.analyze import load_records, analyze

    tr = load_records(run_dir / "transcript.jsonl")
    co = load_records(run_dir / "arena_cost.jsonl")
    report = analyze(tr, co)

    # player_id=1 -> model-a, player_id=2 -> model-b
    assert 1 in report["by_player"]
    assert 2 in report["by_player"]

    # Series ordered by turn
    a_series = report["by_player"][1]["series"]
    assert len(a_series) == 2
    assert a_series[0]["turn"] == 1
    assert a_series[1]["turn"] == 2

    b_series = report["by_player"][2]["series"]
    assert len(b_series) == 2
    assert b_series[0]["turn"] == 1
    assert b_series[1]["turn"] == 2


def test_series_contains_expected_fields(run_dir: Path) -> None:
    from civ_mcp.arena.analyze import load_records, analyze

    tr = load_records(run_dir / "transcript.jsonl")
    co = load_records(run_dir / "arena_cost.jsonl")
    report = analyze(tr, co)

    row = report["by_player"][1]["series"][0]
    for field in ("turn", "score", "cities", "units", "science", "culture",
                  "prompt_tokens", "completion_tokens", "wall_clock_s", "step_count", "state_delta"):
        assert field in row, f"Missing field: {field}"

    # state_after values propagated
    assert row["score"] == 20
    assert row["cities"] == 2
    assert row["units"] == 3


def test_invalid_call_rate_model_a(run_dir: Path) -> None:
    """model-a (player 1): 1 invalid call across 3+2=5 steps => rate = 0.2 exactly."""
    from civ_mcp.arena.analyze import load_records, analyze

    tr = load_records(run_dir / "transcript.jsonl")
    co = load_records(run_dir / "arena_cost.jsonl")
    report = analyze(tr, co)

    rates_a = report["by_player"][1]["rates"]
    assert rates_a["invalid_call_rate"] == pytest.approx(1 / 5)


def test_invalid_call_rate_model_b(run_dir: Path) -> None:
    """model-b (player 2): CLI producer hardcodes invalid_tool_calls=[] — rate is 0.0.

    Real invalid-call detection for CLI drivers requires captured stream-json/codex stdout
    and is deferred to live Task 9.  This test documents that gap: the fixture accurately
    reflects what the producer emits today (an empty list), not a capability it lacks.
    """
    from civ_mcp.arena.analyze import load_records, analyze

    tr = load_records(run_dir / "transcript.jsonl")
    co = load_records(run_dir / "arena_cost.jsonl")
    report = analyze(tr, co)

    rates_b = report["by_player"][2]["rates"]
    assert rates_b["invalid_call_rate"] == pytest.approx(0.0)


def test_config_summary_groups_by_player() -> None:
    from civ_mcp.arena.analyze import config_summary

    recs = [
        _config_rec(3, 4, 1, 30000, 2),
        _config_rec(3, 6, 0, 31000, 3),
        _config_rec(4, 2, 0, 0, 1),
    ]

    summary = config_summary(recs)

    p3 = summary["3"]
    assert p3["turns"] == 2
    assert p3["avg_steps"] == 5.0
    assert p3["invalid_call_rate"] == pytest.approx(1 / 10)
    assert p3["avg_briefing_tokens"] == 30500
    assert p3["avg_score_delta"] == 2.5
    assert p3["civ_options"]["tools"] == "standard"
    assert "4" in summary


def test_config_summary_splits_same_player_when_fingerprint_changes() -> None:
    from civ_mcp.arena.analyze import config_summary

    records = [
        {
            "player_id": 3,
            "model": "m-a",
            "provider": "local",
            "civ_options": {"tools": "minimal", "max_steps": 6},
            "n_ctx": 16384,
            "step_count": 2,
            "invalid_tool_calls": [],
            "briefing_tokens": 0,
            "state_delta": {"score": 1},
        },
        {
            "player_id": 3,
            "model": "m-b",
            "provider": "local",
            "civ_options": {"tools": "standard", "max_steps": 10},
            "n_ctx": 131072,
            "step_count": 4,
            "invalid_tool_calls": [],
            "briefing_tokens": 1200,
            "state_delta": {"score": 3},
        },
    ]

    summary = config_summary(records)

    assert set(summary) == {"3#1", "3#2"}
    assert summary["3#1"]["model"] == "m-a"
    assert summary["3#1"]["civ_options"]["tools"] == "minimal"
    assert summary["3#1"]["turns"] == 1
    assert summary["3#2"]["model"] == "m-b"
    assert summary["3#2"]["civ_options"]["tools"] == "standard"
    assert summary["3#2"]["turns"] == 1


def test_config_summary_does_not_split_on_mid_run_n_ctx_change() -> None:
    """Same model/options across turns stays ONE group even if n_ctx changes.

    A cold llama-swap backend resolves DEFAULT_N_CTX on turn 1 then the real
    context window once warm; keying the config fingerprint on n_ctx used to
    fragment that single continuous player into 3#1/3#2 and corrupt the averages.
    """
    from civ_mcp.arena.analyze import config_summary

    records = [
        {"player_id": 3, "model": "m", "provider": "local",
         "civ_options": {"tools": "minimal"}, "n_ctx": 4096,
         "step_count": 2, "invalid_tool_calls": []},
        {"player_id": 3, "model": "m", "provider": "local",
         "civ_options": {"tools": "minimal"}, "n_ctx": 131072,
         "step_count": 3, "invalid_tool_calls": []},
    ]

    summary = config_summary(records)

    assert set(summary) == {"3"}
    assert summary["3"]["turns"] == 2
    # Report the largest resolved window, not the transient default.
    assert summary["3"]["n_ctx"] == 131072


def test_config_summary_prefers_latest_non_default_n_ctx_over_cold_default() -> None:
    from civ_mcp.arena.analyze import config_summary

    records = [
        {
            "player_id": 3,
            "model": "m",
            "provider": "local",
            "civ_options": {"tools": "minimal"},
            "n_ctx": 16384,
            "n_ctx_source": "default",
            "step_count": 1,
            "invalid_tool_calls": [],
        },
        {
            "player_id": 3,
            "model": "m",
            "provider": "local",
            "civ_options": {"tools": "minimal"},
            "n_ctx": 8192,
            "n_ctx_source": "upstream_props",
            "step_count": 1,
            "invalid_tool_calls": [],
        },
    ]

    summary = config_summary(records)

    assert summary["3"]["n_ctx"] == 8192


def test_config_summary_keeps_plain_player_key_for_single_fingerprint() -> None:
    from civ_mcp.arena.analyze import config_summary

    records = [
        {
            "player_id": 3,
            "model": "m-a",
            "provider": "local",
            "civ_options": {"tools": "minimal"},
            "n_ctx": 16384,
            "step_count": 2,
            "invalid_tool_calls": [],
        },
        {
            "player_id": 3,
            "model": "m-a",
            "provider": "local",
            "civ_options": {"tools": "minimal"},
            "n_ctx": 16384,
            "step_count": 3,
            "invalid_tool_calls": [],
        },
    ]

    summary = config_summary(records)

    assert set(summary) == {"3"}
    assert summary["3"]["turns"] == 2


def test_config_summary_falls_back_when_player_id_missing() -> None:
    from civ_mcp.arena.analyze import config_summary

    legacy_model = _config_rec(0, 4, 1, 12000, 2)
    legacy_model.pop("player_id")
    legacy_model["model"] = "legacy-model"
    legacy_model["provider"] = "local"

    legacy_provider = _config_rec(0, 2, 0, 8000, 1)
    legacy_provider.pop("player_id")
    legacy_provider["model"] = ""
    legacy_provider["provider"] = "cli-claude"

    unknown = _config_rec(0, 1, 0, 0, 0)
    unknown.pop("player_id")
    unknown["model"] = ""
    unknown["provider"] = ""

    summary = config_summary([legacy_model, legacy_provider, unknown])

    assert summary["legacy-model"]["turns"] == 1
    assert summary["cli-claude"]["turns"] == 1
    assert summary["unknown"]["turns"] == 1


def test_analyze_report_carries_config_summary() -> None:
    from civ_mcp.arena.analyze import analyze

    report = analyze([_config_rec(3, 4, 1, 30000, 2)], [])

    assert report["config_summary"]["3"]["turns"] == 1


def test_render_markdown_has_experiment_config_table() -> None:
    from civ_mcp.arena.analyze import analyze, render_markdown

    report = analyze([_config_rec(3, 4, 1, 30000, 2)], [])
    md = render_markdown(report)

    assert "## Experiment config" in md
    assert "| player | model | tools | max_steps | n_ctx | avg briefing tok | avg steps | invalid rate | avg Δscore |" in md
    assert "gemma4-26b" in md


def test_render_markdown_sorts_numeric_players_numerically() -> None:
    from civ_mcp.arena.analyze import analyze, render_markdown

    report = analyze([
        _config_rec(10, 1, 0, 100, 1),
        _config_rec(2, 1, 0, 100, 1),
    ], [])
    md = render_markdown(report)

    assert md.index("| 2 |") < md.index("| 10 |")


def test_config_summary_and_markdown_tolerate_partial_config_fields() -> None:
    from civ_mcp.arena.analyze import analyze, render_markdown

    report = analyze([{
        "player_id": 5,
        "turn": 1,
    }], [])
    summary = report["config_summary"]["5"]
    md = render_markdown(report)

    assert summary["model"] == ""
    assert summary["provider"] == ""
    assert summary["civ_options"] == {}
    assert summary["n_ctx"] is None
    assert summary["invalid_call_rate"] == 0.0
    assert "| 5 |" in md


def test_truncation_incident_rate_model_a(run_dir: Path) -> None:
    """model-a (player 1, in_process): 1 truncated step out of 3+2=5 local steps => 0.2."""
    from civ_mcp.arena.analyze import load_records, analyze

    tr = load_records(run_dir / "transcript.jsonl")
    co = load_records(run_dir / "arena_cost.jsonl")
    report = analyze(tr, co)

    rates_a = report["by_player"][1]["rates"]
    assert rates_a["truncation_incident_rate"] == pytest.approx(1 / 5)


def test_truncation_incident_rate_model_b_zero(run_dir: Path) -> None:
    """model-b (player 2, cli): no truncation tracking => rate 0.0."""
    from civ_mcp.arena.analyze import load_records, analyze

    tr = load_records(run_dir / "transcript.jsonl")
    co = load_records(run_dir / "arena_cost.jsonl")
    report = analyze(tr, co)

    rates_b = report["by_player"][2]["rates"]
    assert rates_b["truncation_incident_rate"] == pytest.approx(0.0)


def test_rubric_founded_extra_city_model_a(run_dir: Path) -> None:
    """model-a (player 1) turn 1 has state_delta.cities=1 => rubric founded_extra_city set."""
    from civ_mcp.arena.analyze import load_records, analyze

    tr = load_records(run_dir / "transcript.jsonl")
    co = load_records(run_dir / "arena_cost.jsonl")
    report = analyze(tr, co)

    rubric_a = report["by_player"][1]["rubric"]
    assert rubric_a["founded_extra_city"] is not None
    assert rubric_a["founded_extra_city"]["turn"] == 1


def test_rubric_hallucinated_tools_model_a(run_dir: Path) -> None:
    """model-a (player 1) turn 1 has unknown_tool invalid call => rubric hallucinated_tools set."""
    from civ_mcp.arena.analyze import load_records, analyze

    tr = load_records(run_dir / "transcript.jsonl")
    co = load_records(run_dir / "arena_cost.jsonl")
    report = analyze(tr, co)

    rubric_a = report["by_player"][1]["rubric"]
    assert rubric_a["hallucinated_tools"] is not None
    assert rubric_a["hallucinated_tools"]["turn"] == 1
    assert rubric_a["hallucinated_tools"]["tool_name"] == "fake_tool"


def test_rubric_hallucinated_tools_model_b(run_dir: Path) -> None:
    """model-b (player 2, CLI driver): hallucinated_tools is NOT detectable yet.

    The CLI producer hardcodes invalid_tool_calls=[] — real detection requires parsing
    captured stream-json/codex stdout, deferred to live Task 9.  This test documents
    the gap: rubric hallucinated_tools must be None for CLI transcripts today.
    """
    from civ_mcp.arena.analyze import load_records, analyze

    tr = load_records(run_dir / "transcript.jsonl")
    co = load_records(run_dir / "arena_cost.jsonl")
    report = analyze(tr, co)

    rubric_b = report["by_player"][2]["rubric"]
    # CLI cannot detect hallucinated tools yet (Task 9); must be None, not a false positive.
    assert rubric_b["hallucinated_tools"] is None


def test_out_of_tier_calls_do_not_count_as_invalid_rate_or_hallucination():
    from civ_mcp.arena.analyze import analyze

    records = [
        {
            "player_id": 1,
            "model": "m",
            "provider": "local",
            "driver": "in_process",
            "turn": 1,
            "step_count": 2,
            "steps": [],
            "invalid_tool_calls": [
                {"tool_name": "get_map_area", "reason": "out_of_tier"},
            ],
        }
    ]

    report = analyze(records, [])

    assert report["by_player"][1]["rates"]["invalid_call_rate"] == 0.0
    assert report["by_player"][1]["rubric"]["hallucinated_tools"] is None
    assert report["config_summary"]["1"]["invalid_call_rate"] == 0.0


def test_default_output_paths(tmp_path: Path) -> None:
    """Default output paths are derived from --runs-dir and --run-id."""
    from civ_mcp.arena.analyze import main
    import sys

    runs_dir = tmp_path / "arena_runs"
    run_id = "my-run"
    run_dir_path = runs_dir / run_id
    run_dir_path.mkdir(parents=True)
    _write_jsonl(run_dir_path / "transcript.jsonl", [])
    _write_jsonl(run_dir_path / "arena_cost.jsonl", [])

    sys.argv = [
        "civ-arena-analyze",
        "--run-id", run_id,
        "--runs-dir", str(runs_dir),
    ]
    main()

    assert (run_dir_path / "report.md").exists()
    assert (run_dir_path / "report.json").exists()


def test_custom_output_paths(tmp_path: Path) -> None:
    """Custom --output-md / --output-json paths are honoured."""
    from civ_mcp.arena.analyze import main
    import sys

    runs_dir = tmp_path / "arena_runs"
    run_id = "custom-out"
    run_dir_path = runs_dir / run_id
    run_dir_path.mkdir(parents=True)
    _write_jsonl(run_dir_path / "transcript.jsonl", [])
    _write_jsonl(run_dir_path / "arena_cost.jsonl", [])

    custom_md = tmp_path / "out" / "my_report.md"
    custom_json = tmp_path / "out" / "my_report.json"

    sys.argv = [
        "civ-arena-analyze",
        "--run-id", run_id,
        "--runs-dir", str(runs_dir),
        "--output-md", str(custom_md),
        "--output-json", str(custom_json),
    ]
    main()

    assert custom_md.exists()
    assert custom_json.exists()


def test_render_markdown_non_empty_contains_model_names(run_dir: Path) -> None:
    from civ_mcp.arena.analyze import load_records, analyze, render_markdown

    tr = load_records(run_dir / "transcript.jsonl")
    co = load_records(run_dir / "arena_cost.jsonl")
    report = analyze(tr, co)
    md = render_markdown(report)

    assert len(md) > 0
    assert "model-a" in md
    assert "model-b" in md


def test_render_markdown_contains_rate_and_rubric(run_dir: Path) -> None:
    from civ_mcp.arena.analyze import load_records, analyze, render_markdown

    tr = load_records(run_dir / "transcript.jsonl")
    co = load_records(run_dir / "arena_cost.jsonl")
    report = analyze(tr, co)
    md = render_markdown(report)

    # Must mention rates and rubric flags
    assert "invalid" in md.lower()
    assert "truncat" in md.lower()
    assert "founded_extra_city" in md or "founded" in md.lower()


def test_token_totals_use_top_level_not_step_sum(tmp_path: Path) -> None:
    """
    Critical: a reply with 2 tool-call steps each has prompt_tokens=100.
    Step-sum = 200. Top-level prompt_tokens = 100.
    Analyzer must report 100, not 200.
    """
    from civ_mcp.arena.analyze import load_records, analyze

    run_id = "token-test"
    d = tmp_path / "arena_runs" / run_id
    d.mkdir(parents=True)

    # Two steps for the SAME reply — step-level tokens repeat (double-counting trap)
    steps_two_calls = [
        _make_step(0, tool_name="get_units", prompt_tokens=100, completion_tokens=50),
        _make_step(1, tool_name="end_turn",  prompt_tokens=100, completion_tokens=50),
    ]
    # step-sum would be 200 prompt / 100 completion
    # top-level says 100 / 50 — that's what we should use

    tr_rec = {
        "schema_version": 1,
        "run_id": run_id,
        "ts": "2026-01-01T00:00:00Z",
        "player_id": 1,
        "turn": 5,
        "provider": "local",
        "model": "token-check-model",
        "driver": "in_process",
        "steps": steps_two_calls,
        "invalid_tool_calls": [],
        "wall_clock_s": 5.0,
        "final_summary": "two-call reply",
        "prompt_tokens": 100,      # top-level — the correct value
        "completion_tokens": 50,
        "max_steps_reached": False,
        "step_count": 2,
        "usd": 0.0,
        "state_before": None,
        "state_after": None,
        "state_delta": None,
    }

    _write_jsonl(d / "transcript.jsonl", [tr_rec])
    _write_jsonl(d / "arena_cost.jsonl", [
        {"turn": 5, "player_id": 1, "provider": "local",
         "model": "token-check-model", "prompt_tokens": 100, "completion_tokens": 50, "usd": 0.0}
    ])

    tr = load_records(d / "transcript.jsonl")
    co = load_records(d / "arena_cost.jsonl")
    report = analyze(tr, co)

    series = report["by_player"][1]["series"]
    assert len(series) == 1
    row = series[0]
    # Must be 100, NOT 200 (which step-sum would give)
    assert row["prompt_tokens"] == 100, (
        f"Token double-counting bug: got {row['prompt_tokens']}, expected 100 (top-level)"
    )
    assert row["completion_tokens"] == 50


def test_rubric_flags_contain_turn_citation(run_dir: Path) -> None:
    """Every non-None rubric flag must carry a 'turn' key."""
    from civ_mcp.arena.analyze import load_records, analyze

    tr = load_records(run_dir / "transcript.jsonl")
    co = load_records(run_dir / "arena_cost.jsonl")
    report = analyze(tr, co)

    for seat, data in report["by_player"].items():
        for flag, val in data["rubric"].items():
            if val is not None:
                assert "turn" in val, (
                    f"seat={seat} rubric flag={flag} is set but missing 'turn': {val}"
                )


def test_empty_run_no_crash(tmp_path: Path) -> None:
    """analyze() with empty inputs returns a report with empty by_player."""
    from civ_mcp.arena.analyze import analyze

    report = analyze([], [])
    assert "by_player" in report
    assert report["by_player"] == {}


def test_json_output_is_valid(run_dir: Path) -> None:
    """The JSON output produced by main() must be valid JSON containing by_player."""
    from civ_mcp.arena.analyze import main
    import sys

    runs_dir = run_dir.parent  # tmp/arena_runs
    run_id = run_dir.name

    sys.argv = [
        "civ-arena-analyze",
        "--run-id", run_id,
        "--runs-dir", str(runs_dir),
    ]
    main()

    json_path = run_dir / "report.json"
    data = json.loads(json_path.read_text())
    assert "by_player" in data
    # JSON serialises integer keys as strings; player_id=1 → "1"
    assert "1" in data["by_player"]
    assert data["by_player"]["1"]["model"] == "model-a"


# ---------------------------------------------------------------------------
# Fix #1c — local vocabulary rubric assertions (would FAIL under pre-fix rubric)
# These assertions require the _step_verb normalizer in analyze.py to map flat
# LOCAL tool names (move_unit, skip_unit, etc.) to the correct verbs.
# Under the OLD rubric (unit_action-only), all four would return None.
# ---------------------------------------------------------------------------

def test_rubric_explored_vs_idle_local_vocab(run_dir: Path) -> None:
    """model-a (player 1) uses local move_unit step → explored_vs_idle fires at turn 1.
    Would FAIL under old rubric: old code required tool_name='unit_action'+action='move'."""
    from civ_mcp.arena.analyze import load_records, analyze

    tr = load_records(run_dir / "transcript.jsonl")
    co = load_records(run_dir / "arena_cost.jsonl")
    report = analyze(tr, co)

    rubric_a = report["by_player"][1]["rubric"]
    assert rubric_a["explored_vs_idle"] is not None, (
        "explored_vs_idle must fire for local move_unit step (old rubric would miss this)"
    )
    assert rubric_a["explored_vs_idle"]["turn"] == 1
    assert "explored" in rubric_a["explored_vs_idle"]["note"]


def test_rubric_wasted_move_local_vocab(run_dir: Path) -> None:
    """model-a (player 1) turn 2 has move_unit with ERROR result → wasted_move fires.
    Would FAIL under old rubric: old code required tool_name='unit_action'+action='move'."""
    from civ_mcp.arena.analyze import load_records, analyze

    tr = load_records(run_dir / "transcript.jsonl")
    co = load_records(run_dir / "arena_cost.jsonl")
    report = analyze(tr, co)

    rubric_a = report["by_player"][1]["rubric"]
    assert rubric_a["wasted_move"] is not None, (
        "wasted_move must fire for local move_unit+ERROR step (old rubric would miss this)"
    )
    assert rubric_a["wasted_move"]["turn"] == 2


def test_rubric_truncation_bad_move_local_vocab(run_dir: Path) -> None:
    """model-a (player 1) turn 1: truncated move_unit then skip_unit → truncation_bad_move fires.
    Would FAIL under old rubric: old code required tool_name='unit_action'+action='skip'."""
    from civ_mcp.arena.analyze import load_records, analyze

    tr = load_records(run_dir / "transcript.jsonl")
    co = load_records(run_dir / "arena_cost.jsonl")
    report = analyze(tr, co)

    rubric_a = report["by_player"][1]["rubric"]
    assert rubric_a["truncation_bad_move"] is not None, (
        "truncation_bad_move must fire for truncated step + skip_unit (old rubric would miss this)"
    )
    assert rubric_a["truncation_bad_move"]["turn"] == 1


def test_rubric_founded_extra_city_via_local_step_path(tmp_path: Path) -> None:
    """found_city flat tool name fires founded_extra_city via step path when state_delta.cities=0.
    Would FAIL under old rubric: old code required tool_name='unit_action'+action='found_city'."""
    from civ_mcp.arena.analyze import load_records, analyze

    run_id = "found-city-step-path"
    d = tmp_path / "arena_runs" / run_id
    d.mkdir(parents=True)

    rec = {
        "schema_version": 1,
        "run_id": run_id,
        "ts": "2026-01-01T00:00:00Z",
        "player_id": 1,
        "turn": 3,
        "provider": "local",
        "model": "local-found",
        "driver": "in_process",
        "steps": [
            _make_step(0, tool_name="found_city", tool_args={"unit_index": 0},
                       tool_result_full="City founded."),
        ],
        "invalid_tool_calls": [],
        "wall_clock_s": 1.0,
        "final_summary": "founded",
        "prompt_tokens": 50,
        "completion_tokens": 10,
        "max_steps_reached": False,
        "step_count": 1,
        "usd": 0.0,
        "state_before": None,
        "state_after": None,
        "state_delta": {"cities": 0},   # zero delta — forces step-path check
    }
    _write_jsonl(d / "transcript.jsonl", [rec])
    _write_jsonl(d / "arena_cost.jsonl", [])

    tr = load_records(d / "transcript.jsonl")
    report = analyze(tr, [])
    # player_id=1 is present in the record, so key is 1
    rubric = report["by_player"][1]["rubric"]
    assert rubric["founded_extra_city"] is not None, (
        "founded_extra_city must fire for flat found_city tool via step path "
        "(old rubric would miss this)"
    )
    assert rubric["founded_extra_city"]["turn"] == 3


def test_rubric_cli_vocabulary_mcp_prefixed(tmp_path: Path) -> None:
    """CLI-style mcp__civ6__unit_action+tool_args={'action':'move'} fires explored_vs_idle.
    Proves the _step_verb normalizer handles the MCP-prefixed CLI vocabulary."""
    from civ_mcp.arena.analyze import load_records, analyze

    run_id = "cli-vocab-test"
    d = tmp_path / "arena_runs" / run_id
    d.mkdir(parents=True)

    cli_step = {
        "idx": 0,
        "role": "tool",
        "tool_name": "mcp__civ6__unit_action",
        "tool_args": {"action": "move", "unit_id": 1, "target_x": 5, "target_y": 5},
        "tool_result_full": "OK",
        "truncated": False,
        "ts_start": "2026-01-01T00:00:00Z",
        "ts_end": "2026-01-01T00:00:01Z",
        "prompt_tokens": 200,
        "completion_tokens": 30,
    }
    wasted_step = {
        "idx": 1,
        "role": "tool",
        "tool_name": "mcp__civ6__unit_action",
        "tool_args": {"action": "move", "unit_id": 2, "target_x": 9, "target_y": 9},
        "tool_result_full": "MOVING_TO|9,9|BLOCKED",
        "truncated": False,
        "ts_start": "2026-01-01T00:00:01Z",
        "ts_end": "2026-01-01T00:00:02Z",
        "prompt_tokens": 200,
        "completion_tokens": 30,
    }
    set_res_step = {
        "idx": 2,
        "role": "tool",
        "tool_name": "mcp__civ6__set_research",
        "tool_args": {"tech": "TECH_POTTERY"},
        "tool_result_full": "Research set.",
        "truncated": False,
        "ts_start": "2026-01-01T00:00:02Z",
        "ts_end": "2026-01-01T00:00:03Z",
        "prompt_tokens": 200,
        "completion_tokens": 30,
    }
    rec = {
        "schema_version": 1,
        "run_id": run_id,
        "ts": "2026-01-01T00:00:00Z",
        "player_id": 2,
        "turn": 1,
        "provider": "anthropic",
        "model": "cli-model",
        "driver": "cli",
        "steps": [cli_step, wasted_step, set_res_step],
        "invalid_tool_calls": [],
        "wall_clock_s": 5.0,
        "final_summary": "moved",
        "prompt_tokens": 200,
        "completion_tokens": 30,
        "max_steps_reached": False,
        "step_count": 3,
        "usd": 0.001,
        "state_before": None,
        "state_after": None,
        "state_delta": None,
    }
    _write_jsonl(d / "transcript.jsonl", [rec])
    _write_jsonl(d / "arena_cost.jsonl", [])

    tr = load_records(d / "transcript.jsonl")
    report = analyze(tr, [])
    # player_id=2 is present in the record, so key is 2
    rubric = report["by_player"][2]["rubric"]

    assert rubric["explored_vs_idle"] is not None, (
        "explored_vs_idle must fire for mcp__civ6__unit_action+action=move"
    )
    assert rubric["wasted_move"] is not None, (
        "wasted_move must fire for mcp__civ6__unit_action+action=move+ERROR"
    )
    assert rubric["set_research_or_production"] is not None, (
        "set_research_or_production must fire for mcp__civ6__set_research (non-ERROR)"
    )


# ---------------------------------------------------------------------------
# Fix #1 Task-A — real game failure strings are detected as errors
# ---------------------------------------------------------------------------

def test_rubric_set_research_or_production_error_result_not_counted(tmp_path: Path) -> None:
    """A set_city_production step returning 'Error: CANNOT_START|...' must NOT be
    counted as a successful set — rubric["set_research_or_production"] must remain None.
    This FAILS under the old startswith("ERROR") logic (title-case 'Error:' was not caught)."""
    from civ_mcp.arena.analyze import load_records, analyze

    run_id = "set-prod-error"
    d = tmp_path / "arena_runs" / run_id
    d.mkdir(parents=True)

    rec = {
        "schema_version": 1,
        "run_id": run_id,
        "ts": "2026-01-01T00:00:00Z",
        "player_id": 1,
        "turn": 1,
        "provider": "local",
        "model": "set-prod-model",
        "driver": "in_process",
        "steps": [
            _make_step(0, tool_name="set_city_production",
                       tool_args={"city_id": 1, "item_type": "UNIT", "item_name": "UNIT_WARRIOR"},
                       tool_result_full="Error: CANNOT_START|UNIT_WARRIOR cannot start."),
        ],
        "invalid_tool_calls": [],
        "wall_clock_s": 1.0,
        "final_summary": "set failed",
        "prompt_tokens": 50,
        "completion_tokens": 10,
        "max_steps_reached": False,
        "step_count": 1,
        "usd": 0.0,
        "state_before": None,
        "state_after": None,
        "state_delta": None,
    }
    _write_jsonl(d / "transcript.jsonl", [rec])
    _write_jsonl(d / "arena_cost.jsonl", [])

    tr = load_records(d / "transcript.jsonl")
    report = analyze(tr, [])
    # player_id=1 is present in the record, so key is 1
    rubric = report["by_player"][1]["rubric"]
    assert rubric["set_research_or_production"] is None, (
        "Error: CANNOT_START|... must be treated as a failure and NOT set "
        "set_research_or_production (old startswith('ERROR') would miss this)"
    )


# ---------------------------------------------------------------------------
# Fix #2 — empty model falls back to provider for grouping key
# ---------------------------------------------------------------------------

def test_empty_model_falls_back_to_provider(tmp_path: Path) -> None:
    """Model='' with provider='cli-claude': group key is player_id=1; provider label
    is preserved as 'cli-claude' (not dropped or overwritten with 'unknown')."""
    from civ_mcp.arena.analyze import load_records, analyze

    run_id = "cli-claude-no-model"
    d = tmp_path / "arena_runs" / run_id
    d.mkdir(parents=True)

    rec = {
        "schema_version": 1,
        "run_id": run_id,
        "ts": "2026-01-01T00:00:00Z",
        "player_id": 1,
        "turn": 1,
        "provider": "cli-claude",
        "model": "",          # empty — typical for cli-claude
        "driver": "cli",
        "steps": [],
        "invalid_tool_calls": [],
        "wall_clock_s": 2.0,
        "final_summary": "done",
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "max_steps_reached": False,
        "step_count": 0,
        "usd": 0.0,
        "state_before": None,
        "state_after": None,
        "state_delta": None,
    }
    _write_jsonl(d / "transcript.jsonl", [rec])
    _write_jsonl(d / "arena_cost.jsonl", [])

    tr = load_records(d / "transcript.jsonl")
    report = analyze(tr, [])

    # Grouped by player_id=1; provider label must be preserved
    assert 1 in report["by_player"], (
        "player_id=1 must be a key in by_player"
    )
    group = report["by_player"][1]
    assert group["provider"] == "cli-claude", (
        "provider label must be 'cli-claude' (not 'unknown')"
    )
    assert group["player_id"] == 1


# ---------------------------------------------------------------------------
# Task D — load_records must skip non-dict JSONL lines (Finding 5)
# ---------------------------------------------------------------------------

def test_load_records_skips_non_dict_lines(tmp_path: Path) -> None:
    """load_records must drop bare scalars and JSON lists; only dicts are returned.

    Under the unfixed code, load_records appends ANY parsed JSON value, so
    analyze() later crashes with AttributeError when rec.get() is called on
    a non-dict. This test is RED until isinstance(obj, dict) guard is added.
    """
    from civ_mcp.arena.analyze import load_records, analyze

    jsonl_path = tmp_path / "mixed.jsonl"
    valid_rec = {
        "schema_version": 1,
        "run_id": "task-d-test",
        "ts": "2026-01-01T00:00:00Z",
        "player_id": 1,
        "turn": 1,
        "provider": "local",
        "model": "task-d-model",
        "driver": "in_process",
        "steps": [],
        "invalid_tool_calls": [],
        "wall_clock_s": 1.0,
        "final_summary": "done",
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "max_steps_reached": False,
        "step_count": 0,
        "usd": 0.0,
        "state_before": None,
        "state_after": None,
        "state_delta": None,
    }
    # Write a JSONL with: bare integer, JSON list, valid dict, bare string
    lines = [
        "123",
        "[1, 2]",
        json.dumps(valid_rec),
        '"just a string"',
    ]
    jsonl_path.write_text("\n".join(lines) + "\n")

    records = load_records(jsonl_path)

    # Only the dict should survive
    assert len(records) == 1, (
        f"Expected 1 dict record, got {len(records)}: {records!r}"
    )
    assert records[0]["model"] == "task-d-model"

    # analyze must not raise AttributeError on the filtered records
    report = analyze(records, [])
    assert 1 in report["by_player"]


# ---------------------------------------------------------------------------
# Task E — group by seat (player_id), not model (Finding 6)
# ---------------------------------------------------------------------------

def test_same_model_different_seat_produces_two_groups(tmp_path: Path) -> None:
    """Two records sharing the same model but different player_id must produce
    two distinct groups (one per seat).  FAILS today — model-only grouping
    collapses them into one series."""
    from civ_mcp.arena.analyze import analyze

    base = {
        "schema_version": 1, "run_id": "seat-test", "ts": "2026-01-01T00:00:00Z",
        "turn": 1, "provider": "local", "model": "model-x",
        "driver": "in_process", "steps": [], "invalid_tool_calls": [],
        "wall_clock_s": 1.0, "final_summary": "r",
        "prompt_tokens": 10, "completion_tokens": 5, "max_steps_reached": False,
        "step_count": 0, "usd": 0.0,
        "state_before": None, "state_after": None, "state_delta": None,
    }
    rec1 = {**base, "player_id": 1}
    rec3 = {**base, "player_id": 3}

    report = analyze([rec1, rec3], [])

    assert "by_player" in report, "result must use 'by_player' key, not 'by_model'"
    by_player = report["by_player"]
    assert len(by_player) == 2, (
        f"Expected 2 groups (one per seat), got {len(by_player)}: {list(by_player.keys())}"
    )
    assert 1 in by_player, "seat 1 must be a key"
    assert 3 in by_player, "seat 3 must be a key"
    # Labels must be present on each group
    assert by_player[1]["player_id"] == 1
    assert by_player[1]["model"] == "model-x"
    assert by_player[3]["player_id"] == 3
    assert by_player[3]["model"] == "model-x"


# ---------------------------------------------------------------------------
# Task-H3 — shared vocab coupling test
# ---------------------------------------------------------------------------

def test_local_tool_verbs_subset_of_registry():
    """All LOCAL_TOOL_VERBS keys must appear in registry TOOL_REGISTRY.

    A rename in either place without updating the other will surface here.
    FAILS before H3 (vocab.py does not exist yet).
    """
    from civ_mcp.arena.registry import TOOL_REGISTRY
    from civ_mcp.arena.vocab import LOCAL_TOOL_VERBS

    missing = set(LOCAL_TOOL_VERBS) - set(TOOL_REGISTRY)
    assert not missing, (
        f"LOCAL_TOOL_VERBS keys not in registry TOOL_REGISTRY: {missing!r}"
    )


def test_local_tool_verbs_mirror_registry_verbs_exactly() -> None:
    """LOCAL_TOOL_VERBS must be an exact mirror of the registry's ``verb`` fields.

    The registry is the single source of truth: each action tool declares its
    analysis verb via ``_tool(..., verb=...)``. vocab.LOCAL_TOOL_VERBS duplicates
    that map so analyze.py stays import-light. This test fails if the two drift —
    e.g. a new action tool declares a verb in the registry but nobody updates the
    vocab mirror (which would silently drop it from rubric coverage), or vice
    versa.
    """
    from civ_mcp.arena.registry import TOOL_REGISTRY
    from civ_mcp.arena.vocab import LOCAL_TOOL_VERBS

    registry_verbs = {
        name: tool.verb for name, tool in TOOL_REGISTRY.items() if tool.verb
    }

    assert LOCAL_TOOL_VERBS == registry_verbs
    # Sanity: the previously-missed exploration/skip actions are covered.
    assert registry_verbs["automate_explore"] == "automate"
    assert registry_verbs["skip_remaining_units"] == "skip"
    assert registry_verbs["respond_to_diplomacy"] == "respond_to_diplomacy"
    assert registry_verbs["respond_to_trade"] == "respond_to_trade"
    assert registry_verbs["propose_trade"] == "propose_trade"
    assert registry_verbs["propose_peace"] == "propose_peace"
    assert registry_verbs["send_diplomatic_action"] == "send_diplomatic_action"
    assert registry_verbs["form_alliance"] == "form_alliance"
    # Task 7 — behavior-critical action tools.
    assert registry_verbs["promote_governor"] == "promote_governor"
    assert registry_verbs["choose_dedication"] == "choose_dedication"
    assert registry_verbs["found_religion"] == "found_religion"
    assert registry_verbs["recruit_great_person"] == "recruit_great_person"
    assert registry_verbs["patronize_great_person"] == "patronize_great_person"
    assert registry_verbs["reject_great_person"] == "reject_great_person"
    assert registry_verbs["start_trade_route"] == "start_trade_route"
    assert registry_verbs["teleport_trader"] == "teleport_trader"
    assert registry_verbs["queue_wc_votes"] == "queue_wc_votes"
    assert registry_verbs["city_attack"] == "city_attack"
    assert registry_verbs["resolve_city_capture"] == "resolve_city_capture"


def test_rubric_counts_automate_explore_as_exploration() -> None:
    from civ_mcp.arena.analyze import analyze

    report = analyze(
        [
            {
                "player_id": 1,
                "model": "m",
                "provider": "local",
                "driver": "in_process",
                "turn": 1,
                "steps": [
                    {
                        "tool_name": "automate_explore",
                        "tool_args": {"unit_index": 2},
                        "tool_result_full": "OK",
                    }
                ],
                "invalid_tool_calls": [],
            }
        ],
        [],
    )

    assert report["by_player"][1]["rubric"]["explored_vs_idle"] is not None


def test_step_verb_covers_all_shared_vocab_entries():
    """_step_verb must map each LOCAL_TOOL_VERBS key to its verb value, and strip MCP_CIV6_PREFIX."""
    from civ_mcp.arena.analyze import _step_verb
    from civ_mcp.arena.vocab import LOCAL_TOOL_VERBS, MCP_CIV6_PREFIX

    # Local flat names
    for tool_name, expected_verb in LOCAL_TOOL_VERBS.items():
        base, verb = _step_verb({"tool_name": tool_name, "tool_args": {}})
        assert base == tool_name, f"tool_base wrong for {tool_name}: got {base!r}"
        assert verb == expected_verb, f"verb wrong for {tool_name}: got {verb!r}, expected {expected_verb!r}"

    # MCP-prefixed variant of a local tool (e.g. mcp__civ6__move_unit)
    base, verb = _step_verb({"tool_name": MCP_CIV6_PREFIX + "move_unit", "tool_args": {}})
    assert base == "move_unit"
    assert verb == "move"

    # CLI unit_action branch unchanged
    base, verb = _step_verb({"tool_name": MCP_CIV6_PREFIX + "unit_action",
                              "tool_args": {"action": "automate"}})
    assert base == "unit_action"
    assert verb == "automate"

    # MCP-prefixed variant of a Task 7 behavior-critical action tool.
    base, verb = _step_verb({"tool_name": MCP_CIV6_PREFIX + "city_attack", "tool_args": {}})
    assert base == "city_attack"
    assert verb == "city_attack"
