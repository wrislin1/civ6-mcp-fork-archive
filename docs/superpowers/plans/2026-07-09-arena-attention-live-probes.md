# Attention & turn-skipping live-probe checklist

> **Status 2026-07-09 (feature branch `arena-attention-turn-skipping`):** the
> attention system (digest-based wake triggers, turn-skipping framework) and
> playbook guidance are **implementation-complete** at 935 tests passed. Live
> probes below exercise the core behaviors on a real game before merge. Fixtures
> pinned per P1 become the regression anchors; P2-P4 validate end-to-end workflows.

No greenfield-backed tool reaches production until its probe below captures a
real fixture or the spec records a degrade/cut decision.

**Preconditions:** the feature branch is checked out; a live game is loaded past
the early era (turn 50+ recommended for robust GREAT_PERSON_AVAILABLE detection).
Run each probe from the branch checkout with a direct FireTuner connection.

- [ ] **P1 scan:** `build_attention_query` returns and parses on a live game
      (all 11 families, no ATTN_ERR); pin the captured lines as a fixture in
      tests/arena/test_attention.py (the turn-380 fixture pattern).
- [ ] **P2 sleep:** an `auto`-mode puppet on a genuinely quiet seat sleeps; the
      turn advances; a `turn_kind:"slept"` record is written; human seat
      restored.
- [ ] **P3 hostile wake:** move a hostile unit within threat_radius of a puppet
      city (or use a live barbarian); next captured turn wakes with
      wake_cause=ENEMY_NEAR and the digest names the unit.
- [ ] **P4 mini-run:** 2-civ `hybrid` run end-to-end; transcript shows sleeps,
      a digest-injected wake, sane budgets (turns_slept + puppet_turns_played
      == captured turns), analyze renders the Attention section.
