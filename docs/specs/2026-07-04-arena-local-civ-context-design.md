# Arena Local-Civ Context Design

> **Status (2026-07-04): IMPLEMENTED on branch arena-local-civ-context (pending live gates + user review).**
> Companion plan: `docs/plans/2026-07-04-arena-local-civ-context.md`.

## Problem

Live baseline runs (`baseline-20t-20260704T145456Z`) confirmed a ~100× data asymmetry
between local in-process civs and CLI civs:

| | Local civs (`agent.py`) | CLI civs (`cli_agent.py`) |
|---|---|---|
| Tools | 9 hand-rolled (no map, no tech tree, no advisors, **no attack/improve/purchase**) | full civ6 MCP surface (~70 tools minus denied) |
| Tool-result cap | 1,500 chars (`MODEL_FEED_CHAR_CAP`) | none |
| Steps per turn | 6 (`max_steps`) | 40 agent turns |
| Strategy knowledge | 3-line system prompt | full `CLAUDE.md` playbook via project discovery |
| Measured prompt size | ~900–3,000 tokens/call | 200–350k tokens/turn |

Meanwhile the local models have real headroom: `gemma4-26b` is served at 131,072 ctx;
`qwen3.6-27b` at 32,768 (raisable). The in-play roster is every ~16–20 GB single-GPU
model in the llama-swap per-GPU configs (gemma4-26b/31b, qwen3.6-27b/35b,
qwen3-coder-30b, deepseek-r1-32b, devstral-24b, …).

## Goal

Improve local-civ decision quality by closing the context gap, **as an experiment
platform**: every lever (toolset, briefing, caps, playbook, context budget) is a per-civ
config knob so configurations can be A/B tested. Target filling each model's real
context window (`context_budget: auto`).

## Non-goals

- No changes to CLI civ behavior, hook/coordinator seize-restore mechanics, or watcher
  operating pattern (scripts only gain a `--config` passthrough).
- No new eval framework — comparison uses existing transcripts + `analyze.py` additions.
- No N-civ scaling, replay UI, or fine-tuning work.

## Architecture

**Hybrid push + pull.** The host pre-assembles a rich **turn briefing** (push) injected
as the opening user message, and the model keeps a configurable **toolset** (pull +
actions) for follow-up queries and moves.

**One new concept:** `CivOptions` — per-civ experiment knobs carried on `PlayerSpec`.
**One new component:** the briefing builder (`briefing.py`).
**One reshaped component:** the tool registry (`registry.py`) replaces `agent.py`'s
hand-written `TOOLS` + `_dispatch` with a table of name → schema → `GameState` method,
from which named tiers are subsets.

**Rendering:** every tool result and briefing section is rendered via `civ_mcp.narrate` —
the same compact text CLI civs see through the MCP server — never Python dataclass reprs
(reprs cost ~2–3× the tokens for the same information and are noisier for 20–30B models).
This applies to all tiers including the `minimal` control: the A/B control is defined by
same tools/caps/steps/prompt, not by preserving the old accidental repr rendering.

### Experiment config file

`civ-arena --config experiments/<name>.yaml` (existing `--player` flags remain as
shorthand; both paths produce the same `ArenaConfig`).

```yaml
run_id: rich-context-v1          # optional; generated when empty
max_puppet_turns: 80             # TOTAL across seats (existing semantics)
idle_poll_limit: 3600
gateway_url: http://192.168.20.196:11444/v1   # global default
civs:
  - player: 3
    provider: local
    model: gemma4-26b
    gateway: http://192.168.20.196:11440/v1
    briefing:
      enabled: true
      map_radius: 3              # starting radius; 0 disables the map section
      sections: [overview, units, cities, map, research, production_options]
    tools: standard              # minimal | standard | full | [explicit, tool, names]
    result_char_cap: 6000        # replaces hardcoded 1500
    max_steps: 10                # replaces hardcoded 6
    playbook: condensed          # none | condensed
    context_budget: auto         # auto | <int tokens>
  - player: 1
    provider: cli-claude         # CLI civs: knobs above are rejected if present
```

Validation happens at load, before any game contact: unknown provider / tool name /
tier / section / playbook value, duplicate player ids, local-only knobs on CLI civs,
and malformed or out-of-range numeric knobs (`map_radius` must be 0–5) all fail fast
with the offending civ and field named.

### Context budgeting

```
briefing_budget_tokens = n_ctx − reserve
reserve = playbook_tokens + tool_schema_tokens
          + max_steps × (result_char_cap/3 + 512 completion) + 1024 margin
```

`n_ctx` resolution order (path recorded in the transcript):
1. `context_budget: <int>` — explicit, used as `n_ctx` directly.
2. `auto`: GET `{origin}/upstream/{model}/props` (llama-swap route), then
   `{origin}/props` (bare llama-server), reading `default_generation_settings.n_ctx`.
3. Fallback: 16,384 conservative default.

Budget is denominated in tokens; text is measured at 3 chars/token — deliberately
conservative. Civ text is identifier-dense (`TERRAIN_GRASS`, coordinates) and tokenizes
at ~3–3.3 chars/token; measuring at 4 would overestimate the budget in the direction that
blows the context window. The live smoke gate verifies empirically that the first-step
`prompt_tokens` stays under the resolved `n_ctx`.

### Briefing builder

Sections build **independently** (a failure logs to `briefing_errors` and is skipped)
and fill in priority order until the budget is spent:

1. `overview` — `get_game_overview()`
2. `units` — `get_units()`, untruncated
3. `cities` — `get_cities()`
4. `production_options` — `list_city_production(city_id)` per city (fetches cities
   itself if the `cities` section is not configured); sits directly after `cities`
   because it is small, high-value input to `set_city_production`
5. `map` — `get_map_area(x, y, radius)` around each unit and city, deduplicated;
   radius starts at `map_radius` and **auto-expands** (up to 5) while ≥25% of the
   budget remains unspent — the "fill to ceiling" lever. Expansion is a single
   predictive jump: tile count per center grows as 3r²+3r+1, so the cost of a larger
   radius is projected from the first fetch and at most one extra fetch pass runs
   (each pass is one FireTuner Lua round-trip per center)
6. `research` — `get_tech_civics()`
7. Extended, config-enabled: `empire_resources`, `rivals` (`get_rival_snapshot`),
   `threats` (`get_threat_scan`), `victory` (`get_victory_progress`)

The assembled briefing is **hard-truncated at budget** even if estimates were wrong; an
overflow must never blow the model's window. Briefing token count, section list, radius
reached, and errors are recorded per turn.

### Toolset tiers

Registry-driven; a tier is a named subset of the one `TOOL_REGISTRY` table. Dispatch is
**gated on the per-civ resolved toolset**: a tool name outside the civ's tier — even one
defined in the registry — is rejected with an `ERROR:` result, never executed. Without the
gate, a control-seat civ that hallucinates an out-of-tier tool name would silently escape
its tier and contaminate the A/B comparison.

- `minimal` — today's 9: `get_overview`, `get_units`, `get_cities`, `move_unit`,
  `found_city`, `set_city_production`, `set_research`, `fortify_unit`, `skip_unit`
  (baseline control group — same tools/caps/steps/prompt as today; results narrated
  like everything else).
- `standard` — minimal + reads `get_map_area`, `get_tech_civics`; + actions
  `attack_unit`, `improve_tile`, `remove_feature`, `purchase_item`, `heal_unit`,
  `alert_unit`, `set_civic`.
- `full` — standard + `get_settle_advisor`, `get_district_advisor`,
  `get_wonder_advisor`, `get_builder_tasks`, `get_diplomacy`, `get_city_states`,
  `get_great_people`, `get_empire_resources`, `get_victory_progress`,
  `get_pathing_estimate`, `send_envoy`, `set_policies`, `get_policies`,
  `appoint_governor`, `assign_governor`, `choose_pantheon`, `get_pantheon_status`,
  `upgrade_unit`, `promote_unit`, `get_unit_promotions`, `automate_explore`,
  `skip_remaining_units`, `purchase_tile`, `get_purchasable_tiles`, `set_city_focus`.
- Explicit list — any subset of registry names, for surgical A/B tests.

Never exposed to local civs (host-owned or unsafe): `end_turn`, save/load/lifecycle,
`execute_lua`, diplomacy session responses, World Congress voting.

### Playbook

`playbook: condensed` prepends `src/civ_mcp/arena/playbook.md` — a ~2–4k-token strategy
digest distilled from `CLAUDE.md` (turn loop, expansion/growth rules, combat basics,
district adjacency, research heuristics), with MCP-specific tool references rewritten to
match arena tool names. `none` keeps today's 3-line prompt.

### Telemetry

Per-turn transcript records gain: resolved `civ_options` fingerprint (tier, caps,
playbook, budget + resolution path), `briefing_tokens`, `briefing_sections`,
`briefing_radius`, `briefing_errors`. `analyze.py` gains a per-run config header and a
compact comparison summary (avg steps/turn, invalid-call rate, per-turn state deltas) so
two run dirs diff cleanly.

## Error handling

- Briefing section failure → skip + log, never fatal.
- Budget probe failure → explicit value → 16,384 default; path recorded.
- Config validation errors name the civ and field, and abort before connecting.
- Hard briefing truncation at budget.

## Testing

Unit: config load/validation (YAML → `ArenaConfig`, `--player` equivalence), budget
allocator math, briefing builder against a fake `GameState` (priority order, budget
fill, radius expansion, section-failure skip, hard truncation), registry tier subsets +
dispatch, `LLMPolicy` honoring per-civ caps/toolset/playbook.

Live gates, in order: (1) `--config` run in `--dry-run`; (2) one-round live smoke with a
rich config on gemma4-26b; (3) A/B — same model, `minimal`+no-briefing vs
`standard`+auto-budget, a few rounds each.

## Rollout

- **Slice 0 (environment, `/opt/brothereye`, not this repo):** per in-play model, run
  the llama.cpp memory-fit estimate and raise `--ctx-size` in
  `infra/llama-swap/config.per-gpu-{0,1}.yaml` as far as each model fits on its 3090
  (weights 16–20 GB + q8_0 KV must fit in 24 GB; some models may top out below 128k —
  `auto` budgeting adapts per model).
- **Slice 1:** experiment config file + tool registry/tiers + configurable caps +
  playbook. Independently valuable before briefings exist.
- **Slice 2:** briefing builder + auto context budget + telemetry additions.

## Known costs / risks

- 3090 prefill ≈ 1–2k tok/s: a 100k-token briefing ≈ 1–2 min first-step prefill per
  turn (in-turn steps reuse the prefix cache; the briefing changes across turns so each
  turn pays once). The budget knob exists to find the quality/latency sweet spot.
- 20–30B models are weak multi-step tool-callers; the briefing reduces their need to
  chain queries, but `full` tier + many steps may raise invalid-call rates — that is a
  measurable experiment outcome, not a defect.
