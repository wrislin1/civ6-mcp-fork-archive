from __future__ import annotations
import asyncio
import sys
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

async def run_arena(conn, gs, config, policy=None, policy_for=None) -> dict:
    if policy_for is None:
        if policy is None:
            raise ValueError("run_arena needs policy or policy_for")
        policy_for = lambda _pid: policy
    puppet_ids = set(config.puppet_ids or [p.player_id for p in config.players])
    played, log = 0, []
    try:
        await hook.inject(conn, sorted(puppet_ids))
        remaining = config.max_puppet_turns
        deadline_polls = 600  # ~poll budget; human may take a while to end their turn
        while remaining > 0 and deadline_polls > 0:
            st = await hook.poll(conn)
            if st.active and st.local in puppet_ids:
                pol = policy_for(st.local)
                exclusive = bool(getattr(pol, "needs_exclusive_tuner", False))
                if exclusive and conn.is_connected:
                    await conn.disconnect()       # free the single tuner slot for the CLI
                result = await pol(gs, st.local, st.turn)
                log.append({"player": st.local, "turn": st.turn, **result})
                if exclusive and not conn.is_connected:
                    await _reconnect_with_retry(conn)   # reclaim before we end the turn
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
        # skip a later step; the FIRST such exception is captured and re-raised after the whole
        # handback completes, so cancellation is propagated, never swallowed.
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
        if first_exc is not None:
            raise first_exc
