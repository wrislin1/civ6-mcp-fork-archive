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
    tool_base: str = raw_name.removeprefix("mcp__civ6__")
    tool_args = step.get("tool_args")
    if not isinstance(tool_args, dict):
        tool_args = {}

    if tool_base == "unit_action":
        verb: str = tool_args.get("action", "")
    elif tool_base == "move_unit":
        verb = "move"
    elif tool_base == "skip_unit":
        verb = "skip"
    elif tool_base == "fortify_unit":
        verb = "fortify"
    elif tool_base == "found_city":
        verb = "found_city"
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
        invalid_calls: list[dict] = rec.get("invalid_tool_calls") or []

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
                tool_base = (step.get("tool_name") or "").removeprefix("mcp__civ6__")
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
                        "note": "move returned ERROR",
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
          "by_model": {
            "<model>": {
              "series": [...],
              "rates": {"invalid_call_rate": float, "truncation_incident_rate": float},
              "rubric": {...}
            }
          }
        }
    """
    # Group by model, preserve insertion order within groups
    by_model: dict[str, list[dict]] = defaultdict(list)
    for rec in transcript_records:
        model = rec.get("model") or rec.get("provider") or "unknown"
        by_model[model].append(rec)

    # Sort each model's records by turn
    for model in by_model:
        by_model[model].sort(key=lambda r: r.get("turn", 0))

    result: dict[str, dict] = {}

    for model, records in by_model.items():
        series: list[dict] = []
        total_invalid = 0
        total_steps = 0
        truncated_count = 0
        total_local_steps = 0

        for rec in records:
            turn: int = rec.get("turn", 0)
            state_after: dict = rec.get("state_after") or {}
            state_delta: dict = rec.get("state_delta") or {}
            steps = _steps_of(rec)
            invalid_calls: list = rec.get("invalid_tool_calls") or []
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

        result[model] = {
            "series": series,
            "rates": {
                "invalid_call_rate": inv_rate,
                "truncation_incident_rate": trunc_rate,
            },
            "rubric": _rubric_for_model(records),
        }

    return {"by_model": result}


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------

def render_markdown(report: dict) -> str:
    """Render the analysis report as a Markdown string."""
    lines: list[str] = []
    lines.append("# Arena Analysis Report\n")

    by_model: dict = report.get("by_model", {})
    if not by_model:
        lines.append("_No models found in this run._\n")
        return "\n".join(lines)

    for model, data in by_model.items():
        lines.append(f"## Model: `{model}`\n")

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
