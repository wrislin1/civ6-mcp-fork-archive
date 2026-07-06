from __future__ import annotations
import asyncio
import sys
from datetime import datetime, timezone
from civ_mcp import lua as lq
from civ_mcp.arena import autoresolve, hook
from civ_mcp.arena.config import CivOptions
from civ_mcp.arena.memory import (
    extract_standing_plan,
    format_memory_block,
    load_memory,
    save_memory,
)
from civ_mcp.arena.task_tracker import (
    format_task_block,
    load_task_state,
    merge_tasks,
    parse_task_lines,
    run_pre_model_tasks,
    save_task_state,
)


async def _reconnect_with_retry(conn, attempts=5, delay=0.5):
    last = None
    for i in range(attempts):
        try:
            # Close any half-open writer from a prior failed attempt before reconnecting,
            # so repeated tries do not leak a socket/fd (connect() reassigns the writer).
            await conn.disconnect()
            await conn.connect(); return True
        except Exception as e:
            last = e
            if i < attempts - 1:          # no point sleeping after the final failed attempt
                await asyncio.sleep(delay)
    print(f"[arena] WARNING: reclaim connect failed after {attempts} attempts: {last!r}", file=sys.stderr)
    return False


async def _overview_snapshot(gs):
    """Bootstrap-free lightweight overview snapshot; returns dict or None on failure."""
    try:
        lines = await gs.conn.execute_write(lq.build_overview_query())
        ov = lq.parse_overview_response(lines)
        return {
            "score":    ov.score,
            "gold":     ov.gold,
            "science":  ov.science_yield,
            "culture":  ov.culture_yield,
            "faith":    ov.faith,
            "research": ov.current_research,
            "civic":    ov.current_civic,
            "cities":   ov.num_cities,
            "units":    ov.num_units,
        }
    except Exception:
        return None


# Reactive-only recovery for an orphaned first-meet greeting (the puppet local-player
# switch can leave one on screen — a session, or a view with no locatable session).
# NOT run automatically from the poll loop: it cannot tell an orphaned greeting from a
# leader scene the human is actively using, and force-hiding the latter blacks out the
# map. Invoked manually when a stuck greeting is actually reported.
async def _clear_blocking_diplomacy(conn) -> str:
    """Best-effort: if a diplomacy modal is blocking the idle human, clear it
    (close any real session, hide orphaned views, restore the in-game UI). Only
    acts when a view is actually visible; never raises into the poll loop."""
    try:
        lines = await conn.execute_write(lq.build_clear_blocking_diplomacy())
    except Exception:
        return "err"
    for line in lines:
        if line.startswith("CLEAR|"):
            return line
    return "?"


class ScriptedPolicy:
    """Deterministic no-LLM policy for the dry-run gate: observe, then skip unit 0."""
    async def __call__(self, gs, player_id: int, turn: int, **kwargs) -> dict:
        await gs.get_game_overview()
        await gs.get_units()
        try:
            await gs.skip_unit(0)
        except Exception as e:
            return {"summary": f"scripted: skip failed {e!r}", "actions": []}
        return {"summary": "scripted: observed + skipped unit 0", "actions": [{"tool": "skip_unit"}]}

async def run_arena(conn, gs, config, policy=None, policy_for=None, transcript=None) -> dict:
    if policy_for is None:
        if policy is None:
            raise ValueError("run_arena needs policy or policy_for")
        policy_for = lambda _pid: policy
    puppet_ids = set(config.puppet_ids or [p.player_id for p in config.players])
    played, log = 0, []
    _tx_on = transcript is not None and getattr(transcript, "enabled", True)
    try:
        await hook.inject(conn, sorted(puppet_ids))
        remaining = config.max_puppet_turns
        deadline_polls = config.idle_poll_limit  # ~poll budget; human may take a while to end their turn
        while remaining > 0 and deadline_polls > 0:
            st = await hook.poll(conn)
            if st.active and st.local in puppet_ids:
                pol = policy_for(st.local)
                exclusive = bool(getattr(pol, "needs_exclusive_tuner", False))
                opts = getattr(pol, "options", CivOptions())
                run_id = getattr(config, "run_id", "")
                transcript_dir = config.transcript_dir
                state_before = await _overview_snapshot(gs) if _tx_on else None

                # --- Load standing memory / task tracker state and run deterministic
                # pre-model task follow-through. This MUST happen before the exclusive
                # disconnect below: it uses `gs`, which is backed by the live `conn` —
                # a CLI turn's exclusive disconnect leaves no connection for these reads.
                memory = load_memory(transcript_dir, run_id, st.local) if opts.memory.enabled else None
                memory_block = format_memory_block(memory)

                active_tasks_before: tuple = ()
                updated_tasks: tuple = ()
                task_results: list = []
                active_tasks_after: tuple = ()
                task_block = ""
                if opts.task_tracker.enabled:
                    task_state = load_task_state(transcript_dir, run_id, st.local)
                    active_tasks_before = tuple(
                        t for t in task_state.tasks if t.status == "active"
                    )
                    updated_tasks, task_results = await run_pre_model_tasks(
                        gs, active_tasks_before
                    )
                    pre_model_state = save_task_state(
                        transcript_dir, run_id, st.local, updated_tasks
                    )
                    active_tasks_after = pre_model_state.tasks
                    task_block = format_task_block(updated_tasks, task_results)

                if exclusive and conn.is_connected:
                    await conn.disconnect()       # free the single tuner slot for the CLI
                result = await pol(
                    gs, st.local, st.turn, memory_block=memory_block, task_block=task_block
                )
                if exclusive and not conn.is_connected:
                    await _reconnect_with_retry(conn)   # reclaim before we end the turn
                try:
                    swept = await autoresolve.sweep_promotions(gs)
                except Exception as e:
                    swept = []
                    print(f"[arena] promotion sweep failed: {e!r}", file=sys.stderr)

                # --- Capture this turn's standing plan / tasks from the final summary.
                # Runs whenever either feature is enabled, since both parse the same
                # STANDING PLAN block the prompt asked for (build_opening_prompt's
                # include_standing_plan_instruction uses the same OR condition).
                captured_plan = ""
                if opts.memory.enabled or opts.task_tracker.enabled:
                    final_summary = (
                        result.get("transcript", {}).get("final_summary")
                        or result.get("summary", "")
                    )
                    captured_plan = extract_standing_plan(final_summary, opts.memory.max_chars)
                if opts.memory.enabled and captured_plan:
                    save_memory(
                        transcript_dir, run_id, st.local, st.turn, captured_plan,
                        opts.memory.max_chars,
                    )
                if opts.task_tracker.enabled:
                    new_tasks = parse_task_lines(captured_plan, st.turn)
                    merged = merge_tasks(updated_tasks, new_tasks, opts.task_tracker.max_tasks)
                    captured_state = save_task_state(transcript_dir, run_id, st.local, merged)
                    active_tasks_after = captured_state.tasks

                state_after = await _overview_snapshot(gs) if _tx_on else None
                _log_entry = {
                    k: v
                    for k, v in result.items()
                    if k not in ("transcript", "promotion_sweep")
                }
                _standing_memory_fields = {
                    "loaded": bool(memory),
                    "injected_chars": len(memory_block),
                    "captured_chars": len(captured_plan),
                }
                _task_tracker_fields = {
                    "active_before": len(active_tasks_before),
                    "pre_model_results": task_results,
                    "active_after": len(active_tasks_after),
                }
                log.append({
                    "player": st.local,
                    "turn": st.turn,
                    **_log_entry,
                    "promotion_sweep": swept,
                    "standing_memory": _standing_memory_fields,
                    "task_tracker": _task_tracker_fields,
                })
                if _tx_on and result.get("transcript"):
                    payload = result["transcript"]
                    steps = payload.get("steps", [])
                    if state_before is not None and state_after is not None:
                        _num = ("score", "gold", "science", "culture", "faith", "cities", "units")
                        state_delta = {k: state_after[k] - state_before[k] for k in _num}
                        state_delta["research"] = state_after["research"]
                        state_delta["civic"]    = state_after["civic"]
                    else:
                        state_delta = None
                    _pol_backend = getattr(pol, "backend", None)
                    record = {
                        **payload,
                        "schema_version": 1,
                        "run_id":   getattr(config, "run_id", ""),
                        "ts":       datetime.now(timezone.utc).isoformat(),
                        "player_id": st.local,
                        "turn":     st.turn,
                        "provider": getattr(pol, "provider", "local"),
                        "model":    getattr(_pol_backend, "model", getattr(pol, "model", "")),
                        "driver":   "cli" if str(getattr(pol, "provider", "local")).startswith("cli") else "in_process",
                        "step_count": len(steps),
                        "usd":      float(result.get("usage", {}).get("usd", 0.0)),
                        "state_before": state_before,
                        "state_after":  state_after,
                        "state_delta":  state_delta,
                        "promotion_sweep": swept,
                        "standing_memory": _standing_memory_fields,
                        "task_tracker": _task_tracker_fields,
                    }
                    transcript.write(record)
                # End this puppet's turn and hand control back toward the human.
                # DESIGN NOTE — the turn-end method is validated by the live dry-run gate (Task 9).
                # Primary (verified in the feasibility spike): finish_units(K) + restore_local(0).
                # If the live gate shows the engine does NOT advance / hand back cleanly, add an
                # InGame `UI.RequestAction(ActionTypes.ACTION_ENDTURN)` HERE — while local == K,
                # before restore_local. NEVER add it in the finally block (local is already 0 there).
                await hook.finish_units(conn, st.local)
                await hook.restore_local(conn, 0)
                played += 1
                remaining -= 1
            else:
                # Human seat is idle. Do NOT auto-clear diplomacy here: it cannot
                # distinguish an orphaned first-meet greeting from a leader scene the
                # human is actively using (declaring war, denouncing, trading), and
                # force-hiding the latter mid-transition can black out the map. Stuck
                # greetings are handled reactively via _clear_blocking_diplomacy.
                await asyncio.sleep(1.0)
            deadline_polls -= 1
        return {"puppet_turns_played": played, "log": log}
    finally:
        # Human safety invariant: ALWAYS hand control back. Reclaim a released connection first,
        # then restore the human, then disable — run all three best-effort so a failure in one
        # never skips the others. Each step is guarded against BaseException (not just Exception)
        # so an asyncio.CancelledError mid-handback (e.g. Ctrl-C during connect/restore) cannot
        # skip a later step.
        #
        # Re-raise policy: only interrupts (BaseException that is NOT a plain Exception —
        # CancelledError, KeyboardInterrupt, SystemExit) are re-raised; ordinary cleanup failures
        # (a dead-socket ConnectionError from reclaim, a transient hook.disable blip) are logged
        # and swallowed, matching the original best-effort contract. This is load-bearing: when
        # cancellation originates in the TRY BODY (Ctrl-C during the long CLI turn) it is already
        # in flight as we run cleanup; re-raising an ordinary cleanup Exception here would REPLACE
        # that in-flight CancelledError and swallow the cancellation. Swallowing best-effort
        # Exceptions lets the body's CancelledError keep propagating; re-raising a cleanup-origin
        # interrupt still surfaces it. Either way cancellation is propagated, never swallowed.
        first_exc = None
        steps = []
        if not conn.is_connected:
            steps.append(("reclaim-retry", lambda: _reconnect_with_retry(conn)))
        steps.append(("restore_local(0)", lambda: hook.restore_local(conn, 0)))
        steps.append(("hook.disable", lambda: hook.disable(conn)))
        for label, step in steps:
            try:
                await step()
            except BaseException as e:
                if first_exc is None:
                    first_exc = e
                print(f"[arena] WARNING: {label} failed in cleanup: {e!r}", file=sys.stderr)
        if first_exc is not None and not isinstance(first_exc, Exception):
            raise first_exc
