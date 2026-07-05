# Arena Local Civ Context Review Fixes Implementation Plan

> **Status (2026-07-05): DONE on `arena-local-civ-context`.** Task commits:
> `c313b40` (briefing enablement), `e3aead5` (ArenaConfig defaults + config run-id
> validation), `ab52135` (default local `max_agent_steps`), `5009da4` (context-window
> reporting + top-level `n_ctx` parsing), `5486593` (briefing map radius telemetry +
> parallel fetches), and `8cd2730` (dynamic full tool tier + no generic clamp layer).
> Verification: `/home/riz/.local/bin/uv run pytest tests/arena -q` = 293 passed;
> `/home/riz/.local/bin/uv run pytest tests -q` = 397 passed. Full `pytest -q`
> still collects legacy script harnesses: `scripts/test_game_state.py` requires a live
> FireTuner connection at `127.0.0.1:4318`, and `scripts/test_queries.py` imports
> absent `civ_mcp.lua_queries`. The original no-push/no-merge implementation boundary
> was superseded by the operator request to commit, push, and merge this branch.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the ranked arena-local-civ-context review findings that can silently disable briefings, misreport context windows, mis-thread arena defaults, corrupt briefing telemetry, or hide newly registered tools.

**Architecture:** Keep the fixes in the existing arena modules. YAML parsing and default preservation stay in `experiment.py`, CLI/config boundary validation stays in `arena.py`, local policy construction stays in `arena.py`/`agent.py`, context probing stays in `budget.py`, report aggregation stays in `analyze.py`, briefing assembly stays in `briefing.py`, and tool exposure stays in `registry.py`. Each task adds a focused regression next to the current tests before changing production code.

**Tech Stack:** Python 3.12, pytest/pytest-asyncio, PyYAML, argparse, asyncio, existing `uv` test runner.

---

## Verified Scope

The review findings were checked against the current `arena-local-civ-context` checkout before writing this plan.

Accepted fixes:

1. A non-empty `briefing:` block without `enabled: true` currently validates but stays disabled.
2. `_representative_n_ctx()` reports `max(n_ctx)` and can prefer a cold default over the real smaller window.
3. Partial map briefing sections append text but leave `briefing_radius` at `0`.
4. `ArenaConfig.max_agent_steps` does not affect `build_policies()` when player specs carry default `CivOptions`.
5. `load_experiment(defaults=...)` preserves only part of the supplied `ArenaConfig` defaults.
6. `resolve_config()` can return an unsafe CLI `--run-id` in config mode.
7. `_n_ctx_from()` ignores top-level `n_ctx` payloads.
8. `_apply_param_bounds()` is a second, generic clamp layer competing with `get_map_area`'s own clamp.
9. Briefing map/production sections run their per-center and per-city fetches sequentially (parallelizable). The related map "throwaway" fetch is left as-is — see the Task 5 scope note.
10. `TIERS["full"]` is hand-listed and `resolve_tools("full")` can silently miss a future registry addition.

Out of scope for this plan:

- Cosmetic deduplication such as shared `_render()` helpers.
- Broader briefing section-builder reuse between `briefing.py` and `registry.py`.
- Changing analyzer semantics for intentionally excluded `out_of_tier` invalid-call rates.
- Rewriting `_map()`'s two-fetch radius projection into a one-fetch fixed-estimate form (reviewed and rejected: it drops the post-fetch budget guard and makes two regression tests vacuous for a marginal, arguably illusory efficiency gain — the first fetch is the measurement sample, not pure waste).

**Implementation boundary:** Implement these tasks on the existing `arena-local-civ-context` branch (or a worktree cut from it). Do NOT merge to `main` or push — leave the finished commits on the branch for a separate-session review, per the standing process. Each task's commit message carries no LLM attribution.

## File Structure

- `src/civ_mcp/arena/experiment.py`: briefing block parsing and default-preserving experiment config construction.
- `src/civ_mcp/arena/arena.py`: config-mode run-id validation and effective local policy options.
- `src/civ_mcp/arena/analyze.py`: representative context-window selection.
- `src/civ_mcp/arena/budget.py`: `/props` schema fallback for top-level `n_ctx`.
- `src/civ_mcp/arena/briefing.py`: partial map radius telemetry and parallel map/production fetches (map expansion projection unchanged).
- `src/civ_mcp/arena/registry.py`: remove generic parameter bounds and make full-tier resolution track the registry.
- `tests/arena/test_experiment.py`: YAML parsing/default regressions.
- `tests/arena/test_arena_wiring.py`: CLI/config boundary and policy wiring regressions.
- `tests/arena/test_analyze.py`: context-window reporting regression.
- `tests/arena/test_budget.py`: top-level `n_ctx` payload regression.
- `tests/arena/test_briefing.py`: partial-map metadata and map refetch regressions.
- `tests/arena/test_registry.py`: registry clamp ownership and full-tier drift regressions.

### Task 1: Make Configured Briefing Blocks Enable Briefings

**Files:**
- Modify: `src/civ_mcp/arena/experiment.py`
- Test: `tests/arena/test_experiment.py`

- [ ] **Step 1: Add failing tests for implicit and explicit briefing enablement**

Append these tests after `test_load_good()` in `tests/arena/test_experiment.py`:

```python
def test_non_empty_briefing_block_defaults_enabled_true(tmp_path):
    text = GOOD.replace(
        "briefing: {enabled: true, map_radius: 4, sections: [overview, units, map]}",
        "briefing: {map_radius: 4, sections: [overview, map, rivals]}",
    )

    cfg = load_experiment(_write(tmp_path, text))
    briefing = cfg.players[0].options.briefing

    assert briefing.enabled is True
    assert briefing.map_radius == 4
    assert briefing.sections == ("overview", "map", "rivals")


def test_briefing_block_explicit_enabled_false_stays_disabled(tmp_path):
    text = GOOD.replace(
        "briefing: {enabled: true, map_radius: 4, sections: [overview, units, map]}",
        "briefing: {enabled: false, map_radius: 4, sections: [overview, map]}",
    )

    cfg = load_experiment(_write(tmp_path, text))
    briefing = cfg.players[0].options.briefing

    assert briefing.enabled is False
    assert briefing.map_radius == 4
    assert briefing.sections == ("overview", "map")
```

- [ ] **Step 2: Run the new tests and confirm the current bug**

Run:

```bash
/home/riz/.local/bin/uv run pytest tests/arena/test_experiment.py::test_non_empty_briefing_block_defaults_enabled_true tests/arena/test_experiment.py::test_briefing_block_explicit_enabled_false_stays_disabled -q
```

Expected: `test_non_empty_briefing_block_defaults_enabled_true` fails because `briefing.enabled` is `False`; the explicit false test should pass.

- [ ] **Step 3: Default non-empty briefing mappings to enabled**

In `src/civ_mcp/arena/experiment.py`, replace this line inside `_parse_briefing()`:

```python
    enabled = raw.get("enabled", _BRIEFING_DEFAULTS.enabled)
```

with:

```python
    enabled = raw.get("enabled", bool(raw))
```

Keep the existing boolean validation directly below it:

```python
    if "enabled" in raw and not isinstance(enabled, bool):
        raise _err(civ_label, f"briefing.enabled must be a boolean, got {enabled!r}")
```

- [ ] **Step 4: Run the task tests**

Run:

```bash
/home/riz/.local/bin/uv run pytest tests/arena/test_experiment.py::test_non_empty_briefing_block_defaults_enabled_true tests/arena/test_experiment.py::test_briefing_block_explicit_enabled_false_stays_disabled -q
```

Expected: both tests pass.

- [ ] **Step 5: Run the surrounding experiment parser tests**

Run:

```bash
/home/riz/.local/bin/uv run pytest tests/arena/test_experiment.py -q
```

Expected: all `tests/arena/test_experiment.py` tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/civ_mcp/arena/experiment.py tests/arena/test_experiment.py
git commit -m "fix: enable configured arena briefings by default"
```

### Task 2: Preserve Experiment Defaults And Validate Config Run IDs

**Files:**
- Modify: `src/civ_mcp/arena/experiment.py`
- Modify: `src/civ_mcp/arena/arena.py`
- Test: `tests/arena/test_experiment.py`
- Test: `tests/arena/test_arena_wiring.py`

- [ ] **Step 1: Add a failing test that `load_experiment()` preserves every omitted `ArenaConfig` default**

Append this test near `test_load_experiment_uses_supplied_defaults_for_omitted_run_controls()` in `tests/arena/test_experiment.py`:

```python
def test_load_experiment_preserves_all_supplied_defaults_for_omitted_arena_fields(tmp_path):
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
            gateway_url="http://launcher.example/v1",
            api_key_env="LOCAL_ARENA_KEY",
            dry_run=True,
            max_agent_steps=3,
            idle_poll_limit=3600,
            cost_path="custom-cost.jsonl",
            run_id="default-run",
            transcript_dir="custom-runs",
        ),
    )

    assert [p.player_id for p in cfg.players] == [3]
    assert cfg.puppet_ids == [3]
    assert cfg.max_puppet_turns == 8
    assert cfg.gateway_url == "http://launcher.example/v1"
    assert cfg.api_key_env == "LOCAL_ARENA_KEY"
    assert cfg.dry_run is True
    assert cfg.max_agent_steps == 3
    assert cfg.idle_poll_limit == 3600
    assert cfg.cost_path == "custom-cost.jsonl"
    assert cfg.run_id == "default-run"
    assert cfg.transcript_dir == "custom-runs"
```

- [ ] **Step 2: Add a failing test for unsafe config-mode CLI run IDs at `resolve_config()`**

Append this test near the existing config run-id tests in `tests/arena/test_arena_wiring.py`:

```python
def test_config_rejects_unsafe_cli_run_id_at_resolve_boundary(tmp_path):
    p = tmp_path / "e.yaml"
    p.write_text("civs:\n  - {player: 3, provider: local, model: m}\n")

    with pytest.raises(SystemExit, match="invalid run_id"):
        resolve_config(build_args(["--config", str(p), "--run-id", "../../evil"]))
```

- [ ] **Step 3: Add a failing test for config-mode runtime fields returned by `resolve_config()`**

Append this test near `test_config_yaml_run_id_survives_when_cli_run_id_absent()` in `tests/arena/test_arena_wiring.py`:

```python
def test_config_resolve_threads_runtime_fields_into_cfg(tmp_path):
    p = tmp_path / "e.yaml"
    cost_path = tmp_path / "cost.jsonl"
    transcript_dir = tmp_path / "runs"
    p.write_text("civs:\n  - {player: 3, provider: local, model: m}\n")

    cfg = resolve_config(
        build_args(
            [
                "--config",
                str(p),
                "--api-key-env",
                "LOCAL_ARENA_KEY",
                "--dry-run",
                "--cost-path",
                str(cost_path),
                "--transcript-dir",
                str(transcript_dir),
            ]
        )
    )

    assert cfg.api_key_env == "LOCAL_ARENA_KEY"
    assert cfg.dry_run is True
    assert cfg.cost_path == str(cost_path)
    assert cfg.transcript_dir == str(transcript_dir)
```

- [ ] **Step 4: Run the new tests and confirm failures**

Run:

```bash
/home/riz/.local/bin/uv run pytest tests/arena/test_experiment.py::test_load_experiment_preserves_all_supplied_defaults_for_omitted_arena_fields tests/arena/test_arena_wiring.py::test_config_rejects_unsafe_cli_run_id_at_resolve_boundary tests/arena/test_arena_wiring.py::test_config_resolve_threads_runtime_fields_into_cfg -q
```

Expected: failures for unpreserved defaults, unsafe `cfg.run_id`, and missing `cost_path`/`transcript_dir` in the resolved config.

- [ ] **Step 5: Preserve defaults with `dataclasses.replace()` in `load_experiment()`**

In `src/civ_mcp/arena/experiment.py`, add this import:

```python
from dataclasses import replace
```

Then replace the final `return ArenaConfig(...)` block in `load_experiment()` with:

```python
    return replace(
        arena_defaults,
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

- [ ] **Step 6: Add a shared run-id validation helper in `arena.py`**

In `src/civ_mcp/arena/arena.py`, add this helper near `_value_or_default()`:

```python
def _require_safe_run_id(run_id: str) -> None:
    from civ_mcp.run_id import is_safe_run_id

    if not is_safe_run_id(run_id):
        raise SystemExit(
            f"invalid run_id {run_id!r}: must contain only letters, numbers, '.', '_', or '-' "
            "and must not be '.' or '..'"
        )
```

Then, in the config path of `resolve_config()`, replace:

```python
        if args.run_id:
            cfg.run_id = args.run_id
        cfg.dry_run = args.dry_run
        cfg.api_key_env = args.api_key_env
        return cfg
```

with:

```python
        if args.run_id:
            _require_safe_run_id(args.run_id)
            cfg.run_id = args.run_id
        cfg.dry_run = args.dry_run
        cfg.api_key_env = args.api_key_env
        cfg.cost_path = args.cost_path or defaults.cost_path
        cfg.transcript_dir = args.transcript_dir
        return cfg
```

- [ ] **Step 7: Reuse the helper in `_run()`**

In `src/civ_mcp/arena/arena.py`, change the `_run()` import from:

```python
    from civ_mcp.run_id import generate_run_id, is_safe_run_id
```

to:

```python
    from civ_mcp.run_id import generate_run_id
```

Then replace the existing `_run()` validation block:

```python
    if not is_safe_run_id(run_id):
        raise SystemExit(
            f"invalid run_id {run_id!r}: must contain only letters, numbers, '.', '_', or '-' "
            "and must not be '.' or '..'"
        )
```

with:

```python
    _require_safe_run_id(run_id)
```

- [ ] **Step 8: Run the task tests**

Run:

```bash
/home/riz/.local/bin/uv run pytest tests/arena/test_experiment.py::test_load_experiment_preserves_all_supplied_defaults_for_omitted_arena_fields tests/arena/test_arena_wiring.py::test_config_rejects_unsafe_cli_run_id_at_resolve_boundary tests/arena/test_arena_wiring.py::test_config_resolve_threads_runtime_fields_into_cfg -q
```

Expected: all three tests pass.

- [ ] **Step 9: Run the surrounding config tests**

Run:

```bash
/home/riz/.local/bin/uv run pytest tests/arena/test_experiment.py tests/arena/test_arena_wiring.py -q
```

Expected: both files pass.

- [ ] **Step 10: Commit**

```bash
git add src/civ_mcp/arena/experiment.py src/civ_mcp/arena/arena.py tests/arena/test_experiment.py tests/arena/test_arena_wiring.py
git commit -m "fix: preserve arena config defaults"
```

### Task 3: Make Programmatic `max_agent_steps` Affect Default Local Specs

**Files:**
- Modify: `src/civ_mcp/arena/arena.py`
- Test: `tests/arena/test_arena_wiring.py`

- [ ] **Step 1: Add a failing policy wiring test**

Append this test after `test_build_policies_threads_options()` in `tests/arena/test_arena_wiring.py`:

```python
def test_build_policies_uses_arena_max_agent_steps_for_default_local_options(tmp_path):
    spec = parse_player_spec("3:local:m")
    cfg = ArenaConfig(players=[spec], max_agent_steps=2)
    cost = CostLog(str(tmp_path / "c.jsonl"))

    policies, _backends = build_policies([spec], cost, cfg)
    pol = policies[3]

    assert pol.max_steps == 2
    assert pol.options.max_steps == 2
```

- [ ] **Step 2: Run the new test and confirm the current bug**

Run:

```bash
/home/riz/.local/bin/uv run pytest tests/arena/test_arena_wiring.py::test_build_policies_uses_arena_max_agent_steps_for_default_local_options -q
```

Expected: the test fails because `pol.max_steps` is `6`.

- [ ] **Step 3: Import `CivOptions` in `arena.py`**

In `src/civ_mcp/arena/arena.py`, change the arena config import block to include `CivOptions`:

```python
from civ_mcp.arena.config import (
    ArenaConfig,
    CivOptions,
    CLI_PROVIDER_COMMANDS,
    parse_player_spec,
    DEFAULT_GATEWAY_URL,
)
```

- [ ] **Step 4: Thread `cfg.max_agent_steps` only when the spec has default local options**

In `src/civ_mcp/arena/arena.py`, replace the local-policy construction at the end of `build_policies()`:

```python
            local_backends.append(backend)
            policies[spec.player_id] = LLMPolicy(
                backend, cost, max_steps=cfg.max_agent_steps, options=spec.options)
```

with:

```python
            local_backends.append(backend)
            options = spec.options
            if (
                cfg.max_agent_steps != _ARENA_DEFAULTS.max_agent_steps
                and options.max_steps == CivOptions().max_steps
            ):
                options = replace(options, max_steps=cfg.max_agent_steps)
            policies[spec.player_id] = LLMPolicy(backend, cost, options=options)
```

This keeps per-civ YAML `max_steps` authoritative while making `ArenaConfig(max_agent_steps=...)` work for default-option specs.

- [ ] **Step 5: Run targeted policy wiring tests**

Run:

```bash
/home/riz/.local/bin/uv run pytest tests/arena/test_arena_wiring.py::test_build_policies_threads_options tests/arena/test_arena_wiring.py::test_build_policies_uses_arena_max_agent_steps_for_default_local_options tests/arena/test_arena_wiring.py::test_player_shorthand_honors_max_agent_steps -q
```

Expected: all three tests pass.

- [ ] **Step 6: Run the surrounding arena wiring tests**

Run:

```bash
/home/riz/.local/bin/uv run pytest tests/arena/test_arena_wiring.py -q
```

Expected: all arena wiring tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/civ_mcp/arena/arena.py tests/arena/test_arena_wiring.py
git commit -m "fix: honor arena max agent steps for local specs"
```

### Task 4: Report Real Context Windows And Parse Top-Level `n_ctx`

**Files:**
- Modify: `src/civ_mcp/arena/analyze.py`
- Modify: `src/civ_mcp/arena/budget.py`
- Test: `tests/arena/test_analyze.py`
- Test: `tests/arena/test_budget.py`

- [ ] **Step 1: Add a failing analyzer test for cold-default masking**

Append this test near `test_config_summary_does_not_split_on_mid_run_n_ctx_change()` in `tests/arena/test_analyze.py`:

```python
def test_config_summary_prefers_latest_non_default_n_ctx_over_cold_default() -> None:
    from civ_mcp.arena.analyze import config_summary

    records = [
        {
            "player_id": 3,
            "model": "m",
            "provider": "local",
            "civ_options": {"tools": "minimal"},
            "n_ctx": 16384,
            "n_ctx_source": "default",
            "step_count": 1,
            "invalid_tool_calls": [],
        },
        {
            "player_id": 3,
            "model": "m",
            "provider": "local",
            "civ_options": {"tools": "minimal"},
            "n_ctx": 8192,
            "n_ctx_source": "upstream_props",
            "step_count": 1,
            "invalid_tool_calls": [],
        },
    ]

    summary = config_summary(records)

    assert summary["3"]["n_ctx"] == 8192
```

- [ ] **Step 2: Add a failing budget test for top-level props schema**

Append this test after `test_auto_falls_back_to_bare_props_then_default()` in `tests/arena/test_budget.py`:

```python
@pytest.mark.asyncio
async def test_auto_accepts_top_level_n_ctx_from_props():
    async def top_level(url):
        return {"n_ctx": 4096}

    n, src = await resolve_n_ctx("http://h:1/v1", "m", "auto", http_get=top_level)

    assert (n, src) == (4096, "upstream_props")
```

- [ ] **Step 3: Run the new tests and confirm failures**

Run:

```bash
/home/riz/.local/bin/uv run pytest tests/arena/test_analyze.py::test_config_summary_prefers_latest_non_default_n_ctx_over_cold_default tests/arena/test_budget.py::test_auto_accepts_top_level_n_ctx_from_props -q
```

Expected: the analyzer test reports `16384`, and the budget test falls back to the default instead of `4096`.

- [ ] **Step 4: Prefer the latest non-default `n_ctx` in `analyze.py`**

In `src/civ_mcp/arena/analyze.py`, replace `_representative_n_ctx()` with:

```python
def _representative_n_ctx(recs: list[dict]) -> int | None:
    """Pick the n_ctx to report for a group.

    Prefer the latest value whose source is not the transient fallback default.
    If all records are default-sourced, report the latest non-null value. Older
    records without n_ctx_source are treated as real resolved values.
    """
    fallback: int | None = None
    for rec in reversed(recs):
        n_ctx = rec.get("n_ctx")
        if n_ctx is None:
            continue
        if fallback is None:
            fallback = n_ctx
        if rec.get("n_ctx_source") != "default":
            return n_ctx
    return fallback
```

- [ ] **Step 5: Parse nested and top-level `n_ctx` in `budget.py`**

In `src/civ_mcp/arena/budget.py`, replace `_n_ctx_from()` with:

```python
def _coerce_n_ctx(value: Any) -> int | None:
    try:
        n_ctx = int(value)
    except (TypeError, ValueError):
        return None
    return n_ctx if n_ctx > 0 else None


def _n_ctx_from(payload: dict[str, Any] | None) -> int | None:
    if payload is None:
        return None

    settings = payload.get("default_generation_settings")
    if isinstance(settings, dict):
        n_ctx = _coerce_n_ctx(settings.get("n_ctx"))
        if n_ctx is not None:
            return n_ctx

    return _coerce_n_ctx(payload.get("n_ctx"))
```

- [ ] **Step 6: Run targeted context tests**

Run:

```bash
/home/riz/.local/bin/uv run pytest tests/arena/test_analyze.py::test_config_summary_prefers_latest_non_default_n_ctx_over_cold_default tests/arena/test_analyze.py::test_config_summary_does_not_split_on_mid_run_n_ctx_change tests/arena/test_budget.py::test_auto_uses_upstream_props_first tests/arena/test_budget.py::test_auto_falls_back_to_bare_props_then_default tests/arena/test_budget.py::test_auto_accepts_top_level_n_ctx_from_props -q
```

Expected: all targeted tests pass.

- [ ] **Step 7: Run surrounding analyzer and budget tests**

Run:

```bash
/home/riz/.local/bin/uv run pytest tests/arena/test_analyze.py tests/arena/test_budget.py -q
```

Expected: both files pass.

- [ ] **Step 8: Commit**

```bash
git add src/civ_mcp/arena/analyze.py src/civ_mcp/arena/budget.py tests/arena/test_analyze.py tests/arena/test_budget.py
git commit -m "fix: report resolved arena context windows"
```

### Task 5: Fix Briefing Map Radius Metadata And Parallelize Fetches

**Files:**
- Modify: `src/civ_mcp/arena/briefing.py`
- Test: `tests/arena/test_briefing.py`

**Scope note (review decision):** Finding #9 (the map "throwaway" fetch at the
smaller radius) is intentionally NOT rewritten. That first fetch is the
measurement sample `_map()`'s radius projection depends on; replacing it with a
fixed per-tile estimate would drop the `used + len(larger) <= budget` post-fetch
budget guard and turn `test_map_expansion_accounts_for_header_and_separator` and
`test_map_expansion_accounts_for_join_separator_after_prior_section` into vacuous
passes (the estimate dwarfs their budgets, so expansion never triggers regardless
of projection sanity). This task leaves `_map()`'s measured projection untouched
and delivers only the two safe wins: correct partial-map radius telemetry
(finding #3) and concurrent map/production fetches (the sequential-await
efficiency items). The original `test_map_radius_expands_with_budget()` is kept
unchanged.

- [ ] **Step 1: Change the partial-map radius test to expect the rendered radius**

In `tests/arena/test_briefing.py`, rename `test_partial_map_section_does_not_report_radius()` to `test_partial_map_section_reports_rendered_radius()` and replace its final assertion:

```python
    assert b.radius == 0
```

with:

```python
    assert b.radius == 5
```

The complete test should be:

```python
@pytest.mark.asyncio
async def test_partial_map_section_reports_rendered_radius():
    gs = FakeGS()

    b = await build_briefing(
        gs,
        BriefingOptions(enabled=True, map_radius=5, sections=("map",)),
        70,
    )

    assert "map" in b.sections
    assert "== MAP ==" in b.text
    assert len(b.text) <= 70 * 3
    assert b.radius == 5
```

- [ ] **Step 2: Run the changed test and confirm it fails**

Run:

```bash
/home/riz/.local/bin/uv run pytest tests/arena/test_briefing.py::test_partial_map_section_reports_rendered_radius -q
```

Expected: the test fails because `b.radius` is currently `0` on a partial map append (the old `keep_building` guard only records the radius on a full append).

- [ ] **Step 3: Import `asyncio`**

At the top of `src/civ_mcp/arena/briefing.py`, add `asyncio`:

```python
import asyncio
```

- [ ] **Step 4: Parallelize map and production fetches (projection logic unchanged)**

Replace `_production_options()` with:

```python
async def _production_options(gs: Any, ctx: dict[str, Any]) -> str:
    cities = ctx.get("cities")
    if cities is None:
        result = await gs.get_cities()
        if isinstance(result, str):
            return result
        cities, _ = result
        ctx["cities"] = cities

    if not cities:
        return "No cities available for production options."

    results = await asyncio.gather(
        *(gs.list_city_production(city.city_id) for city in cities)
    )
    parts = []
    for city, result in zip(cities, results, strict=True):
        text = _render(result, nr.narrate_city_production)
        parts.append(f"[city {city.city_id} {city.name}]\n{text}")
    return "\n".join(parts)
```

Replace `_map_text()` with:

```python
async def _map_text(gs: Any, centers: list[tuple[int, int]], radius: int) -> str:
    tiles = {}
    results = await asyncio.gather(
        *(gs.get_map_area(x, y, radius) for x, y in centers)
    )
    for area in results:
        for tile in area:
            tiles[(tile.x, tile.y)] = tile
    return nr.narrate_map([tiles[key] for key in sorted(tiles)])
```

Leave `_map()`, `_tile_count()`, and the expansion projection loop exactly as they are — do NOT introduce a fixed per-tile estimate or a `_target_map_radius()` helper.

- [ ] **Step 5: Record map radius whenever a map block is appended**

In `build_briefing()`, replace:

```python
        if name == "map" and keep_building:
            briefing.radius = radius
```

with:

```python
        if name == "map" and briefing.sections and briefing.sections[-1] == "map":
            briefing.radius = radius
```

`_append_block()` appends `"map"` to `briefing.sections` on both the full-append and partial-append branches, but not when the block is too small to append. Keying on `sections[-1] == "map"` therefore records a full or partial appended map block while preserving `0` when the map block is dropped.

- [ ] **Step 6: Run the changed and kept map tests**

Run:

```bash
/home/riz/.local/bin/uv run pytest tests/arena/test_briefing.py::test_partial_map_section_reports_rendered_radius tests/arena/test_briefing.py::test_map_radius_expands_with_budget tests/arena/test_briefing.py::test_map_expansion_accounts_for_header_and_separator tests/arena/test_briefing.py::test_map_expansion_accounts_for_join_separator_after_prior_section tests/arena/test_briefing.py::test_map_radius_capped_at_five tests/arena/test_briefing.py::test_map_radius_stays_zero_when_map_not_included tests/arena/test_briefing.py::test_map_tiles_deduplicated -q
```

Expected: all listed tests pass. The unchanged expansion/accounting tests still pass because `_map()`'s measured projection is untouched; `gather()` preserves per-radius call counts (the first `_map_text()` invocation still runs fully before any expansion fetch), so `test_map_radius_expands_with_budget` still sees `{2, 5}` and four map calls.

- [ ] **Step 7: Run all briefing tests**

Run:

```bash
/home/riz/.local/bin/uv run pytest tests/arena/test_briefing.py -q
```

Expected: all briefing tests pass.

- [ ] **Step 8: Commit**

```bash
git add src/civ_mcp/arena/briefing.py tests/arena/test_briefing.py
git commit -m "fix: correct arena briefing map radius telemetry and parallelize fetches"
```

### Task 6: Remove Generic Parameter Bounds And Make Full Tier Track The Registry

**Files:**
- Modify: `src/civ_mcp/arena/registry.py`
- Test: `tests/arena/test_registry.py`

- [ ] **Step 1: Replace the generic clamp test with an ownership test**

In `tests/arena/test_registry.py`, replace `test_apply_param_bounds_clamps_any_declared_integer_param()` with:

```python
def test_registry_has_no_generic_param_bounds_layer():
    import civ_mcp.arena.registry as registry_mod

    assert not hasattr(registry_mod, "_apply_param_bounds")
```

- [ ] **Step 2: Add a failing test proving `resolve_tools("full")` tracks runtime registry additions**

Append this test near the tier tests in `tests/arena/test_registry.py`:

```python
def test_resolve_tools_full_tracks_registry_additions(monkeypatch):
    from civ_mcp.arena.registry import ToolDef

    async def _noop(gs, args):
        return ""

    monkeypatch.setitem(
        TOOL_REGISTRY,
        "__probe_tool__",
        ToolDef(
            name="__probe_tool__",
            description="probe",
            params={},
            required=(),
            call=_noop,
        ),
    )

    assert "__probe_tool__" in resolve_tools("full")
```

- [ ] **Step 3: Add a direct guard for initial full-tier coverage**

Append this test near `test_tiers_nest()` in `tests/arena/test_registry.py`:

```python
def test_full_tier_initially_matches_registry_order():
    assert TIERS["full"] == tuple(TOOL_REGISTRY)
```

- [ ] **Step 4: Run the new registry tests and confirm failures**

Run:

```bash
/home/riz/.local/bin/uv run pytest tests/arena/test_registry.py::test_registry_has_no_generic_param_bounds_layer tests/arena/test_registry.py::test_resolve_tools_full_tracks_registry_additions tests/arena/test_registry.py::test_full_tier_initially_matches_registry_order -q
```

Expected: the no-generic-layer test fails while `_apply_param_bounds` exists; the runtime registry addition test fails because `resolve_tools("full")` returns the static tuple.

- [ ] **Step 5: Delete `_apply_param_bounds()` and call tools with raw parsed args**

In `src/civ_mcp/arena/registry.py`, delete the entire `_apply_param_bounds()` function.

Then replace `dispatch()`:

```python
async def dispatch(
    gs: Any,
    name: str,
    args: dict[str, Any],
    allowed: Sequence[str] | None = None,
) -> str:
    if allowed is not None and name not in allowed:
        raise KeyError(name)
    tool = TOOL_REGISTRY[name]
    return await tool.call(gs, _apply_param_bounds(tool, args))
```

with:

```python
async def dispatch(
    gs: Any,
    name: str,
    args: dict[str, Any],
    allowed: Sequence[str] | None = None,
) -> str:
    if allowed is not None and name not in allowed:
        raise KeyError(name)
    tool = TOOL_REGISTRY[name]
    return await tool.call(gs, args)
```

- [ ] **Step 6: Derive the initial full tier from `TOOL_REGISTRY`**

In `src/civ_mcp/arena/registry.py`, replace the hand-written `"full": (...)` tuple in `TIERS` with:

```python
    "full": tuple(TOOL_REGISTRY),
```

Keep the existing `minimal` and `standard` curated tuples as explicit lists.

- [ ] **Step 7: Make `resolve_tools("full")` dynamic**

In `src/civ_mcp/arena/registry.py`, replace the string-selector branch of `resolve_tools()`:

```python
    if isinstance(selector, str):
        if selector in TIERS:
            return TIERS[selector]
        if selector in TOOL_REGISTRY:
            return (selector,)
        raise ValueError(f"Unknown tool tier or tool: {selector}")
```

with:

```python
    if isinstance(selector, str):
        if selector == "full":
            return tuple(TOOL_REGISTRY)
        if selector in TIERS:
            return TIERS[selector]
        if selector in TOOL_REGISTRY:
            return (selector,)
        raise ValueError(f"Unknown tool tier or tool: {selector}")
```

- [ ] **Step 8: Run targeted registry tests**

Run:

```bash
/home/riz/.local/bin/uv run pytest tests/arena/test_registry.py::test_registry_has_no_generic_param_bounds_layer tests/arena/test_registry.py::test_resolve_tools_full_tracks_registry_additions tests/arena/test_registry.py::test_full_tier_initially_matches_registry_order tests/arena/test_registry.py::test_get_map_area_radius_clamped_before_game_state tests/arena/test_registry.py::test_get_map_area_radius_tolerates_null_and_non_numeric -q
```

Expected: all targeted registry tests pass, including the map-specific clamp tests.

- [ ] **Step 9: Run all registry tests**

Run:

```bash
/home/riz/.local/bin/uv run pytest tests/arena/test_registry.py -q
```

Expected: all registry tests pass.

- [ ] **Step 10: Commit**

```bash
git add src/civ_mcp/arena/registry.py tests/arena/test_registry.py
git commit -m "fix: derive arena full tool tier"
```

### Task 7: Final Verification

**Files:**
- Verify: `src/civ_mcp/arena/*.py`
- Verify: `tests/arena/*.py`

- [ ] **Step 1: Run the complete arena test suite**

Run:

```bash
/home/riz/.local/bin/uv run pytest tests/arena -q
```

Expected: all arena tests pass.

- [ ] **Step 2: Run the broader Python test suite**

Run:

```bash
/home/riz/.local/bin/uv run pytest -q
```

Expected: all repository tests pass. If a live-game dependent test is skipped by environment checks, record the skip reason in the final implementation note.

- [ ] **Step 3: Inspect the final diff**

Run:

```bash
git diff --stat
git diff -- src/civ_mcp/arena/experiment.py src/civ_mcp/arena/arena.py src/civ_mcp/arena/analyze.py src/civ_mcp/arena/budget.py src/civ_mcp/arena/briefing.py src/civ_mcp/arena/registry.py tests/arena/test_experiment.py tests/arena/test_arena_wiring.py tests/arena/test_analyze.py tests/arena/test_budget.py tests/arena/test_briefing.py tests/arena/test_registry.py
```

Expected: the diff is limited to the files named in this plan, aside from any pre-existing unrelated worktree changes.

- [ ] **Step 4: Commit final verification notes only if needed**

If implementation produces a small follow-up fix from final verification, commit it with:

```bash
git add src/civ_mcp/arena tests/arena
git commit -m "test: verify arena review fixes"
```

If final verification produces no code changes, do not create an empty commit.

## Self-Review Notes

- Spec coverage: all ten ranked findings map to Tasks 1 through 6. Finding #9 is deliberately narrowed to the parallelization win only (see the Task 5 scope note); the two-fetch projection is left intact by design.
- Placeholder scan: no red-flag placeholder markers or unspecified test/error-handling instructions remain.
- Type consistency: snippets use existing `ArenaConfig`, `CivOptions`, `BriefingOptions`, `ToolDef`, `resolve_tools()`, `load_experiment()`, and `build_args()` names exactly as they exist in the current branch.
- Source verification (2026-07-05 review): every `replace X with Y` target was checked against the live branch — `experiment.py:125`, `experiment.py:248-271` (`_top_int`/`_non_blank_string`/`_run_id_string`/`arena_defaults`/`data`/`ids` all present), `arena.py:107-113`/`142-146`/`135`, `budget.py:27-37` (`Any` imported), and `registry.py` (`_apply_param_bounds` referenced only at its def + in `dispatch`; only `get_map_area` declares `minimum`/`maximum`; `TOOL_REGISTRY` at line 134 precedes `TIERS` at 528). `agent.py:124-125,186-187` confirmed to record `n_ctx_source`, so Task 4's analyzer key is populated in real runs, not just tests.
