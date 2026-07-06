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


def test_load_memory_wrong_type_updated_turn_returns_none(tmp_path):
    path = memory_path(str(tmp_path), "run1", 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"schema_version": 1, "run_id": "run1", "player_id": 0, '
        '"updated_turn": "5", "text": "Keep marching."}'
    )

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


def test_extract_standing_plan_accepts_markdown_heading_forms():
    cases = [
        "**STANDING PLAN:**\n- keep scout moving\n",
        "**STANDING PLAN**:\n- keep scout moving\n",
        "- STANDING PLAN:\n- keep scout moving\n",
        "## STANDING PLAN:\n- keep scout moving\n",
    ]

    for summary in cases:
        assert extract_standing_plan(summary, max_chars=1200) == "keep scout moving"


def test_extract_standing_plan_stops_at_next_section_header():
    summary = (
        "STANDING PLAN:\n"
        "- Keep marching.\n"
        "STRATEGIC NOTES:\n"
        "- something unrelated\n"
    )

    result = extract_standing_plan(summary, max_chars=1200)

    assert result == "Keep marching."


def test_extract_standing_plan_stops_at_titlecase_known_unbulleted_header():
    summary = (
        "STANDING PLAN:\n"
        "- keep builder near copper\n"
        "Tactical:\n"
        "- unrelated reflection content\n"
    )

    result = extract_standing_plan(summary, max_chars=1200)

    assert result == "keep builder near copper"


def test_extract_standing_plan_stops_at_emphasized_known_unbulleted_header():
    summary = (
        "STANDING PLAN:\n"
        "- keep builder near copper\n"
        "**Tactical:**\n"
        "- unrelated reflection content\n"
    )

    result = extract_standing_plan(summary, max_chars=1200)

    assert result == "keep builder near copper"


def test_extract_standing_plan_stops_at_emphasized_planning_header_even_with_task_line():
    summary = (
        "STANDING PLAN:\n"
        "- keep scout moving\n"
        "**Planning:**\n"
        "- TASK settle unit_id=42 target=10,12\n"
    )

    assert extract_standing_plan(summary, max_chars=1200) == "keep scout moving"


def test_extract_standing_plan_absent_marker_returns_empty_string():
    summary = "TACTICAL: moved settler.\nSTRATEGIC: still ahead in score.\n"

    result = extract_standing_plan(summary, max_chars=1200)

    assert result == ""


def test_extract_standing_plan_clamps_to_max_chars():
    summary = "STANDING PLAN: " + ("x" * 5000)

    result = extract_standing_plan(summary, max_chars=1200)

    assert len(result) == 1200


def test_extract_standing_plan_keeps_all_caps_bullet_ending_colon():
    summary = (
        "STANDING PLAN:\n"
        "- BUILD CAMPUS:\n"
        "- TASK builder_improve unit_id=456 target=12,19 improvement=IMPROVEMENT_MINE\n"
        "TACTICAL:\n"
        "- unrelated next section\n"
    )

    result = extract_standing_plan(summary, max_chars=1200)

    assert result == (
        "BUILD CAMPUS:\n"
        "TASK builder_improve unit_id=456 target=12,19 improvement=IMPROVEMENT_MINE"
    )


def test_extract_standing_plan_stops_at_bulleted_reflection_header():
    summary = (
        "STANDING PLAN:\n"
        "- Keep settler 123 marching to (18,24).\n"
        "- TASK settle unit_id=123 target=18,24\n"
        "- TACTICAL:\n"
        "- Settler moved one tile this turn.\n"
    )

    result = extract_standing_plan(summary, max_chars=1200)

    assert result == (
        "Keep settler 123 marching to (18,24).\n"
        "TASK settle unit_id=123 target=18,24"
    )


def test_extract_standing_plan_stops_at_titlecase_bulleted_reflection_header():
    summary = (
        "STANDING PLAN:\n"
        "- Keep settler 123 marching to (18,24).\n"
        "- TASK settle unit_id=123 target=18,24\n"
        "- Tactical:\n"
        "- Settler moved one tile this turn.\n"
    )

    result = extract_standing_plan(summary, max_chars=1200)

    assert result == (
        "Keep settler 123 marching to (18,24).\n"
        "TASK settle unit_id=123 target=18,24"
    )


def test_extract_standing_plan_stops_at_lowercase_bulleted_reflection_header():
    summary = (
        "STANDING PLAN:\n"
        "- Keep settler 123 marching to (18,24).\n"
        "- TASK settle unit_id=123 target=18,24\n"
        "- tactical:\n"
        "- Settler moved one tile this turn.\n"
    )

    result = extract_standing_plan(summary, max_chars=1200)

    assert result == (
        "Keep settler 123 marching to (18,24).\n"
        "TASK settle unit_id=123 target=18,24"
    )


def test_extract_standing_plan_stops_at_bulleted_reflection_header_even_with_task_line():
    summary = (
        "STANDING PLAN:\n"
        "- Keep scout near city.\n"
        "- TACTICAL:\n"
        "- TASK settle unit_id=123 target=18,24\n"
    )

    result = extract_standing_plan(summary, max_chars=1200)

    assert result == "Keep scout near city."


def test_extract_standing_plan_keeps_reserved_bullet_heading_when_block_contains_task():
    summary = (
        "STANDING PLAN:\n"
        "- Planning:\n"
        "- TASK settle unit_id=123 target=18,24\n"
        "- Keep escort near the target.\n"
        "TACTICAL:\n"
        "- unrelated next section\n"
    )

    result = extract_standing_plan(summary, max_chars=1200)

    assert result == (
        "Planning:\n"
        "TASK settle unit_id=123 target=18,24\n"
        "Keep escort near the target."
    )


def test_extract_standing_plan_keeps_reserved_bullet_heading_when_block_contains_cancel():
    summary = (
        "STANDING PLAN:\n"
        "- Planning:\n"
        "- CANCEL unit_id=123\n"
        "- Keep escort near the target.\n"
        "TACTICAL:\n"
        "- unrelated next section\n"
    )

    result = extract_standing_plan(summary, max_chars=1200)

    assert result == (
        "Planning:\n"
        "CANCEL unit_id=123\n"
        "Keep escort near the target."
    )


def test_extract_standing_plan_ignores_unsupported_cancel_as_task_signal():
    summary = (
        "STANDING PLAN:\n"
        "- Keep scout near city.\n"
        "- Planning:\n"
        "- CANCEL settle unit_id=123 reason=site unsafe\n"
        "- Keep escort near the target.\n"
        "TACTICAL:\n"
        "- unrelated next section\n"
    )

    result = extract_standing_plan(summary, max_chars=1200)

    assert result == "Keep scout near city."


def test_format_memory_block_exact_heading():
    memory = StandingMemory(
        schema_version=1,
        run_id="run1",
        player_id=0,
        updated_turn=5,
        text="Keep marching.",
    )

    result = format_memory_block(memory)

    assert result == "== STANDING PLAN (captured turn 5) ==\nKeep marching."


def test_format_memory_block_surfaces_turn_age():
    memory = StandingMemory(
        schema_version=1,
        run_id="run1",
        player_id=0,
        updated_turn=5,
        text="Keep marching.",
    )

    result = format_memory_block(memory, current_turn=8)

    assert result == "== STANDING PLAN (captured turn 5, 3 turns old) ==\nKeep marching."


def test_format_memory_block_surfaces_one_turn_old():
    memory = StandingMemory(
        schema_version=1,
        run_id="run1",
        player_id=0,
        updated_turn=7,
        text="Keep marching.",
    )

    result = format_memory_block(memory, current_turn=8)

    assert result == "== STANDING PLAN (captured turn 7, 1 turn old) ==\nKeep marching."


def test_format_memory_block_omits_stale_memory_when_max_age_exceeded():
    memory = StandingMemory(
        schema_version=1,
        run_id="run1",
        player_id=0,
        updated_turn=5,
        text="Keep marching.",
    )

    result = format_memory_block(memory, current_turn=16, max_age_turns=10)

    assert result == ""


def test_format_memory_block_includes_memory_at_max_age_boundary():
    memory = StandingMemory(
        schema_version=1,
        run_id="run1",
        player_id=0,
        updated_turn=5,
        text="Keep marching.",
    )

    result = format_memory_block(memory, current_turn=15, max_age_turns=10)

    assert result == "== STANDING PLAN (captured turn 5, 10 turns old) ==\nKeep marching."


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
