# Slice 4 live-probe checklist (now a POST-MERGE TEST GATE)

> **Status 2026-07-08:** the slice-4 branch (incl. lua-injection hardening) was
> **merged to `main` on all four copies at `0de49fb`** on riz's directive,
> *before* these probes ran — so this is no longer a pre-merge gate but the
> **remaining test step**. All 9 probes below are still `[ ]`. They need a
> **late-game save** (Nationalism units, an air unit, an archaeologist, Great
> Works, a Gathering Storm game) — a fresh Turn-1 game cannot exercise them.
> Run them by **reading through the existing `civ-mcp` connection or a single
> freed FireTuner slot**, never a competing direct client (that wedges the
> single-client tuner). Until worked, treat the greenfield tools as provisional.

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

- [ ] **caps snapshot** — `build_caps_query(<pid>)` via execute_read. Verify all
      9 flags emit and flip correctly (check a civ with/without Diplomatic
      Service; verify great_works building scan and formation enums).
- [ ] **gossip** — `build_gossip_query()` via execute_write. GRIEV lines are
      expected to work; GOSSIP lines depend on Game.GetGossipManager existing.
      Likely outcome if absent: degrade to grievances-only (pre-approved in
      spec §3.1).
- [ ] **loyalty** — `build_loyalty_query()` via execute_write. LOYAL lines
      expected solid; LOYSRC breakdown is the probe target.
- [ ] **climate** — `build_climate_query()` via execute_write on a Gathering
      Storm game. Verify phase/sea/CO2 and DISASTER lines; on a base-game
      ruleset confirm the -1 degrade path.
- [ ] **great works query** — `build_great_works_query()` via execute_write on
      a save owning >=1 work + >=1 empty slot.
- [ ] **great works move** — `build_move_great_work(...)` between two owned
      slots; verify with a follow-up query that the work moved. UI.MoveGreatWork
      is the least certain API in the slice.
- [ ] **form corps/army** — on a save with Nationalism + two same-type units:
      `build_form_formation(...)`; verify via get_units that one unit remains
      with corps formation.
- [ ] **rebase** — with any air unit: `build_unit_operation(idx,"REBASE",x,y)`.
      If UnitOperationTypes.REBASE is nil, capture the operation hash the way
      espionage.py documents its _SPY_OP_HASHES and hardcode it.
- [ ] **excavate** — with an archaeologist + revealed antiquity site:
      `build_unit_operation(idx,"EXCAVATE",x,y)`; same hash fallback note.

Record results inline here (output snippet or "DEGRADED: <reason>" / "CUT:
<reason>") and mirror any degrade/cut into the spec before merge.

## Review-fix probes (2026-07-08)

- [ ] **Formation enum constants** (capabilities.py, finding #3): in a live InGame
      context, confirm `MilitaryFormationTypes.CORPS_FORMATION` and
      `MilitaryFormationTypes.STANDARD_FORMATION` are non-nil (the live-verified
      spelling elsewhere in the repo is the longer `STANDARD_MILITARY_FORMATION`).
      If either is nil, the fail-open guard keeps `corps`/`army` exposed but the
      detection is inert — fix the constant name and capture the correct enum.
- [ ] **Naval Fleet gating** (capabilities.py, finding #2): with a Nationalism-era
      naval-only roster, confirm `corps` reports 1 (a Fleet-eligible pair is
      detected), i.e. `form_corps` is exposed for naval civs.
- [ ] **GameClimate numeric format** (climate.py, finding #4): capture a real
      `CLIMATE|` line in a Gathering-Storm game and confirm the parser handles the
      actual formatting (integer vs float) of `GetClimateChangeLevel` /
      `GetSeaLevel` / `GetTotalCO2Footprint`.
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
