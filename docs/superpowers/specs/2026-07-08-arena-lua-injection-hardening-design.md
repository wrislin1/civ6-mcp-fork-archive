# Arena Lua-Injection Hardening — Design Spec

**Date:** 2026-07-08
**Branch:** `arena-slice4-full-toolset` (worktree; stays UNMERGED)
**Status:** design approved (enforcement location + fold-in-ids), pending spec review

## Goal

Close the remaining LLM-argument → Lua-injection surface in the arena so the
`arena-slice4-full-toolset` branch can clear its security sign-off. After the
prior review-fix wave, all **numeric unit** args are coerced; this pass closes
the **string-enum** class, the **one free-text** param, and the **residual
numeric id-family** (plus one missed coordinate pair) — leaving no untrusted arg
reaching a bare Lua context.

## Background / threat model

In-process ("local") LLM civs supply tool arguments through
`src/civ_mcp/arena/registry.py` `dispatch()`, which type-coerces **nothing**.
Args flow into `GameState` methods (`src/civ_mcp/game_state.py`) which call Lua
*builder* functions (`src/civ_mcp/lua/*.py`) — and sometimes interpolate args a
second time in inline verify-query f-strings of their own. Builders assemble Lua
source strings executed over FireTuner. Any untrusted value spliced into a bare
Lua context (a `"…"` literal, a `["…"]` table index, or a numeric position)
lets a crafted value break out and execute arbitrary Lua. This is the exact
capability whose removal motivated pulling `run_lua`; the arena must uphold it.

The complete, verified site inventory is in
`.superpowers/sdd/lua-injection-inventory.md` (Class S = 21 string params /
~30 sinks; Class N = 4 numeric id/coord params / ~23 sinks; Class OK =
already-safe). This spec references it as the authoritative site map; the plan
will enumerate exact placements from it.

## Design decisions (approved)

1. **Enforcement = self-defense at the innermost function that dominates all of
   a param's Lua sinks.** For almost every param that is the **GameState method
   entry** (`game_state.py`): validating the arg once at method entry covers
   both the builder it calls *and* any inline verify-query it interpolates
   itself, and defends both the arena and the FastMCP/server callers. (This is
   the completeness-driven realization of "builder-level self-defense" — pure
   leaf-builder validation would miss the `game_state.py` inline sinks for
   `tech`/`civic_name`/`governor_type`/`promotion_type`.) Three convergent
   exceptions validate at the interpolating builder/helper instead, because the
   untrusted value only takes its final shape there:
   - trade **resource tokens** — validated per-token in `_lua_deal_item`
     (`lua/diplomacy.py`), after the registry comma-split.
   - **existing builder self-defenses are retained** as defense-in-depth
     (`build_form_formation`, `build_move_great_work`,
     `build_resolve_city_capture`, the `_SPY_OP_HASHES` guard) — not removed.
2. **Scope = the whole remaining surface**: Class S (string enums + the one
   free-text param) **and** Class N (numeric id-family + the missed
   `set_city_production` coordinate pair). Nothing injection-related is left
   deferred after this pass.

## Mechanisms

Three small helpers in `src/civ_mcp/lua/_helpers.py` (next to `_int`,
`_unit_index`), imported where needed (including `game_state.py`):

- **`_safe_enum(value: str, field: str = "value") -> str`** — returns `value`
  unchanged iff it matches `^[A-Za-z0-9_]+$`; otherwise raises
  `ValueError(f"invalid {field}: {value!r}")`. That charset admits every
  legitimate Civ type token (`IMPROVEMENT_FARM`, `BELIEF_…`, `RESOURCE_SILK`,
  `POSITIVE`, `DECLARE_FRIENDSHIP`, …) while making it impossible to contain
  `"`, `\`, newline, `[`, `]`, `.`, space, `(`, `;` — so a value can break out
  of neither a `"…"` literal nor a `["…"]` index. Generalizes the existing
  `building.replace("_","").isalnum()` guard and the Task-1 spy_action charset.
  Applied to the value actually interpolated (after any `.upper()`/prefix
  transform); optional params validate the resolved value
  (`_safe_enum(yield_type or "YIELD_GOLD")`).
- **`_lua_escape(value: str) -> str`** — returns the value with `\`→`\\`,
  `"`→`\"`, `\n`→`\\n`, `\r` and `\0` stripped, for interpolation *inside* an
  existing `"…"` literal (adds no surrounding quotes). Used ONLY for
  `item_name`, which legitimately carries mixed-case, space-containing display
  names ("Ancient Walls", "Qhapaq Ñan") that a charset whitelist would reject.
  A crafted `item_name` is neutralized into a harmless Lua string that matches
  no display name and falls through to the existing "not found" bail.
- **numeric coercion = plain `int(...)`** (already the established pattern) for
  Class N id params and the `set_city_production` coordinates. For the many
  `city_id` sinks that route through the shared `_lua_get_city(city_id)` helper,
  the helper additionally `int()`-casts its argument internally (mirroring how
  `_unit_index()` centralizes the unit-id fix) as defense-in-depth; the few
  direct `city_id` interpolations (e.g. `build_verify_production`,
  `build_send_envoy`) plus `other_player_id`/`city_state_player_id` are cast at
  their GameState method entry.

## Scope detail

Per the inventory (authoritative site list there):

- **Class S — `_safe_enum` at GameState method entry:** `item_type`, `tech`,
  `civic_name`, `district_type`, `wonder_name`, `focus` (post `YIELD_` prefix),
  `yield_type` (purchase + patronize), `governor_type`, `promotion_type`
  (governor + unit), `belief_type`, `religion_name`, `follower_belief`,
  `founder_belief`, `government_type`, `improvement_name`, `set_policies`
  policy-type values (inside `assignments`, validated where the slot loop reads
  each policy string), `send_diplomatic_action` `action` (closes the *live*
  `DiplomacyManager.RequestSession` splice, not just an error echo),
  `respond_to_diplomacy` `response`, `form_alliance` `alliance_type`.
- **Class S — per-token `_safe_enum` in `_lua_deal_item`:**
  `offer_resources`/`request_resources`.
- **Class S — `_lua_escape`:** `item_name` (both `set_city_production` and
  `purchase_item`; covers all 11 sink lines + the verify-readback query).
- **Class N — `int()`:** `city_id` (via hardened `_lua_get_city` + direct
  sites), `other_player_id`, `city_state_player_id`, and
  `set_city_production`'s `target_x`/`target_y`.

## Testing

- **Injection tests (per class, parametrized):** for each hardened tool, a
  crafted payload (`'X" .. evil() .. "'` for strings, `'1) print(1) --'` for
  numerics) must raise `ValueError`/`TypeError` (or, for `item_name`, be escaped
  so the generated Lua contains no unescaped `"`/breakout) **before** the value
  reaches `GameState`/Lua. Reuse the established `FakeGS` (`__getattr__` →
  `raise AssertionError("must not reach GS")`) pattern for the coercion tests,
  and builder-output assertions (no unescaped breakout chars) for the escape and
  per-token-resource cases.
- **Happy-path preservation:** a legitimate enum (`IMPROVEMENT_FARM`) still
  passes; a legitimate friendly `item_name` ("Ancient Walls") still produces the
  same builder output as today (escape is a no-op on it); legitimate numeric ids
  resolve to the same unit/city.
- **No regression:** full suite green (`uv run pytest tests/ -q`).

## Non-goals / out of scope

- No schema-driven coercion pass in `_dispatch` (explicitly ruled out) — this is
  localized, per-choke-point validation.
- `run_lua` stays removed; never re-registered.
- The WC-voting `GameState` methods `build_congress_vote` (not wired to any
  arena tool) and the `_lua_deal_item` `CITY` branch (dead code — the registry
  never builds a CITY deal item) are documented as unreachable, not hardened.
- Branch stays **UNMERGED**. The merge gate remains the live-probe checklist
  (`docs/superpowers/plans/2026-07-07-arena-slice4-live-probes.md`) + riz's
  separate-session review; live probes cover greenfield Lua APIs, not this
  Python-side validation.

## Completion criteria

- Every Class S and Class N site in the inventory is dominated by a validation.
- Full suite green.
- The residual-surface bullet in the live-probe checklist is updated to mark the
  string-literal + id-family classes **CLOSED** (with a pointer to the helpers),
  leaving only genuinely-live/greenfield probes as the merge gate.
