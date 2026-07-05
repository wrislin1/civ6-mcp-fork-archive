# Civ Arena — Hybrid Driver Implementation Plan

> **Status (2026-06-30): IMPLEMENTED + live-verified.** The hybrid driver shipped on `main`
> (implementation through `f62854e`, planning/live-verification notes through `c9416aa`).
> Both CLI paths are live-verified on the gaming PC: `cli-codex` and `cli-claude` each drove real
> puppet turns with clean human hand-back. Security remediation
> (host lockdown, server-side `run_lua`/lifecycle removal, cancellation-safe handback) landed
> across `25f390e`..`19cfb81`. See `arena-live-gate-cli-mcp-loading-issue.md` for the resolved
> CLI-MCP loading investigation and the cli-claude live-verification record. Remaining checkboxes
> below are historical task tracking, not open work.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps
> use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Drive the two AI civs (P1, P2) of the live single-player game with LLMs — P1 by an
**in-process local** model and P2 by a **headless CLI agent** (`cli-claude` or `cli-codex`) as
the cloud cost-probe — proving per-civ driver routing, real per-turn token/cost accounting, and
clean hand-back to the human (P0).

**Architecture:** Builds on the existing `arena-vertical-slice` branch. The proven
deterministic core is **reused unchanged**: `hook.py` (tuner-injected `SetLocalPlayerAndObserver`
takeover + hold) and `coordinator.py`'s inject→wait-for-held-puppet→run-policy→end-turn→restore
loop. The new work is a **per-civ policy router** and a second policy type, **`CLIAgentPolicy`**,
that spawns a headless CLI (`claude -p` via project `.mcp.json` auto-discovery, or `codex exec`
with inline civ6 MCP config) pointed at the `civ6` MCP server. Because the
FireTuner is **single-client** (verified live), a CLI civ needs *exclusive* tuner access: the
coordinator **releases its connection** for the duration of the CLI run and **reclaims** it to
end the turn. In-process civs have no contention (the coordinator is the sole client).

**Tech Stack:** Python 3.12, `asyncio`, existing `civ_mcp.connection.GameConnection` +
`civ_mcp.game_state.GameState`, `openai` SDK (in-process backend), `claude` and `codex` CLIs
(subprocess) for CLI civs, `pytest` + `pytest-asyncio`.

## Global Constraints

- **Runtime is WSL on the gaming PC** (`riz@192.168.20.141`, mirrored networking). The tuner
  `127.0.0.1:4318` is reachable from WSL (verified — `civ-mcp` read the live game). The arena
  runs from `~/projects/civ6-mcp` (where `.mcp.json` registers the `civ6` MCP server). No
  Windows-side runtime needed anymore.
- **FireTuner is single-client (verified live).** A second concurrent connection fails. The
  coordinator MUST hold at most one connection, and MUST drop it before launching a CLI civ
  (whose `civ6` MCP opens its own connection) and re-open it afterward. The seized seat stays
  frozen across the hand-off (takeover is in-game state, not connection state).
- **Human safety invariant:** every coordinator path — success, exception, KeyboardInterrupt —
  ends with the hook disabled and local player restored to 0, via a connection the coordinator
  re-opens if it had released it.
- **Lua context rules (verified):** `SetLocalPlayerAndObserver`, `UnitManager.FinishMoves`,
  unit iteration → GameCore (`execute_read`); `UI.RequestAction(ACTION_ENDTURN)`, `UI.*` →
  InGame (`execute_write`).
- **In-process backend endpoint:** riz-llm per-GPU Ollama at `http://192.168.20.196:11430/v1`
  (LAN-reachable, OpenAI-compatible, tool-calling). LiteLLM `:4000` and Ollama-unified `:11434`
  on riz-llm are loopback-only — unreachable from the gaming PC; do not use them.
- **boomtube is not a civ brain** (single-shot, no tool-calling, local-only). It is intentionally
  excluded from arena CLI civs: `cli-claude` is restricted to project/local `mcp__civ6`, and
  `cli-codex` uses inline civ6-only MCP config. No user-scope auxiliary MCP servers are part of
  the trusted arena path.
- **Live game state (verified):** P0=human, P1/P2=AI majors, P3–P11=city-states, turn 3. This
  is the target game.
- **Scope:** two AI seats, one in-process + one CLI (`cli-claude` or `cli-codex`). No scaling
  to N civs, no replay UI, and no per-civ config file in this plan. `cli-codex` is allowed only
  because the server-side `CIV_MCP_ARENA_PUPPET=1` gate removes `end_turn`, lifecycle tools, and
  `run_lua`; Codex does not provide a Claude-style per-MCP-tool denylist.

---

## File Structure

- `src/civ_mcp/arena/config.py` — MODIFY: `PlayerSpec.provider` becomes a driver tag
  (`local` | `cli-claude` | `cli-codex`); add `driver_kind()` helper; **validate** provider against the known
  set in `parse_player_spec` (reject typos).
- `src/civ_mcp/arena/cli_agent.py` — CREATE: `CLIAgentPolicy` (`claude`/`codex` subprocess spawner + usage parse).
- `src/civ_mcp/arena/coordinator.py` — MODIFY: accept a `policy_for(player_id)` router; honor
  `policy.needs_exclusive_tuner` by releasing/reclaiming the connection around the policy call.
- `src/civ_mcp/arena/cost.py` — MODIFY: `record(...)` accepts an optional `usd` override (CLI
  reports cost directly).
- `src/civ_mcp/arena/arena.py` — MODIFY: extract a pure `build_policies(specs, cost, cfg)` helper
  that maps specs → per-civ policy dict (+ in-process backend); `_run` calls it and passes the router.
- `tests/arena/test_cli_agent.py`, `tests/arena/test_coordinator_router.py`,
  `tests/arena/test_arena_wiring.py` (new), `tests/arena/test_config.py` (extend) — unit tests
  with fakes (no game, no CLI).
- `tests/arena/test_coordinator.py` — MODIFY: extend the existing `FakeConn` with
  `is_connected`/`connect`/`disconnect` so the new `run_arena` finally-block (which reads
  `is_connected`) keeps the existing dry-run test green.

---

## Task 1: Per-civ driver tag in config

**Files:** Modify `src/civ_mcp/arena/config.py`; extend `tests/arena/test_config.py`.

**Interfaces:**
- Produces: `PlayerSpec(player_id:int, provider:str, model:str)` where `provider` ∈
  `{"local","cli-claude","cli-codex"}`; `PlayerSpec.driver_kind() -> "in_process"|"cli"`.
  `parse_player_spec("2:cli-claude:")` → `PlayerSpec(2,"cli-claude","")` (empty model = CLI
  default). `parse_player_spec("1:local:qwen3-coder:30b")` →
  `PlayerSpec(1,"local","qwen3-coder:30b")` (model may itself contain a colon — split on the
  FIRST TWO colons only). `parse_player_spec` **rejects** an unknown provider (e.g.
  `"1:typo:model"`) with `ValueError` — no silent fall-through to `in_process`.

- [ ] **Step 1: Write the failing test**
```python
# tests/arena/test_config.py  (add)
import pytest
from civ_mcp.arena.config import parse_player_spec, PlayerSpec

def test_local_model_with_colon():
    s = parse_player_spec("1:local:qwen3-coder:30b")
    assert s == PlayerSpec(1, "local", "qwen3-coder:30b")
    assert s.driver_kind() == "in_process"

def test_cli_claude_empty_model():
    s = parse_player_spec("2:cli-claude:")
    assert s == PlayerSpec(2, "cli-claude", "")
    assert s.driver_kind() == "cli"

def test_cli_codex_model_with_colon():
    s = parse_player_spec("2:cli-codex:gpt-5.5")
    assert s == PlayerSpec(2, "cli-codex", "gpt-5.5")
    assert s.driver_kind() == "cli"

def test_rejects_unknown_provider():
    with pytest.raises(ValueError):
        parse_player_spec("1:typo:model")
```

- [ ] **Step 2: Run test to verify it fails**
Run: `uv run pytest tests/arena/test_config.py -v`
Expected: FAIL (`driver_kind` missing; colon-in-model assert fails; unknown-provider not yet rejected).

- [ ] **Step 3: Write minimal implementation**
```python
# src/civ_mcp/arena/config.py  (replace parse_player_spec + add driver_kind)
_CLI_PROVIDERS = {"cli-claude", "cli-codex"}
_VALID_PROVIDERS = {"local"} | _CLI_PROVIDERS

def parse_player_spec(s: str) -> "PlayerSpec":
    parts = s.split(":", 2)            # id : provider : model(may contain ':')
    if len(parts) != 3:
        raise ValueError(f"bad --player spec {s!r}; want '<id>:<provider>:<model>'")
    pid, provider, model = parts
    if provider not in _VALID_PROVIDERS:
        raise ValueError(
            f"unknown provider {provider!r} in --player spec {s!r}; "
            f"want one of {sorted(_VALID_PROVIDERS)}")
    return PlayerSpec(int(pid), provider, model)
```
Add to `PlayerSpec` (it is a frozen dataclass):
```python
    def driver_kind(self) -> str:
        return "cli" if self.provider in _CLI_PROVIDERS else "in_process"
```
(`_CLI_PROVIDERS` is defined above the class; if Python complains about forward use, move the
set definition to the top of the module.)

- [ ] **Step 4: Run test to verify it passes** → PASS.

- [ ] **Step 5: Commit**
```bash
git add src/civ_mcp/arena/config.py tests/arena/test_config.py
git commit -m "feat(arena): per-civ driver tag (local vs cli-*) + colon-safe model parse"
```

---

## Task 2: cost.py accepts a CLI-reported USD override

**Files:** Modify `src/civ_mcp/arena/cost.py`; extend `tests/arena/test_cost.py`.

**Interfaces:**
- Produces: `CostLog.record(player_id, model, provider, prompt_tokens, completion_tokens,
  turn, usd=None)`. When `usd` is provided (CLI civs report cost directly) it is used verbatim;
  when `None`, the existing `PRICES` table computes it (local → 0.0).

- [ ] **Step 1: Write the failing test**
```python
# tests/arena/test_cost.py  (add)
from civ_mcp.arena.cost import CostLog

def test_usd_override(tmp_path):
    log = CostLog(str(tmp_path / "c.jsonl"))
    log.record(player_id=2, model="claude", provider="cli-claude",
               prompt_tokens=1000, completion_tokens=200, turn=3, usd=0.0123)
    s = log.summary()
    assert s["by_player"][2]["usd"] == 0.0123
    assert s["total_usd"] == 0.0123
```

- [ ] **Step 2: Run test to verify it fails**
Run: `uv run pytest tests/arena/test_cost.py::test_usd_override -v` → FAIL (`record` has no `usd`).

- [ ] **Step 3: Write minimal implementation**
In `record(...)` add the `usd=None` parameter and replace the usd line:
```python
    def record(self, player_id, model, provider, prompt_tokens, completion_tokens, turn, usd=None):
        rec = {
            "turn": turn, "player_id": player_id, "provider": provider, "model": model,
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "usd": self._usd(provider, model, prompt_tokens, completion_tokens) if usd is None else round(float(usd), 6),
        }
        self._records.append(rec)
        with open(self.path, "a") as f:
            f.write(json.dumps(rec) + "\n")
```

- [ ] **Step 4: Run test to verify it passes** → PASS (and existing cost tests still pass).

- [ ] **Step 5: Commit**
```bash
git add src/civ_mcp/arena/cost.py tests/arena/test_cost.py
git commit -m "feat(arena): cost log accepts CLI-reported usd override"
```

---

## Task 3: CLIAgentPolicy (claude subprocess driver)

**Files:** Create `src/civ_mcp/arena/cli_agent.py`; create `tests/arena/test_cli_agent.py`.

**Interfaces:**
- Consumes: a `CostLog` (Task 2). The coordinator calls it with `(gs, player_id, turn)` but a
  CLI policy **ignores `gs`** (it has no live tuner connection — see Task 4 exclusivity).
- Produces: `class CLIAgentPolicy(provider, cost, project_dir, model="", timeout_s=900,
  max_turns=40)` with `needs_exclusive_tuner = True` and
  `async __call__(gs, player_id, turn) -> dict`. It builds the argv (`_build_argv`), runs the
  CLI via `asyncio.create_subprocess_exec`, parses provider JSON (`_parse_claude` or
  `_parse_codex`) into `(summary, prompt_tokens, completion_tokens, usd)`, records cost, and
  returns `{"summary":..., "actions":[], "usage":{...}}`. Shipped providers are `cli-claude`
  and `cli-codex`; unknown CLI providers raise `ValueError` in `_build_argv`. Argv construction
  is unit-tested; the subprocess is not.

- [ ] **Step 1: Write the failing test** (pure argv + parser; no subprocess)
```python
# tests/arena/test_cli_agent.py
import json
from civ_mcp.arena.cli_agent import CLIAgentPolicy

class FakeCost:
    def __init__(self): self.records = []
    def record(self, **kw): self.records.append(kw)

def test_claude_argv_contains_mcp_and_safety():
    pol = CLIAgentPolicy("cli-claude", FakeCost(), project_dir="/x", max_turns=20)
    argv = pol._build_argv(player_id=2, turn=3)
    assert argv[0] == "claude"
    assert "-p" in argv and "--output-format" in argv and "json" in argv
    # restrict to civ6 tools and forbid ending the turn (host ends it)
    assert "--allowedTools" in argv and "mcp__civ6" in " ".join(argv)
    assert "--disallowedTools" in argv and "mcp__civ6__end_turn" in " ".join(argv)
    # the prompt names the seat
    assert any("player 2" in a for a in argv)

def test_parse_claude_usage():
    pol = CLIAgentPolicy("cli-claude", FakeCost(), project_dir="/x")
    blob = json.dumps({"type": "result", "subtype": "success", "result": "settled & moved",
                       "total_cost_usd": 0.0123,
                       "usage": {"input_tokens": 1000, "output_tokens": 200}})
    summary, pt, ct, usd = pol._parse_claude(blob)
    assert summary == "settled & moved" and pt == 1000 and ct == 200 and usd == 0.0123
```

- [ ] **Step 2: Run test to verify it fails** → FAIL (no module).

- [ ] **Step 3: Write minimal implementation**

> Historical note: this first minimal slice was Claude-only. Current `main` extends the shipped
> implementation with `cli-codex`, server-side puppet env gating, provider-specific parsers, and
> process-group cleanup. Do not copy this snippet as current complete code; use
> `src/civ_mcp/arena/cli_agent.py` as the source of truth for maintenance.

```python
# src/civ_mcp/arena/cli_agent.py
from __future__ import annotations
import asyncio, json

_PROMPT = (
    "You are playing player {pid} (an AI civ) in the running Civilization VI game; it is "
    "turn {turn} and YOU are currently the active player. Use the civ6 tools to observe your "
    "situation and take a few sensible early-game actions (scout, move/settle a settler, set "
    "city production and research). Do NOT end the turn — the host ends it for you. When done, "
    "give a one-line summary."
)

class CLIAgentPolicy:
    needs_exclusive_tuner = True   # the CLI's civ6 MCP needs the single tuner slot

    def __init__(self, provider, cost, project_dir, model="", timeout_s=900, max_turns=40):
        self.provider, self.cost, self.project_dir = provider, cost, project_dir
        self.model, self.timeout_s, self.max_turns = model, timeout_s, max_turns

    def _build_argv(self, player_id: int, turn: int) -> list[str]:
        prompt = _PROMPT.format(pid=player_id, turn=turn)
        if self.provider == "cli-claude":
            argv = ["claude", "-p", prompt, "--output-format", "json",
                    "--permission-mode", "bypassPermissions",
                    "--allowedTools", "mcp__civ6",
                    "--disallowedTools", "mcp__civ6__end_turn",
                    "--max-turns", str(self.max_turns)]
            if self.model:
                argv += ["--model", self.model]
            return argv
        raise ValueError(f"unknown CLI provider {self.provider!r}")

    @staticmethod
    def _parse_claude(stdout: str):
        obj = None
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                cand = json.loads(line)
            except ValueError:
                continue
            if isinstance(cand, dict) and cand.get("type") == "result":
                obj = cand
        if obj is None:                       # --output-format json may emit one object
            try:
                obj = json.loads(stdout)
            except ValueError:
                return ("(unparseable CLI output)", 0, 0, 0.0)
        u = obj.get("usage", {}) or {}
        return (str(obj.get("result", ""))[:500],
                int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0)),
                float(obj.get("total_cost_usd", 0.0)))

    async def __call__(self, gs, player_id: int, turn: int) -> dict:
        argv = self._build_argv(player_id, turn)
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=self.project_dir,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            return {"summary": f"cli timeout after {self.timeout_s}s", "actions": [], "usage": {}}
        stdout = out.decode("utf-8", "replace")
        summary, pt, ct, usd = self._parse_claude(stdout)
        self.cost.record(player_id=player_id, model=(self.model or self.provider),
                         provider=self.provider, prompt_tokens=pt, completion_tokens=ct,
                         turn=turn, usd=usd)
        return {"summary": summary, "actions": [],
                "usage": {"prompt_tokens": pt, "completion_tokens": ct, "usd": usd,
                          "exit": proc.returncode, "stderr": err.decode("utf-8","replace")[-400:]}}
```

- [ ] **Step 4: Run test to verify it passes** → PASS.

- [ ] **Step 5: Commit**
```bash
git add src/civ_mcp/arena/cli_agent.py tests/arena/test_cli_agent.py
git commit -m "feat(arena): CLIAgentPolicy — headless claude civ driver with usage parse"
```

---

## Task 4: Coordinator policy router + tuner exclusivity hand-off

**Files:** Modify `src/civ_mcp/arena/coordinator.py`; create `tests/arena/test_coordinator_router.py`.

**Interfaces:**
- Consumes: `hook.*`; a `GameState` `gs`; a `conn` exposing `connect()`, `disconnect()`, and the
  verified `is_connected` property (`connection.py:39`); a router `policy_for(player_id) -> policy`
  where a policy may set `needs_exclusive_tuner = True`. NOTE (verified): `connect()` does NOT
  guard a double-call and the tuner is single-client, so every connect/disconnect MUST be gated on
  `is_connected`.
- Produces: `async run_arena(conn, gs, config, policy=None, policy_for=None) -> dict`. Back-compat:
  if `policy_for` is None, wrap the single `policy` as a constant router. When the selected policy
  has `needs_exclusive_tuner` truthy: after observing the held puppet, **`await conn.disconnect()`**,
  run the policy (it drives the game out-of-band via its own CLI/MCP), then **`await conn.connect()`**
  before `finish_units`/`restore_local`. In-process policies run inline as today.

- [ ] **Step 1: Write the failing test** (FakeConn tracks connect/disconnect ordering)
```python
# tests/arena/test_coordinator_router.py
import pytest
from civ_mcp.arena.coordinator import run_arena
from civ_mcp.arena.config import ArenaConfig, PlayerSpec

class FakeConn:
    def __init__(self):
        self.events = []; self.restored = False; self._connected = True
        self._polls = iter([
            ["LOCAL|2", "TURN|3", "ACTIVE|true", "LAST|2"],
        ])
    @property
    def is_connected(self): return self._connected
    async def connect(self): self._connected = True; self.events.append("connect")
    async def disconnect(self): self._connected = False; self.events.append("disconnect")
    async def execute_read(self, lua, timeout=5.0):
        if "GetCurrentGameTurn" in lua and "ACTIVE" in lua:
            try: return next(self._polls)
            except StopIteration: return ["LOCAL|0", "TURN|3", "ACTIVE|false", "LAST|2"]
        if "SetLocalPlayerAndObserver(0)" in lua:
            self.restored = True; return ["LOCAL|0"]
        if "HOOK_OK" in lua or "__pt_registered" in lua: return ["HOOK_OK|true"]
        if "DISABLED" in lua: return ["DISABLED|true"]
        if "FINISHED" in lua: return ["FINISHED|1"]
        return []
    async def execute_write(self, lua, timeout=5.0): return []

class ExclusivePolicy:
    needs_exclusive_tuner = True
    def __init__(self): self.called_with_events = None
    async def __call__(self, gs, player_id, turn):
        self.called_with_events = list(gs.conn.events)  # snapshot at call time
        return {"summary": "cli ran", "actions": []}

class FakeGS:
    def __init__(self, conn): self.conn = conn
    async def get_game_overview(self): return "OV"
    async def get_units(self): return []

@pytest.mark.asyncio
async def test_exclusive_policy_releases_then_reclaims_tuner():
    conn = FakeConn(); gs = FakeGS(conn); pol = ExclusivePolicy()
    cfg = ArenaConfig(players=[PlayerSpec(2, "cli-claude", "")], max_puppet_turns=1, puppet_ids=[2])
    result = await run_arena(conn, gs, cfg, policy_for=lambda pid: pol)
    assert result["puppet_turns_played"] == 1
    # disconnect happened BEFORE the policy ran; reconnect happened before restore
    assert "disconnect" in pol.called_with_events
    assert conn.events.index("disconnect") < conn.events.index("connect")
    assert conn.restored is True
```

- [ ] **Step 2: Run test to verify it fails** → FAIL (`policy_for` unsupported; no release/reclaim).

- [ ] **Step 3: Write minimal implementation** (replace `run_arena`; keep `ScriptedPolicy`)
```python
async def run_arena(conn, gs, config, policy=None, policy_for=None) -> dict:
    if policy_for is None:
        if policy is None:
            raise ValueError("run_arena needs policy or policy_for")
        policy_for = lambda _pid: policy
    puppet_ids = set(config.puppet_ids or [p.player_id for p in config.players])
    played, log = 0, []
    try:
        await hook.inject(conn, sorted(puppet_ids))
        remaining = config.max_puppet_turns
        deadline_polls = 600
        while remaining > 0 and deadline_polls > 0:
            st = await hook.poll(conn)
            if st.active and st.local in puppet_ids:
                pol = policy_for(st.local)
                exclusive = bool(getattr(pol, "needs_exclusive_tuner", False))
                if exclusive and conn.is_connected:
                    await conn.disconnect()       # free the single tuner slot for the CLI
                result = await pol(gs, st.local, st.turn)
                log.append({"player": st.local, "turn": st.turn, **result})
                if exclusive and not conn.is_connected:
                    await conn.connect()          # reclaim before we end the turn
                await hook.finish_units(conn, st.local)
                await hook.restore_local(conn, 0)
                played += 1
                remaining -= 1
            else:
                await asyncio.sleep(1.0)
            deadline_polls -= 1
        return {"puppet_turns_played": played, "log": log}
    finally:
        # Human-safety: reclaim a connection if we released one, then restore + disable.
        try:
            if not conn.is_connected:
                await conn.connect()
        except Exception:
            pass
        try:
            await hook.restore_local(conn, 0)
        except Exception:
            pass
        try:
            await hook.disable(conn)
        except Exception:
            pass
```
> NOTE (verified against `connection.py`): `is_connected` is a property (line 39); `connect()`
> does not guard a re-call and the tuner is single-client, so the gates above are required, not
> optional.

- [ ] **Step 4: Update the EXISTING fake so the prior dry-run test still passes.**
The current `tests/arena/test_coordinator.py` `FakeConn` (line 5) has **no** `is_connected`,
`connect`, or `disconnect`. The new `run_arena` finally-block reads `conn.is_connected`
unconditionally on every path, so without this the existing `test_coordinator_runs_one_puppet_turn_and_restores`
would raise `AttributeError`. Extend that fake (the real `GameConnection` already exposes all
three, so the fake should match the contract rather than the production code defensively
`getattr`-ing it):
```python
# tests/arena/test_coordinator.py — add to FakeConn
    def __init__(self):
        self.restored = False
        self._connected = True            # NEW
        self._polls = iter([
            ["LOCAL|0", "TURN|1", "ACTIVE|false", "LAST|nil"],
            ["LOCAL|1", "TURN|2", "ACTIVE|true", "LAST|1"],
        ])
    @property                              # NEW
    def is_connected(self): return self._connected
    async def connect(self): self._connected = True      # NEW
    async def disconnect(self): self._connected = False  # NEW
```
(`ScriptedPolicy` is non-exclusive, so `connect`/`disconnect` are never called on this path —
but `is_connected` IS read in the finally-block, so the property is mandatory.)

- [ ] **Step 5: Run both router and coordinator tests** → PASS.
Run: `uv run pytest tests/arena/test_coordinator_router.py tests/arena/test_coordinator.py -v`
Expected: the new release/reclaim test passes AND the existing dry-run test still passes.

- [ ] **Step 6: Commit**
```bash
git add src/civ_mcp/arena/coordinator.py tests/arena/test_coordinator_router.py tests/arena/test_coordinator.py
git commit -m "feat(arena): per-civ policy router + single-client tuner release/reclaim for CLI civs"
```

---

## Task 5: Wire the CLI + in-process endpoint in arena.py

**Files:** Modify `src/civ_mcp/arena/arena.py`; create `tests/arena/test_arena_wiring.py`.

**Interfaces:** Extract a **pure, importable** `build_policies(specs, cost, cfg) ->
(policies: dict[int, policy], in_proc_backend | None)` that maps `--player` specs to per-civ
policies: `local` → `LLMPolicy` over `OpenAICompatBackend` (Ollama `:11430`);
`cli-claude`/`cli-codex` → `CLIAgentPolicy`. `_run` calls `build_policies`, then passes a `policy_for` closure to
`run_arena`. The router is unit-tested directly (not only via live gates) — construction of all
three policy types is side-effect-free (no network on construct), so the test needs no
monkeypatch.

- [ ] **Step 1: Write the failing wiring test**
```python
# tests/arena/test_arena_wiring.py
from civ_mcp.arena.arena import build_policies
from civ_mcp.arena.config import PlayerSpec, ArenaConfig
from civ_mcp.arena.agent import LLMPolicy
from civ_mcp.arena.cli_agent import CLIAgentPolicy

class FakeCost:
    def record(self, **kw): pass

def test_build_policies_routes_by_provider():
    specs = [PlayerSpec(1, "local", "qwen3-coder:30b"), PlayerSpec(2, "cli-claude", "")]
    cfg = ArenaConfig(players=specs)
    policies, backend = build_policies(specs, FakeCost(), cfg)
    assert isinstance(policies[1], LLMPolicy)        # local → in-process LLM
    assert isinstance(policies[2], CLIAgentPolicy)   # cli-claude → CLI subprocess
    assert backend is not None                       # an in-process backend was constructed
```

- [ ] **Step 2: Run test to verify it fails** → FAIL (`build_policies` does not exist yet).

- [ ] **Step 3: Implement**
```python
# arena.py — add the helper at module scope
def build_policies(specs, cost, cfg):
    """Pure: specs -> ({player_id: policy}, in_proc_backend|None). No network on construct."""
    from civ_mcp.arena.cli_agent import CLIAgentPolicy
    from civ_mcp.arena.backends import OpenAICompatBackend
    from civ_mcp.arena.agent import LLMPolicy
    policies, in_proc_backend = {}, None
    for spec in specs:
        if spec.driver_kind() == "cli":
            policies[spec.player_id] = CLIAgentPolicy(
                spec.provider, cost, project_dir=os.getcwd(), model=spec.model)
        else:  # in_process local
            in_proc_backend = OpenAICompatBackend(
                cfg.gateway_url, os.environ.get(cfg.api_key_env, "x"), spec.model)
            policies[spec.player_id] = LLMPolicy(in_proc_backend, cost, max_steps=cfg.max_agent_steps)
    return policies, in_proc_backend
```
```python
# arena.py — replace the policy-construction block in _run(args)
    cost = CostLog(cfg.cost_path)
    policies, in_proc_backend = build_policies(specs, cost, cfg)
    if args.dry_run:
        from civ_mcp.arena.coordinator import ScriptedPolicy
        sp = ScriptedPolicy()
        policy_for = lambda pid: sp
    else:
        if in_proc_backend is not None and not await in_proc_backend.reachable():
            raise SystemExit(f"in-process backend not reachable at {cfg.gateway_url}")
        policy_for = lambda pid: policies[pid]
    conn = GameConnection(); await conn.connect()
    gs = GameState(conn)
    result = await run_arena(conn, gs, cfg, policy_for=policy_for)
    print(json.dumps({"result": result, "cost": cost.summary()}, indent=2))
```
Also change the `--gateway-url` default to `http://192.168.20.196:11430/v1`.

- [ ] **Step 4: Run test to verify it passes** → PASS.

- [ ] **Step 5: Smoke the parser**
Run: `uv run civ-arena --help` → exit 0.

- [ ] **Step 6: Commit**
```bash
git add src/civ_mcp/arena/arena.py tests/arena/test_arena_wiring.py
git commit -m "feat(arena): per-civ policy wiring (build_policies + router) and Ollama :11430 default"
```

---

## Gate 6: Deploy branch to the gaming PC + verify the two backends

**Pre:** Tasks 1–5 committed on `arena-vertical-slice` in the riz-llm dev clone; 10+ unit tests
green (config incl. unknown-provider rejection, cost usd-override, cli_agent argv+parse, coordinator
router release/reclaim, the unchanged dry-run coordinator test, and the arena `build_policies` router).

- [ ] **Step 1: Push the branch to the gaming PC and check it out (no worktree).**
```bash
# from the riz-llm dev clone
git push origin arena-vertical-slice
# on the gaming PC (origin is the gaming PC's repo; main is checked out, branch is not)
ssh riz@192.168.20.141 'bash -lc "cd ~/projects/civ6-mcp && git fetch origin && git checkout arena-vertical-slice && git log --oneline -1 && uv sync --extra test && uv run pytest tests/arena -q"'
```
Expected: checkout OK, all arena unit tests pass on the gaming PC.

- [ ] **Step 2: In-process backend reachable AND actually emits a tool call (Ollama :11430).**
`reachable()` only runs `models.list()` — it proves the endpoint is up, NOT that the model will
emit the tool calls `LLMPolicy` depends on. Drive one synthetic tool-call through the real
`chat(...)` path and assert a tool call comes back:
```bash
ssh riz@192.168.20.141 'bash -lc "cd ~/projects/civ6-mcp && uv run python - <<PY
import asyncio
from civ_mcp.arena.backends import OpenAICompatBackend
b = OpenAICompatBackend(\"http://192.168.20.196:11430/v1\", \"x\", \"qwen3-coder:30b\")
tool = {\"type\": \"function\", \"function\": {\"name\": \"ping\",
        \"description\": \"Acknowledge by calling this tool.\",
        \"parameters\": {\"type\": \"object\", \"properties\": {\"ok\": {\"type\": \"boolean\"}},
                         \"required\": [\"ok\"]}}}
msgs = [{\"role\": \"user\", \"content\": \"Call the ping tool with ok=true. Do not reply in text.\"}]
print(\"reachable\", asyncio.run(b.reachable()))
r = asyncio.run(b.chat(msgs, [tool]))
print(\"tool_calls\", [tc[\"name\"] for tc in r.tool_calls])
assert r.tool_calls, \"model returned no tool call — pick another tool-calling tag\"
PY"'
```
Expected: `reachable True` then `tool_calls ['ping']`. If the assert fails (empty `tool_calls`),
the tag does not tool-call reliably — switch the local model to another tool-calling Ollama tag
(e.g. `qwen3:32b`) via `--player 1:local:<tag>` and re-run.

- [ ] **Step 3: CLI loads the civ6 MCP headlessly (game NOT required; just MCP registration).**
```bash
ssh riz@192.168.20.141 'bash -lc "cd ~/projects/civ6-mcp && timeout 120 claude -p \"List the MCP tools whose name starts with mcp__civ6 and then stop.\" --output-format json --permission-mode bypassPermissions --allowedTools mcp__civ6 2>&1 | tail -c 1200"'
```
Expected: JSON result mentioning civ6 tools (confirms `claude -p` loads `.mcp.json`'s `civ6`
server). If civ6 is not loaded, do **not** fall back to explicit `--mcp-config`: live testing found
that explicit `--mcp-config` did not expose the civ6 stdio tools under headless `claude -p`.
Instead, debug project auto-discovery, `--setting-sources project,local`, `.mcp.json`, and the
env relay documented in `arena-live-gate-cli-mcp-loading-issue.md`, then reconcile
`cli_agent._build_argv` to the verified invocation.

---

## Gate 7: Scripted dry-run takeover on the LIVE game (no LLM, no CLI)

**Pre:** Civ running, you on seat 0; FireTuner on; `FireTuner.exe` window may stay closed.

- [ ] **Step 1: Drive P1 with the scripted policy for one turn.**
```bash
ssh riz@192.168.20.141 'bash -lc "cd ~/projects/civ6-mcp && uv run civ-arena --dry-run --player 1:local: --max-puppet-turns 1"'
```
- [ ] **Step 2: When it polls, end YOUR turn in the Civ UI** so P1's turn starts.
- [ ] **Step 3: Verify** JSON `puppet_turns_played: 1`, process exit 0, and **in-game control
  returns to you**. If control does NOT hand back, apply the existing coordinator DESIGN NOTE
  (add an InGame `UI.RequestAction(ActionTypes.ACTION_ENDTURN)` in `hook` before
  `restore_local`, executed while `local==K`) and re-run. This validates takeover→end→handback
  on THIS game before any LLM/CLI nondeterminism.

---

## Gate 8: P2 cloud cost-probe — one `cli-claude` turn (the headline result)

**Pre:** Gates 6–7 green; Civ running.

- [ ] **Step 1: Run P2 as a claude CLI civ for one turn.**
```bash
ssh riz@192.168.20.141 'bash -lc "cd ~/projects/civ6-mcp && uv run civ-arena --player 2:cli-claude: --max-puppet-turns 1 --cost-path arena_cost.jsonl"'
```
- [ ] **Step 2: End your turn in the UI** so P1 (untouched — not a puppet here) and then P2
  reach their turns; the coordinator only seizes P2. (Only P2 is in `--player`, so P1 plays as
  the built-in AI this run.)
- [ ] **Step 3: Verify** the JSON `log[0]` has the model's summary; the coordinator released
  then reclaimed the tuner around the CLI; `cost.by_player["2"]` shows non-zero
  `prompt_tokens`/`completion_tokens` and a **real `usd`** (Claude `total_cost_usd`);
  `arena_cost.jsonl` has the record; and **control returns to you** in-game. This is the
  token-cost probe you asked for.

---

## Gate 9: Full pass — P1 in-process local + P2 cli-claude

**Pre:** Gate 8 green.

- [ ] **Step 1: Drive both AI seats.**
```bash
ssh riz@192.168.20.141 'bash -lc "cd ~/projects/civ6-mcp && uv run civ-arena \
  --player 1:local:qwen3-coder:30b --player 2:cli-claude: \
  --max-puppet-turns 2 --gateway-url http://192.168.20.196:11430/v1 --cost-path arena_cost.jsonl"'
```
- [ ] **Step 2: End your turn in the UI;** P1 is seized first (in-process, no tuner release),
  then P2 (tuner released for the CLI, reclaimed after).
- [ ] **Step 3: Verify** `puppet_turns_played: 2`; `log` has a P1 entry (in-process actions,
  local provider, $0) and a P2 entry (CLI summary, real usd); both seats acted in-game; control
  returns to you. Per-civ cost breakdown in `cost.summary()` shows local-vs-cloud side by side.
- [ ] **Step 4: Commit any gate-driven fixes** (turn-end mechanic, argv, model tag).
Stage **explicitly** — never `git add -A` here: this repo has untracked `.serena/` and the gate
runs generate `arena_cost.jsonl` cost logs, both of which must stay out of the commit. Review,
then stage only the arena source/tests:
```bash
git status --short                       # review — confirm no .serena/ or *.jsonl is about to be staged
git add src/civ_mcp/arena tests/arena
git commit -m "fix(arena): hybrid-driver live-gate fixes"
```

---

## Self-Review

- **Spec coverage:** per-civ routing (T4 + T5 `build_policies` unit test), in-process local civ
  (existing `LLMPolicy` + T5 endpoint), CLI cloud civs (`cli-claude` and `cli-codex`), per-turn
  token/cost incl. real Claude USD and Codex token accounting (T2 + T3 + Gate 8), single-client tuner hand-off (Global
  Constraint + T4 + test), human-safety restore (T4 finally), live P1+P2 (Gate 9). ✓
- **Single-client constraint** is verified live and is the spine of T4; the test asserts
  disconnect-before-policy and reconnect-before-restore. The new finally-block reads
  `conn.is_connected`, so T4 Step 4 extends the *existing* `test_coordinator.py` fake to match
  the real `GameConnection` contract — the prior dry-run test stays green (not just asserted, re-run
  in Step 5). ✓
- **Provider validation:** `parse_player_spec` rejects unknown providers (T1 `test_rejects_unknown_provider`)
  — no silent `in_process` fall-through. ✓
- **`cli-codex` security:** shipped because `CIV_MCP_ARENA_PUPPET=1` removes `end_turn`,
  lifecycle tools, and `run_lua` server-side. The valid provider set is
  `{local, cli-claude, cli-codex}`. Codex remains more dependent on the server-side gate because it
  has no Claude-style per-MCP-tool denylist. ✓
- **Placeholder scan:** the only deferred unknowns are CLI argv exactness (Gate 6 Step 3 pins
  it) and the turn-end mechanic (Gate 7 decides `finish_units+restore` vs adding
  `ACTION_ENDTURN`) — both explicit gates. ✓
- **Type consistency:** `PlayerSpec.driver_kind()`, `CostLog.record(..., usd=None)`,
  policy signature `(gs, player_id, turn) -> dict`, `needs_exclusive_tuner` attribute, and
  `run_arena(conn, gs, config, policy=None, policy_for=None)` are used identically across tasks. ✓
