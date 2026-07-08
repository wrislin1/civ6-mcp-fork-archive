# Arena Slice 4 — Full Toolset & Era-Gated Abilities (Design)

**Date:** 2026-07-07
**Status:** Approved by riz (brainstorming session, this date)
**Predecessor:** Slice 3 (standing memory + task tracker), merged at `7ade24c`, hardened through `7875728`.

## Context & Motivation

The live 8-civ / 3×gemma4-26b 50-turn run (run_id `8civ-3gemma-50r-20260707T164226Z`)
validated Slice 3: noticeably better decisions at acceptable per-move latency. Roadmap
item **D — wider game-system tools** (decided 2026-07-06: D → A → C → B) is the next
slice. riz chose the **full D scope** in one slice: close every parity gap AND build all
new game-system tools, with era/state gating and the noted completion-cap raise.

Two structural facts anchor the design:

1. **Two tool layers.** In-process `local` civs get tools from the arena registry
   (`src/civ_mcp/arena/registry.py`); `cli-claude`/`cli-codex` civs get the real MCP
   server toolset (minus the `run_lua` sandbox removal). Exact tool counts drift and
   are test-backed, not restated here. Parity and gating are registry-layer work;
   each NEW system tool lands in **both** layers.
2. **Context is not a constraint.** gemma4-26b runs at `--ctx-size 131072` on both
   gateways (verified in `/opt/brothereye/infra/llama-swap/config.*.yaml`); current
   prompts are ~12–16k tokens. Gating exists for invalid-call churn and attention
   quality, not context survival.

## Decisions (riz, this session)

| Decision | Choice |
|---|---|
| Slice scope | **Full D**: parity + cap + all new game-system tools |
| Tool exposure | **Era/state-gated** via per-turn capability snapshot |
| `MAX_COMPLETION_TOKENS` | **6144** (was 3072; one live truncation observed) |
| Structure | **Approach A** — phased, value-first: cap → parity → gating → systems |
| Old "Slice 4 Option 2 deterministic autonomy" | **Displaced**, not merged — remains open for a later slice |

## Section 1 — Capability Snapshot & Era Gating

**Mechanism.**
- Once per puppet turn the coordinator runs one cheap `execute_read` Lua query — ~15
  `HasTech` / `HasCivic` / unit-class checks — emitting one line:
  `CAPS|spies=0|government=1|religious_unit=0|gp_unit=0|corps=0|army=0|air=0|archaeology=0|great_works=0`
  parsed into a flags dict.
- `ToolDef` gains optional `requires: str | None` naming a snapshot flag. A single
  `filter_tools(names, caps) -> tuple[str, ...]` drops names whose flag is false
  (no flag = always exposed).
- **The filtered list is the agent's only tool vocabulary.** `LLMPolicy` computes
  `visible_tool_names = filter_tools(self._tool_names, caps)` once per turn and uses
  it for all three consumers: the schema build (`openai_tools(visible_tool_names)`),
  invalid-call classification (`agent.py:175`), and the `_dispatch(..., allowed=...)`
  execution allowlist (`agent.py:186`). Filtering only the schema would leave gated
  tools silently callable.
- `resolve_tools("full")` semantics unchanged (all registered names); gating filters
  downstream of resolution, so pinned-list A/B configs and `tools: full` gate uniformly.

**Fail-open.** Snapshot failure (Lua error, parse failure, disconnect) degrades to the
FULL toolset with a stderr `[arena]` log line — an ungated tool costs churn; an
over-closed gate silently removes an ability. Mirrors the degrade-not-abort doctrine.

**Gate principle: action tools gate on *executable-now* state** — the unlock AND the
game objects the action needs — so an exposed tool can always be legally invoked.
Unlock-only gates (e.g. a flight tech with no aircraft) would re-introduce exactly the
invalid-call churn gating exists to remove. Discoverability is not lost: production
lists and the playbook carry the "build toward it" knowledge; the tool appears on the
turn after the prerequisite unit/object exists (the snapshot is per-turn).

**Gate table.**

| Flag | Gates | Source of truth |
|---|---|---|
| `spies` | `get_spies`, `spy_action` | CIVIC_DIPLOMATIC_SERVICE |
| `government` | `change_government` | CIVIC_CODE_OF_LAWS |
| `religious_unit` | `spread_religion` | owns ≥1 missionary/apostle/inquisitor |
| `gp_unit` | `activate_great_person` | owns ≥1 Great Person unit |
| `corps` | `form_corps` | CIVIC_NATIONALISM **and** owns ≥2 same-type land military units |
| `army` | `form_army` | CIVIC_MOBILIZATION **and** owns ≥1 corps-formation unit |
| `air` | `rebase_unit` | owns ≥1 air unit |
| `archaeology` | `excavate_artifact` | owns ≥1 archaeologist with ≥1 charge (Natural History is a **civic** — CIVIC_NATURAL_HISTORY — and is implied by ownership) |
| `great_works` | `move_great_work` | owns ≥1 great work |
| *(none)* | readouts: strategic map, notifications, gossip, loyalty, climate, `get_great_works` (slot visibility matters before the first work exists) | always exposed |

**CLI civs are not gated** (they see MCP server tools directly). Acceptable: frontier
models handle "not yet unlocked" replies gracefully; gating's target is small local
models' step churn.

## Section 2 — Parity Tools (registry wiring only; backing methods exist)

Newly exposed to `local` civs: `get_spies`, `spy_action` (single tool, action enum
mirroring the server), `change_government`, `spread_religion`, `activate_great_person`,
`get_strategic_map`, `get_notifications`.

**Naming note.** `activate_great_person` and `spread_religion` are **local flat
wrappers over existing GameState methods** (`gs.activate_great_person`,
`gs.spread_religion`); the MCP server reaches those same methods through
`unit_action(action="activate"|"spread_religion")`. The flat form is a small-model
ergonomics choice, not name-for-name server parity. All new unit-taking registry
tools accept the composite `unit_id` and convert via `_unit_index()`
(`registry.py:152`), matching the existing wrapper convention — this locks in for
`activate_great_person`, `spread_religion`, `form_corps`, `form_army`, `rebase_unit`,
and `excavate_artifact`.

Deliberately NOT exposed to puppets, with reasons recorded here:
- `end_turn` — the coordinator owns turn-end (`finish_units` + `restore_local`).
- `get_diary` — puppets use standing memory (Slice 3), not the human diary.
- Save/load/lifecycle (`list_saves`, `load_save`, `kill_game`, `launch_game`,
  `restart_and_load`, `load_save_from_menu`, `load_game_save`) — game-lifecycle control
  stays with the operator.
- `run_lua` — the decisive sandbox layer stays removed.
- `dismiss_popup` — not registered in the arena registry (local civs); autoresolve +
  the orphan-session sweep (`7875728`) own blocking UI. The server-side CLI-civ
  toolset still includes it today (`_ARENA_PUPPET_TOOLS` at `server.py:378` removes
  only lifecycle, `end_turn`, `run_lua`) — left unchanged; CLI-civ gating is out of
  scope.

## Section 3 — New D Systems

Uniform pipeline per system: Lua builder + parser (models) → GameState method → MCP
server tool → registry `ToolDef` (+ `requires`) → narrator → fixture tests. Ordered
cheapest-first so value lands early:

1. **Gossip & grievances** — `get_gossip`: recent gossip entries per met civ + grievance
   levels both directions. Foundation: `GetGrievancesAgainst` already parsed in
   `get_diplomacy` (`src/civ_mcp/lua/diplomacy.py:40`); the gossip log API needs live
   probing. Always on.
2. **Loyalty detail** — `get_loyalty`: per-city loyalty, per-turn delta, pressure-source
   breakdown (`CulturalIdentity` API: `GetLoyalty`, `GetLoyaltyPerTurn`, identity-source
   breakdown). Today's `get_cities` shows a summary number only. Always on.
3. **Climate & disasters** — `get_climate`: climate phase, sea level, active/recent
   disasters with tile positions (`Game.GetClimate` area; greenfield, live probing).
   Always on.
4. **Great Works** — `get_great_works` (works, slots, theming status per building;
   always-on readout) + `move_great_work(work_id, target_city, building, slot)`,
   gated `great_works` (owns ≥1 great work).
5. **Formations** — `form_corps(unit_id, merge_unit_id)`, `form_army(unit_id, merge_unit_id)`
   via `UnitCommandTypes.FORM_CORPS` / `FORM_ARMY`. Gated `corps` / `army`.
6. **Air operations** — `rebase_unit(unit_id, x, y)`; air attacks reuse the existing
   `attack_unit` path (ranged attacks to the engine). Gated `air`.
7. **Archaeology** — `excavate_artifact(unit_id, x, y)` (`UNITOPERATION_EXCAVATE`);
   archaeologist purchase already works via `purchase_item`. Gated `archaeology`.

**Live-verification protocol.** Systems 1, 3, 4–7 depend on Lua APIs only confirmable
against a running game, and the watcher owns the single FireTuner connection until the
current 50-turn run ends. Therefore: Lua builders, parsers, GameState methods, and
tools build in-branch against *synthetic fixtures* (offline-testable) — but a
synthetic fixture proves the parser, **not that the Civ API exists**. The slice's
merge gate is the per-system **live-probe checklist**: no greenfield-backed tool
reaches a live run until its probe has captured a real fixture, or the spec records a
degrade/cut decision for it. (**Status update 2026-07-08:** riz directed the branch
merged to `main` — all four copies at `0de49fb` — *before* the probes ran, so the
checklist is now a **post-merge test gate**: the greenfield tools ship in `main` but
remain live-unproven until `docs/superpowers/plans/2026-07-07-arena-slice4-live-probes.md`
is worked against a late-game save. Treat those tools as provisional in live runs
until then.) An API that doesn't pan out live degrades its tool to readout-only or
gets cut with a note here — never silently faked. Parity tools (Section 2) are exempt: their GameState methods are already
live-proven via the MCP server.

**Live-probe outcomes (2026-07-08, turn-380 Future-era Gathering-Storm game).**
The checklist was worked; three systems resolved to degrade/cut decisions here
(the rest captured real fixtures — see the checklist and
`tests/test_live_probe_fixtures.py`):

- **Climate sea-level — DEGRADE.** `GetSeaLevel` and its 3 candidate alternatives
  are all nil in the tuner context; `get_climate` reports `sea_level = -1`
  (explicit sentinel, inside a `pcall`). Phase + CO2 + disasters are solid.
- **Loyalty pressure-source breakdown — DEGRADE.** `GetLoyaltyBreakdown` is nil;
  `get_loyalty` omits `LOYSRC` lines and `sources` degrades to `[]`. Per-city
  loyalty / max / per-turn delta are solid.
- **`move_great_work` — CUT to readout.** `UI.MoveGreatWork`, `Game.GetGreatWorks`,
  and `GreatWorksManager` are all nil; no working move API exists in the tuner
  context. The builder returns an informative `UNAVAILABLE:` line (never a crash),
  and the tool description + playbook mark it unavailable. The **query**
  (`get_great_works`) is fully retained.
- **Gossip / excavate — FIXED (not degraded).** Gossip now emits `entry[1]` text
  capped newest-first (15/civ); excavate uses the hardcoded op hash `1548958412`
  (`UnitOperationTypes.EXCAVATE` is nil in the tuner context).

## Section 4 — Completion Cap

`MAX_COMPLETION_TOKENS` 3072 → **6144** in `src/civ_mcp/arena/backends.py`; the
`test_caps_are_bounded` upper bound widens to 8192. Rationale: largest observed legit
step ~1,900 tokens; one live step hit 3,072 (truncated tool call → gateway 500 → the
`37a48ef` crash). 6144 ≈ 3× observed max.

**Timeout/retry interplay.** The 3072-era comment "the timeout should essentially
never fire" no longer holds: at local speeds (~25–35 tok/s on a 3090) a 6144-token
generation runs 3–4 minutes, past `REQUEST_TIMEOUT_S = 120`. Decisions:

- `REQUEST_TIMEOUT_S` 120 → **300**, so the token cap — not the clock — bounds a
  legitimate long step.
- **Timeout-class failures are not retried** (`openai.APITimeoutError` re-raises
  immediately): a 300-second timeout at this cap means runaway generation, and
  resampling a runaway 3× would stall one seat ~15 minutes. Other transient classes
  (gateway 500 on malformed tool JSON, llama-swap 503 during model load, network
  blips) keep the existing 3-attempt retry.
- Worst-case single-step stall: ~5 min (one 300 s attempt), then the coordinator's
  degrade guard skips the turn.

No experiment-config changes: `tools: full` auto-includes new registry entries;
pinned-list configs unaffected.

## Section 5 — Metrics, Vocab, Playbook

- New tool names added to `vocab.py` and analyze counters, plus a mirror-consistency
  test so the lists cannot drift (closes a known review nit on `_count_tool_calls`).
- Playbook (condensed): 1–2 lines per new system (when to form corps, rebase logic,
  dig priority, theming).
- **No new briefing sections** — sensorium/digest work is roadmap B, out of scope.

## Section 6 — File Map

| File | Role in this slice |
|---|---|
| `src/civ_mcp/arena/capabilities.py` (new) | `build_caps_query()` Lua + `parse_caps()` → flags dict; fail-open default |
| `src/civ_mcp/arena/registry.py` | `ToolDef.requires`, `filter_tools(names, caps)`, parity + system ToolDefs |
| `src/civ_mcp/arena/agent.py` | per-turn `visible_tool_names` used for schema, invalid-call classification, and dispatch |
| `src/civ_mcp/arena/coordinator.py` | fires the caps snapshot each puppet turn; passes caps to the policy |
| `src/civ_mcp/arena/backends.py` | 6144 cap, 300 s timeout, no-retry-on-timeout |
| `src/civ_mcp/arena/vocab.py` + `analyze.py` | tool-name mirrors/counters + mirror-consistency test |
| `src/civ_mcp/arena/playbook.md` | 1–2 lines per new system |
| `src/civ_mcp/lua/diplomacy.py` | gossip & grievances builder/parser |
| `src/civ_mcp/lua/cities.py` | loyalty-detail builder/parser |
| `src/civ_mcp/lua/climate.py` (new) | climate/disasters builder/parser |
| `src/civ_mcp/lua/great_works.py` (new) | great-works query + move builders/parsers |
| `src/civ_mcp/lua/units.py` | form-corps/army, rebase, excavate builders |
| `src/civ_mcp/lua/models.py` | new dataclasses (gossip, loyalty, climate, great works) |
| `src/civ_mcp/lua/__init__.py` | re-exports for all new builders/parsers/models |
| `src/civ_mcp/game_state.py` | one method per new tool |
| `src/civ_mcp/narrate.py` | narrators for the new readouts |
| `src/civ_mcp/server.py` | MCP tools for the 7 systems |
| `tests/arena/test_capabilities.py` (new) | CAPS parser, gate table, fail-open |
| `tests/arena/test_registry.py`, `tests/arena/test_agent.py` | gating + parity + composite-id coverage |
| `tests/test_parsers.py` | new-domain parser fixture tests |
| this spec | live-probe results / degrade-cut decisions recorded here |

## Out of Scope

- LLM↔LLM interaction, autonomous seat 0 (roadmap A — next slice).
- Scenario/perturbation tooling (roadmap C), sensorium arms (roadmap B).
- Deterministic autonomy ("old Slice 4 Option 2") — displaced by this slice, still open.
- New briefing sections; CLI-civ gating.

## Success Criteria

1. A `tools: full` local civ's per-turn schema set contains only tools its game state
   can execute, and gated names are also refused at dispatch (one visible list feeds
   schema, classification, and dispatch). The same civ sees `form_corps` appear at
   Nationalism + a same-type pair, and `rebase_unit` appear with its first aircraft.
2. All parity tools callable by a local civ in a live run (spy mission, government
   change, religion spread, GP activation observed in transcripts).
3. Each new system tool returns real data against a live game (probe checklist passed)
   or is explicitly degraded/cut with a recorded reason.
4. Full test suite green (baseline 709 + new); box venv green; snapshot failure
   demonstrably fails open (test-pinned).
5. No experiment YAML changes required for existing configs to benefit.
