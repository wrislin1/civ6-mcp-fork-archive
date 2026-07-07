"""Pure-offline analysis of an arena run.

Reads transcript.jsonl + arena_cost.jsonl and produces:
  - report.json  — structured per-model series, rates, rubric
  - report.md    — human-readable Markdown with tables and narrative

Entry point: civ-arena-analyze (see pyproject.toml).

TOKEN SEMANTICS NOTE
--------------------
Per-step prompt_tokens / completion_tokens in a transcript REPEAT the originating
reply's values across every tool call in that reply — they are NOT per-step independent.
Always use the TOP-LEVEL transcript prompt_tokens / completion_tokens per record.
Never sum step-level token fields.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from civ_mcp.arena.task_tracker import (
    BLOCKED_VISIBLE_HOSTILE,
    DROPPED_FUTURE_DATED,
    RESOLVED_STATUSES,
    SKIPPED_NO_MOVES,
    UNITS_FETCH_FAILED,
)
from civ_mcp.arena.vocab import MCP_CIV6_PREFIX, LOCAL_TOOL_VERBS


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def load_records(path: "Path | str") -> list[dict]:
    """Load a JSONL file and return a list of dicts.  Missing file → empty list."""
    p = Path(path)
    if not p.exists():
        return []
    records: list[dict] = []
    with p.open() as fh:
        for raw in fh:
            raw = raw.strip()
            if raw:
                try:
                    obj = json.loads(raw)
                    if isinstance(obj, dict):
                        records.append(obj)
                except json.JSONDecodeError:
                    pass
    return records


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------

def _safe_str(v: object) -> str:
    return str(v) if v is not None else ""


def _steps_of(rec: dict) -> list[dict]:
    """Return steps list, filtering out non-dict entries."""
    return [s for s in (rec.get("steps") or []) if isinstance(s, dict)]


def _counted_invalid_calls(rec: dict) -> list[dict]:
    counted = []
    for item in rec.get("invalid_tool_calls") or []:
        if not isinstance(item, dict):
            counted.append(item)
            continue
        if item.get("reason") == "out_of_tier":
            continue
        counted.append(item)
    return counted


def _is_error_result(s: str) -> bool:
    """Return True if a tool result string represents a game-level failure.

    Matches real conventions from game_state.py:
    - Title-case ``Error: ...`` (set_city_production, set_research, found_city)
    - Pipe-delimited ``|BLOCKED`` suffix (move_unit blocked path)
    - Legacy all-caps ``ERROR: ...`` from agent.py exception wrapper (rare)
    """
    s2 = (s or "").strip().lower()
    return s2.startswith("error") or "|blocked" in s2


def _is_local_driver(rec: dict) -> bool:
    return rec.get("driver", "in_process") == "in_process"


# ---------------------------------------------------------------------------
# Slice 3 — standing memory / task tracker / behavior-critical tool helpers
# ---------------------------------------------------------------------------

# Tool-name sets (post _step_verb tool_base, i.e. MCP_CIV6_PREFIX already stripped)
# used to count per-system tool calls for the neutral behavior/performance metrics.
_GREAT_PEOPLE_TOOLS: frozenset[str] = frozenset({
    "recruit_great_person", "patronize_great_person", "reject_great_person",
    "get_great_people", "get_gp_advisor",
})
_TRADE_ROUTE_TOOLS: frozenset[str] = frozenset({
    "get_trade_routes", "get_trade_destinations", "start_trade_route", "teleport_trader",
})
# CLI puppets express trader actions through unit_action(action=...) rather than
# the local flat tools above; these verbs count as trade-route behavior too.
_TRADE_ROUTE_UNIT_ACTIONS: frozenset[str] = frozenset({"trade_route", "teleport"})
_RELIGION_WC_TOOLS: frozenset[str] = frozenset({
    "found_religion", "get_religion_beliefs", "get_religion_spread",
    "queue_wc_votes", "get_world_congress",
})


def _count_tool_calls(steps: list[dict], tool_bases: "frozenset[str]") -> int:
    """Count behavior tool calls after normalizing local and CLI tool vocabularies."""
    count = 0
    for step in steps:
        tool_base, verb = _step_verb(step)
        if tool_base in tool_bases:
            count += 1
            continue
        if tool_bases is _TRADE_ROUTE_TOOLS and tool_base == "unit_action" and verb in _TRADE_ROUTE_UNIT_ACTIONS:
            count += 1
    return count


def _standing_memory_injected(rec: dict) -> bool:
    sm = rec.get("standing_memory")
    if not isinstance(sm, dict):
        return False
    if "injected" in sm:
        return bool(sm.get("injected"))
    # Older Slice 3 records already included injected_chars; loaded alone only
    # proves a file existed, not that TTL allowed prompt injection.
    return bool((sm.get("injected_chars") or 0) > 0)


def _standing_memory_captured(rec: dict) -> bool:
    sm = rec.get("standing_memory")
    return bool(isinstance(sm, dict) and (sm.get("captured_chars") or 0) > 0)


def _task_tracker_pre_model_results(rec: dict) -> list[dict]:
    tt = rec.get("task_tracker")
    if not isinstance(tt, dict):
        return []
    results = tt.get("pre_model_results")
    if not isinstance(results, list):
        return []
    return [r for r in results if isinstance(r, dict)]


def _task_tracker_active(rec: dict) -> bool:
    """True only when the task tracker had meaningful state or results this turn.

    A zero-filled ``task_tracker`` dict (tracker enabled but idle, or a disabled
    record that still carries the field) must not count as a tracker-active turn.
    """
    tt = rec.get("task_tracker")
    if not isinstance(tt, dict):
        return False
    if tt.get("active_before") or tt.get("active_after"):
        return True
    return bool(_task_tracker_pre_model_results(rec))


# Bookkeeping results run_pre_model_tasks emits without issuing a game action
# or deciding anything about the task: a unit with no moves left, a transient
# unit-fetch failure (one entry PER executable task), a rollback drop. Counting
# them as follow-through attempts inflates the metric on flaky-tuner turns.
_NON_ATTEMPT_RESULTS = frozenset(
    {SKIPPED_NO_MOVES, UNITS_FETCH_FAILED, DROPPED_FUTURE_DATED}
)


def _classify_task_results(rec: dict) -> dict[str, int]:
    """Per-record task-result counts.

    Single owner of the status/result classification so behavior_metrics'
    global section and analyze()'s per-player accumulators can never disagree
    on what counts as complete/lost/failed/blocked.
    """
    counts = {
        "attempts": 0,
        "complete": 0,
        "lost": 0,
        "failed": 0,
        "blocked_visible_hostile": 0,
    }
    for entry in _task_tracker_pre_model_results(rec):
        if entry.get("result") not in _NON_ATTEMPT_RESULTS:
            counts["attempts"] += 1
        status = entry.get("status")
        if status in RESOLVED_STATUSES:
            counts[status] += 1
        if entry.get("result") == BLOCKED_VISIBLE_HOSTILE:
            counts["blocked_visible_hostile"] += 1
    return counts


def behavior_metrics(transcript_records: list[dict]) -> dict:
    """Aggregate NEUTRAL behavior/performance metrics across all transcript records.

    Populated from the Task 5 ``standing_memory`` / ``task_tracker`` transcript
    fields plus driver/tool-call counts. This is Slice 3 behavior testing over
    N puppets — deliberately NOT framed as an A/B treatment/control comparison.
    """
    standing_memory_turns = 0
    standing_memory_captured_turns = 0
    task_tracker_turns = 0
    task_pre_model_actions = 0
    task_completed = 0
    task_blocked_visible_hostile = 0
    task_lost = 0
    task_failed = 0
    drivers = {"in_process": 0, "cli": 0}
    puppeted_players: set = set()

    for rec in transcript_records:
        pid = rec.get("player_id")
        if pid is not None:
            puppeted_players.add(pid)

        if _is_local_driver(rec):
            drivers["in_process"] += 1
        else:
            drivers["cli"] += 1

        if _standing_memory_injected(rec):
            standing_memory_turns += 1
        if _standing_memory_captured(rec):
            standing_memory_captured_turns += 1

        if _task_tracker_active(rec):
            task_tracker_turns += 1

        task_counts = _classify_task_results(rec)
        task_pre_model_actions += task_counts["attempts"]
        task_completed += task_counts["complete"]
        task_lost += task_counts["lost"]
        task_failed += task_counts["failed"]
        task_blocked_visible_hostile += task_counts["blocked_visible_hostile"]

    return {
        "standing_memory_turns": standing_memory_turns,
        "standing_memory_captured_turns": standing_memory_captured_turns,
        "task_tracker_turns": task_tracker_turns,
        "task_pre_model_actions": task_pre_model_actions,
        "task_completed": task_completed,
        "task_blocked_visible_hostile": task_blocked_visible_hostile,
        "task_lost": task_lost,
        "task_failed": task_failed,
        "drivers": drivers,
        "puppeted_players": sorted(puppeted_players),
    }


def _config_summary_group_key(rec: dict) -> str:
    pid = rec.get("player_id")
    if pid is not None:
        return str(pid)
    return str(rec.get("model") or rec.get("provider") or "unknown")


def _config_summary_sort_key(key: object) -> tuple[int, int | str]:
    key_str = str(key)
    if key_str.isdigit():
        return (0, int(key_str))
    return (1, key_str)


def _json_fingerprint(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _config_fingerprint(rec: dict) -> dict:
    # n_ctx is deliberately NOT part of the fingerprint: it is a runtime-resolved
    # value that can legitimately change mid-run (a cold llama-swap backend probes
    # the default first, then the real context window once warm). Keying on it
    # would split one continuous player's turns into separate config groups.
    return {
        "model": rec.get("model", ""),
        "provider": rec.get("provider", ""),
        "civ_options": rec.get("civ_options") or {},
    }


def _representative_n_ctx(recs: list[dict]) -> int | None:
    """Pick the n_ctx to report for a group.

    Prefer the latest value whose source is not the transient fallback default.
    If all records are default-sourced, report the latest non-null value. Older
    records without n_ctx_source are treated as real resolved values.
    """
    fallback: int | None = None
    for rec in reversed(recs):
        n_ctx = rec.get("n_ctx")
        if n_ctx is None:
            continue
        if fallback is None:
            fallback = n_ctx
        if rec.get("n_ctx_source") != "default":
            return n_ctx
    return fallback


def config_summary(records: list[dict]) -> dict:
    """Return per-player experiment config fingerprints and outcome averages."""
    by_pid: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    fingerprints: dict[tuple[str, str], dict] = {}
    for rec in records:
        pid = _config_summary_group_key(rec)
        fingerprint = _config_fingerprint(rec)
        fingerprint_key = _json_fingerprint(fingerprint)
        by_pid[pid][fingerprint_key].append(rec)
        fingerprints[(pid, fingerprint_key)] = fingerprint

    summary: dict[str, dict] = {}
    for pid, groups in sorted(by_pid.items(), key=lambda item: _config_summary_sort_key(item[0])):
        ordered_groups = sorted(
            groups.items(),
            key=lambda item: (
                str(fingerprints[(pid, item[0])].get("model", "")),
                str(fingerprints[(pid, item[0])].get("provider", "")),
                item[0],
            ),
        )
        for index, (fingerprint_key, recs) in enumerate(ordered_groups, start=1):
            summary_key = pid if len(ordered_groups) == 1 else f"{pid}#{index}"
            fingerprint = fingerprints[(pid, fingerprint_key)]
            total_steps = 0
            total_invalid = 0
            total_briefing_tokens = 0
            total_score_delta = 0

            for rec in recs:
                step_count = rec.get("step_count")
                if step_count is None:
                    step_count = len(_steps_of(rec))
                total_steps += step_count or 0
                total_invalid += len(_counted_invalid_calls(rec))
                total_briefing_tokens += rec.get("briefing_tokens") or 0
                total_score_delta += (rec.get("state_delta") or {}).get("score", 0) or 0

            turns = len(recs)
            summary[summary_key] = {
                "model": fingerprint.get("model", ""),
                "provider": fingerprint.get("provider", ""),
                "civ_options": fingerprint.get("civ_options") or {},
                "n_ctx": _representative_n_ctx(recs),
                "turns": turns,
                "avg_steps": total_steps / turns,
                "invalid_call_rate": (total_invalid / total_steps) if total_steps else 0.0,
                "avg_briefing_tokens": total_briefing_tokens / turns,
                "avg_score_delta": total_score_delta / turns,
            }

    return summary


# ---------------------------------------------------------------------------
# Rubric helpers (turns 1-20, purely heuristic)
# ---------------------------------------------------------------------------

def _step_verb(step: dict) -> tuple[str, str]:
    """Return (tool_base, verb) normalizing both local-flat and CLI MCP-prefixed vocabularies.

    tool_base: step tool_name with any leading "mcp__civ6__" prefix stripped; "" if None.
    verb:
      - unit_action form (CLI): value of tool_args["action"]
      - move_unit (local): "move"
      - skip_unit (local): "skip"
      - fortify_unit (local): "fortify"
      - found_city (local): "found_city"
      - everything else: ""
    """
    raw_name: str = step.get("tool_name") or ""
    tool_base: str = raw_name.removeprefix(MCP_CIV6_PREFIX)
    tool_args = step.get("tool_args")
    if not isinstance(tool_args, dict):
        tool_args = {}

    if tool_base == "unit_action":
        verb: str = tool_args.get("action", "")
    elif tool_base in LOCAL_TOOL_VERBS:
        verb = LOCAL_TOOL_VERBS[tool_base]
    else:
        verb = ""

    return tool_base, verb


def _rubric_for_model(records: list[dict]) -> dict:
    """Compute early-game rubric flags for one model's records."""
    rubric: dict[str, dict | None] = {
        "founded_extra_city": None,
        "explored_vs_idle": None,
        "set_research_or_production": None,
        "wasted_move": None,
        "hallucinated_tools": None,
        "truncation_bad_move": None,
    }

    early = [r for r in records if r.get("turn", 0) <= 20]

    for rec in early:
        turn: int = rec.get("turn", 0)
        state_delta: dict = rec.get("state_delta") or {}
        steps = _steps_of(rec)
        invalid_calls: list[dict] = _counted_invalid_calls(rec)

        # ---- founded_extra_city ----
        if rubric["founded_extra_city"] is None:
            cities_delta = state_delta.get("cities", 0) or 0
            if cities_delta > 0:
                rubric["founded_extra_city"] = {
                    "turn": turn,
                    "note": f"cities delta={cities_delta}",
                }
            else:
                for step in steps:
                    _, verb = _step_verb(step)
                    if verb == "found_city":
                        rubric["founded_extra_city"] = {
                            "turn": turn,
                            "note": "found_city action issued",
                        }
                        break

        # ---- explored_vs_idle ----
        if rubric["explored_vs_idle"] is None:
            has_explore = False
            skip_fortify = 0
            for step in steps:
                _, verb = _step_verb(step)
                if verb in ("automate", "move"):
                    has_explore = True
                if verb in ("skip", "fortify"):
                    skip_fortify += 1
            if has_explore:
                rubric["explored_vs_idle"] = {
                    "turn": turn,
                    "note": "explored (automate/move)",
                }
            elif skip_fortify >= 2:
                rubric["explored_vs_idle"] = {
                    "turn": turn,
                    "note": f"idle loop: {skip_fortify} skip/fortify",
                }

        # ---- set_research_or_production ----
        if rubric["set_research_or_production"] is None:
            for step in steps:
                tool_base = (step.get("tool_name") or "").removeprefix(MCP_CIV6_PREFIX)
                result = _safe_str(step.get("tool_result_full", ""))
                if tool_base in ("set_research", "set_city_production"):
                    if not _is_error_result(result):
                        rubric["set_research_or_production"] = {
                            "turn": turn,
                            "tool": tool_base,
                        }
                        break

        # ---- wasted_move ----
        if rubric["wasted_move"] is None:
            for step in steps:
                _, verb = _step_verb(step)
                result = _safe_str(step.get("tool_result_full", ""))
                if verb == "move" and _is_error_result(result):
                    rubric["wasted_move"] = {
                        "turn": turn,
                        "note": "move returned error/blocked",
                    }
                    break

        # ---- hallucinated_tools ----
        if rubric["hallucinated_tools"] is None:
            for itc in invalid_calls:
                if not isinstance(itc, dict):
                    continue
                if itc.get("reason") == "unknown_tool":
                    rubric["hallucinated_tools"] = {
                        "turn": turn,
                        "tool_name": itc.get("tool_name"),
                    }
                    break

        # ---- truncation_bad_move ----
        if rubric["truncation_bad_move"] is None and _is_local_driver(rec):
            # Look for a truncated step followed by a skip/fortify in the same record
            saw_truncation = False
            for step in steps:
                if step.get("truncated", False):
                    saw_truncation = True
                if saw_truncation:
                    _, verb = _step_verb(step)
                    if verb in ("skip", "fortify"):
                        rubric["truncation_bad_move"] = {
                            "turn": turn,
                            "note": "skip/fortify after truncation",
                        }
                        break

    return rubric


# ---------------------------------------------------------------------------
# Main analyze function
# ---------------------------------------------------------------------------

def analyze(transcript_records: list[dict], cost_records: list[dict]) -> dict:  # noqa: ARG001
    """Analyze transcript and cost records.

    Parameters
    ----------
    transcript_records:
        Parsed records from transcript.jsonl.
    cost_records:
        Parsed records from arena_cost.jsonl (used for totals validation; currently
        transcript top-level tokens are the primary source per the caveat above).

    Returns
    -------
    dict with shape::

        {
          "by_player": {
            <player_id or model-fallback>: {
              "player_id": <int | None>,
              "model": <str>,
              "provider": <str | None>,
              "series": [...],
              "rates": {"invalid_call_rate": float, "truncation_incident_rate": float},
              "rubric": {...}
            }
          }
          "config_summary": {
            <player_id>: {
              "model": <str>,
              "provider": <str | None>,
              "civ_options": <dict>,
              "n_ctx": <int | None>,
              "turns": <int>,
              "avg_steps": <float>,
              "invalid_call_rate": <float>,
              "avg_briefing_tokens": <float>,
              "avg_score_delta": <float>,
            }
          }
        }

    Grouping key is ``player_id`` when present; falls back to
    ``model or provider or "unknown"`` for forward-compat with older records
    that lack ``player_id``.
    """
    # Group by seat (player_id), falling back to model identity for older records
    by_player: dict = defaultdict(list)
    # Capture identity labels from the first record seen per group
    group_labels: dict = {}

    for rec in transcript_records:
        pid = rec.get("player_id")
        key = pid if pid is not None else (rec.get("model") or rec.get("provider") or "unknown")
        by_player[key].append(rec)
        if key not in group_labels:
            group_labels[key] = {
                "player_id": pid,
                "model": rec.get("model") or "",
                "provider": rec.get("provider"),
                "driver": rec.get("driver", "in_process"),
            }

    # Sort each group's records by turn
    for key in by_player:
        by_player[key].sort(key=lambda r: r.get("turn", 0))

    result: dict = {}

    for key, records in by_player.items():
        series: list[dict] = []
        total_invalid = 0
        total_steps = 0
        truncated_count = 0
        total_local_steps = 0

        # Slice 3 — per-player behavior/performance accumulators
        mem_injected_turns = 0
        mem_captured_turns = 0
        task_attempts = 0
        task_completions = 0
        task_blocked = 0
        task_lost_count = 0
        task_failed_count = 0
        gp_calls = 0
        trade_calls = 0
        religion_wc_calls = 0

        for rec in records:
            turn: int = rec.get("turn", 0)
            state_after: dict = rec.get("state_after") or {}
            state_delta: dict = rec.get("state_delta") or {}
            steps = _steps_of(rec)
            invalid_calls: list = _counted_invalid_calls(rec)
            step_count: int = rec.get("step_count") or len(steps)

            # --- token totals: always top-level, never step-sum ---
            prompt_tokens: int = rec.get("prompt_tokens") or 0
            completion_tokens: int = rec.get("completion_tokens") or 0

            # Accumulate for rates
            total_invalid += len(invalid_calls)
            total_steps += step_count

            # Truncation: only meaningful for local (in_process) driver
            if _is_local_driver(rec):
                for step in steps:
                    total_local_steps += 1
                    if step.get("truncated", False):
                        truncated_count += 1

            # Slice 3 — standing memory / task tracker / behavior-critical tool calls
            if _standing_memory_injected(rec):
                mem_injected_turns += 1
            if _standing_memory_captured(rec):
                mem_captured_turns += 1
            task_counts = _classify_task_results(rec)
            task_attempts += task_counts["attempts"]
            task_completions += task_counts["complete"]
            task_lost_count += task_counts["lost"]
            task_failed_count += task_counts["failed"]
            task_blocked += task_counts["blocked_visible_hostile"]
            gp_calls += _count_tool_calls(steps, _GREAT_PEOPLE_TOOLS)
            trade_calls += _count_tool_calls(steps, _TRADE_ROUTE_TOOLS)
            religion_wc_calls += _count_tool_calls(steps, _RELIGION_WC_TOOLS)

            series.append({
                "turn": turn,
                "score": state_after.get("score"),
                "cities": state_after.get("cities"),
                "units": state_after.get("units"),
                "science": state_after.get("science"),
                "culture": state_after.get("culture"),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "wall_clock_s": rec.get("wall_clock_s"),
                "step_count": step_count,
                "state_delta": state_delta,
            })

        # Rates
        inv_rate = total_invalid / total_steps if total_steps > 0 else 0.0
        trunc_rate = (
            truncated_count / total_local_steps if total_local_steps > 0 else 0.0
        )

        labels = group_labels[key]
        result[key] = {
            "player_id": labels["player_id"],
            "model": labels["model"],
            "provider": labels["provider"],
            "series": series,
            "rates": {
                "invalid_call_rate": inv_rate,
                "truncation_incident_rate": trunc_rate,
            },
            "rubric": _rubric_for_model(records),
            "behavior": {
                "driver": labels["driver"],
                "provider": labels["provider"],
                "model": labels["model"],
                "standing_memory_injected_turns": mem_injected_turns,
                "standing_memory_captured_turns": mem_captured_turns,
                "task_follow_through_attempts": task_attempts,
                "task_completions": task_completions,
                "task_blocked": task_blocked,
                "task_lost": task_lost_count,
                "task_failed": task_failed_count,
                "great_people_tool_calls": gp_calls,
                "trade_route_tool_calls": trade_calls,
                "religion_wc_tool_calls": religion_wc_calls,
            },
        }

    return {
        "by_player": result,
        "config_summary": config_summary(transcript_records),
        "behavior": behavior_metrics(transcript_records),
    }


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------

def render_markdown(report: dict) -> str:
    """Render the analysis report as a Markdown string."""
    lines: list[str] = []
    lines.append("# Arena Analysis Report\n")

    by_player: dict = report.get("by_player", {})
    if not by_player:
        lines.append("_No players found in this run._\n")
        return "\n".join(lines)

    config: dict = report.get("config_summary", {})
    if config:
        lines.append("## Experiment config\n")
        lines.append(
            "| player | model | tools | max_steps | n_ctx | avg briefing tok | avg steps | invalid rate | avg Δscore |"
        )
        lines.append(
            "|--------|-------|-------|-----------|-------|------------------|-----------|--------------|------------|"
        )
        for pid, data in sorted(config.items(), key=lambda item: _config_summary_sort_key(item[0])):
            civ_options = data.get("civ_options") or {}
            tools = civ_options.get("tools", "")
            if isinstance(tools, list):
                tools = ", ".join(str(tool) for tool in tools)
            max_steps = civ_options.get("max_steps", "")
            model = data.get("model") or data.get("provider") or ""
            n_ctx = data.get("n_ctx")
            avg_briefing = data.get("avg_briefing_tokens", 0.0)
            avg_steps = data.get("avg_steps", 0.0)
            invalid_rate = data.get("invalid_call_rate", 0.0)
            avg_score_delta = data.get("avg_score_delta", 0.0)
            lines.append(
                f"| {pid} | {model} | {tools} | {max_steps} | {'' if n_ctx is None else n_ctx} | "
                f"{avg_briefing:.1f} | {avg_steps:.1f} | {invalid_rate:.1%} | "
                f"{avg_score_delta:.1f} |"
            )
        lines.append("")

    behavior: dict = report.get("behavior", {})
    if behavior:
        lines.append("## Behavior Metrics\n")
        drivers = behavior.get("drivers", {})
        puppeted = behavior.get("puppeted_players", [])
        lines.append(
            f"- **Standing memory injected turns**: {behavior.get('standing_memory_turns', 0)}"
        )
        lines.append(
            f"- **Standing memory captured turns**: {behavior.get('standing_memory_captured_turns', 0)}"
        )
        lines.append(f"- **Task tracker active turns**: {behavior.get('task_tracker_turns', 0)}")
        lines.append(f"- **Task pre-model actions**: {behavior.get('task_pre_model_actions', 0)}")
        lines.append(f"- **Task completions**: {behavior.get('task_completed', 0)}")
        lines.append(
            f"- **Task blocked (visible hostile)**: {behavior.get('task_blocked_visible_hostile', 0)}"
        )
        lines.append(f"- **Task lost**: {behavior.get('task_lost', 0)}")
        lines.append(f"- **Task failed**: {behavior.get('task_failed', 0)}")
        lines.append(
            f"- **Driver mix**: in_process={drivers.get('in_process', 0)}, cli={drivers.get('cli', 0)}"
        )
        lines.append(
            f"- **Puppeted players**: {', '.join(str(p) for p in puppeted) if puppeted else 'none'}\n"
        )

        lines.append(
            "| player_id | driver | provider | model | mem injected | mem captured | "
            "task attempts | task complete | task blocked | task lost | task failed | GP calls | trade calls | religion/WC calls |"
        )
        lines.append(
            "|-----------|--------|----------|-------|---------------|--------------|"
            "---------------|----------------|--------------|-----------|-------------|----------|-------------|-------------------|"
        )
        for _seat, data in sorted(by_player.items(), key=lambda item: _config_summary_sort_key(item[0])):
            pb = data.get("behavior") or {}
            pid = data.get("player_id")
            pid_str = str(pid) if pid is not None else str(_seat)
            lines.append(
                f"| {pid_str} | {pb.get('driver', '')} | {pb.get('provider', '') or ''} | "
                f"{pb.get('model', '') or ''} | {pb.get('standing_memory_injected_turns', 0)} | "
                f"{pb.get('standing_memory_captured_turns', 0)} | {pb.get('task_follow_through_attempts', 0)} | "
                f"{pb.get('task_completions', 0)} | {pb.get('task_blocked', 0)} | {pb.get('task_lost', 0)} | "
                f"{pb.get('task_failed', 0)} | "
                f"{pb.get('great_people_tool_calls', 0)} | {pb.get('trade_route_tool_calls', 0)} | "
                f"{pb.get('religion_wc_tool_calls', 0)} |"
            )
        lines.append("")

    for _seat, data in by_player.items():
        pid = data.get("player_id")
        model = data.get("model") or data.get("provider") or str(_seat)
        if pid is not None:
            heading = f"## Player {pid} — model `{model}`\n"
        else:
            heading = f"## Model: `{model}`\n"
        lines.append(heading)

        # Rates
        rates = data.get("rates", {})
        inv_rate = rates.get("invalid_call_rate", 0.0)
        trunc_rate = rates.get("truncation_incident_rate", 0.0)
        lines.append(f"- **Invalid call rate**: {inv_rate:.4f} ({inv_rate:.1%})")
        lines.append(f"- **Truncation incident rate**: {trunc_rate:.4f} ({trunc_rate:.1%})\n")

        # Turn series table
        series = data.get("series", [])
        if series:
            lines.append("### Turn Series\n")
            lines.append(
                "| Turn | Score | Cities | Units | Science | Culture "
                "| Prompt Tok | Compl Tok | Wall (s) | Steps |"
            )
            lines.append(
                "|------|-------|--------|-------|---------|---------|"
                "------------|-----------|----------|-------|"
            )
            for row in series:
                def _v(k: str) -> str:
                    v = row.get(k)
                    return "" if v is None else str(v)

                lines.append(
                    f"| {_v('turn')} | {_v('score')} | {_v('cities')} | {_v('units')} | "
                    f"{_v('science')} | {_v('culture')} | "
                    f"{_v('prompt_tokens')} | {_v('completion_tokens')} | "
                    f"{_v('wall_clock_s')} | {_v('step_count')} |"
                )
            lines.append("")

        # Early-game rubric
        rubric = data.get("rubric", {})
        lines.append("### Early-Game Rubric (Turns 1–20)\n")
        flag_labels = {
            "founded_extra_city": "Founded extra city",
            "explored_vs_idle": "Explored vs idle loops",
            "set_research_or_production": "Set research / production (non-ERROR)",
            "wasted_move": "Wasted / blind move (ERROR result)",
            "hallucinated_tools": "Hallucinated / unknown tool names",
            "truncation_bad_move": "Truncation → bad move correlation",
        }
        for flag, label in flag_labels.items():
            val = rubric.get(flag)
            if val is not None:
                detail = ", ".join(f"{k}={v}" for k, v in val.items())
                lines.append(f"- **{label}**: YES — {detail}")
            else:
                lines.append(f"- **{label}**: not observed")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point for `civ-arena-analyze`."""
    parser = argparse.ArgumentParser(
        prog="civ-arena-analyze",
        description=(
            "Pure-offline analysis of an arena run. "
            "Reads transcript.jsonl + arena_cost.jsonl; "
            "writes report.md and report.json."
        ),
    )
    parser.add_argument(
        "--run-id",
        required=True,
        help="Run ID (the directory name under --runs-dir).",
    )
    parser.add_argument(
        "--runs-dir",
        default="arena_runs",
        help="Base directory containing arena run directories (default: arena_runs).",
    )
    parser.add_argument(
        "--output-md",
        default=None,
        help="Output Markdown path (default: <runs-dir>/<run-id>/report.md).",
    )
    parser.add_argument(
        "--output-json",
        default=None,
        help="Output JSON path (default: <runs-dir>/<run-id>/report.json).",
    )

    args = parser.parse_args()

    runs_dir = Path(args.runs_dir)
    run_dir = runs_dir / args.run_id

    output_md = Path(args.output_md) if args.output_md else run_dir / "report.md"
    output_json = Path(args.output_json) if args.output_json else run_dir / "report.json"

    transcript_path = run_dir / "transcript.jsonl"
    cost_path = run_dir / "arena_cost.jsonl"

    transcript_records = load_records(transcript_path)
    cost_records = load_records(cost_path)

    report = analyze(transcript_records, cost_records)

    # Write JSON
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, indent=2))

    # Write Markdown
    output_md.parent.mkdir(parents=True, exist_ok=True)
    output_md.write_text(render_markdown(report))

    print(f"Report written: {output_json}  |  {output_md}")
