"""Per-turn capability snapshot: CAPS| line -> flags dict, fail-open everywhere."""
from civ_mcp.arena.capabilities import CAP_FLAGS, build_caps_query, parse_caps


CAPS_LINE = ("CAPS|spies=0|government=1|religious_unit=0|gp_unit=1|corps=0"
             "|army=0|air=0|archaeology=0|great_works=1")


def test_cap_flags_inventory():
    assert CAP_FLAGS == ("spies", "government", "religious_unit", "gp_unit",
                         "corps", "army", "air", "archaeology", "great_works")


def test_build_caps_query_shape():
    lua = build_caps_query(3)
    assert "Players[3]" in lua                    # explicit pid, not GetLocalPlayer
    assert "HasCivic" in lua
    assert "MilitaryFormationTypes" in lua
    assert "pcall" in lua                         # per-check fail-open
    assert "---END---" in lua
    assert "CAPS|" in lua


def test_caps_query_counts_naval_formations_and_guards_enum():
    lua = build_caps_query(3)
    # #2: Fleets (naval) count toward the corps mergeable-pair check
    assert "FORMATION_CLASS_NAVAL" in lua
    # #3: corps/army only override their fail-open default when the enum resolves
    assert "MilitaryFormationTypes.CORPS_FORMATION ~= nil" in lua
    assert "MilitaryFormationTypes.STANDARD_FORMATION ~= nil" in lua


def test_parse_caps_happy_path():
    flags = parse_caps([CAPS_LINE, "---END---"])
    assert flags == {"spies": False, "government": True, "religious_unit": False,
                     "gp_unit": True, "corps": False, "army": False, "air": False,
                     "archaeology": False, "great_works": True}


def test_parse_caps_fail_open_paths():
    assert parse_caps(None) is None
    assert parse_caps([]) is None
    assert parse_caps(["LUA ERROR: nope"]) is None
    # partial line: unknown keys skipped, known keys kept, missing keys absent
    flags = parse_caps(["CAPS|spies=1|bogus=1|government="])
    assert flags == {"spies": True}


import asyncio as _aio

import pytest

from civ_mcp.arena.config import ArenaConfig, PlayerSpec
from civ_mcp.arena.coordinator import run_arena

from .test_coordinator import FakeConn, FakeGS   # same pattern as test_orphan_sweep


CAPTURE_POLLS = [
    ["LOCAL|1", "TURN|2", "ACTIVE|true", "LAST|1"],
    ["LOCAL|0", "TURN|2", "ACTIVE|false", "LAST|1"],
]


class CapsConn(FakeConn):
    def __init__(self, caps_lines=None, raise_on_caps=False):
        super().__init__()
        self.caps_lines = caps_lines or [CAPS_LINE, "---END---"]
        self.raise_on_caps = raise_on_caps

    async def execute_read(self, lua, timeout=5.0):
        if "CAPS|" in lua:
            if self.raise_on_caps:
                raise ConnectionError("read context dead")
            return self.caps_lines
        return await super().execute_read(lua, timeout=timeout)


class CapsRecordingPolicy:
    def __init__(self):
        self.received = "NOT_CALLED"

    async def __call__(self, gs, player_id, turn, *, caps=None, **kw):
        self.received = caps
        return {"summary": "ok", "actions": []}


def _cfg():
    return ArenaConfig(players=[PlayerSpec(1, "local", "m")], max_puppet_turns=1,
                       dry_run=True, puppet_ids=[1], idle_poll_limit=10)


@pytest.mark.asyncio
async def test_coordinator_passes_parsed_caps_to_policy(monkeypatch):
    async def noop(_d): pass
    monkeypatch.setattr(_aio, "sleep", noop)
    conn = CapsConn()
    conn._polls = iter(CAPTURE_POLLS)
    pol = CapsRecordingPolicy()
    await run_arena(conn, FakeGS(), _cfg(), policy=pol)
    assert pol.received == parse_caps([CAPS_LINE])


@pytest.mark.asyncio
async def test_snapshot_failure_fails_open_and_run_continues(monkeypatch):
    async def noop(_d): pass
    monkeypatch.setattr(_aio, "sleep", noop)
    conn = CapsConn(raise_on_caps=True)
    conn._polls = iter(CAPTURE_POLLS)
    pol = CapsRecordingPolicy()
    result = await run_arena(conn, FakeGS(), _cfg(), policy=pol)
    assert pol.received is None            # kwarg default: full toolset
    assert result["puppet_turns_played"] == 1


def test_parse_caps_real_capture():
    from civ_mcp.arena.capabilities import parse_caps
    flags = parse_caps([
        "CAPS|spies=1|government=1|religious_unit=0|gp_unit=0|corps=1|army=1|air=1|archaeology=0|great_works=1"
    ])
    assert flags["corps"] and flags["army"] and flags["air"] and flags["great_works"]
    assert flags["archaeology"] is False  # charge-0 archaeologist -> flag flips correctly
