"""Attention & turn-skipping for arena LLM puppets (quiet-turn fast path).

Owns: SKIP:/WAKE IF: directive parsing, the per-turn trigger scan, the
persisted per-civ attention state, and the wake-digest accumulator/renderer.
Spec: docs/superpowers/specs/2026-07-09-arena-attention-turn-skipping-design.md

Philosophy: every failure here degrades toward MORE model turns, never more
blind skips. Directive unparseable -> no directive. State corrupt -> reset +
wake. Scan error or partial -> wake.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from civ_mcp.json_io import read_json_file, write_json_file_atomic

SOFT_TRIGGERS: tuple[str, ...] = (
    "GREAT_PERSON_AVAILABLE",
    "CITY_GREW",
    "TRADE_ROUTE_IDLE",
    "GOLD_STOCKPILE_HIGH",
)

# Tolerant line matchers, the STANDING_PLAN_RE lesson (memory.py): models
# reformat instructed plain markers into bullets/headings/emphasis, and a
# silent miss is a directive that never takes effect. The keyword must open
# the line (after markdown prefixes) so prose like "I will skip: nothing"
# never matches. Also imported by memory.extract_standing_plan as standing-
# plan TERMINATORS so a directive after the plan block is never persisted
# as plan text (external-review catch: the plan collector's header test
# requires a trailing colon, so "SKIP: 3" would otherwise be swallowed).
_DIRECTIVE_PREFIX = r"^\s*(?:[-*•]+\s+)?(?:#{1,6}\s*)?(?:[*_]{1,3})?\s*"
SKIP_LINE_RE = re.compile(
    _DIRECTIVE_PREFIX + r"skip\s*(?:[*_]{1,3})?\s*:\s*(?P<body>.*)$", re.IGNORECASE
)
WAKE_IF_LINE_RE = re.compile(
    _DIRECTIVE_PREFIX + r"wake\s+if\s*(?:[*_]{1,3})?\s*:\s*(?P<body>.*)$", re.IGNORECASE
)


@dataclass(frozen=True)
class Directive:
    skip: int
    wake_if: tuple[str, ...] = ()
    unknown_tokens: tuple[str, ...] = ()
    clamped: bool = False


def has_directive_lines(summary: str) -> bool:
    """True if any line looks like a directive attempt (valid or not)."""
    return any(
        SKIP_LINE_RE.match(line) or WAKE_IF_LINE_RE.match(line)
        for line in summary.splitlines()
    )


def parse_directive(summary: str, max_skip: int) -> Directive | None:
    """Extract a SKIP/WAKE IF directive from a final summary, or None.

    First parsed SKIP line wins; later SKIP lines never override it, and a
    SKIP line with no integer does not block a later parseable one. WAKE IF
    without SKIP is inert (spec: sleep must be freshly and explicitly
    chosen). Unknown WAKE IF tokens are collected, not fatal. SKIP body must
    contain an integer ("SKIP: 3 turns" tolerated).
    """
    skip: int | None = None
    clamped = False
    wake_if: list[str] = []
    unknown: list[str] = []
    for line in summary.splitlines():
        m = SKIP_LINE_RE.match(line)
        if m and skip is None:
            num = re.search(r"-?\d+", m.group("body"))
            if num:
                n = int(num.group())
                skip = min(max(n, 1), max_skip)
                clamped = skip != n
            continue
        m = WAKE_IF_LINE_RE.match(line)
        if m:
            for tok in re.split(r"[,\s]+", m.group("body")):
                token = tok.strip("`*_.").upper()
                if not token:
                    continue
                if token in SOFT_TRIGGERS:
                    if token not in wake_if:
                        wake_if.append(token)
                else:
                    unknown.append(token)
    if skip is None:
        return None
    return Directive(
        skip=skip, wake_if=tuple(wake_if), unknown_tokens=tuple(unknown), clamped=clamped
    )


SCHEMA_VERSION = 1
DIGEST_MAX_CHARS = 1200
DIGEST_MAX_NOTIFICATIONS = 10  # the gossip lesson: never an unbounded feed


@dataclass(frozen=True)
class AttentionState:
    """Per-civ persisted skip state. Corrupt file -> fresh state -> wake."""
    schema_version: int = SCHEMA_VERSION
    run_id: str = ""
    player_id: int = -1
    directive: dict | None = None      # {"skip": int, "wake_if": [...]} as issued
    skips_remaining: int = 0
    streak: int = 0                    # consecutive sleeps since last model turn
    last_wake_turn: int = -1
    last_snapshot: dict | None = None  # overview snapshot at previous captured turn
    last_scan: dict | None = None      # stored scalars: at_war_with/era_index/total_population
    slept: list = field(default_factory=list)   # digest accumulator, one dict per slept turn
    directive_ack: str = ""            # reported in the next wake digest


def attention_path(transcript_dir: str, run_id: str, player_id: int) -> Path:
    return Path(transcript_dir) / run_id / "attention" / f"player_{player_id}.json"


def load_attention_state(transcript_dir: str, run_id: str, player_id: int) -> AttentionState:
    data = read_json_file(attention_path(transcript_dir, run_id, player_id))
    fresh = AttentionState(run_id=run_id, player_id=player_id)
    if not isinstance(data, dict):
        return fresh
    try:
        for key in ("directive", "last_snapshot", "last_scan"):
            value = data.get(key)
            if value is not None and not isinstance(value, dict):
                raise TypeError(f"{key} must be a dict or null")
        slept = data.get("slept", [])
        if not isinstance(slept, list) or not all(isinstance(r, dict) for r in slept):
            raise TypeError("slept must be a list of dicts")
        st = AttentionState(
            schema_version=int(data["schema_version"]),
            run_id=str(data["run_id"]),
            player_id=int(data["player_id"]),
            directive=data.get("directive"),
            skips_remaining=int(data.get("skips_remaining", 0)),
            streak=int(data.get("streak", 0)),
            last_wake_turn=int(data.get("last_wake_turn", -1)),
            last_snapshot=data.get("last_snapshot"),
            last_scan=data.get("last_scan"),
            slept=list(slept),
            directive_ack=str(data.get("directive_ack", "")),
        )
    except (KeyError, TypeError, ValueError):
        return fresh
    if st.run_id != run_id or st.player_id != player_id:
        return fresh
    return st


def save_attention_state(
    transcript_dir: str, run_id: str, player_id: int, state: AttentionState
) -> None:
    payload = {
        "schema_version": state.schema_version,
        "run_id": state.run_id,
        "player_id": state.player_id,
        "directive": state.directive,
        "skips_remaining": state.skips_remaining,
        "streak": state.streak,
        "last_wake_turn": state.last_wake_turn,
        "last_snapshot": state.last_snapshot,
        "last_scan": state.last_scan,
        "slept": state.slept,
        "directive_ack": state.directive_ack,
    }
    write_json_file_atomic(attention_path(transcript_dir, run_id, player_id), payload)


def note_sleep(
    state: AttentionState, *, turn: int, snapshot: dict | None,
    scan_scalars: dict | None, task_notes: list, notifications: list,
) -> AttentionState:
    record = {
        "turn": turn,
        "snapshot": snapshot,
        "task_notes": list(task_notes),
        "notifications": [list(n) for n in notifications][:DIGEST_MAX_NOTIFICATIONS],
    }
    return replace(
        state,
        skips_remaining=max(0, state.skips_remaining - 1),
        streak=state.streak + 1,
        last_snapshot=snapshot if snapshot is not None else state.last_snapshot,
        last_scan=scan_scalars if scan_scalars is not None else state.last_scan,
        slept=[*state.slept, record],
    )


def note_wake(
    state: AttentionState, *, turn: int, wake_cause: str, directive: "Directive | None",
    directive_ack: str, snapshot: dict | None, scan_scalars: dict | None,
) -> AttentionState:
    # Any wake cancels the remainder (spec: sleep is always freshly chosen).
    new_directive = None
    remaining = 0
    if directive is not None:
        new_directive = {"skip": directive.skip, "wake_if": list(directive.wake_if)}
        remaining = directive.skip
    return replace(
        state,
        directive=new_directive,
        skips_remaining=remaining,
        streak=0,
        last_wake_turn=turn,
        last_snapshot=snapshot if snapshot is not None else state.last_snapshot,
        last_scan=scan_scalars if scan_scalars is not None else state.last_scan,
        slept=[],
        directive_ack=directive_ack,
    )


def render_digest(
    state: AttentionState, *, wake_turn: int, wake_cause: str, wake_detail: str
) -> str:
    """Priority order (spec section 4): wake cause, directive ack, accumulated
    deltas, tracker progress, notifications (newest first, capped).

    Must be called on the pre-wake state (while ``slept`` is still populated)
    — ``note_wake`` clears the accumulator.
    """
    if not state.slept:
        return ""
    first = state.slept[0]["turn"]
    last = state.slept[-1]["turn"]
    n = len(state.slept)
    lines = [f"== WHILE YOU SLEPT (turns {first}–{last}, {n} skipped) =="]
    cause = wake_cause + (f" — {wake_detail}" if wake_detail else "")
    lines.append(f"Woke because: {cause}")
    if state.directive_ack:
        lines.append(f"Your directive: {state.directive_ack}")
    snaps = [r["snapshot"] for r in state.slept if r.get("snapshot")]
    if snaps:
        first_s, last_s = snaps[0], snaps[-1]
        lines.append("Empire while asleep:")
        deltas = []
        for key in ("score", "gold", "science", "culture", "cities", "units"):
            a, b = first_s.get(key), last_s.get(key)
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                deltas.append(f"{key} {a}→{b}")
        if deltas:
            lines.append("- " + ", ".join(deltas))
    notes = [note for r in state.slept for note in r.get("task_notes", [])]
    if notes:
        lines.append("Tracker: " + "; ".join(notes[-5:]))
    tagged = [
        (rec["turn"], pair[1] if len(pair) > 1 else str(pair))
        for rec in state.slept
        for pair in rec.get("notifications", [])
    ]
    if tagged:
        lines.append(
            f"Notifications during sleep (newest first, max {DIGEST_MAX_NOTIFICATIONS}):"
        )
        for turn_no, msg in list(reversed(tagged))[:DIGEST_MAX_NOTIFICATIONS]:
            lines.append(f"- [T{turn_no}] {msg}")
    text = "\n".join(lines)
    return text[:DIGEST_MAX_CHARS].rstrip()
