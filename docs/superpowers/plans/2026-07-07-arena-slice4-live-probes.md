# Slice 4 live-probe checklist (now a POST-MERGE TEST GATE)

> **Status 2026-07-08 (fix branch `arena-slice4-live-probe-fixes`):** the
> slice-4 branch (incl. lua-injection hardening) was **merged to `main` at
> `0de49fb`** *before* these probes ran. The probes have since been **run
> against a live turn-380 Future-era Gathering-Storm game**, read through a
> single freed FireTuner slot (one persistent connection), never a competing
> direct client (that wedges the single-client tuner). Outcomes are recorded
> per-box below: **3 defects fixed** (gossip table-pointer + 13k-line spam →
> `entry[1]`, capped; excavate nil enum → hardcoded hash; great-works move nil
> API → UNAVAILABLE), **2 degrades** (loyalty breakdown, climate sea-level),
> **1 cut** (great-works move). Real fixtures are pinned in
> `tests/test_live_probe_fixtures.py` + `tests/arena/test_capabilities.py`.

No greenfield-backed tool reaches a live run until its probe below captures a
real fixture, or the spec records a degrade/cut decision
(docs/superpowers/specs/2026-07-07-arena-slice4-full-toolset-design.md §3).

**Preconditions:** the 50-turn run has ended (no watcher owns FireTuner);
a game is loaded past the relevant era where noted. Run each probe from the
branch checkout with a direct connection:

    uv run python - <<'EOF'
    import asyncio
    from civ_mcp.connection import GameConnection
    from civ_mcp import lua as lq

    async def main():
        conn = GameConnection()
        await conn.connect()
        lines = await conn.execute_write(lq.build_gossip_query())  # <- swap per probe
        print("\n".join(lines))

    asyncio.run(main())
    EOF

> **Caps-snapshot probe exception:** `build_caps_query` is not exported via
> `civ_mcp.lua` — import it directly and use the read context:
> `from civ_mcp.arena.capabilities import build_caps_query` then
> `lines = await conn.execute_read(build_caps_query(<pid>))`.

For each probe: paste the real output lines into the matching parser test as a
fixture (replacing/augmenting the synthetic one), re-run the suite, and tick
the box. If an API errors, either fix the Lua from the live error, or record
the degrade/cut in the spec and tick with "DEGRADED"/"CUT".

- [x] **caps snapshot** — `build_caps_query(<pid>)` via execute_read.
      **→ RESULT:** real CAPS line captured; all 9 flags emit and flip.
      `archaeology=0` on a save that *owns* an (charge-0) archaeologist proves
      the gating scan is live, not hardcoded. Fixture: `test_parse_caps_real_capture`.
- [x] **gossip** — `build_gossip_query()` via execute_write.
      **→ RESULT:** GRIEV ok; GOSSIP worked but printed the table pointer
      (`tostring(entry)`) and emitted **13,493 lines/turn**. FIXED to extract
      `entry[1]` text + `entry[2]` turn, capped newest-first at 15/civ
      (commit `b770626`).
- [x] DEGRADED **loyalty** — `build_loyalty_query()` via execute_write.
      **→ RESULT:** LOYAL ×32 solid; LOYSRC omitted — `GetLoyaltyBreakdown` is
      nil in the tuner context, so `sources` degrades to `[]` (spec §3).
- [x] DEGRADED **climate** — `build_climate_query()` via execute_write on the
      Gathering-Storm game.
      **→ RESULT:** phase + CO2 solid (`CLIMATE|11|-1|17376`); **sea level
      degrades to `-1`** — `GetSeaLevel` + 3 alternatives all nil (spec §3).
- [x] **great works query** — `build_great_works_query()` via execute_write.
      **→ RESULT:** **145 GWSLOT lines** captured (filled works + empty-slot
      `-1` sentinels). Fixture: `test_real_great_works_slots`.
- [x] CUT **great works move** — `build_move_great_work(...)`. UI.MoveGreatWork
      was flagged the least-certain API in the slice.
      **→ RESULT:** `UI.MoveGreatWork`, `Game.GetGreatWorks`, and
      `GreatWorksManager` are **all nil** in the tuner context — no working move
      API. CUT to an informative `UNAVAILABLE:` readout; tool description +
      playbook now say so (commit `7cf085c`). Query path unaffected.
- [x] **form corps/army** — on the Nationalism-era roster:
      `build_form_formation(...)`, verified via get_units.
      **→ RESULT:** form corps OK — merged pair verified at `mf=1`. form army
      command path validated via `CanStartCommand`; no adjacent same-type trio
      existed on this save for a live merge (armies already present), so the
      army success path wasn't fully exercised.
- [x] **rebase** — air unit: `build_unit_operation(idx,"REBASE",x,y)`.
      **→ RESULT:** OK — `UnitOperationTypes.REBASE` resolves; its hash
      (`-1054550409`) is now pinned alongside excavate for stability (`8706814`).
- [x] DEGRADED **excavate** — `build_unit_operation(idx,"EXCAVATE",x,y)`.
      **→ RESULT:** `UnitOperationTypes.EXCAVATE` is **nil** → the op was
      silently failing. FIXED by hardcoding the hash `1548958412`
      (`DB.MakeHash("UNITOPERATION_EXCAVATE")`), mirroring espionage.py
      (commit `8706814`). Needs a charged archaeologist on a revealed site to
      exercise the full success path (this save's archaeologist had 0 charges).

Results are recorded inline above (real snippet / "DEGRADED" / "CUT"); the two
degrades and one cut are mirrored into the spec §3.

## Review-fix probes (2026-07-08)

- [x] **Formation enum constants** (capabilities.py, finding #3):
      **→ RESULT:** `GetMilitaryFormation` returns the integer enum
      **0 (standard) / 1 (corps) / 2 (army)** live; the caps scan reads them
      correctly, so `corps`/`army` detection is not inert.
- [x] **Naval Fleet gating** (capabilities.py, finding #2):
      **→ RESULT:** confirmed — a Fleet-eligible pair on the Nationalism-era
      roster is detected, so `form_corps` is exposed for naval rosters.
- [x] **GameClimate numeric format** (climate.py, finding #4):
      **→ RESULT:** the real `CLIMATE|` fields are **integers**
      (`CLIMATE|11|-1|17376`); the parser handles them. Sea-level reads `-1`
      (the documented degrade — see the climate probe above).
- [x] **Residual id-arg coercion** (registry.py, follow-up to finding #6 sweep):
      all always-on NUMERIC LLM args reaching bare Lua are now coerced. Round 1
      closed the flat `unit_index`/`unit_id` numeric tools (including the
      minimal-tier `found_city`/`fortify_unit`/`skip_unit`). Round 2 closed
      `individual_id` (`recruit_great_person`/`patronize_great_person`/
      `reject_great_person`) and `dedication_index` (`choose_dedication`).
      `move_great_work`'s `work_id`/`slot` were checked and left uncast at the
      wrapper: `build_move_great_work` (src/civ_mcp/lua/great_works.py) already
      runs `int(work_index)`/`int(target_city_id)`/`int(slot)` internally before
      Lua interpolation, so a non-numeric value raises `ValueError` before it can
      reach Lua — no wrapper-level gap there. `votes` (`queue_wc_votes`) and
      `merge_unit_id` (`form_corps`/`form_army`) were re-verified already safe
      (JSON-parsed + per-field int-coerced; wrapped in `_unit_index(...)`,
      respectively) and left untouched.
- [x] **LLM→Lua injection surface CLOSED (2026-07-08 hardening pass).** All
      untrusted args are validated at their GameState-method entry:
      `_safe_enum` (charset whitelist) for GameInfo-table enums, `_one_of`
      (closed allowlist) for small live-action enums
      (send_diplomatic_action/response/alliance_type/item_type/yield_type),
      `_lua_escape` for the one free-text param `item_name`, and `int()` for the
      `city_id`/`other_player_id`/`city_state_player_id` family +
      `set_city_production` coords. Helpers in `src/civ_mcp/lua/_helpers.py`;
      spec `docs/superpowers/specs/2026-07-08-arena-lua-injection-hardening-design.md`;
      inventory `.superpowers/sdd/lua-injection-inventory.md`. Out of scope
      (documented non-goals): the human-facing FastMCP `run_lua` (server.py:2864)
      and the unwired `build_congress_vote`. (Review follow-up 2026-07-08:
      `_lua_deal_item` now self-defends every branch at the builder — `subtype`
      via `_safe_enum`, `amount`/`duration`/`city_id` via `int()` — so the
      dead CITY branch is no longer an injection vector if revived; and
      `individual_id` is int()-cast at the recruit/patronize/reject GameState
      entries as defense-in-depth over the existing registry cast.)
      (Verified 2026-07-08: `target_city_id` and `joint_war_target` — named in
      the prior residual note's id family but not in this pass's closed set —
      were re-checked and are not open gaps. `target_city_id` is int()-cast
      inside `build_move_great_work` (`src/civ_mcp/lua/great_works.py:109`)
      before Lua interpolation, same as `work_index`/`slot` above.
      `joint_war_target`'s raw value never reaches Lua at all — both
      `arena/registry.py:446` and `server.py:1516` only test it for positivity
      to decide whether to append a hardcoded `"JOINT_WAR"` subtype string; the
      argument's own value is never spliced anywhere.)
