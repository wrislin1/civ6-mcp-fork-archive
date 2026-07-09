# Arena Attention & Turn Skipping — Quiet-Turn Fast Path (Design)

**Date:** 2026-07-09
**Status:** Approved by riz (brainstorming session, this date)
**Revised:** 2026-07-09 — six findings from riz's separate-session review applied
(standing-plan collision, decoupled directive wiring, config contract, slept
record schema, scan contract, TASK_COMPLETED hard wake)
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
   digest accumulator (persisted), write a slept transcript record (exact schema in
   Section 5), `finish_units`, `restore_local` — no model call, no briefing built.
   `max_game_turns` decrements; `max_puppet_turns` does not.
4. **On WAKE:** build briefing as today plus the `== WHILE YOU SLEPT ==` digest
   block; invoke the model; parse a new `SKIP:` directive from its final summary
   — a channel the attention mode itself enables, independent of memory/task
   settings (Section 3). `max_game_turns` decrements on every
   captured turn (slept or played); `max_puppet_turns` decrements only here.

**Engine-risk note:** the slept-turn path (`finish_units` + `restore_local`, no
model actions) is mechanically identical to the existing failed-turn degrade path,
already proven live — low engine risk, still probed (Section 6).

**Error-handling philosophy (applies to every component):** degrade toward today's
behavior, never abort the run. Directive unparseable → no directive. Skip-state
file corrupt → reset + wake (the `save_memory` poison-file self-heal convention).
Trigger scan raises → wake. Failures can only produce *more* model turns, never
more blind skips. Persisted-but-wrong-typed state (dict-shaped, e.g.
`last_snapshot={"units":"5"}`) passes the load-time shape check but raises
inside `evaluate()`'s comparisons; the coordinator wraps `evaluate()` in its own
try/except for exactly this, producing wake cause **`STATE_CORRUPT`**: state
reset + wake, never abort. `note_wake`'s save on the fresh state self-heals
the file, same as any other corrupt-state path.

### Config contract

**`AttentionOptions`** — new frozen dataclass in `config.py` (the
`MemoryOptions` pattern):

| Field | Default | Validation |
|---|---|---|
| `mode` | `"off"` | one of `off / auto / model / hybrid`; anything else raises at parse time |
| `max_skip` | `5` | int ≥ 1 — upper clamp for `SKIP: n` |
| `max_streak` | `5` | int ≥ 1 — coordinator-side consecutive-sleep cap |
| `threat_radius` | `4` | int ≥ 1 — hostile-scan radius around cities/civilians |

- `CivOptions` gains `attention: AttentionOptions`, and `fingerprint()` gains
  the sub-dict `{"mode", "max_skip", "max_streak", "threat_radius"}`. Every
  fingerprint changes once; Section 5 already declares the comparability break
  structural.
- YAML: `attention` joins `_SHARED_KNOBS` in `experiment.py` as a per-civ
  mapping whose sub-keys are exactly the four fields; unknown sub-keys are
  rejected the same way `memory` / `task_tracker` reject theirs.
- Default `mode="off"` reproduces today's behavior exactly — existing configs
  and CLI invocations change fingerprint but not behavior.

**`max_game_turns`** — new `ArenaConfig` field capping ALL captured turns
(played, slept, or failed). Default `0` = uncapped (slept turns stay bounded
by the streak cap: `max_streak` forces a STREAK_CAP wake — a played turn,
which consumes `max_puppet_turns` — so total captured turns stay ≤
`max_puppet_turns` × (`max_streak` + 1)). `idle_poll_limit` is a
**consecutive-idle** poll budget, not a whole-run cap: `deadline_polls`
refills to its configured value on every captured puppet turn — played,
slept, or failed — so it only bites during a genuinely idle stretch (no
puppet civ active), never as a side effect of a long sleep streak. Wired
everywhere `max_puppet_turns`
already is: top-level YAML key (`_TOP_KEYS`), CLI `--max-game-turns`, and the
suppressed `--config-default-max-game-turns` passthrough.

**Loop accounting & return counters:** `remaining` (the `max_puppet_turns`
budget) decrements only on model-invoked turns (played or failed, as today); a
separate counter enforces `max_game_turns` across every captured turn and ends
the run when exhausted. `run_arena`'s result gains `"turns_slept"` alongside
the existing `"puppet_turns_played"` (whose meaning — model-invoked turns —
does not change). Slept entries in the run log carry `"slept": true` plus the
`attention` sub-object, never the failed-turn `"skipped": true` key.

## Section 2 — Trigger Vocabulary

Organizing principle: every trigger maps to something checkable in one batched
read-only Lua query (the `build_caps_query` pattern) or an existing snapshot delta.
No trigger may require judgment; anything requiring judgment is a wake.

### Trigger-scan contract

The scan is a `build_attention_query(player_id, threat_radius)` /
`parse_attention_scan(lines)` pair in `attention.py` — the caps-query pattern:
one batched **read-only** Lua query executed via `conn.execute_read`, parser
returns `None` on any malformed payload, and `None` → WAKE (fail-open). Parsed
fields, one per hard-trigger family:

- `hostile_count` + `nearest_hostile` (unit type, distance, what it is near) —
  radius `threat_radius` around owned cities and civilian units
- `damaged_city_ids` (below full HP)
- `at_war_with` (player-id set; delta vs. the stored set = war/peace change)
- `negative_loyalty_city_ids` (net trend, the slice-4 `get_loyalty` read)
- `wc_turns_until_next` (0 = in session / imminent)
- `era_index` (delta vs. stored = era changed)
- `blocker_types`: the game's own `EndTurnBlocking` type names (the proven
  `build_end_turn_blocking_query` read), minus a small ignore-set (unit
  promotions — `sweep_promotions` resolves those post-turn). One passthrough
  covers empty queues, research/civic choices, units needing orders,
  governors, policies, beliefs — every "needs input" signal the game itself
  raises. Tracker-owned units don't false-trigger: the scan runs after
  `run_pre_model_tasks`, so their moves are already spent.
- `pending_diplomacy`: any open diplomacy session involving this seat (the
  orphan-sweep session-read idiom)
- `total_population`, `great_person_available`, `trade_route_idle` — feed the
  soft triggers (`CITY_GREW` via stored-population delta, the others directly)
- `notifications`: (type, summary) pairs for wake-list matching + digest.
  NOTIFY reads the list in two passes — wake-list types first, then
  everything else — so a wake-worthy entry is never crowded out of the
  shared 10-line cap by list position.
- Every family is individually `pcall`-guarded in the Lua, but the two tiers
  handle a failed guard differently. Hard-trigger families — e.g. **CITYHP**
  and **LOYALTY** — are strict: a failure propagates to `ATTN_ERR|<FAMILY>` →
  `failed_families` → `SCAN_PARTIAL` wake (partial scans never silently
  narrow attention). **GP** and **TRADE** are the deliberate exception —
  soft-trigger tier, degrade-tolerant by design: their inner `pcall`s swallow
  failures locally (a loud failure would wake every turn for an opt-in
  signal), so a GP/TRADE glitch degrades that one soft signal instead of
  forcing a wake.

**Attention snapshots are not transcript-gated.** Today `_overview_snapshot`
runs only when transcripts are on (`_tx_on`); with attention mode ≠ `off`, the
snapshot (feeding the delta triggers) and the scan run on every captured turn
regardless of transcript mode. Both run on the live `conn` before any
exclusive-CLI disconnect (the pre-model-task precedent).

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

**From the tracker:** any task **completed, failed, or blocked** this turn.
Failure/block means standing intent broke; completion must also hard-wake so
the model chooses its next intent — in `auto` mode there is no `WAKE IF`
subscription to catch it, and completion does not reliably raise a blocker (a
builder spending its last charge leaves no idle unit behind).

### Soft triggers (fixed enum, model-subscribable via `WAKE IF:`)

v1 list — each maps to one cheap check; models copy names from the playbook, never
author predicates:

`GREAT_PERSON_AVAILABLE`, `CITY_GREW`, `TRADE_ROUTE_IDLE`,
`GOLD_STOCKPILE_HIGH` (fixed threshold ~500g in v1). (`TASK_COMPLETED` was in
the draft enum; it is a **hard** trigger now — subscribing to an unconditional
wake is meaningless.)

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
WAKE IF: GREAT_PERSON_AVAILABLE, CITY_GREW
```

**Parsing** reuses the standing-plan extractor's paid-for tolerance:
case-insensitive; forgiving of bullets, `**SKIP:**` emphasis, heading prefixes
(the `memory.py` lesson — models reformat markers into markdown and silent misses
freeze state). Directive lines are matched **line-wise anywhere in the final
summary** — placement relative to the STANDING PLAN block must not matter.
`SKIP:` takes an integer (tolerating "SKIP: 3 turns"), clamped 1–`max_skip`
(default 5). `WAKE IF:` takes comma/space-separated enum tokens. Garbage → no
directive.

**Standing-plan collision (external-review catch, must-fix):** as written
today, `extract_standing_plan` collects lines after the marker until a section
header, and its header test requires a trailing colon — so `SKIP: 3` placed
after the plan block (the canonical format above) would be swallowed **into
persisted standing memory** and re-injected every turn. Fix in `memory.py`:
a line matching the attention-directive regexes **terminates plan collection
and is never included in plan text**. The regexes (`SKIP_LINE_RE`,
`WAKE_IF_LINE_RE`) live in `attention.py` and are imported by `memory.py` —
the existing `TASK_LINE_RE`/`CANCEL_LINE_RE` import precedent. Tests must
prove both directions: directives after the plan are parsed and absent from
saved memory; directives before the plan leave plan capture intact.

**Prompt & capture wiring (decoupled from memory/tasks — external-review
catch):** today the final-summary channel exists only when standing-plan
capture is on — `STANDING_PLAN_INSTRUCTION` ships only via
`include_standing_plan_instruction`, and the coordinator reads
`final_summary` only under `opts.standing_plan_enabled`. Attention modes
`model`/`hybrid` establish their own path: `prompting.py` gains an
`ATTENTION_INSTRUCTION` block (exact `SKIP:`/`WAKE IF:` format + the soft
enum), appended whenever the seat's attention mode is `model` or `hybrid`;
the coordinator extracts `final_summary` and parses directives under that
same condition, independent of `memory.enabled`/`task_tracker.enabled`.
Directive parsing never writes standing memory or tasks — a memory-off,
tracker-off civ can still sleep.

**Semantics:**
- Effective from the seat's next captured turn; decrements per slept turn.
- **Any wake cancels the remainder.** A woken model does not resume a stale sleep;
  it re-issues the directive if it still wants quiet — sleep is always freshly
  chosen against current information.
- `WAKE IF:` without `SKIP:` is inert (logged). Directives in `off`/`auto` are ignored.
- `WAKE IF:` subscriptions are gated on **the issuing directive standing** —
  cleared/replaced on every wake (`note_wake`) — not on skips remaining. In
  `hybrid`, once the `SKIP: n` count is spent the seat keeps auto-sleeping
  under the mode's own quiet test, and subscriptions keep being honored
  through that auto-sleep tail after the skip count is spent; a subscription
  never outlives its sleep streak because the directive itself is cleared at
  wake.
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

**Slept transcript record (exact schema — external-review catch):** slept
turns write a transcript record even though no policy ran; analyze never
guesses. Keys mirror the played record the coordinator writes today:

```json
{
  "schema_version": 1, "run_id": "...", "ts": "...",
  "player_id": 3, "turn": 47,
  "provider": "...", "model": "...", "driver": "...",
  "turn_kind": "slept", "slept": true,
  "step_count": 0, "usd": 0.0,
  "prompt_tokens": 0, "completion_tokens": 0,
  "state_before": {"score": 210, "gold": 312, "...": "..."},
  "state_after":  {"score": 214, "gold": 341, "...": "..."},
  "state_delta":  {"score": 4,   "gold": 29,  "...": "..."},
  "standing_memory": {"loaded": true, "injected": false, "...": "..."},
  "task_tracker": {"active_before": 1, "pre_model_results": [], "...": "..."},
  "attention": {
    "mode": "hybrid", "decision": "slept",
    "directive": {"skip": 3, "wake_if": ["GREAT_PERSON_AVAILABLE"]},
    "skips_remaining": 2, "streak": 1, "wake_cause": null
  }
}
```

`provider`/`model`/`driver` come from the seat's policy object exactly as on
played records (no invocation needed). `state_before` is the previous captured
turn's snapshot; `state_after` is this turn's attention snapshot; the delta is
the same computation played records use. `standing_memory`/`task_tracker` are
the same field dicts (the tracker still ran; nothing was captured). The record
never carries `"skipped"` — that key means a FAILED turn. Played (wake)
records gain `"turn_kind": "played"` and the same `attention` object
(`decision: "woke"`, `wake_cause` set). All additions are additive:
`schema_version` stays 1, and records without `turn_kind` (pre-feature) read
as played. Failed turns remain log-only with `"skipped": true`, unchanged.

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
- Memory interaction (the collision fix): directives after a STANDING PLAN block
  terminate plan collection and never appear in saved memory text; directives
  before the block leave plan capture intact; a directive-only summary (no plan)
  still parses; directive extraction works with memory and task tracking disabled.
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
