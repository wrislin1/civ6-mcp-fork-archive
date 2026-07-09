# Arena Attention Review-2 Fixes Implementation Plan

> **Status:** ✓ DONE — executed 2026-07-09 (11/11 tasks, commits
> `56fe296..abdfb7e`); merged to main at `7f1ac2c`. Do not re-execute.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the 10 confirmed findings (plus 2 quick follow-ups) from riz's second separate-session review of branch `arena-attention-turn-skipping`.

**Architecture:** All changes are point fixes inside the existing attention slice: fail-open hardening in `coordinator.py`/`attention.py`, Lua scan strictness in `_ATTENTION_LUA`, slept-record awareness in `analyze.py`, and one CLI validation in `arena.py`. No new modules, no new config knobs.

**Tech Stack:** Python 3.12, pytest + pytest-asyncio (in the `test` extra), Civ 6 FireTuner Lua (string-templated, offline-untestable — textual pins + the existing live probes P1–P4 cover it).

## Global Constraints

- Work in the existing worktree: `/home/riz/dev/civ6-mcp/.claude/worktrees/arena-attention-turn-skipping`, branch `arena-attention-turn-skipping` (currently at `331fe56`, 954 tests green). **Never merge to main, never push** — end state is commits on this branch + a summary for riz.
- Test command is always `uv run --extra test pytest tests/ -q` (bare `pytest` collects `scripts/` and fails on live-game imports; `pytest-asyncio` lives in the `test` extra, not the dev group).
- Fail-open invariant (attention.py module docstring): every attention failure degrades toward MORE model turns, never a blind sleep. Every fix below must preserve this direction.
- `"slept": true` = fast-path turn; `"skipped": true` = FAILED turn. Never conflate the keys.
- `run_lua` must never be added to the arena registry at any tier.
- All file paths below are relative to the worktree root.

## File Structure

| File | Changes |
|---|---|
| `src/civ_mcp/arena/coordinator.py` | Task 1 (evaluate guard), Task 2 (state_after gate), Task 7 (deadline_polls refill) |
| `src/civ_mcp/arena/attention.py` | Task 3 (CITYHP/LOYALTY Lua), Task 4 (NOTIFY wake-list), Task 5 (SKIP parse), Task 6 (subscription gating) |
| `src/civ_mcp/arena/analyze.py` | Task 8 (slept-aware metrics), Task 9 (attention-data guard) |
| `src/civ_mcp/arena/arena.py` | Task 10 (CLI validation) |
| `docs/superpowers/specs/2026-07-09-arena-attention-turn-skipping-design.md` | Task 11 (spec alignment) |
| `tests/arena/test_coordinator.py`, `test_attention.py`, `test_analyze.py`, `test_prompting.py`, `test_arena.py` | per-task tests |

Existing test fixtures to reuse in `tests/arena/test_coordinator.py` (defined near line 1530): `QUIET_SCAN_LINES`, `_ATTN_BASELINE_SNAPSHOT`, `AttnConn`, `CountingPolicy`, `FakeGSWithConn`, `FakeSink`.

---

### Task 1: STATE_CORRUPT guard — evaluate() must reset + wake, never abort (finding 1)

**Files:**
- Modify: `src/civ_mcp/arena/coordinator.py` (imports ~line 9; evaluate call ~line 285)
- Test: `tests/arena/test_coordinator.py`

**Interfaces:**
- Consumes: `attention.Decision` (existing dataclass, `attention.py:674`), `attention.AttentionState`
- Produces: new wake-cause string `"STATE_CORRUPT"` (Task 11 adds it to the spec vocabulary)

The load/scan `try/except` (coordinator.py:267-273) does not wrap `evaluate()`. `load_attention_state` validates only that `last_snapshot`/`last_scan`/`directive` are dicts — not their value types — so a dict-shaped-but-corrupt persisted file (`{"units":"5"}` → `int < str` TypeError in `_hard_triggers`; `directive={"wake_if":5}` → `tuple(5)` TypeError) propagates through the try/finally and kills `run_arena`, violating the module's "state corrupt → reset + wake" contract.

- [ ] **Step 1: Write the failing tests**

Add to `tests/arena/test_coordinator.py` (after the existing attention tests, near `test_tampered_slept_record_costs_digest_not_run`):

```python
@pytest.mark.asyncio
async def test_corrupt_snapshot_resets_and_wakes_not_aborts(tmp_path):
    """Review-2 finding 1: a dict-shaped but wrong-typed persisted snapshot
    passes load's shape validation and used to explode inside evaluate()'s
    comparisons, killing run_arena. Contract: reset + wake (STATE_CORRUPT)."""
    from civ_mcp.arena.attention import (
        AttentionState, load_attention_state, save_attention_state,
    )
    from civ_mcp.arena.config import AttentionOptions

    conn = AttnConn(); gs = FakeGSWithConn(conn); sink = FakeSink()
    opts = CivOptions(attention=AttentionOptions(mode="auto"))
    pol = CountingPolicy(opts)
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m", options=opts)],
                      max_puppet_turns=1, idle_poll_limit=5,
                      transcript_dir=str(tmp_path), run_id="rc1", puppet_ids=[1])
    corrupt = dict(_ATTN_BASELINE_SNAPSHOT)
    corrupt["units"] = "5"          # int < str -> TypeError in _hard_triggers
    save_attention_state(str(tmp_path), "rc1", 1, AttentionState(
        run_id="rc1", player_id=1, last_snapshot=corrupt,
        last_scan={"at_war_with": [], "era_index": 1, "total_population": 12}))

    result = await run_arena(conn, gs, cfg, policy=pol, transcript=sink)

    assert pol.calls == 1                       # woke; the run did not die
    assert result["puppet_turns_played"] == 1
    rec = sink.records[-1]
    assert rec["attention"]["wake_cause"] == "STATE_CORRUPT"
    healed = load_attention_state(str(tmp_path), "rc1", 1)
    assert healed.last_snapshot is not None
    assert healed.last_snapshot["units"] == 5   # note_wake rewrote the baseline


@pytest.mark.asyncio
async def test_corrupt_directive_resets_and_wakes_not_aborts(tmp_path):
    """Same contract for a corrupt directive: wake_if=5 makes the subscription
    tuple() call raise; must degrade to STATE_CORRUPT wake, not abort."""
    from civ_mcp.arena.attention import AttentionState, save_attention_state
    from civ_mcp.arena.config import AttentionOptions

    conn = AttnConn(); gs = FakeGSWithConn(conn); sink = FakeSink()
    opts = CivOptions(attention=AttentionOptions(mode="hybrid"))
    pol = CountingPolicy(opts)
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m", options=opts)],
                      max_puppet_turns=1, idle_poll_limit=5,
                      transcript_dir=str(tmp_path), run_id="rc2", puppet_ids=[1])
    save_attention_state(str(tmp_path), "rc2", 1, AttentionState(
        run_id="rc2", player_id=1,
        directive={"skip": 2, "wake_if": 5},    # dict-shaped, corrupt value
        skips_remaining=2,
        last_snapshot=dict(_ATTN_BASELINE_SNAPSHOT),
        last_scan={"at_war_with": [], "era_index": 1, "total_population": 12}))

    result = await run_arena(conn, gs, cfg, policy=pol, transcript=sink)

    assert pol.calls == 1
    assert result["puppet_turns_played"] == 1
    assert sink.records[-1]["attention"]["wake_cause"] == "STATE_CORRUPT"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra test pytest tests/arena/test_coordinator.py -q -k "corrupt_snapshot_resets or corrupt_directive_resets"`
Expected: both FAIL — the first with a `TypeError` escaping `run_arena` (`'<' not supported between instances of 'int' and 'str'`), the second with `TypeError: 'int' object is not iterable`.

- [ ] **Step 3: Implement the guard**

In `src/civ_mcp/arena/coordinator.py`, add `Decision` to the attention import block (line 9, alphabetical order — after `cancel_remainder`, before `evaluate`):

```python
from civ_mcp.arena.attention import (
    AttentionState,
    Decision,
    build_attention_query,
    cancel_remainder,
    evaluate,
    ...
)
```

(Note: keep the existing names; only insert `Decision`. The list is roughly alphabetical — `Decision` goes after `AttentionState`.)

Replace the bare `evaluate` call (currently ~line 285):

```python
                    decision = evaluate(
                        attention_mode, att_state, att_scan, state_before,
                        max_streak=opts.attention.max_streak, task_event=task_event,
                    )
```

with:

```python
                    try:
                        decision = evaluate(
                            attention_mode, att_state, att_scan, state_before,
                            max_streak=opts.attention.max_streak, task_event=task_event,
                        )
                    except Exception as e:
                        # Corrupt persisted values (dict-shaped but wrong-typed,
                        # e.g. last_snapshot={"units":"5"} or directive
                        # wake_if=5) pass load's shape check and explode inside
                        # evaluate's comparisons. Contract (attention.py module
                        # docstring): state corrupt -> reset + wake, never
                        # abort (review-2 finding 1). note_wake on the fresh
                        # state rewrites the baselines and its save self-heals
                        # the file.
                        att_state = AttentionState(run_id=run_id, player_id=st.local)
                        decision = Decision("wake", "STATE_CORRUPT")
                        print(f"[arena] attention evaluate failed; reset + wake: {e!r}",
                              file=sys.stderr)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: the same two-test command as Step 2.
Expected: 2 passed.

- [ ] **Step 5: Run the coordinator file and commit**

Run: `uv run --extra test pytest tests/arena/test_coordinator.py -q`
Expected: all pass.

```bash
git add src/civ_mcp/arena/coordinator.py tests/arena/test_coordinator.py
git commit -m "fix(arena): corrupt attention state resets + wakes (STATE_CORRUPT), never aborts the run"
```

---

### Task 2: state_after de-gated for attention — wake baseline must be post-play (finding 2)

**Files:**
- Modify: `src/civ_mcp/arena/coordinator.py:536`
- Test: `tests/arena/test_coordinator.py`

**Interfaces:**
- Consumes: `coordinator._overview_snapshot` (module-level async helper — monkeypatchable), `attention.load_attention_state`
- Produces: nothing new; `note_wake`'s `snapshot=` argument now receives a post-play snapshot even with transcripts off

`state_before` was widened to `(_tx_on or attention_on)` (line 190) but `state_after` still computes only under `_tx_on` (line 536). With attention on and transcripts off, `note_wake` (line 567) falls back to `state_before` — the next quiet turn's hard-trigger comparison then predates the puppet's own turn, masking `UNITS_LOST`/`CITY_COUNT_CHANGED`/`GOLD_CRASH` or spuriously waking on the civ's own actions.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_wake_baseline_is_post_play_with_transcripts_off(tmp_path, monkeypatch):
    """Review-2 finding 2: state_after was gated on _tx_on only, so with
    transcripts off note_wake stored the PRE-play snapshot as the next wake
    baseline -- the following quiet turn's hard triggers would compare
    against a state that predates the puppet's own actions."""
    from civ_mcp.arena import coordinator as coord
    from civ_mcp.arena.attention import load_attention_state
    from civ_mcp.arena.config import AttentionOptions

    calls = []
    async def fake_snapshot(_gs):
        calls.append(1)
        return {**_ATTN_BASELINE_SNAPSHOT, "units": 5 + len(calls)}
    monkeypatch.setattr(coord, "_overview_snapshot", fake_snapshot)

    conn = AttnConn(); gs = FakeGSWithConn(conn)
    opts = CivOptions(attention=AttentionOptions(mode="auto"))
    pol = CountingPolicy(opts)
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m", options=opts)],
                      max_puppet_turns=1, idle_poll_limit=5,
                      transcript_dir=str(tmp_path), run_id="rt2", puppet_ids=[1])

    # transcripts OFF; no seeded baseline -> NO_BASELINE wake
    result = await run_arena(conn, gs, cfg, policy=pol, transcript=None)

    assert result["puppet_turns_played"] == 1
    assert len(calls) == 2                       # before AND after now taken
    st = load_attention_state(str(tmp_path), "rt2", 1)
    assert st.last_snapshot["units"] == 7        # the POST-play (2nd) snapshot
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra test pytest "tests/arena/test_coordinator.py::test_wake_baseline_is_post_play_with_transcripts_off" -q`
Expected: FAIL — `len(calls) == 1` (state_after never computed) and `st.last_snapshot["units"] == 6`.

- [ ] **Step 3: Implement**

In `src/civ_mcp/arena/coordinator.py` line 536, replace:

```python
                state_after = await _overview_snapshot(gs) if _tx_on else None
```

with:

```python
                # Attention needs the POST-play snapshot as the next wake
                # baseline even with transcripts off (review-2 finding 2) --
                # note_wake's state_before fallback would otherwise bake the
                # puppet's own turn into the next quiet-turn delta.
                state_after = (
                    await _overview_snapshot(gs) if (_tx_on or attention_on) else None
                )
```

- [ ] **Step 4: Run test to verify it passes**

Run: same command as Step 2. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/coordinator.py tests/arena/test_coordinator.py
git commit -m "fix(arena): compute state_after for attention runs with transcripts off (stale wake baseline)"
```

---

### Task 3: CITYHP/LOYALTY Lua — inner pcalls must not swallow API errors (findings 3+4)

**Files:**
- Modify: `src/civ_mcp/arena/attention.py` (`_ATTENTION_LUA`: CITYHP family ~line 520, LOYALTY family ~line 540; provenance comment ~line 422; GP family comment ~line 572)
- Test: `tests/arena/test_attention.py`

**Interfaces:**
- Consumes: the existing `fam(name, fn)` Lua wrapper (its pcall emits `ATTN_ERR|<FAMILY>` on failure → `failed_families` → `SCAN_PARTIAL` wake)
- Produces: unchanged line protocol; failures now surface as `ATTN_ERR|CITYHP` / `ATTN_ERR|LOYALTY`

The spec says "a failed family is reported and → WAKE". The inner pcalls violate that: a `DefenseTypes`/`GetCulturalIdentity` failure leaves `damaged=`/`negative=` empty with no error sentinel, so a bombarded capital or a flipping city is blind-skipped. Deliberate deferral (record in the code comment): the **GP and TRADE** families keep their inner pcalls — they feed opt-in soft triggers only, and making their failures loud would wake every turn on quirky builds for far less benefit. Hard-trigger families must be strict.

- [ ] **Step 1: Write the failing test**

Add to `tests/arena/test_attention.py`:

```python
def _lua_family_segment(query: str, name: str, next_name: str) -> str:
    start = query.index(f'fam("{name}"')
    end = query.index(f'fam("{next_name}"')
    return query[start:end]


def test_hard_family_lua_propagates_errors():
    """Review-2 findings 3+4: CITYHP and LOYALTY must not swallow API errors
    in inner pcalls -- a failure has to reach fam()'s pcall so the family
    reports ATTN_ERR and evaluate() wakes on SCAN_PARTIAL."""
    q = build_attention_query(1, 4)
    assert "pcall" not in _lua_family_segment(q, "CITYHP", "LOYALTY")
    assert "pcall" not in _lua_family_segment(q, "LOYALTY", "WC")
```

(Family order in the template is ERA, WAR, POP, THREAT, CITYHP, LOYALTY, WC, GP, TRADE, DIPLO, BLOCKERS, NOTIFY — so CITYHP's segment ends at `fam("LOYALTY"` and LOYALTY's at `fam("WC"`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra test pytest "tests/arena/test_attention.py::test_hard_family_lua_propagates_errors" -q`
Expected: FAIL (both segments currently contain `pcall`).

- [ ] **Step 3: Implement**

In `_ATTENTION_LUA`, replace the CITYHP family body:

```lua
fam("CITYHP", function()
    local damaged = {}
    local ccIdx = GameInfo.Districts["DISTRICT_CITY_CENTER"].Index
    for _, c in p:GetCities():Members() do
        local garDmg, wallDmg = 0, 0
        for _, d in c:GetDistricts():Members() do
            if d:GetType() == ccIdx then
                -- No inner pcall: a GetDamage/DefenseTypes failure must reach
                -- fam()'s pcall -> ATTN_ERR|CITYHP -> SCAN_PARTIAL wake, never
                -- an empty damaged= that blind-skips a siege (review-2 f3).
                garDmg = d:GetDamage(DefenseTypes.DISTRICT_GARRISON) or 0
                wallDmg = d:GetDamage(DefenseTypes.DISTRICT_OUTER) or 0
                break
            end
        end
        if garDmg > 0 or wallDmg > 0 then
            table.insert(damaged, tostring(c:GetID()))
        end
    end
    print("ATTN|CITYHP|damaged=" .. table.concat(damaged, ","))
end)
```

and the LOYALTY family body:

```lua
fam("LOYALTY", function()
    local negative = {}
    for _, c in p:GetCities():Members() do
        -- No inner pcalls: a GetCulturalIdentity/GetLoyaltyPerTurn failure
        -- must reach fam()'s pcall -> ATTN_ERR|LOYALTY -> SCAN_PARTIAL wake,
        -- never an empty negative= that blind-skips a loyalty flip
        -- (review-2 f4).
        local pt = c:GetCulturalIdentity():GetLoyaltyPerTurn()
        if pt < 0 then
            table.insert(negative, tostring(c:GetID()))
        end
    end
    print("ATTN|LOYALTY|negative=" .. table.concat(negative, ","))
end)
```

Update the LOYALTY provenance comment (~line 422) — it currently says "degrade-tolerant double pcall around GetCulturalIdentity()/GetLoyaltyPerTurn()"; change to:

```python
#   LOYALTY  - cities.py:852-878 (_LOYALTY_LUA accessor names; unlike that
#              read tool, failures here PROPAGATE to fam() -> ATTN_ERR --
#              a hard-trigger family must wake on failure, not degrade)
```

In the GP family, add one comment line above its inner `pcall` (~line 572):

```lua
                    -- Soft-trigger tier: swallowing is deliberate here and in
                    -- TRADE (a loud failure would wake every turn for an
                    -- opt-in signal). Hard families must NOT copy this.
                    pcall(function()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra test pytest tests/arena/test_attention.py -q`
Expected: all pass (existing `SCAN_PARTIAL`/parse tests confirm the ATTN_ERR path is already handled Python-side).

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/attention.py tests/arena/test_attention.py
git commit -m "fix(arena): CITYHP/LOYALTY scan failures propagate to ATTN_ERR instead of blind-skipping"
```

---

### Task 4: NOTIFY prioritizes wake-list types within the 10-line cap (finding 5)

**Files:**
- Modify: `src/civ_mcp/arena/attention.py` (`_ATTENTION_LUA` header ~line 441, NOTIFY family ~line 639, `build_attention_query` ~line 663)
- Test: `tests/arena/test_attention.py`

**Interfaces:**
- Consumes: `NOTIFICATION_WAKE_LIST` (attention.py:298, four `NOTIFICATION_*` names)
- Produces: `build_attention_query` output now embeds the wake list; line protocol unchanged

`if emitted >= 10 then break` truncates in `GetList` order. `NOTIFICATION_SPY_CAUGHT` has no redundant trigger family (unlike CITY_UNDER_ATTACK → CITYHP, LOW_LOYALTY/REBELLION → LOYALTY), so on a busy turn it can be dropped and the seat sleeps through the espionage event. Fix: inject the Python-side wake list into the Lua and emit wake-list matches first (two passes, same 10-line cap).

- [ ] **Step 1: Write the failing test**

```python
def test_attention_query_embeds_wake_list_priority():
    """Review-2 finding 5: NOTIFY must emit wake-list types first so they can
    never be truncated out by the 10-line cap (SPY_CAUGHT has no redundant
    trigger family)."""
    q = build_attention_query(1, 4)
    for name in NOTIFICATION_WAKE_LIST:
        assert f'["{name}"]=true' in q
    assert "__WAKELIST__" not in q
```

(Import `NOTIFICATION_WAKE_LIST` at the top of the test file if not already imported.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra test pytest "tests/arena/test_attention.py::test_attention_query_embeds_wake_list_priority" -q`
Expected: FAIL (no wake-list entries in the query).

- [ ] **Step 3: Implement**

In `_ATTENTION_LUA`, after `local radius = __RADIUS__` add:

```lua
local wakeTypes = {__WAKELIST__}
```

Replace the NOTIFY family body:

```lua
fam("NOTIFY", function()
    local list = NotificationManager.GetList(me)
    if not list then return end
    local emitted = 0
    local function tryEmit(nid, wantWake)
        -- per-entry pcall (notifications.py:53-102 idiom): one malformed
        -- notification skips itself, not the rest of the list
        pcall(function()
            local entry = NotificationManager.Find(me, nid)
            if entry and not entry:IsDismissed() then
                local typeName = entry:GetTypeName() or "UNKNOWN"
                if (wakeTypes[typeName] == true) == wantWake then
                    local msg = (entry:GetMessage() or ""):gsub("|", "/")
                    print("ATTN|NOTIFY|type=" .. typeName .. "|msg=" .. msg)
                    emitted = emitted + 1
                end
            end
        end)
    end
    -- pass 1: wake-list types always make the cut, whatever their list
    -- position (review-2 f5: SPY_CAUGHT has no redundant trigger family)
    for _, nid in ipairs(list) do
        if emitted >= 10 then break end
        tryEmit(nid, true)
    end
    -- pass 2: fill the remaining slots with everything else, list order
    for _, nid in ipairs(list) do
        if emitted >= 10 then break end
        tryEmit(nid, false)
    end
end)
```

Replace `build_attention_query`:

```python
def build_attention_query(player_id: int, threat_radius: int) -> str:
    # sorted() so the query text is deterministic (test + cache friendliness)
    wake_entries = ", ".join(
        f'["{name}"]=true' for name in sorted(NOTIFICATION_WAKE_LIST)
    )
    return (
        _ATTENTION_LUA
        .replace("__PID__", str(int(player_id)))
        .replace("__RADIUS__", str(int(threat_radius)))
        .replace("__WAKELIST__", wake_entries)
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra test pytest tests/arena/test_attention.py tests/arena/test_coordinator.py -q`
Expected: all pass (coordinator fixtures feed canned scan lines, so they're insensitive to the Lua change; the two-pass emission itself is live-probe territory — P1 already exercises the real scan).

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/attention.py tests/arena/test_attention.py
git commit -m "fix(arena): NOTIFY scan emits wake-list notification types first (SPY_CAUGHT truncation)"
```

---

### Task 5: SKIP integer must lead the body — digit-bearing prose is not a directive (finding 6)

**Files:**
- Modify: `src/civ_mcp/arena/attention.py:60-97` (`parse_directive`)
- Test: `tests/arena/test_attention.py`

**Interfaces:**
- Consumes/Produces: `parse_directive(summary, max_skip) -> Directive | None` — signature unchanged; only the SKIP-body number extraction tightens.

`re.search(r"-?\d+", body)` grabs any stray digit, so `SKIP: hold until turn 340` (meaning *don't* skip yet) becomes a max-clamped blind skip — inverting fail-open. Anchor the integer at the start of the body (markdown decoration tolerated); anything else → no directive → wake.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("body", [
    "hold until turn 340",
    "maybe in 3 if peaceful",
    "after 2 more builds",
])
def test_digit_bearing_prose_is_not_a_directive(body):
    """Review-2 finding 6: a stray digit inside prose must not become a
    max-clamped blind skip -- no leading integer means no directive (wake)."""
    assert parse_directive(f"all quiet.\nSKIP: {body}", 5) is None


@pytest.mark.parametrize("body,expected", [
    ("3", 3),
    ("3 turns", 3),
    ("**3**", 3),
    ("`2`", 2),
])
def test_leading_integer_still_parses(body, expected):
    d = parse_directive(f"all quiet.\nSKIP: {body}", 5)
    assert d is not None and d.skip == expected
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra test pytest tests/arena/test_attention.py -q -k "digit_bearing_prose or leading_integer"`
Expected: the three prose cases FAIL (they currently parse as skip=5, 3, 2); the leading-integer cases pass already.

- [ ] **Step 3: Implement**

In `parse_directive`, replace:

```python
            num = re.search(r"-?\d+", m.group("body"))
            if num:
                n = int(num.group())
```

with:

```python
            # The integer must LEAD the body (markdown decoration tolerated):
            # "SKIP: 3" / "SKIP: 3 turns" / "SKIP: **3**" parse; digit-bearing
            # prose like "SKIP: hold until turn 340" must NOT become a
            # max-clamped blind skip (review-2 f6) -- no directive -> wake.
            num = re.match(r"[\s*_`~'\"(\[]*(-?\d+)", m.group("body"))
            if num:
                n = int(num.group(1))
```

Update the docstring's last sentence from `SKIP body must contain an integer ("SKIP: 3 turns" tolerated).` to:

```
SKIP body must START with an integer ("SKIP: 3 turns" tolerated;
digit-bearing prose like "SKIP: hold until turn 340" does not parse).
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra test pytest tests/arena/test_attention.py -q`
Expected: all pass (check no existing test asserted the mid-body-digit behavior; if one did, its assertion contradicts the fail-open invariant and should be updated to expect `None` with a comment citing review-2 f6).

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/attention.py tests/arena/test_attention.py
git commit -m "fix(arena): SKIP directive requires a leading integer; prose digits no longer blind-skip"
```

---

### Task 6: WAKE-IF subscriptions survive a spent directive in hybrid (finding 10)

**Files:**
- Modify: `src/civ_mcp/arena/attention.py:744-746` (`evaluate`)
- Test: `tests/arena/test_attention.py`

**Interfaces:**
- Consumes/Produces: `evaluate(...) -> Decision` — signature unchanged. Gating change only: soft triggers key on the directive **existing** (`state.directive["wake_if"]` non-empty), not on `skips_remaining > 0`. `note_wake` clears/replaces the directive on every wake, so a subscription never outlives its sleep streak; `cancel_remainder` keeping the directive means a failed-wake seat that re-sleeps still honors it (more wake conditions = fail-open direction).

In hybrid, once `skips_remaining` hits 0 the seat keeps auto-sleeping to STREAK_CAP but the soft block is bypassed — the model's explicit `WAKE IF: CITY_GREW` silently lapses mid-sleep.

- [ ] **Step 1: Write the failing test**

Add (reusing the module's existing quiet-scan fixture if one exists; otherwise this is self-contained):

```python
_QUIET_LINES_POP13 = [
    "ATTN|THREAT|count=0|nearest=", "ATTN|CITYHP|damaged=", "ATTN|WAR|with=",
    "ATTN|LOYALTY|negative=", "ATTN|WC|turns=5", "ATTN|ERA|index=1",
    "ATTN|POP|total=13", "ATTN|GP|available=0", "ATTN|TRADE|idle=0",
    "ATTN|DIPLO|pending=0", "ATTN|BLOCKERS|types=",
]

_SNAP = {
    "score": 50, "gold": 100.0, "science": 10.0, "culture": 8.0,
    "faith": 20.0, "research": "Mining", "civic": "Drama",
    "cities": 2, "units": 5,
}


def test_hybrid_honors_subscription_after_directive_spent():
    """Review-2 finding 10: hybrid keeps auto-sleeping once skips_remaining
    hits 0; the model's WAKE IF subscription must keep being honored for that
    whole streak, not silently lapse with the skip count."""
    st = AttentionState(
        directive={"skip": 3, "wake_if": ["CITY_GREW"]},
        skips_remaining=0, streak=3,
        last_snapshot=dict(_SNAP),
        last_scan={"at_war_with": [], "era_index": 1, "total_population": 12},
    )
    scan = parse_attention_scan(list(_QUIET_LINES_POP13))  # population 12 -> 13
    d = evaluate("hybrid", st, scan, dict(_SNAP), max_streak=5, task_event=False)
    assert d.action == "wake"
    assert d.wake_cause == "CITY_GREW"


def test_model_mode_spent_directive_still_wakes_no_directive():
    """Regression pin: model mode with a spent directive and a FALSE soft
    condition must still wake NO_DIRECTIVE (no accidental sleep)."""
    st = AttentionState(
        directive={"skip": 3, "wake_if": ["CITY_GREW"]},
        skips_remaining=0, streak=1,
        last_snapshot=dict(_SNAP),
        last_scan={"at_war_with": [], "era_index": 1, "total_population": 13},
    )
    scan = parse_attention_scan(list(_QUIET_LINES_POP13))  # 13 -> 13: no growth
    d = evaluate("model", st, scan, dict(_SNAP), max_streak=5, task_event=False)
    assert d.action == "wake"
    assert d.wake_cause == "NO_DIRECTIVE"
```

- [ ] **Step 2: Run tests to verify the first fails**

Run: `uv run --extra test pytest tests/arena/test_attention.py -q -k "subscription_after_directive_spent or spent_directive_still_wakes"`
Expected: `test_hybrid_honors_subscription_after_directive_spent` FAILS (hybrid returns `Decision("sleep")`); the model-mode pin passes.

- [ ] **Step 3: Implement**

In `evaluate`, replace:

```python
    directive_active = state.skips_remaining > 0
    if mode in ("model", "hybrid") and directive_active:
        subscribed = tuple((state.directive or {}).get("wake_if", []))
```

with:

```python
    directive_active = state.skips_remaining > 0
    # Subscriptions key on the directive EXISTING, not on skips remaining:
    # in hybrid the seat keeps auto-sleeping after the directive is spent,
    # and the model's explicit WAKE IF must keep being honored for that
    # whole streak (review-2 f10). note_wake clears/replaces the directive
    # on every wake, so a subscription never outlives its sleep streak.
    subscribed = tuple((state.directive or {}).get("wake_if", ()))
    if mode in ("model", "hybrid") and subscribed:
```

(The soft-check block body is unchanged; `directive_active` remains in use by the `mode == "model"` branch below.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra test pytest tests/arena/test_attention.py tests/arena/test_coordinator.py -q`
Expected: all pass (the existing `SOFT_TRUE_CONDITIONS` parametrized tests set an active directive, so they remain valid).

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/attention.py tests/arena/test_attention.py
git commit -m "fix(arena): hybrid honors WAKE-IF subscriptions after the skip count is spent"
```

---

### Task 7: deadline_polls becomes a consecutive-idle budget — slept turns must not end the run (finding 8)

**Files:**
- Modify: `src/civ_mcp/arena/coordinator.py` (init comment ~line 174; active-branch top ~line 182)
- Test: `tests/arena/test_coordinator.py`

**Interfaces:**
- Consumes: `config.idle_poll_limit` (existing)
- Produces: `deadline_polls` refilled to `config.idle_poll_limit` on every captured puppet turn (played, slept, or failed all flow through the same active branch)

`deadline_polls` (default 600) is initialized once and decremented on every loop iteration — slept turns burn it without decrementing `remaining`, so a healthy quiet game with long sleep streaks exits well short of `max_puppet_turns`. Its own comment says it exists for "human may take a while to end their turn" — i.e. idle waiting. Refill on capture makes it exactly that. Boundedness is preserved: played/failed turns consume `remaining`, and sleeping is capped at `max_streak` consecutive per seat before a STREAK_CAP wake (a played turn), so total captured turns stay bounded by `remaining × (max_streak + 1)` even with `max_game_turns=0`. The existing `test_coordinator_respects_idle_poll_limit` (pure idle, zero captures) is unaffected.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_slept_turns_refill_idle_budget(tmp_path):
    """Review-2 finding 8: slept turns burned deadline_polls (never refilled)
    while leaving `remaining` untouched, so a quiet game could end far short
    of its budget. A captured turn is activity: it must refill the budget."""
    from civ_mcp.arena.attention import AttentionState, save_attention_state
    from civ_mcp.arena.config import AttentionOptions

    conn = AttnConn(); gs = FakeGSWithConn(conn); sink = FakeSink()
    opts = CivOptions(attention=AttentionOptions(mode="auto", max_streak=10))
    pol = CountingPolicy(opts)
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m", options=opts)],
                      max_puppet_turns=1, max_game_turns=5, idle_poll_limit=3,
                      transcript_dir=str(tmp_path), run_id="r8", puppet_ids=[1])
    conn._polls = iter([
        ["LOCAL|1", f"TURN|{t}", "ACTIVE|true", "LAST|1"] for t in range(2, 9)
    ])
    save_attention_state(str(tmp_path), "r8", 1, AttentionState(
        run_id="r8", player_id=1,
        last_snapshot=dict(_ATTN_BASELINE_SNAPSHOT),
        last_scan={"at_war_with": [], "era_index": 1, "total_population": 12}))

    result = await run_arena(conn, gs, cfg, policy=pol, transcript=sink)

    # idle_poll_limit=3 < 5 slept turns: pre-fix the run died after 3 polls.
    assert result["turns_slept"] == 5     # stopped by max_game_turns, not the idle budget
    assert pol.calls == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra test pytest "tests/arena/test_coordinator.py::test_slept_turns_refill_idle_budget" -q`
Expected: FAIL with `turns_slept == 3`.

- [ ] **Step 3: Implement**

In `run_arena`, update the init comment (line 174):

```python
        deadline_polls = config.idle_poll_limit  # consecutive-idle poll budget; refilled on every captured turn
```

and at the top of the active branch (line ~182, right where `idle_streak` resets):

```python
            if st.active and st.local in puppet_ids:
                idle_streak = 0
                # A captured puppet turn is ACTIVITY: refill the idle budget.
                # deadline_polls means "consecutive polls with nothing to do",
                # not a whole-run cap that slept turns burn through without
                # consuming max_puppet_turns (review-2 f8).
                deadline_polls = config.idle_poll_limit
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra test pytest tests/arena/test_coordinator.py -q`
Expected: all pass, including `test_coordinator_respects_idle_poll_limit`.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/coordinator.py tests/arena/test_coordinator.py
git commit -m "fix(arena): deadline_polls is a consecutive-idle budget; slept turns no longer end the run early"
```

---

### Task 8: config_summary / behavior_metrics become slept-aware (finding 7)

**Files:**
- Modify: `src/civ_mcp/arena/analyze.py` (`behavior_metrics` ~line 206, `config_summary` ~line 312)
- Test: `tests/arena/test_analyze.py`

**Interfaces:**
- Consumes: `_turn_kind(rec)` (analyze.py:379 — module-level, definition order doesn't matter at runtime)
- Produces: `config_summary` aggregates played records only; `behavior_metrics` gates driver + standing-memory tallies on played, keeps task-result classification over ALL records (pre-model task follow-through genuinely runs on slept turns)

Slept records (step_count 0, no briefing_tokens, no `civ_options` key) flow into the Slice-3 aggregations: averages deflate, driver counts inflate, and in-process civs split into a spurious empty-config fingerprint group.

- [ ] **Step 1: Write the failing tests**

Add to `tests/arena/test_analyze.py`:

```python
def test_config_summary_ignores_slept_records():
    """Review-2 finding 7: slept records (step_count 0, no civ_options) must
    not deflate averages or split a civ into a spurious empty-config group."""
    played = {
        "player_id": 1, "provider": "local", "model": "m",
        "civ_options": {"tools": "minimal"}, "turn_kind": "played",
        "step_count": 4, "briefing_tokens": 100,
        "state_delta": {"score": 2},
    }
    slept = {
        "player_id": 1, "provider": "local", "model": "m",
        "turn_kind": "slept", "slept": True, "step_count": 0,
        "state_delta": {"score": 1},
    }
    summary = config_summary([played, slept, dict(played)])
    assert list(summary) == ["1"]      # ONE group -- no empty-config split
    entry = summary["1"]
    assert entry["turns"] == 2         # played turns only
    assert entry["avg_steps"] == 4.0   # not deflated by the slept 0
    assert entry["avg_briefing_tokens"] == 100.0


def test_behavior_metrics_drivers_played_only_tasks_still_counted():
    """Drivers/standing-memory tally MODEL turns; slept-turn task
    follow-through is real behavior and must still count."""
    played = {"player_id": 1, "driver": "in_process", "turn_kind": "played",
              "standing_memory": {"injected": True, "injected_chars": 10,
                                  "captured_chars": 5}}
    slept = {"player_id": 1, "driver": "in_process", "turn_kind": "slept",
             "slept": True,
             "task_tracker": {"active_before": 1, "active_after": 0,
                              "pre_model_results": [
                                  {"kind": "settle", "action": "found_city",
                                   "status": "complete", "result": "city founded"}]}}
    m = behavior_metrics([played, slept])
    assert m["drivers"] == {"in_process": 1, "cli": 0}  # slept != a model turn
    assert m["standing_memory_turns"] == 1
    assert m["task_completed"] == 1                     # slept follow-through counts
    assert m["puppeted_players"] == [1]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra test pytest tests/arena/test_analyze.py -q -k "ignores_slept or drivers_played_only"`
Expected: both FAIL (`summary` has two groups / `drivers["in_process"] == 2`).

- [ ] **Step 3: Implement**

`config_summary` — filter at the top and extend the docstring:

```python
def config_summary(records: list[dict]) -> dict:
    """Return per-player experiment config fingerprints and outcome averages.

    Played turns only: slept records carry step_count=0, no briefing_tokens
    and no civ_options, so counting them would deflate every average and
    split an in-process civ into a spurious empty-config fingerprint group
    (review-2 f7). attention_metrics owns the slept-turn story.
    """
    records = [rec for rec in records if _turn_kind(rec) == "played"]
```

`behavior_metrics` — gate the driver and standing-memory tallies (task classification stays over all records):

```python
    for rec in transcript_records:
        pid = rec.get("player_id")
        if pid is not None:
            puppeted_players.add(pid)

        # Driver and standing-memory tallies are per MODEL turn; slept
        # records (no model invocation) would inflate them (review-2 f7).
        # Task results below still count on every record:
        # run_pre_model_tasks executes on slept turns too, so their
        # follow-through is real behavior.
        if _turn_kind(rec) == "played":
            if _is_local_driver(rec):
                drivers["in_process"] += 1
            else:
                drivers["cli"] += 1
            if _standing_memory_injected(rec):
                standing_memory_turns += 1
            if _standing_memory_captured(rec):
                standing_memory_captured_turns += 1

        if _task_tracker_active(rec):
            task_tracker_turns += 1

        task_counts = _classify_task_results(rec)
        ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra test pytest tests/arena/test_analyze.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/analyze.py tests/arena/test_analyze.py
git commit -m "fix(arena): config_summary/behavior_metrics no longer distorted by slept records"
```

---

### Task 9: _has_attention_data keys on real attention markers (finding 9)

**Files:**
- Modify: `src/civ_mcp/arena/analyze.py:596-607`
- Test: `tests/arena/test_analyze.py`

**Interfaces:**
- Consumes: transcript records — the `attention` dict is written only when a civ's attention mode is on (both slept and woken records); `turn_kind: "played"` is written on EVERY transcripts-on record (coordinator.py:639, pinned by `test_off_mode_bit_for_bit_today`).
- Produces: `_has_attention_data` unchanged signature.

`turn_kind` being unconditional means any transcripts-on run trips the guard and `render_markdown` emits an all-zeros "## Attention" table — exactly what the guard's docstring says it prevents. Fix the guard, not the coordinator (`turn_kind` is a useful schema field on every record).

- [ ] **Step 1: Write the failing test**

```python
def test_has_attention_data_ignores_bare_turn_kind():
    """Review-2 finding 9: turn_kind:"played" is written on EVERY
    transcripts-on record regardless of attention mode -- it must not trip
    the guard and grow an all-zeros Attention section for attention-off runs."""
    attention_off = [{"player_id": 1, "turn_kind": "played", "step_count": 3}]
    assert _has_attention_data(attention_off) is False

    assert _has_attention_data([{"attention": {"decision": "woke"}}]) is True
    assert _has_attention_data([{"slept": True}]) is True
    assert _has_attention_data([]) is False
```

(Import `_has_attention_data` in the test module if not already imported. Also grep for existing tests pinning the old guard: `grep -n "_has_attention_data" tests/arena/test_analyze.py` — if one asserts that `turn_kind` alone trips it, update it to use an `attention`-carrying record with a comment citing review-2 f9.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra test pytest "tests/arena/test_analyze.py::test_has_attention_data_ignores_bare_turn_kind" -q`
Expected: FAIL on the first assert.

- [ ] **Step 3: Implement**

Replace `_has_attention_data`:

```python
def _has_attention_data(records: list[dict]) -> bool:
    """True if any record carries a real attention/turn-skipping marker.

    ``turn_kind: "played"`` is written on EVERY transcripts-on record
    regardless of attention mode (a schema field, not a feature marker), so
    it must not trip this guard (review-2 f9). The ``attention`` dict is
    written only when a civ's attention mode is on -- on slept records and
    woken played records alike -- so its presence (or ``slept: True``) is
    the signal. Pre-feature transcripts stay falsy and ``render_markdown``
    keeps skipping the section.
    """
    return any(
        rec.get("slept") is True or "attention" in rec for rec in records
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra test pytest tests/arena/test_analyze.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/analyze.py tests/arena/test_analyze.py
git commit -m "fix(arena): attention report section only renders for runs that had attention on"
```

---

### Task 10: Follow-ups — max_skip drift pin + CLI --max-game-turns validation (reviewer scope notes)

**Files:**
- Modify: `src/civ_mcp/arena/arena.py` (`resolve_config`, right after `max_game_turns_arg = getattr(...)`)
- Test: `tests/arena/test_prompting.py`, `tests/arena/test_arena.py` (place beside the existing `resolve_config` tests — `grep -n "resolve_config" tests/arena/*.py` to find them)

**Interfaces:**
- Consumes: `prompting.ATTENTION_INSTRUCTION`, `config.AttentionOptions`, `agent.load_playbook`, `arena.build_args`/`resolve_config`
- Produces: no behavior change to prompting (test-only pin); `resolve_config` now rejects a negative `--max-game-turns` with `SystemExit`

Two quick follow-ups the reviewer flagged: (a) `SKIP: <1-5>` in `prompting.py:32` and `SKIP n (1-5)` in `playbook.md:169` hardcode the range while `max_skip` is configurable — pin the literals to the default so drift breaks loudly (same idiom as the existing SOFT_TRIGGERS sync test, see prompting.py:28-31); (b) the YAML path validates `max_game_turns >= 0` (`experiment.py:332`) but the CLI path doesn't — a negative silently reads as *uncapped* via the `max_game_turns <= 0` loop guard.

- [ ] **Step 1: Write the tests**

`tests/arena/test_prompting.py`:

```python
def test_attention_instruction_skip_range_matches_default_max_skip():
    """The '1-5' range is literal prompt text on purpose (no invisible
    drift); this pin breaks loudly if AttentionOptions.max_skip's default
    changes without the prompt following (review-2 scope note)."""
    from civ_mcp.arena.agent import load_playbook
    from civ_mcp.arena.config import AttentionOptions

    default = AttentionOptions().max_skip
    assert f"<1-{default}>" in ATTENTION_INSTRUCTION
    assert f"SKIP n (1-{default})" in load_playbook()
```

`tests/arena/test_arena.py`:

```python
def test_negative_max_game_turns_rejected_on_cli():
    """YAML validates max_game_turns >= 0; the CLI path must too -- a
    negative silently means 'uncapped' via the `<= 0` loop guard
    (review-2 scope note)."""
    args = build_args(["--player", "1:local:m", "--max-game-turns", "-3"])
    with pytest.raises(SystemExit):
        resolve_config(args)
```

- [ ] **Step 2: Run tests to verify status**

Run: `uv run --extra test pytest tests/arena/test_prompting.py tests/arena/test_arena.py -q -k "skip_range_matches or negative_max_game_turns"`
Expected: the prompting pin PASSES already (it's a drift guard — that's fine, keep it); the CLI test FAILS (no SystemExit raised).

- [ ] **Step 3: Implement the CLI validation**

In `src/civ_mcp/arena/arena.py`, `resolve_config`, immediately after `max_game_turns_arg = getattr(args, "max_game_turns", None)`:

```python
    if max_game_turns_arg is not None and max_game_turns_arg < 0:
        # The YAML path enforces >= 0 (experiment._top_non_negative_int); a
        # negative from the CLI silently reads as "uncapped" through the
        # `max_game_turns <= 0` loop guard (review-2 scope note).
        raise SystemExit("--max-game-turns must be an integer >= 0")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: same command as Step 2. Expected: both pass.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/arena.py tests/arena/test_prompting.py tests/arena/test_arena.py
git commit -m "fix(arena): reject negative --max-game-turns; pin prompt SKIP range to max_skip default"
```

---

### Task 11: Spec alignment, ledger record, full gate

**Files:**
- Modify: `docs/superpowers/specs/2026-07-09-arena-attention-turn-skipping-design.md` (the worktree copy, committed on this branch)
- Modify: `.superpowers/sdd/progress.md` (gitignored ledger — edit, do not commit)

**Interfaces:** documentation only.

- [ ] **Step 1: Update the spec**

Make these edits so the spec matches the post-fix behavior (find each section by its heading/keyword):

1. Wake-cause vocabulary (section 3, the cause list): add `STATE_CORRUPT` — *"persisted attention state was dict-shaped but wrong-typed; state reset + wake (never abort)"*.
2. Soft-trigger gating (section 3/4 wherever `WAKE IF` subscriptions are described): change "while a directive is active (skips remaining)" phrasing to *"while the issuing directive stands (cleared/replaced on every wake) — in hybrid, subscriptions keep being honored through the auto-sleep tail after the skip count is spent"*.
3. Scan contract (the family list): note that CITYHP/LOYALTY failures propagate to `ATTN_ERR` (hard-trigger families are strict; GP/TRADE deliberately degrade-tolerant), and that NOTIFY emits wake-list types first within its 10-line cap.
4. Config contract (where `idle_poll_limit` appears, or add one line): `idle_poll_limit` is a **consecutive-idle** poll budget, refilled on every captured turn.

- [ ] **Step 2: Append the fix-wave record to the ledger**

Append to `.superpowers/sdd/progress.md`: review-2 (10 findings, all confirmed on verification), the 11 commits of this plan, test count before/after, and the two deliberate deferrals (GP/TRADE inner pcalls stay degrade-tolerant — soft-trigger tier; `directive_ack`/record-shape cleanup items skipped as YAGNI).

- [ ] **Step 3: Full suite gate**

Run: `uv run --extra test pytest tests/ -q`
Expected: 0 failures, total ≥ 970 (954 baseline + ~16 new tests). Also run `git status --short` — only the spec doc staged; ledger dirty is expected (gitignored).

- [ ] **Step 4: Commit the spec**

```bash
git add docs/superpowers/specs/2026-07-09-arena-attention-turn-skipping-design.md docs/superpowers/plans/2026-07-09-arena-attention-review2-fixes.md
git commit -m "docs(arena): align attention spec with review-2 fixes; add review-2 fix plan"
```

(This also lands the plan document itself on the branch. **Do not merge or push.**)

---

## Self-Review Notes

- Spec coverage: all 10 findings map to Tasks 1–9 (findings 3+4 share Task 3's shape); both named follow-ups are Task 10; the reviewer's cleanup items (record-shape dedup, `directive_ack` re-parse) are deliberately deferred (YAGNI) and recorded in the ledger.
- Type consistency: `Decision("wake", "STATE_CORRUPT")` uses the existing positional `(action, wake_cause)` shape; `subscribed`/`directive_active` names match `evaluate`'s current body; `_turn_kind` is module-level in analyze.py so `config_summary` can call it despite being defined earlier in the file.
- Order sensitivity: Task 1's corrupt-directive test passes both before and after Task 6 (the `tuple()` raise happens in either gating shape). Task 7's test needs `max_game_turns=5` to terminate — do not drop it.
