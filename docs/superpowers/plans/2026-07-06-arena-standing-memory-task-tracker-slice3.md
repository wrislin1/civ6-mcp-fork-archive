# Arena Standing Memory and Task Tracker (Slice 3) Implementation Plan

## Status - 2026-07-06

- **Design approved:** Option 1, integrated behavior slice.
- **Live-test target:** 8 total civs, player 0 human, 3 LLM-controlled puppet civs, and 4 normal game AI civs.
- **A/B testing status:** Complete. Slice 3 analysis is performance/behavior testing over N puppeted civs, not treatment/control comparison.
- **Next slice note:** Option 2, broader deterministic autonomy for judgment-heavy actions, is explicitly deferred to Slice 4 after live testing Slice 3 with the 3-LLM behavior game.

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` or `superpowers:executing-plans` to implement this plan task by task. Use checkbox (`- [ ]`) status tracking. Do not expand into Option 2 during this slice.

**Goal:** Give arena puppets cross-turn continuity and behavior-test coverage across local and CLI drivers: standing memory, deterministic low-risk unit-task follow-through, Great People and strategic briefing gaps, behavior-critical arena tools, neutral behavior reporting, and a 3-LLM puppet config for the next 8-civ live test.

**Architecture:** Slice 3 adds two bounded run-local state layers:

1. **Standing memory:** a short per-run/per-player text block captured from the puppet's final summary and injected into its next turn. This is not the global Civ diary and must not leak across runs.
2. **Deterministic task tracker:** a per-run/per-player JSON task state for low-risk unit logistics. It runs before the model turn, resolves current `unit_index` from stable `unit_id`, executes only safe civilian/logistics follow-through, and injects the results into the prompt.

Both layers are shared by in-process local agents and CLI agents. The model still makes judgment calls. Slice 3 does not automate war planning, trade valuation, diplomacy choices, World Congress strategy, Great People choices, or city-capture decisions.

**Tech Stack:** Python 3.12, `pytest`, `uv`, existing `civ_mcp.arena` coordinator/policy stack, existing `GameState` methods and narrators.

---

## Scope Boundary

Implement:

- Standing memory for local and CLI arena agents.
- Deterministic pre-model follow-through for low-risk `settle` and `builder_improve` unit tasks.
- Explicit `STANDING PLAN` prompt contract and parser.
- CLI support for shared behavior options: `playbook`, `briefing`, `memory`, and `task_tracker`.
- Great People briefing section and playbook doctrine.
- Behavior-critical arena tool additions for Great People, trade routes, religion, World Congress, city ranged attacks/capture resolution, government/dedication blockers, global settling, and city production lookup.
- Behavior/performance analysis fields in transcripts and reports.
- New behavior test config with exactly 3 LLM puppet seats.
- Spec/status documentation updates.

Do not implement in Slice 3:

- Option 2 deterministic judgment automation.
- Automated war planning, peace/trade valuation, GP recruitment, religion founding, World Congress voting, espionage, or city-capture policy.
- Generic raw `unit_action`, raw `city_action`, `run_lua`, save/load, kill/launch, or other lifecycle/destructive tools in the arena registry.
- Cross-run memory via `civ_mcp.diary`.

## File Structure

- Add: `src/civ_mcp/arena/memory.py`
- Add: `src/civ_mcp/arena/task_tracker.py`
- Add: `src/civ_mcp/arena/prompting.py`
- Add: `tests/arena/test_memory.py`
- Add: `tests/arena/test_task_tracker.py`
- Add: `tests/arena/test_prompting.py`
- Add: `experiments/arena-behavior-3llm-slice3.yaml`
- Modify: `src/civ_mcp/arena/config.py`
- Modify: `src/civ_mcp/arena/experiment.py`
- Modify: `src/civ_mcp/arena/arena.py`
- Modify: `src/civ_mcp/arena/agent.py`
- Modify: `src/civ_mcp/arena/cli_agent.py`
- Modify: `src/civ_mcp/arena/coordinator.py`
- Modify: `src/civ_mcp/arena/briefing.py`
- Modify: `src/civ_mcp/arena/registry.py`
- Modify: `src/civ_mcp/arena/vocab.py`
- Modify: `src/civ_mcp/arena/playbook.md`
- Modify: `src/civ_mcp/arena/analyze.py`
- Modify: `tests/arena/test_experiment.py`
- Modify: `tests/arena/test_registry.py`
- Modify: `tests/arena/test_analyze.py`
- Modify: `docs/superpowers/specs/2026-07-05-arena-puppet-decision-making-design.md`

---

## Task 0: Branch Setup

**Files:**
- No source changes.

- [ ] **Step 1: Create an isolated implementation worktree**

Run:

```bash
git status --short --branch
git worktree add /home/riz/.config/superpowers/worktrees/civ6-mcp/arena-standing-memory-task-tracker-slice3 -b arena-standing-memory-task-tracker-slice3 main
cd /home/riz/.config/superpowers/worktrees/civ6-mcp/arena-standing-memory-task-tracker-slice3
```

Expected:

```text
Preparing worktree (new branch 'arena-standing-memory-task-tracker-slice3')
```

- [ ] **Step 2: Confirm the worktree is clean**

Run:

```bash
git status --short --branch
```

Expected: branch is `arena-standing-memory-task-tracker-slice3`; no tracked file changes.

---

## Task 1: Shared Behavior Options

**Files:**
- Modify: `src/civ_mcp/arena/config.py`
- Modify: `src/civ_mcp/arena/experiment.py`
- Modify: `src/civ_mcp/arena/arena.py`
- Modify: `tests/arena/test_experiment.py`

- [ ] **Step 1: Add config dataclasses**

In `src/civ_mcp/arena/config.py`, add frozen dataclasses:

```python
@dataclass(frozen=True)
class MemoryOptions:
    enabled: bool = False
    max_chars: int = 1200


@dataclass(frozen=True)
class TaskTrackerOptions:
    enabled: bool = False
    max_tasks: int = 8
```

Add fields to `CivOptions`:

```python
memory: MemoryOptions = field(default_factory=MemoryOptions)
task_tracker: TaskTrackerOptions = field(default_factory=TaskTrackerOptions)
```

Extend `CivOptions.fingerprint()` with:

```python
"memory": {"enabled": self.memory.enabled, "max_chars": self.memory.max_chars},
"task_tracker": {"enabled": self.task_tracker.enabled, "max_tasks": self.task_tracker.max_tasks},
```

- [ ] **Step 2: Split local-only and shared YAML knobs**

In `src/civ_mcp/arena/experiment.py`:

- The non-local rejection at `experiment.py:175-178` keys off the `_LOCAL_KNOBS` tuple (`experiment.py:22-29`), which **currently** contains `tools`, `result_char_cap`, `max_steps`, `playbook`, `context_budget`, `briefing`. You must **remove** `playbook`, `context_budget`, and `briefing` from `_LOCAL_KNOBS` — otherwise CLI civs that set them are still rejected.
- Keep these local-only in `_LOCAL_KNOBS`: `tools`, `result_char_cap`, `max_steps`. (`gateway` is already a separate top-level `_CIV_KEYS` entry, not a member of `_LOCAL_KNOBS`; it must still be rejected for non-local providers — the existing check at `experiment.py:175-178` handles `gateway` distinctly, so preserve that.)
- Treat these as shared behavior knobs for local and CLI: `playbook`, `context_budget`, `briefing`, `memory`, `task_tracker`. Add `memory` and `task_tracker` to `_CIV_KEYS` (and, being shared, they must NOT be in `_LOCAL_KNOBS`).
- Net effect: `_CIV_KEYS` = `{"player", "provider", "model", "gateway", *_LOCAL_KNOBS, "playbook", "context_budget", "briefing", "memory", "task_tracker"}` with `_LOCAL_KNOBS` = `("tools", "result_char_cap", "max_steps")`.
- For `provider != "local"`, reject only `_LOCAL_KNOBS` members and `gateway`, not the shared behavior knobs.
- Require non-empty `model` only for local providers; keep CLI `model` optional.

- [ ] **Step 3: Add parsers**

Add helpers:

```python
def _parse_memory(civ_label: str, raw: object) -> MemoryOptions:
    ...


def _parse_task_tracker(civ_label: str, raw: object) -> TaskTrackerOptions:
    ...
```

Validation:

- Mapping only.
- Keys for memory: `enabled`, `max_chars`.
- Keys for task tracker: `enabled`, `max_tasks`.
- Boolean fields must be real booleans, not strings.
- `max_chars` and `max_tasks` must be positive integers.

- [ ] **Step 4: Parse shared options for every provider**

In `_parse_civ`, build a `CivOptions` for CLI providers too. For CLI providers, use defaults for local-only fields and parsed values for shared fields.

Expected shape:

```python
opts = CivOptions(
    tools=tools,
    result_char_cap=cap,
    max_steps=steps,
    playbook=playbook,
    context_budget=budget,
    briefing=briefing,
    memory=memory,
    task_tracker=task_tracker,
)
return PlayerSpec(player_id, provider, model, gateway, opts)
```

For CLI, `tools`, `result_char_cap`, and `max_steps` remain defaults.

- [ ] **Step 5: Pass options into CLI policies**

In `src/civ_mcp/arena/arena.py`, change `build_policies` so CLI construction passes `spec.options`:

```python
policies[spec.player_id] = CLIAgentPolicy(
    spec.provider,
    cost,
    project_dir=os.getcwd(),
    model=spec.model,
    options=spec.options,
)
```

- [ ] **Step 6: Add experiment tests**

In `tests/arena/test_experiment.py`, add tests that:

- Local providers parse `memory` and `task_tracker`.
- CLI providers parse `playbook`, `briefing`, `memory`, and `task_tracker`.
- CLI providers still reject `tools`, `result_char_cap`, `max_steps`, and `gateway`.
- Invalid `memory.enabled: "true"` and `task_tracker.max_tasks: 0` fail with clear errors.
- `CivOptions.fingerprint()` contains `memory` and `task_tracker`.

- [ ] **Step 7: Run targeted config tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_experiment.py -q
```

Expected: all tests in `test_experiment.py` pass.

---

## Task 2: Standing Memory Store and Parser

**Files:**
- Add: `src/civ_mcp/arena/memory.py`
- Add: `tests/arena/test_memory.py`

- [ ] **Step 1: Add run-local memory data model**

Create `src/civ_mcp/arena/memory.py` with:

```python
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class StandingMemory:
    schema_version: int
    run_id: str
    player_id: int
    updated_turn: int
    text: str
```

Storage path:

```text
<transcript_dir>/<run_id>/memory/player_<player_id>.json
```

Do not use `civ_mcp.diary`.

- [ ] **Step 2: Add path and IO helpers**

Implement:

```python
def run_dir(transcript_dir: str, run_id: str) -> Path
def memory_path(transcript_dir: str, run_id: str, player_id: int) -> Path
def load_memory(transcript_dir: str, run_id: str, player_id: int) -> StandingMemory | None
def save_memory(transcript_dir: str, run_id: str, player_id: int, turn: int, text: str, max_chars: int) -> StandingMemory
```

Requirements:

- Create parent directories.
- Clamp text to `max_chars`.
- Strip leading/trailing whitespace.
- Write JSON with `schema_version`, `run_id`, `player_id`, `updated_turn`, and `text`.
- Use a temporary file plus `Path.replace()` for atomic best-effort writes.
- Never raise on malformed existing JSON from `load_memory`; return `None`.

- [ ] **Step 3: Add final-summary extraction**

Implement:

```python
def extract_standing_plan(summary: str, max_chars: int) -> str:
    ...
```

Rules:

- Find a case-insensitive line that starts with `STANDING PLAN:`.
- Capture that line's content plus following non-empty lines until one of:
  - a new all-caps section header ending in `:`
  - end of string
- Remove markdown bullets only from the left edge.
- Clamp to `max_chars`.
- Return `""` when no standing plan is present.

Accepted examples:

```text
STANDING PLAN:
- Keep settler 123 marching to (18,24).
- TASK settle unit_id=123 target=18,24
```

```text
Standing Plan: finish archer movement, then settle unit_id=123 at 18,24.
```

- [ ] **Step 4: Add prompt block formatter**

Implement:

```python
def format_memory_block(memory: StandingMemory | None) -> str:
    ...
```

Output exactly:

```text
== STANDING PLAN FROM LAST TURN ==
<memory text>
```

Return `""` when no memory or empty text exists.

- [ ] **Step 5: Add tests**

`tests/arena/test_memory.py` must cover:

- Missing file returns `None`.
- Malformed JSON returns `None`.
- Save/load round trip.
- Text is clamped.
- Extraction handles multiline block.
- Extraction handles inline `STANDING PLAN: ...`.
- Extraction returns empty when marker is absent.
- Formatter returns the exact heading.

- [ ] **Step 6: Run memory tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_memory.py -q
```

Expected: all memory tests pass.

---

## Task 3: Deterministic Low-Risk Unit Task Tracker

**Files:**
- Add: `src/civ_mcp/arena/task_tracker.py`
- Add: `tests/arena/test_task_tracker.py`

- [ ] **Step 1: Add task data model**

Create `src/civ_mcp/arena/task_tracker.py` with:

```python
TASK_KINDS = {"settle", "builder_improve"}


@dataclass(frozen=True)
class UnitTask:
    task_id: str
    kind: str
    unit_id: int
    target_x: int
    target_y: int
    created_turn: int
    updated_turn: int
    improvement: str = ""
    status: str = "active"
    last_result: str = ""


@dataclass(frozen=True)
class TaskState:
    schema_version: int
    run_id: str
    player_id: int
    tasks: tuple[UnitTask, ...]
```

Storage path:

```text
<transcript_dir>/<run_id>/tasks/player_<player_id>.json
```

- [ ] **Step 2: Add IO helpers**

Implement:

```python
def task_path(transcript_dir: str, run_id: str, player_id: int) -> Path
def load_task_state(transcript_dir: str, run_id: str, player_id: int) -> TaskState
def save_task_state(transcript_dir: str, run_id: str, player_id: int, tasks: Sequence[UnitTask]) -> TaskState
```

Requirements:

- Malformed JSON returns an empty state.
- Only active tasks are persisted unless a test needs recent completed state.
- Enforce max tasks at capture time, not load time.
- Use atomic best-effort writes.

- [ ] **Step 3: Parse explicit task lines from standing plans**

Implement:

```python
def parse_task_lines(plan_text: str, turn: int) -> list[UnitTask]:
    ...
```

Supported syntax:

```text
TASK settle unit_id=123 target=18,24
TASK builder_improve unit_id=456 target=12,19 improvement=IMPROVEMENT_FARM
CANCEL unit_id=123
```

Rules:

- `unit_id`, `target`, and kind are required for `TASK`.
- `improvement` is required for `builder_improve`.
- `task_id` format is `<kind>:<unit_id>`.
- Ignore invalid lines rather than raising.
- `CANCEL` lines produce a task with matching `task_id`, status `cancelled`, and no action.

- [ ] **Step 4: Upsert parsed tasks**

Implement:

```python
def merge_tasks(existing: Sequence[UnitTask], updates: Sequence[UnitTask], max_tasks: int) -> tuple[UnitTask, ...]:
    ...
```

Rules:

- `TASK` updates replace tasks with the same `task_id`.
- `CANCEL` marks the existing task cancelled and removes it from active persistence.
- Existing active tasks persist when a turn has no task lines.
- Keep the newest `max_tasks` active tasks.

- [ ] **Step 5: Add pre-model follow-through**

Implement:

```python
async def run_pre_model_tasks(gs: Any, tasks: Sequence[UnitTask]) -> tuple[tuple[UnitTask, ...], list[dict[str, Any]]]:
    ...
```

Execution rules:

- Resolve current units by stable `unit_id`; use the current `unit_index` for actions.
- If the unit is missing, mark task `status="lost"` with `last_result`.
- If `moves_remaining <= 0`, keep task active and record `skipped_no_moves`.
- Before moving a settler or builder, call `gs.get_map_area(current_x, current_y, 2)` and `gs.get_map_area(target_x, target_y, 2)` (signature is `get_map_area(center_x, center_y, radius=2)`, `game_state.py:191`).
- Check the **`TileInfo.units`** field (`models.py:315`), which is `list[str] | None` holding *visible foreign* unit descriptions (e.g. `["Barbarian WARRIOR"]`) — this is exactly the hostile-adjacency signal you want. Do NOT use `own_units`. Guard for `None`: `if any(t.units for t in tiles)`. If truthy, do not move; keep task active and record `blocked_visible_hostile`.
- Do not attack, fortify, escort, purchase, chop, recruit, vote, trade, or make diplomacy choices.
- For `settle`:
  - If current position equals target, call `gs.found_city(unit_index)`.
  - Mark complete when the result does not start with `Error:`.
  - Otherwise call `gs.move_unit(unit_index, target_x, target_y)`.
- For `builder_improve`:
  - If current position equals target and `improvement` is in `unit.valid_improvements`, call `gs.improve_tile(unit_index, improvement)`.
  - Mark complete when the result does not start with `Error:`.
  - If current position equals target but the improvement is not valid, keep task active and record `blocked_improvement_not_valid`.
  - Otherwise call `gs.move_unit(unit_index, target_x, target_y)`.
- Catch per-task exceptions and record `error:<repr>` without aborting the turn.

Each result dict must include:

```python
{
    "task_id": "...",
    "kind": "...",
    "unit_id": 123,
    "target": [18, 24],
    "status": "active|complete|lost|cancelled",
    "action": "move|found_city|improve|skip|block|error",
    "result": "...",
}
```

- [ ] **Step 6: Add task block formatter**

Implement:

```python
def format_task_block(tasks: Sequence[UnitTask], results: Sequence[dict[str, Any]]) -> str:
    ...
```

Output starts with:

```text
== DETERMINISTIC TASK TRACKER ==
```

Include at most 8 active task lines and at most 8 result lines. Return `""` when both are empty.

- [ ] **Step 7: Add tests**

`tests/arena/test_task_tracker.py` must cover:

- Save/load round trip and malformed JSON.
- Parse valid `settle`, `builder_improve`, and `CANCEL` lines.
- Invalid task lines are ignored.
- Merge keeps existing active tasks when no updates are present.
- Merge replaces by `task_id`.
- Merge respects `max_tasks`.
- Pre-model settle resolves current `unit_index` from `unit_id`.
- Pre-model settle calls `found_city` at target.
- Pre-model builder calls `improve_tile` only when the requested improvement is valid.
- Visible foreign units in current or target radius block civilian movement.
- Missing unit marks task lost.
- Formatter includes the exact heading and bounded content.

- [ ] **Step 8: Run task tracker tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_task_tracker.py -q
```

Expected: all task tracker tests pass.

---

## Task 4: Shared Prompt Assembly for Local and CLI Agents

**Files:**
- Add: `src/civ_mcp/arena/prompting.py`
- Modify: `src/civ_mcp/arena/agent.py`
- Modify: `src/civ_mcp/arena/cli_agent.py`
- Add: `tests/arena/test_prompting.py`
- Modify: `tests/arena/test_cli_agent.py`

- [ ] **Step 1: Add prompt builder**

Create `src/civ_mcp/arena/prompting.py` with:

```python
STANDING_PLAN_INSTRUCTION = """End your final response with:
STANDING PLAN:
- One to three short bullets for next turn.
- Optional task lines, for example:
  TASK settle unit_id=123 target=18,24
  TASK builder_improve unit_id=456 target=12,19 improvement=IMPROVEMENT_FARM
"""


def build_opening_prompt(
    *,
    player_id: int,
    turn: int,
    briefing_text: str = "",
    memory_block: str = "",
    task_block: str = "",
    include_standing_plan_instruction: bool = False,
) -> str:
    ...
```

Ordering must be:

1. `briefing_text`
2. `memory_block`
3. `task_block`
4. `It is turn {turn}. You control player {player_id}. Begin.`
5. `STANDING_PLAN_INSTRUCTION` when memory or task tracking is enabled

Omit empty blocks without extra blank lines.

- [ ] **Step 2: Add CLI playbook loading**

In `src/civ_mcp/arena/cli_agent.py`:

- Add `options: CivOptions | None = None` to `CLIAgentPolicy.__init__`.
- Store `self.options = options or CivOptions()`.
- Store `self._system_prefix = load_playbook()` equivalent behavior only when `self.options.playbook == "condensed"`.
- Do not change MCP lockdown env vars.

- [ ] **Step 3: Let CLI build prompt inside `__call__`**

Change `_build_argv` to accept a final prompt string:

```python
def _build_argv(self, prompt: str) -> list[str]:
    ...
```

In `__call__`, build the prompt before spawning:

- Build `briefing` if `self.options.briefing.enabled`, using `build_briefing`.
- Use `DEFAULT_N_CTX` from `arena.budget` for CLI briefing budget.
- Include playbook text at the top of the prompt for CLI when condensed playbook is enabled, because CLI mode does not have the local `SYSTEM` message.
- Use `build_opening_prompt(...)`.

The CLI prompt must still contain the existing core instruction. The current template is `_PROMPT` in `src/civ_mcp/arena/cli_agent.py:71-77`:

```text
You are playing player {pid} (an AI civ) in the running Civilization VI game; it is
turn {turn} and YOU are currently the active player. Use the civ6 tools to observe your
situation and take a few sensible early-game actions (scout, move/settle a settler, set
city production and research). Do NOT end the turn — the host ends it for you. When done,
give a one-line summary.
```

Preserve the **`Do NOT end the turn — the host ends it for you.`** clause verbatim — it is load-bearing (the host ends turns; if the CLI agent ends its own turn the coordinator handoff breaks). See Step 3b below for the required change to the trailing "give a one-line summary" instruction when memory/task tracking is enabled.

- [ ] **Step 3b: Preserve the standing-plan block through CLI summary capture**

Memory/task capture (Task 5 Step 4) parses the `STANDING PLAN:` block out of the policy's returned summary. Two things in `cli_agent.py` currently defeat this on the CLI path and MUST be fixed here, or CLI puppets will never capture memory — silently breaking the CLI+local parity this slice exists to validate:

1. **The summary is truncated to `[:500]`** in `_parse_claude` / `_parse_codex` (`cli_agent.py:162`, `summary = str(item.get("text") or "")[:500]`). A `STANDING PLAN` block (1-3 bullets plus `TASK …` lines) can exceed 500 chars or fall past the cutoff. Raise this clamp to at least `max(1200, options.memory.max_chars)` when memory or task tracking is enabled, so the trailing standing-plan region survives. Do not remove the clamp entirely — keep a generous bound (e.g. 4000) to avoid unbounded transcript growth.
2. **The prompt asks for a "one-line summary."** When `self.options.memory.enabled` or `self.options.task_tracker.enabled` is true, the CLI prompt tail must instead instruct the model to end with the multi-line `STANDING PLAN:` block (reuse `STANDING_PLAN_INSTRUCTION` from `prompting.py`). Keep the plain "give a one-line summary" tail only when both are disabled.

- [ ] **Step 3c: Add a CLI standing-plan-survival test**

In `tests/arena/test_cli_agent.py` (or `test_prompting.py`), add a test that feeds a fake CLI stdout whose final message contains a multi-line `STANDING PLAN:` block longer than 500 chars, and assert the returned `summary` / `transcript["final_summary"]` still contains the full `STANDING PLAN:` region so `extract_standing_plan` can recover it. This is the regression guard for the `[:500]` clamp.

- [ ] **Step 4: Refactor local policy opening**

In `src/civ_mcp/arena/agent.py`, replace local ad hoc opening construction with `build_opening_prompt(...)`.

At this task stage, pass empty `memory_block` and `task_block`. Coordinator wiring comes later.

- [ ] **Step 5: Extend transcripts**

Both local and CLI transcripts must include:

```python
"prompt_injections": {
    "memory": bool(memory_block),
    "task_tracker": bool(task_block),
    "standing_plan_instruction": include_standing_plan_instruction,
}
```

At this task stage, memory/task booleans can be false until coordinator wiring passes blocks in.

- [ ] **Step 6: Add tests**

`tests/arena/test_prompting.py` must verify:

- Block order.
- Empty blocks are omitted cleanly.
- Standing plan instruction appears only when requested.
- Local policy uses `build_opening_prompt` via a fake backend receiving the opening message.
- CLI `_build_argv(prompt)` preserves the full prompt for both `cli-claude` and `cli-codex`.

- [ ] **Step 7: Run prompt tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_prompting.py -q
```

Expected: all prompt tests pass.

---

## Task 5: Coordinator Integration for Memory and Tasks

**Files:**
- Modify: `src/civ_mcp/arena/coordinator.py`
- Modify: `src/civ_mcp/arena/agent.py`
- Modify: `src/civ_mcp/arena/cli_agent.py`
- Modify: `tests/arena/test_memory.py`
- Modify: `tests/arena/test_task_tracker.py`

- [ ] **Step 1: Extend policy call signatures**

Change both `LLMPolicy.__call__` and `CLIAgentPolicy.__call__` to accept optional injected blocks:

```python
async def __call__(
    self,
    gs,
    player_id: int,
    turn: int,
    *,
    memory_block: str = "",
    task_block: str = "",
) -> dict:
    ...
```

Update all direct test fakes or scripted policies to accept `**kwargs` if needed.

- [ ] **Step 2: Load and run pre-model state in coordinator**

In `run_arena`, before the policy call and after `state_before`:

1. Resolve `run_id = config.run_id`.
2. Resolve `transcript_dir = config.transcript_dir`.
3. Load standing memory only if `pol.options.memory.enabled` is true.
4. Load task state only if `pol.options.task_tracker.enabled` is true.
5. Run `task_tracker.run_pre_model_tasks(gs, active_tasks)` before the model turn.
6. Save updated task state immediately after pre-model results.
7. Build `memory_block` and `task_block`.

Use `getattr(pol, "options", CivOptions())` so scripted and legacy policies still work.

- [ ] **Step 3: Pass blocks into policy**

Change:

```python
result = await pol(gs, st.local, st.turn)
```

to:

```python
result = await pol(gs, st.local, st.turn, memory_block=memory_block, task_block=task_block)
```

Keep exclusive tuner disconnect/reconnect behavior unchanged for CLI agents.

- [ ] **Step 4: Capture standing plan and tasks after policy**

After the policy returns and before transcript/log write:

- Read `final_summary` from `result["transcript"]["final_summary"]` when available; fallback to `result["summary"]`.
- If memory is enabled, call `extract_standing_plan(...)`.
- If extracted text is non-empty, save it with `save_memory(...)`.
- If task tracker is enabled, parse task lines from the extracted standing plan, merge with current task state, and save.
- Do not erase previous memory when the current turn has no `STANDING PLAN`.
- Do not erase existing active tasks when the current turn has no task lines.

- [ ] **Step 5: Add transcript and log fields**

Add to coordinator log entries:

```python
"standing_memory": {
    "loaded": bool(memory),
    "injected_chars": len(memory_block),
    "captured_chars": len(captured_plan),
},
"task_tracker": {
    "active_before": len(active_tasks_before),
    "pre_model_results": task_results,
    "active_after": len(active_tasks_after),
},
```

Add the same fields to transcript records.

- [ ] **Step 6: Keep promotion sweep order**

Final turn order must be:

1. Load memory/tasks.
2. Run deterministic pre-model task follow-through.
3. Run policy.
4. Reconnect exclusive CLI tuner if needed.
5. Run promotion sweep.
6. Capture memory/tasks from final summary.
7. Snapshot and write transcript/log.
8. `finish_units` and `restore_local`.

Do not move the existing promotion sweep before the model turn.

- [ ] **Step 7: Add integration tests with fake policy**

Add tests that simulate:

- Memory from turn N is injected on turn N+1.
- A final summary with `STANDING PLAN` saves memory.
- A final summary with `TASK settle ...` creates a persisted task.
- Pre-model task results are included in transcript/log.
- A CLI-style policy with `needs_exclusive_tuner=True` still receives memory/task blocks.

- [ ] **Step 8: Run coordinator-related tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_memory.py tests/arena/test_task_tracker.py tests/arena/test_prompting.py -q
```

Expected: all tests pass.

---

## Task 6: Great People Briefing and Playbook Doctrine

**Files:**
- Modify: `src/civ_mcp/arena/briefing.py`
- Modify: `src/civ_mcp/arena/config.py`
- Modify: `src/civ_mcp/arena/playbook.md`
- Modify: `tests/arena/test_experiment.py`

- [ ] **Step 1: Add `great_people` briefing section**

In `src/civ_mcp/arena/config.py`, add `"great_people"` to `VALID_SECTIONS`.

In `src/civ_mcp/arena/briefing.py`:

- Add `_great_people(gs, ctx)`.
- Register it in `_BUILDERS`.
- Add it to `_ORDER` after `empire_resources` and before `rivals`.

Implementation:

```python
async def _great_people(gs: Any, ctx: dict[str, Any]) -> str:
    gp = await gs.get_great_people()
    if isinstance(gp, str):
        return gp
    text = nr.narrate_great_people(gp)
    ...
```

Render only concise content. If `nr.narrate_great_people` is too long, keep:

- Recruitable candidates.
- Candidates where the puppet is close to recruitment.
- Candidates where a rival is close.
- Patronage cost when available.

- [ ] **Step 2: Add briefing tests**

Update existing briefing/config tests to verify:

- `great_people` is accepted in YAML.
- The section renders when configured.
- The section is omitted when not configured.

- [ ] **Step 3: Update playbook**

Append concise sections to `src/civ_mcp/arena/playbook.md`:

- `## Standing plan` - end every turn with `STANDING PLAN:` and use `TASK` lines for multi-turn settler/builder work.
- `## Great People` - recruit available GPs promptly, use `get_great_people`, use `get_gp_advisor` for placement, do not delete GP units.
- `## Trade routes` - check `get_trade_routes`; idle routes are free yields; use `get_trade_destinations` then `start_trade_route`.
- `## World Congress and religion` - monitor but make explicit votes/belief choices only after reading the relevant tool output.

Keep additions concise. This playbook is prompt budget.

- [ ] **Step 4: Run briefing/config tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_experiment.py tests/arena/test_prompting.py -q
```

Expected: all tests pass.

---

## Task 7: Behavior-Critical Arena Tools

**Files:**
- Modify: `src/civ_mcp/arena/registry.py`
- Modify: `src/civ_mcp/arena/vocab.py`
- Modify: `tests/arena/test_registry.py`
- Modify: `tests/arena/test_analyze.py`

- [ ] **Step 1: Add read wrappers**

Add arena registry tools:

- `get_city_production(city_id)` -> `gs.list_city_production(city_id)` plus existing production narrator used by the server.
- `get_global_settle_advisor()` -> `gs.get_global_settle_scan()` plus settle narration.
- `get_governors()` -> existing `gs.get_governors()` plus narrator.
- `get_dedications()` -> existing `gs.get_dedications()` plus narrator.
- `get_religion_beliefs()` -> `gs.get_religion_founding_status()` plus `nr.narrate_religion_founding_status`. (The arena *tool* is named `get_religion_beliefs`, but there is no `gs.get_religion_beliefs()` method — the GameState method is `get_religion_founding_status()`, returning `lq.ReligionFoundingStatus`, at `game_state.py:1152`.)
- `get_religion_spread()` -> existing `gs.get_religion_status()` plus `nr.narrate_religion_status`.
- `get_trade_routes()` -> `gs.get_trade_routes()` plus `nr.narrate_trade_routes`.
- `get_trade_destinations(unit_id)` -> resolve composite `unit_id` to current `unit_index`, then `gs.get_trade_destinations(unit_index)` plus `nr.narrate_trade_destinations`.
- `get_gp_advisor(unit_id)` -> resolve composite `unit_id` to current `unit_index`, then `gs.get_gp_advisor(unit_index)` plus `nr.narrate_gp_advisor`.
- `get_world_congress()` -> `gs.get_world_congress()` plus `nr.narrate_world_congress`.

If a required narrator does not exist for one of these, add a small local formatter in `registry.py` rather than changing Lua or `GameState`.

- [ ] **Step 2: Add action wrappers**

Add arena registry tools:

- `promote_governor(governor_type, promotion_type)` -> `gs.promote_governor(...)`
- `choose_dedication(dedication_index)` -> `gs.choose_dedication(...)`
- `found_religion(religion_name, follower_belief, founder_belief)` -> `gs.found_religion(...)`
- `recruit_great_person(individual_id)` -> `gs.recruit_great_person(...)`
- `patronize_great_person(individual_id, yield_type)` -> `gs.patronize_great_person(...)`
- `reject_great_person(individual_id)` -> `gs.reject_great_person(...)`
- `start_trade_route(unit_id, target_x, target_y)` -> resolve `unit_id`, then `gs.make_trade_route(...)`
- `teleport_trader(unit_id, target_x, target_y)` -> resolve `unit_id`, then `gs.teleport_to_city(...)`
- `queue_wc_votes(votes)` -> parse JSON string/list, validate list of dicts, then `gs.queue_wc_votes(...)`
- `city_attack(city_id, target_x, target_y)` -> `gs.city_attack(...)`
- `resolve_city_capture(action)` -> accept only `keep`, `raze`, `liberate_founder`, `liberate_previous`, then `gs.resolve_city_capture(...)`

Use discrete names. Do not add generic `unit_action` or `city_action`.

- [ ] **Step 3: Add verbs**

In `registry.py`, set `verb=` for action tools:

```python
"promote_governor"
"choose_dedication"
"found_religion"
"recruit_great_person"
"patronize_great_person"
"reject_great_person"
"start_trade_route"
"teleport_trader"
"queue_wc_votes"
"city_attack"
"resolve_city_capture"
```

Mirror them exactly in `src/civ_mcp/arena/vocab.py`.

- [ ] **Step 4: Keep tier boundary**

All new tools in this task are `full` only through the existing `full = tuple(TOOL_REGISTRY)` behavior. Do not add them to `minimal` or `standard`.

- [ ] **Step 5: Add registry tests**

In `tests/arena/test_registry.py`, add tests that:

- New tools are registered and full-only.
- Raw/lifecycle tools remain absent: `unit_action`, `city_action`, `run_lua`, `load_game_save`, `load_save`, `restart_and_load`, `kill_game`, `launch_game`, `list_saves`, `end_turn`.
- `get_trade_destinations`, `get_gp_advisor`, `start_trade_route`, and `teleport_trader` resolve `unit_id` to current `unit_index`.
- `queue_wc_votes` rejects malformed JSON and non-list payloads.
- `resolve_city_capture` rejects unknown actions.
- Vocab mirrors registry verbs.

- [ ] **Step 6: Add analysis verb tests**

In `tests/arena/test_analyze.py`, add explicit checks that the new action verbs are normalized in the step verb metrics.

- [ ] **Step 7: Run registry/analyze tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_registry.py tests/arena/test_analyze.py -q
```

Expected: all tests pass.

---

## Task 8: Neutral Behavior and Performance Analysis

**Files:**
- Modify: `src/civ_mcp/arena/analyze.py`
- Modify: `tests/arena/test_analyze.py`

- [ ] **Step 1: Add behavior metrics**

Extend analysis JSON with a new top-level key:

```python
"behavior": {
    "standing_memory_turns": int,
    "standing_memory_captured_turns": int,
    "task_tracker_turns": int,
    "task_pre_model_actions": int,
    "task_completed": int,
    "task_blocked_visible_hostile": int,
    "task_lost": int,
    "drivers": {"in_process": int, "cli": int},
    "puppeted_players": [...],
}
```

Populate from transcript fields added in Task 5.

- [ ] **Step 2: Preserve existing A/B analysis without centering it**

Do not delete existing A/B code. Add a neutral behavior section to the Markdown report before model rubrics:

```markdown
## Behavior Metrics
```

This section must not call players treatment/control. Use `player_id`, `driver`, `provider`, and `model`.

- [ ] **Step 3: Add CLI and task fields to per-player summaries**

Per-player summaries should include:

- driver
- provider/model
- standing memory injected turns
- standing memory captured turns
- task follow-through attempts
- task completions
- task blocked/lost counts
- Great People tool calls
- trade route tool calls
- religion/world congress tool calls

- [ ] **Step 4: Add tests**

Add fixture records in `tests/arena/test_analyze.py` covering:

- One in-process record with memory/task fields.
- One CLI record with memory/task fields.
- Report JSON includes behavior metrics.
- Markdown contains `## Behavior Metrics`.
- No treatment/control wording is required for behavior metrics.

- [ ] **Step 5: Run analysis tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_analyze.py -q
```

Expected: all analysis tests pass.

---

## Task 9: 3-LLM Behavior Test Config

**Files:**
- Add: `experiments/arena-behavior-3llm-slice3.yaml`
- Modify: `tests/arena/test_experiment.py`

- [ ] **Step 1: Add behavior config**

Create `experiments/arena-behavior-3llm-slice3.yaml`.

Use exactly these seats:

- Player 0: human, omitted from `civs`.
- Players `1`, `3`, and `5`: LLM puppets.
- Players `2`, `4`, `6`, and `7`: omitted from `civs`, so they remain regular game AI.

Use local Gemma settings matching the previous full-treatment arm:

```yaml
# Slice 3: behavior/performance run.
#
# Human plays seat 0. Only seats 1,3,5 are LLM-controlled puppets.
# Seats 2,4,6,7 are omitted intentionally and remain regular game AI.
# This is not an A/B config.
max_puppet_turns: 140
idle_poll_limit: 3600
civs:
  - player: 1
    provider: local
    model: gemma4-26b
    gateway: http://192.168.20.196:11440/v1
    tools: full
    result_char_cap: 6000
    max_steps: 10
    playbook: condensed
    context_budget: auto
    memory:
      enabled: true
      max_chars: 1200
    task_tracker:
      enabled: true
      max_tasks: 8
    briefing:
      enabled: true
      map_radius: 3
      sections: [promotions, overview, units, cities, map, research, production_options, threats, rivals, empire_resources, great_people, victory]
  - player: 3
    provider: local
    model: gemma4-26b
    gateway: http://192.168.20.196:11440/v1
    tools: full
    result_char_cap: 6000
    max_steps: 10
    playbook: condensed
    context_budget: auto
    memory:
      enabled: true
      max_chars: 1200
    task_tracker:
      enabled: true
      max_tasks: 8
    briefing:
      enabled: true
      map_radius: 3
      sections: [promotions, overview, units, cities, map, research, production_options, threats, rivals, empire_resources, great_people, victory]
  - player: 5
    provider: local
    model: gemma4-26b
    gateway: http://192.168.20.196:11440/v1
    tools: full
    result_char_cap: 6000
    max_steps: 10
    playbook: condensed
    context_budget: auto
    memory:
      enabled: true
      max_chars: 1200
    task_tracker:
      enabled: true
      max_tasks: 8
    briefing:
      enabled: true
      map_radius: 3
      sections: [promotions, overview, units, cities, map, research, production_options, threats, rivals, empire_resources, great_people, victory]
```

- [ ] **Step 2: Add config load test**

In `tests/arena/test_experiment.py`, add a test that loads this file and asserts:

- `len(cfg.players) == 3`
- player IDs are `[1, 3, 5]`
- every player has `memory.enabled is True`
- every player has `task_tracker.enabled is True`
- every player has `briefing.enabled is True`
- `great_people` is present in every briefing section list

- [ ] **Step 3: Run experiment tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_experiment.py -q
```

Expected: all experiment tests pass.

---

## Task 10: Documentation Updates

**Files:**
- Modify: `docs/superpowers/specs/2026-07-05-arena-puppet-decision-making-design.md`
- Modify: `src/civ_mcp/arena/playbook.md`

- [ ] **Step 1: Update spec status**

Change the status line in `docs/superpowers/specs/2026-07-05-arena-puppet-decision-making-design.md` to reflect:

- Slice 1 implemented and hardened.
- Slice 2 implemented and hardened.
- Slice 3 planned by this document.
- A/B testing is complete.
- Next live validation is 8 civs with 3 LLM puppets and 4 regular AI civs.

- [ ] **Step 2: Update program shape**

Update the program-shape table so Slice 3 is no longer described only as cross-turn memory. It should read as:

```text
3. Standing memory + deterministic low-risk task tracker + behavior tools
```

Add a new row or paragraph:

```text
4. Broader deterministic autonomy after Slice 3 live testing
```

Make clear Slice 4 is Option 2 from brainstorming and waits for Slice 3 live behavior results.

- [ ] **Step 3: Update playbook note**

Ensure `src/civ_mcp/arena/playbook.md` says the agent should end with a `STANDING PLAN` block, but does not claim deterministic tracker can make strategic choices.

- [ ] **Step 4: Run markdown sanity**

Run:

```bash
git diff --check
```

Expected: no whitespace errors.

---

## Task 11: Full Verification

**Files:**
- No new files beyond prior tasks.

- [ ] **Step 1: Run targeted arena tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest \
  tests/arena/test_memory.py \
  tests/arena/test_task_tracker.py \
  tests/arena/test_prompting.py \
  tests/arena/test_registry.py \
  tests/arena/test_experiment.py \
  tests/arena/test_analyze.py \
  -q
```

Expected: all targeted tests pass.

- [ ] **Step 2: Run full suite**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests -q
```

Expected: full suite passes.

- [ ] **Step 3: Run diff whitespace check**

Run:

```bash
git diff --check
```

Expected: no output.

- [ ] **Step 4: Review public behavior boundary**

Manually verify:

- No raw `run_lua` or lifecycle tools were exposed to arena puppets.
- Deterministic task tracker only handles `settle` and `builder_improve`.
- Prompt injection is run-local and bounded.
- CLI agents receive standing memory and task tracker blocks.
- The new behavior config controls exactly players `1`, `3`, and `5`.
- Option 2/Slice 4 is documented but not implemented.

- [ ] **Step 5: Record completion status**

After verification, update this plan's status block with:

- implementation commits
- targeted test result
- full test result
- `git diff --check` result
- live validation status, if a live run was performed

Do not mark live validation complete unless an actual 8-civ / 3-LLM run was observed.

---

## Notes for Slice 4

Slice 4 is Option 2 from brainstorming and should wait until Slice 3 has live behavior results. Candidate Slice 4 work:

- Deterministic war-prep queues.
- Trade valuation helpers.
- Peace timing helpers.
- Great Person recommendation automation.
- World Congress voting policy.
- Religion founding/spread policy helpers.
- Espionage tools and spy mission policy.
- More aggressive unit-task types such as escort, war staging, city attack plans, and trader route optimization.

Slice 4 should start from measured Slice 3 failures, not from theoretical parity.
