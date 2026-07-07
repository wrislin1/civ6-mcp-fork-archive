"""Orphaned diplomacy-session sweep.

Live failure (2026-07-07, run 8civ-3gemma-50r): two PUPPET civs (seats 1 and 3)
first-met each other during the AI phase. The greeting session was keyed between
the two puppets, so `_clear_blocking_diplomacy`'s me-centric scan (me=0) found
nothing to close (`CLEAR|blocked|closed=0`) and the view-hide alone did not
unwedge the engine -- turn processing stalled before seat 5's activation and the
whole game froze on an unclickable first-meet scene. Closing the 1<->3 session
directly unwedged the game within 2s.

The sweep closes open sessions NOT involving the local player (those can never
be clickable by the human and are orphaned by construction during arena play)
and is triggered from the coordinator's idle branch once the human seat has
been idle long enough to look wedged.
"""
import asyncio

import pytest

from civ_mcp.arena.config import ArenaConfig, PlayerSpec
from civ_mcp.arena.coordinator import (
    ORPHAN_SWEEP_IDLE_POLLS,
    _sweep_orphan_sessions,
    run_arena,
    ScriptedPolicy,
)

from .test_coordinator import FakeConn, FakeGS


class SweepRecordingConn(FakeConn):
    """FakeConn that records execute_write lua and serves a canned sweep result."""

    def __init__(self, sweep_lines=None):
        super().__init__()
        self.write_calls = []
        self._sweep_lines = sweep_lines or ["ORPHANS|none"]

    async def execute_write(self, lua, timeout=5.0):
        self.write_calls.append(lua)
        self._maybe_die()
        if "FindOpenSessionID" in lua:
            return self._sweep_lines
        return []


def _sweep_writes(conn):
    return [w for w in conn.write_calls if "FindOpenSessionID" in w]


def test_build_close_orphan_sessions_lua_shape():
    """The sweep lua scans all player pairs, skips the local player's own
    sessions (never touch a scene the human is using), closes what it finds,
    and reports under the ORPHANS| prefix."""
    from civ_mcp import lua as lq

    lua = lq.build_close_orphan_sessions()
    assert "FindOpenSessionID" in lua
    assert "CloseSession" in lua
    assert "GetLocalPlayer" in lua      # me-sessions are excluded, not swept
    assert "ORPHANS|" in lua


def test_sweep_orphan_sessions_reports_and_swallows_errors():
    """Best-effort contract, mirroring _clear_blocking_diplomacy."""

    class _Boom:
        async def execute_write(self, lua, timeout=5.0):
            raise ConnectionError("dead socket")

    assert asyncio.run(_sweep_orphan_sessions(_Boom())) == "err"

    conn = SweepRecordingConn(sweep_lines=["ORPHANS|1-3#65539"])
    assert asyncio.run(_sweep_orphan_sessions(conn)) == "ORPHANS|1-3#65539"


@pytest.mark.asyncio
async def test_long_idle_streak_triggers_orphan_sweep(monkeypatch):
    """Once the human seat has idled past ORPHAN_SWEEP_IDLE_POLLS consecutive
    polls (the wedge signature: game stuck mid-AI-phase, nothing advancing),
    the coordinator fires the orphan-session sweep."""
    async def noop(_delay):
        pass

    monkeypatch.setattr(asyncio, "sleep", noop)

    conn = SweepRecordingConn()
    # every poll idle: StopIteration fallback in FakeConn stays LOCAL|0/ACTIVE|false
    conn._polls = iter([])
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m")], max_puppet_turns=1,
                      dry_run=True, puppet_ids=[1],
                      idle_poll_limit=ORPHAN_SWEEP_IDLE_POLLS + 5)

    await run_arena(conn, FakeGS(), cfg, policy=ScriptedPolicy())

    assert len(_sweep_writes(conn)) == 1


@pytest.mark.asyncio
async def test_short_idle_does_not_sweep(monkeypatch):
    """An ordinary human turn (idle below the threshold) never fires the sweep --
    the human's own open scenes must not be poked at every poll."""
    async def noop(_delay):
        pass

    monkeypatch.setattr(asyncio, "sleep", noop)

    conn = SweepRecordingConn()
    conn._polls = iter([])
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m")], max_puppet_turns=1,
                      dry_run=True, puppet_ids=[1],
                      idle_poll_limit=ORPHAN_SWEEP_IDLE_POLLS - 1)

    await run_arena(conn, FakeGS(), cfg, policy=ScriptedPolicy())

    assert _sweep_writes(conn) == []


@pytest.mark.asyncio
async def test_puppet_capture_resets_idle_streak(monkeypatch):
    """A puppet turn between idle stretches resets the streak: two half-threshold
    idle stretches separated by a capture must NOT add up to a sweep."""
    async def noop(_delay):
        pass

    monkeypatch.setattr(asyncio, "sleep", noop)

    half = ORPHAN_SWEEP_IDLE_POLLS // 2 + 5
    conn = SweepRecordingConn()
    polls = (
        [["LOCAL|0", "TURN|1", "ACTIVE|false", "LAST|nil"]] * half
        + [["LOCAL|1", "TURN|2", "ACTIVE|true", "LAST|1"]]
        + [["LOCAL|0", "TURN|2", "ACTIVE|false", "LAST|1"]] * half
    )
    conn._polls = iter(polls)
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m")], max_puppet_turns=2,
                      dry_run=True, puppet_ids=[1],
                      idle_poll_limit=2 * half + 2)

    await run_arena(conn, FakeGS(), cfg, policy=ScriptedPolicy())

    assert _sweep_writes(conn) == []
