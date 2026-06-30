import pytest
import asyncio
from civ_mcp.arena.coordinator import run_arena, ScriptedPolicy, _reconnect_with_retry
from civ_mcp.arena.config import ArenaConfig, PlayerSpec

class FakeConn:
    """Serves canned GameCore reads by matching key substrings in the Lua.

    Models a REAL socket: when disconnected it raises on execute_* (a dead FireTuner
    socket cannot serve reads). This is what makes the human-safety tests honest — a
    permanently-dead connection genuinely cannot restore the human, and the tests must
    observe that rather than pass on canned reads served over a dead socket.
    """
    def __init__(self):
        self.restored = False
        self._connected = True
        self._dead_when_disconnected = True   # behave like a real socket
        self.read_calls = []                  # every lua passed to execute_read (even if it raises)
        self._polls = iter([
            ["LOCAL|0", "TURN|1", "ACTIVE|false", "LAST|nil"],   # human turn
            ["LOCAL|1", "TURN|2", "ACTIVE|true", "LAST|1"],      # puppet held
        ])
    @property
    def is_connected(self): return self._connected
    async def connect(self): self._connected = True
    async def disconnect(self): self._connected = False
    def _maybe_die(self):
        if self._dead_when_disconnected and not self._connected:
            raise ConnectionError("FakeConn: socket dead while disconnected")
    async def execute_read(self, lua, timeout=5.0):
        self.read_calls.append(lua)
        self._maybe_die()
        if "GetCurrentGameTurn" in lua and "GetLocalPlayer" in lua and "ACTIVE" in lua:
            try: return next(self._polls)
            except StopIteration: return ["LOCAL|0", "TURN|2", "ACTIVE|false", "LAST|1"]
        if "SetLocalPlayerAndObserver(0)" in lua:
            self.restored = True; return ["LOCAL|0"]
        if "HOOK_OK" in lua or "__pt_registered" in lua: return ["HOOK_OK|true"]
        if "DISABLED" in lua: return ["DISABLED|true"]
        if "FINISHED" in lua: return ["FINISHED|1"]
        return []
    async def execute_write(self, lua, timeout=5.0):
        self._maybe_die()
        return []

class FakeGS:
    def __init__(self): self.ran = 0
    async def get_game_overview(self): return "OV"
    async def get_units(self): return []
    async def skip_unit(self, i): self.ran += 1; return "SKIP"

@pytest.mark.asyncio
async def test_coordinator_runs_one_puppet_turn_and_restores():
    conn, gs = FakeConn(), FakeGS()
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m")], max_puppet_turns=1,
                      dry_run=True, puppet_ids=[1])
    result = await run_arena(conn, gs, cfg, policy=ScriptedPolicy())
    assert result["puppet_turns_played"] == 1
    assert conn.restored is True
    assert gs.ran == 1


@pytest.mark.asyncio
async def test_coordinator_respects_idle_poll_limit(monkeypatch):
    async def noop(_delay): pass
    monkeypatch.setattr(asyncio, "sleep", noop)

    conn, gs = FakeConn(), FakeGS()
    conn._polls = iter([
        ["LOCAL|0", "TURN|1", "ACTIVE|false", "LAST|nil"],
        ["LOCAL|0", "TURN|1", "ACTIVE|false", "LAST|nil"],
    ])
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m")], max_puppet_turns=1,
                      dry_run=True, puppet_ids=[1], idle_poll_limit=2)
    result = await run_arena(conn, gs, cfg, policy=ScriptedPolicy())
    assert result["puppet_turns_played"] == 0
    poll_reads = [c for c in conn.read_calls if "GetCurrentGameTurn" in c]
    assert len(poll_reads) == 2


class FakeConnFlaky(FakeConn):
    """FakeConn where connect() raises on the first `fail_times` calls then succeeds."""
    def __init__(self, fail_times=1):
        super().__init__()
        self._fail_remaining = fail_times
        self.connect_attempts = 0

    async def connect(self):
        self.connect_attempts += 1
        if self._fail_remaining > 0:
            self._fail_remaining -= 1
            raise OSError("port 4318 still in use")
        await super().connect()


@pytest.mark.asyncio
async def test_reconnect_retry_succeeds_after_failures():
    """_reconnect_with_retry returns True when connect eventually succeeds."""
    conn = FakeConnFlaky(fail_times=2)
    conn._connected = False  # start disconnected
    result = await _reconnect_with_retry(conn, attempts=5, delay=0)
    assert result is True
    assert conn.is_connected is True
    assert conn.connect_attempts == 3  # 2 failures + 1 success


@pytest.mark.asyncio
async def test_reconnect_retry_all_fail():
    """_reconnect_with_retry returns False (no raise) when all attempts fail."""
    conn = FakeConnFlaky(fail_times=999)
    conn._connected = False
    result = await _reconnect_with_retry(conn, attempts=3, delay=0)
    assert result is False
    assert conn.connect_attempts == 3
    assert conn.is_connected is False


@pytest.mark.asyncio
async def test_coordinator_reclaim_retry_restores_human(monkeypatch):
    """Human is restored even when reclaim connect fails on the first attempt."""
    async def noop(_delay): pass
    monkeypatch.setattr(asyncio, "sleep", noop)

    class ExclusivePol:
        needs_exclusive_tuner = True
        async def __call__(self, gs, player_id, turn):
            return {"summary": "cli ran", "actions": []}

    conn = FakeConnFlaky(fail_times=1)
    gs = FakeGS()
    cfg = ArenaConfig(players=[PlayerSpec(1, "cli-claude", "")], max_puppet_turns=1,
                      puppet_ids=[1])
    result = await run_arena(conn, gs, cfg, policy=ExclusivePol())
    assert result["puppet_turns_played"] == 1
    assert conn.restored is True
    assert conn.is_connected is True


@pytest.mark.asyncio
async def test_coordinator_dead_socket_attempts_full_handback_then_surfaces(monkeypatch):
    """Permanently-dead tuner socket: the coordinator ATTEMPTS reclaim, restore, and disable
    (all three, best-effort), then surfaces the failure. It must NOT falsely report the human
    restored over a socket that genuinely cannot carry the restore command."""
    async def noop(_delay): pass
    monkeypatch.setattr(asyncio, "sleep", noop)

    class ExclusivePol:
        needs_exclusive_tuner = True
        async def __call__(self, gs, player_id, turn):
            return {"summary": "cli ran", "actions": []}

    class DeadSocketConn(FakeConn):
        """connect() always fails → after the exclusive disconnect the socket stays dead."""
        def __init__(self):
            super().__init__()
            self.connect_attempts = 0
        async def connect(self):
            self.connect_attempts += 1
            raise OSError("port 4318 still held")

    conn = DeadSocketConn()
    gs = FakeGS()
    cfg = ArenaConfig(players=[PlayerSpec(1, "cli-claude", "")], max_puppet_turns=1,
                      puppet_ids=[1])
    # Over a dead socket the handback cannot complete; run_arena surfaces the failure
    # rather than returning a fabricated success.
    with pytest.raises(ConnectionError):
        await run_arena(conn, gs, cfg, policy=ExclusivePol())
    # reclaim was attempted to exhaustion (in-loop + finally budgets)
    assert conn.connect_attempts >= 5
    # restore_local(0) AND disable were still ATTEMPTED in the finally despite the dead socket
    assert any("SetLocalPlayerAndObserver(0)" in c for c in conn.read_calls)
    assert any("DISABLED" in c for c in conn.read_calls)
    # ...but restore did NOT succeed — no fake handback over a dead socket
    assert conn.restored is False


@pytest.mark.asyncio
async def test_coordinator_body_cancellation_not_masked_by_cleanup_error(monkeypatch):
    """The realistic Ctrl-C path: cancellation originates in the policy BODY (during the long
    CLI turn), not in a finally step. The finally then runs over a dead socket, so reclaim/
    restore/disable each raise an ordinary ConnectionError. The propagated exception MUST stay
    CancelledError — a best-effort cleanup Exception must NOT replace the in-flight cancellation.
    Goes red under the pre-fix `raise first_exc` (which would surface ConnectionError instead)."""
    async def noop(_delay): pass
    monkeypatch.setattr(asyncio, "sleep", noop)

    class CancelInBodyPol:
        needs_exclusive_tuner = True
        async def __call__(self, gs, player_id, turn):
            raise asyncio.CancelledError()   # Ctrl-C lands mid-turn

    class DeadSocketConn(FakeConn):
        """connect() always fails → after the exclusive disconnect the socket stays dead, so
        every finally step raises an ordinary ConnectionError."""
        def __init__(self):
            super().__init__()
            self.connect_attempts = 0
        async def connect(self):
            self.connect_attempts += 1
            raise OSError("port 4318 still held")

    conn = DeadSocketConn()
    gs = FakeGS()
    cfg = ArenaConfig(players=[PlayerSpec(1, "cli-claude", "")], max_puppet_turns=1,
                      puppet_ids=[1])
    with pytest.raises(asyncio.CancelledError):
        await run_arena(conn, gs, cfg, policy=CancelInBodyPol())
    # cleanup was still attempted best-effort despite the in-flight cancellation
    assert any("SetLocalPlayerAndObserver(0)" in c for c in conn.read_calls)
    assert any("DISABLED" in c for c in conn.read_calls)


@pytest.mark.asyncio
async def test_coordinator_cancelled_in_finally_reraises_after_full_handback(monkeypatch):
    """A CancelledError from the FINALLY reclaim must (a) not skip restore/disable and (b) be
    the exception that propagates. The socket is dead so finish_units leaves a ConnectionError
    in flight; only the finally's `raise first_exc` turns the surfaced exception into
    CancelledError — so this test goes red if either the re-raise or the BaseException capture
    is removed."""
    async def noop(_delay): pass
    monkeypatch.setattr(asyncio, "sleep", noop)

    class ExclusivePol:
        needs_exclusive_tuner = True
        async def __call__(self, gs, player_id, turn):
            return {"summary": "cli ran", "actions": []}

    class CancelInFinallyConn(FakeConn):
        """In-loop reclaim fails with OSError (retry returns False, socket stays dead); the
        first dead-socket read marks that we've left the loop body, so the FINALLY reclaim's
        connect() is the one that raises CancelledError."""
        def __init__(self):
            super().__init__()
            self._headed_to_finally = False
        async def connect(self):
            if self._headed_to_finally:
                raise asyncio.CancelledError()
            raise OSError("port 4318 busy")
        async def execute_read(self, lua, timeout=5.0):
            if not self._connected:
                self._headed_to_finally = True
            return await super().execute_read(lua, timeout)

    conn = CancelInFinallyConn()
    gs = FakeGS()
    cfg = ArenaConfig(players=[PlayerSpec(1, "cli-claude", "")], max_puppet_turns=1,
                      puppet_ids=[1])
    with pytest.raises(asyncio.CancelledError):
        await run_arena(conn, gs, cfg, policy=ExclusivePol())
    # restore_local(0) was still attempted in the handback despite the CancelledError
    assert any("SetLocalPlayerAndObserver(0)" in c for c in conn.read_calls)
