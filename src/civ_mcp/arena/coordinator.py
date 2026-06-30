from __future__ import annotations
import asyncio
import sys
from datetime import datetime, timezone
from civ_mcp import lua as lq
from civ_mcp.arena import hook


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


class ScriptedPolicy:
    """Deterministic no-LLM policy for the dry-run gate: observe, then skip unit 0."""
    async def __call__(self, gs, player_id: int, turn: int) -> dict:
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
    try:
        await hook.inject(conn, sorted(puppet_ids))
        remaining = config.max_puppet_turns
        deadline_polls = config.idle_poll_limit  # ~poll budget; human may take a while to end their turn
        while remaining > 0 and deadline_polls > 0:
            st = await hook.poll(conn)
            if st.active and st.local in puppet_ids:
                pol = policy_for(st.local)
                exclusive = bool(getattr(pol, "needs_exclusive_tuner", False))
                state_before = await _overview_snapshot(gs) if transcript is not None else None
                if exclusive and conn.is_connected:
                    await conn.disconnect()       # free the single tuner slot for the CLI
                result = await pol(gs, st.local, st.turn)
                _log_entry = {k: v for k, v in result.items() if k != "transcript"}
                log.append({"player": st.local, "turn": st.turn, **_log_entry})
                if exclusive and not conn.is_connected:
                    await _reconnect_with_retry(conn)   # reclaim before we end the turn
                state_after = await _overview_snapshot(gs) if transcript is not None else None
                if transcript is not None and result.get("transcript"):
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
