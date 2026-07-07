# Arena Puppet Decision-Making — Design Spec

**Date:** 2026-07-05
**Status:** Slice 1 implemented + hardened (2026-07-06, through `dc7f7e3`); Slice 2 implemented + hardened (2026-07-06, through `2dfc3d4`); Slice 3 (standing memory + deterministic low-risk task tracker + behavior tools) implemented + hardened (2026-07-07, through `1f9914c`; Tasks 1-9 in `docs/superpowers/plans/2026-07-06-arena-standing-memory-task-tracker-slice3.md`, post-slice code-review hardening pass in `docs/superpowers/plans/2026-07-07-arena-slice3-code-review-fixes.md`) — A/B testing is complete; next live validation is 8 civs with 3 LLM puppets (seats 1, 3, 5) and 4 regular AI civs. Slice 4 (broader deterministic autonomy) is deferred until that live test produces behavior results.
**Author:** riz + Claude (brainstorming-locals + superpowers:brainstorming)

## Motivation

The gemma slice1 A/B (2026-07-05) showed treatment puppets decisively outplaying
the minimal baseline, but surfaced three concrete execution gaps in *how* the
LLM puppets play, independent of the A/B arms:

1. **No unit promotions.** Across the whole run no puppet ever promoted a unit —
   even treatment civs, which have `promote_unit` in the `full` tier. Passive
   surfacing was not just insufficient, it was **absent**: the `**NEEDS
   PROMOTION**` flag the briefing keys on (`get_units().needs_promotion`) is
   hardcoded to `"0"` in `lua/units.py:105` and never fires (a mid-turn XP-threshold
   check double-promotes, so it was intentionally disabled). The authoritative
   detector is `get_unit_promotions(unit_id)` — see the ground-truth reference.
   Compounding Civ mechanic: a unit holding an unspent
   promotion earns **zero further XP until it is spent**, so an un-promoted unit
   is silently capped.
2. **No diplomacy/trade/peace capability at all.** `send_diplomatic_action`,
   `propose_peace`, `propose_trade`, `get_trade_options`, `form_alliance` exist in
   the live MCP server but are **not registered in the arena `TOOL_REGISTRY`** —
   so no puppet at any tier can declare formal war, make peace, trade, or ally.
   The only inter-civ action available is tactical `attack_unit`.
3. **Fumbled multi-turn execution.** Treatment made ~19 `found_city` attempts but
   reached only ~3 second cities. Root cause: **every puppet turn is fully
   stateless** — `LLMPolicy.__call__` rebuilds the message list from scratch each
   turn (`agent.py:99-100`); there is no diary, standing plan, or memory of
   last turn's intent. A settler mid-march is re-derived from zero each turn.

## Guiding principle: hybrid autonomy by lever

Deterministic scaffolding is used **only for near-always-correct, low-judgment
mechanical actions** (spend a pending promotion, don't leave a builder idle,
escort an exposed settler). All judgment calls (when to war, where to settle,
when to make peace, which victory to chase) remain passive: better context +
doctrine, model decides. This keeps puppets genuinely autonomous where it matters
for the experiment while guaranteeing the mechanical follow-through that "fix
execution, not just intent" requires.

## Program shape (four slices, built in order)

| Slice | Deliverable | Depth |
|---|---|---|
| **1. Doctrine + signals + promotion lever** | Playbook doctrine, promotion ACTION block, deterministic end-of-turn promotion sweep | Shallow, no new architecture |
| **2. Capability tools + doctrine** | Register diplomacy/peace/trade/alliance arena tools + their playbook doctrine | Medium — wrap existing MCP handlers |
| **3. Standing memory + deterministic low-risk task tracker + behavior tools** | Per-puppet standing plan/memory persisted across turns; deterministic pre-model follow-through for `settle`/`builder_improve` unit tasks only; Great People, trade, religion, and World Congress behavior-critical tools | Deep — new architecture |
| **4. Broader deterministic autonomy after Slice 3 live testing** | Option 2 from brainstorming: extend deterministic scaffolding to judgment-heavier actions. Deferred until the Slice 3 live validation run (8 civs: 3 LLM puppets + 4 regular AI civs) produces behavior results | Not yet scoped |

Slices 2 and 3 each got their own brainstorm + spec before implementation
(slice 3's plan lives at
`docs/superpowers/plans/2026-07-06-arena-standing-memory-task-tracker-slice3.md`).
This document specifies **slice 1 in implementation-ready detail** and slices
2–3 at scope level (see their own specs/plans for implementation detail).
Slice 4 is not yet planned — it waits on Slice 3 live behavior results before
its own brainstorm and spec.

---

## Ground-truth reference (verified 2026-07-05)

All paths under `/home/riz/dev/civ6-mcp/`.

- Tool tiers: `src/civ_mcp/arena/registry.py` — `minimal` (9 tools, `registry.py:529-539`),
  `standard` (18, `:540-559`), `full` (all 43, `:560`). `promote_unit` (`:461`) and
  `get_unit_promotions` (`:472`) are `full`-only. Diplomacy/trade/peace/alliance tools
  are **absent from the registry entirely**.
- Turn driver: `LLMPolicy.__call__` in `src/civ_mcp/arena/agent.py:72-193`; loop is
  `for _ in range(self.max_steps)` (`agent.py:107`); ends early on a no-tool-call
  reply (`agent.py:115-131`); `max_steps` default 6 (`config.py:36`), treatment 10 /
  control 6 in the slice1 YAML.
- Briefing: `build_briefing` in `src/civ_mcp/arena/briefing.py:256-296`; section
  builders `briefing.py:33-155`; already injects era score (via `_overview` →
  `narrate.py:92-98`) and loyalty+amenities (via `_cities` → `narrate.py:344-364`).
  A section only renders if its name is in **both** `_ORDER` (`briefing.py:132-143`)
  and the civ's `opts.sections` (filtered at `briefing.py:269-273`); `opts.sections`
  is validated against `VALID_SECTIONS` (`config.py:8-19`). Adding a new section
  therefore requires editing `_ORDER`, `_BUILDERS`, `VALID_SECTIONS`, **and** each
  treatment civ's `sections:` list in the experiment YAML.
- **Promotion detection (authoritative):** `get_units().needs_promotion` is dead
  (`lua/units.py:105`, hardcoded `"0"`). The real signal is
  `get_unit_promotions(unit_id)` (`game_state.py:1028`): its query
  (`lua/governance.py:310-363`) returns early with **no** `PROMO` rows unless
  `xp >= xpNeeded` (`:325`), and emits a `PROMO` row only when GameCore
  `exp:CanPromote(promo.Index)` is true (`:352`) — the same check `end_turn` uses.
  So **a pending promotion ⇔ `get_unit_promotions(unit_id).promotions` is
  non-empty.** Returns a `UnitPromotionStatus` (`lua/models.py:900-912`) with
  fields `promotions: list[PromotionOption]`, `xp`, `xp_needed`, `promotion_count`,
  `unit_id`, `unit_index`, `unit_type`; each `PromotionOption` (`lua/models.py:893`)
  has `.promotion_type`, `.name`, `.description`. Do **not** pre-filter on
  `needs_promotion` — it would drop every unit.
- Coordinator turn hand-off: `src/civ_mcp/arena/coordinator.py:129-138` —
  `pol(...)` returns, then `hook.finish_units(conn, st.local)` + `hook.restore_local(conn, 0)`.
  This is the insertion point for the end-of-turn sweep (while `local == st.local`,
  BEFORE `finish_units`).
- Game-state actions available regardless of tier (full `GameState`, not gated by
  registry): `gs.get_units()`, `gs.get_unit_promotions(unit_id)` (`game_state.py:1028`),
  `gs.promote_unit(unit_id, promotion_type)` (`game_state.py:1034`).
- Playbook: `src/civ_mcp/arena/playbook.md` (73 lines), appended to system prompt
  only for `playbook: condensed` civs (`agent.py:29-30, 66-67`). Currently covers
  expansion/growth/research/builders/combat-basics/districts; **no** mention of
  promotions, upgrades, or inter-civ diplomacy.

---

## Slice 1 — Doctrine + signals + promotion lever (implementation-ready)

### 1a. Playbook doctrine additions (`src/civ_mcp/arena/playbook.md`)

Append new sections (pure text; only treatment/`condensed` civs see them). Content
sourced from the 2026-07-05 Civ VI research briefing. Add:

- **`## Unit promotions`** — Units earn XP by surviving combat; ranged units earn
  XP without taking damage. **A unit with an unspent promotion earns no more XP
  until you spend it — always promote when `NEEDS PROMOTION` shows.** Promoting
  also heals the unit (use it as mid-fight sustain). Strong early picks: melee →
  Battlecry (+7 attacking); ranged → Volley (+5 vs land); recon → prefer a vision or
  mobility promotion when offered (Sentry, Spyglass, Ranger, Alpine — more vision and
  reach = more of the map you can act on). Use `get_unit_promotions(unit_id)`
  then `promote_unit(unit_id, promotion_type)`.
- **`## Unit upgrades`** — Upgrade obsolete units when you have the tech + resources
  + gold (Slinger→Archer with Archery, Warrior→Swordsman with Iron Working). Units
  fall behind rivals fast if not upgraded. Use `upgrade_unit(unit_id)`.
- **Extend `## Expansion`** — Settler siting: prefer **fresh water** (river/lake/oasis
  = 5 base housing) and **flat or plains-hills** tiles; a settler arriving on
  forest/hills may have no movement left to found that turn. **Escort settlers** —
  they have 0 combat strength and a single barbarian captures them; keep a military
  unit adjacent.
- **Extend `## Combat basics`** — War against a rival is a sequence: position units
  adjacent to targets **while still at peace**, then attack; the combat engine does
  not register the new enemy until the **turn after** you declare, so declaring and
  attacking the same turn fails. (Full rival-war *tools* arrive in slice 2; this is
  doctrine groundwork.)
- **`## Signals to watch`** — Loyalty below 75 penalizes a city's yields (assign a
  governor / fix amenities); each new *distinct* luxury = +1 amenity (duplicates are
  worthless — save them to trade later); watch era score vs the Golden/Dark
  thresholds shown in the overview.

Keep additions concise — the playbook is appended to every treatment system prompt
and competes with the briefing for the token budget (`agent.py:87-95`).

### 1b. Promotion ACTION block (briefing)

Add a new high-salience briefing section that renders **only when at least one unit
has a pending promotion** (per the authoritative detector, not `needs_promotion`),
placed **first** in the briefing so it is not truncated by the char budget.

- New builder `_promotions(gs, ctx)` in `briefing.py`, registered in `_BUILDERS`
  and placed at the **front** of `_ORDER` (before `overview`). Also add
  `"promotions"` to `VALID_SECTIONS` (`config.py:8`) and to each treatment civ's
  `sections:` list in the experiment YAML — see 1e.
- Logic: fetch units once (reuse `ctx["units"]` if `_units` already ran, but since
  this section runs **first** it usually won't be) and **store them back into
  `ctx["units"]`** so the later `units`/`map` sections reuse the same fetch instead
  of re-querying. Then, concurrently (`asyncio.gather`, `return_exceptions=True`),
  call `gs.get_unit_promotions(u.unit_id)` for each unit; keep the units whose
  returned `UnitPromotionStatus.promotions` is non-empty (this is the pending-promotion
  test — civilians and units-without-a-point come back empty or raise and are dropped).
  If none pend, return `""` (the section is skipped). Otherwise render:
  ```
  == ACTION: PROMOTIONS AVAILABLE ==
  These units earn NO XP until promoted. Promote them this turn:
  - <unit_type> (id:<unit_id>) at (<x>,<y>): suggested <suggested.name>
      options: <opt.name> (<opt.promotion_type>), ...
  Use promote_unit(unit_id, promotion_type).
  ```
- The `suggested` pick is `autoresolve.pick_promotion(status)` (1c) applied to the
  unit's own `UnitPromotionStatus`, so the block's suggestion and the sweep's actual
  choice agree exactly. `<suggested.name>` is the `PromotionOption.name` of that pick.
- Only civs with `briefing.enabled` **and** `"promotions"` in `sections` (treatment)
  get this block; control relies entirely on the sweep (1c).
- Budget note: `_ORDER` is iterated and blocks appended until the char budget is
  hit (`briefing.py:271-292`); putting this block first guarantees it survives.
  It is empty (returns `""`, skipped) whenever no promotion pends, so it does not
  crowd out other sections on normal turns. Cost when it does run: one `get_units`
  (shared via `ctx`) plus N concurrent `get_unit_promotions` calls.

### 1c. End-of-turn promotion sweep (coordinator, the hybrid lever)

A deterministic sweep that runs after the model's turn and applies a promotion to
any unit **still** holding an unspent one. The model acts first (and may pick a
smarter promotion or promote as part of a combat plan); the sweep is the safety
net that guarantees no unit stays XP-frozen. Runs for **all** puppets — it is
infrastructure, not a treatment arm, and works via the full `GameState`
regardless of tool tier.

- New module `src/civ_mcp/arena/autoresolve.py` with:
  - `PREFERRED_PROMOTIONS: tuple[str, ...]` — a single flat, ordered global
    preference of `promotion_type` strings (research-backed first picks across the
    common unit trees), e.g.
    `("PROMOTION_VOLLEY", "PROMOTION_BATTLECRY", "PROMOTION_SENTRY", "PROMOTION_SPYGLASS", "PROMOTION_RANGER", "PROMOTION_ALPINE", "PROMOTION_TORTOISE", "PROMOTION_GARRISON")`.
    Keyed on the promotion type itself, not the unit's class — the type already
    identifies its tree (VOLLEY is ranged, BATTLECRY is melee, SENTRY/SPYGLASS/RANGER/
    ALPINE are recon vision/mobility), so no `PromotionClass` field on the unit record
    is required. The list is a *preference*, not a whitelist — anything not listed is
    still taken via the first-available fallback.
  - `pick_promotion(status) -> PromotionOption | None` — given a `UnitPromotionStatus`
    (`lua/models.py:900-912`: `status.promotions: list[PromotionOption]`, each with
    `.promotion_type`, `.name`, `.description`), return the first `PROMOTION_OPTION`
    in `status.promotions` whose `.promotion_type` matches the earliest entry in
    `PREFERRED_PROMOTIONS`; else the **first** element of `status.promotions` (any
    promotion unfreezes XP + heals — a suboptimal pick still beats none); else `None`
    if the list is empty. Returns the whole `PromotionOption` so 1b can render
    `.name` and the sweep can pass `.promotion_type`.
  - `async def sweep_promotions(gs) -> list[dict]` — call `gs.get_units()` to
    enumerate unit ids, then for **every** unit call `gs.get_unit_promotions(unit_id)`
    (do **not** pre-filter on `needs_promotion`; it is always false — see ground
    truth). A unit has a pending promotion iff `status.promotions` is non-empty;
    for those, `pick_promotion(status)` → `gs.promote_unit(unit_id, pick.promotion_type)`.
    Collect `{"unit_id", "unit_type", "promotion_type", "ok": bool}` per attempt;
    swallow per-unit exceptions (best-effort — a civilian's `get_unit_promotions`
    may raise, which simply means "no promotion", and a failed `promote_unit` records
    `ok: false`). Never raise into the coordinator.
- Wire into `coordinator.py` (see 1e for exact placement): the sweep must run
  **after** the exclusive-tuner reconnect (`coordinator.py:98-99`, so `gs` has a live
  connection) and **before** the log append (`:97`) and transcript write (`:128`), so
  its result is captured in telemetry. Guarded so a sweep failure can never block the
  human hand-back:
  ```python
  try:
      swept = await autoresolve.sweep_promotions(gs)
  except Exception as e:
      swept = []
      print(f"[arena] promotion sweep failed: {e!r}", file=sys.stderr)
  ```
  Record `swept` under a `promotion_sweep` key in the turn log entry (and transcript
  record if `_tx_on`) so A/B analysis can see how often the sweep fired vs. the model
  self-promoted.
- The class/type strings above are the expected Civ VI identifiers; the plan's
  first task validates them against a live `get_unit_promotions` dump and against
  `GameInfo.UnitPromotions` — if any differ, `pick_promotion`'s "first available"
  fallback still guarantees a promotion, so a wrong preference string degrades
  gracefully rather than failing.

### 1e. Config + YAML + coordinator wiring

Two integration points that the section/module work above depends on:

**Config & experiment YAML (so 1b actually renders):**
- Add `"promotions"` to `VALID_SECTIONS` (`config.py:8-19`). Leave the
  `BriefingOptions.sections` default (`config.py:31`) unchanged — control civs must
  not silently gain the section; it is opt-in per civ.
- Add `promotions` to the `sections:` list of **every treatment civ (1, 3, 5, 7)** in
  `experiments/gemma-strategy-ab-slice1.yaml` (and the `-50r` variant if still used).
  Put it first for clarity, e.g.
  `sections: [promotions, overview, units, cities, map, research, production_options, threats, rivals, empire_resources]`.
  (Ordering in the YAML is cosmetic; actual render order is fixed by `_ORDER`.)
- Any test fixture / snapshot that asserts the exact `VALID_SECTIONS` tuple or a
  treatment civ's `sections` list must be updated in the same task
  (`tests/arena/test_config.py`, `tests/arena/test_experiment.py`).

**Coordinator ordering (Finding 3 — telemetry-correct placement):** the sweep must
land where `gs` is connected *and* its result can be recorded. Current flow in
`run_arena` (`coordinator.py:95-128`): policy returns (`:95`) → log append (`:97`) →
reconnect if exclusive (`:98-99`) → `state_after` snapshot (`:100`) → transcript write
(`:128`). Reorder to:

1. `result = await pol(...)` (`:95`)
2. reconnect if `exclusive and not conn.is_connected` (move `:98-99` up, before the sweep — the sweep needs a live tuner)
3. `swept = await autoresolve.sweep_promotions(gs)` inside the guarded try/except above
4. `state_after = await _overview_snapshot(gs)` (so the snapshot reflects post-sweep heals/promotions)
5. `log.append({"player":…, "turn":…, **_log_entry, "promotion_sweep": swept})`
6. transcript `record` gains `"promotion_sweep": swept`

This keeps `swept` in both the `.arena-runs` log and the transcript, and guarantees
the sweep runs on a reconnected tuner. The existing human-safety `finally` block
(`coordinator.py:148+`) is unchanged.

### 1d. GPP briefing section — DEFERRED

`get_great_people` is the one genuinely-missing signal (not injected in the
briefing). Deferred out of slice 1 to keep scope tight; revisit as a fast add in
slice 1.5 or fold into slice 3.

### Slice 1 testing

- **Unit tests** (`tests/arena/`):
  - `test_autoresolve.py`: `pick_promotion` returns the `PromotionOption` whose
    `.promotion_type` is highest in `PREFERRED_PROMOTIONS` when one is offered; falls
    back to the first `PromotionOption` when none of the preferred types are present;
    returns `None` on an empty `promotions` list. `sweep_promotions` (stub `gs`):
    promotes each unit whose `get_unit_promotions` returns non-empty `promotions`
    exactly once with the picked `promotion_type`; skips units whose `promotions` is
    empty; does **not** pre-filter on `needs_promotion` (a stub unit with
    `needs_promotion=False` but a non-empty promotion list is still promoted); swallows
    a `get_unit_promotions` raise and a `promote_unit` raise without raising, recording
    `ok: false` for the latter.
  - `test_briefing.py` (extend): `_promotions` renders the ACTION block only when a
    stub unit's `get_unit_promotions` returns a non-empty list, includes the suggested
    pick's `.name`, and returns `""` (section skipped) otherwise; it stores fetched
    units in `ctx["units"]`; verify `"promotions"` is first in `_ORDER` and present in
    `VALID_SECTIONS`.
  - `test_coordinator.py` (extend): with a `FakeGS` whose `get_unit_promotions` yields
    a pending promotion, the sweep promotes before `finish_units`, `promotion_sweep`
    appears in the log entry, and a sweep that raises does not prevent `conn.restored`
    hand-back. Confirm the sweep runs **after** reconnect (exclusive-policy path) so it
    is not called on a disconnected tuner.
- **Config/YAML**: assert `"promotions"` in `VALID_SECTIONS` and that the loaded
  slice1 experiment gives treatment civs a `sections` list containing `promotions`
  and control civs one that does not (extend `tests/arena/test_experiment.py`).
- **Playbook**: a lightweight assertion (extend `tests/arena/test_experiment.py`
  style) that the new section headers exist in `playbook.md`.
- **Live validation**: in the running recon game, confirm a treatment unit with a
  pending promotion is (a) shown the ACTION block and (b) promoted by end of turn
  even if the model ignores it; confirm a control unit is promoted by the sweep.
  Do **not** stop the watcher mid-AI-phase (see
  `reference-arena-no-autonomous-mode` memory).

---

## Slice 2 — Capability tools + doctrine (scope-level)

Register the missing inter-civ tools into `TOOL_REGISTRY`, wrapping the MCP
handlers that already exist in `game_state.py`:

- `send_diplomatic_action`, `propose_peace`, `propose_trade`, `get_trade_options`,
  `form_alliance`; likely also the reactive `respond_to_diplomacy` /
  `respond_to_trade` (AI-initiated sessions).
- Add to the `full` tier; consider a diplomacy-capable mid tier.
- Playbook doctrine: propose peace when losing a war (10-turn cooldown); sell
  surplus luxuries and diplomatic favor for gold/GPT (the AI over-values both —
  a reliable exploit); send delegations on first meeting; pursue friendships →
  alliances for favor income.

**Open questions for slice 2's brainstorm:** reactive AI-initiated diplomacy
sessions are stateful and block turn progression — how does a puppet handle a
diplomacy session that opens mid-turn? Does the human seat 0 absorb these today,
or do puppets get them? Trade proposals have a `test` vs `send` mode and a
counter-offer flow — how much of that does the puppet drive? These need live
investigation before planning.

## Slice 3 — Cross-turn memory / standing plan (scope-level)

**Implemented as:** a model-authored `STANDING PLAN:` block (captured as standing
memory) combined with the deterministic execution-tracker option below, scoped to
`settle` and `builder_improve` unit tasks only — see
`docs/superpowers/plans/2026-07-06-arena-standing-memory-task-tracker-slice3.md`
for implementation-ready detail. The brainstorm below is kept for history; it
predates that choice.

Persist a short per-puppet plan/intent across turns so a settler march or war-prep
survives to completion. Candidate mechanisms (to be chosen in slice 3's brainstorm):

- **Model-authored scratchpad**: the puppet writes a 1–3 line plan at end of turn;
  it is injected at the top of next turn's context. Minimal, model-owned.
- **Diary reuse**: register/adapt the existing `get_diary`/diary infra from the MCP
  server for arena puppets.
- **Deterministic execution-tracker**: engine-side tracking of "unit U is executing
  task T (settle at X,Y)" that survives without LLM memory — extends the slice-1
  `autoresolve` harness to mechanical follow-through (idle builders, exposed
  settlers, in-progress settle chains).

Storage must be per-puppet and per-game, and must not bloat the token budget.
Interacts with the stateless `LLMPolicy` (`agent.py:99-100`) — the chosen mechanism
adds exactly one injected block and one end-of-turn capture step.

---

## Decisions locked in brainstorming

1. Scope: all three improvements, sequenced (doctrine+signals → tools → memory).
2. Autonomy: hybrid by lever — deterministic only for near-always-correct mechanical
   actions; passive guidance for judgment calls.
3. Promotion lever: **sweep + better surfacing** (loud ACTION block with a suggested
   pick, plus a deterministic end-of-turn sweep as the safety net).
4. The slice-1 sweep runs for **all** puppets (infrastructure), not treatment-only.
5. GPP briefing section deferred out of slice 1.
