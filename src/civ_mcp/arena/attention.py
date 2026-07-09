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
from dataclasses import dataclass

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
