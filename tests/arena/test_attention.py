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

def test_wrong_shaped_nested_fields_reset(tmp_path):
    p = attention_path(str(tmp_path), "r1", 3)
    p.parent.mkdir(parents=True)
    p.write_text(
        '{"schema_version": 1, "run_id": "r1", "player_id": 3, "directive": null,'
        ' "skips_remaining": 1, "streak": 1, "last_wake_turn": 4,'
        ' "last_snapshot": null, "last_scan": null, "slept": "abc", "directive_ack": ""}'
    )
    st = load_attention_state(str(tmp_path), "r1", 3)
    assert st.slept == [] and st.streak == 0  # fresh, not ['a','b','c']

def test_run_or_player_mismatch_resets(tmp_path):
    st = _seeded_state()
    save_attention_state(str(tmp_path), "r1", 3, st)
    # seeded state has skips_remaining=3 and a snapshot; fresh has neither
    mismatched_run = load_attention_state(str(tmp_path), "OTHER", 3)
    assert mismatched_run.skips_remaining == 0 and mismatched_run.last_snapshot is None
    mismatched_player = load_attention_state(str(tmp_path), "r1", 4)
    assert mismatched_player.skips_remaining == 0 and mismatched_player.last_snapshot is None


from civ_mcp.arena.attention import (
    AttentionScan,
    build_attention_query,
    parse_attention_scan,
    scan_scalars,
)

QUIET_LINES = [
    "ATTN|THREAT|count=0|nearest=",
    "ATTN|CITYHP|damaged=",
    "ATTN|WAR|with=",
    "ATTN|LOYALTY|negative=",
    "ATTN|WC|turns=5",
    "ATTN|ERA|index=1",
    "ATTN|POP|total=12",
    "ATTN|GP|available=0",
    "ATTN|TRADE|idle=0",
    "ATTN|DIPLO|pending=0",
    "ATTN|BLOCKERS|types=",
]


def test_parse_quiet_scan():
    scan = parse_attention_scan(QUIET_LINES)
    assert scan.hostile_count == 0 and scan.blocker_types == ()
    assert scan.at_war_with == () and scan.era_index == 1
    assert scan.failed_families == ()

def test_parse_busy_scan():
    lines = [
        "ATTN|THREAT|count=2|nearest=Barbarian Horseman d3 near Suwon",
        "ATTN|CITYHP|damaged=17,42",
        "ATTN|WAR|with=3",
        "ATTN|LOYALTY|negative=17",
        "ATTN|WC|turns=0",
        "ATTN|ERA|index=3",
        "ATTN|POP|total=23",
        "ATTN|GP|available=1",
        "ATTN|TRADE|idle=1",
        "ATTN|DIPLO|pending=1",
        "ATTN|BLOCKERS|types=NOTIFICATION_PRODUCTION,ENDTURN_BLOCKING_UNIT_PROMOTION",
        "ATTN|NOTIFY|type=NOTIFICATION_REBELLION|msg=Rebels near Pusan",
    ]
    scan = parse_attention_scan(lines)
    assert scan.hostile_count == 2 and "Horseman" in scan.nearest_hostile
    assert scan.damaged_city_ids == (17, 42) and scan.at_war_with == (3,)
    # promotion blocker filtered by BLOCKER_IGNORE
    assert scan.blocker_types == ("NOTIFICATION_PRODUCTION",)
    assert scan.notifications == (("NOTIFICATION_REBELLION", "Rebels near Pusan"),)

def test_parse_failed_family_flagged():
    scan = parse_attention_scan([*QUIET_LINES[:4], "ATTN_ERR|WC", *QUIET_LINES[5:]])
    assert "WC" in scan.failed_families

def test_parse_missing_family_flagged():
    scan = parse_attention_scan([l for l in QUIET_LINES if "ATTN|ERA" not in l])
    assert "ERA" in scan.failed_families

def test_parse_no_attn_lines_none():
    assert parse_attention_scan([]) is None
    assert parse_attention_scan(None) is None
    assert parse_attention_scan(["GARBAGE"]) is None

def test_build_query_int_casts():
    lua = build_attention_query("7", "4")  # str inputs must not splice raw
    assert "__PID__" not in lua and "__RADIUS__" not in lua
    assert " 7" in lua or "[7]" in lua or "(7)" in lua

def test_scan_scalars_shape():
    scan = parse_attention_scan(QUIET_LINES)
    assert scan_scalars(scan) == {"at_war_with": [], "era_index": 1, "total_population": 12}
