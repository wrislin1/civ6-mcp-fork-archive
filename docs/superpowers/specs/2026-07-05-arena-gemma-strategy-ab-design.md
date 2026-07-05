# Arena Gemma Strategy A/B Design

> **Status:** Draft design for user review. This slice is intentionally limited to
> experiment YAML and `src/civ_mcp/arena/playbook.md` content. No Lua, FireTuner,
> arena coordinator, analyzer, or game-logic code changes are in scope.

## Problem

The previous local-civ context work added per-seat knobs for tools, briefings, result
caps, step caps, and condensed strategy text. The next question is whether that
configuration package changes early-game behavior in a live game, especially around
known failures: not seeing the map, ignoring huts/city-states, slow expansion, missing
pantheon/government timing, and reacting to barbarians only after damage is done.

## Goal

Run a same-game, same-map A/B test using only `gemma4-26b` seats. The treatment arm
gets fuller eyes, more tools, and explicit early-game doctrine. The control arm stays
on the current minimal local-civ baseline. The result should answer one question:

> Does the full Slice 1 package improve early-game choices enough to justify the next
> slice of map-salience or analyzer code?

This is a bundle test, not an attribution test. A treatment win does not prove whether
briefing, tools, playbook, result caps, or step caps mattered most.

## Experiment Setup

- Human player: seat `0` (Gaul), manually ends each round.
- Puppet seats: `1` through `7`.
- Model: all puppet seats run `gemma4-26b`.
- Gateway: all puppet seats use GPU0 llama-swap at `http://192.168.20.196:11440/v1`.
- Run length: `max_puppet_turns: 140` (20 rounds x 7 puppet seats).
- Code scope: experiment YAML plus playbook text only.
- Analysis: run `civ-arena-analyze` after the live run, then add a short manual audit
  for outcomes the analyzer does not currently count.

### A/B Assignment

Seats are interleaved so turn order does not bias one arm toward first-come map rewards
such as goody huts and city-state first-meets.

| Seats | Arm | Tools | Briefing | Playbook |
| --- | --- | --- | --- | --- |
| `1, 3, 5, 7` | Treatment | `full` | enabled, radius 3, sections below | `condensed` |
| `2, 4, 6` | Control | `minimal` | disabled | `none` |

Average seat position is `4` for both arms. Treatment still has one extra seat, so
head-to-head interpretation must use per-seat averages and per-seat narratives, not
raw totals alone.

### Treatment Configuration

Treatment seats use the full registered local arena tool tier. As of this design,
`full` resolves dynamically to the current registry rather than a fixed count; recent
recon showed 43 tools. The spec should not rely on the count staying fixed.

Briefing sections:

```yaml
[overview, units, cities, map, research, production_options, threats, rivals, empire_resources]
```

Rationale:

- `map`, `threats`, and `empire_resources` address the sensorium gap.
- `rivals` adds score/yield/city-count context, which the control does not get pushed.
- `production_options` lets the model act on expansion doctrine without first spending
  a step discovering build choices.
- `victory` is intentionally omitted for a T20 early-game run to keep the briefing
  focused on near-term choices.

Other treatment knobs:

```yaml
tools: full
result_char_cap: 6000
max_steps: 10
playbook: condensed
context_budget: auto
briefing:
  enabled: true
  map_radius: 3
```

### Control Configuration

Control seats explicitly pin the current minimal local baseline so later defaults do not
silently change the comparison:

```yaml
tools: minimal
result_char_cap: 1500
max_steps: 6
playbook: none
context_budget: auto
briefing:
  enabled: false
```

The control is the current minimal baseline, not a byte-for-byte recreation of older
raw-dataclass-output runs; narrated rendering now applies globally.

## Playbook Additions

Add doctrine that the baseline postmortems and live observations showed were missing or
too weak:

- Goody huts: visible `IMPROVEMENT_GOODY_HUT` tiles are free rewards; move any safe unit
  onto them quickly because they are first-come.
- City-states/envoys: meeting city-states early gives first-meet value and later envoy
  leverage; use `send_envoy` when tokens are available.
- Pantheon timing: by about turn 20, check whether a pantheon can be chosen and favor
  practical early-game beliefs.
- Political Philosophy: early civics should drive toward a tier-1 government because
  4 policy slots beat the starting 2-slot government.
- Boosts: eureka and inspiration boosts are half-cost accelerators; if a small action
  unlocks one soon, prefer it over blind beelining.
- District specialization: do not make every city generic; use the first districts to
  create a focused economy and preserve future discount opportunities.
- Barbarian scouts: a barb scout that sees a city can report home and trigger raids;
  intercept or kill it before the camp escalates.

## Measurement

Use `civ-arena-analyze` for analyzer-native metrics:

- Final and per-turn score by seat and arm.
- Cities by turn 20.
- Science/culture pace.
- Successful research or production setting.
- Exploration-vs-idle rubric signals.
- Invalid-call rate.
- Average steps per turn.
- Prompt and completion tokens.
- Briefing tokens and briefing sections.
- Wall-clock seconds per puppet turn.
- Truncation incident rate.

Manual audit is required for metrics the current analyzer does not directly count:

- Goody huts observed and collected.
- City-states first-met and envoys sent.
- Barbarian camps discovered, approached, or cleared.
- Barbarian scouts intercepted before returning to camp.
- Pantheon chosen or pantheon check attempted by turn 20.
- Political Philosophy progress and government change if reached.
- Settler queued/produced by T15/T20, not just cities founded by T20.
- Obvious queue quality errors, such as low-leverage infrastructure before first settler.

## Guardrails

Preflight before arming the live watcher:

1. `http://192.168.20.196:11440/v1/models` lists `gemma4-26b`.
2. The experiment YAML parses with `load_experiment`.
3. The live watcher is started with one run id and no competing arena/Codex/MCP process.

Runtime gates to check in the first treatment transcript records:

1. `n_ctx` resolves near `131072`, not fallback `16384`.
2. `prompt_tokens < n_ctx`.
3. `briefing_errors` is empty or every error is understood.
4. `briefing_sections` contains the configured treatment sections unless budget pressure
   explains truncation.
5. Full-tool invalid-call rate and wall time are tolerable enough to finish the run.

Abort or annotate the run if treatment seats spend most steps floundering in the full
tool tier. That is a meaningful result, but the report should distinguish tool overload
from strategic weakness.

## Limitations

- Three to four seats per arm is directional only. Start positions, neighbors, huts, and
  barb camp placement create large variance.
- Treatment has one extra seat. Report per-seat averages and show seat-level tables.
- Same-map A/B creates interaction effects: one arm can consume huts, meet city-states,
  block settlements, or alter barb behavior before another seat acts.
- This slice deliberately does not add salience code. If treatment seats see huts or
  barb scouts in the briefing and still ignore them, that is evidence for a later
  map-salience/code slice rather than a failure of the experiment.

## Artifacts

- Experiment config: `experiments/gemma-strategy-ab-slice1.yaml`
- Playbook: `src/civ_mcp/arena/playbook.md`
- Analysis command after the run:

```bash
/home/riz/.local/bin/uv run civ-arena-analyze --run-id <RUN_ID>
```
