# Attention & turn-skipping live-probe checklist

> **Status 2026-07-14: ALL FOUR PROBES PASSED** on a live game (Korea save,
> turns 155-225). Three code fixes fell out and are applied with tests (1001
> green): (1) scan moved GameCore→InGame — 4 families were nil in GameCore and
> the feature was silently inert; (2) ENDTURN_BLOCKING_UNITS added to
> BLOCKER_IGNORE — post-model-turn wake loop; (3) SCAN_ERROR wakes now carry a
> raw-line/missing-snapshot detail. Live wake causes observed: NO_BASELINE,
> STREAK_CAP, ENEMY_NEAR, WC_SESSION, ERA_CHANGED, SCAN_ERROR,
> BLOCKER_ENDTURN_BLOCKING_UNITS (pre-fix). Digests rendered at 189-344 chars.
> Attention is cleared for attention-enabled runs.

> **Status 2026-07-09 (updated post-merge):** the attention system (digest-based
> wake triggers, turn-skipping framework) and playbook guidance are
> **implementation-complete and MERGED to main** at `7f1ac2c` (998 tests green,
> after three separate-session review-fix waves). riz merged ahead of the probes;
> the probes below are now the **hard gate before any attention-enabled run**.
> P1 especially: check the wake_cause distribution for SCAN_PARTIAL dominance —
> each SCAN_PARTIAL record now carries the Lua error text in `wake_detail`, and
> the 120-char Lua cap should leave useful text after the `query:NN:` prefix.
> Fixtures pinned per P1 become the regression anchors; P2-P4 validate
> end-to-end workflows.

No greenfield-backed tool reaches production until its probe below captures a
real fixture or the spec records a degrade/cut decision.

**Preconditions:** `main` at `7f1ac2c` or later is checked out (the slice is
merged); a live game is loaded past the early era (turn 50+ recommended for
robust GREAT_PERSON_AVAILABLE detection). Run each probe with a direct FireTuner
connection.

- [x] **P1 scan** (2026-07-14, Korea turn 155): `build_attention_query(0, 4)`
      returns and parses — all 11 families, no ATTN_ERR — **but only in the
      InGame state**. In GameCore (the original `execute_read` call path),
      CITYHP/LOYALTY/WC/DIPLO ATTN_ERR with nil-API errors (`DefenseTypes`,
      `GetCulturalIdentity`, `Game.GetWorldCongress`, `DiplomacyManager` don't
      exist there) → SCAN_PARTIAL wake every turn, feature silently inert.
      Exactly the SCAN_PARTIAL-dominance failure this probe was gating on.
      Fixed: coordinator scan moved to `conn.execute_write`; live capture
      pinned as `LIVE_T155_LINES` in tests/arena/test_attention.py; context
      regression asserted in test_coordinator.py (999 tests green).
- [x] **P2 sleep** (2026-07-14, run `p2-attn-sleep-20260714T143323Z`, seat 5
      Sumeria/gemma4-26b): capture 1 woke NO_BASELINE (model played, 10 steps);
      captures 2-6 (turns 156-160) all **slept** — `turn_kind:"slept"`,
      step_count 0, streak 1→5; turn 161 woke STREAK_CAP with a 189-char
      digest injected. Result: `puppet_turns_played: 2, turns_slept: 5`
      (budget identity holds). Human seat verified restored post-run:
      `PuppetState(local=0, active=False)`. Bonus coverage: STREAK_CAP and
      digest-injection validated live, ahead of P4.
- [x] **P3 hostile wake** (2026-07-14, same run id): observed twice with live
      barbarians. Turn 193: ENEMY_NEAR, wake_detail "Builder d4 near Trader"
      (barbarian-held Builder, radius 4). Turn 217: the composite artifact —
      turn 216 slept, turn 217 woke ENEMY_NEAR "Builder d2 near Trader" with a
      292-char digest injected (digest "Woke because" line carries the unit
      string). A spawned barb Warrior (turn-172 attempt) was attacked and
      killed by the woken model the same turn — threat response works.
      **Two fixes fell out of this probe:**
      1. `ENDTURN_BLOCKING_UNITS` wake-loop: after any model-played turn the
         units are left awake, so this blocker rode every later capture and
         auto mode could never sleep again (turns 172, 182). Added to
         BLOCKER_IGNORE — the sleep path finish_units()es the seat, so it is
         auto-resolvable. Verified live: 8 sleeps after model-played turns.
      2. SCAN_ERROR diagnosability: parse-None wakes (turns 190, 212) carried
         empty detail and empty stderr. The coordinator now attaches a raw-line
         preview / missing-snapshot marker to wake_detail.
      Bonus live coverage: WC_SESSION wake (turn 204, digest 297ch) and
      ERA_CHANGED wake (turn 208, digest 344ch) fired naturally during probe
      cycles — both correct.
- [x] **P4 mini-run** (2026-07-14, run `p4-attn-minirun-20260714T163401Z-b`,
      seats 3 Byzantium/auto + 5 Sumeria/hybrid, max_streak 2): textbook shape
      — T222 both NO_BASELINE played, T223-224 both slept, T225 both woke
      STREAK_CAP with digests (288/291 chars). Budgets: turns_slept 4 +
      puppet_turns_played 4 == 8 captured. analyze renders the Attention
      section (per-player captured/slept/skip-rate/causes). Bonus: seat 5 in
      hybrid mode emitted a real model directive on its wake turn
      (`skip: 1`, 4 wake_if subscriptions) — directive parse validated live.
      A first take with seats 4+7 recorded zero sleeps because both seats had
      genuine trouble (ENEMY_NEAR / CITY_DAMAGED) — correct refusals, kept in
      run `p4-attn-minirun-20260714T162446Z`.
