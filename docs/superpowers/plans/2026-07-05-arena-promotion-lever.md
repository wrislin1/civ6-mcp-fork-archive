# Arena Puppet Promotion Lever (Slice 1) Implementation Plan

## Status — 2026-07-06

- **Slice 1 — implemented + hardened.** Commits: `cc7937c`, `0e6e65f`, `786d0d7`, `72ede30`, `c4d6b91`, `cd6e43c`, `80203b9`, `68c9462`, `dc7f7e3`.
- ✅ Host verification — `/home/riz/.local/bin/uv run --extra test pytest tests -q` passed with `429 passed in 13.97s` on `arena-promotion-lever-slice1` at `dc7f7e3`.
- ✅ Review hardening — final cleanup commit `dc7f7e3` reuses the shared briefing unit fetch helper, preserves `_units` string-result rendering, records the deferred bulk-query efficiency follow-up, and keeps promotion headers in section metadata.
- ⚠️ Live recon-game validation — not run; this plan explicitly made live validation best-effort and non-blocking to avoid interrupting the watcher mid-AI-phase.
- **Slice 2 — REMAINING.** Capability tools + diplomacy/trade/peace/alliance doctrine remain follow-on work.
- **Slice 3 — REMAINING.** Cross-turn memory / standing plan remains follow-on work.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make arena LLM puppets reliably spend unit promotions — via loud briefing surfacing for treatment civs plus a deterministic end-of-turn sweep for all civs — and add promotion/upgrade/expansion/war doctrine to the playbook.

**Architecture:** A new pure `autoresolve` module holds the promotion-picking policy (`pick_promotion`) and the best-effort end-of-turn `sweep_promotions(gs)`. The briefing gains a `_promotions` section (rendered first, treatment-only) that reuses the same picker so its suggestion matches the sweep. The coordinator runs the sweep after the model turn — on a reconnected tuner, before telemetry is captured — so the sweep result lands in both the log and the transcript. Playbook and experiment-YAML text changes round out the slice.

**Tech Stack:** Python 3.12, `asyncio`, `pytest` + `pytest-asyncio`, `uv` for running. Dataclasses from `civ_mcp.lua` (`UnitPromotionStatus`, `PromotionOption`, `UnitInfo`).

## Global Constraints

- Test runner is `uv`: `/home/riz/.local/bin/uv run pytest <targets> -q`. Plain `pytest` also collects `scripts/test_game_state.py` / `scripts/test_queries.py`, which fail outside this slice — always pass explicit `tests/...` targets.
- **Promotion detection is `get_unit_promotions(unit_id).promotions` non-empty.** `get_units().needs_promotion` is hardcoded `"0"` (`src/civ_mcp/lua/units.py:105`) and must never be used as a filter — it drops every unit.
- `pick_promotion` returns a whole `PromotionOption` (so callers can read both `.name` and `.promotion_type`), or `None`.
- The sweep is **best-effort infrastructure for all puppets**: it must never raise into the coordinator, and a sweep failure must never block the human hand-back.
- A new briefing section requires four edits in lockstep: `_ORDER` + `_BUILDERS` (`briefing.py`), `VALID_SECTIONS` (`config.py:8`), and each treatment civ's `sections:` in the experiment YAML. The `BriefingOptions.sections` default (`config.py:31`) stays unchanged (opt-in per civ).
- Do **not** stop the live arena watcher mid-AI-phase (hangs the game; needs a save reload). Live validation steps are best-effort and never gate a task.
- Do not commit/push/merge without explicit direction from riz. Task commit steps stage + commit on the current branch only.

---

### Task 1: `autoresolve` module — promotion picker

**Files:**
- Create: `src/civ_mcp/arena/autoresolve.py`
- Test: `tests/arena/test_autoresolve.py`

**Interfaces:**
- Consumes: `civ_mcp.lua.PromotionOption`, `civ_mcp.lua.UnitPromotionStatus` (fields: `promotions: list[PromotionOption]`; each option has `.promotion_type`, `.name`, `.description`).
- Produces: `PREFERRED_PROMOTIONS: tuple[str, ...]`; `pick_promotion(status: UnitPromotionStatus) -> PromotionOption | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/arena/test_autoresolve.py
from civ_mcp import lua as lq
from civ_mcp.arena import autoresolve


def _status(*types):
    return lq.UnitPromotionStatus(
        unit_id=65537,
        unit_index=1,
        unit_type="UNIT_WARRIOR",
        promotions=[
            lq.PromotionOption(promotion_type=t, name=t.replace("PROMOTION_", "").title(), description="d")
            for t in types
        ],
    )


def test_pick_prefers_earliest_preferred_type():
    # BATTLECRY is earlier in PREFERRED_PROMOTIONS than GARRISON; offered order is reversed.
    pick = autoresolve.pick_promotion(_status("PROMOTION_GARRISON", "PROMOTION_BATTLECRY"))
    assert pick.promotion_type == "PROMOTION_BATTLECRY"


def test_pick_falls_back_to_first_available_when_none_preferred():
    pick = autoresolve.pick_promotion(_status("PROMOTION_UNKNOWN_A", "PROMOTION_UNKNOWN_B"))
    assert pick.promotion_type == "PROMOTION_UNKNOWN_A"


def test_pick_returns_none_on_empty():
    assert autoresolve.pick_promotion(_status()) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/home/riz/.local/bin/uv run pytest tests/arena/test_autoresolve.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'civ_mcp.arena.autoresolve'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/civ_mcp/arena/autoresolve.py
"""Deterministic end-of-turn auto-resolution for arena puppets.

Slice 1 covers unit promotions: the model turn runs first (and may promote as
part of a combat plan), then `sweep_promotions` is the safety net that spends any
promotion still pending. `get_units().needs_promotion` is dead (lua/units.py:105);
the authoritative signal is a non-empty `get_unit_promotions(unit_id).promotions`.
"""

from __future__ import annotations

from typing import Any

from civ_mcp.lua import PromotionOption, UnitPromotionStatus

# Ordered global preference of promotion *types*. The type identifies its own tree
# (VOLLEY = ranged, BATTLECRY = melee, SENTRY/SPYGLASS/RANGER/ALPINE = recon), so no
# unit-class field is needed. This is a preference, not a whitelist — anything not
# listed is still taken via the first-available fallback. Identifiers are best-guess
# Civ VI strings; a wrong one simply misses the preference and falls through.
PREFERRED_PROMOTIONS: tuple[str, ...] = (
    "PROMOTION_VOLLEY",
    "PROMOTION_BATTLECRY",
    "PROMOTION_SENTRY",
    "PROMOTION_SPYGLASS",
    "PROMOTION_RANGER",
    "PROMOTION_ALPINE",
    "PROMOTION_TORTOISE",
    "PROMOTION_GARRISON",
)


def pick_promotion(status: UnitPromotionStatus) -> PromotionOption | None:
    """Choose a promotion for a unit that has one pending.

    Returns the offered `PromotionOption` whose type is highest in
    `PREFERRED_PROMOTIONS`; else the first offered option (any promotion unfreezes
    XP and heals — a suboptimal pick still beats none); else `None` if none offered.
    """
    promos = list(getattr(status, "promotions", None) or [])
    if not promos:
        return None
    by_type = {p.promotion_type: p for p in promos}
    for pref in PREFERRED_PROMOTIONS:
        if pref in by_type:
            return by_type[pref]
    return promos[0]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/home/riz/.local/bin/uv run pytest tests/arena/test_autoresolve.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: (Best-effort, non-blocking) validate identifier strings live**

If the recon game is up and the watcher is idle (human seat, not mid-AI-phase), dump a real unit's promotions to sanity-check the `PROMOTION_*` strings:
Run: `/home/riz/.local/bin/uv run python -c "import asyncio; from civ_mcp.game_state import GameState; ..."` — or simply note that the first-available fallback guarantees a promotion regardless. **Do not** block this task on live access; the unit tests are the gate.

- [ ] **Step 6: Commit**

```bash
git add src/civ_mcp/arena/autoresolve.py tests/arena/test_autoresolve.py
git commit -m "feat(arena): promotion picker with preference + first-available fallback"
```

---

### Task 2: `sweep_promotions` — end-of-turn safety net

**Files:**
- Modify: `src/civ_mcp/arena/autoresolve.py`
- Test: `tests/arena/test_autoresolve.py`

**Interfaces:**
- Consumes: `pick_promotion` (Task 1); a `gs` object exposing `async get_units() -> list[UnitInfo] | str`, `async get_unit_promotions(unit_id) -> UnitPromotionStatus`, `async promote_unit(unit_id, promotion_type) -> str`.
- Produces: `async def sweep_promotions(gs: Any) -> list[dict]` returning one dict per promotion *attempt*: `{"unit_id", "unit_type", "promotion_type", "ok": bool}` (plus `"error"` on exception).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/arena/test_autoresolve.py
import pytest


class _Unit:
    def __init__(self, unit_id, unit_type="UNIT_WARRIOR", needs_promotion=False):
        self.unit_id = unit_id
        self.unit_type = unit_type
        self.needs_promotion = needs_promotion  # always false in reality; must be ignored


class _GS:
    def __init__(self, units, promo_by_id, promote_impl=None):
        self._units = units
        self._promo = promo_by_id
        self._promote_impl = promote_impl
        self.promoted = []

    async def get_units(self):
        return self._units

    async def get_unit_promotions(self, unit_id):
        v = self._promo[unit_id]
        if isinstance(v, Exception):
            raise v
        return v

    async def promote_unit(self, unit_id, promotion_type):
        self.promoted.append((unit_id, promotion_type))
        if self._promote_impl is not None:
            return self._promote_impl(unit_id, promotion_type)
        return f"Promoted {unit_id} to {promotion_type}"


@pytest.mark.asyncio
async def test_sweep_promotes_units_with_pending_promotions():
    gs = _GS(
        units=[_Unit(1, needs_promotion=False), _Unit(2)],
        promo_by_id={1: _status("PROMOTION_BATTLECRY"), 2: _status()},  # unit 2 has none
    )
    swept = await autoresolve.sweep_promotions(gs)
    assert gs.promoted == [(1, "PROMOTION_BATTLECRY")]  # unit 2 skipped despite no filter on needs_promotion
    assert swept == [{"unit_id": 1, "unit_type": "UNIT_WARRIOR", "promotion_type": "PROMOTION_BATTLECRY", "ok": True}]


@pytest.mark.asyncio
async def test_sweep_swallows_get_promotions_error():
    gs = _GS(units=[_Unit(1)], promo_by_id={1: RuntimeError("no experience")})
    swept = await autoresolve.sweep_promotions(gs)
    assert swept == []
    assert gs.promoted == []


@pytest.mark.asyncio
async def test_sweep_records_ok_false_on_promote_error_string():
    gs = _GS(
        units=[_Unit(1)],
        promo_by_id={1: _status("PROMOTION_BATTLECRY")},
        promote_impl=lambda uid, pt: "Error: CANNOT_PROMOTE",
    )
    swept = await autoresolve.sweep_promotions(gs)
    assert swept[0]["ok"] is False


@pytest.mark.asyncio
async def test_sweep_swallows_promote_exception():
    def _raise(uid, pt):
        raise RuntimeError("boom")

    gs = _GS(units=[_Unit(1)], promo_by_id={1: _status("PROMOTION_BATTLECRY")}, promote_impl=_raise)
    swept = await autoresolve.sweep_promotions(gs)  # must not raise
    assert swept[0]["ok"] is False and "error" in swept[0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/home/riz/.local/bin/uv run pytest tests/arena/test_autoresolve.py -q`
Expected: FAIL with `AttributeError: module 'civ_mcp.arena.autoresolve' has no attribute 'sweep_promotions'`

- [ ] **Step 3: Write minimal implementation**

Append to `src/civ_mcp/arena/autoresolve.py`:

```python
async def sweep_promotions(gs: Any) -> list[dict]:
    """Spend any pending promotion on every puppet unit. Best-effort: never raises.

    Enumerates units, then for EACH unit checks `get_unit_promotions` (do not
    pre-filter on `needs_promotion` — it is always false). A non-empty
    `status.promotions` means a promotion is pending; pick one and apply it.
    """
    swept: list[dict] = []
    try:
        units = await gs.get_units()
    except Exception:
        return swept
    if isinstance(units, str):
        return swept

    for u in units:
        try:
            status = await gs.get_unit_promotions(u.unit_id)
        except Exception:
            continue  # e.g. civilians have no experience object
        if not getattr(status, "promotions", None):
            continue
        pick = pick_promotion(status)
        if pick is None:
            continue
        entry: dict = {
            "unit_id": u.unit_id,
            "unit_type": getattr(u, "unit_type", ""),
            "promotion_type": pick.promotion_type,
            "ok": False,
        }
        try:
            result = await gs.promote_unit(u.unit_id, pick.promotion_type)
            entry["ok"] = not (isinstance(result, str) and result.startswith("Error"))
        except Exception as e:  # never let a single unit break the sweep
            entry["error"] = repr(e)
        swept.append(entry)
    return swept
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/home/riz/.local/bin/uv run pytest tests/arena/test_autoresolve.py -q`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/autoresolve.py tests/arena/test_autoresolve.py
git commit -m "feat(arena): best-effort end-of-turn promotion sweep"
```

---

### Task 3: `promotions` briefing section + `VALID_SECTIONS`

**Files:**
- Modify: `src/civ_mcp/arena/config.py:8-19` (add `"promotions"` to `VALID_SECTIONS`)
- Modify: `src/civ_mcp/arena/briefing.py` (new `_promotions` builder; front of `_ORDER`; `_BUILDERS`; skip-empty guard in `build_briefing`)
- Test: `tests/arena/test_briefing.py`

**Interfaces:**
- Consumes: `autoresolve.pick_promotion` (Task 1); `gs.get_units()`, `gs.get_unit_promotions(unit_id)`.
- Produces: briefing section `"promotions"` rendered first; populates `ctx["units"]` for downstream sections.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/arena/test_briefing.py
from civ_mcp.arena import briefing as _briefing
from civ_mcp.arena.config import VALID_SECTIONS


class _PromoUnit:
    def __init__(self, unit_id, x, y, unit_type="UNIT_WARRIOR"):
        self.unit_id = unit_id
        self.x = x
        self.y = y
        self.unit_type = unit_type
        self.needs_promotion = False  # always false in reality; section must not rely on it


def _promo_status(*types):
    return lq.UnitPromotionStatus(
        unit_id=1, unit_index=1, unit_type="UNIT_WARRIOR",
        promotions=[lq.PromotionOption(promotion_type=t, name=t.replace("PROMOTION_", "").title(), description="d") for t in types],
    )


class _PromoGS:
    def __init__(self, units, promo_by_id):
        self._units = units
        self._promo = promo_by_id

    async def get_units(self):
        return self._units

    async def get_unit_promotions(self, unit_id):
        return self._promo[unit_id]


def test_promotions_is_first_and_registered():
    assert _briefing._ORDER[0] == "promotions"
    assert "promotions" in _briefing._BUILDERS
    assert "promotions" in VALID_SECTIONS


@pytest.mark.asyncio
async def test_promotions_renders_action_block_and_populates_ctx():
    gs = _PromoGS([_PromoUnit(1, 3, 4)], {1: _promo_status("PROMOTION_BATTLECRY")})
    ctx = {}
    text = await _briefing._promotions(gs, ctx)
    assert "promote_unit(unit_id, promotion_type)" in text
    assert "PROMOTION_BATTLECRY" in text
    assert "Battlecry" in text            # suggested pick name
    assert "(id:1)" in text and "(3,4)" in text
    assert ctx["units"] == gs._units      # stored for downstream sections


@pytest.mark.asyncio
async def test_promotions_empty_when_nothing_pending():
    gs = _PromoGS([_PromoUnit(1, 3, 4)], {1: _promo_status()})  # no options
    assert await _briefing._promotions(gs, {}) == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/home/riz/.local/bin/uv run pytest tests/arena/test_briefing.py -q`
Expected: FAIL — `AttributeError: module '...briefing' has no attribute '_promotions'` and `assert 'promotions' in VALID_SECTIONS`

- [ ] **Step 3a: Add `"promotions"` to `VALID_SECTIONS`**

In `src/civ_mcp/arena/config.py`, edit the `VALID_SECTIONS` tuple (lines 8-19) to add `"promotions"` as the first entry:

```python
VALID_SECTIONS = (
    "promotions",
    "overview",
    "units",
    "cities",
    "map",
    "research",
    "production_options",
    "empire_resources",
    "rivals",
    "threats",
    "victory",
)
```

Leave `BriefingOptions.sections` default (line 31) unchanged.

- [ ] **Step 3b: Add the `_promotions` builder to `briefing.py`**

Add the import near the top of `src/civ_mcp/arena/briefing.py` (after the existing `from civ_mcp.arena.config import BriefingOptions`):

```python
from civ_mcp.arena import autoresolve
```

Add the builder (place it above `_overview`, so it reads first):

```python
async def _promotions(gs: Any, ctx: dict[str, Any]) -> str:
    units = ctx.get("units")
    if units is None:
        result = await gs.get_units()
        units = [] if isinstance(result, str) else result
        ctx["units"] = units            # share the fetch with the units/map sections
    if not units:
        return ""

    statuses = await asyncio.gather(
        *(gs.get_unit_promotions(u.unit_id) for u in units),
        return_exceptions=True,
    )
    lines: list[str] = []
    for u, status in zip(units, statuses, strict=True):
        if isinstance(status, Exception) or not getattr(status, "promotions", None):
            continue
        pick = autoresolve.pick_promotion(status)
        if pick is None:
            continue
        opts = ", ".join(f"{o.name} ({o.promotion_type})" for o in status.promotions)
        lines.append(
            f"- {u.unit_type} (id:{u.unit_id}) at ({u.x},{u.y}): suggested {pick.name}\n"
            f"    options: {opts}"
        )
    if not lines:
        return ""
    body = "\n".join(lines)
    return (
        "These units earn NO XP until promoted. Promote them this turn:\n"
        f"{body}\n"
        "Use promote_unit(unit_id, promotion_type)."
    )
```

Register it at the **front** of `_ORDER`:

```python
_ORDER = (
    "promotions",
    "overview",
    "units",
    "cities",
    "production_options",
    "map",
    "research",
    "empire_resources",
    "rivals",
    "threats",
    "victory",
)
```

Add it to `_BUILDERS`:

```python
_BUILDERS: dict[str, Callable[[Any, dict[str, Any]], Awaitable[str]]] = {
    "promotions": _promotions,
    "overview": _overview,
    # ... rest unchanged
}
```

- [ ] **Step 3c: Skip empty sections in `build_briefing`**

An empty section text would otherwise render a bare `== PROMOTIONS ==` header. In `build_briefing`, the non-map branch (`briefing.py:281-282`) becomes:

```python
            else:
                text = await _BUILDERS[name](gs, ctx)
                if not text:
                    continue
```

(No current builder returns `""`, so this only affects the new opt-in section.)

- [ ] **Step 4: Run test to verify it passes**

Run: `/home/riz/.local/bin/uv run pytest tests/arena/test_briefing.py tests/arena/test_config.py -q`
Expected: PASS (existing briefing/config tests + 3 new)

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/config.py src/civ_mcp/arena/briefing.py tests/arena/test_briefing.py
git commit -m "feat(arena): promotions ACTION briefing section (renders first, treatment opt-in)"
```

---

### Task 4: Coordinator wiring — run sweep, record telemetry

**Files:**
- Modify: `src/civ_mcp/arena/coordinator.py` (import `autoresolve`; reorder `:93-100`; add `promotion_sweep` to log + transcript)
- Test: `tests/arena/test_coordinator.py`

**Interfaces:**
- Consumes: `autoresolve.sweep_promotions(gs)` (Task 2).
- Produces: each `log` entry and transcript `record` gains a `"promotion_sweep": list[dict]` key.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/arena/test_coordinator.py
from civ_mcp import lua as lq


class _PromoUnit:
    def __init__(self, unit_id):
        self.unit_id = unit_id
        self.unit_type = "UNIT_WARRIOR"


class SweepGS(FakeGS):
    """FakeGS whose one unit always has a pending BATTLECRY promotion."""
    def __init__(self):
        super().__init__()
        self.promoted = []

    async def get_units(self):
        return [_PromoUnit(1)]

    async def get_unit_promotions(self, unit_id):
        return lq.UnitPromotionStatus(
            unit_id=unit_id, unit_index=1, unit_type="UNIT_WARRIOR",
            promotions=[lq.PromotionOption(promotion_type="PROMOTION_BATTLECRY", name="Battlecry", description="d")],
        )

    async def promote_unit(self, unit_id, promotion_type):
        self.promoted.append((unit_id, promotion_type))
        return f"Promoted {unit_id}"


@pytest.mark.asyncio
async def test_sweep_runs_and_is_logged():
    conn, gs = FakeConn(), SweepGS()
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m")], max_puppet_turns=1,
                      dry_run=True, puppet_ids=[1])
    result = await run_arena(conn, gs, cfg, policy=ScriptedPolicy())
    assert conn.restored is True
    assert gs.promoted == [(1, "PROMOTION_BATTLECRY")]
    entry = result["log"][0]
    assert entry["promotion_sweep"][0]["promotion_type"] == "PROMOTION_BATTLECRY"


class ExplodingSweepGS(FakeGS):
    async def get_units(self):
        raise RuntimeError("tuner blip")


@pytest.mark.asyncio
async def test_sweep_failure_does_not_block_handback(capsys):
    # sweep_promotions swallows internally, but even a hard raise must not stop restore.
    conn, gs = FakeConn(), ExplodingSweepGS()
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m")], max_puppet_turns=1,
                      dry_run=True, puppet_ids=[1])
    result = await run_arena(conn, gs, cfg, policy=ScriptedPolicy())
    assert conn.restored is True
    assert result["log"][0]["promotion_sweep"] == []
```

Note: `sweep_promotions` swallows `get_units` errors and returns `[]`, so `ExplodingSweepGS` exercises the empty-result path; the coordinator's own try/except is the belt-and-suspenders guard tested by the `restored` assertion.

- [ ] **Step 2: Run test to verify it fails**

Run: `/home/riz/.local/bin/uv run pytest tests/arena/test_coordinator.py -q`
Expected: FAIL — `KeyError: 'promotion_sweep'` (and `gs.promoted` empty).

- [ ] **Step 3a: Import `autoresolve`**

In `src/civ_mcp/arena/coordinator.py`, extend the arena import (line 6):

```python
from civ_mcp.arena import hook, autoresolve
```

- [ ] **Step 3b: Reorder the turn body and add the sweep**

Replace the current block (`coordinator.py:93-100`):

```python
                if exclusive and conn.is_connected:
                    await conn.disconnect()       # free the single tuner slot for the CLI
                result = await pol(gs, st.local, st.turn)
                _log_entry = {k: v for k, v in result.items() if k != "transcript"}
                log.append({"player": st.local, "turn": st.turn, **_log_entry})
                if exclusive and not conn.is_connected:
                    await _reconnect_with_retry(conn)   # reclaim before we end the turn
                state_after = await _overview_snapshot(gs) if _tx_on else None
```

with (reconnect moved up before the sweep; sweep before log/snapshot):

```python
                if exclusive and conn.is_connected:
                    await conn.disconnect()       # free the single tuner slot for the CLI
                result = await pol(gs, st.local, st.turn)
                if exclusive and not conn.is_connected:
                    await _reconnect_with_retry(conn)   # reclaim BEFORE the sweep/snapshot/end-turn
                # Deterministic end-of-turn promotion sweep (all puppets). Runs on the
                # reconnected tuner; guarded so a failure can never block the hand-back.
                try:
                    swept = await autoresolve.sweep_promotions(gs)
                except Exception as e:
                    swept = []
                    print(f"[arena] promotion sweep failed: {e!r}", file=sys.stderr)
                _log_entry = {k: v for k, v in result.items() if k != "transcript"}
                log.append({"player": st.local, "turn": st.turn, "promotion_sweep": swept, **_log_entry})
                state_after = await _overview_snapshot(gs) if _tx_on else None
```

- [ ] **Step 3c: Add `promotion_sweep` to the transcript record**

In the `record = { ... }` dict (`coordinator.py:112-127`), add one key (e.g. after `"turn": st.turn,`):

```python
                        "promotion_sweep": swept,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/home/riz/.local/bin/uv run pytest tests/arena/test_coordinator.py -q`
Expected: PASS (existing coordinator tests + 2 new)

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/coordinator.py tests/arena/test_coordinator.py
git commit -m "feat(arena): run promotion sweep post-turn, record in log + transcript"
```

---

### Task 5: Playbook doctrine + experiment YAML sections

**Files:**
- Modify: `src/civ_mcp/arena/playbook.md` (append doctrine sections)
- Modify: `experiments/gemma-strategy-ab-slice1.yaml` (add `promotions` to treatment civ sections)
- Modify: `experiments/gemma-strategy-ab-slice1-50r.yaml` if it exists (same edit)
- Test: `tests/arena/test_experiment.py`

**Interfaces:**
- Consumes: `VALID_SECTIONS` now containing `"promotions"` (Task 3).
- Produces: treatment civs (1, 3, 5, 7) render the promotions section; playbook contains the new headers.

- [ ] **Step 1: Write the failing test**

Update `treatment_sections` in `tests/arena/test_experiment.py:52-63` to include `promotions` first:

```python
    treatment_sections = (
        "promotions",
        "overview",
        "units",
        "cities",
        "map",
        "research",
        "production_options",
        "threats",
        "rivals",
        "empire_resources",
    )
```

Add a playbook-content assertion (new test in the same file):

```python
def test_playbook_covers_promotions_and_expansion_doctrine():
    text = (REPO_ROOT / "src" / "civ_mcp" / "arena" / "playbook.md").read_text()
    for header in ("## Unit promotions", "## Unit upgrades", "## Signals to watch"):
        assert header in text
    assert "NEEDS PROMOTION" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/home/riz/.local/bin/uv run pytest tests/arena/test_experiment.py -q`
Expected: FAIL — `assert player.options.briefing.sections == treatment_sections` (YAML not yet updated) and the new playbook assertion (`## Unit promotions` missing).

- [ ] **Step 3a: Add `promotions` to each treatment civ in the slice1 YAML**

In `experiments/gemma-strategy-ab-slice1.yaml`, for players **1, 3, 5, 7**, change each `sections:` line (currently lines 25, 48, 72, 96) to:

```yaml
      sections: [promotions, overview, units, cities, map, research, production_options, threats, rivals, empire_resources]
```

Leave control civs (2, 4, 6) unchanged (briefing disabled). If `experiments/gemma-strategy-ab-slice1-50r.yaml` exists with the same treatment `sections`, apply the identical edit there.

- [ ] **Step 3b: Append doctrine to `playbook.md`**

Append these sections to `src/civ_mcp/arena/playbook.md` (keep them concise — the playbook shares the token budget with the briefing):

```markdown
## Unit promotions
Units earn XP by surviving combat; ranged units earn XP without taking damage. A
unit with an unspent promotion earns NO more XP until you spend it — always promote
when NEEDS PROMOTION shows. Promoting also heals the unit (use it as mid-fight
sustain). Strong early picks: melee -> Battlecry (+7 attacking); ranged -> Volley
(+5 vs land); recon -> prefer a vision/mobility promotion when offered (Sentry,
Spyglass, Ranger, Alpine). Use get_unit_promotions(unit_id) then
promote_unit(unit_id, promotion_type).

## Unit upgrades
Upgrade obsolete units when you have the tech + resources + gold (Slinger->Archer
with Archery, Warrior->Swordsman with Iron Working). Units fall behind rivals fast
if not upgraded. Use upgrade_unit(unit_id).

## Signals to watch
Loyalty below 75 penalizes a city's yields — assign a governor or fix amenities.
Each new DISTINCT luxury = +1 amenity; duplicates are worthless, so save them to
trade later. Watch era score against the Golden/Dark thresholds shown in the
overview.
```

Also extend the existing `## Expansion` and `## Combat basics` sections in `playbook.md` per the spec (settler siting: prefer fresh water + flat/plains-hills, escort settlers; war is a sequence — position while at peace, the combat engine registers a new enemy only the turn after you declare). Keep each addition to one or two sentences.

- [ ] **Step 4: Run test to verify it passes**

Run: `/home/riz/.local/bin/uv run pytest tests/arena/test_experiment.py -q`
Expected: PASS

- [ ] **Step 5: Run the full arena suite for regressions**

Run: `/home/riz/.local/bin/uv run pytest tests/arena -q`
Expected: PASS (all arena tests green)

- [ ] **Step 6: Commit**

```bash
git add src/civ_mcp/arena/playbook.md experiments/gemma-strategy-ab-slice1.yaml tests/arena/test_experiment.py
git commit -m "feat(arena): promotion/upgrade/expansion doctrine + treatment promotions section"
```

---

## Final verification

- [ ] Run the whole suite (explicit targets to avoid the unrelated `scripts/` collection failures):

Run: `/home/riz/.local/bin/uv run pytest tests -q`
Expected: all green (prior baseline was 398 passed; this slice adds ~14 tests).

- [ ] (Best-effort, non-blocking) Live validation in the recon game, only while the watcher is idle on the human seat: confirm a treatment unit with a pending promotion is shown the ACTION block and is promoted by end of turn, and a control unit is promoted by the sweep. Never stop the watcher mid-AI-phase.

---

## Self-review notes

- **Spec coverage:** 1a (playbook) → Task 5; 1b (ACTION block) → Task 3; 1c (autoresolve + sweep + coordinator) → Tasks 1, 2, 4; 1e (config/YAML/coordinator order) → Tasks 3, 4, 5. 1d (GPP) is explicitly deferred, no task. All slice-1 testing bullets map to a task's tests.
- **Detection correctness:** every filter uses non-empty `get_unit_promotions().promotions`; no code path reads `needs_promotion` (Task 2 test asserts a `needs_promotion=False` unit is still promoted).
- **Type consistency:** `pick_promotion` returns a `PromotionOption` in every task that uses it (Task 3 reads `pick.name`, Task 2 reads `pick.promotion_type`); `sweep_promotions` returns `list[dict]` with the same key set the coordinator and its test expect (`promotion_type`, `ok`).
- **Ordering:** `"promotions"` is first in both `_ORDER` (`briefing.py`) and `VALID_SECTIONS` (`config.py`) and first in each treatment civ's YAML `sections`; `treatment_sections` in the test matches.
