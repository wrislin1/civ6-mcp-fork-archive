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


@pytest.mark.parametrize("body", [
    "hold until turn 340",
    "maybe in 3 if peaceful",
    "after 2 more builds",
])
def test_digit_bearing_prose_is_not_a_directive(body):
    """Review-2 finding 6: a stray digit inside prose must not become a
    max-clamped blind skip -- no leading integer means no directive (wake)."""
    assert parse_directive(f"all quiet.\nSKIP: {body}", 5) is None


@pytest.mark.parametrize("body,expected", [
    ("3", 3),
    ("3 turns", 3),
    ("**3**", 3),
    ("`2`", 2),
])
def test_leading_integer_still_parses(body, expected):
    d = parse_directive(f"all quiet.\nSKIP: {body}", 5)
    assert d is not None and d.skip == expected


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
    NOTIFICATION_WAKE_LIST,
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


def _lua_family_segment(query: str, name: str, next_name: str) -> str:
    start = query.index(f'fam("{name}"')
    end = query.index(f'fam("{next_name}"')
    return query[start:end]


def test_hard_family_lua_propagates_errors():
    """Review-2 findings 3+4: CITYHP and LOYALTY must not swallow API errors
    in inner pcalls -- a failure has to reach fam()'s pcall so the family
    reports ATTN_ERR and evaluate() wakes on SCAN_PARTIAL."""
    q = build_attention_query(1, 4)
    assert "pcall" not in _lua_family_segment(q, "CITYHP", "LOYALTY")
    assert "pcall" not in _lua_family_segment(q, "LOYALTY", "WC")


def test_attention_query_embeds_wake_list_priority():
    """Review-2 finding 5: NOTIFY must emit wake-list types first so they can
    never be truncated out by the 10-line cap (SPY_CAUGHT has no redundant
    trigger family)."""
    q = build_attention_query(1, 4)
    for name in NOTIFICATION_WAKE_LIST:
        assert f'["{name}"]=true' in q
    assert "__WAKELIST__" not in q


from civ_mcp.arena.attention import Decision, evaluate

QUIET = parse_attention_scan(QUIET_LINES)
SNAP = {"score": 100, "gold": 200, "units": 4, "cities": 2}


def _st(**kw):
    base = dict(run_id="r", player_id=1, last_snapshot=dict(SNAP),
                last_scan={"at_war_with": [], "era_index": 1, "total_population": 12})
    base.update(kw)
    return AttentionState(**base)


# (mode, state kwargs, scan, snapshot, task_event) -> (action, wake_cause)
MATRIX = [
    # quiet world, no directive
    ("auto",   {},                                        QUIET, SNAP, False, "sleep", None),
    ("model",  {},                                        QUIET, SNAP, False, "wake", "NO_DIRECTIVE"),
    ("hybrid", {},                                        QUIET, SNAP, False, "sleep", None),
    # quiet world, active directive
    ("model",  {"skips_remaining": 2, "directive": {"skip": 3, "wake_if": []}},
                                                          QUIET, SNAP, False, "sleep", None),
    # streak cap beats everything quiet
    ("auto",   {"streak": 5},                             QUIET, SNAP, False, "wake", "STREAK_CAP"),
    # scan/baseline failures
    ("auto",   {},                                        None,  SNAP, False, "wake", "SCAN_ERROR"),
    ("auto",   {"last_snapshot": None, "last_scan": None}, QUIET, SNAP, False, "wake", "NO_BASELINE"),
    # task event is a hard wake in every mode (external-review finding 6)
    ("auto",   {},                                        QUIET, SNAP, True,  "wake", "TASK_EVENT"),
    ("hybrid", {"skips_remaining": 3, "directive": {"skip": 3, "wake_if": []}},
                                                          QUIET, SNAP, True,  "wake", "TASK_EVENT"),
]


@pytest.mark.parametrize("mode,st_kw,scan,snap,task_event,action,cause", MATRIX)
def test_skip_decision_matrix(mode, st_kw, scan, snap, task_event, action, cause):
    d = evaluate(mode, _st(**st_kw), scan, snap, max_streak=5, task_event=task_event)
    assert (d.action, d.wake_cause) == (action, cause)


def test_hard_triggers_fire():
    busy = parse_attention_scan([
        "ATTN|THREAT|count=1|nearest=Warrior d2 near Suwon", *QUIET_LINES[1:],
    ])
    d = evaluate("auto", _st(), busy, SNAP, max_streak=5, task_event=False)
    assert d.action == "wake" and d.wake_cause == "ENEMY_NEAR"
    assert "Suwon" in d.wake_detail

def test_units_lost_delta_wakes():
    d = evaluate("auto", _st(), QUIET, {**SNAP, "units": 3}, max_streak=5, task_event=False)
    assert d.wake_cause == "UNITS_LOST"

def test_gold_crash_projection():
    st = _st(last_snapshot={**SNAP, "gold": 100})
    d = evaluate("auto", st, QUIET, {**SNAP, "gold": 60}, max_streak=5, task_event=False)
    assert d.wake_cause == "GOLD_CRASH"  # 60 + 5*(-40) < 0

def test_scan_partial_wakes():
    partial = parse_attention_scan([*QUIET_LINES[1:], "ATTN_ERR|THREAT"])
    d = evaluate("auto", _st(), partial, SNAP, max_streak=5, task_event=False)
    assert d.wake_cause == "SCAN_PARTIAL"

def test_soft_trigger_requires_subscription():
    grown = parse_attention_scan([l.replace("total=12", "total=13") for l in QUIET_LINES])
    st_sub = _st(skips_remaining=2, directive={"skip": 3, "wake_if": ["CITY_GREW"]})
    st_nosub = _st(skips_remaining=2, directive={"skip": 3, "wake_if": []})
    assert evaluate("hybrid", st_sub, grown, SNAP, max_streak=5, task_event=False).wake_cause == "CITY_GREW"
    assert evaluate("hybrid", st_nosub, grown, SNAP, max_streak=5, task_event=False).action == "sleep"

def test_soft_triggers_ignored_in_auto():
    grown = parse_attention_scan([l.replace("total=12", "total=13") for l in QUIET_LINES])
    st = _st(skips_remaining=2, directive={"skip": 3, "wake_if": ["CITY_GREW"]})
    assert evaluate("auto", st, grown, SNAP, max_streak=5, task_event=False).action == "sleep"

def test_blocker_wakes_with_type_name():
    blocked = parse_attention_scan([
        *QUIET_LINES[:10], "ATTN|BLOCKERS|types=NOTIFICATION_PRODUCTION",
    ])
    d = evaluate("auto", _st(), blocked, SNAP, max_streak=5, task_event=False)
    assert d.wake_cause == "BLOCKER_NOTIFICATION_PRODUCTION"

def test_notification_wake_list():
    noisy = parse_attention_scan([
        *QUIET_LINES, "ATTN|NOTIFY|type=NOTIFICATION_REBELLION|msg=Rebels!",
    ])
    d = evaluate("auto", _st(), noisy, SNAP, max_streak=5, task_event=False)
    assert d.wake_cause == "NOTIFICATION_WAKE"

def test_wake_detail_not_misattributed():
    # ENEMY_NEAR fires alongside a higher-priority trigger: detail must not
    # bleed onto the winning cause (review catch)
    busy = parse_attention_scan([
        "ATTN|THREAT|count=1|nearest=Warrior d2 near Suwon", *QUIET_LINES[1:],
    ])
    d = evaluate("auto", _st(), busy, SNAP, max_streak=5, task_event=True)
    assert d.wake_cause == "TASK_EVENT" and d.wake_detail == ""
    assert "TASK_EVENT" in d.hard and "ENEMY_NEAR" in d.hard


def test_cancel_remainder_keeps_digest_and_streak():
    """Final-review Important 2 helper: cancelling a stale directive must zero
    only skips_remaining -- the slept accumulator (digest) and streak (its cap
    keeps bounding model-free turns) survive, and the issued-directive record
    stays for the ack/metrics trail."""
    from civ_mcp.arena.attention import cancel_remainder

    st = _st(directive={"skip": 3, "wake_if": []}, skips_remaining=3)
    st = note_sleep(st, turn=45, snapshot=st.last_snapshot, scan_scalars=st.last_scan,
                    task_notes=[], notifications=[])
    assert st.skips_remaining == 2 and st.streak == 1
    st = cancel_remainder(st)
    assert st.skips_remaining == 0
    assert len(st.slept) == 1 and st.streak == 1
    assert st.directive == {"skip": 3, "wake_if": []}


# Final-review triage (T8): dedicated true-condition tests for every hard
# trigger branch that only had matrix/false-side coverage.
HARD_TRUE_CONDITIONS = [
    ("ATTN|CITYHP|damaged=", "ATTN|CITYHP|damaged=17", "CITY_DAMAGED"),
    ("ATTN|WAR|with=", "ATTN|WAR|with=3", "WAR_PEACE_CHANGED"),
    ("ATTN|LOYALTY|negative=", "ATTN|LOYALTY|negative=17", "LOYALTY_NEGATIVE"),
    ("ATTN|WC|turns=5", "ATTN|WC|turns=0", "WC_SESSION"),
    ("ATTN|ERA|index=1", "ATTN|ERA|index=2", "ERA_CHANGED"),
    ("ATTN|DIPLO|pending=0", "ATTN|DIPLO|pending=1", "BLOCKER_DIPLOMACY_SESSION"),
]


@pytest.mark.parametrize("old,new,cause", HARD_TRUE_CONDITIONS)
def test_hard_trigger_true_conditions(old, new, cause):
    scan = parse_attention_scan([l.replace(old, new) for l in QUIET_LINES])
    d = evaluate("auto", _st(), scan, SNAP, max_streak=5, task_event=False)
    assert (d.action, d.wake_cause) == ("wake", cause)


def test_city_count_changed_wakes():
    d = evaluate("auto", _st(), QUIET, {**SNAP, "cities": 3}, max_streak=5, task_event=False)
    assert d.wake_cause == "CITY_COUNT_CHANGED"


def test_peace_direction_also_wakes():
    # WAR_PEACE_CHANGED is a set inequality: leaving a war wakes too
    st = _st(last_scan={"at_war_with": [3], "era_index": 1, "total_population": 12})
    d = evaluate("auto", st, QUIET, SNAP, max_streak=5, task_event=False)
    assert d.wake_cause == "WAR_PEACE_CHANGED"


SOFT_TRUE_CONDITIONS = [
    ("ATTN|GP|available=0", "ATTN|GP|available=1", "GREAT_PERSON_AVAILABLE", SNAP),
    ("ATTN|TRADE|idle=0", "ATTN|TRADE|idle=1", "TRADE_ROUTE_IDLE", SNAP),
    (None, None, "GOLD_STOCKPILE_HIGH", {**SNAP, "gold": 600}),
]


@pytest.mark.parametrize("old,new,token,snap", SOFT_TRUE_CONDITIONS)
def test_soft_trigger_true_conditions(old, new, token, snap):
    lines = QUIET_LINES if old is None else [l.replace(old, new) for l in QUIET_LINES]
    scan = parse_attention_scan(lines)
    st = _st(skips_remaining=2, directive={"skip": 3, "wake_if": [token]})
    d = evaluate("hybrid", st, scan, snap, max_streak=5, task_event=False)
    assert (d.action, d.wake_cause) == ("wake", token)
    assert token in d.soft
