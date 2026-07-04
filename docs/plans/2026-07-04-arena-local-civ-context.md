# Arena Local-Civ Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the ~100× context gap between local in-process civs and CLI civs by adding a
per-civ experiment config (toolset tiers, result/step caps, strategy playbook, rich turn
briefing sized to each model's real context window), so configurations can be A/B tested.

**Architecture:** Hybrid push + pull. A new briefing builder pre-assembles a budgeted game-state
briefing as the opening user message (push); a new tool registry replaces `agent.py`'s
hand-written tool table and exposes named tier subsets (pull + actions). All knobs live on a new
`CivOptions` dataclass carried by `PlayerSpec`, populated either from a YAML experiment file
(`civ-arena --config`) or defaulted by the existing `--player` shorthand (which must reproduce
today's behavior exactly).

**Tech Stack:** Python 3.12, asyncio, pytest + pytest-asyncio, PyYAML (new explicit dep),
httpx (already present via the openai SDK).

**Spec:** `docs/specs/2026-07-04-arena-local-civ-context-design.md` — read it first.

## Global Constraints

- `--player` shorthand runs must be **behavior-identical** to today: tier `minimal`,
  `result_char_cap=1500`, `max_steps=6` (or `--max-agent-steps`), playbook `none`, briefing off.
- Local civs must NEVER see: `end_turn`, save/load/lifecycle tools, `execute_lua`,
  diplomacy-session responses, World Congress voting. The registry simply never defines them.
- The assembled briefing is hard-truncated at budget — never blow the model's window.
- Briefing section failures are logged, never fatal.
- Token estimate heuristic everywhere: `tokens = len(text) // 4`.
- Stage files explicitly — never `git add -A` (repo has untracked `.serena/`, generated
  `*.jsonl`, `arena_runs/`).
- End state: commits on the feature branch, **unmerged**; the user reviews in a separate
  session before integration. Never push or merge to main.
- All tests: `uv run pytest tests/arena/ -q` must pass at every commit.

---

## File Structure

- `src/civ_mcp/arena/registry.py` — CREATE: `ToolDef`, `TOOL_REGISTRY`, `TIERS`,
  `resolve_tools()`, `openai_tools()`, `dispatch()`.
- `src/civ_mcp/arena/config.py` — MODIFY: add `BriefingOptions`, `CivOptions`; `PlayerSpec`
  gains `options` field.
- `src/civ_mcp/arena/experiment.py` — CREATE: `load_experiment(path) -> ArenaConfig` with
  fail-fast validation.
- `src/civ_mcp/arena/playbook.md` — CREATE: condensed strategy digest (packaged data file).
- `src/civ_mcp/arena/agent.py` — MODIFY: `LLMPolicy` honors `CivOptions` (toolset, caps,
  playbook, briefing); delegates tool table to registry.
- `src/civ_mcp/arena/budget.py` — CREATE: `resolve_n_ctx()`, `briefing_budget()`.
- `src/civ_mcp/arena/briefing.py` — CREATE: `build_briefing(gs, opts, budget_tokens)`.
- `src/civ_mcp/arena/arena.py` — MODIFY: `--config` flag; `build_policies` passes options.
- `src/civ_mcp/arena/analyze.py` — MODIFY: per-run config summary block.
- `pyproject.toml` — MODIFY: add `pyyaml>=6` dependency; include `playbook.md` as package data
  if needed (hatchling includes package files by default — verify).
- `tools/skills/civ6-arena-live/scripts/start-hybrid-watch.sh` — MODIFY: `--config` passthrough.
- `experiments/` — CREATE: `smoke-rich-gemma.yaml`, `ab-minimal-vs-standard.yaml`.
- Tests: `tests/arena/test_registry.py`, `test_experiment.py`, `test_budget.py`,
  `test_briefing.py`; extend `test_config.py`, `test_agent.py`, `test_arena_wiring.py`,
  `test_analyze.py`.

---

### Task 0: Branch setup

- [ ] **Step 1: Create the feature branch** (worktree optional; a branch on the main checkout
  is acceptable since this session owns the repo)

```bash
cd /home/riz/dev/civ6-mcp
git checkout -b arena-local-civ-context
uv run pytest tests/arena/ -q   # baseline green before any change
```

Expected: all arena tests pass (115+). If not, STOP and report.

---

### Task 1: Tool registry with tiers

**Files:**
- Create: `src/civ_mcp/arena/registry.py`
- Modify: `src/civ_mcp/arena/agent.py` (delegate `TOOLS`/`_dispatch` to registry)
- Test: `tests/arena/test_registry.py`

**Interfaces:**
- Consumes: `GameState` async methods (`get_game_overview`, `get_units`, `get_cities`,
  `move_unit(unit_index,x,y)`, `found_city(unit_index)`,
  `set_city_production(city_id,item_type,item_name)`, `set_research(tech)`,
  `fortify_unit`, `skip_unit`, `get_map_area(center_x,center_y,radius)`, `get_tech_civics()`,
  `attack_unit(unit_index,target_x,target_y)`, `improve_tile(unit_index,improvement_name)`,
  `remove_feature(unit_index)`, `purchase_item(city_id,item_type,item_name)`,
  `heal_unit`, `alert_unit`, `set_civic(civic_name)`, `get_settle_advisor(unit_index)`,
  `get_district_advisor`, `get_wonder_advisor`, `get_builder_tasks`, `get_diplomacy()`,
  `get_city_states()`, `get_great_people()`, `get_empire_resources()`,
  `get_victory_progress()`, `get_pathing_estimate`, `send_envoy(city_state_player_id)`,
  `set_policies`, `get_policies()`, `appoint_governor`, `assign_governor`,
  `choose_pantheon(belief_type)`, `get_pantheon_status()`, `upgrade_unit(unit_id)`,
  `promote_unit(unit_id,promotion_type)`, `get_unit_promotions(unit_id)`,
  `automate_explore(unit_index)`, `skip_remaining_units()`, `purchase_tile(city_id,x,y)`,
  `get_purchasable_tiles(city_id)`, `set_city_focus(city_id,focus)`)
- Produces: `ToolDef(name, description, params, required, call)`;
  `TOOL_REGISTRY: dict[str, ToolDef]`; `TIERS: dict[str, tuple[str, ...]]` with keys
  `minimal|standard|full`; `resolve_tools(selector: str | Sequence[str]) -> tuple[str, ...]`
  (ValueError on unknown tier/tool); `openai_tools(names) -> list[dict]`;
  `async dispatch(gs, name: str, args: dict)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/arena/test_registry.py
import pytest
from civ_mcp.arena.registry import (
    TOOL_REGISTRY, TIERS, resolve_tools, openai_tools, dispatch)

MINIMAL_9 = {"get_overview", "get_units", "get_cities", "move_unit", "found_city",
             "set_city_production", "set_research", "fortify_unit", "skip_unit"}

def test_minimal_tier_is_todays_nine():
    assert set(TIERS["minimal"]) == MINIMAL_9

def test_tiers_nest():
    assert set(TIERS["minimal"]) < set(TIERS["standard"]) < set(TIERS["full"])

def test_standard_adds_map_and_combat():
    extra = set(TIERS["standard"]) - set(TIERS["minimal"])
    assert {"get_map_area", "get_tech_civics", "attack_unit", "improve_tile",
            "purchase_item"} <= extra

def test_forbidden_tools_never_defined():
    for name in ("end_turn", "execute_lua", "load_game_save", "kill_game",
                 "queue_wc_votes", "diplomacy_respond"):
        assert name not in TOOL_REGISTRY

def test_resolve_tools_tier_and_explicit_list():
    assert resolve_tools("minimal") == TIERS["minimal"]
    assert resolve_tools(["get_units", "move_unit"]) == ("get_units", "move_unit")

def test_resolve_tools_rejects_unknown():
    with pytest.raises(ValueError):
        resolve_tools("mega")
    with pytest.raises(ValueError):
        resolve_tools(["get_units", "launch_nuke"])

def test_openai_tools_schema_shape():
    (t,) = openai_tools(["move_unit"])
    fn = t["function"]
    assert t["type"] == "function" and fn["name"] == "move_unit"
    assert set(fn["parameters"]["required"]) == {"unit_index", "x", "y"}

@pytest.mark.asyncio
async def test_dispatch_maps_args():
    calls = []
    class FakeGS:
        async def move_unit(self, unit_index, target_x, target_y):
            calls.append((unit_index, target_x, target_y)); return "MOVING_TO|4,5"
        async def attack_unit(self, unit_index, target_x, target_y):
            calls.append(("atk", unit_index)); return "ATTACKED"
    assert await dispatch(FakeGS(), "move_unit", {"unit_index": 1, "x": 4, "y": 5}) == "MOVING_TO|4,5"
    assert await dispatch(FakeGS(), "attack_unit", {"unit_index": 2, "x": 9, "y": 9}) == "ATTACKED"
    assert calls == [(1, 4, 5), ("atk", 2)]

def test_agent_module_still_exposes_tools():
    from civ_mcp.arena.agent import TOOLS
    names = {t["function"]["name"] for t in TOOLS}
    assert names == MINIMAL_9
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/arena/test_registry.py -q`
Expected: FAIL — `ModuleNotFoundError: civ_mcp.arena.registry`

- [ ] **Step 3: Implement `registry.py`**

```python
# src/civ_mcp/arena/registry.py
"""Tool registry for in-process local civs.

One table maps tool name -> schema -> GameState call. Tiers are named subsets.
Host-owned or unsafe operations (end_turn, save/load, execute_lua, diplomacy
session responses, World Congress votes) are NEVER defined here.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Sequence

@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    call: Callable[[Any, dict], Awaitable[Any]]  # (gs, args) -> result
    params: dict = field(default_factory=dict)   # JSON-schema properties
    required: tuple[str, ...] = ()

def _i(desc=""):
    return {"type": "integer", **({"description": desc} if desc else {})}

def _s(desc=""):
    return {"type": "string", **({"description": desc} if desc else {})}

_UNIT = {"unit_index": _i("from get_units")}
_XY = {"x": _i(), "y": _i()}

_DEFS = [
    # ---- minimal (today's nine) ----
    ToolDef("get_overview", "Empire/turn overview for your civ",
            lambda gs, a: gs.get_game_overview()),
    ToolDef("get_units", "List your units (with their unit_index)",
            lambda gs, a: gs.get_units()),
    ToolDef("get_cities", "List your cities",
            lambda gs, a: gs.get_cities()),
    ToolDef("move_unit", "Move a unit toward (x,y)",
            lambda gs, a: gs.move_unit(a["unit_index"], a["x"], a["y"]),
            {**_UNIT, **_XY}, ("unit_index", "x", "y")),
    ToolDef("found_city", "Found a city with a settler",
            lambda gs, a: gs.found_city(a["unit_index"]), dict(_UNIT), ("unit_index",)),
    ToolDef("set_city_production", "Set a city's production",
            lambda gs, a: gs.set_city_production(a["city_id"], a["item_type"], a["item_name"]),
            {"city_id": _i(), "item_type": _s("UNIT | BUILDING | DISTRICT | PROJECT"),
             "item_name": _s("e.g. UNIT_WARRIOR, BUILDING_MONUMENT")},
            ("city_id", "item_type", "item_name")),
    ToolDef("set_research", "Set the research tech (TECH_*)",
            lambda gs, a: gs.set_research(a["tech"]), {"tech": _s()}, ("tech",)),
    ToolDef("fortify_unit", "Fortify a unit",
            lambda gs, a: gs.fortify_unit(a["unit_index"]), dict(_UNIT), ("unit_index",)),
    ToolDef("skip_unit", "Skip a unit this turn",
            lambda gs, a: gs.skip_unit(a["unit_index"]), dict(_UNIT), ("unit_index",)),
    # ---- standard additions ----
    ToolDef("get_map_area", "Tiles around (x,y): terrain, resources, foreign units",
            lambda gs, a: gs.get_map_area(a["x"], a["y"], a.get("radius", 2)),
            {**_XY, "radius": _i("default 2, max 5")}, ("x", "y")),
    ToolDef("get_tech_civics", "Available techs and civics with turns to complete",
            lambda gs, a: gs.get_tech_civics()),
    ToolDef("attack_unit", "Attack an enemy at (x,y) with a unit (melee/ranged auto)",
            lambda gs, a: gs.attack_unit(a["unit_index"], a["x"], a["y"]),
            {**_UNIT, **_XY}, ("unit_index", "x", "y")),
    ToolDef("improve_tile", "Builder: build an improvement on the current tile",
            lambda gs, a: gs.improve_tile(a["unit_index"], a["improvement"]),
            {**_UNIT, "improvement": _s("e.g. IMPROVEMENT_FARM, IMPROVEMENT_MINE")},
            ("unit_index", "improvement")),
    ToolDef("remove_feature", "Builder: chop forest/jungle/marsh on current tile",
            lambda gs, a: gs.remove_feature(a["unit_index"]), dict(_UNIT), ("unit_index",)),
    ToolDef("purchase_item", "Buy a unit/building instantly with gold",
            lambda gs, a: gs.purchase_item(a["city_id"], a["item_type"], a["item_name"]),
            {"city_id": _i(), "item_type": _s("UNIT | BUILDING"), "item_name": _s()},
            ("city_id", "item_type", "item_name")),
    ToolDef("heal_unit", "Fortify until healed (auto-wakes at full HP)",
            lambda gs, a: gs.heal_unit(a["unit_index"]), dict(_UNIT), ("unit_index",)),
    ToolDef("alert_unit", "Sleep but wake when an enemy nears",
            lambda gs, a: gs.alert_unit(a["unit_index"]), dict(_UNIT), ("unit_index",)),
    ToolDef("set_civic", "Set the civic being researched (CIVIC_*)",
            lambda gs, a: gs.set_civic(a["civic"]), {"civic": _s()}, ("civic",)),
    # ---- full additions ----
    ToolDef("get_settle_advisor", "Rank settle spots near a settler",
            lambda gs, a: gs.get_settle_advisor(a["unit_index"]), dict(_UNIT), ("unit_index",)),
    ToolDef("get_district_advisor", "Ranked tiles for a district in a city",
            lambda gs, a: gs.get_district_advisor(a["city_id"], a["district_type"]),
            {"city_id": _i(), "district_type": _s("e.g. DISTRICT_CAMPUS")},
            ("city_id", "district_type")),
    ToolDef("get_wonder_advisor", "Placement tiles for a wonder in a city",
            lambda gs, a: gs.get_wonder_advisor(a["city_id"], a["wonder_name"]),
            {"city_id": _i(), "wonder_name": _s()}, ("city_id", "wonder_name")),
    ToolDef("get_builder_tasks", "All tiles needing improvements, prioritized",
            lambda gs, a: gs.get_builder_tasks()),
    ToolDef("get_diplomacy", "Rival civs: strength, relationship, agendas",
            lambda gs, a: gs.get_diplomacy()),
    ToolDef("get_city_states", "City-states, envoy counts, suzerains",
            lambda gs, a: gs.get_city_states()),
    ToolDef("get_great_people", "Great People candidates and recruitment progress",
            lambda gs, a: gs.get_great_people()),
    ToolDef("get_empire_resources", "Stockpiles, owned and nearby resources",
            lambda gs, a: gs.get_empire_resources()),
    ToolDef("get_victory_progress", "All victory types, your and rivals' progress",
            lambda gs, a: gs.get_victory_progress()),
    ToolDef("get_pathing_estimate", "Turns for a unit to reach (x,y)",
            lambda gs, a: gs.get_pathing_estimate(a["unit_index"], a["x"], a["y"]),
            {**_UNIT, **_XY}, ("unit_index", "x", "y")),
    ToolDef("send_envoy", "Send an envoy to a city-state",
            lambda gs, a: gs.send_envoy(a["city_state_player_id"]),
            {"city_state_player_id": _i()}, ("city_state_player_id",)),
    ToolDef("get_policies", "Current government, slots, and available policies",
            lambda gs, a: gs.get_policies()),
    ToolDef("set_policies", "Assign policy cards to slots",
            lambda gs, a: gs.set_policies({int(k): v for k, v in a["assignments"].items()}),
            {"assignments": {"type": "object",
                             "description": "slot index -> POLICY_* name"}},
            ("assignments",)),
    ToolDef("appoint_governor", "Appoint a governor type (e.g. GOVERNOR_THE_EDUCATOR)",
            lambda gs, a: gs.appoint_governor(a["governor_type"]),
            {"governor_type": _s()}, ("governor_type",)),
    ToolDef("assign_governor", "Assign an appointed governor to a city",
            lambda gs, a: gs.assign_governor(a["governor_type"], a["city_id"]),
            {"governor_type": _s(), "city_id": _i()}, ("governor_type", "city_id")),
    ToolDef("choose_pantheon", "Choose a pantheon belief (BELIEF_*)",
            lambda gs, a: gs.choose_pantheon(a["belief"]), {"belief": _s()}, ("belief",)),
    ToolDef("get_pantheon_status", "Pantheon availability and belief options",
            lambda gs, a: gs.get_pantheon_status()),
    ToolDef("upgrade_unit", "Upgrade a unit to its next type (needs tech+gold)",
            lambda gs, a: gs.upgrade_unit(a["unit_id"]), {"unit_id": _i()}, ("unit_id",)),
    ToolDef("promote_unit", "Apply a promotion to a unit",
            lambda gs, a: gs.promote_unit(a["unit_id"], a["promotion"]),
            {"unit_id": _i(), "promotion": _s("PROMOTION_*")}, ("unit_id", "promotion")),
    ToolDef("get_unit_promotions", "Available promotions for a unit",
            lambda gs, a: gs.get_unit_promotions(a["unit_id"]), {"unit_id": _i()}, ("unit_id",)),
    ToolDef("automate_explore", "Set a scout to auto-explore",
            lambda gs, a: gs.automate_explore(a["unit_index"]), dict(_UNIT), ("unit_index",)),
    ToolDef("skip_remaining_units", "Skip all units that still have moves",
            lambda gs, a: gs.skip_remaining_units()),
    ToolDef("purchase_tile", "Buy a border tile with gold",
            lambda gs, a: gs.purchase_tile(a["city_id"], a["x"], a["y"]),
            {"city_id": _i(), **_XY}, ("city_id", "x", "y")),
    ToolDef("get_purchasable_tiles", "Tiles a city can buy and their cost",
            lambda gs, a: gs.get_purchasable_tiles(a["city_id"]), {"city_id": _i()}, ("city_id",)),
    ToolDef("set_city_focus", "Set a city's yield focus (FOOD/PRODUCTION/GOLD/...)",
            lambda gs, a: gs.set_city_focus(a["city_id"], a["focus"]),
            {"city_id": _i(), "focus": _s()}, ("city_id", "focus")),
]

TOOL_REGISTRY: dict[str, ToolDef] = {d.name: d for d in _DEFS}

_MINIMAL = ("get_overview", "get_units", "get_cities", "move_unit", "found_city",
            "set_city_production", "set_research", "fortify_unit", "skip_unit")
_STANDARD = _MINIMAL + ("get_map_area", "get_tech_civics", "attack_unit", "improve_tile",
                        "remove_feature", "purchase_item", "heal_unit", "alert_unit",
                        "set_civic")
_FULL = _STANDARD + ("get_settle_advisor", "get_district_advisor", "get_wonder_advisor",
                     "get_builder_tasks", "get_diplomacy", "get_city_states",
                     "get_great_people", "get_empire_resources", "get_victory_progress",
                     "get_pathing_estimate", "send_envoy", "get_policies", "set_policies",
                     "appoint_governor", "assign_governor", "choose_pantheon",
                     "get_pantheon_status", "upgrade_unit", "promote_unit",
                     "get_unit_promotions", "automate_explore", "skip_remaining_units",
                     "purchase_tile", "get_purchasable_tiles", "set_city_focus")
TIERS: dict[str, tuple[str, ...]] = {"minimal": _MINIMAL, "standard": _STANDARD, "full": _FULL}

def resolve_tools(selector: str | Sequence[str]) -> tuple[str, ...]:
    if isinstance(selector, str):
        if selector not in TIERS:
            raise ValueError(f"unknown toolset tier {selector!r}; want one of {sorted(TIERS)}")
        return TIERS[selector]
    unknown = [n for n in selector if n not in TOOL_REGISTRY]
    if unknown:
        raise ValueError(f"unknown tool name(s) {unknown}; see registry.TOOL_REGISTRY")
    return tuple(selector)

def openai_tools(names: Sequence[str]) -> list[dict]:
    out = []
    for n in names:
        d = TOOL_REGISTRY[n]
        out.append({"type": "function", "function": {
            "name": d.name, "description": d.description,
            "parameters": {"type": "object", "properties": d.params,
                           "required": list(d.required)}}})
    return out

async def dispatch(gs, name: str, args: dict):
    return await TOOL_REGISTRY[name].call(gs, args)
```

- [ ] **Step 4: Rewire `agent.py` to the registry (behavior-neutral)**

In `src/civ_mcp/arena/agent.py`, delete the `_tool` helper, the `TOOLS` list literal, the
`_KNOWN_TOOLS` frozenset, and the `_dispatch` function. Replace with:

```python
from civ_mcp.arena.registry import resolve_tools, openai_tools, dispatch as _registry_dispatch

_MINIMAL_NAMES = resolve_tools("minimal")
TOOLS = openai_tools(_MINIMAL_NAMES)          # module-level default, minimal tier

async def _dispatch(gs, name, args):
    a = json.loads(args or "{}")
    return await _registry_dispatch(gs, name, a)
```

In `LLMPolicy.__call__`, replace the `tc["name"] not in _KNOWN_TOOLS` check with
`tc["name"] not in _MINIMAL_NAMES` for now (Task 4 makes this per-civ), and replace the
`table[name]()` dispatch with the new `_dispatch`. Dispatch of an unknown name must still
land in the existing `except Exception` → `result = f"ERROR: {e!r}"` path (KeyError does).

- [ ] **Step 5: Run the full arena suite**

Run: `uv run pytest tests/arena/ -q`
Expected: PASS (registry tests + all pre-existing, incl. `test_agent.py`).

- [ ] **Step 6: Commit**

```bash
git add src/civ_mcp/arena/registry.py src/civ_mcp/arena/agent.py tests/arena/test_registry.py
git commit -m "feat(arena): tool registry with minimal/standard/full tiers"
```

---

### Task 2: `CivOptions` on `PlayerSpec`

**Files:**
- Modify: `src/civ_mcp/arena/config.py`
- Test: `tests/arena/test_config.py` (extend)

**Interfaces:**
- Produces:
  `BriefingOptions(enabled: bool = False, map_radius: int = 3, sections: tuple[str, ...] = ("overview","units","cities","map","research","production_options"))`;
  `CivOptions(tools: str | tuple = "minimal", result_char_cap: int = 1500, max_steps: int = 6, playbook: str = "none", context_budget: int | str = "auto", briefing: BriefingOptions = default)`;
  `CivOptions.fingerprint() -> dict` (JSON-safe, for transcripts);
  `PlayerSpec.options: CivOptions` (default factory — existing constructors unchanged).
  `VALID_SECTIONS = ("overview","units","cities","map","research","production_options","empire_resources","rivals","threats","victory")`;
  `VALID_PLAYBOOKS = ("none","condensed")`.

- [ ] **Step 1: Write the failing tests** (append to `tests/arena/test_config.py`)

```python
from civ_mcp.arena.config import CivOptions, BriefingOptions

def test_civ_options_defaults_match_today():
    o = CivOptions()
    assert (o.tools, o.result_char_cap, o.max_steps, o.playbook) == ("minimal", 1500, 6, "none")
    assert o.context_budget == "auto"
    assert o.briefing.enabled is False

def test_player_spec_gets_default_options():
    s = parse_player_spec("1:local:qwen3-coder:30b")
    assert s.options == CivOptions()

def test_civ_options_fingerprint_is_json_safe():
    import json
    o = CivOptions(tools=("get_units", "move_unit"), max_steps=10,
                   briefing=BriefingOptions(enabled=True, map_radius=4))
    fp = o.fingerprint()
    assert json.dumps(fp)          # no TypeError
    assert fp["tools"] == ["get_units", "move_unit"]
    assert fp["briefing"]["enabled"] is True
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/arena/test_config.py -q`
Expected: FAIL — `ImportError: cannot import name 'CivOptions'`

- [ ] **Step 3: Implement in `config.py`** (add above `PlayerSpec`; give `PlayerSpec` the new
  field with a default factory so every existing constructor call keeps working)

```python
VALID_SECTIONS = ("overview", "units", "cities", "map", "research", "production_options",
                  "empire_resources", "rivals", "threats", "victory")
VALID_PLAYBOOKS = ("none", "condensed")

@dataclass(frozen=True)
class BriefingOptions:
    enabled: bool = False
    map_radius: int = 3
    sections: tuple[str, ...] = ("overview", "units", "cities", "map",
                                 "research", "production_options")

@dataclass(frozen=True)
class CivOptions:
    tools: str | tuple[str, ...] = "minimal"
    result_char_cap: int = 1500
    max_steps: int = 6
    playbook: str = "none"            # VALID_PLAYBOOKS
    context_budget: int | str = "auto"
    briefing: BriefingOptions = field(default_factory=BriefingOptions)

    def fingerprint(self) -> dict:
        return {
            "tools": list(self.tools) if not isinstance(self.tools, str) else self.tools,
            "result_char_cap": self.result_char_cap,
            "max_steps": self.max_steps,
            "playbook": self.playbook,
            "context_budget": self.context_budget,
            "briefing": {"enabled": self.briefing.enabled,
                         "map_radius": self.briefing.map_radius,
                         "sections": list(self.briefing.sections)},
        }
```

And on `PlayerSpec` add the field (after `gateway`):

```python
    options: CivOptions = field(default_factory=CivOptions)
```

(`from dataclasses import dataclass, field` is already imported.)

- [ ] **Step 4: Run the suite**

Run: `uv run pytest tests/arena/ -q` — Expected: PASS (PlayerSpec equality tests still pass:
same defaults compare equal).

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/config.py tests/arena/test_config.py
git commit -m "feat(arena): CivOptions/BriefingOptions experiment knobs on PlayerSpec"
```

---

### Task 3: Experiment YAML loader

**Files:**
- Create: `src/civ_mcp/arena/experiment.py`
- Modify: `pyproject.toml` (add `"pyyaml>=6"` to `[project] dependencies`)
- Test: `tests/arena/test_experiment.py`

**Interfaces:**
- Consumes: `CivOptions`, `BriefingOptions`, `PlayerSpec`, `ArenaConfig`, `_VALID_PROVIDERS`,
  `VALID_SECTIONS`, `VALID_PLAYBOOKS` from `config.py`; `resolve_tools` from `registry.py`.
- Produces: `load_experiment(path: str | Path) -> ArenaConfig`. Raises `ValueError` naming the
  offending civ/field on: unknown provider, duplicate player ids, unknown tool tier/name,
  unknown briefing section, unknown playbook, local-only knobs on a CLI civ, non-positive caps.

- [ ] **Step 1: Write the failing tests**

```python
# tests/arena/test_experiment.py
import pytest
from civ_mcp.arena.experiment import load_experiment
from civ_mcp.arena.config import CivOptions

GOOD = """
run_id: exp-1
max_puppet_turns: 80
idle_poll_limit: 3600
gateway_url: http://gw:11444/v1
civs:
  - player: 3
    provider: local
    model: gemma4-26b
    gateway: http://gw:11440/v1
    tools: standard
    result_char_cap: 6000
    max_steps: 10
    playbook: condensed
    context_budget: auto
    briefing: {enabled: true, map_radius: 4, sections: [overview, units, map]}
  - player: 1
    provider: cli-claude
    model: ""
"""

def _write(tmp_path, text):
    p = tmp_path / "exp.yaml"; p.write_text(text); return p

def test_load_good(tmp_path):
    cfg = load_experiment(_write(tmp_path, GOOD))
    assert cfg.run_id == "exp-1" and cfg.max_puppet_turns == 80
    assert cfg.gateway_url == "http://gw:11444/v1"
    assert cfg.puppet_ids == [3, 1]
    local = cfg.players[0]
    assert local.gateway == "http://gw:11440/v1"
    assert local.options.tools == "standard"
    assert local.options.max_steps == 10
    assert local.options.briefing.enabled and local.options.briefing.map_radius == 4
    assert local.options.briefing.sections == ("overview", "units", "map")
    cli = cfg.players[1]
    assert cli.provider == "cli-claude" and cli.options == CivOptions()

def test_rejects_duplicate_players(tmp_path):
    bad = GOOD.replace("player: 1", "player: 3")
    with pytest.raises(ValueError, match="duplicate"):
        load_experiment(_write(tmp_path, bad))

def test_rejects_unknown_tier(tmp_path):
    with pytest.raises(ValueError, match="player 3"):
        load_experiment(_write(tmp_path, GOOD.replace("tools: standard", "tools: mega")))

def test_rejects_unknown_section(tmp_path):
    with pytest.raises(ValueError, match="player 3"):
        load_experiment(_write(tmp_path, GOOD.replace("[overview, units, map]",
                                                      "[overview, minimap]")))

def test_rejects_local_knobs_on_cli_civ(tmp_path):
    bad = GOOD + "    max_steps: 9\n"
    with pytest.raises(ValueError, match="cli-claude"):
        load_experiment(_write(tmp_path, bad))

def test_explicit_tool_list(tmp_path):
    cfg = load_experiment(_write(tmp_path,
        GOOD.replace("tools: standard", "tools: [get_units, move_unit]")))
    assert cfg.players[0].options.tools == ("get_units", "move_unit")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/arena/test_experiment.py -q`
Expected: FAIL — `ModuleNotFoundError: civ_mcp.arena.experiment`

- [ ] **Step 3: Add the dependency and implement**

In `pyproject.toml` `[project] dependencies`, add `"pyyaml>=6",` after `"openai>=1.0",`
then run `uv sync --extra test`.

```python
# src/civ_mcp/arena/experiment.py
"""Load a YAML experiment file into an ArenaConfig (fail-fast validation)."""
from __future__ import annotations
from pathlib import Path
import yaml

from civ_mcp.arena.config import (
    ArenaConfig, PlayerSpec, CivOptions, BriefingOptions,
    _VALID_PROVIDERS, VALID_SECTIONS, VALID_PLAYBOOKS, DEFAULT_GATEWAY_URL)
from civ_mcp.arena.registry import resolve_tools

_LOCAL_KNOBS = ("tools", "result_char_cap", "max_steps", "playbook",
                "context_budget", "briefing")
_CIV_KEYS = {"player", "provider", "model", "gateway", *_LOCAL_KNOBS}
_TOP_KEYS = {"run_id", "max_puppet_turns", "idle_poll_limit", "gateway_url", "civs"}

def _err(civ_label: str, msg: str) -> ValueError:
    return ValueError(f"experiment config: {civ_label}: {msg}")

def _parse_briefing(civ_label: str, raw: dict) -> BriefingOptions:
    keys = set(raw) - {"enabled", "map_radius", "sections"}
    if keys:
        raise _err(civ_label, f"unknown briefing key(s) {sorted(keys)}")
    sections = tuple(raw.get("sections", BriefingOptions().sections))
    bad = [s for s in sections if s not in VALID_SECTIONS]
    if bad:
        raise _err(civ_label, f"unknown briefing section(s) {bad}; want {VALID_SECTIONS}")
    return BriefingOptions(enabled=bool(raw.get("enabled", False)),
                           map_radius=int(raw.get("map_radius", 3)),
                           sections=sections)

def _parse_civ(raw: dict) -> PlayerSpec:
    label = f"player {raw.get('player', '?')}"
    unknown = set(raw) - _CIV_KEYS
    if unknown:
        raise _err(label, f"unknown key(s) {sorted(unknown)}")
    provider = raw.get("provider", "")
    if provider not in _VALID_PROVIDERS:
        raise _err(label, f"unknown provider {provider!r}; want {sorted(_VALID_PROVIDERS)}")
    if provider != "local":
        present = [k for k in _LOCAL_KNOBS if k in raw]
        if present:
            raise _err(label, f"knob(s) {present} only apply to local civs, not {provider}")
        return PlayerSpec(int(raw["player"]), provider, str(raw.get("model", "")),
                          str(raw.get("gateway", "")))
    tools = raw.get("tools", "minimal")
    if isinstance(tools, list):
        tools = tuple(tools)
    try:
        resolve_tools(tools)
    except ValueError as e:
        raise _err(label, str(e)) from None
    playbook = raw.get("playbook", "none")
    if playbook not in VALID_PLAYBOOKS:
        raise _err(label, f"unknown playbook {playbook!r}; want {VALID_PLAYBOOKS}")
    budget = raw.get("context_budget", "auto")
    if budget != "auto":
        budget = int(budget)
        if budget <= 0:
            raise _err(label, "context_budget must be positive or 'auto'")
    cap = int(raw.get("result_char_cap", 1500))
    steps = int(raw.get("max_steps", 6))
    if cap <= 0 or steps <= 0:
        raise _err(label, "result_char_cap and max_steps must be positive")
    opts = CivOptions(tools=tools, result_char_cap=cap, max_steps=steps,
                      playbook=playbook, context_budget=budget,
                      briefing=_parse_briefing(label, dict(raw.get("briefing") or {})))
    return PlayerSpec(int(raw["player"]), provider, str(raw.get("model", "")),
                      str(raw.get("gateway", "")), opts)

def load_experiment(path: str | Path) -> ArenaConfig:
    data = yaml.safe_load(Path(path).read_text())
    if not isinstance(data, dict) or "civs" not in data:
        raise ValueError(f"experiment config {path}: want a mapping with a 'civs' list")
    unknown = set(data) - _TOP_KEYS
    if unknown:
        raise ValueError(f"experiment config {path}: unknown top-level key(s) {sorted(unknown)}")
    players = [_parse_civ(c) for c in data["civs"]]
    ids = [p.player_id for p in players]
    if len(ids) != len(set(ids)):
        raise ValueError(f"experiment config {path}: duplicate player ids {ids}")
    return ArenaConfig(
        players=players,
        max_puppet_turns=int(data.get("max_puppet_turns", 1)),
        gateway_url=str(data.get("gateway_url", DEFAULT_GATEWAY_URL)),
        idle_poll_limit=int(data.get("idle_poll_limit", 600)),
        puppet_ids=ids,
        run_id=str(data.get("run_id", "")))
```

- [ ] **Step 4: Run the suite**

Run: `uv run pytest tests/arena/ -q` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/experiment.py tests/arena/test_experiment.py pyproject.toml uv.lock
git commit -m "feat(arena): YAML experiment config loader with fail-fast validation"
```

---

### Task 4: `LLMPolicy` honors `CivOptions` + condensed playbook

**Files:**
- Create: `src/civ_mcp/arena/playbook.md`
- Modify: `src/civ_mcp/arena/agent.py`
- Test: `tests/arena/test_agent.py` (extend)

**Interfaces:**
- Consumes: `CivOptions` (Task 2), `resolve_tools`/`openai_tools`/`dispatch` (Task 1).
- Produces: `LLMPolicy(backend, cost, max_steps=6, options: CivOptions | None = None)` —
  when `options` is given it wins over `max_steps`; `load_playbook() -> str` (module fn,
  cached read of `playbook.md`). Transcript dict gains `"civ_options": options.fingerprint()`.
  Tool-call classification uses the per-civ allowed set.

- [ ] **Step 1: Write the failing tests** (append to `tests/arena/test_agent.py`; reuse that
  file's existing fake-backend pattern — read it first and follow its fixtures. The tests below
  assume a `FakeBackend` that returns queued `Reply` objects; adapt names to the file's own.)

```python
from civ_mcp.arena.config import CivOptions
from civ_mcp.arena.agent import LLMPolicy, load_playbook

def _no_tool_reply(text="done"):
    from civ_mcp.arena.backends import Reply
    return Reply(text=text, tool_calls=[], prompt_tokens=10, completion_tokens=5)

class SpyBackend:
    """Records the kwargs of every chat() call; returns queued replies."""
    model = "fake"
    def __init__(self, replies): self.replies, self.calls = list(replies), []
    async def chat(self, messages, tools):
        self.calls.append({"messages": messages, "tools": tools})
        return self.replies.pop(0)

@pytest.mark.asyncio
async def test_options_select_toolset_and_playbook(fake_cost):
    be = SpyBackend([_no_tool_reply()])
    opts = CivOptions(tools="standard", playbook="condensed", max_steps=3)
    pol = LLMPolicy(be, fake_cost, options=opts)
    await pol(gs=None, player_id=3, turn=5)
    call = be.calls[0]
    names = {t["function"]["name"] for t in call["tools"]}
    assert "get_map_area" in names and "attack_unit" in names
    assert load_playbook() in call["messages"][0]["content"]

@pytest.mark.asyncio
async def test_options_cap_and_steps(fake_cost):
    from civ_mcp.arena.backends import Reply
    tool_reply = Reply(text=None, tool_calls=[
        {"id": "1", "name": "get_units", "arguments": "{}"}],
        prompt_tokens=10, completion_tokens=5)
    be = SpyBackend([tool_reply, _no_tool_reply()])
    class FakeGS:
        async def get_units(self): return "U" * 10_000
    opts = CivOptions(result_char_cap=2000, max_steps=2)
    pol = LLMPolicy(be, fake_cost, options=opts)
    out = await pol(FakeGS(), 3, 5)
    tool_msg = [m for m in be.calls[1]["messages"] if m["role"] == "tool"][0]
    assert len(tool_msg["content"]) == 2000
    step = out["transcript"]["steps"][0]
    assert step["result_chars_fed_to_model"] == 2000 and step["truncated"]

@pytest.mark.asyncio
async def test_transcript_carries_options_fingerprint(fake_cost):
    be = SpyBackend([_no_tool_reply()])
    opts = CivOptions(tools="standard")
    pol = LLMPolicy(be, fake_cost, options=opts)
    out = await pol(None, 3, 5)
    assert out["transcript"]["civ_options"]["tools"] == "standard"

def test_playbook_loads_and_is_reasonably_sized():
    text = load_playbook()
    assert 2000 < len(text) < 20000
    assert "settler" in text.lower()
```

(`fake_cost` — use the existing cost fixture/fake in `test_agent.py`; if none exists, a
minimal `class FakeCost:  def record(self, **kw): pass` suffices.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/arena/test_agent.py -q`
Expected: FAIL — `load_playbook` missing / `options` kwarg unexpected.

- [ ] **Step 3: Write `playbook.md`** (condensed strategy digest — complete content)

```markdown
# Civ VI Strategy Digest (arena local civ)

## Every turn, in order
1. get_overview — turn, yields, what you are researching.
2. get_units — every unit acts every turn: move, attack, improve, fortify, or skip.
3. get_cities — no city may have an empty production queue.
4. If you have a settler: settle good land fast (see Expansion). If you have a builder:
   improve tiles (see Builders). If military: scout, escort, or clear barbarians.

## Expansion (the strongest lever)
- More cities = more science, gold, and production. Aim for a new city every ~10 turns
  early; 4+ cities by turn 60.
- Settle on flat land near fresh water (river/lake), 3+ tiles from another city, with
  hills and resources nearby. Coastal is fine if the land is good.
- A settler caught alone is captured: keep a warrior adjacent or ahead on the path.
- Production priority in a new empire: Scout -> Settler -> Settler/Builder, adding a
  Warrior when barbarians threaten and a Monument when safe.

## Growth
- Fix any city with food surplus <= 0 immediately: Farm, Granary, or switch production.
- Housing caps growth: settle near fresh water, build farms in pairs/triangles.

## Research and civics
- Early tech order that rarely fails: what your terrain needs (Mining for hills/woods,
  Animal Husbandry for pastures), then Pottery, Writing, Bronze Working (reveals Iron).
- Set a civic every time one finishes: Code of Laws -> Foreign Trade -> Craftsmanship ->
  Early Empire (boosts from settling/improving accelerate these).
- Anything flagged as completable in <= 2 turns is usually worth grabbing first.

## Builders
- 3 charges each. Improve bonus/luxury resources first (Plantation, Mine, Pasture,
  Camp), then Farms on flat river tiles, Mines on bare hills.
- Forest/jungle blocks Farms: remove_feature first, or build a Lumber Mill on forest.
- Never walk a builder into unexplored or enemy-visible tiles unescorted.

## Combat basics
- Warrior 20 CS melee; Slinger 15 RS ranged (range 1); Archer 25 RS (range 2).
  Barbarian warriors are 20 CS.
- Ranged units take no damage attacking; melee units do. Soften with ranged, finish
  with melee. Fortified units get +4 and heal each turn.
- Clear barbarian camps near your cities within a few turns of spotting them, or they
  will spawn endless raiders. One warrior + one slinger/archer clears an early camp.
- Keep one military unit in or beside each city.

## Districts (unlock with population)
- Campus (science) next to mountains; Commercial Hub (gold) on rivers; Holy Site
  (faith) next to mountains/forest. Place with set_city_production once available.

## Using the map
- The briefing shows tiles around your units and cities: terrain, resources, rivers,
  hills, and any visible foreign units. Unexplored area means threats you cannot see —
  move scouts toward it.
- Hills and forest cost 2 movement each (stacking); plan multi-turn moves accordingly.

## Priorities when unsure
1. Empty production queue -> fix it. 2. Idle unit -> use it. 3. Settler ready and a
spot known -> settle. 4. Barbarian camp near a city -> clear it. 5. Otherwise: improve
tiles, scout, and keep research/civics running.
```

- [ ] **Step 4: Implement `agent.py` changes**

Replace the `LLMPolicy.__init__` and the top of `__call__`:

```python
from functools import lru_cache
from pathlib import Path
from civ_mcp.arena.config import CivOptions

@lru_cache(maxsize=1)
def load_playbook() -> str:
    return (Path(__file__).parent / "playbook.md").read_text()

class LLMPolicy:
    def __init__(self, backend, cost, max_steps: int = 6,
                 options: CivOptions | None = None):
        self.backend, self.cost = backend, cost
        self.options = options or CivOptions(max_steps=max_steps)
        self.max_steps = self.options.max_steps
        self._tool_names = resolve_tools(self.options.tools)
        self._tools = openai_tools(self._tool_names)
        self._char_cap = self.options.result_char_cap
        self._system = SYSTEM
        if self.options.playbook == "condensed":
            self._system = SYSTEM + "\n\n" + load_playbook()
```

Inside `__call__`: use `self._system` for the system message, `self._tools` in the
`backend.chat` call, `self._tool_names` for the unknown-tool classification,
`self._char_cap` in place of both `MODEL_FEED_CHAR_CAP` uses, and add
`"civ_options": self.options.fingerprint()` to BOTH returned transcript dicts (the
normal-return one and the max-steps one). Keep `MODEL_FEED_CHAR_CAP = 1500` as the
`CivOptions.result_char_cap` default's documentation anchor (it is now only referenced
by older tests, if any — check and update them rather than deleting the constant).

- [ ] **Step 5: Run the suite**

Run: `uv run pytest tests/arena/ -q` — Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/civ_mcp/arena/agent.py src/civ_mcp/arena/playbook.md tests/arena/test_agent.py
git commit -m "feat(arena): LLMPolicy honors CivOptions; condensed strategy playbook"
```

---

### Task 5: `--config` CLI wiring

**Files:**
- Modify: `src/civ_mcp/arena/arena.py`
- Test: `tests/arena/test_arena_wiring.py` (extend)

**Interfaces:**
- Consumes: `load_experiment` (Task 3), `CivOptions` (Task 2).
- Produces: `civ-arena --config <file>` — mutually exclusive with `--player`; with
  `--config`, the file provides players/max_puppet_turns/idle_poll_limit/gateway_url and the
  CLI still provides `--run-id`, `--transcript-dir`, `--no-transcript`, `--cost-path`,
  `--dry-run`, `--api-key-env`. `build_policies` passes `spec.options` into `LLMPolicy`.

- [ ] **Step 1: Write the failing tests** (append to `tests/arena/test_arena_wiring.py`,
  following that file's existing style for exercising `build_policies` / `build_args`)

```python
from civ_mcp.arena.arena import build_args, build_policies, resolve_config
from civ_mcp.arena.config import ArenaConfig, CivOptions, parse_player_spec
from civ_mcp.arena.cost import CostLog

def test_build_args_accepts_config():
    a = build_args(["--config", "experiments/x.yaml"])
    assert a.config == "experiments/x.yaml"

def test_config_and_player_are_mutually_exclusive(tmp_path, capsys):
    import pytest
    with pytest.raises(SystemExit):
        resolve_config(build_args(["--config", "x.yaml", "--player", "1:local:m"]))

def test_resolve_config_from_file(tmp_path):
    p = tmp_path / "e.yaml"
    p.write_text("max_puppet_turns: 12\ncivs:\n  - {player: 3, provider: local, "
                 "model: m, max_steps: 9}\n")
    cfg = resolve_config(build_args(["--config", str(p)]))
    assert cfg.max_puppet_turns == 12
    assert cfg.players[0].options.max_steps == 9

def test_build_policies_threads_options(tmp_path):
    spec = parse_player_spec("3:local:m")
    object.__setattr__(spec, "options", CivOptions(max_steps=11, tools="standard"))
    cfg = ArenaConfig(players=[spec])
    cost = CostLog(str(tmp_path / "c.jsonl"))
    policies, backends = build_policies([spec], cost, cfg)
    pol = policies[3]
    assert pol.max_steps == 11
    assert any(t["function"]["name"] == "get_map_area" for t in pol._tools)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/arena/test_arena_wiring.py -q`
Expected: FAIL — no `--config` arg, no `resolve_config`.

- [ ] **Step 3: Implement in `arena.py`**

Add to `build_args`:

```python
    ap.add_argument("--config", default="",
                    help="YAML experiment file (mutually exclusive with --player)")
```

Add a pure resolver (used by `_run`, testable without asyncio):

```python
def resolve_config(args) -> ArenaConfig:
    """--config file XOR --player flags -> ArenaConfig (run_id/cost/transcript set later)."""
    from civ_mcp.arena.experiment import load_experiment
    if args.config and args.player:
        raise SystemExit("--config and --player are mutually exclusive")
    if args.config:
        cfg = load_experiment(args.config)
        cfg.dry_run = args.dry_run
        cfg.api_key_env = args.api_key_env
        return cfg
    specs = [parse_player_spec(s) for s in args.player]
    return ArenaConfig(players=specs, max_puppet_turns=args.max_puppet_turns,
                       gateway_url=args.gateway_url, api_key_env=args.api_key_env,
                       dry_run=args.dry_run, max_agent_steps=args.max_agent_steps,
                       idle_poll_limit=getattr(args, "idle_poll_limit", 600),
                       puppet_ids=[s.player_id for s in specs])
```

In `_run`, replace the inline `specs = [...]` + `cfg = ArenaConfig(...)` construction with
`cfg = resolve_config(args)`, then `specs = cfg.players`, keeping the existing run_id /
run_dir / cost_path / transcript lines and the `cfg.run_id = run_id` assignment
(file-provided `cfg.run_id` wins over generation when non-empty:
`run_id = args.run_id or cfg.run_id or generate_run_id()`), and set
`cfg.cost_path = cost_path` after computing it.

In `build_policies`, thread options into `LLMPolicy` — replace the `LLMPolicy(...)` line:

```python
            policies[spec.player_id] = LLMPolicy(
                backend, cost, max_steps=cfg.max_agent_steps, options=spec.options)
```

Wait — `--player` shorthand must still honor `--max-agent-steps`. `spec.options` from
`parse_player_spec` is all-defaults (`max_steps=6`), which would silently override a CLI
`--max-agent-steps 12`. Resolve in `resolve_config`'s `--player` branch: after building
specs, if `args.max_agent_steps != 6`, rebuild each local spec's options with
`dataclasses.replace(spec.options, max_steps=args.max_agent_steps)` (and
`dataclasses.replace(spec, options=...)` since both are frozen). Add a test:

```python
def test_player_shorthand_honors_max_agent_steps():
    cfg = resolve_config(build_args(["--player", "3:local:m", "--max-agent-steps", "12"]))
    assert cfg.players[0].options.max_steps == 12
```

- [ ] **Step 4: Run the suite**

Run: `uv run pytest tests/arena/ -q` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/arena.py tests/arena/test_arena_wiring.py
git commit -m "feat(arena): --config experiment file wiring; options threaded to LLMPolicy"
```

---

### Task 6: Context-budget resolver

**Files:**
- Create: `src/civ_mcp/arena/budget.py`
- Test: `tests/arena/test_budget.py`

**Interfaces:**
- Consumes: `CivOptions` (Task 2); `httpx` (dep of openai SDK).
- Produces:
  `async resolve_n_ctx(base_url: str, model: str, context_budget: int | str, http_get=None) -> tuple[int, str]`
  returning `(n_ctx, source)` with source ∈ `explicit|upstream_props|props|default`;
  `briefing_budget(n_ctx: int, options: CivOptions, playbook_chars: int, tool_schema_chars: int) -> int`
  (tokens, ≥ 0); `DEFAULT_N_CTX = 16384`; `CHARS_PER_TOKEN = 4`.
  `http_get` is an injectable `async (url) -> dict | None` for tests; production default
  uses `httpx.AsyncClient` with a 5s timeout, returning `None` on any error/non-200.

- [ ] **Step 1: Write the failing tests**

```python
# tests/arena/test_budget.py
import pytest
from civ_mcp.arena.budget import resolve_n_ctx, briefing_budget, DEFAULT_N_CTX
from civ_mcp.arena.config import CivOptions

@pytest.mark.asyncio
async def test_explicit_budget_skips_probe():
    async def boom(url): raise AssertionError("must not probe")
    assert await resolve_n_ctx("http://h:1/v1", "m", 65536, http_get=boom) == (65536, "explicit")

@pytest.mark.asyncio
async def test_auto_uses_upstream_props_first():
    seen = []
    async def fake(url):
        seen.append(url)
        if "/upstream/" in url:
            return {"default_generation_settings": {"n_ctx": 131072}}
        return None
    n, src = await resolve_n_ctx("http://h:11440/v1", "gemma4-26b", "auto", http_get=fake)
    assert (n, src) == (131072, "upstream_props")
    assert seen[0] == "http://h:11440/upstream/gemma4-26b/props"

@pytest.mark.asyncio
async def test_auto_falls_back_to_bare_props_then_default():
    async def only_bare(url):
        return {"default_generation_settings": {"n_ctx": 32768}} if url.endswith("/props") \
            and "/upstream/" not in url else None
    n, src = await resolve_n_ctx("http://h:1/v1", "m", "auto", http_get=only_bare)
    assert (n, src) == (32768, "props")
    async def nothing(url): return None
    n, src = await resolve_n_ctx("http://h:1/v1", "m", "auto", http_get=nothing)
    assert (n, src) == (DEFAULT_N_CTX, "default")

def test_briefing_budget_formula():
    opts = CivOptions(max_steps=10, result_char_cap=6000)
    # reserve = playbook + schemas + steps*(cap/4 + 512) + 1024
    got = briefing_budget(131072, opts, playbook_chars=12000, tool_schema_chars=4000)
    reserve = 12000 // 4 + 4000 // 4 + 10 * (6000 // 4 + 512) + 1024
    assert got == 131072 - reserve

def test_briefing_budget_floors_at_zero():
    opts = CivOptions(max_steps=50, result_char_cap=20000)
    assert briefing_budget(8192, opts, playbook_chars=0, tool_schema_chars=0) == 0
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/arena/test_budget.py -q`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `budget.py`**

```python
# src/civ_mcp/arena/budget.py
"""Resolve a local model's real context window and the briefing token budget."""
from __future__ import annotations

DEFAULT_N_CTX = 16384
CHARS_PER_TOKEN = 4
_COMPLETION_RESERVE_PER_STEP = 512
_MARGIN_TOKENS = 1024

async def _default_http_get(url: str) -> dict | None:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(url)
            return r.json() if r.status_code == 200 else None
    except Exception:
        return None

def _n_ctx_of(payload: dict | None) -> int | None:
    try:
        return int(payload["default_generation_settings"]["n_ctx"])
    except (TypeError, KeyError, ValueError):
        return None

async def resolve_n_ctx(base_url: str, model: str, context_budget,
                        http_get=None) -> tuple[int, str]:
    if isinstance(context_budget, int):
        return context_budget, "explicit"
    get = http_get or _default_http_get
    origin = base_url.rstrip("/")
    origin = origin[:-3] if origin.endswith("/v1") else origin
    n = _n_ctx_of(await get(f"{origin}/upstream/{model}/props"))   # llama-swap route
    if n:
        return n, "upstream_props"
    n = _n_ctx_of(await get(f"{origin}/props"))                    # bare llama-server
    if n:
        return n, "props"
    return DEFAULT_N_CTX, "default"

def briefing_budget(n_ctx: int, options, playbook_chars: int,
                    tool_schema_chars: int) -> int:
    reserve = (playbook_chars // CHARS_PER_TOKEN
               + tool_schema_chars // CHARS_PER_TOKEN
               + options.max_steps * (options.result_char_cap // CHARS_PER_TOKEN
                                      + _COMPLETION_RESERVE_PER_STEP)
               + _MARGIN_TOKENS)
    return max(n_ctx - reserve, 0)
```

- [ ] **Step 4: Run the suite**

Run: `uv run pytest tests/arena/ -q` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/budget.py tests/arena/test_budget.py
git commit -m "feat(arena): context-window probe + briefing budget allocator"
```

---

### Task 7: Briefing builder

**Files:**
- Create: `src/civ_mcp/arena/briefing.py`
- Test: `tests/arena/test_briefing.py`

**Interfaces:**
- Consumes: `BriefingOptions` (Task 2); `GameState` methods `get_game_overview()`,
  `get_units()`, `get_cities() -> (list, warnings)`, `list_city_production(city_id)`,
  `get_map_area(x, y, radius) -> list[TileInfo(x, y, ...)]`, `get_tech_civics()`,
  `get_empire_resources()`, `get_rival_snapshot()`, `get_threat_scan()`,
  `get_victory_progress()`; `CHARS_PER_TOKEN` (Task 6).
- Produces: `Briefing(text: str, tokens: int, sections: list[str], radius: int,
  errors: list[str])`; `async build_briefing(gs, opts: BriefingOptions, budget_tokens: int) -> Briefing`.
  Behavior: sections build independently in the fixed priority order below, appended while
  they fit the char budget (`budget_tokens * CHARS_PER_TOKEN`); the map section retries at
  radius+1 (max 5) while total usage stays under 75% of the char budget; final text is
  hard-truncated at the char budget.

- [ ] **Step 1: Write the failing tests**

```python
# tests/arena/test_briefing.py
import pytest
from dataclasses import dataclass
from civ_mcp.arena.briefing import build_briefing, Briefing
from civ_mcp.arena.config import BriefingOptions

@dataclass
class T:  # minimal TileInfo stand-in (str() used for rendering)
    x: int; y: int
    def __str__(self): return f"({self.x},{self.y}) grass"

@dataclass
class U:
    x: int; y: int
    def __str__(self): return f"Warrior at ({self.x},{self.y})"

class FakeGS:
    def __init__(self): self.map_calls = []
    async def get_game_overview(self): return "OVERVIEW turn=5"
    async def get_units(self): return [U(10, 10)]
    async def get_cities(self): return ([type("C", (), {"x": 12, "y": 12,
        "city_id": 65536, "__str__": lambda s: "CITY Nidaros"})()], ["warn"])
    async def list_city_production(self, city_id): return [f"opt-{city_id}"]
    async def get_map_area(self, x, y, radius):
        self.map_calls.append((x, y, radius))
        return [T(x + dx, y, ) for dx in range(-radius, radius + 1)]
    async def get_tech_civics(self): return "TECHS: pottery 3t"

ALL = ("overview", "units", "cities", "map", "research", "production_options")

@pytest.mark.asyncio
async def test_sections_in_priority_order_and_meta():
    gs = FakeGS()
    b = await build_briefing(gs, BriefingOptions(enabled=True, sections=ALL), 100_000)
    assert isinstance(b, Briefing)
    assert b.sections == ["overview", "units", "cities", "map", "research",
                          "production_options"]
    for marker in ("OVERVIEW turn=5", "Warrior at (10,10)", "CITY Nidaros",
                   "TECHS: pottery 3t", "opt-65536"):
        assert marker in b.text
    assert b.tokens == len(b.text) // 4
    assert b.errors == []

@pytest.mark.asyncio
async def test_map_radius_expands_with_budget():
    gs = FakeGS()
    b = await build_briefing(gs, BriefingOptions(enabled=True, map_radius=2,
                                                 sections=ALL), 100_000)
    assert b.radius == 5                       # plenty of budget -> max radius
    assert gs.map_calls[0][2] == 2             # started at configured radius

@pytest.mark.asyncio
async def test_map_tiles_deduplicated():
    gs = FakeGS()
    b = await build_briefing(gs, BriefingOptions(enabled=True, map_radius=2,
                                                 sections=("map",)), 100_000)
    # unit (10,10) and city (12,12) rows overlap at radius>=2; each tile once
    assert b.text.count("(10,10) grass") == 1

@pytest.mark.asyncio
async def test_hard_truncation_at_budget():
    gs = FakeGS()
    b = await build_briefing(gs, BriefingOptions(enabled=True, sections=ALL), 50)
    assert len(b.text) <= 50 * 4

@pytest.mark.asyncio
async def test_failing_section_skipped_and_logged():
    gs = FakeGS()
    async def boom(): raise RuntimeError("no tuner")
    gs.get_tech_civics = boom
    b = await build_briefing(gs, BriefingOptions(enabled=True, sections=ALL), 100_000)
    assert "research" not in b.sections
    assert any("research" in e and "no tuner" in e for e in b.errors)
    assert "OVERVIEW" in b.text                # earlier sections unaffected
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/arena/test_briefing.py -q`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `briefing.py`**

```python
# src/civ_mcp/arena/briefing.py
"""Assemble a budgeted game-state briefing for a local civ's turn (the 'push' half)."""
from __future__ import annotations
from dataclasses import dataclass, field

from civ_mcp.arena.budget import CHARS_PER_TOKEN

_MAX_RADIUS = 5
_EXPAND_BELOW = 0.75   # expand map radius while total usage < 75% of char budget

@dataclass
class Briefing:
    text: str = ""
    tokens: int = 0
    sections: list[str] = field(default_factory=list)
    radius: int = 0
    errors: list[str] = field(default_factory=list)

def _fmt(items) -> str:
    if isinstance(items, (list, tuple)):
        return "\n".join(str(i) for i in items)
    return str(items)

async def _sec_overview(gs, ctx): return _fmt(await gs.get_game_overview())
async def _sec_units(gs, ctx):
    ctx["units"] = await gs.get_units()
    return _fmt(ctx["units"])
async def _sec_cities(gs, ctx):
    cities, _warn = await gs.get_cities()
    ctx["cities"] = cities
    return _fmt(cities)
async def _sec_research(gs, ctx): return _fmt(await gs.get_tech_civics())
async def _sec_production_options(gs, ctx):
    parts = []
    for c in ctx.get("cities") or []:
        opts = await gs.list_city_production(c.city_id)
        parts.append(f"[city {c.city_id}]\n" + _fmt(opts))
    return "\n".join(parts)
async def _sec_empire_resources(gs, ctx): return _fmt(await gs.get_empire_resources())
async def _sec_rivals(gs, ctx): return _fmt(await gs.get_rival_snapshot())
async def _sec_threats(gs, ctx): return _fmt(await gs.get_threat_scan())
async def _sec_victory(gs, ctx): return _fmt(await gs.get_victory_progress())

async def _map_at(gs, centers, radius) -> str:
    tiles = {}
    for (x, y) in centers:
        for t in await gs.get_map_area(x, y, radius):
            tiles[(t.x, t.y)] = t
    ordered = [tiles[k] for k in sorted(tiles)]
    return "\n".join(str(t) for t in ordered)

_ORDER = ("overview", "units", "cities", "map", "research", "production_options",
          "empire_resources", "rivals", "threats", "victory")
_BUILDERS = {"overview": _sec_overview, "units": _sec_units, "cities": _sec_cities,
             "research": _sec_research, "production_options": _sec_production_options,
             "empire_resources": _sec_empire_resources, "rivals": _sec_rivals,
             "threats": _sec_threats, "victory": _sec_victory}

async def build_briefing(gs, opts, budget_tokens: int) -> Briefing:
    char_budget = budget_tokens * CHARS_PER_TOKEN
    b = Briefing()
    parts: list[str] = []
    used = 0
    wanted = [s for s in _ORDER if s in opts.sections]
    for name in wanted:
        try:
            if name == "map":
                if opts.map_radius <= 0:
                    continue
                centers = [(u.x, u.y) for u in (b_ctx.get("units") or [])]
                centers += [(c.x, c.y) for c in (b_ctx.get("cities") or [])]
                if not centers:
                    continue
                radius = opts.map_radius
                text = await _map_at(gs, centers, radius)
                while (radius < _MAX_RADIUS
                       and used + len(text) < char_budget * _EXPAND_BELOW):
                    bigger = await _map_at(gs, centers, radius + 1)
                    if used + len(bigger) > char_budget:
                        break
                    radius, text = radius + 1, bigger
                b.radius = radius
            else:
                text = await _BUILDERS[name](gs, b_ctx)
        except Exception as e:
            b.errors.append(f"{name}: {e!r}")
            continue
        block = f"== {name.upper()} ==\n{text}"
        if used + len(block) > char_budget:
            remaining = char_budget - used
            if remaining > 200:                      # partial section still useful
                parts.append(block[:remaining])
                b.sections.append(name)
            break
        parts.append(block)
        b.sections.append(name)
        used += len(block) + 1
    b.text = "\n".join(parts)[:char_budget]
    b.tokens = len(b.text) // CHARS_PER_TOKEN
    return b
```

NOTE for the implementer: `b_ctx` above is the shared dict the section builders read/write
(`units`, `cities`). Initialize `b_ctx: dict = {}` right after `b = Briefing()` — the test
`test_map_tiles_deduplicated` (map-only sections) requires the map section to fall back to
fetching units/cities itself when the dict is empty:

```python
                centers = [(u.x, u.y) for u in (b_ctx.get("units")
                            or await gs.get_units())]
                cities = b_ctx.get("cities")
                if cities is None:
                    cities, _ = await gs.get_cities()
                centers += [(c.x, c.y) for c in cities]
```

- [ ] **Step 4: Run the suite**

Run: `uv run pytest tests/arena/ -q` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/briefing.py tests/arena/test_briefing.py
git commit -m "feat(arena): budgeted turn-briefing builder with expanding map radius"
```

---

### Task 8: Briefing + budget integration into `LLMPolicy`

**Files:**
- Modify: `src/civ_mcp/arena/agent.py`
- Test: `tests/arena/test_agent.py` (extend)

**Interfaces:**
- Consumes: `build_briefing` (Task 7), `resolve_n_ctx`/`briefing_budget` (Task 6),
  `load_playbook` (Task 4), `openai_tools` schemas (Task 1).
- Produces: when `options.briefing.enabled`, `LLMPolicy.__call__` resolves `n_ctx` once
  (cached on the instance), builds the briefing each turn, and prepends it to the opening
  user message. Transcript gains `briefing_tokens`, `briefing_sections`, `briefing_radius`,
  `briefing_errors`, `n_ctx`, `n_ctx_source`.

- [ ] **Step 1: Write the failing tests** (append to `tests/arena/test_agent.py`)

```python
from civ_mcp.arena.config import BriefingOptions

@pytest.mark.asyncio
async def test_briefing_prepended_and_telemetry(fake_cost, monkeypatch):
    from civ_mcp.arena import agent as agent_mod

    async def fake_resolve(base_url, model, budget, http_get=None):
        return 131072, "upstream_props"
    monkeypatch.setattr(agent_mod, "resolve_n_ctx", fake_resolve)

    from civ_mcp.arena.briefing import Briefing
    async def fake_build(gs, opts, budget_tokens):
        assert budget_tokens > 100_000        # ~131072 minus small reserve
        return Briefing(text="BRIEFING BODY", tokens=3,
                        sections=["overview"], radius=4, errors=[])
    monkeypatch.setattr(agent_mod, "build_briefing", fake_build)

    be = SpyBackend([_no_tool_reply()])
    be.base_url = "http://h:11440/v1"
    opts = CivOptions(briefing=BriefingOptions(enabled=True))
    pol = LLMPolicy(be, fake_cost, options=opts)
    out = await pol(None, 3, 7)

    user_msg = [m for m in be.calls[0]["messages"] if m["role"] == "user"][0]
    assert user_msg["content"].startswith("BRIEFING BODY")
    assert "It is turn 7" in user_msg["content"]
    tr = out["transcript"]
    assert tr["briefing_tokens"] == 3 and tr["briefing_sections"] == ["overview"]
    assert tr["n_ctx"] == 131072 and tr["n_ctx_source"] == "upstream_props"

@pytest.mark.asyncio
async def test_n_ctx_resolved_once_across_turns(fake_cost, monkeypatch):
    from civ_mcp.arena import agent as agent_mod
    calls = []
    async def fake_resolve(*a, **kw):
        calls.append(1); return 32768, "props"
    monkeypatch.setattr(agent_mod, "resolve_n_ctx", fake_resolve)
    from civ_mcp.arena.briefing import Briefing
    async def fake_build(gs, opts, budget): return Briefing(text="B", tokens=1)
    monkeypatch.setattr(agent_mod, "build_briefing", fake_build)
    be = SpyBackend([_no_tool_reply(), _no_tool_reply()])
    be.base_url = "http://h:1/v1"
    pol = LLMPolicy(be, fake_cost,
                    options=CivOptions(briefing=BriefingOptions(enabled=True)))
    await pol(None, 3, 7); await pol(None, 3, 8)
    assert len(calls) == 1

@pytest.mark.asyncio
async def test_briefing_disabled_is_todays_message(fake_cost):
    be = SpyBackend([_no_tool_reply()])
    pol = LLMPolicy(be, fake_cost, options=CivOptions())
    await pol(None, 3, 7)
    user_msg = [m for m in be.calls[0]["messages"] if m["role"] == "user"][0]
    assert user_msg["content"] == "It is turn 7. You control player 3. Begin."
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/arena/test_agent.py -q`
Expected: FAIL — briefing not prepended / telemetry keys missing.

- [ ] **Step 3: Implement in `agent.py`**

Imports (module level, so tests can monkeypatch `agent.resolve_n_ctx` / `agent.build_briefing`):

```python
from civ_mcp.arena.budget import resolve_n_ctx, briefing_budget
from civ_mcp.arena.briefing import build_briefing, Briefing
```

In `LLMPolicy.__init__` add `self._n_ctx: int | None = None; self._n_ctx_source = ""`.

At the top of `__call__`, replace the `messages = [...]` construction:

```python
        briefing = Briefing()
        if self.options.briefing.enabled:
            if self._n_ctx is None:
                self._n_ctx, self._n_ctx_source = await resolve_n_ctx(
                    getattr(self.backend, "base_url", ""),
                    getattr(self.backend, "model", ""),
                    self.options.context_budget)
            playbook_chars = len(self._system) - len(SYSTEM)
            schema_chars = len(json.dumps(self._tools))
            budget = briefing_budget(self._n_ctx, self.options,
                                     playbook_chars, schema_chars)
            briefing = await build_briefing(gs, self.options.briefing, budget)
        opening = f"It is turn {turn}. You control player {player_id}. Begin."
        if briefing.text:
            opening = f"{briefing.text}\n\n{opening}"
        messages = [{"role": "system", "content": self._system},
                    {"role": "user", "content": opening}]
```

Add to BOTH transcript dicts (alongside `civ_options` from Task 4):

```python
                    "briefing_tokens": briefing.tokens,
                    "briefing_sections": briefing.sections,
                    "briefing_radius": briefing.radius,
                    "briefing_errors": briefing.errors,
                    "n_ctx": self._n_ctx,
                    "n_ctx_source": self._n_ctx_source,
```

- [ ] **Step 4: Run the suite**

Run: `uv run pytest tests/arena/ -q` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/agent.py tests/arena/test_agent.py
git commit -m "feat(arena): inject budgeted briefing into local-civ turns + telemetry"
```

---

### Task 9: `analyze.py` config summary

**Files:**
- Modify: `src/civ_mcp/arena/analyze.py`
- Test: `tests/arena/test_analyze.py` (extend)

**Interfaces:**
- Consumes: transcript records with `civ_options`, `briefing_tokens`, `n_ctx` (Tasks 4/8),
  plus existing fields (`player_id`, `model`, `step_count`, `invalid_tool_calls`,
  `state_delta`).
- Produces: `config_summary(records: list[dict]) -> dict` — per player id (str key):
  `{"model", "provider", "civ_options", "n_ctx", "turns", "avg_steps",
  "invalid_call_rate", "avg_briefing_tokens", "avg_score_delta"}`. Wired into `report.json`
  under `"config_summary"` and rendered as a Markdown table in `report.md` (follow the
  existing report-assembly pattern in the file — find where the top-level report dict is
  built and the md sections are appended).

- [ ] **Step 1: Write the failing test** (append to `tests/arena/test_analyze.py`)

```python
from civ_mcp.arena.analyze import config_summary

def _rec(pid, steps, invalid, brief, score):
    return {"player_id": pid, "model": "gemma4-26b", "provider": "local",
            "driver": "in_process", "step_count": steps,
            "invalid_tool_calls": [{}] * invalid,
            "civ_options": {"tools": "standard", "max_steps": 10},
            "briefing_tokens": brief, "n_ctx": 131072,
            "state_delta": {"score": score},
            "steps": [], "prompt_tokens": 100, "completion_tokens": 10}

def test_config_summary_groups_by_player():
    recs = [_rec(3, 4, 1, 30000, 2), _rec(3, 6, 0, 31000, 3),
            _rec(4, 2, 0, 0, 1)]
    s = config_summary(recs)
    p3 = s["3"]
    assert p3["turns"] == 2 and p3["avg_steps"] == 5.0
    assert p3["invalid_call_rate"] == pytest.approx(1 / 10)   # 1 invalid / 10 steps
    assert p3["avg_briefing_tokens"] == 30500
    assert p3["avg_score_delta"] == 2.5
    assert p3["civ_options"]["tools"] == "standard"
    assert "4" in s
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/arena/test_analyze.py -q`
Expected: FAIL — `config_summary` missing.

- [ ] **Step 3: Implement**

```python
def config_summary(records: list[dict]) -> dict:
    """Per-player experiment-config fingerprint + outcome averages."""
    by_pid: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        if r.get("player_id") is not None:
            by_pid[str(r["player_id"])].append(r)
    out: dict[str, dict] = {}
    for pid, recs in sorted(by_pid.items()):
        steps = sum(r.get("step_count", 0) for r in recs)
        invalid = sum(len(r.get("invalid_tool_calls") or []) for r in recs)
        briefs = [r.get("briefing_tokens", 0) for r in recs]
        scores = [(r.get("state_delta") or {}).get("score", 0) for r in recs]
        last = recs[-1]
        out[pid] = {
            "model": last.get("model", ""),
            "provider": last.get("provider", ""),
            "civ_options": last.get("civ_options") or {},
            "n_ctx": last.get("n_ctx"),
            "turns": len(recs),
            "avg_steps": steps / len(recs),
            "invalid_call_rate": (invalid / steps) if steps else 0.0,
            "avg_briefing_tokens": sum(briefs) / len(briefs),
            "avg_score_delta": sum(scores) / len(scores),
        }
    return out
```

Wire into the report: find the function that assembles the top-level `report.json` dict and
add `"config_summary": config_summary(transcript_records)`; in the Markdown assembly, add a
`## Experiment config` table with one row per player
(`| player | model | tools | max_steps | n_ctx | avg briefing tok | avg steps | invalid rate | avg Δscore |`).
Follow the file's existing md-table helpers/style.

- [ ] **Step 4: Run the suite**

Run: `uv run pytest tests/arena/ -q` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/analyze.py tests/arena/test_analyze.py
git commit -m "feat(arena): per-run experiment-config summary in analyze reports"
```

---

### Task 10: Experiment files + watcher passthrough + docs

**Files:**
- Create: `experiments/smoke-rich-gemma.yaml`, `experiments/ab-minimal-vs-standard.yaml`
- Modify: `tools/skills/civ6-arena-live/scripts/start-hybrid-watch.sh`
- Modify: `docs/specs/2026-07-04-arena-local-civ-context-design.md` (status line only)

- [ ] **Step 1: Write the experiment files**

```yaml
# experiments/smoke-rich-gemma.yaml — Gate C: one-round live smoke, rich config
max_puppet_turns: 1
idle_poll_limit: 3600
civs:
  - player: 3
    provider: local
    model: gemma4-26b
    gateway: http://192.168.20.196:11440/v1
    tools: standard
    result_char_cap: 6000
    max_steps: 10
    playbook: condensed
    context_budget: auto
    briefing:
      enabled: true
      map_radius: 3
      sections: [overview, units, cities, map, research, production_options]
```

```yaml
# experiments/ab-minimal-vs-standard.yaml — Gate D: same model, control vs rich
max_puppet_turns: 12    # 6 rounds x 2 seats
idle_poll_limit: 3600
civs:
  - player: 3           # control: today's behavior exactly
    provider: local
    model: gemma4-26b
    gateway: http://192.168.20.196:11440/v1
  - player: 4           # treatment: rich context (same model, other GPU)
    provider: local
    model: gemma4-26b
    gateway: http://192.168.20.196:11441/v1
    tools: standard
    result_char_cap: 6000
    max_steps: 10
    playbook: condensed
    context_budget: auto
    briefing:
      enabled: true
      map_radius: 3
      sections: [overview, units, cities, map, research, production_options]
```

- [ ] **Step 2: Watcher passthrough**

In `start-hybrid-watch.sh`: add a `--config <repo-relative path>` option (parsed like
`--run-id`); when set, build `arena_args=("--config" "$config_path" "--run-id" "$run_id")`
INSTEAD of the player/gateway/turn args (they live in the file), keep the existing
process-guard/launch/pidfile logic unchanged, and error out if both `--config` and
`--player` were passed. Verify with `bash -n` and `--help`.

- [ ] **Step 3: Flip the spec status line**

In the spec header change `APPROVED design, not yet implemented.` to
`IMPLEMENTED on branch arena-local-civ-context (pending live gates + user review).`

- [ ] **Step 4: Full suite + commit**

```bash
uv run pytest tests/ -q          # whole repo, not just arena
git add experiments/ tools/skills/civ6-arena-live/scripts/start-hybrid-watch.sh \
        docs/specs/2026-07-04-arena-local-civ-context-design.md
git commit -m "feat(arena): experiment files, watcher --config passthrough"
```

---

## Live Gates (manual, with the user — after all tasks green)

**Gate A — Slice 0 environment (before Gates C/D, `/opt/brothereye`, NOT this repo):**
For each in-play model on each per-GPU config
(`infra/llama-swap/config.per-gpu-{0,1}.yaml`): run `/llamacpp-memory-estimate` with the
GGUF path + proposed `--ctx-size`, raise `--ctx-size` to the largest fitting value
(target 131072; accept less where KV doesn't fit beside 16–20 GB weights on 24 GB),
then verify live: `curl -s http://192.168.20.196:11440/upstream/gemma4-26b/props | jq
.default_generation_settings.n_ctx`. llama-swap has `-watch-config`; no restart needed.

**Gate B — dry-run with config (this repo, no LLM):**
`uv run civ-arena --dry-run --config experiments/smoke-rich-gemma.yaml` on the gaming PC;
expect the scripted policy to run one puppet turn and exit 0, proving config loading +
coordinator wiring.

**Gate C — one-round live smoke:** deploy branch to `.141` (push feature branch, ff-merge
NOT allowed — check it out directly: `git fetch && git checkout arena-local-civ-context`),
start via `start-hybrid-watch.sh --config experiments/smoke-rich-gemma.yaml`, user ends
turn; verify transcript record has `briefing_tokens > 5000`, `n_ctx_source` ∈
{upstream_props, explicit}, no `briefing_errors`, control returns to human.

**Gate D — A/B baseline:** `--config experiments/ab-minimal-vs-standard.yaml`, ~6 rounds;
then `civ-arena-analyze` on the run dir and read the `config_summary` table: does the
treatment seat show lower invalid-call rate / higher Δscore? (Either answer is a valid
experiment result.)

---

## Self-Review

- **Spec coverage:** config file (T3, T5), registry/tiers incl. never-exposed list (T1),
  CivOptions knobs + `--player` behavior parity (T2, T4, T5), playbook (T4), budget probe +
  formula + fallbacks (T6), briefing sections/priority/expansion/truncation/error-skip (T7),
  integration + telemetry fields (T8), analyze summary (T9), experiment files + watcher +
  live gates incl. Slice 0 ctx raises (T10, Gates A–D). ✓
- **Placeholder scan:** all code steps carry complete code; the two "follow the file's
  existing pattern" notes (T4 Step 1 fixtures, T9 Step 3 report wiring) point at concrete
  existing structures the implementer must read, not TBDs. ✓
- **Type consistency:** `CivOptions.fingerprint() -> dict` (T2) consumed in T4/T8/T9;
  `resolve_tools -> tuple[str, ...]` (T1) consumed in T4;
  `resolve_n_ctx(base_url, model, context_budget, http_get) -> (int, str)` (T6) monkeypatched
  with the same shape in T8; `Briefing(text, tokens, sections, radius, errors)` (T7)
  constructed identically in T8's fakes; `build_policies` signature untouched (T5). ✓
- **Known judgment calls for the implementer:** exact `test_agent.py` fixture names (read the
  file first); `analyze.py` report-assembly location; `PlayerSpec` immutability workaround in
  one T5 test uses `object.__setattr__` (acceptable in tests only).
