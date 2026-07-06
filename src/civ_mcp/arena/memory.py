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
from dataclasses import dataclass
from pathlib import Path

from civ_mcp.json_io import read_json_file, write_json_file_atomic

SCHEMA_VERSION = 1

_STANDING_PLAN_RE = re.compile(r"^\s*standing plan:\s*(.*)$", re.IGNORECASE)
_BULLET_PREFIX_RE = re.compile(r"^\s*[-*•]+\s*")


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

    Finds a case-insensitive line starting with ``STANDING PLAN:`` and
    captures that line's trailing content plus following non-empty lines,
    stopping at a new ALL-CAPS section header (e.g. ``TACTICAL:``) or end of
    string. Left-edge markdown bullets are stripped per line. Returns ""
    when no standing plan marker is present.
    """
    lines = summary.splitlines()
    start_idx = None
    inline_content = ""
    for idx, line in enumerate(lines):
        match = _STANDING_PLAN_RE.match(line)
        if match:
            start_idx = idx
            inline_content = match.group(1).strip()
            break

    if start_idx is None:
        return ""

    collected: list[str] = []
    if inline_content:
        collected.append(_strip_bullet(inline_content))

    for line in lines[start_idx + 1 :]:
        if _is_section_header(line):
            break
        if line.strip() == "":
            # Blank lines are not a terminator per the brief; skip them so a
            # plan split across a blank gap keeps all its content.
            continue
        collected.append(_strip_bullet(line.strip()))

    return _clamp("\n".join(collected), max_chars)


def format_memory_block(
    memory: StandingMemory | None, *, current_turn: int | None = None
) -> str:
    """Render standing memory as a prompt-ready block, or "" if empty/absent."""
    if memory is None or not memory.text:
        return ""
    suffix = f"captured turn {memory.updated_turn}"
    if current_turn is not None:
        age = max(0, current_turn - memory.updated_turn)
        if age == 1:
            suffix += ", 1 turn old"
        elif age != 0:
            suffix += f", {age} turns old"
    return f"== STANDING PLAN ({suffix}) ==\n{memory.text}"


def _clamp(text: str, max_chars: int) -> str:
    # Strip, clamp to max_chars, then re-strip so a cut landing inside a
    # whitespace run can't leave a trailing-space artifact. Result is
    # guaranteed <= max_chars (rstrip only removes characters).
    return text.strip()[:max_chars].rstrip()


def _strip_bullet(line: str) -> str:
    return _BULLET_PREFIX_RE.sub("", line, count=1)


def _is_section_header(line: str) -> bool:
    stripped = line.strip()
    if _BULLET_PREFIX_RE.match(stripped):
        return False
    if not stripped.endswith(":"):
        return False
    body = stripped[:-1].strip()
    return bool(body) and body.isupper()
