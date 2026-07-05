# Arena Review Fixes Implementation Plan

> **Status (2026-07-05): DONE on `arena-local-civ-context`.** The 10 confirmed
> review findings in this plan landed in `9f8af09` (`fix(arena): resolve 10 review
> findings from the batch fixes`). Follow-up ranked local-civ context review fixes
> are tracked separately in `docs/superpowers/plans/2026-07-05-arena-local-civ-context-review-fixes.md`.
> Current verification after both review-fix passes: `/home/riz/.local/bin/uv run
> pytest tests/arena -q` = 293 passed; `/home/riz/.local/bin/uv run pytest tests -q`
> = 397 passed. Full `pytest -q` still collects legacy script harnesses requiring a
> live FireTuner connection or the absent `civ_mcp.lua_queries` module.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the 10 confirmed review findings that can invalidate local-civ context experiments, watcher config launches, and arena analysis metrics.

**Architecture:** Keep fixes inside the existing arena modules: CLI/config loading stays in `arena.py` and `experiment.py`, model-turn behavior stays in `agent.py`, tool definitions stay in `registry.py`, briefing budget logic stays in `briefing.py`, and offline reporting stays in `analyze.py`/`vocab.py`. Add focused regression tests next to the current tests for each module; add a small shell dry-run harness for watcher argument construction only if Python coverage cannot inspect it.

**Tech Stack:** Python 3.12, pytest/pytest-asyncio, argparse, PyYAML, Bash, uv.

---

## Verified Review Findings

All 10 review findings were checked against the current branch and are valid:

1. `LLMPolicy` caches `resolve_n_ctx(...)->(DEFAULT_N_CTX, "default")` forever after a transient probe failure.
2. `registry.get_map_area` exposes no schema bounds and sends unclamped radius values to `GameState`.
3. In-registry but out-of-tier tool calls are labelled `unknown_tool` and counted as hallucinations/invalid-rate failures.
4. `start-hybrid-watch.sh --config` does not forward live-run fallback defaults for YAML files omitting `max_puppet_turns`, `idle_poll_limit`, or `gateway_url`.
5. `start-hybrid-watch.sh --config` always forwards an auto-generated `--run-id`, so YAML `run_id` is dead on the primary launch path.
6. `resolve_config` detects config-owned CLI flags by comparing values to hardcoded argparse defaults, so explicit default-valued flags are missed.
7. `experiment.run_id` accepts blank strings and path separators before `_run` uses it in `Path(transcript_dir) / run_id`.
8. Briefing map-radius expansion checks only `len(larger)` and omits the `== MAP ==\n` header plus join separator.
9. `config_summary` groups by player and stamps the group with the last record's config fingerprint, mixing append-mode reruns under one fingerprint.
10. `LOCAL_TOOL_VERBS` still covers only the original four local action tools, so the rubric ignores most standard/full-tier action tools.

## File Structure

- `src/civ_mcp/arena/arena.py`: argparse defaults, explicit flag presence, config-mode overrides, and run-id precedence.
- `src/civ_mcp/arena/experiment.py`: YAML default injection and strict `run_id` validation.
- `tools/skills/civ6-arena-live/scripts/start-hybrid-watch.sh`: config-mode argument forwarding and launcher-vs-arena run id behavior.
- `src/civ_mcp/arena/agent.py`: n_ctx retry semantics and out-of-tier invalid-call classification.
- `src/civ_mcp/arena/registry.py`: bounded integer schema metadata and runtime map-radius clamp.
- `src/civ_mcp/arena/briefing.py`: map expansion budget accounting.
- `src/civ_mcp/arena/analyze.py`: invalid-call filtering, config-summary fingerprint grouping, and rubric behavior.
- `src/civ_mcp/arena/vocab.py`: local action verb vocabulary shared by analysis.
- `tests/arena/test_arena_wiring.py`: config/argparse behavior.
- `tests/arena/test_experiment.py`: YAML defaults and run-id validation.
- `tests/arena/test_agent.py`: policy retry/classification behavior.
- `tests/arena/test_registry.py`: map-radius schema and dispatch clamp.
- `tests/arena/test_briefing.py`: map expansion budget regression.
- `tests/arena/test_analyze.py`: invalid-rate, hallucination, config-summary, and local verb vocabulary regressions.

### Task 1: Config CLI Presence And Defaults

**Files:**
- Modify: `src/civ_mcp/arena/arena.py`
- Modify: `src/civ_mcp/arena/experiment.py`
- Test: `tests/arena/test_arena_wiring.py`
- Test: `tests/arena/test_experiment.py`

- [ ] **Step 1: Write failing tests for explicit default-valued config-owned flags**

Append this test to `tests/arena/test_arena_wiring.py` near `test_resolve_config_rejects_non_default_config_owned_flags`:

```python
@pytest.mark.parametrize(
    ("argv_tail", "flag"),
    [
        (["--max-puppet-turns", "1"], "--max-puppet-turns"),
        (["--gateway-url", DEFAULT_GATEWAY_URL], "--gateway-url"),
        (["--idle-poll-limit", "600"], "--idle-poll-limit"),
        (["--max-agent-steps", "6"], "--max-agent-steps"),
    ],
)
def test_resolve_config_rejects_config_owned_flags_even_when_default_value_passed(
    tmp_path, argv_tail, flag
):
    p = tmp_path / "e.yaml"
    p.write_text("civs:\n  - {player: 3, provider: local, model: m}\n")
    with pytest.raises(SystemExit, match=flag):
        resolve_config(build_args(["--config", str(p), *argv_tail]))
```

Also import `DEFAULT_GATEWAY_URL` in the existing import block if it is not already imported:

```python
from civ_mcp.arena.config import ArenaConfig, CivOptions, DEFAULT_GATEWAY_URL
```

- [ ] **Step 2: Write failing tests for launcher fallback defaults used only when YAML omits keys**

Append this test to `tests/arena/test_experiment.py`:

```python
def test_load_experiment_uses_supplied_defaults_for_omitted_run_controls(tmp_path):
    from civ_mcp.arena.config import ArenaConfig

    p = _write(
        tmp_path,
        """
civs:
  - player: 3
    provider: local
    model: gemma4-26b
""",
    )
    cfg = load_experiment(
        p,
        defaults=ArenaConfig(
            players=[],
            max_puppet_turns=8,
            idle_poll_limit=3600,
            gateway_url="http://launcher.example/v1",
        ),
    )

    assert cfg.max_puppet_turns == 8
    assert cfg.idle_poll_limit == 3600
    assert cfg.gateway_url == "http://launcher.example/v1"
```

Append this companion test:

```python
def test_load_experiment_yaml_values_override_supplied_defaults(tmp_path):
    from civ_mcp.arena.config import ArenaConfig

    p = _write(
        tmp_path,
        """
max_puppet_turns: 12
idle_poll_limit: 7200
gateway_url: http://yaml.example/v1
civs:
  - player: 3
    provider: local
    model: gemma4-26b
""",
    )
    cfg = load_experiment(
        p,
        defaults=ArenaConfig(
            players=[],
            max_puppet_turns=8,
            idle_poll_limit=3600,
            gateway_url="http://launcher.example/v1",
        ),
    )

    assert cfg.max_puppet_turns == 12
    assert cfg.idle_poll_limit == 7200
    assert cfg.gateway_url == "http://yaml.example/v1"
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```bash
/home/riz/.local/bin/uv run pytest tests/arena/test_arena_wiring.py::test_resolve_config_rejects_config_owned_flags_even_when_default_value_passed tests/arena/test_experiment.py::test_load_experiment_uses_supplied_defaults_for_omitted_run_controls tests/arena/test_experiment.py::test_load_experiment_yaml_values_override_supplied_defaults -q
```

Expected: failures showing default-valued flags are not rejected and `load_experiment()` does not accept `defaults=`.

- [ ] **Step 4: Add explicit-presence argparse defaults and hidden config fallback flags**

In `src/civ_mcp/arena/arena.py`, change `build_args()` so config-owned user flags default to `None`, and add hidden launcher fallback flags:

```python
def build_args(argv=None):
    ap = argparse.ArgumentParser(prog="civ-arena")
    ap.add_argument("--player", action="append", default=[],
                    help="'<id>:<provider>:<model>[@<gateway>]' (local civ may pin its own gateway)")
    ap.add_argument("--config", default="",
                    help="YAML experiment file (mutually exclusive with --player)")
    ap.add_argument("--max-puppet-turns", type=int, default=None)
    ap.add_argument("--gateway-url", default=None)
    ap.add_argument("--api-key-env", default="LITELLM_OPENAI_API_KEY")
    ap.add_argument("--cost-path", default="", help="path for cost log (default: auto under run dir)")
    ap.add_argument("--run-id", default=None, help="run ID (generated if empty)")
    ap.add_argument("--transcript-dir", default="arena_runs", help="base directory for run dirs")
    ap.add_argument("--no-transcript", action="store_true", help="disable transcript writing")
    ap.add_argument("--max-agent-steps", type=int, default=None)
    ap.add_argument("--idle-poll-limit", type=int, default=None,
                    help="number of 1s polls to wait for puppet turns before exiting")
    ap.add_argument("--dry-run", action="store_true", help="scripted policy, no LLM")
    ap.add_argument("--config-default-max-puppet-turns", type=int, default=None, help=argparse.SUPPRESS)
    ap.add_argument("--config-default-idle-poll-limit", type=int, default=None, help=argparse.SUPPRESS)
    ap.add_argument("--config-default-gateway-url", default=None, help=argparse.SUPPRESS)
    return ap.parse_args(argv)
```

- [ ] **Step 5: Make non-config mode fill effective defaults and config mode reject flag presence**

In `src/civ_mcp/arena/arena.py`, replace `resolve_config()` with this shape:

```python
def _arena_defaults() -> ArenaConfig:
    return ArenaConfig(players=[])


def _value_or_default(value, default):
    return default if value is None else value


def resolve_config(args) -> ArenaConfig:
    from civ_mcp.arena.experiment import load_experiment

    defaults = _arena_defaults()
    config_path = getattr(args, "config", "")
    if config_path and args.player:
        raise SystemExit("--config and --player are mutually exclusive")
    if config_path:
        rejected = []
        if args.max_puppet_turns is not None:
            rejected.append("--max-puppet-turns")
        if args.gateway_url is not None:
            rejected.append("--gateway-url")
        if args.idle_poll_limit is not None:
            rejected.append("--idle-poll-limit")
        if args.max_agent_steps is not None:
            rejected.append("--max-agent-steps")
        if rejected:
            flags = ", ".join(rejected)
            raise SystemExit(f"--config does not allow overriding config-owned flags: {flags}")

        config_defaults = ArenaConfig(
            players=[],
            max_puppet_turns=_value_or_default(
                getattr(args, "config_default_max_puppet_turns", None),
                defaults.max_puppet_turns,
            ),
            idle_poll_limit=_value_or_default(
                getattr(args, "config_default_idle_poll_limit", None),
                defaults.idle_poll_limit,
            ),
            gateway_url=_value_or_default(
                getattr(args, "config_default_gateway_url", None),
                defaults.gateway_url,
            ),
        )
        cfg = load_experiment(config_path, defaults=config_defaults)
        cfg.dry_run = args.dry_run
        cfg.api_key_env = args.api_key_env
        return cfg

    specs = [parse_player_spec(s) for s in args.player]
    max_agent_steps = _value_or_default(args.max_agent_steps, defaults.max_agent_steps)
    if args.max_agent_steps is not None:
        updated = []
        for spec in specs:
            if spec.provider == "local":
                opts = replace(spec.options, max_steps=max_agent_steps)
                spec = replace(spec, options=opts)
            updated.append(spec)
        specs = updated
    return ArenaConfig(
        players=specs,
        max_puppet_turns=_value_or_default(args.max_puppet_turns, defaults.max_puppet_turns),
        gateway_url=_value_or_default(args.gateway_url, defaults.gateway_url),
        api_key_env=args.api_key_env,
        dry_run=args.dry_run,
        max_agent_steps=max_agent_steps,
        idle_poll_limit=_value_or_default(args.idle_poll_limit, defaults.idle_poll_limit),
        puppet_ids=[s.player_id for s in specs],
    )
```

- [ ] **Step 6: Let `load_experiment()` accept supplied defaults**

In `src/civ_mcp/arena/experiment.py`, change the signature and initial defaults:

```python
def load_experiment(path: str | Path, defaults: ArenaConfig | None = None) -> ArenaConfig:
    config_path = Path(path)
    arena_defaults = defaults or _ARENA_DEFAULTS
```

Then replace `_ARENA_DEFAULTS` uses inside the return object with `arena_defaults`:

```python
    return ArenaConfig(
        players=players,
        max_puppet_turns=_top_int(
            config_path,
            "max_puppet_turns",
            data.get("max_puppet_turns", arena_defaults.max_puppet_turns),
        ),
        gateway_url=(
            arena_defaults.gateway_url
            if "gateway_url" not in data
            else _non_blank_string(str(config_path), "gateway_url", data["gateway_url"])
        ),
        idle_poll_limit=_top_int(
            config_path,
            "idle_poll_limit",
            data.get("idle_poll_limit", arena_defaults.idle_poll_limit),
        ),
        puppet_ids=ids,
        run_id=(
            arena_defaults.run_id
            if "run_id" not in data
            else _run_id_string(str(config_path), data["run_id"])
        ),
    )
```

The `_run_id_string` helper is added in Task 3; if Task 1 is implemented first, temporarily keep `_string(str(config_path), "run_id", data["run_id"])` and replace it in Task 3.

- [ ] **Step 7: Update existing tests expecting parser defaults**

In `tests/arena/test_arena_wiring.py`, change parser-default assertions so they assert resolved config defaults rather than raw argparse defaults. For example:

```python
def test_build_args_accepts_idle_poll_limit():
    args = build_args(["--idle-poll-limit", "12"])
    assert args.idle_poll_limit == 12


def test_resolve_config_non_config_uses_arena_defaults():
    cfg = resolve_config(build_args(["--player", "3:local:m"]))
    assert cfg.max_puppet_turns == 1
    assert cfg.idle_poll_limit == 600
    assert cfg.gateway_url == DEFAULT_GATEWAY_URL
    assert cfg.max_agent_steps == 6
```

- [ ] **Step 8: Run task tests**

Run:

```bash
/home/riz/.local/bin/uv run pytest tests/arena/test_arena_wiring.py tests/arena/test_experiment.py -q
```

Expected: all tests in those two files pass.

- [ ] **Step 9: Commit**

```bash
git add src/civ_mcp/arena/arena.py src/civ_mcp/arena/experiment.py tests/arena/test_arena_wiring.py tests/arena/test_experiment.py
git commit -m "fix(arena): detect config flag presence and support launcher defaults"
```

### Task 2: Watcher Config Argument Forwarding And Run IDs

**Files:**
- Modify: `tools/skills/civ6-arena-live/scripts/start-hybrid-watch.sh`
- Test: `tools/skills/civ6-arena-live/scripts/start-hybrid-watch.sh`

- [ ] **Step 1: Add a local dry-run escape hatch to inspect generated arena args without SSH**

In `tools/skills/civ6-arena-live/scripts/start-hybrid-watch.sh`, add this after `usage()`:

```bash
emit_dry_run_args() {
  printf '%s\n' "${arena_args[@]}"
}
```

Add a parser variable near the other initial variables:

```bash
dry_run_args=0
run_id_supplied=0
```

Add this parser branch before `-h|--help)`:

```bash
    --dry-run-args)
      dry_run_args=1; shift ;;
```

Update the `--run-id` branch so it records explicit user intent:

```bash
    --run-id)
      [[ $# -ge 2 ]] || { echo "error: --run-id requires an argument" >&2; exit 1; }
      run_id_supplied=1
      run_id="$2"; shift 2 ;;
```

Add this hidden option to the usage text under Options:

```text
  --dry-run-args          Print civ-arena arguments and exit before SSH
```

- [ ] **Step 2: Change config-mode arena args**

Replace the config branch that currently sets `arena_args=("--config" "$config_path" "--run-id" "$run_id")` with:

```bash
if [[ "$config_supplied" -eq 1 ]]; then
  arena_args=(
    "--config" "$config_path"
    "--config-default-max-puppet-turns" "$max_puppet_turns"
    "--config-default-idle-poll-limit" "$idle_poll_limit"
    "--config-default-gateway-url" "$gateway_url"
  )
  if [[ "$run_id_supplied" -eq 1 ]]; then
    arena_args+=("--run-id" "$run_id")
  fi
else
```

Leave the non-config branch forwarding `--run-id "$run_id"` as it does today.

- [ ] **Step 3: Keep launcher files named consistently without overriding YAML run_id**

Keep the local run id generation before SSH:

```bash
[[ -n "$run_id" ]] || run_id="hybrid-4civ-$(date -u +%Y%m%dT%H%M%SZ)"
```

This run id remains the watcher log/pidfile id. In config mode, it is not forwarded to `civ-arena` unless the user passed `--run-id`; a YAML `run_id` therefore reaches `arena.py`.

- [ ] **Step 4: Exit before SSH when dry-run-args is requested**

Insert this after `arena_args` is fully built and before `quoted_args=...`:

```bash
if [[ "$dry_run_args" -eq 1 ]]; then
  emit_dry_run_args
  exit 0
fi
```

- [ ] **Step 5: Run watcher syntax and argument checks**

Run:

```bash
bash -n tools/skills/civ6-arena-live/scripts/start-hybrid-watch.sh
```

Expected: exit 0.

Run:

```bash
tools/skills/civ6-arena-live/scripts/start-hybrid-watch.sh --config experiments/smoke-rich-gemma.yaml --dry-run-args
```

Expected output contains exactly one `--config`, contains the three hidden `--config-default-*` flags and their values, and does not contain `--run-id`.

Run:

```bash
tools/skills/civ6-arena-live/scripts/start-hybrid-watch.sh --config experiments/smoke-rich-gemma.yaml --run-id manual-run --dry-run-args
```

Expected output contains `--run-id` followed by `manual-run`.

Run:

```bash
tools/skills/civ6-arena-live/scripts/start-hybrid-watch.sh --config experiments/smoke-rich-gemma.yaml --max-puppet-turns 2 --dry-run-args
```

Expected: exit 1 and stderr includes `--config cannot be combined with config-owned override`.

- [ ] **Step 6: Commit**

```bash
git add tools/skills/civ6-arena-live/scripts/start-hybrid-watch.sh
git commit -m "fix(arena): preserve config run ids through watcher"
```

### Task 3: Strict Experiment Run ID Validation

**Files:**
- Modify: `src/civ_mcp/arena/experiment.py`
- Test: `tests/arena/test_experiment.py`

- [ ] **Step 1: Write failing run_id validation tests**

Append these tests to `tests/arena/test_experiment.py` near the existing run_id tests:

```python
@pytest.mark.parametrize(
    "bad",
    [
        'run_id: ""',
        'run_id: "   "',
        "run_id: ../outside",
        "run_id: nested/path",
        r"run_id: nested\path",
        "run_id: bad id",
        "run_id: .",
        "run_id: ..",
    ],
)
def test_rejects_unsafe_run_id_values(tmp_path, bad):
    with pytest.raises(ValueError, match="run_id"):
        load_experiment(_write(tmp_path, GOOD.replace("run_id: exp-1", bad)))
```

Append this positive test:

```python
@pytest.mark.parametrize("run_id", ["exp-1", "exp_1", "EXP.20260704T000000Z"])
def test_accepts_safe_run_id_values(tmp_path, run_id):
    cfg = load_experiment(_write(tmp_path, GOOD.replace("run_id: exp-1", f"run_id: {run_id}")))
    assert cfg.run_id == run_id
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
/home/riz/.local/bin/uv run pytest tests/arena/test_experiment.py::test_rejects_unsafe_run_id_values tests/arena/test_experiment.py::test_accepts_safe_run_id_values -q
```

Expected: unsafe values with blank/path/space forms are accepted by current code.

- [ ] **Step 3: Implement `_run_id_string()`**

In `src/civ_mcp/arena/experiment.py`, add `import re` near the top:

```python
import re
```

Add this constant near the defaults:

```python
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
```

Add this helper after `_non_blank_string()`:

```python
def _run_id_string(scope: str, value: object) -> str:
    parsed = _non_blank_string(scope, "run_id", value)
    if parsed in {".", ".."} or not _RUN_ID_RE.fullmatch(parsed):
        raise ValueError(
            f"experiment config: {scope}: run_id must contain only letters, numbers, '.', '_', or '-' "
            "and must not be '.' or '..'"
        )
    return parsed
```

In `load_experiment()`, parse run_id through the helper:

```python
        run_id=(
            arena_defaults.run_id
            if "run_id" not in data
            else _run_id_string(str(config_path), data["run_id"])
        ),
```

- [ ] **Step 4: Run task tests**

Run:

```bash
/home/riz/.local/bin/uv run pytest tests/arena/test_experiment.py -q
```

Expected: all `test_experiment.py` tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/experiment.py tests/arena/test_experiment.py
git commit -m "fix(arena): validate experiment run ids"
```

### Task 4: Context Probe Retry Semantics

**Files:**
- Modify: `src/civ_mcp/arena/agent.py`
- Test: `tests/arena/test_agent.py`

- [ ] **Step 1: Write failing test for retrying default auto probe fallback**

Append this test near `test_n_ctx_resolved_once_across_turns` in `tests/arena/test_agent.py`:

```python
@pytest.mark.asyncio
async def test_n_ctx_default_fallback_retries_on_next_turn(monkeypatch):
    from civ_mcp.arena import agent as agent_mod
    from civ_mcp.arena.briefing import Briefing

    calls = []

    async def fake_resolve(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) == 1:
            return 16384, "default"
        return 131072, "upstream_props"

    async def fake_build(gs, opts, budget):
        return Briefing(text="B", tokens=1)

    monkeypatch.setattr(agent_mod, "resolve_n_ctx", fake_resolve)
    monkeypatch.setattr(agent_mod, "build_briefing", fake_build)

    be = SpyBackend([_no_tool_reply(), _no_tool_reply()])
    be.base_url = "http://h:1/v1"
    pol = LLMPolicy(
        be,
        FakeCost(),
        options=CivOptions(briefing=BriefingOptions(enabled=True)),
    )

    first = await pol(None, 3, 7)
    second = await pol(None, 3, 8)

    assert len(calls) == 2
    assert first["transcript"]["n_ctx_source"] == "default"
    assert second["transcript"]["n_ctx"] == 131072
    assert second["transcript"]["n_ctx_source"] == "upstream_props"
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
/home/riz/.local/bin/uv run pytest tests/arena/test_agent.py::test_n_ctx_default_fallback_retries_on_next_turn tests/arena/test_agent.py::test_n_ctx_resolved_once_across_turns -q
```

Expected: the new retry test fails because the second turn does not call `resolve_n_ctx()`.

- [ ] **Step 3: Retry only uncached auto defaults**

In `src/civ_mcp/arena/agent.py`, add this helper near `SYSTEM`:

```python
def _should_resolve_n_ctx(current: int | None, source: str, context_budget: int | str) -> bool:
    if current is None:
        return True
    return context_budget == "auto" and source == "default"
```

Replace the current `if self._n_ctx is None:` block in `LLMPolicy.__call__()` with:

```python
            if _should_resolve_n_ctx(
                self._n_ctx,
                self._n_ctx_source,
                self.options.context_budget,
            ):
                self._n_ctx, self._n_ctx_source = await resolve_n_ctx(
                    getattr(self.backend, "base_url", ""),
                    getattr(self.backend, "model", ""),
                    self.options.context_budget,
                )
```

- [ ] **Step 4: Run task tests**

Run:

```bash
/home/riz/.local/bin/uv run pytest tests/arena/test_agent.py -q
```

Expected: all `test_agent.py` tests pass; `test_n_ctx_resolved_once_across_turns` still confirms successful probes are cached.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/agent.py tests/arena/test_agent.py
git commit -m "fix(arena): retry failed context probes"
```

### Task 5: Tool Registry Map Radius Clamp

**Files:**
- Modify: `src/civ_mcp/arena/registry.py`
- Test: `tests/arena/test_registry.py`

- [ ] **Step 1: Write failing schema and dispatch tests**

Append these tests to `tests/arena/test_registry.py`:

```python
def test_get_map_area_radius_schema_is_bounded():
    (tool,) = openai_tools(["get_map_area"])
    radius = tool["function"]["parameters"]["properties"]["radius"]

    assert radius["type"] == "integer"
    assert radius["minimum"] == 0
    assert radius["maximum"] == 5


@pytest.mark.asyncio
async def test_get_map_area_radius_clamped_before_game_state():
    calls = []

    class FakeGS:
        async def get_map_area(self, x, y, radius):
            calls.append((x, y, radius))
            return []

    await dispatch(FakeGS(), "get_map_area", {"x": 1, "y": 2, "radius": 99})
    await dispatch(FakeGS(), "get_map_area", {"x": 1, "y": 2, "radius": -3})
    await dispatch(FakeGS(), "get_map_area", {"x": 1, "y": 2})

    assert calls == [(1, 2, 5), (1, 2, 0), (1, 2, 2)]
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
/home/riz/.local/bin/uv run pytest tests/arena/test_registry.py::test_get_map_area_radius_schema_is_bounded tests/arena/test_registry.py::test_get_map_area_radius_clamped_before_game_state -q
```

Expected: schema lacks `minimum`/`maximum`, and dispatch forwards radius `99`.

- [ ] **Step 3: Implement bounded integer schema and runtime clamp**

In `src/civ_mcp/arena/registry.py`, replace `_int_param()` with:

```python
def _int_param(
    description: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> dict[str, Any]:
    param: dict[str, Any] = {"type": "integer", "description": description}
    if minimum is not None:
        param["minimum"] = minimum
    if maximum is not None:
        param["maximum"] = maximum
    return param
```

Add constants/helpers near `_coerce_policy_assignments()`:

```python
_MAP_RADIUS_DEFAULT = 2
_MAP_RADIUS_MIN = 0
_MAP_RADIUS_MAX = 5


def _clamp_map_radius(value: Any) -> int:
    radius = int(value)
    return max(_MAP_RADIUS_MIN, min(radius, _MAP_RADIUS_MAX))
```

Change the `get_map_area` radius schema:

```python
            "radius": _int_param(
                "Search radius; defaults to 2 and is clamped to 0..5.",
                minimum=_MAP_RADIUS_MIN,
                maximum=_MAP_RADIUS_MAX,
            ),
```

Change `_narrate_map()`:

```python
async def _narrate_map(gs: Any, args: dict[str, Any]) -> str:
    radius = _clamp_map_radius(args.get("radius", _MAP_RADIUS_DEFAULT))
    return _render(
        await gs.get_map_area(args["x"], args["y"], radius),
        nr.narrate_map,
    )
```

- [ ] **Step 4: Run task tests**

Run:

```bash
/home/riz/.local/bin/uv run pytest tests/arena/test_registry.py -q
```

Expected: all `test_registry.py` tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/registry.py tests/arena/test_registry.py
git commit -m "fix(arena): clamp map radius in registry"
```

### Task 6: Out-Of-Tier Classification And Invalid-Rate Filtering

**Files:**
- Modify: `src/civ_mcp/arena/agent.py`
- Modify: `src/civ_mcp/arena/analyze.py`
- Test: `tests/arena/test_agent.py`
- Test: `tests/arena/test_analyze.py`

- [ ] **Step 1: Write failing policy classification test**

Append this test to `tests/arena/test_agent.py` near `test_transcript_payload`:

```python
class FakeBackendOutOfTier:
    def __init__(self):
        self.n = 0

    async def chat(self, messages, tools):
        self.n += 1
        if self.n == 1:
            return Reply(
                text=None,
                tool_calls=[
                    {"id": "tc1", "name": "get_map_area", "arguments": '{"x": 1, "y": 2}'},
                    {"id": "tc2", "name": "bogus_tool", "arguments": "{}"},
                ],
                prompt_tokens=10,
                completion_tokens=1,
            )
        return Reply(text="done", tool_calls=[], prompt_tokens=10, completion_tokens=1)


@pytest.mark.asyncio
async def test_policy_distinguishes_out_of_tier_from_unknown_tool():
    gs, cost = FakeGS(), FakeCost()
    pol = LLMPolicy(FakeBackendOutOfTier(), cost, options=CivOptions(tools="minimal"))
    out = await pol(gs, player_id=1, turn=3)

    invalid = out["transcript"]["invalid_tool_calls"]
    assert {"tool_name": "get_map_area", "arguments": '{"x": 1, "y": 2}', "reason": "out_of_tier"} in invalid
    assert {"tool_name": "bogus_tool", "arguments": "{}", "reason": "unknown_tool"} in invalid
```

- [ ] **Step 2: Write failing analysis filtering test**

Append this test to `tests/arena/test_analyze.py` near the hallucinated-tools tests:

```python
def test_out_of_tier_calls_do_not_count_as_invalid_rate_or_hallucination():
    from civ_mcp.arena.analyze import analyze

    records = [
        {
            "player_id": 1,
            "model": "m",
            "provider": "local",
            "driver": "in_process",
            "turn": 1,
            "step_count": 2,
            "steps": [],
            "invalid_tool_calls": [
                {"tool_name": "get_map_area", "reason": "out_of_tier"},
            ],
        }
    ]

    report = analyze(records, [])

    assert report["by_player"][1]["rates"]["invalid_call_rate"] == 0.0
    assert report["by_player"][1]["rubric"]["hallucinated_tools"] is None
    assert report["config_summary"]["1"]["invalid_call_rate"] == 0.0
```

- [ ] **Step 3: Run tests to verify failure**

Run:

```bash
/home/riz/.local/bin/uv run pytest tests/arena/test_agent.py::test_policy_distinguishes_out_of_tier_from_unknown_tool tests/arena/test_analyze.py::test_out_of_tier_calls_do_not_count_as_invalid_rate_or_hallucination -q
```

Expected: policy labels `get_map_area` as `unknown_tool`, and analysis counts the out-of-tier call.

- [ ] **Step 4: Classify out-of-tier calls in `LLMPolicy`**

In `src/civ_mcp/arena/agent.py`, import `TOOL_REGISTRY`:

```python
from civ_mcp.arena.registry import (
    TOOL_REGISTRY,
    dispatch as _registry_dispatch,
    openai_tools,
    resolve_tools,
)
```

Replace the invalid-call classification block with:

```python
                if tc["name"] not in self._tool_names:
                    reason = "out_of_tier" if tc["name"] in TOOL_REGISTRY else "unknown_tool"
                    invalid_tool_calls.append({"tool_name": tc["name"], "arguments": tc["arguments"],
                                               "reason": reason})
                else:
                    try:
                        json.loads(tc["arguments"] or "{}")
                    except (json.JSONDecodeError, ValueError):
                        invalid_tool_calls.append({"tool_name": tc["name"], "arguments": tc["arguments"],
                                                   "reason": "bad_arguments"})
```

- [ ] **Step 5: Filter out-of-tier calls from invalid-rate metrics and hallucination rubric**

In `src/civ_mcp/arena/analyze.py`, add helper functions after `_steps_of()`:

```python
def _counted_invalid_calls(rec: dict) -> list[dict]:
    counted = []
    for item in rec.get("invalid_tool_calls") or []:
        if not isinstance(item, dict):
            counted.append(item)
            continue
        if item.get("reason") == "out_of_tier":
            continue
        counted.append(item)
    return counted
```

In `config_summary()`, replace:

```python
            total_invalid += len(rec.get("invalid_tool_calls") or [])
```

with:

```python
            total_invalid += len(_counted_invalid_calls(rec))
```

In `analyze()`, replace:

```python
            invalid_calls: list = rec.get("invalid_tool_calls") or []
```

with:

```python
            invalid_calls: list = _counted_invalid_calls(rec)
```

In `_rubric_for_model()`, replace:

```python
        invalid_calls: list[dict] = rec.get("invalid_tool_calls") or []
```

with:

```python
        invalid_calls: list[dict] = _counted_invalid_calls(rec)
```

Keep the hallucination rubric check as `reason == "unknown_tool"`.

- [ ] **Step 6: Run task tests**

Run:

```bash
/home/riz/.local/bin/uv run pytest tests/arena/test_agent.py tests/arena/test_analyze.py -q
```

Expected: all tests in both files pass.

- [ ] **Step 7: Commit**

```bash
git add src/civ_mcp/arena/agent.py src/civ_mcp/arena/analyze.py tests/arena/test_agent.py tests/arena/test_analyze.py
git commit -m "fix(arena): separate out-of-tier tool calls"
```

### Task 7: Briefing Map Expansion Budget Accounting

**Files:**
- Modify: `src/civ_mcp/arena/briefing.py`
- Test: `tests/arena/test_briefing.py`

- [ ] **Step 1: Write failing regression test for expanded map header overhead**

Append this test to `tests/arena/test_briefing.py` near the map-radius tests:

```python
@pytest.mark.asyncio
async def test_map_expansion_accounts_for_header_and_separator(monkeypatch):
    import civ_mcp.arena.briefing as briefing_mod

    class OneCenterGS:
        async def get_units(self):
            return [_unit(10, 10)]

        async def get_cities(self):
            return ([], [])

        async def get_map_area(self, x, y, radius):
            return [_tile(x + dx, y) for dx in range(radius)]

    async def fake_map_text(gs, centers, radius):
        if radius == 2:
            return "a" * 10
        if radius == 3:
            return "b" * 21
        raise AssertionError(f"unexpected radius {radius}")

    monkeypatch.setattr(briefing_mod, "_MAX_RADIUS", 3)
    monkeypatch.setattr(briefing_mod, "_map_text", fake_map_text)

    b = await build_briefing(
        OneCenterGS(),
        BriefingOptions(enabled=True, map_radius=2, sections=("map",)),
        10,  # 30 chars; header is 9 chars, radius 3 text would make 30 exactly
    )

    assert b.radius == 3
    assert len(b.text) == 30
    assert b.text == "== MAP ==\n" + ("b" * 21)
```

Add a second case for non-empty previous sections:

```python
@pytest.mark.asyncio
async def test_map_expansion_accounts_for_join_separator_after_prior_section(monkeypatch):
    import civ_mcp.arena.briefing as briefing_mod

    class PriorSectionGS(FakeGS):
        async def get_game_overview(self):
            return "OVERVIEW"

    async def fake_map_text(gs, centers, radius):
        if radius == 2:
            return "a" * 10
        if radius == 3:
            return "b" * 20
        raise AssertionError(f"unexpected radius {radius}")

    monkeypatch.setattr(briefing_mod, "_MAX_RADIUS", 3)
    monkeypatch.setattr(briefing_mod, "_map_text", fake_map_text)

    b = await build_briefing(
        PriorSectionGS(),
        BriefingOptions(enabled=True, map_radius=2, sections=("overview", "map")),
        14,
    )

    assert len(b.text) <= 42
    assert b.radius == 2
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
/home/riz/.local/bin/uv run pytest tests/arena/test_briefing.py::test_map_expansion_accounts_for_header_and_separator tests/arena/test_briefing.py::test_map_expansion_accounts_for_join_separator_after_prior_section -q
```

Expected: current expansion accepts radius 3 using only `len(larger)` and produces a partial/incorrect result for the separator case.

- [ ] **Step 3: Pass map-section overhead into `_map()`**

In `src/civ_mcp/arena/briefing.py`, change the `_map()` call in `build_briefing()` from:

```python
                text, radius = await _map(
                    gs, ctx, opts, len("\n".join(parts)), char_budget
                )
```

to:

```python
                map_prefix_used = len(_join_with(parts, "== MAP ==\n"))
                text, radius = await _map(gs, ctx, opts, map_prefix_used, char_budget)
```

Leave `_map()` comparing `used + len(larger) <= budget`; after this change, `used` already includes prior text, a join separator when needed, and the map header.

- [ ] **Step 4: Run task tests**

Run:

```bash
/home/riz/.local/bin/uv run pytest tests/arena/test_briefing.py -q
```

Expected: all `test_briefing.py` tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/briefing.py tests/arena/test_briefing.py
git commit -m "fix(arena): account for map briefing header budget"
```

### Task 8: Config Summary Fingerprint Splitting

**Files:**
- Modify: `src/civ_mcp/arena/analyze.py`
- Test: `tests/arena/test_analyze.py`

- [ ] **Step 1: Write failing test for same-player mixed fingerprints**

Append this test near the existing `config_summary` tests in `tests/arena/test_analyze.py`:

```python
def test_config_summary_splits_same_player_when_fingerprint_changes() -> None:
    from civ_mcp.arena.analyze import config_summary

    records = [
        {
            "player_id": 3,
            "model": "m-a",
            "provider": "local",
            "civ_options": {"tools": "minimal", "max_steps": 6},
            "n_ctx": 16384,
            "step_count": 2,
            "invalid_tool_calls": [],
            "briefing_tokens": 0,
            "state_delta": {"score": 1},
        },
        {
            "player_id": 3,
            "model": "m-b",
            "provider": "local",
            "civ_options": {"tools": "standard", "max_steps": 10},
            "n_ctx": 131072,
            "step_count": 4,
            "invalid_tool_calls": [],
            "briefing_tokens": 1200,
            "state_delta": {"score": 3},
        },
    ]

    summary = config_summary(records)

    assert set(summary) == {"3#1", "3#2"}
    assert summary["3#1"]["model"] == "m-a"
    assert summary["3#1"]["civ_options"]["tools"] == "minimal"
    assert summary["3#1"]["turns"] == 1
    assert summary["3#2"]["model"] == "m-b"
    assert summary["3#2"]["civ_options"]["tools"] == "standard"
    assert summary["3#2"]["turns"] == 1
```

Append this guard to preserve existing single-fingerprint labels:

```python
def test_config_summary_keeps_plain_player_key_for_single_fingerprint() -> None:
    from civ_mcp.arena.analyze import config_summary

    records = [
        {
            "player_id": 3,
            "model": "m-a",
            "provider": "local",
            "civ_options": {"tools": "minimal"},
            "n_ctx": 16384,
            "step_count": 2,
            "invalid_tool_calls": [],
        },
        {
            "player_id": 3,
            "model": "m-a",
            "provider": "local",
            "civ_options": {"tools": "minimal"},
            "n_ctx": 16384,
            "step_count": 3,
            "invalid_tool_calls": [],
        },
    ]

    summary = config_summary(records)

    assert set(summary) == {"3"}
    assert summary["3"]["turns"] == 2
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
/home/riz/.local/bin/uv run pytest tests/arena/test_analyze.py::test_config_summary_splits_same_player_when_fingerprint_changes tests/arena/test_analyze.py::test_config_summary_keeps_plain_player_key_for_single_fingerprint -q
```

Expected: the split test fails because only key `"3"` is present and it uses the last fingerprint.

- [ ] **Step 3: Implement stable config fingerprints**

Add these helpers after `_config_summary_sort_key()` in `src/civ_mcp/arena/analyze.py`:

```python
def _json_fingerprint(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _config_fingerprint(rec: dict) -> dict:
    return {
        "model": rec.get("model", ""),
        "provider": rec.get("provider", ""),
        "civ_options": rec.get("civ_options") or {},
        "n_ctx": rec.get("n_ctx"),
    }
```

Replace the grouping part of `config_summary()` with:

```python
    by_pid: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    fingerprints: dict[tuple[str, str], dict] = {}
    for rec in records:
        pid = _config_summary_group_key(rec)
        fingerprint = _config_fingerprint(rec)
        fingerprint_key = _json_fingerprint(fingerprint)
        by_pid[pid][fingerprint_key].append(rec)
        fingerprints[(pid, fingerprint_key)] = fingerprint

    summary: dict[str, dict] = {}
    for pid, groups in sorted(by_pid.items(), key=lambda item: _config_summary_sort_key(item[0])):
        ordered_groups = sorted(groups.items(), key=lambda item: item[0])
        for index, (fingerprint_key, recs) in enumerate(ordered_groups, start=1):
            summary_key = pid if len(ordered_groups) == 1 else f"{pid}#{index}"
            fingerprint = fingerprints[(pid, fingerprint_key)]
            total_steps = 0
            total_invalid = 0
            total_briefing_tokens = 0
            total_score_delta = 0

            for rec in recs:
                step_count = rec.get("step_count")
                if step_count is None:
                    step_count = len(_steps_of(rec))
                total_steps += step_count or 0
                total_invalid += len(_counted_invalid_calls(rec))
                total_briefing_tokens += rec.get("briefing_tokens") or 0
                total_score_delta += (rec.get("state_delta") or {}).get("score", 0) or 0

            turns = len(recs)
            summary[summary_key] = {
                "model": fingerprint.get("model", ""),
                "provider": fingerprint.get("provider", ""),
                "civ_options": fingerprint.get("civ_options") or {},
                "n_ctx": fingerprint.get("n_ctx"),
                "turns": turns,
                "avg_steps": total_steps / turns,
                "invalid_call_rate": (total_invalid / total_steps) if total_steps else 0.0,
                "avg_briefing_tokens": total_briefing_tokens / turns,
                "avg_score_delta": total_score_delta / turns,
            }
```

Remove the old `last = recs[-1]` block.

- [ ] **Step 4: Run task tests**

Run:

```bash
/home/riz/.local/bin/uv run pytest tests/arena/test_analyze.py -q
```

Expected: all `test_analyze.py` tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/analyze.py tests/arena/test_analyze.py
git commit -m "fix(arena): split config summaries by fingerprint"
```

### Task 9: Local Tool Verb Vocabulary Coverage

**Files:**
- Modify: `src/civ_mcp/arena/vocab.py`
- Test: `tests/arena/test_analyze.py`

- [ ] **Step 1: Write failing coverage tests for standard/full action tools**

Append this test near the existing `LOCAL_TOOL_VERBS` tests in `tests/arena/test_analyze.py`:

```python
def test_local_tool_verbs_cover_registry_action_tools() -> None:
    from civ_mcp.arena.registry import TOOL_REGISTRY
    from civ_mcp.arena.vocab import LOCAL_TOOL_VERBS

    expected_actions = {
        "move_unit",
        "found_city",
        "fortify_unit",
        "skip_unit",
        "attack_unit",
        "improve_tile",
        "remove_feature",
        "purchase_item",
        "heal_unit",
        "alert_unit",
        "set_civic",
        "send_envoy",
        "set_policies",
        "appoint_governor",
        "assign_governor",
        "choose_pantheon",
        "upgrade_unit",
        "promote_unit",
        "automate_explore",
        "skip_remaining_units",
        "purchase_tile",
        "set_city_focus",
    }

    assert expected_actions <= set(TOOL_REGISTRY)
    assert expected_actions <= set(LOCAL_TOOL_VERBS)
```

Append this rubric behavior test:

```python
def test_rubric_counts_automate_explore_as_exploration() -> None:
    from civ_mcp.arena.analyze import analyze

    report = analyze(
        [
            {
                "player_id": 1,
                "model": "m",
                "provider": "local",
                "driver": "in_process",
                "turn": 1,
                "steps": [
                    {
                        "tool_name": "automate_explore",
                        "tool_args": {"unit_index": 2},
                        "tool_result_full": "OK",
                    }
                ],
                "invalid_tool_calls": [],
            }
        ],
        [],
    )

    assert report["by_player"][1]["rubric"]["explored_vs_idle"] is not None
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
/home/riz/.local/bin/uv run pytest tests/arena/test_analyze.py::test_local_tool_verbs_cover_registry_action_tools tests/arena/test_analyze.py::test_rubric_counts_automate_explore_as_exploration -q
```

Expected: coverage test fails because most action tools are absent from `LOCAL_TOOL_VERBS`.

- [ ] **Step 3: Expand `LOCAL_TOOL_VERBS`**

Replace `LOCAL_TOOL_VERBS` in `src/civ_mcp/arena/vocab.py` with:

```python
LOCAL_TOOL_VERBS: dict[str, str] = {
    "move_unit": "move",
    "found_city": "found_city",
    "fortify_unit": "fortify",
    "skip_unit": "skip",
    "attack_unit": "attack",
    "improve_tile": "improve",
    "remove_feature": "remove_feature",
    "purchase_item": "purchase",
    "heal_unit": "heal",
    "alert_unit": "alert",
    "set_civic": "set_civic",
    "send_envoy": "send_envoy",
    "set_policies": "set_policies",
    "appoint_governor": "appoint_governor",
    "assign_governor": "assign_governor",
    "choose_pantheon": "choose_pantheon",
    "upgrade_unit": "upgrade",
    "promote_unit": "promote",
    "automate_explore": "automate",
    "skip_remaining_units": "skip",
    "purchase_tile": "purchase_tile",
    "set_city_focus": "set_city_focus",
}
```

- [ ] **Step 4: Run task tests**

Run:

```bash
/home/riz/.local/bin/uv run pytest tests/arena/test_analyze.py -q
```

Expected: all `test_analyze.py` tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/vocab.py tests/arena/test_analyze.py
git commit -m "fix(arena): cover rich local action vocabulary"
```

### Task 10: Run-ID Precedence Integration

**Files:**
- Modify: `src/civ_mcp/arena/arena.py`
- Test: `tests/arena/test_arena_wiring.py`

- [ ] **Step 1: Write tests for YAML run_id and explicit CLI run_id behavior**

Append these tests to `tests/arena/test_arena_wiring.py` near `test_run_uses_file_run_id_for_config`:

```python
def test_config_yaml_run_id_survives_when_cli_run_id_absent(tmp_path):
    p = tmp_path / "e.yaml"
    p.write_text("run_id: file-run\ncivs:\n  - {player: 3, provider: local, model: m}\n")

    args = build_args(["--config", str(p)])
    cfg = resolve_config(args)

    assert args.run_id is None
    assert cfg.run_id == "file-run"


def test_config_rejects_cli_run_id_when_yaml_run_id_present(tmp_path):
    p = tmp_path / "e.yaml"
    p.write_text("run_id: file-run\ncivs:\n  - {player: 3, provider: local, model: m}\n")

    with pytest.raises(SystemExit, match="--run-id"):
        resolve_config(build_args(["--config", str(p), "--run-id", "cli-run"]))
```

- [ ] **Step 2: Run tests to verify failure where applicable**

Run:

```bash
/home/riz/.local/bin/uv run pytest tests/arena/test_arena_wiring.py::test_config_yaml_run_id_survives_when_cli_run_id_absent tests/arena/test_arena_wiring.py::test_config_rejects_cli_run_id_when_yaml_run_id_present -q
```

Expected: the second test fails until `resolve_config()` checks explicit CLI run-id against YAML run-id.

- [ ] **Step 3: Enforce run-id precedence after config load**

In `src/civ_mcp/arena/arena.py`, after `cfg = load_experiment(config_path, defaults=config_defaults)` in `resolve_config()`, add:

```python
        if args.run_id and cfg.run_id:
            raise SystemExit("--config file run_id cannot be overridden by --run-id")
        if args.run_id:
            cfg.run_id = args.run_id
```

In `_run()`, leave precedence as:

```python
    run_id = args.run_id or cfg.run_id or generate_run_id()
```

This keeps explicit CLI run ids usable only when the YAML does not declare one; Task 2 keeps the watcher from passing an auto-generated CLI run id in config mode.

- [ ] **Step 4: Run task tests**

Run:

```bash
/home/riz/.local/bin/uv run pytest tests/arena/test_arena_wiring.py -q
```

Expected: all `test_arena_wiring.py` tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/arena.py tests/arena/test_arena_wiring.py
git commit -m "fix(arena): protect experiment run id precedence"
```

### Task 11: Final Verification

**Files:**
- No source edits unless a verification command exposes a defect.

- [ ] **Step 1: Run focused arena tests**

Run:

```bash
/home/riz/.local/bin/uv run pytest tests/arena/ -q
```

Expected: all arena tests pass.

- [ ] **Step 2: Run full test suite**

Run:

```bash
/home/riz/.local/bin/uv run pytest tests/ -q
```

Expected: all tests pass.

- [ ] **Step 3: Run diff whitespace check**

Run:

```bash
git diff --check main..HEAD
```

Expected: no output and exit 0.

- [ ] **Step 4: Validate experiment YAML loading**

Run:

```bash
/home/riz/.local/bin/uv run python -m civ_mcp.arena.experiment experiments/smoke-rich-gemma.yaml
/home/riz/.local/bin/uv run python -m civ_mcp.arena.experiment experiments/ab-minimal-vs-standard.yaml
```

Expected: both commands exit 0 with no output.

- [ ] **Step 5: Validate watcher parser behavior**

Run:

```bash
bash -n tools/skills/civ6-arena-live/scripts/start-hybrid-watch.sh
tools/skills/civ6-arena-live/scripts/start-hybrid-watch.sh --help
tools/skills/civ6-arena-live/scripts/start-hybrid-watch.sh --config '' --dry-run-args
tools/skills/civ6-arena-live/scripts/start-hybrid-watch.sh --config experiments/smoke-rich-gemma.yaml --dry-run-args
tools/skills/civ6-arena-live/scripts/start-hybrid-watch.sh --config experiments/smoke-rich-gemma.yaml --run-id manual-run --dry-run-args
tools/skills/civ6-arena-live/scripts/start-hybrid-watch.sh --config experiments/smoke-rich-gemma.yaml --max-puppet-turns 2 --dry-run-args
```

Expected:
- `bash -n` exits 0.
- `--help` exits 0 and shows `--config`.
- `--config '' --dry-run-args` exits 1 before SSH with the non-empty path error.
- Config dry-run args include hidden fallback defaults and omit `--run-id`.
- Explicit `--run-id manual-run` appears only when passed.
- Config plus `--max-puppet-turns 2` exits 1 before SSH with the config-owned override error.

- [ ] **Step 6: Inspect branch state**

Run:

```bash
git status --short
git log --oneline main..HEAD
```

Expected: only known unrelated dirty entries remain outside this work, and the new fix commits are listed after the existing arena-local-civ-context commits.

- [ ] **Step 7: Request final code review**

Use `superpowers:requesting-code-review` and ask the reviewer to verify:
- all 10 original findings are fixed,
- watcher config mode preserves YAML run ids,
- out-of-tier calls no longer bias minimal-tier A/B metrics,
- no analysis regressions were introduced by fingerprint splitting.

Expected: no blocking findings. If findings appear, use `superpowers:receiving-code-review` before changing code.

- [ ] **Step 8: Handle any verification fallout through its owning task**

If no source changes were made during final verification, do not create an empty commit. If a verification command exposes a defect, return to the task that owns the affected file, add a focused regression test beside the existing tests from that task, run that task's verification command, and commit the concrete source/test files with that task's commit pattern.

## Self-Review Notes

- Spec coverage: every one of the 10 confirmed findings maps to at least one task. Tasks 1, 2, and 10 cover config launch correctness; Task 3 covers run-id safety; Task 4 covers context probing; Task 5 covers map radius; Task 6 covers tier-gated invalid calls; Task 7 covers briefing budget fit; Task 8 covers append-mode config summaries; Task 9 covers local action vocabulary.
- Placeholder scan: no task contains TBD/TODO/fill-in steps. Every code-changing step includes concrete code or an exact replacement shape.
- Type consistency: new helpers use existing `ArenaConfig`, `CivOptions`, `BriefingOptions`, `TOOL_REGISTRY`, `LOCAL_TOOL_VERBS`, and pytest fixture names already present in the test files. The hidden CLI fallback flags use argparse names that become `config_default_max_puppet_turns`, `config_default_idle_poll_limit`, and `config_default_gateway_url`.
