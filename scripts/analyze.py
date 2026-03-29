#!/usr/bin/env -S .venv/bin/python
"""CivBench game analysis CLI — tool calling patterns and strategic planning."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Data access helpers
# ---------------------------------------------------------------------------

CONVEX_URL = "https://precious-lion-600.eu-west-1.convex.cloud"
CACHE_DIR = Path("/tmp/civbench_cache")
EVALS_ENV = Path(__file__).resolve().parent.parent / "evals" / ".env"

# Strategic tools worth tracking separately
STRATEGIC_TOOLS = [
    "get_victory_progress",
    "get_diplomacy",
    "get_religion_spread",
    "get_great_people",
    "get_strategic_map",
    "get_global_settle_advisor",
    "get_trade_routes",
    "get_district_advisor",
    "get_tech_civics",
    "get_empire_resources",
    "get_builder_tasks",
]


def _load_env() -> dict[str, str]:
    """Read evals/.env for Azure credentials."""
    env: dict[str, str] = {}
    if EVALS_ENV.exists():
        for line in EVALS_ENV.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def convex_query(path: str, args: dict | None = None) -> Any:
    """HTTP POST to Convex query endpoint."""
    import requests

    resp = requests.post(
        f"{CONVEX_URL}/api/query",
        json={"path": path, "args": args or {}},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "success":
        raise RuntimeError(f"Convex query failed: {data}")
    return data["value"]


def _list_games() -> list[dict]:
    """Fetch all games from Convex."""
    return convex_query("diary:listGames")


def _resolve_run_ids(identifiers: list[str], games: list[dict]) -> list[dict]:
    """Resolve game IDs or run IDs to game dicts."""
    results = []
    for ident in identifiers:
        for g in games:
            if g["runId"] == ident or g["gameId"] == ident:
                results.append(g)
                break
        else:
            print(f"Warning: '{ident}' not found", file=sys.stderr)
    return results


def _games_by_model(model: str, games: list[dict]) -> list[dict]:
    """Filter games by model name (substring match)."""
    return [g for g in games if model.lower() in (g.get("agentModel") or "").lower()]


def cloud_log(run_id: str) -> list[dict]:
    """Fetch and cache log.jsonl from Azure blob."""
    CACHE_DIR.mkdir(exist_ok=True)
    cache_path = CACHE_DIR / f"log_{run_id}.jsonl"

    if cache_path.exists():
        entries = []
        for line in cache_path.read_text().splitlines():
            if line.strip():
                entries.append(json.loads(line))
        return entries

    env = _load_env()
    conn_str = env.get("AZURE_STORAGE_CONNECTION_STRING", "")
    if not conn_str:
        print("Error: AZURE_STORAGE_CONNECTION_STRING not found in evals/.env", file=sys.stderr)
        sys.exit(1)

    import fsspec

    fs = fsspec.filesystem("az", connection_string=conn_str)
    blob_path = f"telemetry/runs/{run_id}/log.jsonl"
    try:
        with fs.open(blob_path) as f:
            raw = f.read()
    except FileNotFoundError:
        print(f"Error: {blob_path} not found in Azure", file=sys.stderr)
        return []

    cache_path.write_bytes(raw)
    entries = []
    for line in raw.decode().splitlines():
        if line.strip():
            entries.append(json.loads(line))
    return entries


def cloud_diary(run_id: str) -> list[dict]:
    """Fetch and cache diary.jsonl from Azure blob."""
    CACHE_DIR.mkdir(exist_ok=True)
    cache_path = CACHE_DIR / f"diary_{run_id}.jsonl"

    if cache_path.exists():
        entries = []
        for line in cache_path.read_text().splitlines():
            if line.strip():
                entries.append(json.loads(line))
        return entries

    env = _load_env()
    conn_str = env.get("AZURE_STORAGE_CONNECTION_STRING", "")
    if not conn_str:
        return []

    import fsspec

    fs = fsspec.filesystem("az", connection_string=conn_str)
    blob_path = f"telemetry/runs/{run_id}/diary.jsonl"
    try:
        with fs.open(blob_path) as f:
            raw = f.read()
    except FileNotFoundError:
        return []

    cache_path.write_bytes(raw)
    entries = []
    for line in raw.decode().splitlines():
        if line.strip():
            entries.append(json.loads(line))
    return entries


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _table(headers: list[str], rows: list[list[str]], align: list[str] | None = None):
    """Print a formatted table."""
    if not rows:
        print("  (no data)")
        return
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(str(cell)))
    if align is None:
        align = ["<"] * len(headers)

    header_line = "  ".join(f"{h:{a}{w}}" for h, w, a in zip(headers, widths, align))
    print(header_line)
    print("  ".join("-" * w for w in widths))
    for row in rows:
        cells = []
        for i, (cell, w, a) in enumerate(zip(row, widths, align)):
            cells.append(f"{str(cell):{a}{w}}")
        print("  ".join(cells))


def _sparkline(values: list[float], width: int = 40) -> str:
    """ASCII sparkline."""
    if not values:
        return ""
    blocks = " ▁▂▃▄▅▆▇█"
    mn, mx = min(values), max(values)
    rng = mx - mn if mx > mn else 1
    # Resample to width
    step = max(1, len(values) / width)
    sampled = []
    for i in range(width):
        idx = min(int(i * step), len(values) - 1)
        sampled.append(values[idx])
    return "".join(blocks[min(8, int((v - mn) / rng * 8))] for v in sampled)


def _pct(num: int, den: int) -> str:
    if den == 0:
        return "0.0%"
    return f"{100 * num / den:.1f}%"


def _percentile(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    k = (len(vals) - 1) * p
    f = int(k)
    c = f + 1
    if c >= len(vals):
        return vals[-1]
    return vals[f] + (k - f) * (vals[c] - vals[f])


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_games(args):
    """List all games."""
    games = _list_games()
    if args.model:
        games = _games_by_model(args.model, games)

    headers = ["Model", "Scenario", "Turns", "Score", "Result", "Victory", "Winner", "RunID"]
    align = ["<", "<", ">", ">", "<", "<", "<", "<"]
    rows = []
    for g in games:
        o = g.get("outcome") or {}
        rows.append([
            g.get("agentModel") or "?",
            g.get("scenarioId") or "?",
            int(g.get("count") or 0),
            int(g.get("score") or 0),
            o.get("result") or g.get("status", "?"),
            o.get("victoryType") or "",
            o.get("winnerCiv") or "",
            g.get("runId") or "?",
        ])
    _table(headers, rows, align)


def _analyze_log(entries: list[dict]) -> dict:
    """Compute analysis stats from log entries."""
    tool_calls = [e for e in entries if e.get("type") == "tool_call"]
    if not tool_calls:
        return {}

    tools = Counter(e["tool"] for e in tool_calls)
    cats = Counter(e["category"] for e in tool_calls)
    errors = [e for e in tool_calls if not e.get("success", True)]
    error_tools = Counter(e["tool"] for e in errors)

    # Per-turn stats
    per_turn: dict[int, int] = defaultdict(int)
    for e in tool_calls:
        per_turn[e.get("turn", 0)] += 1
    turn_counts = sorted(per_turn.values())

    # Duration stats
    durations_by_tool: dict[str, list[float]] = defaultdict(list)
    for e in tool_calls:
        d = e.get("duration_ms")
        if d is not None:
            durations_by_tool[e["tool"]].append(d)

    turns = sorted(set(e.get("turn") for e in tool_calls if e.get("turn") is not None))

    return {
        "total": len(tool_calls),
        "tools": tools,
        "categories": cats,
        "errors": len(errors),
        "error_tools": error_tools,
        "per_turn_counts": turn_counts,
        "turns": turns,
        "durations_by_tool": durations_by_tool,
        "tool_calls": tool_calls,
    }


def cmd_tools(args):
    """Tool calling analysis."""
    games = _list_games()

    if args.model:
        targets = _games_by_model(args.model, games)
    elif args.game_ids:
        targets = _resolve_run_ids(args.game_ids, games)
    else:
        targets = games

    for g in targets:
        rid = g["runId"]
        model = g.get("agentModel") or "?"
        o = g.get("outcome") or {}
        result = o.get("result") or g.get("status", "?")

        print(f"\n{'='*70}")
        print(f"  {rid} | {model} | {g.get('label','?')} | {result} | T{int(g.get('count') or 0)}")
        print(f"{'='*70}")

        entries = cloud_log(rid)
        stats = _analyze_log(entries)
        if not stats:
            print("  No tool call data found")
            continue

        # Category breakdown
        cats = stats["categories"]
        total = stats["total"]
        print(f"\n  Total calls: {total} | Errors: {stats['errors']} ({_pct(stats['errors'], total)})")
        print(f"  Query: {cats.get('query',0)} ({_pct(cats.get('query',0), total)}) | "
              f"Action: {cats.get('action',0)} ({_pct(cats.get('action',0), total)}) | "
              f"Turn: {cats.get('turn',0)} ({_pct(cats.get('turn',0), total)})")

        # Calls per turn
        tc = stats["per_turn_counts"]
        print(f"\n  Calls/turn: min={tc[0]}  med={tc[len(tc)//2]}  "
              f"mean={sum(tc)/len(tc):.1f}  p95={_percentile(tc, 0.95):.0f}  max={tc[-1]}")

        # Top 20 tools
        print(f"\n  Top 20 tools:")
        headers = ["Tool", "Count", "%", "Errors", "Med ms"]
        align = ["<", ">", ">", ">", ">"]
        rows = []
        for tool, count in stats["tools"].most_common(20):
            errs = stats["error_tools"].get(tool, 0)
            durs = sorted(stats["durations_by_tool"].get(tool, []))
            med = f"{durs[len(durs)//2]:.0f}" if durs else "-"
            rows.append([tool, count, _pct(count, total), errs, med])
        _table(headers, rows, align)

        # Error-heavy tools
        if stats["error_tools"]:
            print(f"\n  Top error tools:")
            for tool, count in stats["error_tools"].most_common(10):
                total_for_tool = stats["tools"][tool]
                print(f"    {tool:<30} {count:>4} errors / {total_for_tool:>4} calls ({_pct(count, total_for_tool)})")

        # Slowest tools (by median)
        print(f"\n  Slowest tools (median ms):")
        med_durations = []
        for tool, durs in stats["durations_by_tool"].items():
            if len(durs) >= 3:
                s = sorted(durs)
                med_durations.append((tool, s[len(s) // 2], len(s)))
        med_durations.sort(key=lambda x: -x[1])
        for tool, med, n in med_durations[:10]:
            print(f"    {tool:<30} {med:>7.0f} ms  (n={n})")


def cmd_compare(args):
    """Side-by-side model comparison."""
    games = _list_games()

    if args.models:
        model_names = [m.strip() for m in args.models.split(",")]
    else:
        model_names = sorted(set(g.get("agentModel") or "unknown" for g in games))

    model_games: dict[str, list[dict]] = defaultdict(list)
    for g in games:
        m = g.get("agentModel") or "unknown"
        if m in model_names:
            model_games[m].append(g)

    # --- Win/Loss ---
    print("\n  Win/Loss Record")
    print("  " + "-" * 50)
    for model in model_names:
        gg = model_games[model]
        wins = sum(1 for g in gg if (g.get("outcome") or {}).get("result") == "victory")
        defeats = sum(1 for g in gg if (g.get("outcome") or {}).get("result") == "defeat")
        live = sum(1 for g in gg if g.get("status") == "live" or not g.get("outcome"))
        print(f"    {model:<20} {wins}W / {defeats}L / {live}live  ({len(gg)} games)")

    # --- Aggregate tool patterns (per-game, then averaged) ---
    print(f"\n  Tool Calling Patterns (per-game averages)")
    print("  " + "-" * 50)

    # Compute per-game stats, then aggregate per model
    model_per_game: dict[str, list[dict]] = defaultdict(list)
    model_agg_tools: dict[str, Counter] = defaultdict(Counter)
    for model in model_names:
        for g in model_games[model]:
            entries = cloud_log(g["runId"])
            stats = _analyze_log(entries)
            if stats:
                model_per_game[model].append(stats)
                model_agg_tools[model] += stats["tools"]

    headers = ["Metric"] + model_names
    align = ["<"] + [">"] * len(model_names)
    rows = []

    def _stat(model: str, key: str) -> str:
        per_game = model_per_game.get(model, [])
        if not per_game:
            return "-"
        if key == "games":
            return str(len(per_game))
        if key == "total":
            avg = sum(s["total"] for s in per_game) / len(per_game)
            return f"{avg:.0f}"
        if key == "errors":
            avg_err = sum(s["errors"] for s in per_game) / len(per_game)
            avg_total = sum(s["total"] for s in per_game) / len(per_game)
            return f"{avg_err:.0f} ({_pct(int(avg_err), int(avg_total))})"
        if key == "q_ratio":
            total_q = sum(s["categories"].get("query", 0) for s in per_game)
            total_a = sum(s["categories"].get("action", 0) for s in per_game)
            return f"{_pct(total_q, total_q + total_a)} Q"
        if key == "calls_per_turn":
            # Average the per-game means
            means = []
            meds = []
            for s in per_game:
                tc = s["per_turn_counts"]
                means.append(sum(tc) / len(tc))
                meds.append(tc[len(tc) // 2])
            avg_mean = sum(means) / len(means)
            avg_med = sum(meds) / len(meds)
            return f"{avg_mean:.1f} (med {avg_med:.0f})"
        if key == "turns":
            avg = sum(len(s["turns"]) for s in per_game) / len(per_game)
            return f"{avg:.0f}"
        return "-"

    for key, label in [
        ("games", "Games"),
        ("total", "Avg calls/game"),
        ("turns", "Avg turns/game"),
        ("errors", "Avg errors/game"),
        ("q_ratio", "Query ratio"),
        ("calls_per_turn", "Calls/turn"),
    ]:
        rows.append([label] + [_stat(m, key) for m in model_names])
    _table(headers, rows, align)

    # --- Strategic tool usage (per 100 turns, averaged across games) ---
    print(f"\n  Strategic Tool Usage (per 100 turns, avg across games)")
    print("  " + "-" * 50)
    headers = ["Tool"] + model_names
    align = ["<"] + [">"] * len(model_names)
    rows = []
    for tool in STRATEGIC_TOOLS:
        vals = []
        for model in model_names:
            per_game = model_per_game.get(model, [])
            if not per_game:
                vals.append("-")
                continue
            rates = []
            for s in per_game:
                count = s["tools"].get(tool, 0)
                n_turns = len(s["turns"]) or 1
                rates.append(100 * count / n_turns)
            vals.append(f"{sum(rates) / len(rates):.1f}")
        rows.append([tool] + vals)
    _table(headers, rows, align)

    # --- Yield milestones from turnSeries ---
    print(f"\n  Yield Milestones (agent player)")
    print("  " + "-" * 50)
    milestones = [50, 100, 150, 200, 250]
    for metric in ["score", "science", "culture", "gold", "military", "cities", "territory"]:
        print(f"\n    {metric}:")
        headers = ["Turn"] + model_names
        align = [">"] + [">"] * len(model_names)
        rows = []
        for turn in milestones:
            vals = []
            for model in model_names:
                # Average across games for this model
                game_vals = []
                for g in model_games[model]:
                    summary = convex_query("diary:getGameSummary", {"gameId": g["gameId"]})
                    if not summary or not summary.get("turnSeries"):
                        continue
                    ts = summary["turnSeries"]
                    players = ts.get("players", ts)
                    for pid, pdata in players.items():
                        if pdata.get("is_agent"):
                            series = pdata.get("metrics", {}).get(metric, [])
                            if turn - 1 < len(series):
                                game_vals.append(series[turn - 1])
                if game_vals:
                    avg = sum(game_vals) / len(game_vals)
                    vals.append(f"{avg:.0f}")
                else:
                    vals.append("-")
            rows.append([f"T{turn}"] + vals)
        _table(headers, rows, align)


def cmd_strategy(args):
    """Strategic planning deep-dive."""
    games = _list_games()
    targets = _resolve_run_ids([args.game_id], games)
    if not targets:
        print(f"Game '{args.game_id}' not found")
        return
    g = targets[0]
    rid = g["runId"]
    model = g.get("agentModel") or "?"
    o = g.get("outcome") or {}

    print(f"\n{'='*70}")
    print(f"  Strategy Analysis: {rid} | {model}")
    print(f"  {g.get('label','?')} ({g.get('leader','?')}) | {o.get('result', g.get('status','?'))}")
    print(f"{'='*70}")

    entries = cloud_log(rid)
    tool_calls = [e for e in entries if e.get("type") == "tool_call"]

    # --- Strategic tool timeline ---
    print(f"\n  Strategic Tool Timeline")
    print("  " + "-" * 50)

    strategic_calls: dict[str, list[int]] = defaultdict(list)
    for e in tool_calls:
        if e["tool"] in STRATEGIC_TOOLS:
            t = e.get("turn")
            if t is not None:
                strategic_calls[e["tool"]].append(int(t))

    headers = ["Tool", "Count", "First", "Last", "Max Gap", "Avg Gap"]
    align = ["<", ">", ">", ">", ">", ">"]
    rows = []
    for tool in STRATEGIC_TOOLS:
        turns = sorted(strategic_calls.get(tool, []))
        if not turns:
            rows.append([tool, 0, "-", "-", "-", "-"])
            continue
        gaps = [turns[i + 1] - turns[i] for i in range(len(turns) - 1)]
        max_gap = max(gaps) if gaps else 0
        avg_gap = sum(gaps) / len(gaps) if gaps else 0
        rows.append([
            tool,
            len(turns),
            turns[0],
            turns[-1],
            max_gap,
            f"{avg_gap:.0f}",
        ])
    _table(headers, rows, align)

    # --- Large gaps (>30 turns without checking) ---
    print(f"\n  Notable Gaps (>30 turns without check)")
    print("  " + "-" * 50)
    found_gap = False
    for tool in STRATEGIC_TOOLS:
        turns = sorted(strategic_calls.get(tool, []))
        if len(turns) < 2:
            continue
        for i in range(len(turns) - 1):
            gap = turns[i + 1] - turns[i]
            if gap > 30:
                print(f"    {tool}: T{turns[i]} → T{turns[i+1]} ({gap} turns)")
                found_gap = True
    if not found_gap:
        print("    (none)")

    # --- City expansion timeline (from turnSeries) ---
    print(f"\n  City Expansion Timeline")
    print("  " + "-" * 50)
    summary = convex_query("diary:getGameSummary", {"gameId": g["gameId"]})
    if summary and summary.get("turnSeries"):
        ts = summary["turnSeries"]
        players = ts.get("players", ts)
        for pid, pdata in players.items():
            if pdata.get("is_agent"):
                cities = pdata.get("metrics", {}).get("cities", [])
                if cities:
                    prev = 0
                    for turn, count in enumerate(cities, 1):
                        count = int(count)
                        if count > prev:
                            print(f"    T{turn:>3}: {count} cities")
                            prev = count
                    print(f"    Sparkline: {_sparkline(cities)}")

    # --- Score progression ---
    if summary and summary.get("turnSeries"):
        players = summary["turnSeries"].get("players", summary["turnSeries"])
        for pid, pdata in players.items():
            if pdata.get("is_agent"):
                for metric in ["score", "science", "culture", "military"]:
                    series = pdata.get("metrics", {}).get(metric, [])
                    if series:
                        print(f"    {metric:<12}: {_sparkline(series)}  (final: {series[-1]:.0f})")

    # --- Research changes ---
    print(f"\n  Research Activity")
    print("  " + "-" * 50)
    research_calls = [e for e in tool_calls if e["tool"] == "set_research"]
    if research_calls:
        print(f"    Total set_research calls: {len(research_calls)}")
        # Show first 10 and last 5
        for e in research_calls[:10]:
            p = e.get("params", {})
            tech = p.get("tech") or p.get("tech_or_civic") or "?"
            print(f"      T{e.get('turn',0):>3}: {tech}")
        if len(research_calls) > 15:
            print(f"      ... ({len(research_calls) - 15} more)")
        if len(research_calls) > 10:
            for e in research_calls[-5:]:
                p = e.get("params", {})
                tech = p.get("tech") or p.get("tech_or_civic") or "?"
                print(f"      T{e.get('turn',0):>3}: {tech}")

    # --- Diary reflections (planning field) ---
    print(f"\n  Agent Planning Reflections (from diary)")
    print("  " + "-" * 50)
    diary = cloud_diary(rid)
    agent_rows = [d for d in diary if d.get("is_agent") and d.get("reflections")]
    sample_turns = [1, 25, 50, 100, 150, 200, 250, 300]
    for t in sample_turns:
        for row in agent_rows:
            if row.get("turn") == t:
                refl = row.get("reflections", {})
                planning = refl.get("planning", "")
                hypothesis = refl.get("hypothesis", "")
                if planning or hypothesis:
                    print(f"\n    T{t} planning: {planning[:200]}")
                    if hypothesis:
                        print(f"    T{t} hypothesis: {hypothesis[:200]}")
                break

    # --- Error patterns ---
    print(f"\n  Error Patterns")
    print("  " + "-" * 50)
    error_calls = [e for e in tool_calls if not e.get("success", True)]
    if error_calls:
        error_by_turn = defaultdict(int)
        for e in error_calls:
            # Bucket into 50-turn ranges
            bucket = (e.get("turn", 0) // 50) * 50
            error_by_turn[bucket] += 1
        print(f"    Errors by era:")
        for bucket in sorted(error_by_turn.keys()):
            print(f"      T{bucket:>3}-T{bucket+49:>3}: {error_by_turn[bucket]:>3} errors")

        # Common error messages
        error_msgs = Counter()
        for e in error_calls:
            summary = e.get("result_summary", "")[:80]
            error_msgs[f"{e['tool']}: {summary}"] += 1
        print(f"\n    Top error patterns:")
        for msg, count in error_msgs.most_common(10):
            print(f"      {count:>3}x  {msg}")


def cmd_turns(args):
    """Per-turn breakdown."""
    games = _list_games()
    targets = _resolve_run_ids([args.game_id], games)
    if not targets:
        print(f"Game '{args.game_id}' not found")
        return
    g = targets[0]
    rid = g["runId"]

    entries = cloud_log(rid)
    tool_calls = [e for e in entries if e.get("type") == "tool_call"]

    # Parse turn range
    start, end = 1, 9999
    if args.range:
        parts = args.range.split("-")
        start = int(parts[0])
        end = int(parts[1]) if len(parts) > 1 else start

    # Filter to range
    in_range = [e for e in tool_calls if start <= (e.get("turn") or 0) <= end]

    # Per-turn tool counts
    per_turn: dict[int, Counter] = defaultdict(Counter)
    for e in in_range:
        per_turn[e.get("turn", 0)][e["tool"]] += 1

    turns = sorted(per_turn.keys())
    if not turns:
        print("No data in range")
        return

    # Metrics from turnSeries
    metrics = [m.strip() for m in (args.metric or "score,science,military").split(",")]
    summary = convex_query("diary:getGameSummary", {"gameId": g["gameId"]})
    if summary and summary.get("turnSeries"):
        players = summary["turnSeries"].get("players", summary["turnSeries"])
        for pid, pdata in players.items():
            if pdata.get("is_agent"):
                print(f"\n  Metric Sparklines (T{start}-T{end}):")
                for metric in metrics:
                    series = pdata.get("metrics", {}).get(metric, [])
                    if series:
                        sliced = series[max(0, start - 1):end]
                        mn = min(sliced) if sliced else 0
                        mx = max(sliced) if sliced else 0
                        print(f"    {metric:<15}: {_sparkline(sliced, 50)}  ({mn:.0f} → {mx:.0f})")

    # Per-turn tool heatmap
    print(f"\n  Tool calls per turn (T{start}-T{end}):")
    total_per_turn = [(t, sum(per_turn[t].values())) for t in turns]
    vals = [v for _, v in total_per_turn]
    print(f"    Total calls:  {_sparkline(vals, 50)}  (min={min(vals)} max={max(vals)})")

    # Show turns with unusually high call counts
    mean = sum(vals) / len(vals)
    print(f"\n  Turns with >2x mean calls ({mean:.0f}):")
    high_turns = [(t, n) for t, n in total_per_turn if n > 2 * mean]
    if high_turns:
        for t, n in high_turns[:20]:
            top3 = per_turn[t].most_common(3)
            top_str = ", ".join(f"{tool}={c}" for tool, c in top3)
            print(f"    T{t:>3}: {n:>3} calls  ({top_str})")
        if len(high_turns) > 20:
            print(f"    ... and {len(high_turns) - 20} more")
    else:
        print("    (none)")

    # Error rate by turn
    errors_per_turn: dict[int, int] = defaultdict(int)
    for e in in_range:
        if not e.get("success", True):
            errors_per_turn[e.get("turn", 0)] += 1
    if errors_per_turn:
        err_vals = [errors_per_turn.get(t, 0) for t in turns]
        print(f"\n    Errors/turn:  {_sparkline(err_vals, 50)}  (total={sum(err_vals)})")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="CivBench game analysis CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    # games
    p_games = sub.add_parser("games", help="List all games")
    p_games.add_argument("--model", help="Filter by model name")

    # tools
    p_tools = sub.add_parser("tools", help="Tool calling analysis")
    p_tools.add_argument("game_ids", nargs="*", help="Game/run IDs")
    p_tools.add_argument("--model", help="Analyze all games for a model")

    # compare
    p_compare = sub.add_parser("compare", help="Side-by-side model comparison")
    p_compare.add_argument("--models", help="Comma-separated model names")

    # strategy
    p_strategy = sub.add_parser("strategy", help="Strategic planning deep-dive")
    p_strategy.add_argument("game_id", help="Game or run ID")

    # turns
    p_turns = sub.add_parser("turns", help="Per-turn breakdown")
    p_turns.add_argument("game_id", help="Game or run ID")
    p_turns.add_argument("--range", help="Turn range, e.g. 50-100")
    p_turns.add_argument("--metric", help="Metrics to show (comma-separated)")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    dispatch = {
        "games": cmd_games,
        "tools": cmd_tools,
        "compare": cmd_compare,
        "strategy": cmd_strategy,
        "turns": cmd_turns,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
