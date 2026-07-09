# Arena Attention & Turn Skipping — Quiet-Turn Fast Path (Design)

**Date:** 2026-07-09
**Status:** Approved by riz (brainstorming session, this date)
**Predecessor:** Slice 4 (full toolset + era gating), merged at `b3540d8`, docs through `39fe27c`.
**Sequencing:** riz decided this slice slots **before A (LLM↔LLM interaction)** in the D → A → C → B roadmap.

## Context & Motivation

A human player on a quiet turn just clicks Next Turn. An arena puppet cannot: every
captured puppet turn costs a full LLM invocation (up to `MAX_COMPLETION_TOKENS=6144`
/ 300s for local civs, real API spend for `cli-claude`), regardless of whether
anything needed deciding. Three compounding payoffs justify the slice:

1. **Cost & wall-clock.** The LLM invocation is the most expensive unit in the
   system; a skipped turn eliminates one entirely.
2. **The era ceiling.** `max_puppet_turns` decrements once per captured turn
   (coordinator.py, `remaining -= 1`) and is a TOTAL across seats — a 3-seat
   140-turn run dies around the Classical era, which is why the slice-4 late-game
   toolset (air, archaeology, great works, spies) is currently unreachable in normal
   runs. Making slept turns budget-free decouples run length from LLM spend.
3. **Research value.** "What wakes the agent" is the sensorium problem inverted: a
   wake-trigger vocabulary is a measurable attention policy, and its failure mode
   (false quiet → the civ sleepwalks into disaster) is precisely the failure class
   the paper documents. Turn-skipping also makes the future B-slice arms sharper:
   information gaps become real and variable-length, so digest/vision treatments
   have measurable room to matter.

Honest caveats recorded up front: quiet-turn frequency in past runs is
**unquantified** (early-era turns are rarely quiet; savings grow with era and empire
size — aligned with the era-extension goal). The detector inherits the sensorium
problem — it only sees what it queries; the mitigation is over-scanning,
wake-biased asymmetry, and the streak cap, not omniscience.

**Substrate readiness (verified first-hand, 2026-07-09):** Slice 3 built both
halves this needs. The STANDING PLAN channel (`arena/memory.py`) is persistent
model intent with battle-tested tolerant extraction; the task tracker
(`arena/task_tracker.py`) already executes multi-turn work deterministically
*before* the model runs (`run_pre_model_tasks`), including a hostile-scan interrupt
(`_visible_hostile_nearby`). The coordinator already computes a cheap per-turn
overview snapshot (`_overview_snapshot`: score/gold/science/culture/faith/
cities/units/research/civic) for transcripts — a ready-made delta detector. The
skip decision slots exactly between the pre-model task phase and the `pol(...)`
model call.

## Decisions (riz, this session)

| Decision | Choice |
|---|---|
| Framing | **A+C hybrid**: coordinator-side deterministic detector with unconditional veto (C) + model-expressed skip directives (A) |
| B (pre-committed action queues) | **Out of scope** — future incremental `TASK_KINDS` growth, evidence-driven |
| Budget accounting | `max_puppet_turns` counts **model-invoked turns only**; new `max_game_turns` caps all captured turns |
| Mode | Per-civ knob `off / auto / model / hybrid` — the framing fork itself becomes an experiment arm |
| Roadmap slot | **Before A** (LLM↔LLM interaction) |

## Section 1 — Architecture & Turn Flow

**One new module:** `src/civ_mcp/arena/attention.py` owns directive parsing, the
trigger scan, and the wake-digest accumulator/renderer. **One insertion point:** a
skip-evaluation block in `run_arena` between `run_pre_model_tasks` and the policy
invocation. Nothing else in the turn loop moves.

**Per-civ skip state:** `<transcript_dir>/<run_id>/attention/player_N.json`
(atomic-write JSON, the `memory/` pattern): active directive (skips remaining,
subscribed soft triggers), current streak, last-wake turn, digest accumulator.

**Captured-turn flow:**

1. Load memory + task state; `run_pre_model_tasks` — unchanged. The tracker acts on
   every turn, slept or not.
2. **Skip evaluation** (new): run the trigger scan (one batched read-only Lua query
   + overview-snapshot delta vs. last captured turn). Decide by mode:
   - Any **hard trigger**, streak cap reached, or scan error → **WAKE**. The
     coordinator veto is unconditional; the model cannot opt out of it.
   - Otherwise skip is allowed if the mode permits: `model` needs an unexpired
     directive; `auto` skips whenever the deterministic quiet test passes (no
     blockers, no triggers), no directive needed; `hybrid` allows either; `off`
     reproduces today's behavior exactly.
3. **On SKIP:** append this turn's deltas + notifications + tracker results to the
   digest accumulator (persisted), write a lightweight transcript record, `finish_units`,
   `restore_local` — no model call, no briefing built. `max_game_turns` decrements;
   `max_puppet_turns` does not.
4. **On WAKE:** build briefing as today plus the `== WHILE YOU SLEPT ==` digest
   block; invoke the model; parse a new `SKIP:` directive from its final summary
   (same channel as STANDING PLAN capture). `max_game_turns` decrements on every
   captured turn (slept or played); `max_puppet_turns` decrements only here.

**Engine-risk note:** the slept-turn path (`finish_units` + `restore_local`, no
model actions) is mechanically identical to the existing failed-turn degrade path,
already proven live — low engine risk, still probed (Section 6).

**Error-handling philosophy (applies to every component):** degrade toward today's
behavior, never abort the run. Directive unparseable → no directive. Skip-state
file corrupt → reset + wake (the `save_memory` poison-file self-heal convention).
Trigger scan raises → wake. Failures can only produce *more* model turns, never
more blind skips.

## Section 2 — Trigger Vocabulary

Organizing principle: every trigger maps to something checkable in one batched
read-only Lua query (the `build_caps_query` pattern) or an existing snapshot delta.
No trigger may require judgment; anything requiring judgment is a wake.

### Hard triggers (always wake; not model-optable)

**From the overview-snapshot delta** (zero new cost):
- Unit count dropped (combat loss / disband while asleep).
- City count changed (loss = emergency; gain = production needed).
- Gold deficit trajectory (balance falling, projected negative within 5 turns —
  one max streak).

**From the end-turn-blocker family** (the game's own "needs input" signals — the
literal encoding of "nothing of concern → next turn"):
- Any city production queue empty.
- Research or civic choice due.
- Idle unit with moves that is not fortified/sleeping/alert/automated/tracker-owned.
- Pending diplomacy session involving this seat; incoming trade offer.
- Governor point available; empty policy slot; pantheon/religion choice available.
  (Unit promotions excluded — `sweep_promotions` auto-resolves them post-turn.)

**From one new batched threat/status read** (the new Lua work; all read-only):
- Enemy military unit visible within radius ~4 (configurable) of any owned city or
  civilian unit — the empire-wide generalization of `_visible_hostile_nearby`.
- Any owned city below full HP or damaged since last turn.
- War/peace state changed with any player.
- Any city with net-negative loyalty trend (slice-4 `get_loyalty` net-trend read).
- World Congress in session or imminent (`turns_until_next == 0`).
- Era changed.

**From the notification feed:** any notification whose type is on a curated
wake-list (city under attack, rebellion, spy caught, emergency, …). Starts
conservative; grows from run evidence.

**From the tracker:** a task **failed or blocked** this turn (standing intent
broke; the model must re-plan). Task *completed* generally wakes via a blocker
anyway (settled city → empty queue).

### Soft triggers (fixed enum, model-subscribable via `WAKE IF:`)

v1 list — each maps to one cheap check; models copy names from the playbook, never
author predicates:

`TASK_COMPLETED`, `GREAT_PERSON_AVAILABLE`, `CITY_GREW`, `TRADE_ROUTE_IDLE`,
`GOLD_STOCKPILE_HIGH` (fixed threshold ~500g in v1).

Unknown tokens are dropped with a logged warning — never a parse failure, never a
reason to refuse the directive. Parametrized predicates ("science > 200") are
explicitly **v2**.

### Cadence & caps

- `SKIP: n` clamps to **1–5**.
- Coordinator-side **max skip streak: 5**, independent of directives — the bound on
  everything the scan structurally can't see (including `auto` mode's
  proactive-opportunity blindness: trade routes to start, tiles to buy, policies to
  reset raise no blockers; humans have the same blind spot, hence the playbook's
  10-turn checkpoints). Hitting the cap is itself a recorded wake cause.
- Both are `AttentionOptions` fields — experiment arms can run stricter/looser
  attention policies deliberately.

The redundancy is the safety story: blockers catch "the game needs input," the
threat read catches "the world got dangerous," the snapshot delta catches "we got
worse," notifications catch "Firaxis thinks something happened," and the streak cap
catches the rest. Any scan error wakes.

## Section 3 — Skip Directive

**Format** (final summary, adjacent to the STANDING PLAN block):

```
STANDING PLAN: consolidate; finish Campus in Suwon; settler -> (14,22)
SKIP: 3
WAKE IF: TASK_COMPLETED, GREAT_PERSON_AVAILABLE
```

**Parsing** reuses the standing-plan extractor's paid-for tolerance:
case-insensitive; forgiving of bullets, `**SKIP:**` emphasis, heading prefixes
(the `memory.py` lesson — models reformat markers into markdown and silent misses
freeze state). `SKIP:` takes an integer (tolerating "SKIP: 3 turns"), clamped 1–5.
`WAKE IF:` takes comma/space-separated enum tokens. Garbage → no directive.

**Semantics:**
- Effective from the seat's next captured turn; decrements per slept turn.
- **Any wake cancels the remainder.** A woken model does not resume a stale sleep;
  it re-issues the directive if it still wants quiet — sleep is always freshly
  chosen against current information.
- `WAKE IF:` without `SKIP:` is inert (logged). Directives in `off`/`auto` are ignored.
- Playbook gains a short section: exact format, the trigger enum, when skipping is
  smart (consolidation, long builds, peacetime fortification) and when not (war,
  unsettled settlers, crisis).

**Acknowledgment loop:** directive status is always reported in the next wake
digest — accepted and slept the full term / woken early by X after N / "directive
not recognized." Silent parse failure was the standing-plan system's original bug
class; it is not reintroduced.

## Section 4 — Wake Digest

A `digest_block` injected into the woken turn's prompt like `memory_block` /
`task_block` (signature-gated policy kwarg, same convention):

```
== WHILE YOU SLEPT (turns 45–47, 3 turns skipped) ==
Woke because: ENEMY_NEAR_CITY — barbarian Horseman visible 3 tiles SE of Suwon
Your directive: SKIP 3 accepted; slept 3 of 3. WAKE IF matched: none.
Empire while asleep:
- Score 210→224, Gold 312→401 (+29.7/t), Science 18.2/t, Culture 9.1/t
- Units 9 (unchanged), Cities 5 (unchanged)
- Tracker: settler advanced 3 tiles toward (14,22), 2 remaining
Notifications during sleep (newest first, max 10):
- [T47] Suwon border expanded
- [T46] Barbarian scout sighted near Pusan
```

**Contents, in priority order:** wake cause with concrete detail (the model's first
decision is "how urgent"); directive acknowledgment; accumulated overview deltas
across the whole gap (totals + per-turn rates); tracker progress during the gap;
informational notifications capped newest-first (the gossip lesson — never an
unbounded feed).

**Mechanics:** each slept turn appends one compact record to the attention state
JSON (persisted — a crash mid-streak loses nothing). On wake: render, inject,
clear. Capped ~1,200 chars; participates in existing context-budget accounting the
same way the memory block does.

**Reserved extension slot:** the digest ends with a designed-in slot for the
B-slice map delta — textual "ownership/border changes near you" first, rendered
map image for multimodal seats when the vision arm lands. Designed now, built in B.
(Map *imagery* is deliberately absent from the detector: interpreting an image
requires a model call, which spends what skipping saves; the detector wants
structured data. `map_capture` ownership deltas as a border-shift trigger are a
possible v2 detector input.)

## Section 5 — Metrics & Analysis

**Naming hazard (design-time catch):** the coordinator already logs
`"skipped": True` for *failed* policy turns (degrade path). Attention skips use a
distinct shape — `"slept": true` + an `attention` sub-object. Analyze treats turn
kinds as played / slept / failed; sleeps are never counted as degraded turns.

**Per-civ additions to `analyze.py`:**
- Volume: captured / model / slept turn counts; skip rate; streak histogram + max.
- Savings: LLM calls avoided; estimated USD and wall-clock saved (slept turns ×
  that civ's measured mean model-turn cost/duration from the same transcripts).
- Wake-cause histogram — the attention-policy portrait (mostly `STREAK_CAP` =
  coasting; mostly threat wakes = dangerous neighborhood).
- Directive quality: issued / parse failures / clamps / unknown tokens dropped /
  `WAKE IF` match rate — measures whether each model *can use* the mechanism.

**False-quiet rate (headline accuracy metric):** for each sleep streak, grade
retrospectively from the accumulated per-turn records: units died, cities damaged
or lost, or gold crashed during the streak *without* the responsible trigger being
what ended it → a false-quiet event. Rate = false-quiet streaks / total streaks.
Tunable per trigger; compared across `auto` / `model` / `hybrid` arms it is the
paper's attention-policy result.

**Experiment integrity:** `AttentionOptions` is included in
`CivOptions.fingerprint()`; `experiment.py` arms can vary only the attention mode;
run config records both caps (`max_puppet_turns`, `max_game_turns`). Runs before
and after this feature are not turn-for-turn comparable — the fingerprint makes
that structural, never silent.

## Section 6 — Testing

**Unit (offline, existing conventions):**
- Directive parsing in the standing-plan test style: markdown variants, clamping,
  garbage → none, unknown tokens dropped + warned, `WAKE IF` without `SKIP` inert.
- Trigger-scan parsing from pinned fixtures (real captures, the turn-380 pattern);
  per-trigger true/false condition tests; notification wake-list classification;
  loyalty trend; malformed scan → wake (fail-open, mirroring caps-parser tests).
- **Skip decision matrix**, table-driven: mode × directive state × trigger state →
  expected wake/skip across all four modes — the core correctness artifact.
- Semantics: streak cap, cancel-on-any-wake, streak-cap wake accounting.
- State & digest: JSON round-trip; corruption → reset + wake; accumulation across
  turns; golden render tests; char cap; ack strings; clear-on-wake; crash-recovery
  (accumulate → reload → render).
- Coordinator integration (mock policy/conn): slept turn makes zero policy calls
  but still runs pre-model tasks and `finish_units`; budgets decrement correctly;
  transcript keys correct; `slept` vs `skipped` distinction holds.
- Analyze: synthetic transcripts → skip rate, wake-cause histogram, savings,
  false-quiet grading.

**Live probes (post-merge gate, slice-4 checklist pattern):**
1. Trigger-scan batched query returns and parses on the live game; fixture pinned.
2. An `auto`-mode puppet on a genuinely quiet seat sleeps; turn advances; record written.
3. A hostile-approach wake fires.
4. Short 2-civ `hybrid` mini-run end-to-end: skips in transcript, digest injected
   on wake, budgets sane.

## Non-Goals

- **B (pre-committed action queues / action language).** The task tracker is this
  model for two verbs and took ~900 lines of state machine to do safely; growth is
  future, incremental (`TASK_KINDS`), evidence-driven. Replayed combat is
  explicitly rejected.
- Parametrized soft triggers (v2).
- Map imagery anywhere in this slice (detector or digest) — B-slice work; only the
  digest slot is reserved.
- Push-digests on *every* turn (the B2 experiment arm); this slice's digest exists
  only at wake.
- Any change to the CLI-civ (`cli-claude`/`cli-codex`) turn *content* — skipping
  happens upstream of the policy call, so both driver kinds benefit identically.

## Open Items

- Quiet-turn frequency in historical runs is unquantified; the first `hybrid` run's
  skip-rate metric answers it empirically.
- Notification wake-list contents: start conservative, curate from live-probe and
  first-run evidence.
- Threat-scan radius default (4) to be validated live; configurable regardless.
