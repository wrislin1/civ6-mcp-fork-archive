# Slice 4 live-probe checklist (MERGE GATE)

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
- [ ] **Residual id-arg coercion** (registry.py, follow-up to finding #6 sweep):
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
      respectively) and left untouched. **Residual un-coerced LLM→Lua surface
      still to harden (tracked, out of this branch's scope):** (a) the numeric
      **id family** — `city_id`/`target_city_id`/`other_player_id`/
      `city_state_player_id`/`joint_war_target` — which splice into Lua (e.g. via
      `_lua_get_city`'s `{city_id} % 65536`) but are id-typed; decide int-cast vs
      accept-as-validated-upstream. (No bare `player_id` arg name exists in the
      registry — every player-id param is one of the names above.) (b) **string
      params interpolated into Lua string-literal contexts** — the full set
      found: `improvement_name`, `promotion_type`, `governor_type`,
      `belief_type`, `follower_belief`, `founder_belief`, `district_type`,
      `civic_name`, `tech`, `item_name`, `item_type`, `focus`,
      `government_type`, `religion_name`, `wonder_name`, `alliance_type`,
      `building` (already has an `isalnum()`-style guard in
      `build_move_great_work`), `yield_type`, `response` (`respond_to_diplomacy`
      — spliced into `AddResponse(sid, me, "{response}")`; `.upper()` does not
      neutralize embedded quotes), `action` (`send_diplomatic_action` — spliced
      into `local action = "{action_name}"`; NOTE `spy_action` and
      `resolve_city_capture` action params are ALREADY whitelist-guarded via
      exact-match against `_SPY_OP_HASHES` / `_CITY_CAPTURE_ACTIONS` and are
      safe — do not re-flag them), `offer_resources`/`request_resources`
      (`propose_trade`, both `mode="test"` and `mode="send"` — resource names
      spliced into `GameInfo.Resources["{res_name}"]` after only a `.split(",")`;
      cheapest to close with the same `isalnum()`-style guard as `building`),
      plus policy `policy_type` values via the `assignments` dict in
      `set_policies` (spliced into `GameInfo.Policies["{policy_type}"]`) — none
      of these can be int-cast; they need a whitelist/escaping pass. Decide
      whether to extend the sweep to these or accept them as validated upstream.
