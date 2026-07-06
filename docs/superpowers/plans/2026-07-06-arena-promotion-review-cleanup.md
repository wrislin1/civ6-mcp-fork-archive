# Arena Promotion Review Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Address the valid non-blocking review findings for the arena promotion lever slice without changing promotion behavior.

**Architecture:** Keep promotion detection authoritative through `get_unit_promotions(unit_id).promotions`. Refactor briefing unit-fetch normalization into a shared helper while preserving the visible `_units` string-result behavior, and move section header overrides into metadata next to section order/builders.

**Tech Stack:** Python 3, asyncio, pytest, civ6-mcp arena briefing/autoresolve modules.

---

### Task 1: Characterize Unit String Rendering

**Files:**
- Modify: `tests/arena/test_briefing.py`
- Test: `tests/arena/test_briefing.py`

- [x] **Step 1: Add a test that preserves `_units` string rendering**

Add this test after `test_promotions_and_units_reuse_cached_units`:

```python
@pytest.mark.asyncio
async def test_units_preserves_string_result_while_caching_empty_units():
    class StringUnitsGS:
        async def get_units(self):
            return "ERROR: units unavailable"

    ctx = {}

    text = await _briefing._units(StringUnitsGS(), ctx)

    assert text == "ERROR: units unavailable"
    assert ctx["units"] == []
```

- [x] **Step 2: Run the focused briefing tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_briefing.py -q
```

Expected: all tests pass. This is a characterization test for an existing behavior before the refactor.

- [x] **Step 3: Commit the test with the implementation in Task 2**

Do not commit after this task alone; the test characterizes behavior for the refactor and should land with the cleanup.

### Task 2: Refactor Briefing Unit Fetching and Headers

**Files:**
- Modify: `src/civ_mcp/arena/briefing.py`
- Test: `tests/arena/test_briefing.py`

- [x] **Step 1: Replace duplicated unit-fetch normalization with shared helpers**

Change `_promotions`, `_units`, and `_fetch_units` in `src/civ_mcp/arena/briefing.py` to this shape:

```python
async def _promotions(gs: Any, ctx: dict[str, Any]) -> str:
    units = await _fetch_units(gs, ctx)
    if not units:
        return ""

    results = await asyncio.gather(
        *(gs.get_unit_promotions(u.unit_id) for u in units),
        return_exceptions=True,
    )
    ...


async def _units(gs: Any, ctx: dict[str, Any]) -> str:
    result = await _fetch_units_result(gs, ctx)
    return _render(result, nr.narrate_units)


async def _fetch_units_result(gs: Any, ctx: dict[str, Any]) -> list[Any] | str:
    units = ctx.get("units")
    if units is not None:
        return units
    result = await gs.get_units()
    ctx["units"] = [] if isinstance(result, str) else result
    return result


async def _fetch_units(gs: Any, ctx: dict[str, Any]) -> list[Any]:
    result = await _fetch_units_result(gs, ctx)
    return [] if isinstance(result, str) else result
```

- [x] **Step 2: Move the custom promotions header into section metadata**

Add this near `_BUILDERS`:

```python
_HEADERS = {
    "promotions": "== ACTION: PROMOTIONS AVAILABLE ==\n",
}
```

Then change `_block_header` to:

```python
def _block_header(name: str) -> str:
    """Section header used for both the budget accounting and the rendered block."""
    return _HEADERS.get(name, f"== {name.upper()} ==\n")
```

- [x] **Step 3: Record the deferred bulk-query efficiency follow-up in code**

Place this concise note above the promotion fan-out in `_promotions`:

```python
    # A future bulk promotable-unit query could replace this per-unit fan-out
    # and the matching sweep_promotions probes with one GameCore round-trip.
```

This keeps the current slice scoped while preserving the reviewer's larger efficiency concern.

- [x] **Step 4: Run the focused briefing tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_briefing.py -q
```

Expected: all tests pass.

### Task 3: Verify and Commit

**Files:**
- Modify: `docs/superpowers/plans/2026-07-06-arena-promotion-review-cleanup.md`
- Modify: `src/civ_mcp/arena/briefing.py`
- Modify: `tests/arena/test_briefing.py`

- [x] **Step 1: Run full verification**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests -q
```

Expected: all tests pass.

- [x] **Step 2: Check whitespace and branch status**

Run:

```bash
git diff --check
git status --short --branch
```

Expected: no `git diff --check` output; only the intended modified/new files plus `.serena/`.

- [x] **Step 3: Commit the cleanup**

Run:

```bash
git add docs/superpowers/plans/2026-07-06-arena-promotion-review-cleanup.md src/civ_mcp/arena/briefing.py tests/arena/test_briefing.py
git commit -m "refactor(arena): clean promotion briefing review findings"
```

Expected: one new commit on `arena-promotion-lever-slice1`.

## Self-Review

- Spec coverage: covers review item 2 by reusing shared unit-fetch helpers, review item 3 by moving the custom header into metadata, and review item 1 by recording the larger bulk-query efficiency follow-up without widening this slice.
- Placeholder scan: no task contains open-ended implementation instructions; every code-changing step gives exact code shape.
- Type consistency: `_fetch_units_result()` returns `list[Any] | str`; `_fetch_units()` keeps the existing list-only interface used by `_map` and `_promotions`.
