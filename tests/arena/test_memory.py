from civ_mcp.arena.memory import (
    StandingMemory,
    extract_standing_plan,
    format_memory_block,
    load_memory,
    memory_path,
    save_memory,
)


def test_load_memory_missing_file_returns_none(tmp_path):
    assert load_memory(str(tmp_path), "run1", 0) is None


def test_load_memory_malformed_json_returns_none(tmp_path):
    path = memory_path(str(tmp_path), "run1", 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json")

    assert load_memory(str(tmp_path), "run1", 0) is None


def test_load_memory_malformed_structure_returns_none(tmp_path):
    path = memory_path(str(tmp_path), "run1", 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('["unexpected", "list", "shape"]')

    assert load_memory(str(tmp_path), "run1", 0) is None


def test_save_then_load_round_trip(tmp_path):
    saved = save_memory(str(tmp_path), "run1", 2, turn=15, text="Keep building settlers.", max_chars=1200)

    loaded = load_memory(str(tmp_path), "run1", 2)

    assert loaded == saved
    assert loaded == StandingMemory(
        schema_version=1,
        run_id="run1",
        player_id=2,
        updated_turn=15,
        text="Keep building settlers.",
    )


def test_save_memory_writes_expected_path(tmp_path):
    save_memory(str(tmp_path), "run1", 3, turn=1, text="hi", max_chars=100)

    expected = tmp_path / "run1" / "memory" / "player_3.json"
    assert expected.exists()


def test_save_memory_clamps_text_to_max_chars(tmp_path):
    long_text = "x" * 5000

    saved = save_memory(str(tmp_path), "run1", 0, turn=1, text=long_text, max_chars=1200)

    assert len(saved.text) == 1200
    loaded = load_memory(str(tmp_path), "run1", 0)
    assert len(loaded.text) == 1200


def test_save_memory_no_whitespace_when_truncation_lands_on_whitespace_run(tmp_path):
    # Cut at max_chars=5 lands inside the run of spaces after "abc", which a
    # bare slice would leave as a trailing-space artifact.
    saved = save_memory(str(tmp_path), "run1", 0, turn=1, text="abc     def", max_chars=5)

    assert saved.text == "abc"
    assert saved.text == saved.text.strip()
    assert len(saved.text) <= 5


def test_save_memory_strips_leading_trailing_whitespace(tmp_path):
    saved = save_memory(str(tmp_path), "run1", 0, turn=1, text="   hello there   ", max_chars=100)

    assert saved.text == "hello there"


def test_extract_standing_plan_multiline_block():
    summary = (
        "TACTICAL: moved settler.\n"
        "STANDING PLAN:\n"
        "- Keep settler 123 marching to (18,24).\n"
        "- TASK settle unit_id=123 target=18,24\n"
    )

    result = extract_standing_plan(summary, max_chars=1200)

    assert result == (
        "Keep settler 123 marching to (18,24).\n"
        "TASK settle unit_id=123 target=18,24"
    )


def test_extract_standing_plan_preserves_content_across_internal_blank_line():
    summary = (
        "STANDING PLAN:\n"
        "- Keep marching.\n"
        "\n"
        "- Also queue a settler once loyalty is safe.\n"
    )

    result = extract_standing_plan(summary, max_chars=1200)

    assert result == (
        "Keep marching.\n"
        "Also queue a settler once loyalty is safe."
    )


def test_extract_standing_plan_inline():
    summary = "Standing Plan: finish archer movement, then settle unit_id=123 at 18,24."

    result = extract_standing_plan(summary, max_chars=1200)

    assert result == "finish archer movement, then settle unit_id=123 at 18,24."


def test_extract_standing_plan_stops_at_next_section_header():
    summary = (
        "STANDING PLAN:\n"
        "- Keep marching.\n"
        "STRATEGIC NOTES:\n"
        "- something unrelated\n"
    )

    result = extract_standing_plan(summary, max_chars=1200)

    assert result == "Keep marching."


def test_extract_standing_plan_absent_marker_returns_empty_string():
    summary = "TACTICAL: moved settler.\nSTRATEGIC: still ahead in score.\n"

    result = extract_standing_plan(summary, max_chars=1200)

    assert result == ""


def test_extract_standing_plan_clamps_to_max_chars():
    summary = "STANDING PLAN: " + ("x" * 5000)

    result = extract_standing_plan(summary, max_chars=1200)

    assert len(result) == 1200


def test_format_memory_block_exact_heading():
    memory = StandingMemory(
        schema_version=1,
        run_id="run1",
        player_id=0,
        updated_turn=5,
        text="Keep marching.",
    )

    result = format_memory_block(memory)

    assert result == "== STANDING PLAN FROM LAST TURN ==\nKeep marching."


def test_format_memory_block_returns_empty_for_none():
    assert format_memory_block(None) == ""


def test_format_memory_block_returns_empty_for_empty_text():
    memory = StandingMemory(
        schema_version=1,
        run_id="run1",
        player_id=0,
        updated_turn=5,
        text="",
    )

    assert format_memory_block(memory) == ""
