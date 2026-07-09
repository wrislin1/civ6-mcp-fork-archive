# Arena Attention & Turn Skipping Implementation Plan

> **Status:** ✓ DONE — executed 2026-07-09 (12/12 tasks + final-review fix wave,
> commits `9eb00dd..331fe56`); merged to main at `7f1ac2c`. Do not re-execute.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let arena puppets skip quiet turns without an LLM call — a coordinator-side deterministic trigger detector with unconditional veto, plus model-expressed `SKIP: n` / `WAKE IF:` directives, per-civ mode knob `off/auto/model/hybrid`.

**Architecture:** One new module `src/civ_mcp/arena/attention.py` (directive parsing, trigger scan, persisted per-civ state, wake digest) + one insertion point in `run_arena` between `run_pre_model_tasks` and the policy call. Slept turns mechanically reuse the proven failed-turn degrade path (`finish_units` + `restore_local`). Every failure degrades toward MORE model turns, never more sleeps.

**Tech Stack:** Python 3.12, pytest + pytest-asyncio, dataclasses, FireTuner Lua queries (batched, read-only), JSON state files via `civ_mcp.json_io`.

**Spec:** `docs/superpowers/specs/2026-07-09-arena-attention-turn-skipping-design.md` (revision `a47fdb8` — includes the 6 external-review fixes; read it before starting).

## Global Constraints

- Work happens on branch `arena-attention-turn-skipping` in an isolated worktree (superpowers:using-git-worktrees). END STATE = unmerged branch + summary; NEVER merge to main or push without riz's explicit direction.
- Test command: `uv run --extra test pytest tests/ -q` — the `--extra test` is REQUIRED in a fresh worktree (pytest-asyncio lives in the `test` extra, not the dev group; without it 200+ async tests false-fail).
- Scope tests to `tests/` — bare `pytest` collects `scripts/` and fails on a live-game import.
- Baseline at branch point: 859 passed. Full suite must be green at every task's commit.
- `run_lua` must NEVER be added to the arena registry at any tier.
- Fail-open philosophy everywhere: directive unparseable → no directive; state file corrupt → reset + wake; scan error/partial → wake. A failure may only produce more model turns.
- Never log attention sleeps with the key `"skipped"` — that key means a FAILED policy turn (coordinator.py degrade path).
- Follow existing code style: frozen dataclasses, guard-print-continue error handling, `# comment` density as in `memory.py`/`coordinator.py`.
- Commit messages: `feat(arena): ...` / `test(arena): ...` / `docs(arena): ...`, ending with the Claude co-author line.

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `src/civ_mcp/arena/attention.py` | Create | Directive regexes+parsing, AttentionState load/save, trigger-scan Lua build/parse, decision function, digest accumulate/render |
| `tests/arena/test_attention.py` | Create | All attention unit tests (Tasks 4, 6, 7, 8) |
| `src/civ_mcp/arena/config.py` | Modify | `AttentionOptions`, `CivOptions.attention` + fingerprint, `ArenaConfig.max_game_turns` |
| `src/civ_mcp/arena/experiment.py` | Modify | `attention` YAML knob, `max_game_turns` top-level key |
| `src/civ_mcp/arena/arena.py` | Modify | `--max-game-turns` CLI + resolve_config wiring |
| `src/civ_mcp/arena/memory.py` | Modify | Directive lines terminate standing-plan collection (poisoning fix) |
| `src/civ_mcp/arena/prompting.py` | Modify | `ATTENTION_INSTRUCTION`, `digest_block` + `include_attention_instruction` params |
| `src/civ_mcp/arena/agent.py` | Modify | `digest_block` kwarg, attention instruction flag, prompt_injections |
| `src/civ_mcp/arena/cli_agent.py` | Modify | Same as agent.py for the CLI driver |
| `src/civ_mcp/arena/coordinator.py` | Modify | Skip-evaluation block, slept path, budgets, transcript records, capture gate |
| `src/civ_mcp/arena/analyze.py` | Modify | Turn kinds, skip rate, wake-cause histogram, savings, false-quiet, directive quality |
| `src/civ_mcp/arena/playbook.md` | Modify | "Skipping quiet turns" section |
| `docs/superpowers/plans/2026-07-09-arena-attention-live-probes.md` | Create | Post-merge live-probe checklist (4 probes) |
| `tests/arena/test_config.py`, `test_experiment.py`, `test_arena_wiring.py`, `test_memory.py`, `test_prompting.py`, `test_agent.py`, `test_cli_agent.py`, `test_coordinator.py`, `test_analyze.py` | Modify | Per-task test additions |

Dependency order: Task 1 → (2, 3); Task 4 → 5; Tasks 4+6+7 → 8; Tasks 1+4 → 9; everything → 10; 10 → 11; 12 last.

---

### Task 1: `AttentionOptions` + `max_game_turns` (config plumbing)

**Files:**
- Modify: `src/civ_mcp/arena/config.py` (dataclasses at lines 40–60, fingerprint at 62–84, `ArenaConfig` at 139–150)
- Test: `tests/arena/test_config.py`

**Interfaces:**
- Consumes: existing `MemoryOptions` pattern.
- Produces (later tasks import these): `AttentionOptions(mode: str = "off", max_skip: int = 5, max_streak: int = 5, threat_radius: int = 4)` (frozen dataclass); `CivOptions.attention: AttentionOptions`; property `CivOptions.attention_directives_enabled -> bool` (True iff mode in `("model", "hybrid")`); `ArenaConfig.max_game_turns: int = 0` (0 = uncapped); fingerprint sub-dict `"attention": {"mode", "max_skip", "max_streak", "threat_radius"}`.

- [ ] **Step 1: Write the failing tests** — append to `tests/arena/test_config.py`:

```python
from civ_mcp.arena.config import AttentionOptions

def test_attention_defaults_off():
    opts = CivOptions()
    assert opts.attention.mode == "off"
    assert opts.attention.max_skip == 5
    assert opts.attention.max_streak == 5
    assert opts.attention.threat_radius == 4

def test_attention_in_fingerprint():
    opts = CivOptions(attention=AttentionOptions(mode="hybrid", max_skip=3))
    fp = opts.fingerprint()
    assert fp["attention"] == {
        "mode": "hybrid", "max_skip": 3, "max_streak": 5, "threat_radius": 4,
    }

def test_attention_directives_enabled_property():
    assert not CivOptions().attention_directives_enabled
    assert not CivOptions(attention=AttentionOptions(mode="auto")).attention_directives_enabled
    assert CivOptions(attention=AttentionOptions(mode="model")).attention_directives_enabled
    assert CivOptions(attention=AttentionOptions(mode="hybrid")).attention_directives_enabled

def test_arena_config_max_game_turns_default_uncapped():
    assert ArenaConfig(players=[]).max_game_turns == 0
```

(`CivOptions`, `ArenaConfig` are already imported at the top of this test file.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra test pytest tests/arena/test_config.py -q`
Expected: FAIL — `ImportError: cannot import name 'AttentionOptions'`

- [ ] **Step 3: Implement** in `src/civ_mcp/arena/config.py`. After `TaskTrackerOptions` (line ~49) add:

```python
@dataclass(frozen=True)
class AttentionOptions:
    """Quiet-turn attention policy (spec 2026-07-09). mode: off|auto|model|hybrid."""
    mode: str = "off"
    max_skip: int = 5        # upper clamp for a model's SKIP: n
    max_streak: int = 5      # coordinator-side consecutive-sleep cap
    threat_radius: int = 4   # hostile-scan radius around cities/civilians
```

In `CivOptions` add the field (after `task_tracker`):

```python
    attention: AttentionOptions = field(default_factory=AttentionOptions)
```

In `fingerprint()` add after the `"task_tracker"` entry:

```python
            "attention": {
                "mode": self.attention.mode,
                "max_skip": self.attention.max_skip,
                "max_streak": self.attention.max_streak,
                "threat_radius": self.attention.threat_radius,
            },
```

After the `standing_plan_enabled` property add:

```python
    @property
    def attention_directives_enabled(self) -> bool:
        return self.attention.mode in ("model", "hybrid")
```

In `ArenaConfig` add after `max_puppet_turns`:

```python
    max_game_turns: int = 0  # caps ALL captured turns (played+slept+failed); 0 = uncapped
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra test pytest tests/arena/test_config.py -q`
Expected: PASS

- [ ] **Step 5: Full suite + commit**

Run: `uv run --extra test pytest tests/ -q` — expected: all pass (existing fingerprint golden tests may assert exact dict equality; if one fails, update its expected dict to include the new `attention` sub-dict — that change is the intended fingerprint break, spec §5).

```bash
git add src/civ_mcp/arena/config.py tests/arena/test_config.py
git commit -m "feat(arena): AttentionOptions + max_game_turns config plumbing"
```

---

### Task 2: YAML experiment keys

**Files:**
- Modify: `src/civ_mcp/arena/experiment.py` (`_SHARED_KNOBS` line ~29, `_TOP_KEYS` line 37, defaults block line ~39, `_parse_memory` pattern at 156, civ-build site at ~245, top-level parse at ~323)
- Test: `tests/arena/test_experiment.py`

**Interfaces:**
- Consumes: `AttentionOptions` from Task 1.
- Produces: per-civ YAML key `attention: {mode, max_skip, max_streak, threat_radius}`; top-level YAML key `max_game_turns` (int ≥ 0). Unknown sub-keys / bad mode / non-positive ints raise `ValueError` with the `_err` civ-label prefix.

- [ ] **Step 1: Write the failing tests** — append to `tests/arena/test_experiment.py` (use the file's existing `load_experiment`-from-string helper; every existing test constructs YAML text and calls `load_experiment(path)` via `tmp_path` — copy that local pattern):

```python
def test_attention_yaml_parsed(tmp_path):
    cfg = _load(tmp_path, """
run_id: t1
civs:
  - player: 1
    provider: local
    model: m
    attention:
      mode: hybrid
      max_skip: 3
""")
    assert cfg.players[0].options.attention.mode == "hybrid"
    assert cfg.players[0].options.attention.max_skip == 3
    assert cfg.players[0].options.attention.max_streak == 5  # default preserved

def test_attention_bad_mode_rejected(tmp_path):
    with pytest.raises(ValueError, match="attention.mode"):
        _load(tmp_path, """
run_id: t1
civs:
  - {player: 1, provider: local, model: m, attention: {mode: sometimes}}
""")

def test_attention_unknown_subkey_rejected(tmp_path):
    with pytest.raises(ValueError, match="attention"):
        _load(tmp_path, """
run_id: t1
civs:
  - {player: 1, provider: local, model: m, attention: {mode: auto, nap_time: 9}}
""")

def test_max_game_turns_top_level(tmp_path):
    cfg = _load(tmp_path, """
run_id: t1
max_game_turns: 200
civs:
  - {player: 1, provider: local, model: m}
""")
    assert cfg.max_game_turns == 200

def test_max_game_turns_negative_rejected(tmp_path):
    with pytest.raises(ValueError, match="max_game_turns"):
        _load(tmp_path, """
run_id: t1
max_game_turns: -1
civs:
  - {player: 1, provider: local, model: m}
""")
```

If the file has no `_load(tmp_path, text)` helper, add one that writes `text` to `tmp_path / "exp.yaml"` and returns `load_experiment(str(tmp_path / "exp.yaml"))` — but check first; most tests there wrap exactly this.

- [ ] **Step 2: Run to verify failure**

Run: `uv run --extra test pytest tests/arena/test_experiment.py -q`
Expected: FAIL — attention key rejected as unknown civ key.

- [ ] **Step 3: Implement** in `src/civ_mcp/arena/experiment.py`:

Import `AttentionOptions` alongside the other config imports. Add `"attention"` to `_SHARED_KNOBS`. Add `"max_game_turns"` to `_TOP_KEYS`. Add `_ATTENTION_DEFAULTS = AttentionOptions()` next to `_MEMORY_DEFAULTS`. Add after `_parse_task_tracker`:

```python
_ATTENTION_MODES = ("off", "auto", "model", "hybrid")


def _parse_attention(civ_label: str, raw: object) -> AttentionOptions:
    if not isinstance(raw, dict):
        raise _err(civ_label, f"attention must be a mapping, got {raw!r}")
    _validate_mapping_keys(
        civ_label, raw, {"mode", "max_skip", "max_streak", "threat_radius"}, "attention"
    )
    mode = raw.get("mode", _ATTENTION_DEFAULTS.mode)
    if mode not in _ATTENTION_MODES:
        raise _err(civ_label, f"attention.mode must be one of {_ATTENTION_MODES}, got {mode!r}")
    return AttentionOptions(
        mode=mode,
        max_skip=_positive_int(
            civ_label, "attention.max_skip", raw.get("max_skip", _ATTENTION_DEFAULTS.max_skip)
        ),
        max_streak=_positive_int(
            civ_label, "attention.max_streak", raw.get("max_streak", _ATTENTION_DEFAULTS.max_streak)
        ),
        threat_radius=_positive_int(
            civ_label,
            "attention.threat_radius",
            raw.get("threat_radius", _ATTENTION_DEFAULTS.threat_radius),
        ),
    )
```

At the civ-build site (where `memory = _MEMORY_DEFAULTS if "memory" not in raw else _parse_memory(...)` sits, line ~245), add the same shape:

```python
    attention = (
        _ATTENTION_DEFAULTS if "attention" not in raw else _parse_attention(label, raw["attention"])
    )
```

and pass `attention=attention` into the `CivOptions(...)` construction just below.

At the top-level `ArenaConfig(...)` build (line ~323), add:

```python
        max_game_turns=_top_non_negative_int(
            data.get("max_game_turns", arena_defaults.max_game_turns)
        ),
```

with this helper next to `_top_int` (note `bool` is an `int` subclass — exclude it):

```python
def _top_non_negative_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(
            f"experiment config: max_game_turns must be an integer >= 0, got {value!r}"
        )
    return value
```

- [ ] **Step 4: Run tests** — `uv run --extra test pytest tests/arena/test_experiment.py -q` — PASS.
- [ ] **Step 5: Full suite + commit**

```bash
git add src/civ_mcp/arena/experiment.py tests/arena/test_experiment.py
git commit -m "feat(arena): attention + max_game_turns YAML experiment keys"
```

---

### Task 3: CLI flag `--max-game-turns`

**Files:**
- Modify: `src/civ_mcp/arena/arena.py` (`build_args` line ~41, `resolve_config` line ~79)
- Test: `tests/arena/test_arena_wiring.py`

**Interfaces:**
- Consumes: `ArenaConfig.max_game_turns` (Task 1).
- Produces: `--max-game-turns N` on the `--player` path; on the `--config` path it is config-owned (rejected as an override, like `--max-puppet-turns`) with a suppressed `--config-default-max-game-turns` passthrough.

- [ ] **Step 1: Write the failing tests** — append to `tests/arena/test_arena_wiring.py` (this file already builds args via `build_args([...])` + `resolve_config`; follow its local pattern):

```python
def test_max_game_turns_cli_flag():
    args = build_args(["--player", "1:local:m", "--max-game-turns", "150"])
    cfg = resolve_config(args)
    assert cfg.max_game_turns == 150

def test_max_game_turns_defaults_uncapped():
    args = build_args(["--player", "1:local:m"])
    assert resolve_config(args).max_game_turns == 0

def test_max_game_turns_rejected_with_config(tmp_path):
    exp = tmp_path / "e.yaml"
    exp.write_text("run_id: t1\ncivs:\n  - {player: 1, provider: local, model: m}\n")
    args = build_args(["--config", str(exp), "--max-game-turns", "5"])
    with pytest.raises(SystemExit, match="config-owned"):
        resolve_config(args)
```

- [ ] **Step 2: Run to verify failure** — `uv run --extra test pytest tests/arena/test_arena_wiring.py -q` — FAIL (unrecognized argument).

- [ ] **Step 3: Implement** in `src/civ_mcp/arena/arena.py`:

In `build_args`, after the `--max-puppet-turns` line:

```python
    ap.add_argument("--max-game-turns", type=int, default=None,
                    help="cap on ALL captured turns incl. slept (0 = uncapped)")
```

and with the other suppressed passthroughs:

```python
    ap.add_argument("--config-default-max-game-turns", type=int, default=None, help=argparse.SUPPRESS)
```

In `resolve_config`: add `max_game_turns_arg = getattr(args, "max_game_turns", None)` next to `max_puppet_turns_arg`; add it to the `rejected` config-owned check (`if max_game_turns_arg is not None: rejected.append("--max-game-turns")`); add to `config_defaults`:

```python
            max_game_turns=_value_or_default(
                getattr(args, "config_default_max_game_turns", None),
                defaults.max_game_turns,
            ),
```

and to the final `--player`-path `ArenaConfig(...)`:

```python
                       max_game_turns=_value_or_default(max_game_turns_arg, defaults.max_game_turns),
```

- [ ] **Step 4: Run tests** — PASS.
- [ ] **Step 5: Full suite + commit**

```bash
git add src/civ_mcp/arena/arena.py tests/arena/test_arena_wiring.py
git commit -m "feat(arena): --max-game-turns CLI flag"
```

---

### Task 4: Directive parsing (`attention.py` created)

**Files:**
- Create: `src/civ_mcp/arena/attention.py`
- Create: `tests/arena/test_attention.py`

**Interfaces:**
- Produces (Task 5 imports the regexes; Task 10 imports the functions):
  - `SKIP_LINE_RE`, `WAKE_IF_LINE_RE` — compiled patterns, match a whole line
  - `SOFT_TRIGGERS: tuple[str, ...] = ("GREAT_PERSON_AVAILABLE", "CITY_GREW", "TRADE_ROUTE_IDLE", "GOLD_STOCKPILE_HIGH")`
  - `Directive(skip: int, wake_if: tuple[str, ...], unknown_tokens: tuple[str, ...], clamped: bool)` frozen dataclass
  - `parse_directive(summary: str, max_skip: int) -> Directive | None`
  - `has_directive_lines(summary: str) -> bool` (for the "directive not recognized" ack)

- [ ] **Step 1: Write the failing tests** — create `tests/arena/test_attention.py`:

```python
import pytest

from civ_mcp.arena.attention import (
    Directive,
    has_directive_lines,
    parse_directive,
)


def test_plain_directive():
    d = parse_directive("done.\nSKIP: 3\nWAKE IF: GREAT_PERSON_AVAILABLE, CITY_GREW", 5)
    assert d == Directive(skip=3, wake_if=("GREAT_PERSON_AVAILABLE", "CITY_GREW"),
                          unknown_tokens=(), clamped=False)

def test_markdown_variants():
    # models reformat plain markers into markdown (the memory.py lesson)
    for text in ("**SKIP:** 2", "- SKIP: 2", "## SKIP: 2", "*skip*: 2 turns"):
        d = parse_directive(text, 5)
        assert d is not None and d.skip == 2, text

def test_clamping():
    assert parse_directive("SKIP: 99", 5) == Directive(5, (), (), True)
    assert parse_directive("SKIP: 0", 5) == Directive(1, (), (), True)
    assert parse_directive("SKIP: 2", 3).skip == 2

def test_wake_if_without_skip_is_inert():
    assert parse_directive("WAKE IF: CITY_GREW", 5) is None
    assert has_directive_lines("WAKE IF: CITY_GREW")  # ack loop can say "not recognized"

def test_unknown_tokens_dropped_not_fatal():
    d = parse_directive("SKIP: 2\nWAKE IF: CITY_GREW, SCIENCE_OVER_200", 5)
    assert d.wake_if == ("CITY_GREW",)
    assert d.unknown_tokens == ("SCIENCE_OVER_200",)

def test_garbage_no_directive():
    assert parse_directive("SKIP: soon-ish, when quiet", 5) is None
    assert parse_directive("nothing here", 5) is None
    assert not has_directive_lines("nothing here")

def test_prose_mentioning_skip_mid_sentence_does_not_match():
    assert parse_directive("I will skip: nothing important this turn happened", 5) is None
```

Note the last test: `"I will skip: ..."` must NOT match — the regex requires the line to START (after bullet/heading/emphasis prefixes) with the keyword, same as `STANDING_PLAN_RE`.

- [ ] **Step 2: Run to verify failure** — `uv run --extra test pytest tests/arena/test_attention.py -q` — FAIL (module missing).

- [ ] **Step 3: Implement** — create `src/civ_mcp/arena/attention.py`:

```python
"""Attention & turn-skipping for arena LLM puppets (quiet-turn fast path).

Owns: SKIP:/WAKE IF: directive parsing, the per-turn trigger scan, the
persisted per-civ attention state, and the wake-digest accumulator/renderer.
Spec: docs/superpowers/specs/2026-07-09-arena-attention-turn-skipping-design.md

Philosophy: every failure here degrades toward MORE model turns, never more
blind skips. Directive unparseable -> no directive. State corrupt -> reset +
wake. Scan error or partial -> wake.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

SOFT_TRIGGERS: tuple[str, ...] = (
    "GREAT_PERSON_AVAILABLE",
    "CITY_GREW",
    "TRADE_ROUTE_IDLE",
    "GOLD_STOCKPILE_HIGH",
)

# Tolerant line matchers, the STANDING_PLAN_RE lesson (memory.py): models
# reformat instructed plain markers into bullets/headings/emphasis, and a
# silent miss is a directive that never takes effect. The keyword must open
# the line (after markdown prefixes) so prose like "I will skip: nothing"
# never matches. Also imported by memory.extract_standing_plan as standing-
# plan TERMINATORS so a directive after the plan block is never persisted
# as plan text (external-review catch: the plan collector's header test
# requires a trailing colon, so "SKIP: 3" would otherwise be swallowed).
_DIRECTIVE_PREFIX = r"^\s*(?:[-*•]+\s+)?(?:#{1,6}\s*)?(?:[*_]{1,3})?\s*"
SKIP_LINE_RE = re.compile(
    _DIRECTIVE_PREFIX + r"skip\s*(?:[*_]{1,3})?\s*:\s*(?P<body>.*)$", re.IGNORECASE
)
WAKE_IF_LINE_RE = re.compile(
    _DIRECTIVE_PREFIX + r"wake\s+if\s*(?:[*_]{1,3})?\s*:\s*(?P<body>.*)$", re.IGNORECASE
)


@dataclass(frozen=True)
class Directive:
    skip: int
    wake_if: tuple[str, ...] = ()
    unknown_tokens: tuple[str, ...] = ()
    clamped: bool = False


def has_directive_lines(summary: str) -> bool:
    """True if any line looks like a directive attempt (valid or not)."""
    return any(
        SKIP_LINE_RE.match(line) or WAKE_IF_LINE_RE.match(line)
        for line in summary.splitlines()
    )


def parse_directive(summary: str, max_skip: int) -> Directive | None:
    """Extract a SKIP/WAKE IF directive from a final summary, or None.

    First SKIP line wins. WAKE IF without SKIP is inert (spec: sleep must be
    freshly and explicitly chosen). Unknown WAKE IF tokens are collected, not
    fatal. SKIP body must contain an integer ("SKIP: 3 turns" tolerated).
    """
    skip: int | None = None
    clamped = False
    wake_if: list[str] = []
    unknown: list[str] = []
    for line in summary.splitlines():
        m = SKIP_LINE_RE.match(line)
        if m and skip is None:
            num = re.search(r"-?\d+", m.group("body"))
            if num:
                n = int(num.group())
                skip = min(max(n, 1), max_skip)
                clamped = skip != n
            continue
        m = WAKE_IF_LINE_RE.match(line)
        if m:
            for tok in re.split(r"[,\s]+", m.group("body")):
                token = tok.strip("`*_.").upper()
                if not token:
                    continue
                if token in SOFT_TRIGGERS:
                    if token not in wake_if:
                        wake_if.append(token)
                else:
                    unknown.append(token)
    if skip is None:
        return None
    return Directive(
        skip=skip, wake_if=tuple(wake_if), unknown_tokens=tuple(unknown), clamped=clamped
    )
```

- [ ] **Step 4: Run tests** — PASS.
- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/attention.py tests/arena/test_attention.py
git commit -m "feat(arena): attention directive parsing (SKIP / WAKE IF)"
```

---

### Task 5: Standing-plan poisoning fix (memory.py)

**Files:**
- Modify: `src/civ_mcp/arena/memory.py` (import block line ~21, collection loop in `extract_standing_plan` lines 178–186)
- Test: `tests/arena/test_memory.py`

**Interfaces:**
- Consumes: `SKIP_LINE_RE`, `WAKE_IF_LINE_RE` from Task 4 (`attention.py` imports nothing from `memory.py`, so no cycle — same direction as the existing `TASK_LINE_RE` import from `task_tracker`).
- Produces: `extract_standing_plan` treats any directive line as a terminator; directive text NEVER appears in the returned plan.

- [ ] **Step 1: Write the failing tests** — append to `tests/arena/test_memory.py`:

```python
def test_directive_after_plan_not_persisted():
    summary = (
        "STANDING PLAN:\n- finish Campus in Suwon\n- settler -> (14,22)\n"
        "SKIP: 3\nWAKE IF: GREAT_PERSON_AVAILABLE"
    )
    plan = extract_standing_plan(summary, 1200)
    assert "finish Campus" in plan and "settler" in plan
    assert "SKIP" not in plan and "WAKE IF" not in plan

def test_directive_markdown_form_still_terminates():
    plan = extract_standing_plan("STANDING PLAN: hold the line\n**SKIP:** 2", 1200)
    assert plan == "hold the line"

def test_directive_before_plan_leaves_capture_intact():
    plan = extract_standing_plan("SKIP: 2\nSTANDING PLAN: expand east", 1200)
    assert plan == "expand east"
```

(`extract_standing_plan` is already imported in this test file.)

- [ ] **Step 2: Run to verify failure** — `uv run --extra test pytest tests/arena/test_memory.py -q` — the first two FAIL (directive text collected into the plan).

- [ ] **Step 3: Implement** — in `src/civ_mcp/arena/memory.py`, extend the existing import:

```python
from civ_mcp.arena.attention import SKIP_LINE_RE, WAKE_IF_LINE_RE
```

In `extract_standing_plan`'s collection loop, add the terminator check FIRST (before `_is_section_header`):

```python
    for offset, line in enumerate(following):
        # Attention directives (SKIP:/WAKE IF:) terminate the plan and are
        # never plan content -- without this they'd be persisted and
        # re-injected every turn (external-review catch).
        if SKIP_LINE_RE.match(line) or WAKE_IF_LINE_RE.match(line):
            break
        if _is_section_header(line, following[offset + 1 :]):
            break
```

Also update the docstring's terminator sentence ("Stops at ...") to mention attention-directive lines.

- [ ] **Step 4: Run tests** — PASS (all of test_memory.py — the existing tolerance tests must still pass untouched).
- [ ] **Step 5: Full suite + commit**

```bash
git add src/civ_mcp/arena/memory.py tests/arena/test_memory.py
git commit -m "fix(arena): SKIP/WAKE IF lines terminate standing-plan capture"
```

---

### Task 6: Attention state file + wake digest

**Files:**
- Modify: `src/civ_mcp/arena/attention.py`
- Test: `tests/arena/test_attention.py`

**Interfaces:**
- Consumes: `read_json_file`, `write_json_file_atomic` from `civ_mcp.json_io` (the `memory.py` persistence pattern).
- Produces (Task 10 consumes):
  - `AttentionState` frozen dataclass — fields below; `state.directive` is a plain dict `{"skip": int, "wake_if": [...]}` or `None` (JSON-native, no nested dataclass round-trip)
  - `attention_path(transcript_dir, run_id, player_id) -> Path` = `<transcript_dir>/<run_id>/attention/player_<id>.json`
  - `load_attention_state(transcript_dir, run_id, player_id) -> AttentionState` — absent/corrupt/mismatched → fresh default state (reset + wake semantics)
  - `save_attention_state(transcript_dir, run_id, player_id, state) -> None` — atomic
  - `note_sleep(state, *, turn, snapshot, scan_scalars, task_notes, notifications) -> AttentionState`
  - `note_wake(state, *, turn, wake_cause, directive, directive_ack, snapshot, scan_scalars) -> AttentionState`
  - `render_digest(state, *, wake_turn, wake_cause, wake_detail) -> str` — `""` when nothing slept; capped `DIGEST_MAX_CHARS = 1200`

- [ ] **Step 1: Write the failing tests** — append to `tests/arena/test_attention.py`:

```python
from civ_mcp.arena.attention import (
    AttentionState,
    attention_path,
    load_attention_state,
    note_sleep,
    note_wake,
    render_digest,
    save_attention_state,
)


def _seeded_state():
    return AttentionState(
        run_id="r1", player_id=3, directive={"skip": 3, "wake_if": []},
        skips_remaining=3, streak=0, last_wake_turn=44,
        last_snapshot={"score": 100, "gold": 50, "units": 4, "cities": 2},
        last_scan={"at_war_with": [], "era_index": 1, "total_population": 8},
    )


def test_state_round_trip(tmp_path):
    st = _seeded_state()
    save_attention_state(str(tmp_path), "r1", 3, st)
    assert load_attention_state(str(tmp_path), "r1", 3) == st

def test_corrupt_state_resets(tmp_path):
    p = attention_path(str(tmp_path), "r1", 3)
    p.parent.mkdir(parents=True)
    p.write_text("{not json")
    st = load_attention_state(str(tmp_path), "r1", 3)
    assert st.streak == 0 and st.skips_remaining == 0 and st.last_snapshot is None

def test_note_sleep_accumulates_and_decrements():
    st = _seeded_state()
    st = note_sleep(st, turn=45, snapshot={"score": 104, "gold": 60, "units": 4, "cities": 2},
                    scan_scalars={"at_war_with": [], "era_index": 1, "total_population": 8},
                    task_notes=[], notifications=[("NOTIFICATION_X", "border expanded")])
    assert st.skips_remaining == 2 and st.streak == 1
    assert len(st.slept) == 1 and st.slept[0]["turn"] == 45
    assert st.last_snapshot["score"] == 104  # baseline advances every slept turn

def test_note_wake_cancels_remainder_and_clears_digest():
    st = _seeded_state()
    st = note_sleep(st, turn=45, snapshot=st.last_snapshot, scan_scalars=st.last_scan,
                    task_notes=[], notifications=[])
    st = note_wake(st, turn=46, wake_cause="ENEMY_NEAR", directive=None,
                   directive_ack="woken early by ENEMY_NEAR after 1 of 3",
                   snapshot={"score": 105}, scan_scalars={"era_index": 1})
    assert st.skips_remaining == 0 and st.streak == 0 and st.slept == []
    assert st.last_wake_turn == 46

def test_render_digest_contents_and_cap():
    st = _seeded_state()
    st = note_sleep(st, turn=45, snapshot={"score": 104, "gold": 60, "units": 4, "cities": 2},
                    scan_scalars=st.last_scan, task_notes=["settler advanced"],
                    notifications=[("NOTIFICATION_X", "Suwon border expanded")])
    text = render_digest(st, wake_turn=46, wake_cause="STREAK_CAP", wake_detail="")
    assert text.startswith("== WHILE YOU SLEPT")
    assert "STREAK_CAP" in text and "Suwon border expanded" in text
    assert len(text) <= 1200

def test_render_digest_empty_without_sleeps():
    assert render_digest(_seeded_state(), wake_turn=45, wake_cause="", wake_detail="") == ""
```

- [ ] **Step 2: Run to verify failure** — imports fail.

- [ ] **Step 3: Implement** — add to `attention.py`:

```python
from dataclasses import dataclass, field, replace
from pathlib import Path

from civ_mcp.json_io import read_json_file, write_json_file_atomic

SCHEMA_VERSION = 1
DIGEST_MAX_CHARS = 1200
DIGEST_MAX_NOTIFICATIONS = 10  # the gossip lesson: never an unbounded feed


@dataclass(frozen=True)
class AttentionState:
    """Per-civ persisted skip state. Corrupt file -> fresh state -> wake."""
    schema_version: int = SCHEMA_VERSION
    run_id: str = ""
    player_id: int = -1
    directive: dict | None = None      # {"skip": int, "wake_if": [...]} as issued
    skips_remaining: int = 0
    streak: int = 0                    # consecutive sleeps since last model turn
    last_wake_turn: int = -1
    last_snapshot: dict | None = None  # overview snapshot at previous captured turn
    last_scan: dict | None = None      # stored scalars: at_war_with/era_index/total_population
    slept: list = field(default_factory=list)   # digest accumulator, one dict per slept turn
    directive_ack: str = ""            # reported in the next wake digest


def attention_path(transcript_dir: str, run_id: str, player_id: int) -> Path:
    return Path(transcript_dir) / run_id / "attention" / f"player_{player_id}.json"


def load_attention_state(transcript_dir: str, run_id: str, player_id: int) -> AttentionState:
    data = read_json_file(attention_path(transcript_dir, run_id, player_id))
    fresh = AttentionState(run_id=run_id, player_id=player_id)
    if not isinstance(data, dict):
        return fresh
    try:
        st = AttentionState(
            schema_version=int(data["schema_version"]),
            run_id=str(data["run_id"]),
            player_id=int(data["player_id"]),
            directive=data.get("directive"),
            skips_remaining=int(data.get("skips_remaining", 0)),
            streak=int(data.get("streak", 0)),
            last_wake_turn=int(data.get("last_wake_turn", -1)),
            last_snapshot=data.get("last_snapshot"),
            last_scan=data.get("last_scan"),
            slept=list(data.get("slept", [])),
            directive_ack=str(data.get("directive_ack", "")),
        )
    except (KeyError, TypeError, ValueError):
        return fresh
    if st.run_id != run_id or st.player_id != player_id:
        return fresh
    return st


def save_attention_state(
    transcript_dir: str, run_id: str, player_id: int, state: AttentionState
) -> None:
    payload = {
        "schema_version": state.schema_version,
        "run_id": state.run_id,
        "player_id": state.player_id,
        "directive": state.directive,
        "skips_remaining": state.skips_remaining,
        "streak": state.streak,
        "last_wake_turn": state.last_wake_turn,
        "last_snapshot": state.last_snapshot,
        "last_scan": state.last_scan,
        "slept": state.slept,
        "directive_ack": state.directive_ack,
    }
    write_json_file_atomic(attention_path(transcript_dir, run_id, player_id), payload)


def note_sleep(
    state: AttentionState, *, turn: int, snapshot: dict | None,
    scan_scalars: dict | None, task_notes: list, notifications: list,
) -> AttentionState:
    record = {
        "turn": turn,
        "snapshot": snapshot,
        "task_notes": list(task_notes),
        "notifications": [list(n) for n in notifications][:DIGEST_MAX_NOTIFICATIONS],
    }
    return replace(
        state,
        skips_remaining=max(0, state.skips_remaining - 1),
        streak=state.streak + 1,
        last_snapshot=snapshot if snapshot is not None else state.last_snapshot,
        last_scan=scan_scalars if scan_scalars is not None else state.last_scan,
        slept=[*state.slept, record],
    )


def note_wake(
    state: AttentionState, *, turn: int, wake_cause: str, directive: "Directive | None",
    directive_ack: str, snapshot: dict | None, scan_scalars: dict | None,
) -> AttentionState:
    # Any wake cancels the remainder (spec: sleep is always freshly chosen).
    new_directive = None
    remaining = 0
    if directive is not None:
        new_directive = {"skip": directive.skip, "wake_if": list(directive.wake_if)}
        remaining = directive.skip
    return replace(
        state,
        directive=new_directive,
        skips_remaining=remaining,
        streak=0,
        last_wake_turn=turn,
        last_snapshot=snapshot if snapshot is not None else state.last_snapshot,
        last_scan=scan_scalars if scan_scalars is not None else state.last_scan,
        slept=[],
        directive_ack=directive_ack,
    )


def render_digest(
    state: AttentionState, *, wake_turn: int, wake_cause: str, wake_detail: str
) -> str:
    """Priority order (spec section 4): wake cause, directive ack, accumulated
    deltas, tracker progress, notifications (newest first, capped)."""
    if not state.slept:
        return ""
    first = state.slept[0]["turn"]
    last = state.slept[-1]["turn"]
    n = len(state.slept)
    lines = [f"== WHILE YOU SLEPT (turns {first}–{last}, {n} skipped) =="]
    cause = wake_cause + (f" — {wake_detail}" if wake_detail else "")
    lines.append(f"Woke because: {cause}")
    if state.directive_ack:
        lines.append(f"Your directive: {state.directive_ack}")
    snaps = [r["snapshot"] for r in state.slept if r.get("snapshot")]
    if snaps:
        first_s, last_s = snaps[0], snaps[-1]
        lines.append("Empire while asleep:")
        deltas = []
        for key in ("score", "gold", "science", "culture", "cities", "units"):
            a, b = first_s.get(key), last_s.get(key)
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                deltas.append(f"{key} {a}→{b}")
        if deltas:
            lines.append("- " + ", ".join(deltas))
    notes = [note for r in state.slept for note in r.get("task_notes", [])]
    if notes:
        lines.append("Tracker: " + "; ".join(notes[-5:]))
    tagged = [
        (rec["turn"], pair[1] if len(pair) > 1 else str(pair))
        for rec in state.slept
        for pair in rec.get("notifications", [])
    ]
    if tagged:
        lines.append(
            f"Notifications during sleep (newest first, max {DIGEST_MAX_NOTIFICATIONS}):"
        )
        for turn_no, msg in list(reversed(tagged))[:DIGEST_MAX_NOTIFICATIONS]:
            lines.append(f"- [T{turn_no}] {msg}")
    text = "\n".join(lines)
    return text[:DIGEST_MAX_CHARS].rstrip()
```

- [ ] **Step 4: Run tests** — PASS.
- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/attention.py tests/arena/test_attention.py
git commit -m "feat(arena): attention state file + wake-digest accumulator/renderer"
```

---

### Task 7: Trigger scan (Lua build + parse)

**Files:**
- Modify: `src/civ_mcp/arena/attention.py`
- Test: `tests/arena/test_attention.py`

**Interfaces:**
- Produces (Tasks 8, 10 consume):
  - `build_attention_query(player_id: int, threat_radius: int) -> str`
  - `AttentionScan` frozen dataclass with fields: `hostile_count: int`, `nearest_hostile: str`, `damaged_city_ids: tuple[int, ...]`, `at_war_with: tuple[int, ...]`, `negative_loyalty_city_ids: tuple[int, ...]`, `wc_turns_until_next: int` (−1 unknown), `era_index: int` (−1 unknown), `total_population: int`, `great_person_available: bool`, `trade_route_idle: bool`, `pending_diplomacy: bool`, `blocker_types: tuple[str, ...]`, `notifications: tuple[tuple[str, str], ...]`, `failed_families: tuple[str, ...]`
  - `parse_attention_scan(lines: list[str] | None) -> AttentionScan | None` — no ATTN lines at all → `None`
  - `scan_scalars(scan) -> dict` — `{"at_war_with": [...], "era_index": int, "total_population": int}` (what `AttentionState.last_scan` stores)
  - `BLOCKER_IGNORE = frozenset({"ENDTURN_BLOCKING_UNIT_PROMOTION"})` (promotions are swept post-turn)
  - `NOTIFICATION_WAKE_LIST = frozenset({"NOTIFICATION_CITY_UNDER_ATTACK", "NOTIFICATION_CITY_LOW_LOYALTY", "NOTIFICATION_REBELLION", "NOTIFICATION_SPY_CAUGHT"})` — starts conservative, curated from live evidence (spec Open Items)

**Line protocol** (int-cast `player_id`/`threat_radius` before splicing — the Lua-injection rule; both arrive as ints from config but cast anyway):

```
ATTN|THREAT|count=2|nearest=Barbarian Horseman d3 near Suwon
ATTN|CITYHP|damaged=17,42          (empty value for none)
ATTN|WAR|with=3,5
ATTN|LOYALTY|negative=17
ATTN|WC|turns=7                    (-1 when API unavailable)
ATTN|ERA|index=3
ATTN|POP|total=23
ATTN|GP|available=1
ATTN|TRADE|idle=0
ATTN|DIPLO|pending=0
ATTN|BLOCKERS|types=NOTIFICATION_PRODUCTION,NOTIFICATION_CHOOSE_TECH   (empty for none)
ATTN|NOTIFY|type=NOTIFICATION_X|msg=Suwon border expanded              (repeated, max 10)
ATTN_ERR|LOYALTY                   (a family whose pcall failed)
---END---
```

- [ ] **Step 1: Write the failing tests** — append to `tests/arena/test_attention.py`:

```python
from civ_mcp.arena.attention import (
    AttentionScan,
    build_attention_query,
    parse_attention_scan,
    scan_scalars,
)

QUIET_LINES = [
    "ATTN|THREAT|count=0|nearest=",
    "ATTN|CITYHP|damaged=",
    "ATTN|WAR|with=",
    "ATTN|LOYALTY|negative=",
    "ATTN|WC|turns=5",
    "ATTN|ERA|index=1",
    "ATTN|POP|total=12",
    "ATTN|GP|available=0",
    "ATTN|TRADE|idle=0",
    "ATTN|DIPLO|pending=0",
    "ATTN|BLOCKERS|types=",
]


def test_parse_quiet_scan():
    scan = parse_attention_scan(QUIET_LINES)
    assert scan.hostile_count == 0 and scan.blocker_types == ()
    assert scan.at_war_with == () and scan.era_index == 1
    assert scan.failed_families == ()

def test_parse_busy_scan():
    lines = [
        "ATTN|THREAT|count=2|nearest=Barbarian Horseman d3 near Suwon",
        "ATTN|CITYHP|damaged=17,42",
        "ATTN|WAR|with=3",
        "ATTN|LOYALTY|negative=17",
        "ATTN|WC|turns=0",
        "ATTN|ERA|index=3",
        "ATTN|POP|total=23",
        "ATTN|GP|available=1",
        "ATTN|TRADE|idle=1",
        "ATTN|DIPLO|pending=1",
        "ATTN|BLOCKERS|types=NOTIFICATION_PRODUCTION,ENDTURN_BLOCKING_UNIT_PROMOTION",
        "ATTN|NOTIFY|type=NOTIFICATION_REBELLION|msg=Rebels near Pusan",
    ]
    scan = parse_attention_scan(lines)
    assert scan.hostile_count == 2 and "Horseman" in scan.nearest_hostile
    assert scan.damaged_city_ids == (17, 42) and scan.at_war_with == (3,)
    # promotion blocker filtered by BLOCKER_IGNORE
    assert scan.blocker_types == ("NOTIFICATION_PRODUCTION",)
    assert scan.notifications == (("NOTIFICATION_REBELLION", "Rebels near Pusan"),)

def test_parse_failed_family_flagged():
    scan = parse_attention_scan([*QUIET_LINES[:4], "ATTN_ERR|WC", *QUIET_LINES[5:]])
    assert "WC" in scan.failed_families

def test_parse_missing_family_flagged():
    scan = parse_attention_scan([l for l in QUIET_LINES if "ATTN|ERA" not in l])
    assert "ERA" in scan.failed_families

def test_parse_no_attn_lines_none():
    assert parse_attention_scan([]) is None
    assert parse_attention_scan(None) is None
    assert parse_attention_scan(["GARBAGE"]) is None

def test_build_query_int_casts():
    lua = build_attention_query("7", "4")  # str inputs must not splice raw
    assert "__PID__" not in lua and "__RADIUS__" not in lua
    assert " 7" in lua or "[7]" in lua or "(7)" in lua

def test_scan_scalars_shape():
    scan = parse_attention_scan(QUIET_LINES)
    assert scan_scalars(scan) == {"at_war_with": [], "era_index": 1, "total_population": 12}
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement.** Parser + dataclass in `attention.py`:

```python
_SCAN_FAMILIES = (
    "THREAT", "CITYHP", "WAR", "LOYALTY", "WC", "ERA",
    "POP", "GP", "TRADE", "DIPLO", "BLOCKERS",
)
BLOCKER_IGNORE = frozenset({"ENDTURN_BLOCKING_UNIT_PROMOTION"})
NOTIFICATION_WAKE_LIST = frozenset({
    "NOTIFICATION_CITY_UNDER_ATTACK",
    "NOTIFICATION_CITY_LOW_LOYALTY",
    "NOTIFICATION_REBELLION",
    "NOTIFICATION_SPY_CAUGHT",
})


@dataclass(frozen=True)
class AttentionScan:
    hostile_count: int = 0
    nearest_hostile: str = ""
    damaged_city_ids: tuple[int, ...] = ()
    at_war_with: tuple[int, ...] = ()
    negative_loyalty_city_ids: tuple[int, ...] = ()
    wc_turns_until_next: int = -1
    era_index: int = -1
    total_population: int = 0
    great_person_available: bool = False
    trade_route_idle: bool = False
    pending_diplomacy: bool = False
    blocker_types: tuple[str, ...] = ()
    notifications: tuple[tuple[str, str], ...] = ()
    failed_families: tuple[str, ...] = ()


def _ids(value: str) -> tuple[int, ...]:
    out = []
    for part in value.split(","):
        part = part.strip()
        if part:
            try:
                out.append(int(part))
            except ValueError:
                continue
    return tuple(out)


def parse_attention_scan(lines: "list[str] | None") -> AttentionScan | None:
    if not lines:
        return None
    fields: dict = {}
    seen: set[str] = set()
    failed: list[str] = []
    notifications: list[tuple[str, str]] = []
    for line in lines:
        if line.startswith("ATTN_ERR|"):
            failed.append(line.split("|", 1)[1].strip())
            continue
        if not line.startswith("ATTN|"):
            continue
        parts = line.split("|")
        family = parts[1] if len(parts) > 1 else ""
        kv = {}
        for part in parts[2:]:
            key, sep, val = part.partition("=")
            if sep:
                kv[key] = val
        try:
            if family == "THREAT":
                fields["hostile_count"] = int(kv.get("count", "0"))
                fields["nearest_hostile"] = kv.get("nearest", "")
            elif family == "CITYHP":
                fields["damaged_city_ids"] = _ids(kv.get("damaged", ""))
            elif family == "WAR":
                fields["at_war_with"] = _ids(kv.get("with", ""))
            elif family == "LOYALTY":
                fields["negative_loyalty_city_ids"] = _ids(kv.get("negative", ""))
            elif family == "WC":
                fields["wc_turns_until_next"] = int(kv.get("turns", "-1"))
            elif family == "ERA":
                fields["era_index"] = int(kv.get("index", "-1"))
            elif family == "POP":
                fields["total_population"] = int(kv.get("total", "0"))
            elif family == "GP":
                fields["great_person_available"] = kv.get("available", "0") == "1"
            elif family == "TRADE":
                fields["trade_route_idle"] = kv.get("idle", "0") == "1"
            elif family == "DIPLO":
                fields["pending_diplomacy"] = kv.get("pending", "0") == "1"
            elif family == "BLOCKERS":
                types = tuple(
                    t for t in kv.get("types", "").split(",")
                    if t and t not in BLOCKER_IGNORE
                )
                fields["blocker_types"] = types
            elif family == "NOTIFY":
                notifications.append((kv.get("type", ""), kv.get("msg", "")))
                continue  # repeated family; not part of `seen` accounting
            else:
                continue
        except ValueError:
            failed.append(family)
            continue
        seen.add(family)
    if not seen and not failed:
        return None
    for family in _SCAN_FAMILIES:
        if family not in seen and family not in failed:
            failed.append(family)  # a missing family narrows attention -> treat as failed
    return AttentionScan(
        notifications=tuple(notifications), failed_families=tuple(failed), **fields
    )


def scan_scalars(scan: AttentionScan) -> dict:
    return {
        "at_war_with": list(scan.at_war_with),
        "era_index": scan.era_index,
        "total_population": scan.total_population,
    }
```

`build_attention_query`: one Lua string, every family in its own `pcall` that prints `ATTN_ERR|<FAMILY>` on failure. Use `SENTINEL` from `civ_mcp.lua._helpers` (the existing builders' pattern). Idioms to copy, with sources:

- **THREAT**: my asset plots = my city plots + my civilian-unit plots (`p:GetCities():Members()`, `p:GetUnits():Members()` + `GameInfo.Units[u:GetType()]` — overview.py:37–46; civilian = `entry.FormationClass == "FORMATION_CLASS_CIVILIAN"`). Loop every other alive player; hostile = barbarian (`Players[i]:IsBarbarian()`) or `pDiplo:IsAtWarWith(i)` (diplomacy.py:43). For each hostile unit: visible = `PlayersVisibility[me]:IsVisible(plotIdx)` (map.py:151–161, plotIdx from `Map.GetPlot(x, y):GetIndex()`); distance = `Map.GetPlotDistance(ux, uy, ax, ay)` ≤ radius. Track count + nearest (unit name via `Locale.Lookup(entry.Name)`, distance, nearest asset label). Sanitize `nearest` with `:gsub("|", "/")` (notifications.py:33 idiom).
- **CITYHP**: per city, districts damage — cities.py:54–57 (`d:GetMaxDamage(DefenseTypes.DISTRICT_GARRISON)`, `d:GetDamage(...)`, OUTER likewise); damaged = any damage > 0; emit city IDs (`c:GetID()`).
- **WAR**: `pDiplo:IsAtWarWith(i)` over alive major players (diplomacy.py:43 loop shape).
- **LOYALTY**: `c:GetCulturalIdentity()` + pcall'd `GetLoyaltyPerTurn()` < 0 (cities.py:856–860 — already the degrade-tolerant form).
- **WC**: copy the turns-until-next read from `src/civ_mcp/lua/congress.py` (`build_world_congress_query` — same accessor, emit just the turns int; −1 if nil).
- **ERA**: `Game.GetEras():GetCurrentEra()` (tech.py:93).
- **POP**: sum `c:GetPopulation()` (overview.py:35).
- **GP**: loop `GameInfo.GreatPersonClasses` candidates and `Game.GetGreatPeople():CanRecruitPerson(me, individual)` — copy the candidate loop from great_people.py:147–156; emit `available=1` if any true.
- **TRADE**: idle route capacity — copy the capacity-vs-outgoing computation from `src/civ_mcp/lua/economy.py` (`GetOutgoingRoutes()` at economy.py:32/84 + the capacity read in the same builder); emit `idle=1` when capacity > active routes AND at least one trader unit exists.
- **DIPLO**: open-session check involving `me` — copy the session-iteration idiom from `build_close_orphan_sessions` in the same Lua package (grep `DiplomacyManager` for it); emit `pending=1` if any session involves `me`.
- **BLOCKERS**: the `NotificationManager.GetList(me)` + `entry:GetEndTurnBlocking()` + `EndTurnBlockingTypes` reverse-lookup loop, verbatim from notifications.py:20–39, but emit one comma-joined `types=` line instead of per-type lines.
- **NOTIFY**: same list iteration; for non-dismissed entries emit `type=` (reverse-lookup `entry:GetType()` name if available, else "UNKNOWN") + `msg=(entry:GetMessage() or ""):gsub("|", "/")`, capped at 10 lines.

Skeleton:

```python
from civ_mcp.lua._helpers import SENTINEL

_ATTENTION_LUA = """
local me = __PID__
local radius = __RADIUS__
local p = Players[me]
local pDiplo = p:GetDiplomacy()
local function fam(name, fn)
    local ok = pcall(fn)
    if not ok then print("ATTN_ERR|" .. name) end
end
fam("ERA", function()
    print("ATTN|ERA|index=" .. tostring(Game.GetEras():GetCurrentEra()))
end)
fam("WAR", function()
    local ids = {}
    for _, other in ipairs(PlayerManager.GetAliveMajors()) do
        local i = other:GetID()
        if i ~= me and pDiplo:IsAtWarWith(i) then ids[#ids + 1] = tostring(i) end
    end
    print("ATTN|WAR|with=" .. table.concat(ids, ","))
end)
fam("POP", function()
    local total = 0
    for _, c in p:GetCities():Members() do total = total + c:GetPopulation() end
    print("ATTN|POP|total=" .. tostring(total))
end)
fam("THREAT", function() --[[ full body per THREAT idiom bullet above ]] end)
fam("CITYHP", function() --[[ full body per CITYHP idiom bullet above ]] end)
fam("LOYALTY", function() --[[ full body per LOYALTY idiom bullet above ]] end)
fam("WC", function() --[[ full body per WC idiom bullet above ]] end)
fam("GP", function() --[[ full body per GP idiom bullet above ]] end)
fam("TRADE", function() --[[ full body per TRADE idiom bullet above ]] end)
fam("DIPLO", function() --[[ full body per DIPLO idiom bullet above ]] end)
fam("BLOCKERS", function() --[[ full body per BLOCKERS idiom bullet above ]] end)
fam("NOTIFY", function() --[[ full body per NOTIFY idiom bullet above ]] end)
print("{SENTINEL}")
""".replace("{SENTINEL}", SENTINEL)


def build_attention_query(player_id: int, threat_radius: int) -> str:
    return _ATTENTION_LUA.replace("__PID__", str(int(player_id))).replace(
        "__RADIUS__", str(int(threat_radius))
    )
```

ERA/WAR/POP above are complete — they show the full `fam()` pattern (pcall guard, one `ATTN|` line, comma-joined ids). Each remaining `--[[ ... ]]` body is written out fully by the implementer from its per-family idiom bullet above — every bullet names the exact source file:line to copy the accessor from (e.g. CITYHP ← cities.py:54–57, LOYALTY ← cities.py:856–860, BLOCKERS ← notifications.py:20–39). If `PlayerManager.GetAliveMajors()` is unavailable in this context, fall back to the alive-player loop shape in diplomacy.py:43. The Lua is validated live by probe 1 of the live-probe checklist (Task 12), and each family individually fails open via `ATTN_ERR` — an API-name miss degrades to a wake, never a crash or a blind sleep.

- [ ] **Step 4: Run tests** — parser tests + `test_build_query_int_casts` PASS.
- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/attention.py tests/arena/test_attention.py
git commit -m "feat(arena): attention trigger scan (batched read-only Lua build/parse)"
```

---

### Task 8: Decision function + skip decision matrix

**Files:**
- Modify: `src/civ_mcp/arena/attention.py`
- Test: `tests/arena/test_attention.py`

**Interfaces:**
- Consumes: `AttentionState` (Task 6), `AttentionScan` (Task 7).
- Produces (Task 10 consumes):
  - `Decision(action: str, wake_cause: str | None, wake_detail: str, hard: tuple[str, ...], soft: tuple[str, ...])` — `action` in `("sleep", "wake")`
  - `evaluate(mode: str, state: AttentionState, scan: AttentionScan | None, snapshot: dict | None, *, max_streak: int, task_event: bool) -> Decision`
  - Wake-cause vocabulary (analyze + digest use these exact strings): `SCAN_ERROR`, `SCAN_PARTIAL`, `NO_BASELINE`, `UNITS_LOST`, `CITY_COUNT_CHANGED`, `GOLD_CRASH`, `BLOCKER_<TYPE>`, `ENEMY_NEAR`, `CITY_DAMAGED`, `WAR_PEACE_CHANGED`, `LOYALTY_NEGATIVE`, `WC_SESSION`, `ERA_CHANGED`, `NOTIFICATION_WAKE`, `TASK_EVENT`, `STREAK_CAP`, `NO_DIRECTIVE`, plus the four soft-trigger names.

Decision logic (spec §1/§2, exact):

1. `scan is None or snapshot is None` → wake `SCAN_ERROR`.
2. `scan.failed_families` → wake `SCAN_PARTIAL` (detail = joined family names).
3. `state.last_snapshot is None or state.last_scan is None` → wake `NO_BASELINE` (first captured turn).
4. Hard triggers, in priority order (first match is `wake_cause`; ALL matches go in `hard`):
   `task_event` → `TASK_EVENT`; snapshot `units` < baseline → `UNITS_LOST`; `cities` ≠ baseline → `CITY_COUNT_CHANGED`; gold falling and `gold + 5*(gold - prev_gold) < 0` → `GOLD_CRASH`; `scan.blocker_types` → `BLOCKER_<first type>`; `hostile_count > 0` → `ENEMY_NEAR` (detail = `nearest_hostile`); `damaged_city_ids` → `CITY_DAMAGED`; `set(at_war_with) != set(last_scan["at_war_with"])` → `WAR_PEACE_CHANGED`; `negative_loyalty_city_ids` → `LOYALTY_NEGATIVE`; `wc_turns_until_next == 0` → `WC_SESSION`; `era_index != last_scan["era_index"]` (both ≥ 0) → `ERA_CHANGED`; any notification type in `NOTIFICATION_WAKE_LIST` → `NOTIFICATION_WAKE`; `pending_diplomacy` → `BLOCKER_DIPLOMACY_SESSION`.
5. `state.streak >= max_streak` → wake `STREAK_CAP`.
6. Soft triggers — only when `mode in ("model", "hybrid")` and `state.skips_remaining > 0` and the token is subscribed in `state.directive["wake_if"]`: `GREAT_PERSON_AVAILABLE` ← `scan.great_person_available`; `CITY_GREW` ← `scan.total_population > last_scan["total_population"]`; `TRADE_ROUTE_IDLE` ← `scan.trade_route_idle`; `GOLD_STOCKPILE_HIGH` ← `snapshot["gold"] >= GOLD_STOCKPILE_THRESHOLD` (add `GOLD_STOCKPILE_THRESHOLD = 500`). First match wakes.
7. Otherwise by mode: `auto` → sleep; `model` → sleep if `skips_remaining > 0` else wake `NO_DIRECTIVE`; `hybrid` → sleep. Unknown mode → wake `SCAN_ERROR` (defensive; coordinator never calls with `off`).

- [ ] **Step 1: Write the failing tests** — append to `tests/arena/test_attention.py`. Core artifact = table-driven matrix:

```python
from civ_mcp.arena.attention import Decision, evaluate

QUIET = parse_attention_scan(QUIET_LINES)
SNAP = {"score": 100, "gold": 200, "units": 4, "cities": 2}


def _st(**kw):
    base = dict(run_id="r", player_id=1, last_snapshot=dict(SNAP),
                last_scan={"at_war_with": [], "era_index": 1, "total_population": 12})
    base.update(kw)
    return AttentionState(**base)


# (mode, state kwargs, scan, snapshot, task_event) -> (action, wake_cause)
MATRIX = [
    # quiet world, no directive
    ("auto",   {},                                        QUIET, SNAP, False, "sleep", None),
    ("model",  {},                                        QUIET, SNAP, False, "wake", "NO_DIRECTIVE"),
    ("hybrid", {},                                        QUIET, SNAP, False, "sleep", None),
    # quiet world, active directive
    ("model",  {"skips_remaining": 2, "directive": {"skip": 3, "wake_if": []}},
                                                          QUIET, SNAP, False, "sleep", None),
    # streak cap beats everything quiet
    ("auto",   {"streak": 5},                             QUIET, SNAP, False, "wake", "STREAK_CAP"),
    # scan/baseline failures
    ("auto",   {},                                        None,  SNAP, False, "wake", "SCAN_ERROR"),
    ("auto",   {"last_snapshot": None, "last_scan": None}, QUIET, SNAP, False, "wake", "NO_BASELINE"),
    # task event is a hard wake in every mode (external-review finding 6)
    ("auto",   {},                                        QUIET, SNAP, True,  "wake", "TASK_EVENT"),
    ("hybrid", {"skips_remaining": 3, "directive": {"skip": 3, "wake_if": []}},
                                                          QUIET, SNAP, True,  "wake", "TASK_EVENT"),
]


@pytest.mark.parametrize("mode,st_kw,scan,snap,task_event,action,cause", MATRIX)
def test_skip_decision_matrix(mode, st_kw, scan, snap, task_event, action, cause):
    d = evaluate(mode, _st(**st_kw), scan, snap, max_streak=5, task_event=task_event)
    assert (d.action, d.wake_cause) == (action, cause)


def test_hard_triggers_fire():
    busy = parse_attention_scan([
        "ATTN|THREAT|count=1|nearest=Warrior d2 near Suwon", *QUIET_LINES[1:],
    ])
    d = evaluate("auto", _st(), busy, SNAP, max_streak=5, task_event=False)
    assert d.action == "wake" and d.wake_cause == "ENEMY_NEAR"
    assert "Suwon" in d.wake_detail

def test_units_lost_delta_wakes():
    d = evaluate("auto", _st(), QUIET, {**SNAP, "units": 3}, max_streak=5, task_event=False)
    assert d.wake_cause == "UNITS_LOST"

def test_gold_crash_projection():
    st = _st(last_snapshot={**SNAP, "gold": 100})
    d = evaluate("auto", st, QUIET, {**SNAP, "gold": 60}, max_streak=5, task_event=False)
    assert d.wake_cause == "GOLD_CRASH"  # 60 + 5*(-40) < 0

def test_scan_partial_wakes():
    partial = parse_attention_scan([*QUIET_LINES[1:], "ATTN_ERR|THREAT"])
    d = evaluate("auto", _st(), partial, SNAP, max_streak=5, task_event=False)
    assert d.wake_cause == "SCAN_PARTIAL"

def test_soft_trigger_requires_subscription():
    grown = parse_attention_scan([l.replace("total=12", "total=13") for l in QUIET_LINES])
    st_sub = _st(skips_remaining=2, directive={"skip": 3, "wake_if": ["CITY_GREW"]})
    st_nosub = _st(skips_remaining=2, directive={"skip": 3, "wake_if": []})
    assert evaluate("hybrid", st_sub, grown, SNAP, max_streak=5, task_event=False).wake_cause == "CITY_GREW"
    assert evaluate("hybrid", st_nosub, grown, SNAP, max_streak=5, task_event=False).action == "sleep"

def test_soft_triggers_ignored_in_auto():
    grown = parse_attention_scan([l.replace("total=12", "total=13") for l in QUIET_LINES])
    st = _st(skips_remaining=2, directive={"skip": 3, "wake_if": ["CITY_GREW"]})
    assert evaluate("auto", st, grown, SNAP, max_streak=5, task_event=False).action == "sleep"

def test_blocker_wakes_with_type_name():
    blocked = parse_attention_scan([
        *QUIET_LINES[:10], "ATTN|BLOCKERS|types=NOTIFICATION_PRODUCTION",
    ])
    d = evaluate("auto", _st(), blocked, SNAP, max_streak=5, task_event=False)
    assert d.wake_cause == "BLOCKER_NOTIFICATION_PRODUCTION"

def test_notification_wake_list():
    noisy = parse_attention_scan([
        *QUIET_LINES, "ATTN|NOTIFY|type=NOTIFICATION_REBELLION|msg=Rebels!",
    ])
    d = evaluate("auto", _st(), noisy, SNAP, max_streak=5, task_event=False)
    assert d.wake_cause == "NOTIFICATION_WAKE"
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** `Decision` + `evaluate` in `attention.py` following the numbered logic above exactly:

```python
GOLD_STOCKPILE_THRESHOLD = 500


@dataclass(frozen=True)
class Decision:
    action: str                    # "sleep" | "wake"
    wake_cause: str | None = None
    wake_detail: str = ""
    hard: tuple[str, ...] = ()
    soft: tuple[str, ...] = ()


def _hard_triggers(
    state: AttentionState, scan: AttentionScan, snapshot: dict, task_event: bool
) -> "tuple[list[str], str]":
    prev = state.last_snapshot or {}
    prev_scan = state.last_scan or {}
    hard: list[str] = []
    detail = ""
    if task_event:
        hard.append("TASK_EVENT")
    if snapshot.get("units", 0) < prev.get("units", 0):
        hard.append("UNITS_LOST")
    if snapshot.get("cities", 0) != prev.get("cities", 0):
        hard.append("CITY_COUNT_CHANGED")
    gold, prev_gold = snapshot.get("gold"), prev.get("gold")
    if (
        isinstance(gold, (int, float)) and isinstance(prev_gold, (int, float))
        and gold < prev_gold and gold + 5 * (gold - prev_gold) < 0
    ):
        hard.append("GOLD_CRASH")
    if scan.blocker_types:
        hard.append(f"BLOCKER_{scan.blocker_types[0]}")
    if scan.hostile_count > 0:
        hard.append("ENEMY_NEAR")
        detail = detail or scan.nearest_hostile
    if scan.damaged_city_ids:
        hard.append("CITY_DAMAGED")
    if set(scan.at_war_with) != set(prev_scan.get("at_war_with", [])):
        hard.append("WAR_PEACE_CHANGED")
    if scan.negative_loyalty_city_ids:
        hard.append("LOYALTY_NEGATIVE")
    if scan.wc_turns_until_next == 0:
        hard.append("WC_SESSION")
    prev_era = prev_scan.get("era_index", -1)
    if scan.era_index >= 0 and prev_era >= 0 and scan.era_index != prev_era:
        hard.append("ERA_CHANGED")
    if any(ntype in NOTIFICATION_WAKE_LIST for ntype, _ in scan.notifications):
        hard.append("NOTIFICATION_WAKE")
    if scan.pending_diplomacy:
        hard.append("BLOCKER_DIPLOMACY_SESSION")
    return hard, detail


def evaluate(
    mode: str, state: AttentionState, scan: AttentionScan | None,
    snapshot: dict | None, *, max_streak: int, task_event: bool,
) -> Decision:
    if scan is None or snapshot is None:
        return Decision("wake", "SCAN_ERROR")
    if scan.failed_families:
        return Decision("wake", "SCAN_PARTIAL", ",".join(scan.failed_families))
    if state.last_snapshot is None or state.last_scan is None:
        return Decision("wake", "NO_BASELINE")
    hard, detail = _hard_triggers(state, scan, snapshot, task_event)
    if hard:
        return Decision("wake", hard[0], detail, hard=tuple(hard))
    if state.streak >= max_streak:
        return Decision("wake", "STREAK_CAP")
    directive_active = state.skips_remaining > 0
    if mode in ("model", "hybrid") and directive_active:
        subscribed = tuple((state.directive or {}).get("wake_if", []))
        soft: list[str] = []
        if "GREAT_PERSON_AVAILABLE" in subscribed and scan.great_person_available:
            soft.append("GREAT_PERSON_AVAILABLE")
        if (
            "CITY_GREW" in subscribed
            and scan.total_population > state.last_scan.get("total_population", 0)
        ):
            soft.append("CITY_GREW")
        if "TRADE_ROUTE_IDLE" in subscribed and scan.trade_route_idle:
            soft.append("TRADE_ROUTE_IDLE")
        if (
            "GOLD_STOCKPILE_HIGH" in subscribed
            and snapshot.get("gold", 0) >= GOLD_STOCKPILE_THRESHOLD
        ):
            soft.append("GOLD_STOCKPILE_HIGH")
        if soft:
            return Decision("wake", soft[0], soft=tuple(soft))
    if mode == "auto":
        return Decision("sleep")
    if mode == "model":
        return Decision("sleep") if directive_active else Decision("wake", "NO_DIRECTIVE")
    if mode == "hybrid":
        return Decision("sleep")
    return Decision("wake", "SCAN_ERROR")  # unknown mode: defensive fail-open
```

- [ ] **Step 4: Run tests** — full matrix PASS.
- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/attention.py tests/arena/test_attention.py
git commit -m "feat(arena): attention decision function + skip decision matrix"
```

---

### Task 9: Prompt wiring (instruction + digest block, both drivers)

**Files:**
- Modify: `src/civ_mcp/arena/prompting.py`
- Modify: `src/civ_mcp/arena/agent.py` (`__call__` signature line 88, opening-prompt build lines 135–148)
- Modify: `src/civ_mcp/arena/cli_agent.py` (`__call__` signature line 481, opening build lines 491–518)
- Test: `tests/arena/test_prompting.py`, `tests/arena/test_agent.py`, `tests/arena/test_cli_agent.py`

**Interfaces:**
- Consumes: `CivOptions.attention_directives_enabled` (Task 1), `SOFT_TRIGGERS` (Task 4).
- Produces (Task 10 relies on): both policies accept `digest_block: str = ""` keyword (the coordinator injects it signature-gated, like `memory_block`); `build_opening_prompt(..., digest_block: str = "", include_attention_instruction: bool = False)`; ordering: briefing, memory_block, task_block, digest_block, announcement, STANDING_PLAN_INSTRUCTION, ATTENTION_INSTRUCTION; `prompt_injections` gains `"digest": bool(digest_block)` and `"attention_instruction": <flag>`.

- [ ] **Step 1: Write the failing tests** — append to `tests/arena/test_prompting.py`:

```python
from civ_mcp.arena.prompting import ATTENTION_INSTRUCTION, build_opening_prompt

def test_digest_block_ordered_after_task_block():
    out = build_opening_prompt(
        player_id=1, turn=5, briefing_text="B", memory_block="M",
        task_block="T", digest_block="== WHILE YOU SLEPT ==",
    )
    assert out.index("T") < out.index("WHILE YOU SLEPT") < out.index("It is turn 5")

def test_attention_instruction_appended_when_requested():
    out = build_opening_prompt(player_id=1, turn=5, include_attention_instruction=True)
    assert out.endswith(ATTENTION_INSTRUCTION)
    assert "SKIP:" in ATTENTION_INSTRUCTION and "WAKE IF:" in ATTENTION_INSTRUCTION

def test_attention_instruction_lists_exact_soft_enum():
    from civ_mcp.arena.attention import SOFT_TRIGGERS
    for token in SOFT_TRIGGERS:
        assert token in ATTENTION_INSTRUCTION

def test_attention_independent_of_standing_plan():
    out = build_opening_prompt(
        player_id=1, turn=5,
        include_standing_plan_instruction=False, include_attention_instruction=True,
    )
    assert "STANDING PLAN" not in out and "SKIP:" in out
```

To `tests/arena/test_agent.py` and `tests/arena/test_cli_agent.py`, add one test each asserting the policy's `__call__` accepts `digest_block` and that a `model`-mode options object flips the instruction on — follow each file's existing fake-backend / fake-subprocess pattern for constructing the policy; assert on the built prompt (both files already have tests capturing the opening prompt for memory_block; copy the nearest one and swap in `digest_block="DIGEST-MARKER"` + `attention=AttentionOptions(mode="model")`, asserting `"DIGEST-MARKER"` and `"SKIP:"` appear in the captured prompt).

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement.**

`prompting.py` — add after `STANDING_PLAN_INSTRUCTION`:

```python
# Soft-trigger tokens are duplicated here as literal text on purpose: the
# instruction is a prompt, and importing attention.SOFT_TRIGGERS to format it
# would make prompt text drift with code changes invisibly. The prompting test
# asserts the two stay in sync.
ATTENTION_INSTRUCTION = """If nothing will need your judgment for a few turns, you may ALSO end with:
SKIP: <1-5>
WAKE IF: <optional, comma-separated from exactly: GREAT_PERSON_AVAILABLE, CITY_GREW, TRADE_ROUTE_IDLE, GOLD_STOCKPILE_HIGH>
You will be woken early regardless for any threat, blocker, or task event.
Skip during long builds or peacetime consolidation; never skip at war or with unsettled settlers."""
```

Extend `build_opening_prompt`:

```python
def build_opening_prompt(
    *,
    player_id: int,
    turn: int,
    briefing_text: str = "",
    memory_block: str = "",
    task_block: str = "",
    digest_block: str = "",
    include_standing_plan_instruction: bool = False,
    include_attention_instruction: bool = False,
) -> str:
```

with `digest_block` appended after `task_block`, and at the tail:

```python
    if include_standing_plan_instruction:
        parts.append(STANDING_PLAN_INSTRUCTION)
    if include_attention_instruction:
        parts.append(ATTENTION_INSTRUCTION)
    return "\n\n".join(parts)
```

(Note this changes the existing tail from append-after-announcement to the same position — the announcement stays before both instructions; keep existing tests green.)

`agent.py` `__call__`: add `digest_block: str = ""` to the keyword params; compute `include_attention_instruction = self.options.attention_directives_enabled`; pass both new args into `build_opening_prompt`; extend `prompt_injections` with `"digest": bool(digest_block), "attention_instruction": include_attention_instruction`.

`cli_agent.py` `__call__`: identical three changes (params at line ~487, build at ~501, injections at ~516).

- [ ] **Step 4: Run tests** — PASS (including all pre-existing prompting/agent/cli_agent tests).
- [ ] **Step 5: Full suite + commit**

```bash
git add src/civ_mcp/arena/prompting.py src/civ_mcp/arena/agent.py src/civ_mcp/arena/cli_agent.py tests/arena/test_prompting.py tests/arena/test_agent.py tests/arena/test_cli_agent.py
git commit -m "feat(arena): attention instruction + wake-digest block in both drivers"
```

---

### Task 10: Coordinator integration

**Files:**
- Modify: `src/civ_mcp/arena/coordinator.py` (imports; snapshot gate line 169; insertion after the task-tracker block ending line 233; capture gate line 333; record build 419–437; loop/budget lines 158–161, 317–318, 447–448, 470–471)
- Test: `tests/arena/test_coordinator.py`

**Interfaces:**
- Consumes: everything from Tasks 1, 4, 6, 7, 8, 9.
- Produces: slept turns (no policy call) with exact transcript schema (spec §5); `run_arena` returns `{"puppet_turns_played": played, "turns_slept": slept, "log": log}`; played records gain `"turn_kind": "played"` + `"attention"` object; `max_game_turns` enforced.

Implementation outline (exact anchors):

1. **Imports:**

```python
from civ_mcp.arena.attention import (
    build_attention_query,
    evaluate,
    has_directive_lines,
    load_attention_state,
    note_sleep,
    note_wake,
    parse_attention_scan,
    parse_directive,
    render_digest,
    save_attention_state,
    scan_scalars,
)
```

2. **Counters** (line ~154): `played, slept, game_turns, log = 0, 0, 0, []`. Loop condition (line 161):

```python
        max_game_turns = getattr(config, "max_game_turns", 0)  # tolerate old test-stub configs
        while (
            remaining > 0 and deadline_polls > 0
            and (max_game_turns <= 0 or game_turns < max_game_turns)
        ):
```

3. **Snapshot gate** (line 169): 

```python
                attention_mode = opts.attention.mode
                attention_on = attention_mode in ("auto", "model", "hybrid")
                state_before = (
                    await _overview_snapshot(gs) if (_tx_on or attention_on) else None
                )
```

4. **Skip evaluation** — insert AFTER the task-tracker block (after line 233), BEFORE the `policy_kwargs` build. Note `state_before` here is "this turn's snapshot"; the slept record's `state_before` is the PREVIOUS turn's (from state):

```python
                att_state = None
                att_scan = None
                digest_block = ""
                decision = None
                if attention_on:
                    try:
                        att_state = load_attention_state(transcript_dir, run_id, st.local)
                        scan_lines = await conn.execute_read(
                            build_attention_query(st.local, opts.attention.threat_radius)
                        )
                        att_scan = parse_attention_scan(scan_lines)
                    except Exception as e:
                        att_scan = None
                        print(f"[arena] attention scan failed; waking: {e!r}", file=sys.stderr)
                    if att_state is None:
                        att_state = load_attention_state(transcript_dir, run_id, st.local)
                    task_event = any(
                        r.get("status") not in (None, "active") for r in task_results
                    )
                    decision = evaluate(
                        attention_mode, att_state, att_scan, state_before,
                        max_streak=opts.attention.max_streak, task_event=task_event,
                    )
                if decision is not None and decision.action == "sleep":
                    prev_snapshot = att_state.last_snapshot
                    task_notes = [
                        f"{r.get('kind', '?')} {r.get('action', '')}: {r.get('result', '')}"
                        for r in task_results
                    ]
                    att_state = note_sleep(
                        att_state, turn=st.turn, snapshot=state_before,
                        scan_scalars=scan_scalars(att_scan),
                        task_notes=task_notes, notifications=list(att_scan.notifications),
                    )
                    try:
                        save_attention_state(transcript_dir, run_id, st.local, att_state)
                    except Exception as e:
                        print(f"[arena] attention state save failed: {e!r}", file=sys.stderr)
                    attention_fields = {
                        "mode": attention_mode, "decision": "slept",
                        "directive": att_state.directive,
                        "skips_remaining": att_state.skips_remaining,
                        "streak": att_state.streak, "wake_cause": None,
                    }
                    log.append({
                        "player": st.local, "turn": st.turn,
                        "slept": True, "attention": attention_fields,
                    })
                    if _tx_on:
                        _num = ("score", "gold", "science", "culture", "faith", "cities", "units")
                        if prev_snapshot is not None and state_before is not None:
                            state_delta = {
                                k: state_before[k] - prev_snapshot[k] for k in _num
                            }
                            state_delta["research"] = state_before["research"]
                            state_delta["civic"] = state_before["civic"]
                        else:
                            state_delta = None
                        _pol_backend = getattr(pol, "backend", None)
                        transcript.write({
                            "schema_version": 1,
                            "run_id": run_id,
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "player_id": st.local,
                            "turn": st.turn,
                            "provider": getattr(pol, "provider", "local"),
                            "model": getattr(_pol_backend, "model", getattr(pol, "model", "")),
                            "driver": "cli" if str(getattr(pol, "provider", "local")).startswith("cli") else "in_process",
                            "turn_kind": "slept",
                            "slept": True,
                            "step_count": 0,
                            "usd": 0.0,
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                            "state_before": prev_snapshot,
                            "state_after": state_before,
                            "state_delta": state_delta,
                            "standing_memory": {
                                "loaded": bool(memory), "injected": False,
                                "injected_chars": 0, "captured_chars": 0,
                                "error": memory_error,
                            },
                            "task_tracker": {
                                "active_before": len(active_tasks_before),
                                "pre_model_results": task_results,
                                "active_after": len(active_tasks_after),
                                "error": task_tracker_error,
                            },
                            "attention": attention_fields,
                        })
                    await hook.finish_units(conn, st.local)
                    await hook.restore_local(conn, 0)
                    slept += 1
                    game_turns += 1
                    deadline_polls -= 1
                    continue
                if decision is not None and att_state.slept:
                    digest_block = render_digest(
                        att_state, wake_turn=st.turn,
                        wake_cause=decision.wake_cause or "",
                        wake_detail=decision.wake_detail,
                    )
```

5. **Inject digest** — extend the signature-gated kwarg tuple (line ~240):

```python
                    for name, value in (
                        ("memory_block", memory_block),
                        ("task_block", task_block),
                        ("digest_block", digest_block),
                    )
```

6. **Capture gate** (line 333) — widen:

```python
                if opts.standing_plan_enabled or opts.attention_directives_enabled:
                    final_summary = (
                        result.get("transcript", {}).get("final_summary")
                        or result.get("summary", "")
                    )
                if opts.standing_plan_enabled:
                    captured_plan = extract_standing_plan(
                        final_summary, opts.standing_plan_capture_chars
                    )
```

7. **Post-turn attention update** (after the task-capture block, before `state_after = ...`):

```python
                directive = None
                directive_ack = ""
                wake_attention_fields = None
                if attention_on and att_state is not None:
                    if opts.attention_directives_enabled:
                        directive = parse_directive(final_summary, opts.attention.max_skip)
                        if directive is not None:
                            note = " (clamped)" if directive.clamped else ""
                            directive_ack = f"SKIP {directive.skip} accepted{note}"
                            if directive.unknown_tokens:
                                directive_ack += (
                                    f"; unknown tokens dropped: {','.join(directive.unknown_tokens)}"
                                )
                        elif has_directive_lines(final_summary):
                            directive_ack = "directive not recognized"
                    wake_cause = decision.wake_cause if decision is not None else None
                    wake_attention_fields = {
                        "mode": attention_mode, "decision": "woke",
                        "wake_cause": wake_cause,
                        "directive": (
                            {"skip": directive.skip, "wake_if": list(directive.wake_if)}
                            if directive else None
                        ),
                        "digest_chars": len(digest_block),
                        "directive_ack": directive_ack,
                    }
                    att_state = note_wake(
                        att_state, turn=st.turn,
                        wake_cause=wake_cause or "", directive=directive,
                        directive_ack=directive_ack,
                        snapshot=state_after if state_after is not None else state_before,
                        scan_scalars=scan_scalars(att_scan) if att_scan is not None else None,
                    )
                    try:
                        save_attention_state(transcript_dir, run_id, st.local, att_state)
                    except Exception as e:
                        print(f"[arena] attention state save failed: {e!r}", file=sys.stderr)
```

Placement: insert this block immediately AFTER the `state_after = await _overview_snapshot(gs) if _tx_on else None` line (coordinator.py:371), so the wake baseline uses the freshest post-turn snapshot when transcripts are on and falls back to `state_before` otherwise (the `state_after if state_after is not None else state_before` expression above).

8. **Played record additions** (record dict at 419): add `"turn_kind": "played"` and, when `wake_attention_fields is not None`, `"attention": wake_attention_fields`.

9. **Budget/counters:** played path `played += 1; remaining -= 1; game_turns += 1`. Failed path (line 317): add `game_turns += 1`. Return (line 471): `{"puppet_turns_played": played, "turns_slept": slept, "log": log}`.

- [ ] **Step 1: Write the failing tests** — append to `tests/arena/test_coordinator.py`, building on the file's existing fixtures (`FakeConnWithOverview`, `FakeGSWithConn`, `FakeSink`, `TranscriptPolicy`). Add a canned ATTN responder + a counting policy:

```python
QUIET_SCAN_LINES = [
    "ATTN|THREAT|count=0|nearest=", "ATTN|CITYHP|damaged=", "ATTN|WAR|with=",
    "ATTN|LOYALTY|negative=", "ATTN|WC|turns=5", "ATTN|ERA|index=1",
    "ATTN|POP|total=12", "ATTN|GP|available=0", "ATTN|TRADE|idle=0",
    "ATTN|DIPLO|pending=0", "ATTN|BLOCKERS|types=",
]

class AttnConn(FakeConnWithOverview):
    async def execute_read(self, lua, timeout=5.0):
        if "ATTN" in lua:
            return list(QUIET_SCAN_LINES)
        return await super().execute_read(lua, timeout)

class CountingPolicy:
    def __init__(self, options):
        self.options = options
        self.calls = 0
    async def __call__(self, gs, player_id, turn, *, digest_block="", **kw):
        self.calls += 1
        self.last_digest = digest_block
        return {"summary": "played", "actions": [], "transcript": {"steps": [], "final_summary": "played"}}


@pytest.mark.asyncio
async def test_auto_mode_sleeps_quiet_turn(tmp_path):
    from civ_mcp.arena.attention import AttentionState, save_attention_state
    from civ_mcp.arena.config import AttentionOptions
    conn = AttnConn(); gs = FakeGSWithConn(conn); sink = FakeSink()
    opts = CivOptions(attention=AttentionOptions(mode="auto"))
    pol = CountingPolicy(opts)
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m", options=opts)],
                      max_puppet_turns=3, idle_poll_limit=5,
                      transcript_dir=str(tmp_path), run_id="r1", puppet_ids=[1])
    # seed a baseline so NO_BASELINE doesn't force a wake on the first capture
    save_attention_state(str(tmp_path), "r1", 1, AttentionState(
        run_id="r1", player_id=1,
        last_snapshot={"score": 0, "gold": 0, "science": 0, "culture": 0,
                       "faith": 0, "research": "", "civic": "", "cities": 0, "units": 0},
        last_scan={"at_war_with": [], "era_index": 1, "total_population": 12}))
    result = await run_arena(conn, gs, cfg, policy=pol, transcript=sink)
    assert pol.calls == 0                       # no model invocation
    assert result["turns_slept"] == 1
    assert result["puppet_turns_played"] == 0   # max_puppet_turns NOT consumed
    rec = sink.records[-1]
    assert rec["slept"] is True and rec["turn_kind"] == "slept"
    assert rec["step_count"] == 0 and rec["usd"] == 0.0
    assert "skipped" not in rec                 # that key means FAILED
    assert conn.restored                        # handback still happened
```

(Adapt field names for the overview snapshot to whatever `FakeConnWithOverview` serves — read its canned lines in the test file and mirror them; ArenaConfig may not accept `transcript_dir`/`run_id` kwargs positionally — check the dataclass and existing tests for how run_id/transcript_dir are set, and follow that.)

Also add:

```python
@pytest.mark.asyncio
async def test_first_capture_wakes_no_baseline(tmp_path):
    # no seeded state -> NO_BASELINE -> model runs, played record annotated
    ...assert pol.calls == 1
    rec = sink.records[-1]
    assert rec["turn_kind"] == "played" and rec["attention"]["wake_cause"] == "NO_BASELINE"

@pytest.mark.asyncio
async def test_off_mode_bit_for_bit_today(tmp_path):
    # mode="off": no ATTN read issued, no attention/turn_kind... except turn_kind
    # IS added to played records unconditionally -- assert no "attention" key and
    # that no execute_read call contained "ATTN".
    ...

@pytest.mark.asyncio
async def test_wake_digest_injected_after_sleep(tmp_path):
    # 2 captured turns: first sleeps (quiet scan), second wakes via seeded
    # streak >= max_streak=1 -> STREAK_CAP; assert pol.last_digest contains
    # "WHILE YOU SLEPT" and the record attention.decision == "woke".
    ...

@pytest.mark.asyncio
async def test_directive_captured_without_memory(tmp_path):
    # mode="model", memory+tracker disabled; policy returns final_summary
    # "done\nSKIP: 3"; assert attention state file has skips_remaining == 3
    # and no memory file was created.
    ...

@pytest.mark.asyncio
async def test_max_game_turns_caps_run(tmp_path):
    # max_game_turns=1, quiet scans, auto mode, seeded baseline: run ends after
    # 1 slept turn even though max_puppet_turns=5 remains.
    ...
```

Write each `...` out fully following the first test's setup pattern (the plan reviewer gate: no stub tests may remain — each must construct conn/gs/config, run `run_arena`, and assert the listed conditions).

- [ ] **Step 2: Run to verify failure** — `uv run --extra test pytest tests/arena/test_coordinator.py -q`.
- [ ] **Step 3: Implement** per the outline above.
- [ ] **Step 4: Run coordinator tests, then the full suite** — all green, including every pre-existing coordinator test (the off-mode path must be untouched except `turn_kind` on played records).
- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/coordinator.py tests/arena/test_coordinator.py
git commit -m "feat(arena): coordinator skip evaluation + slept-turn fast path"
```

---

### Task 11: Analyze additions

**Files:**
- Modify: `src/civ_mcp/arena/analyze.py`
- Test: `tests/arena/test_analyze.py`

**Interfaces:**
- Consumes: transcript records from Task 10 (`turn_kind`/`slept`/`attention` keys; pre-feature records have none → played).
- Produces: `attention_metrics(records: list[dict]) -> dict` keyed per player: `{captured, model_turns, slept_turns, skip_rate, max_streak, streak_histogram, wake_causes, directive: {issued, not_recognized, clamped, unknown_tokens}, savings: {llm_calls_avoided, est_usd, est_wall_clock_s}, false_quiet: {streaks, false_quiet_streaks, rate}}`; `analyze()` output gains an `"attention"` key; `render_markdown` gains an "## Attention" section (skip it when no records carry attention data).

Rules:
- `_turn_kind(rec)`: `"slept"` if `rec.get("slept") is True` else `"played"` (records with `"skipped"` never reach transcripts — they are log-only).
- `skip_rate = slept / captured`.
- Streaks: group per player by ascending `turn`; a streak = consecutive slept records; its ending wake cause = the `attention.wake_cause` of the first played record after it (or `"RUN_END"`).
- Savings: `est_usd = slept_turns * mean(usd of that player's played records)`; `est_wall_clock_s = slept_turns * mean(wall_clock_s of played records where present)`.
- False-quiet (spec §5): across a streak, sum `state_delta` for `units`, `cities`; track gold sign change from the streak's records. Harm = summed units < 0, summed cities < 0, or gold ended < 0 having started ≥ 0. False-quiet = harm AND ending wake cause not in `{"UNITS_LOST", "CITY_COUNT_CHANGED", "GOLD_CRASH", "ENEMY_NEAR", "CITY_DAMAGED"}`.
- Directive quality from played records' `attention` objects: `issued` = directive not None; `not_recognized` = `directive_ack == "directive not recognized"`; `clamped` = ack contains "(clamped)"; `unknown_tokens` = ack contains "unknown tokens".

- [ ] **Step 1: Write the failing tests** — append to `tests/arena/test_analyze.py`; build synthetic records:

```python
from civ_mcp.arena.analyze import attention_metrics

def _slept(turn, units_delta=0, cities_delta=0):
    return {"player_id": 1, "turn": turn, "turn_kind": "slept", "slept": True,
            "usd": 0.0, "state_delta": {"units": units_delta, "cities": cities_delta,
                                        "gold": 5, "score": 1, "science": 0,
                                        "culture": 0, "faith": 0}}

def _played(turn, wake_cause=None, usd=0.02):
    rec = {"player_id": 1, "turn": turn, "turn_kind": "played", "usd": usd,
           "transcript_noise": True}
    if wake_cause is not None:
        rec["attention"] = {"decision": "woke", "wake_cause": wake_cause,
                            "directive": None, "directive_ack": ""}
    return rec

def test_attention_metrics_counts_and_savings():
    recs = [_played(1), _slept(2), _slept(3), _played(4, "STREAK_CAP")]
    m = attention_metrics(recs)[1]
    assert m["captured"] == 4 and m["slept_turns"] == 2 and m["model_turns"] == 2
    assert m["skip_rate"] == 0.5 and m["max_streak"] == 2
    assert m["wake_causes"] == {"STREAK_CAP": 1}
    assert m["savings"]["llm_calls_avoided"] == 2
    assert abs(m["savings"]["est_usd"] - 0.04) < 1e-9

def test_false_quiet_detected():
    # a unit died mid-streak but the wake was STREAK_CAP -> false quiet
    recs = [_played(1), _slept(2, units_delta=-1), _played(3, "STREAK_CAP")]
    m = attention_metrics(recs)[1]
    assert m["false_quiet"] == {"streaks": 1, "false_quiet_streaks": 1, "rate": 1.0}

def test_true_quiet_not_flagged():
    recs = [_played(1), _slept(2, units_delta=-1), _played(3, "UNITS_LOST")]
    assert attention_metrics(recs)[1]["false_quiet"]["false_quiet_streaks"] == 0

def test_pre_feature_records_read_as_played():
    m = attention_metrics([{"player_id": 1, "turn": 1, "usd": 0.0}])
    assert m[1]["captured"] == 1 and m[1]["slept_turns"] == 0
```

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** `_turn_kind`, `attention_metrics` per the rules; wire an `"attention": attention_metrics(transcript_records)` key into `analyze()`'s report dict and an `## Attention` table (player, captured, slept, skip rate, top wake causes, est USD saved, false-quiet rate) into `render_markdown` guarded by `if report.get("attention"):`.
- [ ] **Step 4: Run analyze tests + full suite** — green.
- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/analyze.py tests/arena/test_analyze.py
git commit -m "feat(arena): attention metrics (skip rate, wake causes, savings, false-quiet)"
```

---

### Task 12: Playbook section, live-probe checklist, final gate

**Files:**
- Modify: `src/civ_mcp/arena/playbook.md`
- Create: `docs/superpowers/plans/2026-07-09-arena-attention-live-probes.md`

- [ ] **Step 1: Append to `src/civ_mcp/arena/playbook.md`:**

```markdown
## Skipping quiet turns
If nothing needs your judgment for a few turns, end your summary with:
SKIP: 3
WAKE IF: GREAT_PERSON_AVAILABLE, CITY_GREW
- SKIP n (1-5): sleep up to n turns; each slept turn costs you nothing.
- You are ALWAYS woken early for threats, end-turn blockers, war/peace changes,
  loyalty problems, task completion/failure, World Congress, and era changes.
- WAKE IF adds optional wake conditions, exactly from: GREAT_PERSON_AVAILABLE,
  CITY_GREW, TRADE_ROUTE_IDLE, GOLD_STOCKPILE_HIGH.
- Good skips: long builds underway, peacetime consolidation, armies healing.
- Never skip: at war, settlers unsettled, enemies visible, negative gold trend.
```

- [ ] **Step 2: Create the live-probe checklist** `docs/superpowers/plans/2026-07-09-arena-attention-live-probes.md` (slice-4 checklist pattern — see `docs/superpowers/plans/2026-07-07-arena-slice4-live-probes.md` for format). Contents: header naming the feature + merge gate statement, then:

```markdown
- [ ] P1 scan: `build_attention_query` returns and parses on a live game
      (all 11 families, no ATTN_ERR); pin the captured lines as a fixture in
      tests/arena/test_attention.py (the turn-380 fixture pattern).
- [ ] P2 sleep: an `auto`-mode puppet on a genuinely quiet seat sleeps; the
      turn advances; a `turn_kind:"slept"` record is written; human seat
      restored.
- [ ] P3 hostile wake: move a hostile unit within threat_radius of a puppet
      city (or use a live barbarian); next captured turn wakes with
      wake_cause=ENEMY_NEAR and the digest names the unit.
- [ ] P4 mini-run: 2-civ `hybrid` run end-to-end; transcript shows sleeps,
      a digest-injected wake, sane budgets (turns_slept + puppet_turns_played
      == captured turns), analyze renders the Attention section.
```

- [ ] **Step 3: Full-suite final gate**

Run: `uv run --extra test pytest tests/ -q`
Expected: baseline 859 + all new tests, 0 failures.

- [ ] **Step 4: Commit**

```bash
git add src/civ_mcp/arena/playbook.md docs/superpowers/plans/2026-07-09-arena-attention-live-probes.md
git commit -m "docs(arena): attention playbook guidance + live-probe checklist"
```

- [ ] **Step 5: STOP.** Leave the branch unmerged. Report: branch name, commit list, test count, and the live-probe checklist path. riz reviews in a separate session and owns the merge + live probes.
