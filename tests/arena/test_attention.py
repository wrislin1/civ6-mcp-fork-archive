import pytest

from civ_mcp.arena.attention import (
    Directive,
    has_directive_lines,
    parse_directive,
)


def test_plain_directive():
    d = parse_directive("done.\nSKIP: 3\nWAKE IF: GREAT_PERSON_AVAILABLE, CITY_GREW", 5)
    assert d == Directive(skip=3, wake_if=("GREAT_PERSON_AVAILABLE", "CITY_GREW"),
                          unknown_tokens=(), clamped=False)

def test_markdown_variants():
    # models reformat plain markers into markdown (the memory.py lesson)
    for text in ("**SKIP:** 2", "- SKIP: 2", "## SKIP: 2", "*skip*: 2 turns"):
        d = parse_directive(text, 5)
        assert d is not None and d.skip == 2, text

def test_clamping():
    assert parse_directive("SKIP: 99", 5) == Directive(5, (), (), True)
    assert parse_directive("SKIP: 0", 5) == Directive(1, (), (), True)
    assert parse_directive("SKIP: 2", 3).skip == 2

def test_wake_if_without_skip_is_inert():
    assert parse_directive("WAKE IF: CITY_GREW", 5) is None
    assert has_directive_lines("WAKE IF: CITY_GREW")  # ack loop can say "not recognized"

def test_unknown_tokens_dropped_not_fatal():
    d = parse_directive("SKIP: 2\nWAKE IF: CITY_GREW, SCIENCE_OVER_200", 5)
    assert d.wake_if == ("CITY_GREW",)
    assert d.unknown_tokens == ("SCIENCE_OVER_200",)

def test_garbage_no_directive():
    assert parse_directive("SKIP: soon-ish, when quiet", 5) is None
    assert parse_directive("nothing here", 5) is None
    assert not has_directive_lines("nothing here")

def test_prose_mentioning_skip_mid_sentence_does_not_match():
    assert parse_directive("I will skip: nothing important this turn happened", 5) is None

def test_first_parsed_skip_wins():
    # two valid SKIP lines: the first parsed one wins
    assert parse_directive("SKIP: 2\nSKIP: 4", 5).skip == 2
    # a SKIP line with no integer does not block a later parseable one
    assert parse_directive("SKIP: soon\nSKIP: 4", 5).skip == 4


from civ_mcp.arena.attention import (
    AttentionState,
    attention_path,
    load_attention_state,
    note_sleep,
    note_wake,
    render_digest,
    save_attention_state,
)


def _seeded_state():
    return AttentionState(
        run_id="r1", player_id=3, directive={"skip": 3, "wake_if": []},
        skips_remaining=3, streak=0, last_wake_turn=44,
        last_snapshot={"score": 100, "gold": 50, "units": 4, "cities": 2},
        last_scan={"at_war_with": [], "era_index": 1, "total_population": 8},
    )


def test_state_round_trip(tmp_path):
    st = _seeded_state()
    save_attention_state(str(tmp_path), "r1", 3, st)
    assert load_attention_state(str(tmp_path), "r1", 3) == st

def test_corrupt_state_resets(tmp_path):
    p = attention_path(str(tmp_path), "r1", 3)
    p.parent.mkdir(parents=True)
    p.write_text("{not json")
    st = load_attention_state(str(tmp_path), "r1", 3)
    assert st.streak == 0 and st.skips_remaining == 0 and st.last_snapshot is None

def test_note_sleep_accumulates_and_decrements():
    st = _seeded_state()
    st = note_sleep(st, turn=45, snapshot={"score": 104, "gold": 60, "units": 4, "cities": 2},
                    scan_scalars={"at_war_with": [], "era_index": 1, "total_population": 8},
                    task_notes=[], notifications=[("NOTIFICATION_X", "border expanded")])
    assert st.skips_remaining == 2 and st.streak == 1
    assert len(st.slept) == 1 and st.slept[0]["turn"] == 45
    assert st.last_snapshot["score"] == 104  # baseline advances every slept turn

def test_note_wake_cancels_remainder_and_clears_digest():
    st = _seeded_state()
    st = note_sleep(st, turn=45, snapshot=st.last_snapshot, scan_scalars=st.last_scan,
                    task_notes=[], notifications=[])
    st = note_wake(st, turn=46, wake_cause="ENEMY_NEAR", directive=None,
                   directive_ack="woken early by ENEMY_NEAR after 1 of 3",
                   snapshot={"score": 105}, scan_scalars={"era_index": 1})
    assert st.skips_remaining == 0 and st.streak == 0 and st.slept == []
    assert st.last_wake_turn == 46

def test_render_digest_contents_and_cap():
    st = _seeded_state()
    st = note_sleep(st, turn=45, snapshot={"score": 104, "gold": 60, "units": 4, "cities": 2},
                    scan_scalars=st.last_scan, task_notes=["settler advanced"],
                    notifications=[("NOTIFICATION_X", "Suwon border expanded")])
    text = render_digest(st, wake_turn=46, wake_cause="STREAK_CAP", wake_detail="")
    assert text.startswith("== WHILE YOU SLEPT")
    assert "STREAK_CAP" in text and "Suwon border expanded" in text
    assert len(text) <= 1200

def test_render_digest_empty_without_sleeps():
    assert render_digest(_seeded_state(), wake_turn=45, wake_cause="", wake_detail="") == ""
