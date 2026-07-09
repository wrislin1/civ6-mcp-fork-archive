# Arena Unofficial Channels — Private Bilateral LLM↔LLM Deals & Grievances (Design)

**Date:** 2026-07-09
**Status:** Approved by riz (brainstorming session, this date) — design capture for a **deferred** slice
**Predecessor:** Slice 4 (full toolset + era gating) merged at `b3540d8`; Attention & Turn-Skipping slice specced at `docs/superpowers/specs/2026-07-09-arena-attention-turn-skipping-design.md`.
**Sequencing:** This is the substance of roadmap item **A (LLM↔LLM interaction)**, which follows the attention slice in the D → A → C → B order. LLM↔LLM was explicitly *moved down*; this document captures the design and checks feasibility so it is ready when A comes up. It is **not** queued for immediate implementation.
**Scope decision (this session):** Channels only. The **autonomous seat-0** prerequisite is a *separate* future brainstorm + live-gate plan — see Appendix A for the carried-forward findings.

## Context & Motivation

The arena runs one LLM per civ seat. Today those LLMs never talk to each other: each
puppet turn is an isolated invocation whose prompt is assembled from a briefing +
`memory_block` + `task_block` + a turn announcement (`build_opening_prompt`,
`agent.py`), driven seat-by-seat by the coordinator (`coordinator.py`). Diplomacy, if
it happens at all, happens only through the game's official channels (`propose_trade`,
`send_diplomatic_action`, the World Congress) — all of which the game engine mediates
and enforces.

The **unofficial channels** add a side-band the game engine knows nothing about: a civ
can send another civ a free-text message and attach an *enforceable* structured deal
("destroy this barbarian camp and I'll pay you 100 gold"). The recipient can honor the
deal or not. Because the favor half of a deal is an action only the recipient can
voluntarily take, the game cannot force it — so a broken promise leaves an **unofficial
grievance**: an arena-tracked, private, bilateral reputation mark that colors future
dealings but never touches the game's own grievance system.

The research payoff is emergent social behavior: trust, reciprocity, betrayal, blackmail,
and — because unofficial grievances are invisible to the engine — wars that read as
**unprovoked** to every onlooker even though the aggressor feels wholly justified.

### Feasibility summary (verified against current code)

- **The plumbing already exists three times over.** Per-civ persisted state injected
  pre-turn and captured post-turn is exactly how `memory.py` (StandingMemory) and
  `task_tracker.py` (TaskState) work — schema-versioned JSON under the run dir, formatted
  into a prompt block by the coordinator (`coordinator.py:176-242`) and captured after the
  turn (`:357`). The unofficial channel is a **fourth instance of that same pattern**; no
  new architecture.
- **No new game-engine coupling.** Payments ride the existing `propose_trade` tool; the
  game guarantees the transfer once accepted. Everything else is arena-side bookkeeping +
  prompt injection. Nothing writes to the game's grievance/diplomacy engine.
- **Verification is offline-testable.** Each structured term reduces to a pure function
  over game-state snapshots, so verifiers are unit-testable without a live game.
- **Distinct from the game's real grievances.** Civ6 has its own grievance system, already
  surfaced read-only via `get_gossip` ("Grievances both directions per met civ plus recent
  gossip", `registry.py:1243`). Unofficial grievances are a *separate* arena ledger and are
  never merged into, or derived from, the game's grievances.

## Decisions (riz, this session)

Locked tenets, in the order they were settled:

1. **Arena-layer only.** Unofficial grievances never touch Civ6's grievance/diplomacy engine.
2. **Real-game verification.** A deal is "honored" only when the arena confirms the favor
   against actual game state — never on the recipient's say-so.
3. **Unprovoked-by-design.** An action driven by an unofficial grievance carries no official
   justification, so to the game and every other civ it reads as unprovoked and eats the
   normal warmonger/reputation cost.
4. **Private & bilateral.** A message or grievance is known only to the two parties. Nothing
   leaks automatically; a civ may *choose* to pass it on, but that is just another message.
   This is the property that makes #3 hold and makes betrayal both risky and deniable.
5. **Payment via real trades.** The gold leg of a deal goes through the game's real trade
   system (self-enforcing once accepted); "outside the grievance system" ≠ "outside the
   trade system."
6. **Free-text messages + structured deal terms.** Communication is unrestricted prose;
   only a *structured* term attached to a message is verified and can generate a grievance.
   Free prose is pure talk — the home of "complex long-term strategy" until model
   decision-making is strong enough to make prose commitments bite.
7. **Per-deal payment timing.** A `timing` flag (`up_front` | `on_delivery`) is set by the
   proposer; grievances can fire on either side depending on who defaults on the unfunded leg.

Three judgment calls, resolved with riz's approval (each may be revisited at plan time):

- **J1 — Explicit acceptance.** A deal is activated by an explicit `respond_to_deal(accept)`,
  not inferred from the payment trade. Clean, testable state machine.
- **J2 — Attribution = condition-met-by-deadline.** For a favor like `destroy_camp`, the
  condition being true by the deadline counts as honored regardless of *who* caused it. Simple
  and exact; the known edge case (a third party clears the camp and the payee is credited for
  nothing) is accepted for v1. Strict "counterparty's unit did it" is deferred (needs
  kill-attribution the game may not cleanly expose).
- **J3 — Slow decay.** A grievance's weight decays over ~N turns so recent betrayals bite and
  old ones fade, rather than persisting undiminished forever.

## Section 1 — Architecture & Data Model

A new module `src/civ_mcp/arena/channels.py`, sibling to `memory.py` and `task_tracker.py`,
owning per-run JSON state (schema-versioned) with three structures. **Every read is
scoped to a single civ**: a civ's injected view contains only rows where it is `from`/`to`,
`proposer`/`counterparty`, or a party to the grievance.

**Messages** — append-only log:
```
{ id, from_player, to_player, turn, text, deal_id? }
```

**Deals** — structured commitments:
```
{ id, proposer, counterparty,
  favor: { term_type, params },        # e.g. term_type="destroy_camp", params={x,y}
  payment: { gold: N },                # rides propose_trade
  timing: "up_front" | "on_delivery",
  deadline_turn,
  state: "proposed"|"declined"|"active"|"honored"|"broken"|"expired",
  created_turn,
  baseline_snapshot }                  # game state captured at creation, for the verifier
```

**Grievances** — per ordered pair `(wronged → offender)`:
```
{ id, wronged, offender, reason, deal_id, turn, magnitude, decay_ref }
```

Persistence mirrors the existing modules: `load_channels`/`save_channels`, a
`format_channel_block(player_id, ...)` that renders the civ's private inbox + active deals +
standing grievances into a prompt block, and a `SCHEMA_VERSION` constant.

## Section 2 — Tools

New registry entries, gated per-civ through the same tier/`filter_tools` mechanism every
other tool uses (`agent.py`). Each is a normal tool call and therefore consumes one step of
the civ's `max_steps` turn budget.

- **`send_message(to_player, text, deal=None)`** — free prose, plus an optional structured
  `deal` term (`favor`, `payment`, `timing`, `deadline_turn`). Creates a Message row and,
  if `deal` is present, a Deal row in state `proposed`.
- **`respond_to_deal(deal_id, accept|decline)`** — the recipient's handshake (J1). `accept`
  moves the deal to `active` and starts the obligation clock; `decline` closes it.

The recipient's **inbox is auto-injected** into its opening prompt (like `memory_block` /
`task_block`), so no explicit read tool is needed. New messages, active deals awaiting a
response, deals the civ owes on, and standing grievances all render in that block.

## Section 3 — Payment & the Real Trade System

The gold leg is executed through the existing `propose_trade` flow. The game guarantees the
transfer once both sides accept, so **payment itself is never verified by the arena** — the
engine already did. The risk lives entirely in the *unfunded* leg, which the `timing` flag
selects:

- **`up_front`** — payer pays now (real trade); the favor is still owed. If the favor is not
  verified by the deadline → grievance on the **counterparty**. (Payer bears the risk — the
  "I paid you and you did nothing" case.)
- **`on_delivery`** — the favor is verified first; the payment is still owed. If the payer
  does not complete the gold trade by the deadline → grievance on the **proposer**. (Payee
  bears the risk.)

The arena links a deal to its payment by observing the trade (exact observation mechanism —
trade-log read vs. gold-delta — is an implementation detail for the plan, not a design fork).

## Section 4 — Lifecycle & Verification

State machine:
```
proposed ──accept──▶ active ──▶ honored | broken
    │
    ├──decline──▶ declined
    └──deadline reached before accept──▶ expired
```
(`expired` is the never-accepted path only; once `active`, the terminal states are
`honored` or `broken` — no overlap between the two.)

- The proposer sets a **bounded** `deadline_turn`.
- At creation the arena captures `baseline_snapshot` for the favor term (e.g. "camp exists at
  (x,y)"; relevant gold balances).
- At/after the deadline (and optionally each turn) the term's **verifier** — a pure function
  over the baseline + live game state — rules the favor satisfied or not. **Attribution = J2**
  (condition-met-by-deadline).
- Verifier outcomes drive the terminal state and, on default, write a grievance.

**Starter term catalog (v1):** `pay_gold(N)`, `destroy_camp(x,y)`,
`dont_settle_within(r,x,y)`, `declare_war_on(civ)`, `keep_peace_with(civ, until_turn)`,
`spread_religion_to(city)`. Each ships with its own verifier + tests. The catalog is designed
to grow; free-text messages carry everything not yet in it.

## Section 5 — Grievance Model

- On a broken deal the arena writes a grievance with **`magnitude`** = the stiffed value
  (the gold amount, or a fixed unit per favor).
- **Decay (J3):** magnitude decays slowly over ~N turns; the effective weight surfaced to the
  model is the decayed value.
- **Surfacing — private, both directions.** The grievance renders in *both* parties' channel
  blocks: the wronged civ sees, e.g., "Rome took 100g on turn 42 and never destroyed the camp
  at (12,7)"; the offender sees "You owe Egypt a paid-for camp-kill; Egypt distrusts you." This
  is the entire behavioral lever — it drives retaliation, refusal of future deals, and the
  unprovoked-looking war. Nothing about a grievance is ever shown to a third party.

## Section 6 — Agency, Config & Cost

- Channel tools sit behind a per-civ **`channels` knob in the options fingerprint**
  (off/on), exactly like `memory` / `task_tracker` / `briefing`. Off by default; opt-in per
  experiment.
- The playbook (`playbook.md`) gains a short section nudging civs to consider unofficial
  diplomacy when it serves their victory path.
- Using the channel spends turn steps → real token cost (and, for `cli-claude`, real API
  spend). This is the reason it is opt-in and fingerprinted, so A/B arms can isolate its
  effect.

## Section 7 — Testing

- **Verifiers:** pure-function unit tests per term type over synthetic before/after
  game-state snapshots (offline; the pattern that makes `task_tracker` verifiers testable).
- **Lifecycle:** state-machine tests (propose → accept/decline → honored/broken/expired) with
  a fake clock and fake game state.
- **Privacy invariant:** a property test asserting `format_channel_block(p)` never contains a
  row `p` is not a party to — the single most important correctness guarantee (tenet #4).
- **Grievance decay:** deterministic decay-curve tests.
- **Coordinator wiring:** the block is injected pre-turn and channel state captured post-turn,
  mirroring the existing memory/task capture tests.
- **Fingerprint:** the `channels` knob appears in `CivOptions.fingerprint()`.
- **Live gate (deferred to plan):** one live run with two channel-enabled seats exchanging a
  real deal, since the `propose_trade` linkage and game-state verifiers can only be fully
  trusted against the real game.

## Non-Goals

- **No writes to the game's grievance/diplomacy engine** (tenet #1). Unofficial grievances
  stay arena-side.
- **No third-party propagation / gossip of unofficial deals or grievances** (tenet #4).
- **No shadow economy.** Gold moves only through real trades; the arena does not track a
  parallel currency.
- **No free-text adjudication in v1.** Only structured terms are verifiable; prose is talk.
- **No autonomous seat 0** (out of scope this session — Appendix A).
- **No synchronous, same-round negotiation.** Turns are sequential; v1 is asynchronous
  message-passing (a reply lands when the recipient next plays). Real-time bargaining is a
  later concern and is entangled with the seat-0 work.

## Open Items (for plan time)

- Bounds: max `deadline_turn` horizon; max active deals per pair; inbox/message-log pruning
  policy (cap + newest-first, like the gossip readout).
- Exact payment-observation mechanism for the `propose_trade` linkage (trade-log vs. delta).
- `magnitude` units (raw gold vs. normalized) and the decay half-life `N`.
- Whether declined/expired deals leave any trace (a soft "they wouldn't deal" signal) or vanish.
- Playbook wording and whether channel use should be nudged or purely emergent for a cleaner
  research signal.

---

## Appendix A — Carried-forward findings: Autonomous seat 0 (separate future spec)

riz wants LLM-controlled seat 0 (fully autonomous, no human in the loop). That is a **separate
brainstorm + live-gate plan**, not part of this slice, because it is live-Lua turn-mechanics
work with the opposite risk profile from the channels feature (which is offline-testable
Python). These findings are recorded here so they are not re-discovered from scratch:

- **Verified current state:** every puppet turn ends with `finish_units(K)` + `restore_local(0)`
  and **no** explicit end-turn (`coordinator.py:445-446`). A DESIGN NOTE directly above
  (`:439-444`) already spells out the fix. `hook.py` has no end-turn builder yet
  (`build_inject` / `build_finish_units` / `build_restore_local` only).
- **Two coupled fixes required:** (1) issue a real `UI.RequestAction(ACTION_ENDTURN)` for the
  local seat so it advances without a human click; (2) fix the seat-0 restore-to-self loop —
  `restore_local(0)` hands control back to the seat that just played, so an all-puppet game
  replays seat 0 forever (proven in the 8-civ smoke: seat 0 replayed 12/16 turns, seats 5-7
  never moved). Fix = restore to a non-active observer, or gate on turn-advance.
- **Hazard:** validation is only possible live against the running game, and stopping a watcher
  mid-AI-phase has hung the game and cost a save-reload. Requires the human-in-loop safety
  invariant (`Players[0]:IsTurnActive()==true` before any stop).
- **Relationship to channels:** channels do **not** depend on autonomy — seats 1..N can already
  message each other in the human-in-loop model. Autonomy turns channels from a human-advanced
  feature into a self-running all-LLM showcase.
- See memory `reference-arena-no-autonomous-mode` for the full operational detail.
