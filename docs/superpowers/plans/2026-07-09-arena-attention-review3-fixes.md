# Arena Attention Review-3 Fixes Implementation Plan

> **Status:** ✓ DONE — executed 2026-07-09 (10/10 tasks + final-review fixups,
> commits `e1f1822..7f1ac2c`); merged to main at `7f1ac2c`. Do not re-execute.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the 6 actionable residuals from riz's third review of branch `arena-attention-turn-skipping` (re-review of the 12-commit fix wave at `abdfb7e`), plus 3 observability/robustness improvements the review's remaining findings reduce to after adjudication.

**Architecture:** All changes stay inside the existing attention/analyze/prompting modules. The unsafe-direction finding (f1, masked WAKE-IF drop) is fixed at both load time (validation → fresh state → wake) and evaluate time (raise → STATE_CORRUPT guard). The Lua scan gains error-detail plumbing (`ATTN_ERR|FAM|<err>` → `AttentionScan.failure_details` → `wake_detail` → transcript) so strict-family wakes are diagnosable post-run without weakening the strictness riz mandated in review-2. Analytics gaps (f4, f7) get the same `_turn_kind == "played"` gate the sibling sites already have. No behavior of the deliberately-strict CITYHP/LOYALTY families changes.

**Tech Stack:** Python 3.12, pytest (+pytest-asyncio via the `test` extra), string-templated Lua (no Lua runtime available offline).

## Global Constraints

- Work in worktree `/home/riz/dev/civ6-mcp/.claude/worktrees/arena-attention-turn-skipping`, branch `arena-attention-turn-skipping`, base commit `abdfb7e` (974 tests green).
- **Never merge to main, never push. End state is commits on this branch + a summary for riz.**
- Test command is always `uv run --extra test pytest tests/ -q` for full runs (bare `pytest` collects `scripts/` and fails on live-game imports; pytest-asyncio lives in the `test` extra). Focused TDD runs: `uv run --extra test pytest tests/arena/<file>.py -q`.
- **Fail-open invariant:** every attention failure degrades toward MORE model turns, never a blind sleep. `"slept": true` = fast-path turn; `"skipped": true` = FAILED turn. Never conflate them.
- **`run_lua` must never be added to the arena registry at any tier.**
- No `lua`/`luajit` binary exists on this machine. Lua template changes are verified by textual pins in tests plus a hand-verified block-balance count (`function`/`then`/`do` vs `end`), as done for commit `cbb608a`.
- GP/TRADE inner pcalls are deliberately degrade-tolerant (soft tier) — do not touch them.

## Review-3 Adjudications & Non-Goals

These are controller decisions made after verifying all 9 findings against the code. Reviewers of individual tasks: the scope below is deliberate.

1. **f3 (CITYHP/LOYALTY strictness → possible permanent SCAN_PARTIAL): strictness stays.** It is riz's review-2 decision (review-2 f3/f4) and the reviewer concedes it is contract-safe. The actionable residue is diagnosability, fixed by Task 2 (error text flows to `wake_detail` and the transcript). Live probe P1 remains the gate for SCAN_PARTIAL dominance.
2. **f5 (broad `except Exception` STATE_CORRUPT guard): the broad catch stays.** Narrowing it reintroduces the abort class review-2 f1 was raised to kill; fail-open trumps loudness. Mitigation: Task 1 retires the known non-raising corruption class at load, Task 4 records the caught exception's `repr` in the transcript (`wake_detail`) so a mislabeled logic regression is diagnosable per-record, not just via stderr.
3. **f6 (digest degrade to `""`): the broad catch stays; the degrade target changes.** Task 5 degrades to a one-line stub that preserves the fact and length of the sleep plus the error, instead of silently erasing the recap.
4. **Cleanup-tier `deadline_polls -= 1` at coordinator.py:389: NOT removed — the reviewer's "redundant" claim is incorrect.** The loop's per-iteration decrement at coordinator.py:702 is skipped by the slept path's `continue`; line 389 preserves the one-decrement-per-iteration invariant. Removing it would change behavior (a slept turn would stop consuming an idle-budget slot). The `idle_poll_limit==1` degenerate case applies equally to played turns via line 702 and is a config edge, not a slept-path bug. No task.
5. **arena.py:94 duplicate negativity check:** deliberate and commented (CLI path vs YAML path), behaviorally consistent. No task.
6. **NOTIFY two-pass ~2n engine-call cost:** already a recorded P1 watch item. No task.
7. **Four-similar-except-blocks / inline `_turn_kind` predicate factoring:** style tier; stays on the deferred-minors list.

## Model Selection Hints (for the SDD controller)

| Task | Tier | Why |
|---|---|---|
| 1, 2, 3 | sonnet | attention.py integration + Lua template edits |
| 4, 5 | sonnet | coordinator integration, async tests |
| 6, 7 | haiku | transcription-grade single-file gates |
| 8 | sonnet | 3-file plumbing (prompting/agent/cli_agent) |
| 9 | haiku | single regex + parametrized tests |
| 10 | haiku | spec prose alignment, no code |

Task reviewers: sonnet. Final whole-branch review: most capable available model.

---

### Task 1: Validate directive value types at load; raise on non-list wake_if in evaluate (review-3 f1)

A persisted `wake_if: "CITY_GREW"` (string) loads clean today, `tuple("CITY_GREW")` becomes a 9-char tuple without raising, and the model's explicit subscription is silently dropped — a masked skip (unsafe direction) that never self-heals. Fix at both layers: `load_attention_state` rejects wrong-typed directive values (→ fresh state → NO_BASELINE wake → save self-heals, the established corrupt-file contract), and `evaluate` raises `TypeError` on a non-list `wake_if` so state constructed outside load hits the coordinator's STATE_CORRUPT guard instead of sleeping through the subscribed event. Note: `subscribed` is computed above the mode gate, so auto mode with a corrupt `wake_if` now wakes STATE_CORRUPT instead of sleeping — that is the safe direction and deliberate.

**Files:**
- Modify: `src/civ_mcp/arena/attention.py` (load_attention_state ~line 136-142; evaluate ~line 774-781)
- Test: `tests/arena/test_attention.py`

**Interfaces:**
- Consumes: existing test helpers in `tests/arena/test_attention.py`: `_st(...)` (AttentionState factory), `QUIET` (parsed quiet scan), `SNAP` (baseline snapshot dict), `attention_path`, `load_attention_state`.
- Produces: no signature changes. `load_attention_state` returns a fresh `AttentionState` for directive-value corruption; `evaluate` raises `TypeError` for non-list `wake_if`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/arena/test_attention.py` (near the existing load/save tests; reuse this file's existing imports — `attention_path`, `load_attention_state` are already imported at ~line 68):

```python
def _write_state_json(tmp_path, payload):
    import json
    path = attention_path(str(tmp_path), "run1", 3)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


_VALID_STATE_PAYLOAD = {
    "schema_version": 1, "run_id": "run1", "player_id": 3,
    "directive": {"skip": 2, "wake_if": ["CITY_GREW"]},
    "skips_remaining": 2, "streak": 1, "last_wake_turn": 10,
    "last_snapshot": {"units": 3}, "last_scan": {"era_index": 1},
    "slept": [], "directive_ack": "",
}


def test_load_accepts_valid_directive(tmp_path):
    _write_state_json(tmp_path, _VALID_STATE_PAYLOAD)
    st = load_attention_state(str(tmp_path), "run1", 3)
    assert st.directive == {"skip": 2, "wake_if": ["CITY_GREW"]}
    assert st.skips_remaining == 2


@pytest.mark.parametrize("wake_if", [
    "CITY_GREW",             # str: tuple()s to 9 char tokens, never matches
    {"CITY_GREW": True},     # dict: tuple()s to keys today, still wrong type
    [1, 2],                  # list of non-str
    ["CITY_GREW", 5],        # mixed
])
def test_load_rejects_corrupt_wake_if(tmp_path, wake_if):
    """Review-3 f1: wake_if that isn't a list of str must NOT load clean --
    the model's subscription would be silently dropped (masked skip, unsafe
    direction). Corrupt file -> fresh state -> wake -> save self-heals."""
    _write_state_json(tmp_path, {
        **_VALID_STATE_PAYLOAD, "directive": {"skip": 2, "wake_if": wake_if},
    })
    st = load_attention_state(str(tmp_path), "run1", 3)
    assert st.directive is None and st.last_snapshot is None  # fresh


def test_load_rejects_non_int_directive_skip(tmp_path):
    _write_state_json(tmp_path, {
        **_VALID_STATE_PAYLOAD, "directive": {"skip": "2", "wake_if": []},
    })
    assert load_attention_state(str(tmp_path), "run1", 3).directive is None


def test_evaluate_raises_on_non_list_wake_if():
    """Backstop for state constructed outside load (review-3 f1): raising
    reaches the coordinator's STATE_CORRUPT reset+wake guard instead of a
    masked sleep through the subscribed event."""
    st = _st(skips_remaining=2, directive={"skip": 3, "wake_if": "CITY_GREW"})
    with pytest.raises(TypeError):
        evaluate("model", st, QUIET, SNAP, max_streak=5, task_event=False)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra test pytest tests/arena/test_attention.py -q -k "corrupt_wake_if or non_int_directive_skip or raises_on_non_list or accepts_valid_directive"`
Expected: the reject/raise tests FAIL (state loads clean / no raise); `test_load_accepts_valid_directive` PASSES (guard against over-rejection).

- [ ] **Step 3: Implement**

In `load_attention_state`, inside the existing `try:` block, immediately after the dict-or-None shape loop over `("directive", "last_snapshot", "last_scan")`:

```python
        directive = data.get("directive")
        if directive is not None:
            # Value-type validation (review-3 f1): a dict-shaped directive
            # with wake_if as a str tuple()s into per-character tokens
            # WITHOUT raising -- the one corruption class that produces a
            # masked skip instead of a loud failure. Reject it here so the
            # established contract (corrupt file -> fresh state -> wake ->
            # save self-heals) covers it.
            if not isinstance(directive.get("skip"), int):
                raise TypeError("directive.skip must be an int")
            wake_if_val = directive.get("wake_if", [])
            if not isinstance(wake_if_val, list) or not all(
                isinstance(t, str) for t in wake_if_val
            ):
                raise TypeError("directive.wake_if must be a list of str")
```

In `evaluate`, replace the single line `subscribed = tuple((state.directive or {}).get("wake_if", ()))` (keep the existing review-2 f10 comment block above it):

```python
    wake_if_raw = (state.directive or {}).get("wake_if", ())
    if not isinstance(wake_if_raw, (list, tuple)):
        # A non-list wake_if would tuple() into per-character tokens and
        # silently drop the model's subscription -- a masked skip, the
        # unsafe direction. Raise instead: the coordinator's STATE_CORRUPT
        # guard resets + wakes and note_wake's save self-heals the file
        # (review-3 f1 backstop; load_attention_state validates first).
        raise TypeError(
            f"directive.wake_if must be a list, got {type(wake_if_raw).__name__}"
        )
    subscribed = tuple(wake_if_raw)
```

- [ ] **Step 4: Run the full suite**

Run: `uv run --extra test pytest tests/ -q`
Expected: all green (974 + 6 new).

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/attention.py tests/arena/test_attention.py
git commit -m "fix(arena): validate directive value types at load; raise on non-list wake_if (review-3 f1)"
```

---

### Task 2: Carry Lua error detail through ATTN_ERR → failure_details → SCAN_PARTIAL wake_detail (review-3 f3)

CITYHP/LOYALTY strictness stays (adjudication 1). What changes: `fam()` prints the engine error text (`ATTN_ERR|<FAM>|<err>`), the parser splits it into a new `AttentionScan.failure_details` field (backward-compatible with the bare `ATTN_ERR|<FAM>` form), and `evaluate`'s SCAN_PARTIAL decision carries it in `wake_detail` — so a live run's transcript distinguishes "API absent on this build" from a one-off miss.

**Files:**
- Modify: `src/civ_mcp/arena/attention.py` (`fam` in `_ATTENTION_LUA` ~line 452-455; `parse_attention_scan` ~line 341-405; `AttentionScan` ~line 311-326; `evaluate` ~line 761-762)
- Test: `tests/arena/test_attention.py`

**Interfaces:**
- Consumes: existing test constants `QUIET_LINES` (index 4 is the WC line — mirror `test_parse_failed_family_flagged`'s construction), `_st`, `SNAP`, `build_attention_query`.
- Produces: `AttentionScan.failure_details: tuple[str, ...] = ()` (new field, default empty — no other constructor sites break). SCAN_PARTIAL `Decision.wake_detail` format: `"FAM1,FAM2 -- FAM1: <err1>; FAM2: <err2>"`, capped at 300 chars. Tasks 3 and 4 rely on this plumbing.

- [ ] **Step 1: Write the failing tests**

```python
def test_parse_attn_err_detail_captured():
    """Review-3 f3: ATTN_ERR may carry the Lua error text as a third
    segment; the parser must surface it for diagnosability."""
    scan = parse_attention_scan(
        [*QUIET_LINES[:4], "ATTN_ERR|WC|attempt to index a nil value", *QUIET_LINES[5:]]
    )
    assert "WC" in scan.failed_families
    assert scan.failure_details == ("WC: attempt to index a nil value",)


def test_parse_attn_err_bare_form_still_works():
    scan = parse_attention_scan([*QUIET_LINES[:4], "ATTN_ERR|WC", *QUIET_LINES[5:]])
    assert "WC" in scan.failed_families
    assert scan.failure_details == ()


def test_scan_partial_wake_detail_carries_error_text():
    partial = parse_attention_scan(
        [*QUIET_LINES[1:], "ATTN_ERR|THREAT|attempt to index a nil value"]
    )
    d = evaluate("auto", _st(), partial, SNAP, max_streak=5, task_event=False)
    assert d.wake_cause == "SCAN_PARTIAL"
    assert "THREAT" in d.wake_detail and "nil value" in d.wake_detail


def test_fam_wrapper_emits_error_detail():
    """Textual pin: fam() must capture pcall's error value and print it as
    the ATTN_ERR line's third segment (sanitized, capped)."""
    q = build_attention_query(1, 4)
    assert "local ok, err = pcall(fn)" in q
    assert "tostring(err)" in q
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra test pytest tests/arena/test_attention.py -q -k "attn_err or wake_detail_carries or fam_wrapper_emits"`
Expected: all four FAIL (`failure_details` attribute missing; `ok, err` not in the template).

- [ ] **Step 3: Implement**

Lua `fam` wrapper (replace the current 4-line version):

```lua
local function fam(name, fn)
    local ok, err = pcall(fn)
    if not ok then
        -- Carry the engine error text (pipe-sanitized, capped) so a
        -- SCAN_PARTIAL wake is diagnosable post-run: "API absent on this
        -- build" vs a one-off miss (review-3 f3).
        print("ATTN_ERR|" .. name .. "|" .. tostring(err):gsub("|", "/"):sub(1, 120))
    end
end
```

`parse_attention_scan`: initialize `failure_details: list[str] = []` beside `failed`, and replace the two-line `ATTN_ERR|` branch:

```python
        if line.startswith("ATTN_ERR|"):
            parts = line.split("|", 2)
            fam_name = parts[1].strip() if len(parts) > 1 else ""
            detail = parts[2].strip() if len(parts) > 2 else ""
            failed.append(fam_name)
            if detail:
                failure_details.append(f"{fam_name}: {detail}")
            continue
```

`AttentionScan`: add field `failure_details: tuple[str, ...] = ()`. Constructor call at the end of `parse_attention_scan` gains `failure_details=tuple(failure_details)`.

`evaluate`, replace the SCAN_PARTIAL branch:

```python
    if scan.failed_families:
        detail = ",".join(scan.failed_families)
        if scan.failure_details:
            detail = (detail + " -- " + "; ".join(scan.failure_details))[:300]
        return Decision("wake", "SCAN_PARTIAL", detail)
```

- [ ] **Step 4: Run the full suite**

Run: `uv run --extra test pytest tests/ -q`
Expected: all green. The pre-existing `test_scan_partial_wakes` (bare `ATTN_ERR|THREAT`) must still pass via the backward-compatible parse.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/attention.py tests/arena/test_attention.py
git commit -m "feat(arena): carry Lua error detail through ATTN_ERR to SCAN_PARTIAL wake_detail (review-3 f3)"
```

---

### Task 3: NOTIFY counts per-entry failures and raises after both passes (review-3 f2)

NOTIFY's per-entry pcall currently swallows accessor failures, so a raising `Find`/`GetTypeName`/`GetMessage` on a wake-list entry (SPY_CAUGHT — the only wake type with no redundant trigger family) is silently dropped and the seat blind-skips the event. Keep per-entry isolation (one malformed entry must not hide the rest), but count failures and `error()` after both passes so `fam()`'s boundary emits `ATTN_ERR|NOTIFY` → SCAN_PARTIAL wake. Lines already printed survive, so captured notifications still reach the digest.

**Files:**
- Modify: `src/civ_mcp/arena/attention.py` (`fam("NOTIFY", ...)` segment, ~line 651-681)
- Test: `tests/arena/test_attention.py`

**Interfaces:**
- Consumes: Task 2's `failure_details` plumbing (the `error()` message flows through it automatically); `_lua_family_segment` helper exists but takes a `next_name` — NOTIFY is the last family, so slice to end of query instead.
- Produces: no Python signature changes. Scan output may now contain both `ATTN|NOTIFY|...` lines and one `ATTN_ERR|NOTIFY|<n> notification entries unreadable` line.

- [ ] **Step 1: Write the failing tests**

```python
def test_notify_lua_counts_and_raises_entry_failures():
    """Review-3 f2: NOTIFY keeps per-entry isolation but a failed entry must
    end in ATTN_ERR|NOTIFY via fam()'s boundary, never a clean fam() pass
    that blind-skips SPY_CAUGHT (the one wake type with no redundant
    trigger family)."""
    q = build_attention_query(1, 4)
    seg = q[q.index('fam("NOTIFY"'):]
    assert "pcall" in seg                       # per-entry isolation retained
    assert "failures = failures + 1" in seg
    assert "if failures > 0 then" in seg
    assert "error(" in seg


def test_notify_partial_failure_wakes_but_keeps_notifications():
    lines = [
        *QUIET_LINES,
        "ATTN|NOTIFY|type=NOTIFICATION_PRODUCTION|msg=Choose production",
        "ATTN_ERR|NOTIFY|2 notification entries unreadable",
    ]
    scan = parse_attention_scan(lines)
    assert ("NOTIFICATION_PRODUCTION", "Choose production") in scan.notifications
    assert "NOTIFY" in scan.failed_families
    d = evaluate("auto", _st(), scan, SNAP, max_streak=5, task_event=False)
    assert d.wake_cause == "SCAN_PARTIAL"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra test pytest tests/arena/test_attention.py -q -k "notify_lua_counts or notify_partial_failure"`
Expected: the Lua pin FAILS (`failures` not in segment). The parse test may already pass after Task 2 — that is fine; it is the regression pin for this task's contract.

- [ ] **Step 3: Implement**

Replace the whole `fam("NOTIFY", ...)` block with:

```lua
fam("NOTIFY", function()
    local list = NotificationManager.GetList(me)
    if not list then return end
    local emitted = 0
    local failures = 0
    local function tryEmit(nid, wantWake)
        -- Per-entry protected call (notifications.py:53-102 idiom): one
        -- malformed notification skips itself, not the rest of the list.
        -- But failures are COUNTED and raised after both passes -- a
        -- raising accessor must never silently drop a wake-list type;
        -- SPY_CAUGHT has no redundant trigger family (review-3 f2).
        local ok = pcall(function()
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
        if not ok then failures = failures + 1 end
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
    if failures > 0 then
        error(failures .. " notification entries unreadable")
    end
end)
```

Hand-verify the template's block balance after the edit (count `function`/`then`/`do` openers vs `end`s across `_ATTENTION_LUA`; record the counts in your report, as done for cbb608a).

- [ ] **Step 4: Run the full suite**

Run: `uv run --extra test pytest tests/ -q`
Expected: all green, including the existing `test_attention_query_embeds_wake_list_priority` two-pass pins.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/attention.py tests/arena/test_attention.py
git commit -m "fix(arena): NOTIFY counts entry failures and raises to ATTN_ERR, never a silent drop (review-3 f2)"
```

---

### Task 4: Record wake_detail (incl. STATE_CORRUPT exception repr) in the transcript (review-3 f5 mitigation)

The STATE_CORRUPT guard stays broad (adjudication 2), but the caught exception currently reaches stderr only — a mislabeled logic regression is invisible in post-run metrics. Put `repr(e)` into the Decision's `wake_detail` and record `wake_detail` in `wake_attention_fields`, so every wake record in the transcript carries its diagnosis (this also lands Task 2's SCAN_PARTIAL detail in the transcript).

**Files:**
- Modify: `src/civ_mcp/arena/coordinator.py` (STATE_CORRUPT guard ~line 296-308; `wake_attention_fields` ~line 579-588)
- Test: `tests/arena/test_coordinator.py`

**Interfaces:**
- Consumes: `Decision(action, wake_cause, wake_detail, ...)` — third positional arg already exists.
- Produces: wake-turn transcript records' `attention` dict gains `"wake_detail": str` (empty string for detail-less wakes). Analyze changes are NOT in scope — the field is for post-run inspection.

- [ ] **Step 1: Write the failing test**

Locate the two existing STATE_CORRUPT tests in `tests/arena/test_coordinator.py` (docstrings mention "reset + wake (STATE_CORRUPT)", asserts on `rec["attention"]["wake_cause"] == "STATE_CORRUPT"` around lines 1944-2001). Extend BOTH with:

```python
    assert rec["attention"]["wake_detail"]  # exception repr recorded (review-3 f5)
    assert "Error" in rec["attention"]["wake_detail"]  # repr(e) carries the class name
```

(Adjust `rec` to each test's existing record variable. If the triggering exception in a fixture is e.g. `TypeError`, `"Error" in` covers it.)

Also pick one existing non-corrupt wake test that asserts on `rec["attention"]` (e.g. a NO_BASELINE or STREAK_CAP wake) and add:

```python
    assert rec["attention"]["wake_detail"] == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra test pytest tests/arena/test_coordinator.py -q -k "corrupt or STATE_CORRUPT"` (adjust `-k` to the actual test names)
Expected: FAIL with `KeyError: 'wake_detail'`.

- [ ] **Step 3: Implement**

In the guard:

```python
                        decision = Decision("wake", "STATE_CORRUPT", repr(e)[:200])
```

In `wake_attention_fields` (add after `"wake_cause": wake_cause,`):

```python
                        "wake_detail": (
                            decision.wake_detail if decision is not None else ""
                        ),
```

- [ ] **Step 4: Run the full suite**

Run: `uv run --extra test pytest tests/ -q`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/coordinator.py tests/arena/test_coordinator.py
git commit -m "feat(arena): record wake_detail in wake transcript records; STATE_CORRUPT carries exception repr (review-3 f5)"
```

---

### Task 5: Digest render failure degrades to a stub, not an empty block (review-3 f6)

A render failure currently sets `digest_block = ""`, silently erasing the entire "while you slept" recap. Degrade instead to a one-line stub that preserves the fact and length of the sleep plus the error, so the model knows it slept and knows the recap is missing. The broad catch stays (adjudication 3).

**Files:**
- Modify: `src/civ_mcp/arena/coordinator.py` (render_digest except ~line 398-402; import of `DIGEST_MAX_CHARS`)
- Test: `tests/arena/test_coordinator.py` (update `test_tampered_slept_record_costs_digest_not_run`, ~line 1880)

**Interfaces:**
- Consumes: `DIGEST_MAX_CHARS` from `civ_mcp.arena.attention` (add to the existing import block at the top of coordinator.py); `att_state.slept` (non-empty on this path — guarded by `att_state.slept` at the `if`).
- Produces: fallback `digest_block` format: `== WHILE YOU SLEPT (<n> turns; digest unavailable: <repr(e)>) ==`, capped at `DIGEST_MAX_CHARS`.

- [ ] **Step 1: Update the test (it will fail against current code)**

In `test_tampered_slept_record_costs_digest_not_run`, replace:

```python
    assert pol.last_digest == ""              # digest lost, run intact
```

with:

```python
    # Review-3 f6: the DETAIL is lost, but the FACT of the sleep survives --
    # an empty block silently erased the whole recap.
    assert "WHILE YOU SLEPT" in pol.last_digest
    assert "digest unavailable" in pol.last_digest
    assert "1 turns" in pol.last_digest       # len(slept) == 1 in this fixture
```

Also update the test's docstring: "must degrade to a stub digest naming the failure" instead of "must degrade to an empty digest".

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra test pytest tests/arena/test_coordinator.py -q -k tampered_slept_record`
Expected: FAIL (`pol.last_digest` is `""`).

- [ ] **Step 3: Implement**

Add `DIGEST_MAX_CHARS` to the coordinator's `from civ_mcp.arena.attention import (...)` block. Replace the except body:

```python
                    except Exception as e:
                        # A tampered slept record (e.g. missing "turn") or a
                        # render regression must cost the digest DETAIL, not
                        # the run -- and not the FACT of the sleep: an empty
                        # block silently erased the whole recap (review-3 f6).
                        digest_block = (
                            f"== WHILE YOU SLEPT ({len(att_state.slept)} turns; "
                            f"digest unavailable: {e!r}) =="
                        )[:DIGEST_MAX_CHARS]
                        print(f"[arena] wake digest render failed: {e!r}", file=sys.stderr)
```

- [ ] **Step 4: Run the full suite**

Run: `uv run --extra test pytest tests/ -q`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/coordinator.py tests/arena/test_coordinator.py
git commit -m "fix(arena): digest render failure degrades to a stub, not an empty block (review-3 f6)"
```

---

### Task 6: Gate the early-game rubric to played records (review-3 f4)

`_rubric_for_model` iterates all records ≤ turn 20. A slept record's `state_delta` spans the whole sleep, so an AI-founded city during sleep sets `founded_extra_city` — attributing an AI action to the model's competence rubric. This is a present-day false positive in any attention run.

**Files:**
- Modify: `src/civ_mcp/arena/analyze.py` (`_rubric_for_model`, the `early = [...]` list ~line 667)
- Test: `tests/arena/test_analyze.py`

**Interfaces:**
- Consumes: `_turn_kind` (same module), `_rubric_for_model` (import directly in the test — the existing rubric tests use a full-report fixture, but a direct unit test is the tight pin here).
- Produces: no signature changes.

- [ ] **Step 1: Write the failing test**

```python
def test_rubric_ignores_slept_records():
    """Review-3 f4: a slept turn's state_delta spans the whole sleep; an
    AI-founded city during sleep must not set founded_extra_city."""
    from civ_mcp.arena.analyze import _rubric_for_model

    slept = {"turn": 5, "slept": True, "state_delta": {"cities": 1}}
    assert _rubric_for_model([slept])["founded_extra_city"] is None

    played = {"turn": 6, "state_delta": {"cities": 1}}
    assert _rubric_for_model([played])["founded_extra_city"] is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra test pytest tests/arena/test_analyze.py -q -k rubric_ignores_slept`
Expected: FAIL (slept record sets the flag).

- [ ] **Step 3: Implement**

```python
    # Slept turns carry a cross-sleep state_delta and no model steps; an
    # AI-founded city during sleep must not read as model competence
    # (review-3 f4).
    early = [
        r for r in records
        if r.get("turn", 0) <= 20 and _turn_kind(r) == "played"
    ]
```

- [ ] **Step 4: Run the full suite**

Run: `uv run --extra test pytest tests/ -q`
Expected: all green (existing rubric fixture tests use played records and must not change).

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/analyze.py tests/arena/test_analyze.py
git commit -m "fix(arena): gate early-game rubric to played records (review-3 f4)"
```

---

### Task 7: Gate per-player standing-memory tallies to played records (review-3 f7)

The per-player loop in `analyze()` counts `mem_injected_turns`/`mem_captured_turns` over ALL records while the sibling `behavior_metrics` gates the identical tally on played (analyze.py:234). Currently inert (slept records hard-code zeroed `standing_memory`), but it is the recorded carry-forward divergence with no gate. Task classification and tool-call counts deliberately stay over all records — pre-model task follow-through runs on slept turns too (`behavior_metrics` precedent).

**Files:**
- Modify: `src/civ_mcp/arena/analyze.py` (per-player loop, ~line 884-888)
- Test: `tests/arena/test_analyze.py`

**Interfaces:**
- Consumes: `analyze(transcript_records, cost_records)` public entry; report shape `report["by_player"][<pid>]["behavior"]["standing_memory_injected_turns"]`.
- Produces: no signature changes.

- [ ] **Step 1: Write the failing test**

```python
def test_per_player_memory_tallies_exclude_slept_records():
    """Review-3 f7: per-player standing-memory tallies must gate on played,
    matching behavior_metrics -- a future slept record carrying non-zero
    standing_memory must not diverge the per-player table from the summary."""
    from civ_mcp.arena.analyze import analyze

    base = {"player_id": 1, "model": "m", "provider": "p", "driver": "in_process"}
    slept = {**base, "turn": 5, "slept": True,
             "standing_memory": {"injected": True, "captured_chars": 9}}
    played = {**base, "turn": 6,
              "standing_memory": {"injected": True, "captured_chars": 9}}
    report = analyze([slept, played], [])
    behavior = report["by_player"][1]["behavior"]
    assert behavior["standing_memory_injected_turns"] == 1
    assert behavior["standing_memory_captured_turns"] == 1
```

(If `analyze` needs more record fields to run, extend `base` minimally — mirror the smallest existing `analyze()` fixture in this file — but the two assertions are the contract.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra test pytest tests/arena/test_analyze.py -q -k per_player_memory_tallies`
Expected: FAIL (counts == 2).

- [ ] **Step 3: Implement**

Wrap only the two standing-memory tallies:

```python
            # Standing-memory tallies are per MODEL turn (behavior_metrics
            # precedent, review-2 f7 / review-3 f7). Task classification and
            # tool-call counts below stay over ALL records: pre-model task
            # follow-through runs on slept turns too.
            if _turn_kind(rec) == "played":
                if _standing_memory_injected(rec):
                    mem_injected_turns += 1
                if _standing_memory_captured(rec):
                    mem_captured_turns += 1
```

- [ ] **Step 4: Run the full suite**

Run: `uv run --extra test pytest tests/ -q`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/analyze.py tests/arena/test_analyze.py
git commit -m "fix(arena): gate per-player standing-memory tallies to played records (review-3 f7)"
```

---

### Task 8: Render the SKIP range from the run's actual max_skip (review-3 f8)

`ATTENTION_INSTRUCTION` hardcodes `SKIP: <1-5>` while `AttentionOptions.max_skip` is configurable; a non-default run misinforms the model and the parser clamps its intent silently. Parameterize the instruction, thread `max_skip` through `build_opening_prompt`, and pass it from both policies. This is the only hardcoded range site in `src/` (verified by grep).

**Files:**
- Modify: `src/civ_mcp/arena/prompting.py`
- Modify: `src/civ_mcp/arena/agent.py` (~line 138-147)
- Modify: `src/civ_mcp/arena/cli_agent.py` (~line 507-516)
- Test: `tests/arena/test_prompting.py` (extend the drift pin ~line 99-107)

**Interfaces:**
- Consumes: `self.options.attention.max_skip` (available at both call sites — `options` is `CivOptions`).
- Produces: `attention_instruction(max_skip: int) -> str` (new public function); `build_opening_prompt(..., attention_max_skip: int = 5)` (new keyword, default preserves every existing call site); module constant `ATTENTION_INSTRUCTION = attention_instruction(5)` kept for back compat (tests import it).

- [ ] **Step 1: Write the failing tests**

In `tests/arena/test_prompting.py`, extend the drift-pin block:

```python
def test_attention_instruction_renders_configured_max_skip():
    """Review-3 f8: the prompt's stated SKIP range must match the run's
    actual clamp, not the default."""
    from civ_mcp.arena.prompting import attention_instruction

    assert "<1-3>" in attention_instruction(3)
    assert "<1-10>" in attention_instruction(10)
    out = build_opening_prompt(
        player_id=1, turn=5,
        include_attention_instruction=True, attention_max_skip=3,
    )
    assert "<1-3>" in out and "<1-5>" not in out


def test_attention_instruction_constant_matches_default_max_skip():
    from civ_mcp.arena.prompting import attention_instruction

    default = AttentionOptions().max_skip
    assert ATTENTION_INSTRUCTION == attention_instruction(default)
```

(Keep the existing `test_attention_instruction_skip_range_matches_default_max_skip` pin as-is — it still guards default drift.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra test pytest tests/arena/test_prompting.py -q -k "renders_configured or constant_matches"`
Expected: FAIL with ImportError (`attention_instruction` not defined).

- [ ] **Step 3: Implement**

In `prompting.py`, replace the `ATTENTION_INSTRUCTION` constant (keep the drift-rationale comment above it):

```python
_ATTENTION_INSTRUCTION_TEMPLATE = """If nothing will need your judgment for a few turns, you may ALSO end with:
SKIP: <1-{max_skip}>
WAKE IF: <optional, comma-separated from exactly: GREAT_PERSON_AVAILABLE, CITY_GREW, TRADE_ROUTE_IDLE, GOLD_STOCKPILE_HIGH>
You will be woken early regardless for any threat, blocker, or task event.
Skip during long builds or peacetime consolidation; never skip at war or with unsettled settlers."""


def attention_instruction(max_skip: int) -> str:
    """Render the SKIP/WAKE IF instruction for the run's actual clamp
    (review-3 f8: a non-default max_skip must not misinform the model)."""
    return _ATTENTION_INSTRUCTION_TEMPLATE.format(max_skip=max_skip)


# Default-clamp render: kept as a constant for existing imports and the
# default-drift pin (AttentionOptions.max_skip default == 5).
ATTENTION_INSTRUCTION = attention_instruction(5)
```

`build_opening_prompt`: add keyword `attention_max_skip: int = 5` and change the append to `parts.append(attention_instruction(attention_max_skip))`. Update the docstring's block list accordingly.

`agent.py` and `cli_agent.py`: add to each `build_opening_prompt(...)` call:

```python
            attention_max_skip=self.options.attention.max_skip,
```

- [ ] **Step 4: Run the full suite**

Run: `uv run --extra test pytest tests/ -q`
Expected: all green — the existing `endswith(ATTENTION_INSTRUCTION)` and token-sync tests must pass unchanged (default render is byte-identical to the old constant).

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/prompting.py src/civ_mcp/arena/agent.py src/civ_mcp/arena/cli_agent.py tests/arena/test_prompting.py
git commit -m "fix(arena): render SKIP range from the run's actual max_skip (review-3 f8)"
```

---

### Task 9: Accept a small filler-word whitelist before the SKIP integer (review-3 f9)

The review-2 narrowing (leading integer only) correctly rejects `SKIP: hold until turn 340` but also drops `SKIP: for 3 turns` / `SKIP: skip 3 turns` — previously-honored, plausibly-intended phrasings. Allow exactly one optional filler word from `{for, skip, sleep}` (case-insensitive) before the integer. Digit-bearing prose must still not parse (the existing negative pins stay green: "hold until turn 340", "maybe in 3 if peaceful", "after 2 more builds").

**Files:**
- Modify: `src/civ_mcp/arena/attention.py` (`parse_directive` inner regex ~line 81; docstring ~line 66-68)
- Test: `tests/arena/test_attention.py` (extend `test_leading_integer_still_parses` parametrize)

**Interfaces:**
- Consumes/Produces: no signature changes; grammar widens only by the three whitelisted filler words.

- [ ] **Step 1: Write the failing tests**

Extend the `test_leading_integer_still_parses` parametrize list with:

```python
    ("for 3 turns", 3),
    ("skip 3 turns", 3),
    ("sleep 2", 2),
    ("**for 3**", 3),
```

Do NOT touch `test_digit_bearing_prose_is_not_a_directive` — its three cases must stay green.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra test pytest tests/arena/test_attention.py -q -k "leading_integer or digit_bearing"`
Expected: the four new cases FAIL (no directive parsed); the prose cases PASS.

- [ ] **Step 3: Implement**

Replace the `num = re.match(...)` line (and extend the comment above it):

```python
            # The integer must LEAD the body (markdown decoration tolerated),
            # optionally after ONE filler word from a tiny whitelist --
            # "SKIP: for 3 turns" / "SKIP: skip 3" are honored (review-3 f9)
            # while digit-bearing prose like "SKIP: hold until turn 340" still
            # must NOT become a max-clamped blind skip (review-2 f6) -- no
            # directive -> wake.
            num = re.match(
                r"[\s*_`~'\"(\[]*(?:(?:for|skip|sleep)\s+)?(-?\d+)",
                m.group("body"),
                re.IGNORECASE,
            )
```

Update the docstring sentence at ~line 66-68 to mention the filler-word whitelist.

- [ ] **Step 4: Run the full suite**

Run: `uv run --extra test pytest tests/ -q`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/attention.py tests/arena/test_attention.py
git commit -m "fix(arena): accept for/skip/sleep filler before the SKIP integer (review-3 f9)"
```

---

### Task 10: Align the design spec with review-3 changes; commit this plan

The design spec documents the scan line protocol, NOTIFY semantics, the directive grammar, and the prompt instruction — Tasks 1-3, 8, and 9 change all of them. Update the spec so it stays the source of truth, and commit this plan document on-branch (review-2 precedent: commit `8b76fb5`).

**Files:**
- Modify: `docs/superpowers/specs/2026-07-09-arena-attention-turn-skipping-design.md`
- Add: `docs/superpowers/plans/2026-07-09-arena-attention-review3-fixes.md` (this file, verbatim)

**Interfaces:**
- Consumes: the merged behavior of Tasks 1-9 (run this task LAST).
- Produces: spec text only — no code.

- [ ] **Step 1: Make the spec edits**

Locate and update each of these spec statements (search by the quoted fragments; keep each edit minimal and in the spec's existing voice):

1. **Scan line protocol** (section describing `ATTN_ERR|<FAMILY>`): the error line now optionally carries a third segment — `ATTN_ERR|<FAMILY>|<error text, pipe-sanitized, capped 120 chars>` — which the parser surfaces as `AttentionScan.failure_details` and `evaluate` folds into the SCAN_PARTIAL `wake_detail` (review-3 f3: live diagnosability of strict-family failures).
2. **NOTIFY family**: per-entry protected calls remain (one malformed notification skips itself), but entry failures are counted and raised after both passes, so any entry failure degrades the family to `ATTN_ERR|NOTIFY` → SCAN_PARTIAL wake — a raising accessor can never silently drop a wake-list type (review-3 f2).
3. **State persistence / corruption contract**: `load_attention_state` now validates directive VALUE types (`skip` int, `wake_if` list of str), not just dict shape — the one non-raising corruption class (string `wake_if`) that produced a masked skip is rejected at load; `evaluate` raises on a non-list `wake_if` as backstop (review-3 f1). Wake records in the transcript carry `wake_detail` (STATE_CORRUPT records carry the exception repr; review-3 f5).
4. **Directive grammar** (SKIP parse): the integer must lead the body, optionally after one filler word from exactly `{for, skip, sleep}` (case-insensitive); digit-bearing prose still does not parse (review-3 f9 refinement of review-2 f6).
5. **Prompt instruction**: the SKIP range in `ATTENTION_INSTRUCTION` renders from the run's configured `max_skip`, no longer hardcoded `1-5` (review-3 f8).
6. **Wake digest**: on a digest render failure the model receives a one-line stub naming the sleep length and the error, never an empty block (review-3 f6).

- [ ] **Step 2: Verify no code changed**

Run: `git diff --stat -- src/ tests/`
Expected: empty.

- [ ] **Step 3: Run the full suite (unchanged, sanity)**

Run: `uv run --extra test pytest tests/ -q`
Expected: all green.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-07-09-arena-attention-turn-skipping-design.md docs/superpowers/plans/2026-07-09-arena-attention-review3-fixes.md
git commit -m "docs(arena): align attention spec with review-3 fixes; add review-3 fix plan"
```

---

## Final Whole-Branch Review

After all 10 tasks: dispatch the final code reviewer (most capable available model) over `abdfb7e..HEAD` with this plan, the adjudications section above, and the accumulated deferred-minors list from the SDD ledger. The reviewer should specifically re-check: (a) the fail-open invariant across every composed path touched here (Task 1's raise lands in the Task 4-instrumented guard; Task 3's error() lands in Task 2's detail plumbing); (b) `_ATTENTION_LUA` block balance after Tasks 2-3; (c) that no task weakened the review-2 strictness decisions.

**End state:** commits on `arena-attention-turn-skipping`, UNMERGED/UNPUSHED, full suite green, summary for riz. Gates unchanged: riz review/merge decision, then live probes P1-P4 (P1 hard gate; check SCAN_PARTIAL wake_cause dominance — now diagnosable per-record via Task 2/4's `wake_detail`).
