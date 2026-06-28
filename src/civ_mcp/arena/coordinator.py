from __future__ import annotations
import asyncio
from civ_mcp.arena import hook

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

async def run_arena(conn, gs, config, policy=None) -> dict:
    if policy is None:
        raise ValueError("run_arena needs a policy (ScriptedPolicy or LLMPolicy)")
    puppet_ids = set(config.puppet_ids or [p.player_id for p in config.players])
    played, log = 0, []
    try:
        await hook.inject(conn, sorted(puppet_ids))
        remaining = config.max_puppet_turns
        deadline_polls = 600  # ~poll budget; human may take a while to end their turn
        while remaining > 0 and deadline_polls > 0:
            st = await hook.poll(conn)
            if st.active and st.local in puppet_ids:
                result = await policy(gs, st.local, st.turn)
                log.append({"player": st.local, "turn": st.turn, **result})
                # End this puppet's turn: clear its units, hand control back to the human.
                await hook.finish_units(conn, st.local)
                await hook.restore_local(conn, 0)
                played += 1
                remaining -= 1
            else:
                await asyncio.sleep(1.0)
            deadline_polls -= 1
        return {"puppet_turns_played": played, "log": log}
    finally:
        await hook.disable(conn)
        await hook.restore_local(conn, 0)
        # DESIGN NOTE — turn-end method is the thing the dry-run gate (Task 9) validates. Primary:
        # `finish_units(K)` + `restore_local(0)` (matches the verified hold-release). If the live
        # dry-run shows the engine does NOT cleanly advance/hand back (e.g., it waits for an explicit
        # end-turn), add an InGame `UI.RequestAction(ActionTypes.ACTION_ENDTURN)` *before*
        # `restore_local`, executed while `local==K`. Do not add it speculatively — let the gate decide.
