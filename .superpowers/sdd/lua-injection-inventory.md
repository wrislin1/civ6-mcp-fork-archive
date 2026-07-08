# Lua-injection surface inventory — arena LLM-controlled args

Scope: every argument that an in-process ("local") LLM civ can supply through
`src/civ_mcp/arena/registry.py` `dispatch()` that ends up interpolated into a
Lua source string sent over FireTuner. Read-only analysis; no files edited.

Already-fixed classes (verified, not re-listed as open below):
- Numeric `unit_index`/`unit_id`/`individual_id`/`dedication_index` are
  `int()`-coerced at their registry wrappers (or via the shared
  `_unit_index()` helper, `registry.py:239-241`).
- `work_id`/`slot`/`merge_unit_id` are `int()`-coerced; `votes` (queue_wc_votes)
  is fully coerced field-by-field at `registry.py:277-314` **and again**
  inside `build_register_wc_voter`'s `_as_int()` (`lua/congress.py:229-235`).
- `spy_action`'s `action` param is whitelist-guarded against `_SPY_OP_HASHES`
  (`lua/espionage.py:185-198`) — on an unknown mission the raw string is
  charset-scrubbed (`re.sub(r"[^A-Za-z0-9_ ]", ...)`) before being echoed, so
  it never reaches a live Lua sink unsanitized.
- `resolve_city_capture`'s `action` is whitelist-guarded against
  `_CITY_CAPTURE_ACTIONS` at the registry (`registry.py:320-327`) **and
  again** inside `build_resolve_city_capture`'s `directive_map`
  (`lua/cities.py:248-258`) — belt-and-suspenders.
- `move_great_work`'s `building` param passes an `isalnum()` guard
  (`lua/great_works.py:105-106`); `work_index`/`target_city_id`/`slot` are
  self-coerced via `int()` *inside the builder itself*
  (`lua/great_works.py:108-111`) — a raise, not a splice, on bad input.
- `build_spy_escape_route`'s `_ESCAPE_DISTRICTS` is a hardcoded Python
  constant, never LLM input.

---

## Class S — string args reaching a Lua string-literal / table-index context

All of the following are **currently open** (no whitelist/escaping at any
layer). Sink column cites `file:line`. "Transform" is what happens to the
value between the registry arg and the sink.

| Tool (registry wrapper) | Param | GameState method | Builder + sink line(s) | Transform en route | Enum-safe? |
|---|---|---|---|---|---|
| `purchase_item` (`registry.py:622-639`) | `item_type` | `purchase_item` (`game_state.py:701-710`) | `lua/cities.py:587` — `_bail(f"ERR:INVALID_TYPE|... got {item_type}")` (Python-side `_bail()` → `print("...{item_type}")`) | none (raw, reached only when `item_type.upper()` ∉ {UNIT,BUILDING,DISTRICT,PROJECT}) | Yes — enum, but note: the *other* embed of the uppercased value, `lua/cities.py:598` (`if "{itype}" == "UNIT" then`), is only reached **after** this same validation passes, so by construction it can only ever hold one of the 4 safe literals. Line 587 is the real hole. |
| `set_city_production` / `purchase_item` | `item_name` | `set_city_production` (`game_state.py:607-699`), `purchase_item` (`game_state.py:701-710`) | `lua/cities.py:425,427,438,443,450,478,486,591,592,609,642`; verify-readback `lua/cities.py:550,552,554,572` (`build_verify_production`) | none | **NO — flag as free text.** Comment at `lua/cities.py:440-442`: "Models often pass the friendly display name ('Scout') instead of the type name ('UNIT_SCOUT')" — the builder does a case-insensitive fallback match on `Locale.Lookup(row.Name)`, i.e. multi-word, mixed-case, space-containing strings are an *intended* input shape. A charset-only `^[A-Z_]+$` whitelist would break this fallback; needs quote/backslash escaping instead. |
| `set_research` | `tech` | `set_research` (`game_state.py:718-742`) | `lua/tech.py:200,202,204,222` (via `_build_set_ingame`, param name `name`) and `lua/tech.py:279,281,283` (GameCore fallback, via `_build_set_gamecore`) **plus** `game_state.py:730,734` — a *second*, independent interpolation of `tech_name` in the InGame-vs-index verify query built inline in `game_state.py`, not in `lua/*.py` at all | none | Yes — `TECH_*` enum |
| `set_civic` | `civic_name` | `set_civic` (`game_state.py:744-766`) | same pattern as above: `lua/tech.py:200,202,204,222` / `279,281,283` **plus** `game_state.py:754,758` inline verify query | none | Yes — `CIVIC_*` enum |
| `get_district_advisor` | `district_type` | `get_district_advisor` (`game_state.py:1251-1269`) | `lua/map.py:575,576,579,591` | none | Yes — `DISTRICT_*` enum |
| `get_wonder_advisor` | `wonder_name` | `get_wonder_advisor` (`game_state.py:1271-1282`) | `lua/map.py:700,701,702,705` | none | Yes — `BUILDING_*` enum (no friendly-name fallback here, unlike item_name) |
| `set_city_focus` | `focus` | `set_city_focus` (`game_state.py:1464-1467`) | `lua/cities.py:705,706,722` (`DEFAULT` branch at 674-699 has no injectable literal) | `.upper()`, then `YIELD_` prefix prepended if missing | Yes — `YIELD_*` enum (post-transform) |
| `purchase_item` | `yield_type` | `purchase_item` (`game_state.py:701-710`) | `lua/cities.py:593,594,616` | none (default `"YIELD_GOLD"`) | Yes — `YIELD_GOLD`/`YIELD_FAITH` enum |
| `patronize_great_person` | `yield_type` | `patronize_great_person` (`game_state.py:1326-1331`) | `lua/great_people.py:199,208` — **not** a table-index lookup this time; `yield_type.replace("YIELD_","").lower()` is spliced into an English message inside a Lua string literal (`"... " .. cost .. " {yield_type...lower()})"`). `.replace()`/`.lower()` do **not** strip quote/backslash characters. | `.replace("YIELD_","")`, `.lower()` (Python, pre-interpolation — does not sanitize) | Yes — enum in intent, but the transform is cosmetic only; the sink is still injectable for a crafted value like `GOLD" .. os.exit() .. "` |
| `appoint_governor` / `assign_governor` / `promote_governor` | `governor_type` | `appoint_governor` (`game_state.py:1004-1007`), `assign_governor` (`game_state.py:1009-1012`), `promote_governor` (`game_state.py:1014-1039`) | `lua/governance.py:227,228,249,250,272,273,274` **plus** a *second* independent inline interpolation in `promote_governor`'s verify query at `game_state.py:1023` | none | Yes — `GOVERNOR_*` enum |
| `promote_governor` / `promote_unit` | `promotion_type` | `promote_governor` (`game_state.py:1014-1039`), `promote_unit` (`game_state.py:1051-1120`) | `lua/governance.py:275,276,280,287,299` (governor promos), `lua/governance.py:376,377,388` (unit promos) **plus** `game_state.py:1024,1030` inline verify query | none | Yes — enum |
| `choose_pantheon` | `belief_type` | `choose_pantheon` (`game_state.py:1160-1163`) | `lua/religion.py:61,62` | none | Yes — `BELIEF_*` enum |
| `found_religion` | `religion_name` | `found_religion` (`game_state.py:1174-1179`, param renamed `religion_type`) | `lua/religion.py:195,196` | none | Yes — `RELIGION_*` enum |
| `found_religion` | `follower_belief` | same | `lua/religion.py:197,198` | none | Yes |
| `found_religion` | `founder_belief` | same | `lua/religion.py:199,200` | none | Yes |
| `change_government` | `government_type` | `change_government` (`game_state.py:1302-1305`, param renamed `gov_type`) | `lua/governance.py:643,644,645,646,649` | none | Yes — `GOVERNMENT_*` enum |
| `improve_tile` | `improvement_name` | `improve_tile` (`game_state.py:577-580`) | `lua/units.py:977,982,1000,1024,1089` | none | Yes — `IMPROVEMENT_*` enum |
| `set_policies` | policy names inside `assignments` dict | `set_policies` (`game_state.py:972-993`) | `lua/governance.py:109,110,120,128` (per-slot, inside `build_set_policies`'s generated `pre_checks`) | slot key int()-coerced (`registry.py:357-358`, `_coerce_policy_assignments`); the **policy-type string value** is untouched | Yes — `POLICY_*` enum |
| `send_diplomatic_action` | `action` | `send_diplomatic_action` (`game_state.py:839-860`) | `lua/diplomacy.py:454` / `468` (`local action = "{action_name}"`, war vs non-war validation blocks) **and** `lua/diplomacy.py:513` — `DiplomacyManager.RequestSession(me, target, "{session_str}")` where `session_str = session_string_map.get(action_name, action_name)`, i.e. for any action string not in the map (including a hostile one) the **raw uppercased action itself** is spliced straight into the session-open call, not just an error message | `.upper()` (registry) | Yes in intent (12-value enum listed in the tool description) but currently fully unvalidated |
| `respond_to_diplomacy` | `response` | `diplomacy_respond` (`game_state.py:777-837`) | `lua/diplomacy.py:286` (`if "{response}" == "EXIT" then`), `293` (`AddResponse(sid, me, "{response}")`), `294` (echo print) | `.upper()` (inside `diplomacy_respond`, `game_state.py:787`) | Yes — meant to be `POSITIVE`/`NEGATIVE` only, currently unvalidated |
| `form_alliance` | `alliance_type` | `form_alliance` (`game_state.py:958-961`) | `lua/diplomacy.py:1071` (`GameInfo.Alliances["ALLIANCE_{alliance_type_upper}"]`, via Python-built `alliance_key`) **and separately** the raw (already-`.upper()`'d by the registry) value again at `lua/diplomacy.py:1120,1126` (`local typeName = "{alliance_type}"`, echoed in the rejection message) | `.upper()` (registry, then again inside the alliance_key f-string) | Yes — `MILITARY`/`RESEARCH`/`CULTURAL`/`ECONOMIC`/`RELIGIOUS` enum |
| `propose_trade` (`mode="send"`/`"test"`) | `offer_resources` / `request_resources` (comma-split, one token = one deal item) | `propose_trade` / `test_trade` (`game_state.py:905-939`), item built by `_resource_items()` (`registry.py:384-391`) → `_lua_deal_item()` | `lua/diplomacy.py:830` — `GameInfo.Resources["{res_name}"]` inside `_lua_deal_item`'s `RESOURCE` branch, called once per offer/request item | `str(raw).split(",")` then `.strip()` per token (`registry.py:389`) — **no charset validation per token** | Yes in intent (`RESOURCE_*` enum, e.g. `RESOURCE_SILK` per the tool description) but each comma-separated token is unvalidated. Design nuance: a whitelist must run **per token after the split**, not on the raw joined string (commas are meaningful separators here). |

### Class S tally
**21 distinct params**, spread across **~30 distinct builder/game_state.py sink lines** (several params have 3-5 sink sites each; `item_name` alone has 11 sink lines across two builders + the verify-readback query).

---

## Class N — numeric id-family args still passed raw

| Tool (registry wrapper) | Param | GameState method | Builder + sink line(s) |
|---|---|---|---|
| `get_district_advisor` | `city_id` | `get_district_advisor` (`game_state.py:1251-1269`) | `lua/map.py:573` → `_lua_get_city()` (`lua/_helpers.py:87-93`, `CityManager.GetCity(me, {city_id} % 65536)`) |
| `get_wonder_advisor` | `city_id` | `get_wonder_advisor` (`game_state.py:1271-1282`) | `lua/map.py:699` → `_lua_get_city()` |
| `get_city_production` (and internal `list_city_production`) | `city_id` | `list_city_production` (`game_state.py:712-716`) | `lua/cities.py:287` → `_lua_get_city()` |
| `set_city_production` | `city_id` | `set_city_production` (`game_state.py:607-699`) | `lua/cities.py:437` → `_lua_get_city()`; **also** `build_verify_production` (`lua/cities.py:546`) interpolates `city_id` a *second* time, directly (`Players[me]:GetCities():FindID({city_id} % 65536)`), not via the shared helper |
| `set_city_production` | `target_x` / `target_y` | `set_city_production` (`game_state.py:607-699`) | `lua/cities.py:434-435` (`xy_params`/`xy_check_params`, bare `tParams[...] = {target_x}` / `{target_y}`) — **unlike every other coordinate pair in the registry, these are never `int()`-coerced anywhere** (`registry.py:541-542`: `args.get("target_x")`, `args.get("target_y")` passed through untouched; the Python signature `target_x: int | None = None` is a hint only) |
| `purchase_item` | `city_id` | `purchase_item` (`game_state.py:701-710`) | `lua/cities.py:590` → `_lua_get_city()` |
| `get_purchasable_tiles` | `city_id` | `get_purchasable_tiles` (`game_state.py:1288-1291`) | `lua/map.py:768` → `_lua_get_city()` |
| `purchase_tile` | `city_id` | `purchase_tile` (`game_state.py:1293-1296`) | `lua/map.py:832` → `_lua_get_city()` (its `x`/`y` params **are** already `int()`-coerced at `registry.py:875`) |
| `set_city_focus` | `city_id` | `set_city_focus` (`game_state.py:1464-1467`) | `lua/cities.py:650,677,704` → `_lua_get_city()` (three call sites: the yield-focus query plus both branches of `build_set_yield_focus`) |
| `assign_governor` | `city_id` | `assign_governor` (`game_state.py:1009-1012`) | `lua/governance.py:248` → `_lua_get_city()` |
| `city_attack` | `city_id` | `city_attack` (`game_state.py:415-443`) | `lua/cities.py:175` → `_lua_get_city()` (its `target_x`/`target_y` **are** already `int()`-coerced at `registry.py:1154`) |
| `get_trade_options` | `other_player_id` | `get_deal_options` (`game_state.py:890-893`) | `lua/diplomacy.py:600` (`local target = {other_player_id}`) |
| `respond_to_trade` | `other_player_id` | `respond_to_deal` (`game_state.py:900-903`) | `lua/diplomacy.py:799` |
| `propose_trade` (both `mode="test"` and `"send"`) | `other_player_id` | `propose_trade` (`game_state.py:905-928`), `test_trade` (`game_state.py:930-939`) | `lua/diplomacy.py:872` (`build_propose_trade`), `lua/diplomacy.py:955` (`build_test_trade`) |
| `propose_peace` | `other_player_id` | `propose_peace` (`game_state.py:941-956`) | `lua/diplomacy.py:1144` (`build_propose_peace`); its own internal verify round-trip `build_check_war_state` also embeds it again at `lua/diplomacy.py:1165` |
| `send_diplomatic_action` | `other_player_id` | `send_diplomatic_action` (`game_state.py:839-860`) | `lua/diplomacy.py:453` (war branch) / `467` (non-war branch) inside `build_send_diplo_action`; the async cleanup task also re-embeds it in `build_war_close_session` (`lua/diplomacy.py:565`) |
| `form_alliance` | `other_player_id` | `form_alliance` (`game_state.py:958-961`) | `lua/diplomacy.py:1070` |
| `respond_to_diplomacy` | `other_player_id` | `diplomacy_respond` (`game_state.py:777-837`) | `lua/diplomacy.py:284` (`build_diplomacy_respond`) **and** `lua/diplomacy.py:400` (`build_check_diplomacy_session_state`, called mid-method for the async-settle check) |
| `send_envoy` | `city_state_player_id` | `send_envoy` (`game_state.py:1131-1149`) | `lua/governance.py:456,460,463` (three separate bare interpolations inside `build_send_envoy`: a `CanGiveTokensToPlayer()` call, a params-table numeric assignment, and a `PlayerConfigurations[...]` index) |

### Class N tally
**4 distinct uncoerced numeric params** (`city_id`, `other_player_id`, `city_state_player_id`, and the `set_city_production`-only `target_x`/`target_y` pair), reaching **~23 distinct builder-function sink sites** (several id params are re-embedded a second time inside the same tool's own async verify/cleanup query, independent of the primary sink).

Note: `move_great_work`'s `target_city_id` is **not** listed here — it is
self-`int()`-coerced inside `build_move_great_work` itself
(`lua/great_works.py:108-111`) and raises `ValueError` rather than splicing
on bad input, so it is Class OK despite being the same "city id" family as
the open `city_id` params above. This inconsistency (one city-id-shaped
param hardened, ~10 others not) is worth calling out to the spec author —
it shows the hardening pattern already exists in the codebase and just
wasn't applied uniformly.

---

## Class OK — verified safe / not reachable via the arena registry

- `unit_index` / `unit_id` / `individual_id` / `dedication_index` — `int()`-coerced at registry wrappers or via `_unit_index()` (`registry.py:239-241`).
- `work_id` (great work index), `slot`, `merge_unit_id` — `int()`-coerced at registry; `target_city_id` and `work_index` are *also* self-coerced inside `build_move_great_work` (defense in depth).
- `votes` (queue_wc_votes: `hash`/`option`/`target`/`votes` fields) — coerced field-by-field at `registry.py:277-314`, and again inside `build_register_wc_voter`'s `_as_int()` helper.
- `spy_action`'s `action` — whitelisted against `_SPY_OP_HASHES`; unknown values are charset-scrubbed before being echoed (never reach a live Lua call).
- `resolve_city_capture`'s `action` — whitelisted twice (registry `_CITY_CAPTURE_ACTIONS` and builder `directive_map`).
- `move_great_work`'s `building` — `isalnum()`-guarded (after stripping `_`).
- `offer_gold`/`offer_gold_per_turn`/`offer_favor`/`request_gold`/`request_gold_per_turn`/`request_favor`/`joint_war_target` — all routed through `_positive_int()` (`registry.py:374-381`), which coerces via `int()` and clamps ≥0.
- `offer_open_borders`/`request_open_borders`/`accept` (respond_to_trade) — `_strict_bool()` (`registry.py:361-364`) rejects non-bool JSON before it ever reaches a trade-item dict; the deal item's `AGREEMENT`/`subtype` values that reach `DealAgreementTypes.{subtype}` (`lua/diplomacy.py:844`) are Python-constructed literals (`"OPEN_BORDERS"`, `"JOINT_WAR"`), never LLM strings.
- `move_unit`/`attack_unit`/`fortify_unit`/`skip_unit`/`found_city`/`heal_unit`/`alert_unit`/`automate_explore`/`remove_feature`/`get_pathing_estimate`/`get_settle_advisor`/`get_unit_promotions`/`upgrade_unit` and all other pure unit-index/coordinate tools — coordinates are `int()`-coerced inline at the registry lambda (e.g. `int(args["x"])`), and unit_index likewise.
- `form_corps`/`form_army` `command` param ("FORM_CORPS"/"FORM_ARMY") and `rebase_unit`/`excavate_artifact`'s `operation` param ("REBASE"/"EXCAVATE") — both are Python-literal constants supplied by `GameState` methods, never LLM strings; additionally whitelist-guarded (`raise ValueError`) inside their builders (`lua/units.py:1365-1366`, `1401-1402`).
- `vote_world_congress`/`submit_congress` GameState methods (`build_congress_vote`, `lua/congress.py:165-184`) interpolate `resolution_hash`/`option`/`target_index`/`num_votes` bare — but **these are not wired to any registry tool** (the only WC-voting path exposed to the LLM is `queue_wc_votes` → `build_register_wc_voter`, which self-coerces). Noted for completeness, out of scope for the arena RCE surface.
- CITY-type deal items (`_lua_deal_item`'s `"CITY"` branch, `lua/diplomacy.py:846-851`, bare `ci:SetValueType({city_id})`) — dead code from the registry's perspective: `_build_trade_items` (`registry.py:400-450`) never constructs a `CITY`-type item, so this branch is unreachable from `propose_trade`/`test_trade` today.

---

## Summary for spec author

- **Class S: 21 distinct params, ~30 distinct sink lines.**
- **Class N: 4 distinct params** (`city_id`, `other_player_id`, `city_state_player_id`, `set_city_production`'s `target_x`/`target_y`), **~23 distinct sink lines.**
- Free-text flag: **`item_name`** (set_city_production / purchase_item) is the
  one param that legitimately needs to accept non-enum-shaped strings
  (friendly display names like `"Scout"`, `"Ancient Walls"` — mixed case,
  spaces) via an intentional case-insensitive fallback match in the builder.
  It needs quote/backslash **escaping**, not charset whitelisting, or the
  whitelist will silently break that fallback. `offer_resources`/
  `request_resources` are enum-shaped in intent but need **per-token**
  whitelisting after the comma-split, not whole-string.
- Edge cases worth flagging to the spec author:
  1. **`game_state.py` itself is a sink**, independent of `lua/*.py` — the
     `set_research`/`set_civic` verify-readback queries (`game_state.py:730,754`)
     and the `promote_governor` verify-readback query (`game_state.py:1023-1030`)
     each re-interpolate the same raw string a second time in an ad-hoc f-string
     built inline in `game_state.py`. A whitelist patch to `lua/tech.py`/
     `lua/governance.py` alone would miss these.
  2. **`set_city_production`'s `target_x`/`target_y`** are the only
     coordinate pair in the entire registry that is never `int()`-coerced
     anywhere (registry passes `args.get("target_x")` raw) — every other
     coordinate-taking tool coerces at the registry lambda. This is a
     straightforward numeric-injection gap distinct from the city_id-family
     issue in the same tool.
  3. **`move_great_work`'s `target_city_id`** is already self-hardened
     (`int()` inside the builder, raises rather than splices) while ~10
     other `city_id`-family params across other tools are not — the fix
     pattern already exists in-repo, just needs to be applied uniformly
     (ideally via the shared `_lua_get_city()` helper accepting only `int`,
     forcing every call site to coerce before calling it, mirroring how
     `_unit_index()` centralizes the unit-id fix).
  4. **`send_diplomatic_action`'s `action`** doesn't just get echoed into an
     error string on an unrecognized value — an unrecognized value is
     spliced directly into the live `DiplomacyManager.RequestSession(me,
     target, "{session_str}")` call (`lua/diplomacy.py:513`), because
     `session_string_map.get(action_name, action_name)` falls back to the
     raw value itself when it's not one of the twelve known actions. This is
     the most directly "live" (non-error-path) Class S sink found.
