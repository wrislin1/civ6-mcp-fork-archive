# Arena Diplomacy Tools (Slice 2) Implementation Plan

## Status — 2026-07-06

- **Slice 2 — implemented + hardened.** Commits: `4881a96`, `4f823b3`, `cc311bb`, `5411ff2`.
- ✅ Host verification — `/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_registry.py tests/arena/test_analyze.py tests/arena/test_experiment.py -q` passed with `165 passed`; `/home/riz/.local/bin/uv run --extra test pytest tests -q` passed with `442 passed`; `git diff --check` was clean.
- ✅ Review hardening — registry adapters reject malformed trade booleans, reject empty/bad-mode deals, and default missing `propose_trade(mode)` to safe preview/test.
- ⚠️ Live diplomacy-event validation — not observed; read-only status found no active arena watcher, and the branch did not force a live game transition.
- **Slice 3 — REMAINING.** Cross-turn memory / standing plan remains follow-on work.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let full-tier arena puppets use the existing Civ VI diplomacy, trade, peace, and alliance MCP capabilities through the in-process arena tool registry.

**Architecture:** Slice 2 is an adapter-layer change, not new diplomacy engine work. `src/civ_mcp/arena/registry.py` gets public arena tools that mirror the MCP server names and delegate to existing `GameState` methods; `src/civ_mcp/arena/vocab.py` mirrors new action verbs for offline analysis; the condensed playbook teaches when to use the new tools. No new arena tool tier is introduced in this slice: existing `full` puppets gain the tools automatically through `tuple(TOOL_REGISTRY)`, while `minimal` and `standard` remain unchanged.

**Tech Stack:** Python 3.12, `pytest`, `uv`, existing `civ_mcp.game_state.GameState`, existing `civ_mcp.narrate` diplomacy/trade narrators, existing `civ_mcp.lua` dataclasses.

---

## Scope Boundary

Implement exactly these public arena tool names:

- Read tools: `get_pending_diplomacy`, `get_pending_trades`, `get_trade_options`
- Action tools: `respond_to_diplomacy`, `respond_to_trade`, `propose_trade`, `propose_peace`, `send_diplomatic_action`, `form_alliance`

Do not register internal names such as `diplomacy_respond`, `get_deal_options`, `get_pending_deals`, or `respond_to_deal`.

Do not add a new diplomacy-capable mid tier in this slice. `full` gets the new tools through the registry order. `minimal` and `standard` must not gain them.

Do not change the live `GameState` diplomacy/trade Lua handlers unless a test proves the arena adapter cannot call them correctly. The MCP server already exposes working public wrappers; this slice mirrors those semantics inside `arena.registry`.

## File Structure

- `src/civ_mcp/arena/registry.py`
  - Add `_bool_param`.
  - Add read wrappers for pending diplomacy, pending trade deals, and trade options.
  - Add shared trade-argument construction helpers.
  - Add action wrappers for diplomacy responses, trade responses/proposals, peace, proactive diplomatic actions, and alliances.
  - Register all new tools in `TOOL_REGISTRY`; only action tools get `verb=`.
- `src/civ_mcp/arena/vocab.py`
  - Add the new action-tool verbs to `LOCAL_TOOL_VERBS`, exactly mirroring registry `verb=` values.
- `src/civ_mcp/arena/playbook.md`
  - Add concise doctrine for reactive diplomacy, incoming trade deals, proactive delegations/friendships/alliances, surplus-resource trades, peace offers, and war-declaration timing.
- `tests/arena/test_registry.py`
  - Add registration, tier, schema, dispatch, and trade argument tests.
- `tests/arena/test_analyze.py`
  - Add explicit sanity checks for the new action verbs; the existing exact mirror test remains the main coupling gate.
- `tests/arena/test_experiment.py`
  - Verify the current Slice 1 A/B YAML still keeps diplomacy tools treatment/full-only and playbook doctrine is present.

## Task 0: Branch Setup

**Files:**
- No source changes.

- [x] **Step 1: Create an isolated implementation worktree**

Run:

```bash
git status --short --branch
git worktree add /home/riz/.config/superpowers/worktrees/civ6-mcp/arena-diplomacy-tools-slice2 -b arena-diplomacy-tools-slice2
cd /home/riz/.config/superpowers/worktrees/civ6-mcp/arena-diplomacy-tools-slice2
```

Expected:

```text
Preparing worktree (new branch 'arena-diplomacy-tools-slice2')
```

The second line should report the current `main` commit and starts with `HEAD is now at`.

- [x] **Step 2: Verify the worktree starts clean except known local metadata**

Run:

```bash
git status --short --branch
```

Expected: branch is `arena-diplomacy-tools-slice2`; no tracked-file changes.

---

## Task 1: Registry Tool Tests and Implementation

**Files:**
- Modify: `tests/arena/test_registry.py`
- Modify: `src/civ_mcp/arena/registry.py`

- [x] **Step 1: Add tests for canonical tool names, tiers, schemas, and dispatch**

Append this block after `test_dispatch_rejects_out_of_allowed` in `tests/arena/test_registry.py`:

```python
DIPLOMACY_TOOL_NAMES = {
    "get_pending_diplomacy",
    "respond_to_diplomacy",
    "get_pending_trades",
    "respond_to_trade",
    "get_trade_options",
    "propose_trade",
    "propose_peace",
    "send_diplomatic_action",
    "form_alliance",
}


def test_diplomacy_tools_registered_full_only():
    assert DIPLOMACY_TOOL_NAMES <= set(TOOL_REGISTRY)
    assert DIPLOMACY_TOOL_NAMES <= set(resolve_tools("full"))
    assert DIPLOMACY_TOOL_NAMES.isdisjoint(set(resolve_tools("minimal")))
    assert DIPLOMACY_TOOL_NAMES.isdisjoint(set(resolve_tools("standard")))

    for internal_name in (
        "diplomacy_respond",
        "get_deal_options",
        "get_pending_deals",
        "respond_to_deal",
    ):
        assert internal_name not in TOOL_REGISTRY


def test_diplomacy_tool_schema_shape():
    by_name = {tool["function"]["name"]: tool["function"] for tool in openai_tools(sorted(DIPLOMACY_TOOL_NAMES))}

    assert by_name["respond_to_trade"]["parameters"]["properties"]["accept"]["type"] == "boolean"
    assert set(by_name["respond_to_trade"]["parameters"]["required"]) == {"other_player_id", "accept"}
    assert set(by_name["get_trade_options"]["parameters"]["required"]) == {"other_player_id"}
    assert set(by_name["respond_to_diplomacy"]["parameters"]["required"]) == {"other_player_id", "response"}
    assert by_name["propose_trade"]["parameters"]["properties"]["mode"]["type"] == "string"
    assert by_name["form_alliance"]["parameters"]["properties"]["alliance_type"]["type"] == "string"

    action_desc = by_name["send_diplomatic_action"]["parameters"]["properties"]["action"]["description"]
    for token in (
        "DIPLOMATIC_DELEGATION",
        "DECLARE_FRIENDSHIP",
        "DENOUNCE",
        "RESIDENT_EMBASSY",
        "OPEN_BORDERS",
        "DECLARE_SURPRISE_WAR",
        "DECLARE_FORMAL_WAR",
        "DECLARE_HOLY_WAR",
        "DECLARE_LIBERATION_WAR",
        "DECLARE_RECONQUEST_WAR",
        "DECLARE_PROTECTORATE_WAR",
        "DECLARE_COLONIAL_WAR",
        "DECLARE_TERRITORIAL_WAR",
    ):
        assert token in action_desc


@pytest.mark.asyncio
async def test_dispatch_pending_diplomacy_and_trades_are_narrated():
    class FakeGS:
        async def get_diplomacy_sessions(self):
            from civ_mcp import lua as lq

            return [
                lq.DiplomacySession(
                    session_id=12,
                    other_player_id=3,
                    other_civ_name="Rome",
                    other_leader_name="Trajan",
                    choices=[],
                    dialogue_text="Welcome.",
                    buttons="POSITIVE;NEGATIVE",
                )
            ]

        async def get_pending_deals(self):
            from civ_mcp import lua as lq

            return [
                lq.PendingDeal(
                    other_player_id=4,
                    other_player_name="Egypt",
                    other_leader_name="Cleopatra",
                    items_from_them=[
                        lq.DealItem(
                            from_player_id=4,
                            from_player_name="Egypt",
                            item_type="GOLD",
                            name="Gold",
                            amount=50,
                            duration=0,
                            is_from_us=False,
                        )
                    ],
                    items_from_us=[],
                )
            ]

    diplo = await dispatch(FakeGS(), "get_pending_diplomacy", {})
    deals = await dispatch(FakeGS(), "get_pending_trades", {})

    assert "Rome" in diplo and "Respond with: POSITIVE" in diplo
    assert "Egypt" in deals and "respond_to_trade(other_player_id=4" in deals


@pytest.mark.asyncio
async def test_dispatch_trade_options_are_narrated():
    class FakeGS:
        async def get_deal_options(self, other_player_id):
            from civ_mcp import lua as lq

            assert other_player_id == 3
            return lq.DealOptions(
                other_player_id=3,
                other_civ_name="Rome",
                our_gold=120,
                our_gpt=8,
                their_gold=40,
                their_gpt=3,
                our_luxuries=["Silk x2"],
                alliance_eligible=True,
            )

    text = await dispatch(FakeGS(), "get_trade_options", {"other_player_id": 3})

    assert "Trade options with Rome (player 3)" in text
    assert "Silk x2" in text
    assert "Alliance: eligible" in text


@pytest.mark.asyncio
async def test_dispatch_reactive_action_tools_call_gamestate_methods():
    calls = []

    class FakeGS:
        async def diplomacy_respond(self, other_player_id, response):
            calls.append(("diplomacy_respond", other_player_id, response))
            return "OK:RESPONDED|POSITIVE|SESSION_CLOSED"

        async def respond_to_deal(self, other_player_id, accept):
            calls.append(("respond_to_deal", other_player_id, accept))
            return "OK:DEAL_ACCEPTED|Rome"

    diplo = await dispatch(
        FakeGS(),
        "respond_to_diplomacy",
        {"other_player_id": 3, "response": "POSITIVE"},
    )
    trade = await dispatch(
        FakeGS(),
        "respond_to_trade",
        {"other_player_id": 4, "accept": True},
    )

    assert diplo == "OK:RESPONDED|POSITIVE|SESSION_CLOSED"
    assert trade == "OK:DEAL_ACCEPTED|Rome"
    assert calls == [
        ("diplomacy_respond", 3, "POSITIVE"),
        ("respond_to_deal", 4, True),
    ]


@pytest.mark.asyncio
async def test_dispatch_proactive_diplomacy_tools_call_gamestate_methods():
    calls = []

    class FakeGS:
        async def propose_peace(self, other_player_id):
            calls.append(("propose_peace", other_player_id))
            return "ACCEPTED|Peace established with Rome"

        async def send_diplomatic_action(self, other_player_id, action):
            calls.append(("send_diplomatic_action", other_player_id, action))
            return "OK:DIPLOMATIC_DELEGATION|Rome"

        async def form_alliance(self, other_player_id, alliance_type):
            calls.append(("form_alliance", other_player_id, alliance_type))
            return "OK:ALLIANCE_FORMED|Rome|RESEARCH"

    peace = await dispatch(FakeGS(), "propose_peace", {"other_player_id": 3})
    delegation = await dispatch(
        FakeGS(),
        "send_diplomatic_action",
        {"other_player_id": 3, "action": "diplomatic_delegation"},
    )
    alliance = await dispatch(
        FakeGS(),
        "form_alliance",
        {"other_player_id": 3, "alliance_type": "research"},
    )

    assert peace.startswith("ACCEPTED|")
    assert delegation.startswith("OK:DIPLOMATIC_DELEGATION")
    assert alliance.startswith("OK:ALLIANCE_FORMED")
    assert calls == [
        ("propose_peace", 3),
        ("send_diplomatic_action", 3, "DIPLOMATIC_DELEGATION"),
        ("form_alliance", 3, "RESEARCH"),
    ]


@pytest.mark.asyncio
async def test_dispatch_propose_trade_builds_items_for_send_and_test_modes():
    calls = []

    class FakeGS:
        async def test_trade(self, other_player_id, offer_items, request_items):
            calls.append(("test_trade", other_player_id, offer_items, request_items))
            return "AI counter-offer: Rome will accept"

        async def propose_trade(self, other_player_id, offer_items, request_items):
            calls.append(("propose_trade", other_player_id, offer_items, request_items))
            return "OK:ACCEPTED|Trade accepted with Rome"

    test_text = await dispatch(
        FakeGS(),
        "propose_trade",
        {
            "other_player_id": 3,
            "offer_resources": "RESOURCE_SILK, RESOURCE_TEA",
            "request_gold_per_turn": 5,
            "request_open_borders": True,
            "mode": "test",
        },
    )
    send_text = await dispatch(
        FakeGS(),
        "propose_trade",
        {
            "other_player_id": 3,
            "offer_favor": 20,
            "request_gold": 80,
            "mode": "send",
        },
    )

    assert test_text.startswith("AI counter-offer")
    assert send_text.startswith("OK:ACCEPTED")
    assert calls[0] == (
        "test_trade",
        3,
        [
            {"type": "RESOURCE", "name": "RESOURCE_SILK", "amount": 1, "duration": 30},
            {"type": "RESOURCE", "name": "RESOURCE_TEA", "amount": 1, "duration": 30},
        ],
        [
            {"type": "GOLD", "amount": 5, "duration": 30},
            {"type": "AGREEMENT", "subtype": "OPEN_BORDERS"},
        ],
    )
    assert calls[1] == (
        "propose_trade",
        3,
        [{"type": "FAVOR", "amount": 20}],
        [{"type": "GOLD", "amount": 80, "duration": 0}],
    )


@pytest.mark.asyncio
async def test_dispatch_propose_trade_rejects_empty_or_bad_mode():
    class FakeGS:
        async def test_trade(self, other_player_id, offer_items, request_items):
            raise AssertionError("empty trade must not reach GameState")

        async def propose_trade(self, other_player_id, offer_items, request_items):
            raise AssertionError("bad mode must not reach GameState")

    empty = await dispatch(FakeGS(), "propose_trade", {"other_player_id": 3})
    bad_mode = await dispatch(
        FakeGS(),
        "propose_trade",
        {"other_player_id": 3, "offer_gold": 10, "mode": "preview"},
    )

    assert empty == "Error: must specify at least one offer or request item"
    assert bad_mode == 'Error: mode must be "test" or "send"'
```

- [x] **Step 2: Run tests to verify they fail**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_registry.py -q
```

Expected: FAIL. The first failure should report the new diplomacy tool names missing from `TOOL_REGISTRY`.

- [x] **Step 3: Add boolean parameter helper**

Insert this helper immediately after `_str_param`:

```python
def _bool_param(description: str) -> dict[str, str]:
    return {"type": "boolean", "description": description}
```

- [x] **Step 4: Add read-tool wrapper functions**

Insert these functions after `_builder_tasks_text`:

```python
async def _pending_diplomacy_text(gs: Any, args: dict[str, Any]) -> str:
    del args
    return _render(await gs.get_diplomacy_sessions(), nr.narrate_diplomacy_sessions)


async def _pending_trades_text(gs: Any, args: dict[str, Any]) -> str:
    del args
    return _render(await gs.get_pending_deals(), nr.narrate_pending_deals)


async def _trade_options_text(gs: Any, args: dict[str, Any]) -> str:
    return _render(
        await gs.get_deal_options(args["other_player_id"]),
        nr.narrate_deal_options,
    )
```

- [x] **Step 5: Add trade argument helpers**

Insert these helpers after `_coerce_policy_assignments`:

```python
def _positive_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(parsed, 0)


def _resource_items(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    return [
        {"type": "RESOURCE", "name": res, "amount": 1, "duration": 30}
        for res in (part.strip() for part in str(raw).split(","))
        if res
    ]


def _build_trade_items(args: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    offer_items: list[dict[str, Any]] = []
    request_items: list[dict[str, Any]] = []

    offer_gold = _positive_int(args.get("offer_gold", 0))
    if offer_gold:
        offer_items.append({"type": "GOLD", "amount": offer_gold, "duration": 0})

    offer_gpt = _positive_int(args.get("offer_gold_per_turn", 0))
    if offer_gpt:
        offer_items.append({"type": "GOLD", "amount": offer_gpt, "duration": 30})

    offer_items.extend(_resource_items(args.get("offer_resources", "")))

    offer_favor = _positive_int(args.get("offer_favor", 0))
    if offer_favor:
        offer_items.append({"type": "FAVOR", "amount": offer_favor})

    if args.get("offer_open_borders", False):
        offer_items.append({"type": "AGREEMENT", "subtype": "OPEN_BORDERS"})

    request_gold = _positive_int(args.get("request_gold", 0))
    if request_gold:
        request_items.append({"type": "GOLD", "amount": request_gold, "duration": 0})

    request_gpt = _positive_int(args.get("request_gold_per_turn", 0))
    if request_gpt:
        request_items.append({"type": "GOLD", "amount": request_gpt, "duration": 30})

    request_items.extend(_resource_items(args.get("request_resources", "")))

    request_favor = _positive_int(args.get("request_favor", 0))
    if request_favor:
        request_items.append({"type": "FAVOR", "amount": request_favor})

    if args.get("request_open_borders", False):
        request_items.append({"type": "AGREEMENT", "subtype": "OPEN_BORDERS"})

    if _positive_int(args.get("joint_war_target", 0)):
        offer_items.append({"type": "AGREEMENT", "subtype": "JOINT_WAR"})
        request_items.append({"type": "AGREEMENT", "subtype": "JOINT_WAR"})

    return offer_items, request_items


async def _propose_trade_text(gs: Any, args: dict[str, Any]) -> str:
    offer_items, request_items = _build_trade_items(args)
    if not offer_items and not request_items:
        return "Error: must specify at least one offer or request item"

    mode = str(args.get("mode", "send")).lower()
    if mode == "test":
        return await gs.test_trade(args["other_player_id"], offer_items, request_items)
    if mode == "send":
        return await gs.propose_trade(args["other_player_id"], offer_items, request_items)
    # Hardening over the MCP wrapper: typos must not accidentally commit a deal.
    return 'Error: mode must be "test" or "send"'
```

- [x] **Step 6: Register read tools**

Insert these entries in `TOOL_REGISTRY` immediately after the existing `get_diplomacy` entry:

```python
    "get_pending_diplomacy": _tool(
        "get_pending_diplomacy",
        "Check for pending diplomacy encounters that can block turn progression.",
        None,
        (),
        _pending_diplomacy_text,
    ),
    "get_pending_trades": _tool(
        "get_pending_trades",
        "Check for pending incoming trade deal offers.",
        None,
        (),
        _pending_trades_text,
    ),
    "get_trade_options": _tool(
        "get_trade_options",
        "See what both sides can trade with another civilization.",
        {"other_player_id": _int_param("Player ID from get_diplomacy.")},
        ("other_player_id",),
        _trade_options_text,
    ),
```

- [x] **Step 7: Register action tools**

Insert these entries in `TOOL_REGISTRY` after `set_city_focus`:

```python
    "respond_to_diplomacy": _tool(
        "respond_to_diplomacy",
        "Respond to a pending diplomacy encounter with POSITIVE or NEGATIVE.",
        {
            "other_player_id": _int_param("Player ID from get_pending_diplomacy."),
            "response": _str_param("POSITIVE or NEGATIVE."),
        },
        ("other_player_id", "response"),
        lambda gs, args: gs.diplomacy_respond(args["other_player_id"], args["response"]),
        verb="respond_to_diplomacy",
    ),
    "respond_to_trade": _tool(
        "respond_to_trade",
        "Accept or reject a pending incoming trade deal.",
        {
            "other_player_id": _int_param("Player ID from get_pending_trades."),
            "accept": _bool_param("True to accept; false to reject."),
        },
        ("other_player_id", "accept"),
        lambda gs, args: gs.respond_to_deal(args["other_player_id"], args["accept"]),
        verb="respond_to_trade",
    ),
    "propose_trade": _tool(
        "propose_trade",
        "Propose or preview a trade deal with another civilization.",
        {
            "other_player_id": _int_param("Player ID from get_diplomacy."),
            "offer_gold": _int_param("Lump-sum gold to offer."),
            "offer_gold_per_turn": _int_param("Gold per turn to offer for 30 turns."),
            "offer_resources": _str_param("Comma-separated resource types to offer, for example RESOURCE_SILK."),
            "offer_favor": _int_param("Diplomatic Favor to offer."),
            "offer_open_borders": _bool_param("Offer our open borders."),
            "request_gold": _int_param("Lump-sum gold to request."),
            "request_gold_per_turn": _int_param("Gold per turn to request for 30 turns."),
            "request_resources": _str_param("Comma-separated resource types to request."),
            "request_favor": _int_param("Diplomatic Favor to request."),
            "request_open_borders": _bool_param("Request their open borders."),
            "joint_war_target": _int_param("Third-party player ID for joint war; 0 for none."),
            "mode": _str_param('"test" previews AI counter-offer; "send" commits the deal.'),
        },
        ("other_player_id",),
        _propose_trade_text,
        verb="propose_trade",
    ),
    "propose_peace": _tool(
        "propose_peace",
        "Propose white peace to a civilization you are at war with.",
        {"other_player_id": _int_param("Player ID from get_diplomacy.")},
        ("other_player_id",),
        lambda gs, args: gs.propose_peace(args["other_player_id"]),
        verb="propose_peace",
    ),
    "send_diplomatic_action": _tool(
        "send_diplomatic_action",
        "Send a proactive diplomatic action such as delegation, friendship, embassy, denouncement, open borders, or war declaration.",
        {
            "other_player_id": _int_param("Player ID from get_diplomacy."),
            "action": _str_param(
                "One of: DIPLOMATIC_DELEGATION, DECLARE_FRIENDSHIP, DENOUNCE, "
                "RESIDENT_EMBASSY, OPEN_BORDERS, DECLARE_SURPRISE_WAR, "
                "DECLARE_FORMAL_WAR, DECLARE_HOLY_WAR, DECLARE_LIBERATION_WAR, "
                "DECLARE_RECONQUEST_WAR, DECLARE_PROTECTORATE_WAR, "
                "DECLARE_COLONIAL_WAR, DECLARE_TERRITORIAL_WAR. "
                "OPEN_BORDERS is routed through the trade API as mutual open borders."
            ),
        },
        ("other_player_id", "action"),
        lambda gs, args: gs.send_diplomatic_action(args["other_player_id"], args["action"].upper()),
        verb="send_diplomatic_action",
    ),
    "form_alliance": _tool(
        "form_alliance",
        "Form an alliance with another civilization after friendship and Diplomatic Service.",
        {
            "other_player_id": _int_param("Player ID from get_diplomacy."),
            "alliance_type": _str_param("MILITARY, RESEARCH, CULTURAL, ECONOMIC, or RELIGIOUS."),
        },
        ("other_player_id", "alliance_type"),
        lambda gs, args: gs.form_alliance(args["other_player_id"], args["alliance_type"].upper()),
        verb="form_alliance",
    ),
```

Keep the final `"full": tuple(TOOL_REGISTRY)` tier unchanged.

- [x] **Step 8: Run registry tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_registry.py -q
```

Expected: PASS for `tests/arena/test_registry.py`.

- [x] **Step 9: Commit**

```bash
git add src/civ_mcp/arena/registry.py tests/arena/test_registry.py
git commit -m "feat(arena): register diplomacy and trade tools"
```

---

## Task 2: Analysis Verb Mirror

**Files:**
- Modify: `src/civ_mcp/arena/vocab.py`
- Modify: `tests/arena/test_analyze.py`

- [x] **Step 1: Add explicit verb sanity checks**

Add these assertions inside `test_local_tool_verbs_mirror_registry_verbs_exactly`, after the two existing sanity assertions:

```python
    assert registry_verbs["respond_to_diplomacy"] == "respond_to_diplomacy"
    assert registry_verbs["respond_to_trade"] == "respond_to_trade"
    assert registry_verbs["propose_trade"] == "propose_trade"
    assert registry_verbs["propose_peace"] == "propose_peace"
    assert registry_verbs["send_diplomatic_action"] == "send_diplomatic_action"
    assert registry_verbs["form_alliance"] == "form_alliance"
```

- [x] **Step 2: Run analysis test to verify it fails**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_analyze.py::test_local_tool_verbs_mirror_registry_verbs_exactly -q
```

Expected: FAIL because `LOCAL_TOOL_VERBS` does not yet mirror the new registry verbs.

- [x] **Step 3: Update the vocab mirror**

Append these entries to `LOCAL_TOOL_VERBS` in `src/civ_mcp/arena/vocab.py`:

```python
    "respond_to_diplomacy": "respond_to_diplomacy",
    "respond_to_trade": "respond_to_trade",
    "propose_trade": "propose_trade",
    "propose_peace": "propose_peace",
    "send_diplomatic_action": "send_diplomatic_action",
    "form_alliance": "form_alliance",
```

- [x] **Step 4: Run analysis tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_analyze.py::test_local_tool_verbs_mirror_registry_verbs_exactly tests/arena/test_analyze.py::test_step_verb_covers_all_shared_vocab_entries -q
```

Expected: PASS for both selected tests.

- [x] **Step 5: Commit**

```bash
git add src/civ_mcp/arena/vocab.py tests/arena/test_analyze.py
git commit -m "feat(arena): track diplomacy actions in analysis vocab"
```

---

## Task 3: Playbook Doctrine for Diplomacy, Trades, and Peace

**Files:**
- Modify: `src/civ_mcp/arena/playbook.md`
- Modify: `tests/arena/test_experiment.py`

- [x] **Step 1: Add playbook test**

Append this test after `test_playbook_covers_promotions_and_expansion_doctrine`:

```python
def test_playbook_covers_diplomacy_trade_and_peace_doctrine():
    text = (REPO_ROOT / "src" / "civ_mcp" / "arena" / "playbook.md").read_text()

    assert "## Diplomacy, trades, and peace" in text
    assert "get_pending_diplomacy" in text
    assert "respond_to_diplomacy" in text
    assert "get_pending_trades" in text
    assert "respond_to_trade" in text
    assert "get_trade_options" in text
    assert "propose_trade" in text
    assert "propose_peace" in text
    assert "form_alliance" in text
    assert "send_diplomatic_action" in text
    assert "DIPLOMATIC_DELEGATION" in text
    assert "DECLARE_FRIENDSHIP" in text
    assert "RESIDENT_EMBASSY" in text
    assert "DECLARE_SURPRISE_WAR" in text
```

- [x] **Step 2: Add experiment tool-access test**

Add `resolve_tools` to the import block:

```python
from civ_mcp.arena.registry import resolve_tools
```

Then append this test after `test_loads_gemma_strategy_ab_slice1_artifact`:

```python
def test_slice1_treatment_full_tier_has_diplomacy_tools_and_control_does_not():
    cfg = load_experiment(SLICE1_GEMMA_STRATEGY_AB)
    by_player = {player.player_id: player for player in cfg.players}
    diplomacy_tools = {
        "get_pending_diplomacy",
        "respond_to_diplomacy",
        "get_pending_trades",
        "respond_to_trade",
        "get_trade_options",
        "propose_trade",
        "propose_peace",
        "send_diplomatic_action",
        "form_alliance",
    }

    for player_id in (1, 3, 5, 7):
        assert diplomacy_tools <= set(resolve_tools(by_player[player_id].options.tools))

    for player_id in (2, 4, 6):
        assert diplomacy_tools.isdisjoint(set(resolve_tools(by_player[player_id].options.tools)))
```

- [x] **Step 3: Run tests to verify they fail**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_experiment.py::test_playbook_covers_diplomacy_trade_and_peace_doctrine tests/arena/test_experiment.py::test_slice1_treatment_full_tier_has_diplomacy_tools_and_control_does_not -q
```

Expected: FAIL because the playbook header is not present yet. The tool-access test should pass after Task 1.

- [x] **Step 4: Add concise doctrine**

Append this section to `src/civ_mcp/arena/playbook.md`:

```markdown
## Diplomacy, trades, and peace
If a leader screen or deal blocks progress, inspect it first: use get_pending_diplomacy
for spoken encounters and respond_to_diplomacy(other_player_id, POSITIVE/NEGATIVE)
for 2-3 rounds until it closes. Use get_pending_trades for incoming deals, then
respond_to_trade only if the exchange helps you; reject bad deals quickly.

On first meeting, use send_diplomatic_action(action="DIPLOMATIC_DELEGATION") if
you can afford 25 gold. Friendly neighbors are worth converting with
send_diplomatic_action(action="DECLARE_FRIENDSHIP"), then alliances once
Diplomatic Service is available; alliances generate favor and reduce war risk.
Embassies use send_diplomatic_action(action="RESIDENT_EMBASSY") after Writing.
Use get_trade_options before propose_trade or form_alliance so you know gold,
resources, favor, open borders, and alliance eligibility.

Surplus luxuries beyond one copy give no extra amenities. Sell duplicates or spare
diplomatic favor for gold or gold-per-turn, preferably testing the deal first with
propose_trade(mode="test") before committing with mode="send". Open borders are also
trade deals.

If a war is going badly or has stopped producing gains, propose_peace after the
10-turn cooldown. To start a war, send_diplomatic_action(action="DECLARE_SURPRISE_WAR")
or another valid DECLARE_*_WAR token declares it, but attacks usually work next
turn; declare, position safely, then attack on the following turn.
```

- [x] **Step 5: Run experiment and playbook tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_experiment.py -q
```

Expected: PASS for `tests/arena/test_experiment.py`.

- [x] **Step 6: Commit**

```bash
git add src/civ_mcp/arena/playbook.md tests/arena/test_experiment.py
git commit -m "docs(arena): add diplomacy trade and peace doctrine"
```

---

## Task 4: Full Verification

**Files:**
- No source changes unless verification exposes a real bug.

- [x] **Step 1: Run focused arena tests**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests/arena/test_registry.py tests/arena/test_analyze.py tests/arena/test_experiment.py -q
```

Expected: PASS.

- [x] **Step 2: Run full test suite**

Run:

```bash
/home/riz/.local/bin/uv run --extra test pytest tests -q
```

Expected: PASS.

- [x] **Step 3: Check whitespace**

Run:

```bash
git diff --check
```

Expected: no output.

- [x] **Step 4: Optional live validation when safe**

Only run this if the live arena watcher is idle on a human turn and not mid-AI-phase:

1. Start or resume an arena config where a treatment puppet uses `tools: full`.
2. Confirm `openai_tools(resolve_tools("full"))` includes the nine new tool names.
3. If a pending diplomacy or incoming deal naturally appears, confirm the puppet can call the corresponding `get_pending_*` and `respond_to_*` tools.

If no diplomacy event appears, record "live diplomacy event not observed" in the branch summary. Do not force-kill or interrupt the watcher to manufacture this condition.

---

## Task 5: Documentation Sync and Review Handoff

**Files:**
- Modify: `docs/superpowers/specs/2026-07-05-arena-puppet-decision-making-design.md`

- [x] **Step 1: Update the slice status line**

Replace the status line at the top of `docs/superpowers/specs/2026-07-05-arena-puppet-decision-making-design.md` with:

```markdown
**Status:** Slice 1 implemented + hardened (2026-07-06, through `dc7f7e3`); Slice 2 implementation branch ready for review (2026-07-06, `arena-diplomacy-tools-slice2`); Slice 3 remains a follow-on.
```

- [x] **Step 2: Check docs diff**

Run:

```bash
git diff -- docs/superpowers/specs/2026-07-05-arena-puppet-decision-making-design.md
```

Expected: the only spec change is the single status line above.

- [x] **Step 3: Check whitespace**

Run:

```bash
git diff --check
```

Expected: no output.

- [x] **Step 4: Commit docs sync**

```bash
git add docs/superpowers/specs/2026-07-05-arena-puppet-decision-making-design.md
git commit -m "docs: sync arena diplomacy slice status"
```

- [x] **Step 5: Final branch status**

Run:

```bash
git status --short --branch
git log --oneline -5
```

Expected: clean tracked worktree on `arena-diplomacy-tools-slice2`, with the four implementation/docs commits visible:

```text
docs: sync arena diplomacy slice status
docs(arena): add diplomacy trade and peace doctrine
feat(arena): track diplomacy actions in analysis vocab
feat(arena): register diplomacy and trade tools
```

Stop here for separate-session review. Do not merge, push, or remove the worktree without explicit direction from riz in that turn.

---

## Self-Review Notes

- Spec coverage: implements Slice 2 approved boundary: missing inter-civ tools plus doctrine. Reactive sessions are included through existing public MCP semantics; no new state machine is added.
- Tiering: no mid tier; treatment/full gains tools automatically, control/minimal does not.
- Public names: plan exposes `get_pending_diplomacy`, `respond_to_diplomacy`, `get_pending_trades`, `respond_to_trade`, `get_trade_options`; internal `GameState` method names stay behind registry wrappers.
- Testing: failing tests precede each implementation task; full verification includes focused and complete suites.
- Plan hygiene: registry tests and implementation are one reviewable task; the final task updates the design-spec status before review handoff.
