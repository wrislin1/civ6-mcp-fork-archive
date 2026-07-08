# Arena Lua-Injection Hardening Implementation Plan

> **Status: EXECUTED + MERGED (2026-07-08).** All 7 tasks implemented via
> subagent-driven-development (`fdfcb40`..`0ed3f4a`), + final-review Critical
> `4110af9` (patronize `yield_type`) + post-review consistency fixes `0de49fb`
> (`_lua_deal_item` branches + GP `individual_id`). Suite 850 green. Merged to
> `main` on all four copies at `0de49fb` (riz "just merge and push"). The
> LLM→Lua injection surface is CLOSED per the inventory; only live-probe
> *testing* of the broader slice-4 toolset remains
> (`2026-07-07-arena-slice4-live-probes.md`).

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Validate every untrusted LLM argument at its GameState-method entry so no string, id, or coordinate can break out of a Lua literal/index, closing the arena's remaining Lua-injection surface.

**Architecture:** Three validation primitives in `lua/_helpers.py` (`_safe_enum` charset whitelist, `_one_of` closed allowlist, `_lua_escape` free-text escaper) plus `int()` coercion, applied once at the entry of each `GameState` method (game_state.py) that forwards an untrusted arg to Lua — the innermost point dominating every sink (builder call + inline verify-query). Two builder-level exceptions: per-token resource validation in `_lua_deal_item`, and `_lua_get_city` int-hardens internally for the many `city_id` sinks that route through it.

**Tech Stack:** Python 3.12 + `uv`; pytest + pytest-asyncio; the game-facing layer emits Lua strings executed over FireTuner via `conn.execute_read`/`execute_write`.

## Global Constraints

Copied from the spec (`docs/superpowers/specs/2026-07-08-arena-lua-injection-hardening-design.md`); bind every task:

- **Enforcement = GameState-method entry** (the innermost function dominating all of a param's sinks). Exceptions: trade resource tokens validate in `_lua_deal_item`; `_lua_get_city` int-hardens internally; existing builder self-defenses are retained.
- **Primitive per param:** `_one_of` (closed allowlist) for small stable domains — `send_diplomatic_action` action (via builder `.get()`-bail, see Task 5), `response`, `alliance_type`, `item_type`, `yield_type`; `_safe_enum` (charset) for large GameInfo-table params; `_lua_escape` for the one free-text param `item_name`; `int()` for `city_id`/`other_player_id`/`city_state_player_id`/`set_city_production` coords.
- **Threat model = untrusted arena path only.** The human-facing FastMCP `run_lua` (server.py:2864) is intentionally retained and OUT OF SCOPE. `run_lua` is never registered in the arena registry.
- **No `_dispatch` schema-coercion pass.** Localized, per-choke-point validation only.
- **Authoritative site map:** `.superpowers/sdd/lua-injection-inventory.md` (committed). Verify each sink there.
- **Happy-path regression is covered by the existing suite** (a wrongly-rejected legit value fails an existing test) plus the Task-1 helper unit tests; domain-task tests assert injection-rejection only.
- **Existing-test migration:** validating at GameState entry means a deliberately-invalid enum now raises `ValueError` instead of the old `ERR:…NOT_FOUND` bail. Any existing test asserting that bail flips to `pytest.raises(ValueError)`. The full suite identifies exactly which; update them in the same task.
- **Test command:** `uv run pytest tests/ -q` (NEVER bare `uv run pytest` — `scripts/` breaks collection). Focused: `uv run pytest tests/path::name -q`.
- Branch stays **UNMERGED**. Do not merge or push.

---

### Task 1: Validation primitives + `_lua_get_city` int-hardening

**Files:**
- Modify: `src/civ_mcp/lua/_helpers.py` (add three helpers; harden `_lua_get_city`)
- Test: `tests/test_lua_injection_hardening.py` (create)

**Interfaces:**
- Produces: `_safe_enum(value, field="value") -> str`, `_one_of(value, allowed: frozenset[str], field="value") -> str`, `_lua_escape(value) -> str` — all in `civ_mcp.lua._helpers`; and `_lua_get_city(city_id)` now raises `ValueError`/`TypeError` on a non-numeric `city_id`. Also creates the shared `NoExecConn` test double.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_lua_injection_hardening.py`:

```python
import pytest
from civ_mcp.lua._helpers import _safe_enum, _one_of, _lua_escape, _lua_get_city


class NoExecConn:
    """A GameConnection double whose Lua execution fails if reached — proves
    validation raised BEFORE any Lua ran. Reused by the GameState-entry tests."""
    async def execute_read(self, lua, timeout=5.0):
        raise AssertionError("Lua executed — validation should have raised first")
    async def execute_write(self, lua, timeout=5.0):
        raise AssertionError("Lua executed — validation should have raised first")


class CannedConn:
    """Returns an empty result without raising — for happy-path calls."""
    def __init__(self):
        self.calls = []
    async def execute_read(self, lua, timeout=5.0):
        self.calls.append(lua); return []
    async def execute_write(self, lua, timeout=5.0):
        self.calls.append(lua); return []


def test_safe_enum_accepts_civ_tokens():
    assert _safe_enum("IMPROVEMENT_FARM", "improvement") == "IMPROVEMENT_FARM"
    assert _safe_enum("TECH_POTTERY") == "TECH_POTTERY"

@pytest.mark.parametrize("bad", ['X" .. evil() .. "', "A]B", "A B", "A.B", "", "A;B", "A\nB"])
def test_safe_enum_rejects_breakout(bad):
    with pytest.raises(ValueError):
        _safe_enum(bad, "field")

def test_one_of_accepts_and_upcases():
    assert _one_of("military", frozenset({"MILITARY"}), "alliance") == "MILITARY"

@pytest.mark.parametrize("bad", ['UNIT" --', "BOGUS", "", "OPEN BORDERS"])
def test_one_of_rejects_nonmembers(bad):
    with pytest.raises(ValueError):
        _one_of(bad, frozenset({"UNIT", "BUILDING"}), "item_type")

def test_lua_escape_neutralizes_and_preserves_display_names():
    assert _lua_escape("Ancient Walls") == "Ancient Walls"          # legit name unchanged
    out = _lua_escape('x" .. os.exit() .. "')
    assert '"' not in out.replace('\\"', "")                        # no UNescaped quote
    assert "\n" not in _lua_escape("a\nb")

def test_lua_get_city_rejects_nonnumeric():
    with pytest.raises((ValueError, TypeError)):
        _lua_get_city("1) print(1) --")

def test_lua_get_city_accepts_numeric():
    assert "% 65536" in _lua_get_city(65792)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_lua_injection_hardening.py -q`
Expected: FAIL — `_safe_enum`/`_one_of`/`_lua_escape` not defined; `_lua_get_city("…")` currently returns a string (no raise).

- [ ] **Step 3: Implement the helpers**

In `src/civ_mcp/lua/_helpers.py`, add `import re` at the top if absent, then add:

```python
_ENUM_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _safe_enum(value: str, field: str = "value") -> str:
    """Validate an LLM-supplied Civ type token before it is spliced into a bare
    Lua string-literal or ["..."] index. Admits only [A-Za-z0-9_]+, so the value
    can contain no quote/backslash/newline/bracket and cannot break out. Raises
    ValueError otherwise. Use for large GameInfo-table params."""
    if not isinstance(value, str) or not _ENUM_RE.match(value):
        raise ValueError(f"invalid {field}: {value!r}")
    return value


def _one_of(value: str, allowed: "frozenset[str]", field: str = "value") -> str:
    """Closed-domain allowlist for small, stable enums. Upper-cases and checks
    membership — rejects both Lua-breakout payloads and safe-shaped-but-invalid
    values before they reach a live game API. Raises ValueError otherwise."""
    if not isinstance(value, str):
        raise ValueError(f"invalid {field}: {value!r}")
    up = value.upper()
    if up not in allowed:
        raise ValueError(f"invalid {field}: {value!r} (allowed: {sorted(allowed)})")
    return up


def _lua_escape(value: str) -> str:
    """Escape an LLM-supplied free-text value for interpolation INSIDE an existing
    Lua "..." literal (adds no surrounding quotes). For item_name, which carries
    display names with spaces/mixed case. A crafted value is neutralized into a
    harmless Lua string that matches no display name."""
    if not isinstance(value, str):
        raise ValueError(f"item_name must be a string, got {value!r}")
    return (
        value.replace("\\", "\\\\").replace('"', '\\"')
        .replace("\n", "\\n").replace("\r", "").replace("\0", "")
    )
```

Then harden `_lua_get_city` — add `city_id = int(city_id)` as the first line of its body (before the f-string), so the `{city_id}` interpolation can only ever hold an int:

```python
def _lua_get_city(city_id: int) -> str:
    city_id = int(city_id)
    return (
        # ...existing f-string body unchanged...
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_lua_injection_hardening.py -q`
Expected: PASS

- [ ] **Step 5: Run full suite (city_id hardening is a shared helper)**

Run: `uv run pytest tests/ -q`
Expected: PASS — every current caller of `_lua_get_city` already passes an int-valued city_id, so the internal `int()` is a no-op on legit input.

- [ ] **Step 6: Commit**

```bash
git add src/civ_mcp/lua/_helpers.py tests/test_lua_injection_hardening.py
git commit -m "feat(arena): add _safe_enum/_one_of/_lua_escape validators + int-harden _lua_get_city"
```

---

### Task 2: Cities / production methods

Harden every untrusted arg on `set_city_production`, `purchase_item`, `list_city_production`, `set_city_focus` at their `GameState` entries (`game_state.py`). Sink lines: see inventory Class S rows for `item_type`/`item_name`/`yield_type`/`focus` and Class N rows for these tools' `city_id`/`target_x`/`target_y`.

**Files:**
- Modify: `src/civ_mcp/game_state.py` (the four methods)
- Test: `tests/test_lua_injection_hardening.py` (append)

**Interfaces:**
- Consumes: `_safe_enum`, `_one_of`, `_lua_escape` from `civ_mcp.lua._helpers` (Task 1); `NoExecConn` (Task 1). `int()` builtin for coords/city_id.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_lua_injection_hardening.py`:

```python
from civ_mcp.game_state import GameState

@pytest.mark.asyncio
@pytest.mark.parametrize("method,args,kwargs", [
    ("set_city_production", (), {"city_id": 1, "item_name": 'x" .. e() .. "', "item_type": "UNIT"}),
    ("set_city_production", (), {"city_id": '1) e() --', "item_name": "Scout", "item_type": "UNIT"}),
    ("set_city_production", (), {"city_id": 1, "item_name": "Scout", "item_type": 'UNIT" --'}),
    ("set_city_production", (), {"city_id": 1, "item_name": "Scout", "item_type": "UNIT",
                                 "target_x": '9)--', "target_y": 3}),
    ("purchase_item",       (), {"city_id": 1, "item_type": 'UNIT"--', "item_name": "Scout"}),
    ("purchase_item",       (), {"city_id": 1, "item_type": "UNIT", "item_name": "Scout",
                                 "yield_type": 'YIELD_GOLD" --'}),
    ("set_city_focus",      (), {"city_id": 1, "focus": 'FOOD" .. e() .. "'}),
    ("set_city_focus",      (), {"city_id": '1)--', "focus": "FOOD"}),
    ("list_city_production",(), {"city_id": '1) print(1) --'}),
])
async def test_cities_methods_reject_injection(method, args, kwargs):
    gs = GameState(NoExecConn())
    with pytest.raises((ValueError, TypeError)):
        await getattr(gs, method)(*args, **kwargs)
```

(Match the real keyword names/order of each method's signature in `game_state.py`; adjust the `args`/`kwargs` split to the actual signature. If a method takes `item_type` positionally, pass positionally — the point is the crafted value reaches the method and is rejected.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest "tests/test_lua_injection_hardening.py::test_cities_methods_reject_injection" -q`
Expected: FAIL — crafted values currently reach `conn.execute_*` (→ `AssertionError`, not the expected `ValueError`/`TypeError`).

- [ ] **Step 3: Add validation at each method entry** in `src/civ_mcp/game_state.py`.

Add near the other module constants (top of file):

```python
_PURCHASE_ITEM_TYPES = frozenset({"UNIT", "BUILDING", "DISTRICT", "PROJECT"})
_PURCHASE_YIELDS = frozenset({"YIELD_GOLD", "YIELD_FAITH"})
```

Import the helpers at the top: `from civ_mcp.lua._helpers import _safe_enum, _one_of, _lua_escape` (add to the existing `_helpers` import if one exists).

At the very top of each method body (after the docstring, before building Lua), insert:

- `set_city_production(self, city_id, item_name, item_type=..., target_x=None, target_y=None, ...)`:
  ```python
  city_id = int(city_id)
  item_name = _lua_escape(item_name)
  item_type = _one_of(item_type, _PURCHASE_ITEM_TYPES, "item_type")
  if target_x is not None:
      target_x = int(target_x)
  if target_y is not None:
      target_y = int(target_y)
  ```
- `purchase_item(self, city_id, item_type, item_name, yield_type="YIELD_GOLD", ...)`:
  ```python
  city_id = int(city_id)
  item_type = _one_of(item_type, _PURCHASE_ITEM_TYPES, "item_type")
  item_name = _lua_escape(item_name)
  yield_type = _one_of(yield_type, _PURCHASE_YIELDS, "yield_type")
  ```
- `list_city_production(self, city_id)`: `city_id = int(city_id)`
- `set_city_focus(self, city_id, focus)`:
  ```python
  city_id = int(city_id)
  focus = _safe_enum(focus, "focus")
  ```
  (Validate the raw `focus` before the existing `.upper()`/`YIELD_` transform; `_safe_enum` admits `FOOD`/`food` and the transform still produces `YIELD_FOOD`.)

`city_id` is also covered inside `_lua_get_city` (Task 1); the explicit entry cast additionally guards the direct `build_verify_production` interpolation (`lua/cities.py:546`) which does NOT route through the helper.

- [ ] **Step 4: Run the new tests, then the full suite**

Run: `uv run pytest "tests/test_lua_injection_hardening.py::test_cities_methods_reject_injection" -q` → PASS
Run: `uv run pytest tests/ -q` → PASS. If any existing cities/production test fed a bad `item_type`/`focus`/`yield_type` and asserted an `ERR:…` bail, update that assertion to `with pytest.raises(ValueError):` (expected migration; the suite names them).

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/game_state.py tests/test_lua_injection_hardening.py
git commit -m "fix(arena): validate cities/production args at GameState entry (finding-fix pass)"
```

---

### Task 3: Governance methods

Harden `appoint_governor`, `assign_governor`, `promote_governor`, `promote_unit`, `change_government`, `set_policies`. Note `promote_governor` re-interpolates `governor_type`/`promotion_type` in an inline verify query (`game_state.py:1023-1030`) — the single entry validation covers it.

**Files:**
- Modify: `src/civ_mcp/game_state.py` (the six methods)
- Test: `tests/test_lua_injection_hardening.py` (append)

**Interfaces:**
- Consumes: `_safe_enum` (Task 1). `set_policies` receives `assignments: dict[int, str]`.

- [ ] **Step 1: Write the failing tests** — append:

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("method,kwargs", [
    ("appoint_governor",  {"governor_type": 'GOVERNOR_X" --'}),
    ("assign_governor",   {"governor_type": 'GOVERNOR_X" --', "city_id": 1}),
    ("assign_governor",   {"governor_type": "GOVERNOR_LIANG", "city_id": '1)--'}),
    ("promote_governor",  {"governor_type": "GOVERNOR_LIANG", "promotion_type": 'X" --'}),
    ("promote_unit",      {"unit_id": 1, "promotion_type": 'PROMOTION_X" --'}),
    ("change_government",  {"government_type": 'GOVERNMENT_X" --'}),
])
async def test_governance_methods_reject_injection(method, kwargs):
    gs = GameState(NoExecConn())
    with pytest.raises((ValueError, TypeError)):
        await getattr(gs, method)(**kwargs)

@pytest.mark.asyncio
async def test_set_policies_rejects_injection():
    gs = GameState(NoExecConn())
    with pytest.raises((ValueError, TypeError)):
        await gs.set_policies({0: 'POLICY_X" .. e() .. "'})
```

(Match each method's real signature/param names.)

- [ ] **Step 2: Run to verify they fail** — `uv run pytest "tests/test_lua_injection_hardening.py::test_governance_methods_reject_injection" "tests/test_lua_injection_hardening.py::test_set_policies_rejects_injection" -q` → FAIL.

- [ ] **Step 3: Add validation at each method entry** (`game_state.py`):

- `appoint_governor`: `governor_type = _safe_enum(governor_type, "governor_type")`
- `assign_governor`: `governor_type = _safe_enum(governor_type, "governor_type"); city_id = int(city_id)`
- `promote_governor`: `governor_type = _safe_enum(governor_type, "governor_type"); promotion_type = _safe_enum(promotion_type, "promotion_type")`
- `promote_unit`: `promotion_type = _safe_enum(promotion_type, "promotion_type")` (unit_id already coerced upstream)
- `change_government`: `government_type = _safe_enum(government_type, "government_type")`
- `set_policies(self, assignments)`: replace the raw `assignments` with a sanitized copy as the first statement, and use it everywhere the method currently uses `assignments`:
  ```python
  assignments = {
      int(slot): (pol if str(pol).upper() == "NONE" else _safe_enum(pol, "policy"))
      for slot, pol in assignments.items()
  }
  ```
  (This feeds both `build_set_policies(assignments)` and the post-verify comparison loop — matching the spec's set_policies enforcement.)

- [ ] **Step 4: Run new tests + full suite** — both PASS; migrate any existing bad-enum bail assertions to `pytest.raises(ValueError)`.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/game_state.py tests/test_lua_injection_hardening.py
git commit -m "fix(arena): validate governance args at GameState entry; sanitize set_policies assignments"
```

---

### Task 4: Religion methods

Harden `choose_pantheon` (`belief_type`) and `found_religion` (`religion_name`→`religion_type`, `follower_belief`, `founder_belief`).

**Files:**
- Modify: `src/civ_mcp/game_state.py` (two methods)
- Test: `tests/test_lua_injection_hardening.py` (append)

- [ ] **Step 1: Write the failing tests** — append:

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("method,kwargs", [
    ("choose_pantheon", {"belief_type": 'BELIEF_X" --'}),
    ("found_religion",  {"religion_name": 'RELIGION_X" --', "follower_belief": "BELIEF_A", "founder_belief": "BELIEF_B"}),
    ("found_religion",  {"religion_name": "RELIGION_BUDDHISM", "follower_belief": 'X"--', "founder_belief": "BELIEF_B"}),
    ("found_religion",  {"religion_name": "RELIGION_BUDDHISM", "follower_belief": "BELIEF_A", "founder_belief": 'X"--'}),
])
async def test_religion_methods_reject_injection(method, kwargs):
    gs = GameState(NoExecConn())
    with pytest.raises((ValueError, TypeError)):
        await getattr(gs, method)(**kwargs)
```

- [ ] **Step 2: Run to verify they fail** → FAIL.

- [ ] **Step 3: Add validation at each method entry** (`game_state.py`):

- `choose_pantheon`: `belief_type = _safe_enum(belief_type, "belief_type")`
- `found_religion(self, religion_name, follower_belief, founder_belief)` (the param is named `religion_name` at the registry/GameState boundary):
  ```python
  religion_name = _safe_enum(religion_name, "religion")
  follower_belief = _safe_enum(follower_belief, "follower_belief")
  founder_belief = _safe_enum(founder_belief, "founder_belief")
  ```

- [ ] **Step 4: Run new tests + full suite** → PASS; migrate any bad-enum bail assertions.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/game_state.py tests/test_lua_injection_hardening.py
git commit -m "fix(arena): validate religion args at GameState entry"
```

---

### Task 5: Diplomacy & trade methods

Harden `send_diplomatic_action`, `respond_to_diplomacy`, `form_alliance`, `propose_trade`, `test_trade`, `propose_peace`, `get_trade_options`, `respond_to_trade`, `send_envoy`. Closed allowlists for `response`/`alliance_type`; `int()` for the `other_player_id`/`city_state_player_id` family; per-token `_safe_enum` for trade resources in the builder; and a builder `.get()`-bail to close the `send_diplomatic_action` raw-action fallback.

**Files:**
- Modify: `src/civ_mcp/game_state.py` (the diplomacy/trade methods); `src/civ_mcp/lua/diplomacy.py` (`_lua_deal_item` resource token; `build_send_diplo_action` fallback)
- Test: `tests/test_lua_injection_hardening.py` (append)

- [ ] **Step 1: Write the failing tests** — append:

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("method,kwargs", [
    ("send_diplomatic_action", {"other_player_id": 1, "action": 'DECLARE_FRIENDSHIP" --'}),
    ("send_diplomatic_action", {"other_player_id": '1)--', "action": "DECLARE_FRIENDSHIP"}),
    ("respond_to_diplomacy",   {"other_player_id": 1, "response": 'POSITIVE" --'}),
    ("form_alliance",          {"other_player_id": 1, "alliance_type": 'MILITARY" --'}),
    ("form_alliance",          {"other_player_id": 1, "alliance_type": "BOGUS"}),
    ("propose_peace",          {"other_player_id": '1) e() --'}),
    ("get_trade_options",      {"other_player_id": '1)--'}),
    ("respond_to_trade",       {"other_player_id": '1)--', "accept": True}),
    ("send_envoy",             {"city_state_player_id": '1) print(1) --'}),
])
async def test_diplomacy_methods_reject_injection(method, kwargs):
    gs = GameState(NoExecConn())
    with pytest.raises((ValueError, TypeError)):
        await getattr(gs, method)(**kwargs)

@pytest.mark.asyncio
async def test_propose_trade_rejects_resource_injection():
    gs = GameState(NoExecConn())
    with pytest.raises((ValueError, TypeError)):
        await gs.propose_trade(other_player_id=1, mode="test",
                               offer_resources='RESOURCE_IRON,X" .. e() .. "')

def test_send_diplo_action_unknown_does_not_fall_back_to_raw():
    # An unknown (but charset-safe) action must NOT be spliced into RequestSession.
    from civ_mcp.lua.diplomacy import build_send_diplo_action
    lua = build_send_diplo_action(1, "TOTALLY_UNKNOWN_ACTION")
    assert 'RequestSession(me, target, "TOTALLY_UNKNOWN_ACTION")' not in lua
```

(Match each method's real signature. `propose_trade`'s resource params may be named `offer_resources`/`request_resources` and its trade path may need a `mode`; adjust to the real signature.)

- [ ] **Step 2: Run to verify they fail** → FAIL.

- [ ] **Step 3: Add validation.**

Module constants near the top of `game_state.py`:
```python
_ALLIANCE_TYPES = frozenset({"MILITARY", "RESEARCH", "CULTURAL", "ECONOMIC", "RELIGIOUS"})
_DIPLO_RESPONSES = frozenset({"POSITIVE", "NEGATIVE", "EXIT"})
```

At each method entry (`game_state.py`):
- `send_diplomatic_action(self, other_player_id, action)`: `other_player_id = int(other_player_id); action = _safe_enum(action, "action")` (breakout guard; the garbage-fallback is closed in the builder below).
- `respond_to_diplomacy` (`diplomacy_respond`): `other_player_id = int(other_player_id); response = _one_of(response, _DIPLO_RESPONSES, "response")`
- `form_alliance(self, other_player_id, alliance_type)`: `other_player_id = int(other_player_id); alliance_type = _one_of(alliance_type, _ALLIANCE_TYPES, "alliance_type")`
- `propose_trade`, `test_trade`, `propose_peace`, `get_deal_options` (get_trade_options), `respond_to_deal` (respond_to_trade): `other_player_id = int(other_player_id)` at entry.
- `send_envoy`: `city_state_player_id = int(city_state_player_id)` at entry.

In `src/civ_mcp/lua/diplomacy.py`:
- `_lua_deal_item` RESOURCE branch (`~:830`): validate the token before interpolation — `res_name = _safe_enum(res_name, "resource")` (import `_safe_enum` from `._helpers`). This is the one builder-level string exception (tokens only exist post-split).
- `build_send_diplo_action` (`~:446`): change the raw fallback so an unknown action cannot reach `RequestSession`:
  ```python
  session_str = session_string_map.get(action_name)
  if session_str is None:
      return _bail(f"ERR:UNKNOWN_ACTION|{_safe_enum(action_name, 'action')}")
  ```
  (Place the bail before the `RequestSession` interpolation; `_safe_enum` in the echo keeps even the error message injection-proof. Keep the existing war-action handling.)

- [ ] **Step 4: Run new tests + full suite** → PASS; migrate any existing bad-action/alliance bail assertions to `pytest.raises(ValueError)`.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/game_state.py src/civ_mcp/lua/diplomacy.py tests/test_lua_injection_hardening.py
git commit -m "fix(arena): validate diplomacy/trade args at GameState entry; close resource + action fallbacks"
```

---

### Task 6: Research / civics / map methods

Harden `set_research` (`tech`), `set_civic` (`civic_name`), `improve_tile` (`improvement_name`), `get_district_advisor` (`district_type`, `city_id`), `get_wonder_advisor` (`wonder_name`, `city_id`), `get_purchasable_tiles`/`purchase_tile` (`city_id`). `set_research`/`set_civic` re-interpolate their enum in an inline `game_state.py` verify query (`:730,754`) — the single entry validation covers it.

**Files:**
- Modify: `src/civ_mcp/game_state.py` (these methods)
- Test: `tests/test_lua_injection_hardening.py` (append)

- [ ] **Step 1: Write the failing tests** — append:

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("method,kwargs", [
    ("set_research",         {"tech": 'TECH_X" .. e() .. "'}),
    ("set_civic",            {"civic_name": 'CIVIC_X" --'}),
    ("improve_tile",         {"unit_index": 1, "improvement_name": 'IMPROVEMENT_X" --'}),
    ("get_district_advisor", {"city_id": 1, "district_type": 'DISTRICT_X" --'}),
    ("get_district_advisor", {"city_id": '1)--', "district_type": "DISTRICT_CAMPUS"}),
    ("get_wonder_advisor",   {"city_id": 1, "wonder_name": 'BUILDING_X" --'}),
    ("get_purchasable_tiles",{"city_id": '1) print(1) --'}),
])
async def test_research_map_methods_reject_injection(method, kwargs):
    gs = GameState(NoExecConn())
    with pytest.raises((ValueError, TypeError)):
        await getattr(gs, method)(**kwargs)
```

(Match real signatures; `improve_tile`'s unit_index is already coerced — the crafted value here is `improvement_name`.)

- [ ] **Step 2: Run to verify they fail** → FAIL.

- [ ] **Step 3: Add validation at each method entry** (`game_state.py`):

- `set_research`: `tech = _safe_enum(tech, "tech")`
- `set_civic`: `civic_name = _safe_enum(civic_name, "civic")`
- `improve_tile`: `improvement_name = _safe_enum(improvement_name, "improvement")` (unit_index unchanged)
- `get_district_advisor`: `city_id = int(city_id); district_type = _safe_enum(district_type, "district")`
- `get_wonder_advisor`: `city_id = int(city_id); wonder_name = _safe_enum(wonder_name, "wonder")`
- `get_purchasable_tiles`: `city_id = int(city_id)`
- `purchase_tile`: `city_id = int(city_id)` (its x/y already coerced at the registry)

- [ ] **Step 4: Run new tests + full suite** → PASS; migrate any bad-enum bail assertions.

- [ ] **Step 5: Commit**

```bash
git add src/civ_mcp/game_state.py tests/test_lua_injection_hardening.py
git commit -m "fix(arena): validate research/civic/map args at GameState entry"
```

---

### Task 7: Mark the injection classes CLOSED in the live-probe checklist

**Files:**
- Modify: `docs/superpowers/plans/2026-07-07-arena-slice4-live-probes.md`

- [ ] **Step 1: Update the residual-surface bullet**

In the `## Review-fix probes (2026-07-08)` section, replace the "Residual id-arg coercion" bullet's *"Residual un-coerced LLM→Lua surface…"* subsection with a CLOSED status:

```markdown
- [x] **LLM→Lua injection surface CLOSED (2026-07-08 hardening pass).** All
      untrusted args are validated at their GameState-method entry:
      `_safe_enum` (charset whitelist) for GameInfo-table enums, `_one_of`
      (closed allowlist) for small live-action enums
      (send_diplomatic_action/response/alliance_type/item_type/yield_type),
      `_lua_escape` for the one free-text param `item_name`, and `int()` for the
      `city_id`/`other_player_id`/`city_state_player_id` family +
      `set_city_production` coords. Helpers in `src/civ_mcp/lua/_helpers.py`;
      spec `docs/superpowers/specs/2026-07-08-arena-lua-injection-hardening-design.md`;
      inventory `.superpowers/sdd/lua-injection-inventory.md`. Out of scope
      (documented non-goals): the human-facing FastMCP `run_lua` (server.py:2864),
      the unwired `build_congress_vote`, and the dead `_lua_deal_item` CITY branch.
```

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/plans/2026-07-07-arena-slice4-live-probes.md
git commit -m "docs(arena): mark LLM->Lua injection surface CLOSED in live-probe checklist"
```

---

## Self-Review

- **Spec coverage:** primitives (T1); Class S enum params across cities (T2), governance (T3), religion (T4), diplomacy (T5), research/map (T6); `item_name` escape (T2); resource per-token + closed allowlists + action fallback (T5); `set_policies` sanitized dict (T3); Class N id-family + `set_city_production` coords (T2/T5/T6) + `_lua_get_city` hardening (T1); live-probe CLOSED note (T7). Non-goals (run_lua/server, congress-vote, CITY branch) are documented, not implemented — correct.
- **Placeholder scan:** every code step carries verbatim helper/validation/test code. The one bounded judgement — "match the real signature" and "migrate any existing bad-enum bail assertion" — is a real, suite-identified step, not a TODO.
- **Type consistency:** `_safe_enum(value, field)->str`, `_one_of(value, frozenset, field)->str`, `_lua_escape(value)->str` used identically across T2–T6; `NoExecConn` defined in T1 and reused; frozensets (`_PURCHASE_ITEM_TYPES`, `_PURCHASE_YIELDS`, `_ALLIANCE_TYPES`, `_DIPLO_RESPONSES`) named consistently.

## Execution Handoff

Plan complete. Recommended execution: **Subagent-Driven Development** (fresh implementer per task + spec/quality review between tasks), matching how the rest of this branch was built. Task 1 (primitives) first — every later task consumes its helpers; Tasks 2–6 are independent method-groups and may run in any order; Task 7 (docs) last.
