"""Run-local standing memory for arena LLM puppets.

A puppet's final-turn summary can end with a short "STANDING PLAN:" block
describing what it intends to do next. This module extracts that block,
persists it per-run/per-player, and formats it back into a prompt block for
the puppet's next turn -- giving stateless LLM puppets a thin thread of
cross-turn continuity.

Deliberately independent of ``civ_mcp.diary``: diary is the durable,
cross-session memory a human/live-game player builds up; standing memory is
scoped to a single arena run (``<transcript_dir>/<run_id>/memory/``) and must
never leak across runs.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from civ_mcp.arena.attention import SKIP_LINE_RE, WAKE_IF_LINE_RE
from civ_mcp.arena.task_tracker import CANCEL_LINE_RE, TASK_LINE_RE
from civ_mcp.json_io import read_json_file, write_json_file_atomic

SCHEMA_VERSION = 1

# Public: cli_agent's summary clamp reuses this matcher (via
# find_standing_plan_start) so "where does the plan start" can never drift
# between extraction and clamping -- the two copies diverged once already.
#
# Two shapes match: a colon form with optional inline content ("STANDING
# PLAN: settle east", "**STANDING PLAN:**", "STANDING PLAN (next 3 turns):"),
# and a bare header alone on its line ("## STANDING PLAN", "**STANDING
# PLAN**") -- models reformat the instructed "STANDING PLAN:" into markdown
# headings, and missing those silently freezes memory on a stale plan. The
# bare form requires end-of-line so prose mentioning a standing plan
# mid-sentence never matches, and the strict prefix (bullets/#/emphasis only)
# keeps format_memory_block's injected "== STANDING PLAN (...) ==" header
# from re-matching on capture.
STANDING_PLAN_RE = re.compile(
    r"^\s*(?:[-*\u2022]+\s+)?(?:#{1,6}\s*)?(?:[*_]{1,3})?\s*standing plan"
    r"(?:\s*\([^)]*\))?\s*"
    r"(?:(?::\s*(?:[*_]{1,3})?|(?:[*_]{1,3})\s*:)\s*(?P<content>.*)"
    r"|(?:[*_]{1,3})?\s*$)",
    re.IGNORECASE,
)
_BULLET_PREFIX_RE = re.compile(r"^\s*[-*\u2022]+\s+")
_HEADING_PREFIX_RE = re.compile(r"^\s*(?:[-*\u2022]+\s+)?(?:#{1,6}\s*)?")
_HEADING_EMPHASIS_RE = re.compile(r"^(?:[*_]{1,3})?(.*?)(?:[*_]{1,3})?$")
_BULLETED_SECTION_HEADERS = frozenset(
    {
        "TACTICAL",
        "STRATEGIC",
        "TOOLING",
        "PLANNING",
        "HYPOTHESIS",
        "STRATEGIC NOTES",
    }
)
_TASK_AWARE_BULLETED_PLAN_SUBHEADINGS = frozenset({"PLANNING"})


@dataclass(frozen=True)
class StandingMemory:
    schema_version: int
    run_id: str
    player_id: int
    updated_turn: int
    text: str


def run_dir(transcript_dir: str, run_id: str) -> Path:
    return Path(transcript_dir) / run_id


def memory_path(transcript_dir: str, run_id: str, player_id: int) -> Path:
    return run_dir(transcript_dir, run_id) / "memory" / f"player_{player_id}.json"


def load_memory(transcript_dir: str, run_id: str, player_id: int) -> StandingMemory | None:
    """Load standing memory for a player, or None if absent/unreadable/malformed."""
    data = read_json_file(memory_path(transcript_dir, run_id, player_id))
    if not isinstance(data, dict):
        return None
    try:
        schema_version = data["schema_version"]
        saved_run_id = data["run_id"]
        saved_player_id = data["player_id"]
        updated_turn = data["updated_turn"]
        text = data["text"]
    except KeyError:
        return None
    if (
        type(schema_version) is not int
        or type(saved_run_id) is not str
        or type(saved_player_id) is not int
        or type(updated_turn) is not int
        or type(text) is not str
    ):
        return None
    return StandingMemory(
        schema_version=schema_version,
        run_id=saved_run_id,
        player_id=saved_player_id,
        updated_turn=updated_turn,
        text=text,
    )


def save_memory(
    transcript_dir: str,
    run_id: str,
    player_id: int,
    turn: int,
    text: str,
    max_chars: int,
) -> StandingMemory:
    """Persist standing memory for a player, clamped to max_chars, and return it.

    Write is atomic best-effort: a sibling temp file is written first, then
    swapped into place via Path.replace().
    """
    memory = StandingMemory(
        schema_version=SCHEMA_VERSION,
        run_id=run_id,
        player_id=player_id,
        updated_turn=turn,
        text=_clamp(text, max_chars),
    )
    path = memory_path(transcript_dir, run_id, player_id)
    payload = {
        "schema_version": memory.schema_version,
        "run_id": memory.run_id,
        "player_id": memory.player_id,
        "updated_turn": memory.updated_turn,
        "text": memory.text,
    }
    write_json_file_atomic(path, payload)
    return memory


def extract_standing_plan(summary: str, max_chars: int) -> str:
    """Extract a "STANDING PLAN:" block from a puppet's final-turn summary.

    Finds a case-insensitive ``STANDING PLAN`` marker, including common
    markdown heading, bullet, and emphasis forms (with or without a trailing
    colon, and with an optional parenthetical like "(next 3 turns)"), and
    captures that line's trailing content plus following non-empty lines.

    Inline content on the marker line requires a colon ("STANDING PLAN:
    settle east"); the colon-less forms are bare headers whose content must
    begin on the following line, so a no-colon marker with same-line prose is
    intentionally not treated as a plan (see
    test_bare_marker_requires_end_of_line_so_prose_does_not_match).

    Stops at an attention-directive line (SKIP: or WAKE IF:), an unbulleted
    known reflection header (case-insensitive), an unbulleted ALL-CAPS section
    header, a known bulleted reflection header such as ``- TACTICAL:``, or end
    of string. Left-edge markdown bullets are stripped per line. Returns ""
    when no standing plan marker is present.
    """
    lines = summary.splitlines()
    start_idx = None
    inline_content = ""
    for idx, line in enumerate(lines):
        match = STANDING_PLAN_RE.match(line)
        if match:
            start_idx = idx
            inline_content = (match.group("content") or "").strip()
            break

    if start_idx is None:
        return ""

    collected: list[str] = []
    if inline_content:
        collected.append(_strip_bullet(inline_content))

    following = lines[start_idx + 1 :]
    for offset, line in enumerate(following):
        # Attention directives (SKIP:/WAKE IF:) terminate the plan and are
        # never plan content -- without this they'd be persisted and
        # re-injected every turn (external-review catch).
        if SKIP_LINE_RE.match(line) or WAKE_IF_LINE_RE.match(line):
            break
        if _is_section_header(line, following[offset + 1 :]):
            break
        if line.strip() == "":
            # Blank lines are not a terminator per the brief; skip them so a
            # plan split across a blank gap keeps all its content.
            continue
        collected.append(_strip_bullet(line.strip()))

    return _clamp("\n".join(collected), max_chars)


def format_memory_block(
    memory: StandingMemory | None,
    *,
    current_turn: int | None = None,
    max_age_turns: int | None = None,
) -> str:
    """Render standing memory as a prompt-ready block, or "" if empty/absent/stale."""
    if memory is None or not memory.text:
        return ""
    age: int | None = None
    if current_turn is not None:
        age = current_turn - memory.updated_turn
        if age < 0:
            # Future-dated: an earlier save was reloaded, so this plan belongs
            # to an abandoned timeline. Never inject it as fresh.
            return ""
        if max_age_turns is not None and age > max_age_turns:
            return ""

    suffix = f"captured turn {memory.updated_turn}"
    if age is not None:
        if age == 1:
            suffix += ", 1 turn old"
        elif age != 0:
            suffix += f", {age} turns old"
    return f"== STANDING PLAN ({suffix}) ==\n{memory.text}"


def find_standing_plan_start(text: str) -> int:
    """Char offset of the first standing-plan header line in text, or -1."""
    offset = 0
    for line in text.splitlines(keepends=True):
        if STANDING_PLAN_RE.match(line):
            return offset
        offset += len(line)
    return -1


def _clamp(text: str, max_chars: int) -> str:
    # Strip, clamp to max_chars, then re-strip so a cut landing inside a
    # whitespace run can't leave a trailing-space artifact. Result is
    # guaranteed <= max_chars (rstrip only removes characters).
    return text.strip()[:max_chars].rstrip()


def _strip_bullet(line: str) -> str:
    return _BULLET_PREFIX_RE.sub("", line, count=1)


def _has_task_line_before_next_header(lines: Sequence[str]) -> bool:
    for line in lines:
        if line.strip() == "":
            continue
        # Only a line the task parser would actually accept defers the
        # terminator; prose that merely starts with "Task" must not.
        if TASK_LINE_RE.match(line) or CANCEL_LINE_RE.match(line):
            return True
        if _is_section_header(line):
            return False
    return False


def _header_body(line: str) -> tuple[str, bool]:
    stripped = line.strip()
    bullet = _BULLET_PREFIX_RE.match(stripped) is not None
    candidate = _HEADING_PREFIX_RE.sub("", stripped, count=1).strip()
    if not candidate.endswith(":"):
        emphasis = _HEADING_EMPHASIS_RE.match(candidate)
        if emphasis:
            candidate = emphasis.group(1).strip()
        if not candidate.endswith(":"):
            return "", bullet
    body = candidate[:-1].strip()
    emphasis = _HEADING_EMPHASIS_RE.match(body)
    if emphasis:
        body = emphasis.group(1).strip()
    return body, bullet


def _is_section_header(line: str, following_lines: Sequence[str] = ()) -> bool:
    body, bullet = _header_body(line)
    if not body:
        return False

    header = body.upper()
    if bullet:
        # Bulleted lines terminate ONLY on a known reflection header. An arbitrary
        # all-caps imperative bullet like "- BUILD CAMPUS:" is legitimate plan
        # content and must be kept (test_extract_standing_plan_keeps_all_caps_
        # bullet_ending_colon). PLANNING is task-aware: it is only a terminator when
        # no TASK/CANCEL line follows before the next header.
        if header in _TASK_AWARE_BULLETED_PLAN_SUBHEADINGS:
            return not _has_task_line_before_next_header(following_lines)
        return header in _BULLETED_SECTION_HEADERS

    # Unbulleted: a known reflection header (matched case-insensitively, so title-case
    # "Tactical:" terminates) or any all-caps line ("STRATEGIC NOTES:") terminates.
    # PLANNING stays task-aware in unbulleted form too: models emit TASK lines under
    # a "Planning:" subheading, and cutting there would silently drop them.
    if header in _TASK_AWARE_BULLETED_PLAN_SUBHEADINGS:
        return not _has_task_line_before_next_header(following_lines)
    return header in _BULLETED_SECTION_HEADERS or body.isupper()
