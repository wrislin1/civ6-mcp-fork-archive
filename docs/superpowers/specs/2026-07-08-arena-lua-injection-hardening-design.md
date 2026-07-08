# Arena Lua-Injection Hardening — Design Spec

**Date:** 2026-07-08
**Branch:** `arena-slice4-full-toolset` (worktree; stays UNMERGED)
**Status:** design approved; revised per spec review (findings 1–4 + wording)

## Goal

Close the remaining LLM-argument → Lua-injection surface in the arena so the
`arena-slice4-full-toolset` branch can clear its security sign-off. After the
prior review-fix wave, all **numeric unit** args are coerced; this pass closes
the **string-enum** class, the **one free-text** param, and the **residual
numeric id-family** (plus one missed coordinate pair) — leaving no untrusted arg
reaching a bare Lua context through the arena.

## Background / threat model

In-process ("local") LLM civs supply tool arguments through
`src/civ_mcp/arena/registry.py` `dispatch()`, which type-coerces **nothing**.
Args flow into `GameState` methods (`src/civ_mcp/game_state.py`) which call Lua
*builder* functions (`src/civ_mcp/lua/*.py`) — and sometimes interpolate args a
second time in inline verify-query f-strings of their own. Builders assemble Lua
source strings executed over FireTuner via `self.conn.execute_read` /
`self.conn.execute_write`. Any untrusted value spliced into a bare Lua context
(a `"…"` literal, a `["…"]` table index, or a numeric position) lets a crafted
value break out and execute arbitrary Lua. This is the exact capability whose
removal from the arena/puppet toolset motivated the sandbox layering; the arena
must uphold it.

**Threat model = the untrusted arena path only.** The human-facing FastMCP
server intentionally retains `run_lua` (`server.py:2864`) as a debugging escape
hatch; that is out of scope here (see Non-goals).

The complete, verified site inventory is in
`.superpowers/sdd/lua-injection-inventory.md` (Class S = 21 string params /
~30 sinks; Class N = 4 numeric id/coord params / ~23 sinks; Class OK =
already-safe). This spec references it as the authoritative site map; the plan
enumerates exact placements from it.

## Design decisions (approved)

1. **Enforcement = self-defense at the innermost function that dominates all of
   a param's Lua sinks.** For almost every param that is the **GameState method
   entry** (`game_state.py`): validating the arg once at method entry covers
   both the builder it calls *and* any inline verify-query it interpolates
   itself. (Completeness-driven realization of "builder-level self-defense":
   pure leaf-builder validation would miss the `game_state.py` inline sinks for
   `tech`/`civic_name`/`governor_type`/`promotion_type`.) Three convergent
   exceptions validate at the builder/helper instead, because the untrusted
   value only takes its final per-item shape there:
   - trade **resource tokens** — validated per-token in `_lua_deal_item`
     (`lua/diplomacy.py`), after the registry comma-split.
   - **existing builder self-defenses are retained** as defense-in-depth
     (`build_form_formation`, `build_move_great_work`,
     `build_resolve_city_capture`, the `_SPY_OP_HASHES` guard) — not removed.
   - `set_city_production`/`set_policies` receive a **sanitized copy** of their
     structured input at the GameState method entry (see below) — still
     method-entry, not a builder exception.
2. **Scope = the whole remaining arena surface**: Class S (string enums, the
   small closed-domain enums, and the one free-text param) **and** Class N
   (numeric id-family + the missed `set_city_production` coordinate pair).
   Nothing injection-related is left deferred after this pass.

## Mechanisms

Validation primitives in `src/civ_mcp/lua/_helpers.py` (alongside `_int`;
generalizing the `_unit_index` centralization that today lives in
`arena/registry.py`), imported where needed (including `game_state.py`):

- **`_safe_enum(value, field="value") -> str`** — returns `value` unchanged iff
  it matches `^[A-Za-z0-9_]+$`; else raises `ValueError(f"invalid {field}:
  {value!r}")`. That charset admits every legitimate Civ **GameInfo-table** type
  token (`IMPROVEMENT_FARM`, `BELIEF_…`, `RESOURCE_SILK`, hundreds of them) while
  making it impossible to contain `"`, `\`, newline, `[`, `]`, `.`, space, `(`,
  `;` — so a value can break out of neither a `"…"` literal nor a `["…"]` index.
  Used where the valid set is a large engine table that cannot be enumerated.
- **`_one_of(value, allowed, field="value") -> str`** — upper-cases `value`,
  returns it iff `value.upper() in allowed` (a `frozenset`), else raises
  `ValueError`. A **closed allowlist** — strictly stronger than `_safe_enum`: it
  prevents breakout **and** rejects safe-shaped-but-invalid values before they
  reach a live game API. Used for the small, stable, fully-enumerable domains
  (finding 4), so e.g. a bogus `action` never reaches
  `DiplomacyManager.RequestSession` via the `session_string_map.get(action,
  action)` raw fallback (`lua/diplomacy.py:446,513`).
- **`_lua_escape(value) -> str`** — returns the value with `\`→`\\`, `"`→`\"`,
  `\n`→`\\n`, `\r`/`\0` stripped, for interpolation *inside* an existing `"…"`
  literal (adds no surrounding quotes). Used ONLY for `item_name`, which
  legitimately carries mixed-case, space-containing display names ("Ancient
  Walls", "Qhapaq Ñan") that a charset whitelist would reject. A crafted
  `item_name` is neutralized into a harmless Lua string that matches no display
  name and falls through to the existing "not found" bail.
- **numeric coercion = plain `int(...)`** for Class N id params and the
  `set_city_production` coordinates. The shared `_lua_get_city(city_id)` helper
  additionally `int()`-casts its argument internally (mirroring `_unit_index`)
  as defense-in-depth for the many `city_id` sinks that route through it; the
  few direct `city_id` interpolations (`build_verify_production`,
  `build_send_envoy`) plus `other_player_id`/`city_state_player_id` are cast at
  their GameState method entry.

**Primitive-per-param taxonomy:**
- **`_one_of` (closed allowlist):** `send_diplomatic_action` `action`
  (allowlist derived from the builder's own `session_string_map` keys + the
  `DECLARE_*_WAR` pattern, so it stays in sync), `respond_to_diplomacy`
  `response` (`{POSITIVE, NEGATIVE, EXIT}` per the builder's handled values),
  `form_alliance` `alliance_type` (`{MILITARY, RESEARCH, CULTURAL, ECONOMIC,
  RELIGIOUS}`), `purchase_item` `item_type` (`{UNIT, BUILDING, DISTRICT,
  PROJECT}`), `yield_type` (`{YIELD_GOLD, YIELD_FAITH}`).
- **`_safe_enum` (charset whitelist):** the large-table params — `tech`,
  `civic_name`, `district_type`, `wonder_name`, `focus` (post `YIELD_` prefix),
  `governor_type`, `promotion_type` (governor + unit), `belief_type`,
  `religion_name`, `follower_belief`, `founder_belief`, `government_type`,
  `improvement_name`, and `set_policies` policy-type values, and the trade
  resource tokens (per-token in `_lua_deal_item`).
- **`_lua_escape`:** `item_name`.
- **`int()`:** `city_id`, `other_player_id`, `city_state_player_id`,
  `set_city_production` `target_x`/`target_y`.

## Enforcement detail (per structured-input tool)

- **`set_policies`** — at `GameState.set_policies` entry, build a sanitized
  `assignments` dict `{int(slot): _safe_enum(pol) if pol.upper() != "NONE" else
  pol}` and use that sanitized dict for BOTH `build_set_policies(...)` and the
  post-verify comparison loop. (`NONE` is the empty-slot sentinel and passes
  `_safe_enum` anyway; keeping the branch explicit documents intent.) This
  replaces the ambiguous "validate in the slot loop" builder placement.
- **`set_city_production`** — at the GameState method entry, `int()` `city_id`,
  `target_x`, `target_y`; `_lua_escape` `item_name`; validate `yield_type` via
  `_one_of`. The sanitized values feed both the builder and the
  `build_verify_production` readback.

## Testing

- **Injection tests (GameState-entry, parametrized):** because validation lives
  *inside* GameState, tests construct a **real** `GameState(conn)` where `conn`
  is a fake whose `execute_read`/`execute_write` (signature `(lua,
  timeout=5.0)`) raise `AssertionError("Lua executed — validation should have
  raised first")` if reached — modeled on the existing `FakeConn` in
  `tests/arena/test_coordinator_router.py:5`. For each hardened method, a crafted
  payload (`'X" .. evil() .. "'` for strings, `'1) print(1) --'` for numerics)
  must raise `ValueError`/`TypeError` before any `execute_*` call. (The
  registry-wrapper `FakeGS` "must not reach GS" pattern is NOT used here — the
  point is the value *does* reach GameState and is rejected there.)
- **Builder-output tests** (no GameState needed) for the two builder-level
  cases: `_lua_escape(item_name)` output contains no unescaped `"`/breakout for a
  crafted name, and `_lua_deal_item` raises on a crafted resource token
  after the comma-split.
- **Happy-path preservation:** a legit enum (`IMPROVEMENT_FARM`) passes; a legit
  friendly `item_name` ("Ancient Walls") still produces the same builder output
  (escape is a no-op on it); legit numeric ids resolve to the same unit/city;
  every listed closed-allowlist value is accepted.
- **Existing-test migration:** validating at GameState entry means a
  deliberately-invalid enum now raises `ValueError` instead of returning the old
  `ERR:…NOT_FOUND` Lua bail. Any existing test that fed a bad enum and asserted
  that bail flips to asserting the raise — a legitimate, expected update (same
  shape as the Task-5 test adjustments). The plan calls these out per task.
- **No regression:** full suite green (`uv run pytest tests/ -q`).

## Non-goals / out of scope

- No schema-driven coercion pass in `_dispatch` (explicitly ruled out) — this is
  localized, per-choke-point validation.
- `run_lua` is **not exposed through the arena registry / puppet toolset**
  (never registered; removed server-side under `CIV_MCP_ARENA_PUPPET`, the
  decisive sandbox layer per `arena/cli_agent.py`). The **human-facing FastMCP
  `run_lua`** (`server.py:2864`) is an intentional debugging escape hatch and is
  **out of scope** for this pass; it is not part of the untrusted-arena threat
  model. (The GameState-entry validation still runs on the server code path as
  defense-in-depth, but the server is not the surface being locked down here.)
- The WC-voting `GameState` method `build_congress_vote` (not wired to any arena
  tool) and the `_lua_deal_item` `CITY` branch (dead code — the registry never
  builds a CITY deal item) are documented as unreachable, not hardened.
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
