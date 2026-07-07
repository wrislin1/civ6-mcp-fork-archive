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
   (`src/civ_mcp/arena/registry.py`, 72 tools today); `cli-claude`/`cli-codex` civs get
   the real MCP server toolset (76 tools, minus the `run_lua` sandbox removal). Parity
   and gating are registry-layer work; each NEW system tool lands in **both** layers.
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
- `ToolDef` gains optional `requires: str | None` naming a snapshot flag.
  `openai_tools(names, caps)` drops tools whose flag is false; no flag = always exposed.
- `resolve_tools("full")` semantics unchanged (all registered names); gating filters at
  schema-build time, so pinned-list A/B configs and `tools: full` both gate uniformly.

**Fail-open.** Snapshot failure (Lua error, parse failure, disconnect) degrades to the
FULL toolset with a stderr `[arena]` log line — an ungated tool costs churn; an
over-closed gate silently removes an ability. Mirrors the degrade-not-abort doctrine.

**Gate table.**

| Flag | Gates | Source of truth |
|---|---|---|
| `spies` | `get_spies`, `spy_action` | CIVIC_DIPLOMATIC_SERVICE |
| `government` | `change_government` | CIVIC_CODE_OF_LAWS |
| `religious_unit` | `spread_religion` | owns ≥1 missionary/apostle/inquisitor |
| `gp_unit` | `activate_great_person` | owns ≥1 Great Person unit |
| `corps` | `form_corps` | CIVIC_NATIONALISM |
| `army` | `form_army` | CIVIC_MOBILIZATION |
| `air` | `rebase_unit` | TECH_FLIGHT or owns ≥1 air unit |
| `archaeology` | `excavate_artifact` | TECH_NATURAL_HISTORY |
| `great_works` | `get_great_works`, `move_great_work` | owns ≥1 great work |
| *(none)* | readouts: strategic map, notifications, gossip, loyalty, climate | always exposed |

**CLI civs are not gated** (they see MCP server tools directly). Acceptable: frontier
models handle "not yet unlocked" replies gracefully; gating's target is small local
models' step churn.

## Section 2 — Parity Tools (registry wiring only; backing methods exist)

Newly exposed to `local` civs: `get_spies`, `spy_action` (single tool, action enum
mirroring the server), `change_government`, `spread_religion`, `activate_great_person`,
`get_strategic_map`, `get_notifications`.

Deliberately NOT exposed to puppets, with reasons recorded here:
- `end_turn` — the coordinator owns turn-end (`finish_units` + `restore_local`).
- `get_diary` — puppets use standing memory (Slice 3), not the human diary.
- Save/load/lifecycle (`list_saves`, `load_save`, `kill_game`, `launch_game`,
  `restart_and_load`, `load_save_from_menu`, `load_game_save`) — game-lifecycle control
  stays with the operator.
- `run_lua` — the decisive sandbox layer stays removed.
- `dismiss_popup` — puppets don't render popups; autoresolve + the orphan-session sweep
  (`7875728`) own blocking UI.

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
4. **Great Works** — `get_great_works` (works, slots, theming status per building) +
   `move_great_work(work_id, target_city, building, slot)`. Gated `great_works`.
5. **Formations** — `form_corps(unit_id, merge_unit_id)`, `form_army(unit_id, merge_unit_id)`
   via `UnitCommandTypes.FORM_CORPS` / `FORM_ARMY`. Gated `corps` / `army`.
6. **Air operations** — `rebase_unit(unit_id, x, y)`; air attacks reuse the existing
   `attack_unit` path (ranged attacks to the engine). Gated `air`.
7. **Archaeology** — `excavate_artifact(unit_id, x, y)` (`UNITOPERATION_EXCAVATE`);
   archaeologist purchase already works via `purchase_item`. Gated `archaeology`.

**Live-verification protocol.** Systems 1, 3, 4–7 depend on Lua APIs only confirmable
against a running game, and the watcher owns the single FireTuner connection until the
current 50-turn run ends. Therefore: implementation + tests build against *recorded
fixtures* (offline-testable); a separate per-system **live-probe checklist** task batch
runs when the box frees up. An API that doesn't pan out live degrades its tool to
readout-only or gets cut with a note in the spec — never silently faked.

## Section 4 — Completion Cap

`MAX_COMPLETION_TOKENS` 3072 → **6144** in `src/civ_mcp/arena/backends.py`; the
`test_caps_are_bounded` upper bound widens to 8192. Rationale: largest observed legit
step ~1,900 tokens; one live step hit 3,072 (truncated tool call → gateway 500 → the
`37a48ef` crash). 6144 ≈ 3× observed max; a runaway step stalls ~3 min worst case at
local speeds. No experiment-config changes: `tools: full` auto-includes new registry
entries; pinned-list configs unaffected.

## Section 5 — Metrics, Vocab, Playbook

- New tool names added to `vocab.py` and analyze counters, plus a mirror-consistency
  test so the lists cannot drift (closes a known review nit on `_count_tool_calls`).
- Playbook (condensed): 1–2 lines per new system (when to form corps, rebase logic,
  dig priority, theming).
- **No new briefing sections** — sensorium/digest work is roadmap B, out of scope.

## Out of Scope

- LLM↔LLM interaction, autonomous seat 0 (roadmap A — next slice).
- Scenario/perturbation tooling (roadmap C), sensorium arms (roadmap B).
- Deterministic autonomy ("old Slice 4 Option 2") — displaced by this slice, still open.
- New briefing sections; CLI-civ gating.

## Success Criteria

1. A `tools: full` local civ's per-turn schema set contains only tools its game state
   can execute; the same civ at Flight+Nationalism sees air + corps tools appear.
2. All parity tools callable by a local civ in a live run (spy mission, government
   change, religion spread, GP activation observed in transcripts).
3. Each new system tool returns real data against a live game (probe checklist passed)
   or is explicitly degraded/cut with a recorded reason.
4. Full test suite green (baseline 709 + new); box venv green; snapshot failure
   demonstrably fails open (test-pinned).
5. No experiment YAML changes required for existing configs to benefit.
