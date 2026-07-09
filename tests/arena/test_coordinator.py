import pytest
import asyncio
from civ_mcp import lua as lq
from civ_mcp.arena import autoresolve
from civ_mcp.arena.coordinator import run_arena, ScriptedPolicy, _reconnect_with_retry
from civ_mcp.arena.config import (
    ArenaConfig,
    BriefingOptions,
    CivOptions,
    MemoryOptions,
    PlayerSpec,
    TaskTrackerOptions,
)
from civ_mcp.arena.memory import memory_path, save_memory
from civ_mcp.arena.task_tracker import UnitTask, save_task_state, task_path

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


class _PromoUnit:
    def __init__(self, unit_id):
        self.unit_id = unit_id
        self.unit_type = "UNIT_WARRIOR"


class SweepGS(FakeGS):
    def __init__(self):
        super().__init__()
        self.promoted = []

    async def get_units(self):
        return [_PromoUnit(1)]

    async def get_unit_promotions(self, unit_id):
        return lq.UnitPromotionStatus(
            unit_id=unit_id,
            unit_index=1,
            unit_type="UNIT_WARRIOR",
            promotions=[
                lq.PromotionOption(
                    promotion_type="PROMOTION_BATTLECRY",
                    name="Battlecry",
                    description="d",
                )
            ],
        )

    async def promote_unit(self, unit_id, promotion_type):
        self.promoted.append((unit_id, promotion_type))
        return f"Promoted {unit_id}"


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
        async def __call__(self, gs, player_id, turn, **kwargs):
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
        async def __call__(self, gs, player_id, turn, **kwargs):
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
        async def __call__(self, gs, player_id, turn, **kwargs):
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
        async def __call__(self, gs, player_id, turn, **kwargs):
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


@pytest.mark.asyncio
async def test_sweep_runs_and_is_logged():
    conn, gs = FakeConn(), SweepGS()
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m")], max_puppet_turns=1,
                      dry_run=True, puppet_ids=[1])

    result = await run_arena(conn, gs, cfg, policy=ScriptedPolicy())

    assert conn.restored is True
    assert gs.promoted == [(1, "PROMOTION_BATTLECRY")]
    assert result["log"][0]["promotion_sweep"][0]["promotion_type"] == "PROMOTION_BATTLECRY"


@pytest.mark.asyncio
async def test_sweep_failure_does_not_block_handback(monkeypatch):
    async def boom(_gs):
        raise RuntimeError("sweep failed")

    class NoopPolicy:
        async def __call__(self, gs, player_id, turn, **kwargs):
            return {"summary": "noop", "actions": []}

    monkeypatch.setattr(autoresolve, "sweep_promotions", boom)
    conn, gs = FakeConn(), FakeGS()
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m")], max_puppet_turns=1,
                      dry_run=True, puppet_ids=[1])

    result = await run_arena(conn, gs, cfg, policy=NoopPolicy())

    assert conn.restored is True
    assert result["log"][0]["promotion_sweep"] == []


@pytest.mark.asyncio
async def test_policy_result_cannot_overwrite_promotion_sweep_log():
    class ConflictingPolicy:
        async def __call__(self, gs, player_id, turn, **kwargs):
            return {
                "summary": "conflict",
                "actions": [],
                "promotion_sweep": [{"promotion_type": "POLICY_VALUE"}],
            }

    conn, gs = FakeConn(), SweepGS()
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m")], max_puppet_turns=1,
                      dry_run=True, puppet_ids=[1])

    result = await run_arena(conn, gs, cfg, policy=ConflictingPolicy())

    assert result["log"][0]["promotion_sweep"][0]["promotion_type"] == "PROMOTION_BATTLECRY"


@pytest.mark.asyncio
async def test_exclusive_policy_reconnects_before_sweep(monkeypatch):
    sweep_connected = []

    async def recording_sweep(_gs):
        sweep_connected.append(conn.is_connected)
        return [{"promotion_type": "PROMOTION_BATTLECRY"}]

    class ExclusivePolicy:
        needs_exclusive_tuner = True

        async def __call__(self, gs, player_id, turn, **kwargs):
            assert conn.is_connected is False
            return {"summary": "exclusive", "actions": []}

    conn, gs = FakeConn(), FakeGS()
    monkeypatch.setattr(autoresolve, "sweep_promotions", recording_sweep)
    cfg = ArenaConfig(players=[PlayerSpec(1, "cli-claude", "m")], max_puppet_turns=1,
                      dry_run=True, puppet_ids=[1])

    result = await run_arena(conn, gs, cfg, policy=ExclusivePolicy())

    assert result["puppet_turns_played"] == 1
    assert sweep_connected == [True]


# ---------------------------------------------------------------------------
# Task 4 — transcript instrumentation tests
# ---------------------------------------------------------------------------

_OV_BEFORE = "1|1|CivA|Leader|100.0|5.0|10.0|8.0|20.0|Mining|Drama|2|5|50"
_OV_AFTER  = "1|1|CivA|Leader|110.0|5.0|12.0|9.0|25.0|Mining|Drama|2|6|55"


class FakeConnWithOverview(FakeConn):
    """FakeConn that returns two distinct overview lines on sequential execute_write calls."""
    def __init__(self):
        super().__init__()
        self._overview_calls = 0

    async def execute_write(self, lua, timeout=5.0):
        self._maybe_die()
        if "Game.GetLocalPlayer" in lua:
            self._overview_calls += 1
            return [_OV_BEFORE] if self._overview_calls == 1 else [_OV_AFTER]
        return []


class FakeGSWithConn(FakeGS):
    """FakeGS with a .conn attribute for _overview_snapshot."""
    def __init__(self, conn):
        super().__init__()
        self.conn = conn


class FakeSink:
    """Recording transcript sink."""
    def __init__(self): self.records = []
    def write(self, record: dict): self.records.append(record)


class TranscriptPolicy:
    """Policy that returns a transcript payload."""
    provider = "local"
    model = "test-model"

    async def __call__(self, gs, player_id, turn, **kwargs):
        return {
            "summary": "done",
            "transcript": {
                "steps": [{"tool": "get_game_overview"}, {"tool": "end_turn"}],
                "final_answer": "ok",
            },
            "usage": {"usd": 0.05},
        }


@pytest.mark.asyncio
async def test_transcript_write_called_once_per_puppet_turn():
    """transcript.write is called exactly once per puppet turn, with correct payload."""
    conn = FakeConnWithOverview()
    gs = FakeGSWithConn(conn)
    sink = FakeSink()
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m")], max_puppet_turns=1,
                      dry_run=True, puppet_ids=[1])

    result = await run_arena(conn, gs, cfg, policy=TranscriptPolicy(), transcript=sink)

    assert result["puppet_turns_played"] == 1
    assert len(sink.records) == 1

    rec = sink.records[0]
    assert rec["schema_version"] == 1
    assert rec["player_id"] == 1
    assert rec["turn"] == 2          # from _polls: TURN|2
    assert rec["step_count"] == 2
    assert rec["usd"] == pytest.approx(0.05)
    assert rec["provider"] == "local"
    assert rec["model"] == "test-model"
    assert rec["driver"] == "in_process"
    # payload keys merged in
    assert rec["final_answer"] == "ok"


@pytest.mark.asyncio
async def test_transcript_record_includes_promotion_sweep():
    conn = FakeConnWithOverview()
    gs = SweepGS()
    gs.conn = conn
    sink = FakeSink()
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m")], max_puppet_turns=1,
                      dry_run=True, puppet_ids=[1])

    await run_arena(conn, gs, cfg, policy=TranscriptPolicy(), transcript=sink)

    assert sink.records[0]["promotion_sweep"][0]["promotion_type"] == "PROMOTION_BATTLECRY"


@pytest.mark.asyncio
async def test_transcript_state_before_after_delta():
    """state_before / state_after / state_delta are computed from the two overview snapshots."""
    conn = FakeConnWithOverview()
    gs = FakeGSWithConn(conn)
    sink = FakeSink()
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m")], max_puppet_turns=1,
                      dry_run=True, puppet_ids=[1])

    await run_arena(conn, gs, cfg, policy=TranscriptPolicy(), transcript=sink)

    rec = sink.records[0]
    before = rec["state_before"]
    after = rec["state_after"]
    delta = rec["state_delta"]

    assert before["gold"] == pytest.approx(100.0)
    assert after["gold"]  == pytest.approx(110.0)

    assert delta["gold"]    == pytest.approx(10.0)
    assert delta["science"] == pytest.approx(2.0)
    assert delta["culture"] == pytest.approx(1.0)
    assert delta["faith"]   == pytest.approx(5.0)
    assert delta["score"]   == 5
    assert delta["cities"]  == 0
    assert delta["units"]   == 1
    # string fields come from the after snapshot
    assert delta["research"] == "Mining"
    assert delta["civic"]    == "Drama"


@pytest.mark.asyncio
async def test_transcript_none_adds_no_snapshot_reads():
    """transcript=None (default) → ZERO overview queries issued to the game."""
    class CountingWriteConn(FakeConn):
        def __init__(self):
            super().__init__()
            self.overview_queries = 0
        async def execute_write(self, lua, timeout=5.0):
            self._maybe_die()
            if "Game.GetLocalPlayer" in lua:
                self.overview_queries += 1
            return []

    conn = CountingWriteConn()
    gs = FakeGSWithConn(conn)
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m")], max_puppet_turns=1,
                      dry_run=True, puppet_ids=[1])

    # transcript not passed → default None → behavior-neutral
    await run_arena(conn, gs, cfg, policy=TranscriptPolicy())
    assert conn.overview_queries == 0


@pytest.mark.asyncio
async def test_coordinator_run_id_propagates_to_record():
    """run_id set on ArenaConfig reaches the written transcript record."""
    conn = FakeConnWithOverview()
    gs = FakeGSWithConn(conn)
    sink = FakeSink()
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m")], max_puppet_turns=1,
                      dry_run=True, puppet_ids=[1], run_id="arena-run-xyz-42")

    await run_arena(conn, gs, cfg, policy=TranscriptPolicy(), transcript=sink)

    assert len(sink.records) == 1
    assert sink.records[0]["run_id"] == "arena-run-xyz-42"


@pytest.mark.asyncio
async def test_log_entry_excludes_transcript_key():
    """run_arena log entries must NOT carry the 'transcript' key (stdout bloat).
    The sink record must still contain the steps (data not lost)."""
    conn = FakeConnWithOverview()
    gs = FakeGSWithConn(conn)
    sink = FakeSink()
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m")], max_puppet_turns=1,
                      dry_run=True, puppet_ids=[1])

    result = await run_arena(conn, gs, cfg, policy=TranscriptPolicy(), transcript=sink)

    assert result["puppet_turns_played"] == 1
    # Every log entry must be transcript-free
    for entry in result["log"]:
        assert "transcript" not in entry, "log entry must not carry the full transcript"
    # Sink must still have the full record with steps present
    assert len(sink.records) == 1
    assert sink.records[0]["step_count"] == 2


@pytest.mark.asyncio
async def test_null_sink_zero_snapshot_reads():
    """NullSink (enabled=False) → ZERO overview queries issued; write is a no-op.

    This is the H2 gate: NullSink overhead is eliminated. FAILS before H2 (was 2).
    """
    from civ_mcp.arena.transcript import NullSink

    ov_line = "1|1|CivA|Leader|100.0|5.0|10.0|8.0|20.0|Mining|Drama|2|5|50"

    class CountingWriteConn(FakeConn):
        def __init__(self):
            super().__init__()
            self.overview_queries = 0
        async def execute_write(self, lua, timeout=5.0):
            self._maybe_die()
            if "Game.GetLocalPlayer" in lua:
                self.overview_queries += 1
                return [ov_line]
            return []

    conn = CountingWriteConn()
    gs = FakeGSWithConn(conn)
    sink = NullSink()
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m")], max_puppet_turns=1,
                      dry_run=True, puppet_ids=[1])

    result = await run_arena(conn, gs, cfg, policy=TranscriptPolicy(), transcript=sink)

    assert result["puppet_turns_played"] == 1
    # NullSink.enabled=False → coordinator must skip both snapshots entirely
    assert conn.overview_queries == 0, (
        f"NullSink must produce 0 overview queries, got {conn.overview_queries}"
    )


@pytest.mark.asyncio
async def test_transcript_sink_two_snapshot_reads():
    """TranscriptSink (enabled=True) → 2 overview queries per puppet turn (before + after)."""
    import tempfile, os
    from civ_mcp.arena.transcript import TranscriptSink

    ov_line = "1|1|CivA|Leader|100.0|5.0|10.0|8.0|20.0|Mining|Drama|2|5|50"

    class CountingWriteConn(FakeConn):
        def __init__(self):
            super().__init__()
            self.overview_queries = 0
        async def execute_write(self, lua, timeout=5.0):
            self._maybe_die()
            if "Game.GetLocalPlayer" in lua:
                self.overview_queries += 1
                return [ov_line]
            return []

    conn = CountingWriteConn()
    gs = FakeGSWithConn(conn)
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m")], max_puppet_turns=1,
                      dry_run=True, puppet_ids=[1])

    with tempfile.TemporaryDirectory() as td:
        sink = TranscriptSink(os.path.join(td, "transcript.jsonl"))
        result = await run_arena(conn, gs, cfg, policy=TranscriptPolicy(), transcript=sink)

    assert result["puppet_turns_played"] == 1
    # TranscriptSink.enabled=True → both snapshots must fire
    assert conn.overview_queries == 2, (
        f"TranscriptSink must produce 2 overview queries, got {conn.overview_queries}"
    )


class _DiploConn:
    """Stub conn for _clear_blocking_diplomacy: serves the CLEAR result, records the Lua."""
    def __init__(self, clear_lines):
        self._lines = clear_lines
        self.writes = []
    async def execute_write(self, lua, timeout=5.0):
        self.writes.append(lua)
        return self._lines


def test_clear_blocking_diplomacy_reports_blocked():
    from civ_mcp.arena.coordinator import _clear_blocking_diplomacy
    conn = _DiploConn(["CLEAR|blocked|closed=1", "---END---"])
    assert asyncio.run(_clear_blocking_diplomacy(conn)) == "CLEAR|blocked|closed=1"
    # used the single visibility-gated clear builder (checks the views by name)
    assert any("DiplomacyActionView" in w and "LeaderScene" in w for w in conn.writes)


def test_clear_blocking_diplomacy_reports_none_when_nothing_visible():
    from civ_mcp.arena.coordinator import _clear_blocking_diplomacy
    conn = _DiploConn(["CLEAR|none", "---END---"])
    assert asyncio.run(_clear_blocking_diplomacy(conn)) == "CLEAR|none"


def test_clear_blocking_diplomacy_swallows_errors():
    from civ_mcp.arena.coordinator import _clear_blocking_diplomacy
    class _Boom:
        async def execute_write(self, lua, timeout=5.0):
            raise ConnectionError("dead socket")
    assert asyncio.run(_clear_blocking_diplomacy(_Boom())) == "err"


# ---------------------------------------------------------------------------
# Task 5 — standing memory / task tracker coordinator integration
# ---------------------------------------------------------------------------


class RecordingPolicy:
    """Fake policy that records the kwargs of every call and returns a canned result."""

    provider = "local"

    def __init__(self, result, options=None, needs_exclusive_tuner=False):
        self.result = result
        self.options = options or CivOptions()
        self.needs_exclusive_tuner = needs_exclusive_tuner
        self.calls = []

    async def __call__(self, gs, player_id, turn, **kwargs):
        self.calls.append(kwargs)
        return self.result


class FakeGSWithUnit(FakeGS):
    """FakeGS whose get_units() serves a single unit at a fixed position, and that
    supports found_city -- enough for a settle task to complete in run_pre_model_tasks."""

    def __init__(self, unit_id, unit_index, x, y, moves_remaining=2.0):
        super().__init__()
        self._unit = lq.UnitInfo(
            unit_id=unit_id, unit_index=unit_index, name="Settler",
            unit_type="UNIT_SETTLER", x=x, y=y, moves_remaining=moves_remaining,
            max_moves=2.0, health=100, max_health=100, valid_improvements=[],
        )
        self.found_city_calls = []
        self.move_unit_calls = []

    async def get_units(self):
        return [self._unit]

    async def get_diplomacy(self):
        return []

    async def get_threat_scan(self):
        return []

    async def get_map_area(self, x, y, radius=2):
        return []

    async def found_city(self, unit_index):
        self.found_city_calls.append(unit_index)
        return "FOUNDED|5,5"

    async def move_unit(self, unit_index, target_x, target_y):
        self.move_unit_calls.append((unit_index, target_x, target_y))
        return f"MOVING_TO|{target_x},{target_y}"


@pytest.mark.asyncio
async def test_memory_from_turn_n_injected_on_turn_n_plus_1(tmp_path):
    """Standing memory captured from one run_arena call's final summary is loaded and
    injected as memory_block on a LATER, independent run_arena call for the same
    run_id/player -- proving the persistence is run-local, not held in-process state."""
    opts = CivOptions(memory=MemoryOptions(enabled=True, max_chars=1200))
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m")], max_puppet_turns=1,
                      dry_run=True, puppet_ids=[1], run_id="memtest",
                      transcript_dir=str(tmp_path))

    pol1 = RecordingPolicy({
        "summary": "did stuff",
        "transcript": {"final_summary": (
            "TACTICAL: did stuff.\nSTANDING PLAN:\n- march settler to (18,24)\n"
        )},
    }, options=opts)
    await run_arena(FakeConn(), FakeGS(), cfg, policy=pol1)
    # Nothing was on disk yet when turn N's policy was invoked.
    assert pol1.calls[0]["memory_block"] == ""

    pol2 = RecordingPolicy({"summary": "no plan this time"}, options=opts)
    await run_arena(FakeConn(), FakeGS(), cfg, policy=pol2)
    assert pol2.calls[0]["memory_block"].startswith("== STANDING PLAN (captured turn 2")
    assert "march settler to (18,24)" in pol2.calls[0]["memory_block"]


@pytest.mark.asyncio
async def test_stale_memory_loaded_but_not_reported_as_injected(tmp_path):
    run_id, player_id = "stale-memtest", 9
    save_memory(
        str(tmp_path),
        run_id,
        player_id,
        turn=1,
        text="keep settling east",
        max_chars=1200,
    )
    opts = CivOptions(memory=MemoryOptions(enabled=True, max_chars=1200, max_age_turns=1))
    cfg = ArenaConfig(
        players=[PlayerSpec(player_id, "local", "m")],
        max_puppet_turns=1,
        dry_run=True,
        puppet_ids=[player_id],
        run_id=run_id,
        transcript_dir=str(tmp_path),
    )
    pol = RecordingPolicy(
        {
            "summary": "no plan this time",
            "transcript": {"final_summary": "TACTICAL: no standing plan"},
        },
        options=opts,
    )
    conn = FakeConnWithOverview()
    conn._polls = iter([
        [f"LOCAL|{player_id}", "TURN|3", "ACTIVE|true", "LAST|1"],
    ])
    gs = FakeGSWithConn(conn)
    sink = FakeSink()

    result = await run_arena(conn, gs, cfg, policy=pol, transcript=sink)

    assert pol.calls[0]["memory_block"] == ""
    log_memory = result["log"][0]["standing_memory"]
    assert log_memory["loaded"] is True
    assert log_memory["injected"] is False
    assert log_memory["injected_chars"] == 0

    transcript_memory = sink.records[0]["standing_memory"]
    assert transcript_memory["loaded"] is True
    assert transcript_memory["injected"] is False
    assert transcript_memory["injected_chars"] == 0


@pytest.mark.asyncio
async def test_final_summary_with_standing_plan_saves_memory_to_disk(tmp_path):
    """A final summary carrying a STANDING PLAN block is captured to the on-disk
    memory store for this run_id/player, verified via the real file path."""
    opts = CivOptions(memory=MemoryOptions(enabled=True, max_chars=1200))
    cfg = ArenaConfig(players=[PlayerSpec(4, "local", "m")], max_puppet_turns=1,
                      dry_run=True, puppet_ids=[4], run_id="memtest2",
                      transcript_dir=str(tmp_path))
    pol = RecordingPolicy({
        "summary": "ignored",
        "transcript": {"final_summary": "STANDING PLAN:\n- keep exploring\n"},
    }, options=opts)

    conn = FakeConn()
    conn._polls = iter([
        ["LOCAL|0", "TURN|1", "ACTIVE|false", "LAST|nil"],
        ["LOCAL|4", "TURN|2", "ACTIVE|true", "LAST|1"],
    ])
    await run_arena(conn, FakeGS(), cfg, policy=pol)

    path = memory_path(str(tmp_path), "memtest2", 4)
    assert path.exists()
    assert "keep exploring" in path.read_text()


@pytest.mark.asyncio
async def test_final_summary_with_task_line_creates_persisted_task(tmp_path):
    """A final summary carrying a TASK line results in a persisted UnitTask on disk,
    even with no pre-existing task state for this player."""
    opts = CivOptions(task_tracker=TaskTrackerOptions(enabled=True, max_tasks=8))
    cfg = ArenaConfig(players=[PlayerSpec(5, "local", "m")], max_puppet_turns=1,
                      dry_run=True, puppet_ids=[5], run_id="tasktest",
                      transcript_dir=str(tmp_path))
    pol = RecordingPolicy({
        "summary": "ignored",
        "transcript": {"final_summary": (
            "STANDING PLAN:\n- march settler\nTASK settle unit_id=42 target=10,12\n"
        )},
    }, options=opts)

    conn = FakeConn()
    conn._polls = iter([
        ["LOCAL|0", "TURN|1", "ACTIVE|false", "LAST|nil"],
        ["LOCAL|5", "TURN|2", "ACTIVE|true", "LAST|1"],
    ])
    await run_arena(conn, FakeGS(), cfg, policy=pol)

    path = task_path(str(tmp_path), "tasktest", 5)
    assert path.exists()
    assert '"unit_id": 42' in path.read_text()
    assert '"kind": "settle"' in path.read_text()


@pytest.mark.asyncio
async def test_task_line_beyond_capture_clamp_still_creates_task(tmp_path):
    """TASK lines are parsed from the raw final summary, so a long Planning
    section that pushes them past the standing-plan capture budget must not
    silently drop them."""
    opts = CivOptions(task_tracker=TaskTrackerOptions(enabled=True, max_tasks=8))
    filler = "\n".join(f"reflection detail line {i}" for i in range(400))
    cfg = ArenaConfig(players=[PlayerSpec(5, "local", "m")], max_puppet_turns=1,
                      dry_run=True, puppet_ids=[5], run_id="taskclamp",
                      transcript_dir=str(tmp_path))
    pol = RecordingPolicy({
        "summary": "ignored",
        "transcript": {"final_summary": (
            "STANDING PLAN:\n- march settler\nPLANNING:\n"
            f"{filler}\nTASK settle unit_id=42 target=10,12\n"
        )},
    }, options=opts)

    conn = FakeConn()
    conn._polls = iter([
        ["LOCAL|0", "TURN|1", "ACTIVE|false", "LAST|nil"],
        ["LOCAL|5", "TURN|2", "ACTIVE|true", "LAST|1"],
    ])
    await run_arena(conn, FakeGS(), cfg, policy=pol)

    path = task_path(str(tmp_path), "taskclamp", 5)
    assert path.exists()
    assert '"unit_id": 42' in path.read_text()


@pytest.mark.asyncio
async def test_failed_tombstone_blocks_restatement_across_turns(tmp_path):
    """A task that exhausted its failure budget on an earlier turn must stay
    blocked when the model restates it verbatim on a later turn: the tombstone
    is persisted, loaded into the capture base, and wins over the restatement."""
    opts = CivOptions(task_tracker=TaskTrackerOptions(enabled=True, max_tasks=8))
    cfg = ArenaConfig(players=[PlayerSpec(5, "local", "m")], max_puppet_turns=1,
                      dry_run=True, puppet_ids=[5], run_id="tombtest",
                      transcript_dir=str(tmp_path))
    tombstone = UnitTask(
        task_id="settle:42", kind="settle", unit_id=42, target_x=10, target_y=12,
        created_turn=1, updated_turn=1, status="failed",
        last_result="found_city_failed_retry_limit", failure_count=3,
    )
    save_task_state(str(tmp_path), "tombtest", 5, [tombstone])
    pol = RecordingPolicy({
        "summary": "ignored",
        "transcript": {"final_summary": (
            "STANDING PLAN:\n- keep trying\nTASK settle unit_id=42 target=10,12\n"
        )},
    }, options=opts)

    conn = FakeConn()
    conn._polls = iter([
        ["LOCAL|0", "TURN|1", "ACTIVE|false", "LAST|nil"],
        ["LOCAL|5", "TURN|2", "ACTIVE|true", "LAST|1"],
    ])
    await run_arena(conn, FakeGS(), cfg, policy=pol)

    from civ_mcp.arena.task_tracker import load_task_state
    state = load_task_state(str(tmp_path), "tombtest", 5)
    assert [t.status for t in state.tasks] == ["failed"]
    assert state.tasks[0].failure_count == 3


@pytest.mark.asyncio
async def test_pre_model_task_results_appear_in_log_and_transcript(tmp_path):
    """A pre-existing active task that completes during the deterministic pre-model
    phase shows up in both the coordinator log entry and the transcript record's
    task_tracker.pre_model_results field."""
    opts = CivOptions(task_tracker=TaskTrackerOptions(enabled=True))
    run_id, player_id = "tasktest2", 6
    existing_task = UnitTask(
        task_id="settle:42", kind="settle", unit_id=42, target_x=5, target_y=5,
        created_turn=1, updated_turn=1,
    )
    save_task_state(str(tmp_path), run_id, player_id, [existing_task])

    cfg = ArenaConfig(players=[PlayerSpec(player_id, "local", "m")], max_puppet_turns=1,
                      dry_run=True, puppet_ids=[player_id], run_id=run_id,
                      transcript_dir=str(tmp_path))
    pol = RecordingPolicy({"summary": "no plan"}, options=opts)

    conn = FakeConn()
    conn._polls = iter([
        ["LOCAL|0", "TURN|1", "ACTIVE|false", "LAST|nil"],
        [f"LOCAL|{player_id}", "TURN|2", "ACTIVE|true", "LAST|1"],
    ])
    gs = FakeGSWithUnit(unit_id=42, unit_index=7, x=5, y=5)

    from civ_mcp.arena.transcript import TranscriptSink
    import os
    sink = TranscriptSink(os.path.join(str(tmp_path), "transcript.jsonl"))
    result = await run_arena(conn, gs, cfg, policy=pol, transcript=sink)

    assert gs.found_city_calls == [7]
    log_entry = result["log"][0]
    assert log_entry["task_tracker"]["active_before"] == 1
    assert log_entry["task_tracker"]["pre_model_results"][0]["action"] == "found_city"
    assert log_entry["task_tracker"]["pre_model_results"][0]["status"] == "complete"


@pytest.mark.asyncio
async def test_pre_model_task_execution_refreshes_updated_turn(tmp_path):
    opts = CivOptions(task_tracker=TaskTrackerOptions(enabled=True, max_tasks=8))
    run_id, player_id = "task-refresh", 6
    existing_task = UnitTask(
        task_id="settle:65537",
        kind="settle",
        unit_id=65537,
        target_x=10,
        target_y=10,
        created_turn=2,
        updated_turn=2,
    )
    save_task_state(str(tmp_path), run_id, player_id, [existing_task])
    cfg = ArenaConfig(
        players=[PlayerSpec(player_id, "local", "m")],
        max_puppet_turns=1,
        dry_run=True,
        puppet_ids=[player_id],
        run_id=run_id,
        transcript_dir=str(tmp_path),
    )
    pol = RecordingPolicy(
        {"summary": "no new task", "transcript": {"final_summary": "TACTICAL: none"}},
        options=opts,
    )
    conn = FakeConn()
    conn._polls = iter([
        ["LOCAL|0", "TURN|1", "ACTIVE|false", "LAST|nil"],
        [f"LOCAL|{player_id}", "TURN|9", "ACTIVE|true", "LAST|1"],
    ])
    gs = FakeGSWithUnit(unit_id=65537, unit_index=1, x=1, y=1)

    await run_arena(conn, gs, cfg, policy=pol)

    saved = task_path(str(tmp_path), run_id, player_id).read_text()
    assert '"updated_turn": 9' in saved


@pytest.mark.asyncio
async def test_exclusive_cli_policy_still_receives_memory_and_task_blocks(tmp_path):
    """A CLI-style policy (needs_exclusive_tuner=True) still gets memory_block and
    task_block populated from disk, even though the tuner connection is released
    before the policy call -- proving load happens before the exclusive disconnect."""
    run_id, player_id = "clitest", 7
    from civ_mcp.arena.memory import save_memory
    save_memory(str(tmp_path), run_id, player_id, turn=1, text="scout north next.",
                max_chars=1200)

    opts = CivOptions(memory=MemoryOptions(enabled=True, max_chars=1200))
    cfg = ArenaConfig(players=[PlayerSpec(player_id, "cli-claude", "")], max_puppet_turns=1,
                      puppet_ids=[player_id], run_id=run_id, transcript_dir=str(tmp_path))
    pol = RecordingPolicy({"summary": "cli ran"}, options=opts, needs_exclusive_tuner=True)

    conn = FakeConn()
    conn._polls = iter([
        [f"LOCAL|{player_id}", "TURN|2", "ACTIVE|true", "LAST|1"],
    ])
    result = await run_arena(conn, FakeGS(), cfg, policy=pol)

    assert result["puppet_turns_played"] == 1
    assert conn.restored is True  # reconnect + handback still happened
    assert "scout north next." in pol.calls[0]["memory_block"]
    assert pol.calls[0]["memory_block"].startswith("== STANDING PLAN (captured turn 1, 1 turn old) ==")


@pytest.mark.asyncio
async def test_exclusive_cli_briefing_built_before_disconnect(monkeypatch):
    from civ_mcp.arena.briefing import Briefing
    import civ_mcp.arena.coordinator as coord_mod

    built_connected = []

    async def fake_build_briefing(gs, opts, budget):
        built_connected.append(conn.is_connected)
        return Briefing(text="PREBUILT BRIEFING", tokens=4, sections=["overview"])

    class ExclusiveBriefingPolicy(RecordingPolicy):
        needs_exclusive_tuner = True

        async def __call__(self, gs, player_id, turn, **kwargs):
            assert conn.is_connected is False
            assert kwargs["briefing"].text == "PREBUILT BRIEFING"
            return await super().__call__(gs, player_id, turn, **kwargs)

    monkeypatch.setattr(
        "civ_mcp.arena.prompt_context.build_briefing",
        fake_build_briefing,
    )
    conn = FakeConn()
    gs = FakeGS()
    opts = CivOptions(briefing=BriefingOptions(enabled=True))
    cfg = ArenaConfig(
        players=[PlayerSpec(7, "cli-claude", "")],
        max_puppet_turns=1,
        puppet_ids=[7],
    )
    conn._polls = iter([[ "LOCAL|7", "TURN|2", "ACTIVE|true", "LAST|1" ]])
    pol = ExclusiveBriefingPolicy({"summary": "cli ran"}, options=opts, needs_exclusive_tuner=True)

    result = await run_arena(conn, gs, cfg, policy=pol)

    assert result["puppet_turns_played"] == 1
    assert built_connected == [True]


@pytest.mark.asyncio
async def test_briefing_build_failure_does_not_abort_arena_turn(monkeypatch):
    """A briefing-build raise (e.g. a missing playbook file) must degrade this
    civ to no briefing, not abort the whole multi-civ run -- mirroring the
    memory/task-tracker load guards."""
    from civ_mcp.arena import coordinator

    async def boom(*args, **kwargs):
        raise RuntimeError("playbook missing")

    monkeypatch.setattr(coordinator, "maybe_build_briefing", boom)

    seen = {}

    class ExclusiveBriefingPolicy:
        needs_exclusive_tuner = True
        options = CivOptions(briefing=BriefingOptions(enabled=True))
        provider = "cli-claude"

        async def __call__(self, gs, player_id, turn, *, briefing=None):
            seen["briefing"] = briefing
            return {"summary": "ran"}

    conn = FakeConn()
    conn._polls = iter([["LOCAL|7", "TURN|2", "ACTIVE|true", "LAST|1"]])
    cfg = ArenaConfig(
        players=[PlayerSpec(7, "cli-claude", "")],
        max_puppet_turns=1,
        puppet_ids=[7],
    )
    pol = ExclusiveBriefingPolicy()

    result = await run_arena(conn, FakeGS(), cfg, policy=pol)

    assert result["puppet_turns_played"] == 1   # run survived the briefing failure
    assert seen["briefing"] is None              # degraded to no briefing


@pytest.mark.asyncio
async def test_exclusive_policy_without_briefing_kwarg_runs_with_briefing_enabled():
    class NarrowExclusivePolicy:
        needs_exclusive_tuner = True
        options = CivOptions(briefing=BriefingOptions(enabled=True))

        def __init__(self):
            self.calls = []

        async def __call__(
            self,
            gs,
            player_id,
            turn,
            *,
            memory_block="",
            task_block="",
        ):
            self.calls.append(
                {
                    "player_id": player_id,
                    "turn": turn,
                    "memory_block": memory_block,
                    "task_block": task_block,
                }
            )
            return {"summary": "narrow exclusive policy ran", "actions": []}

    conn = FakeConn()
    gs = FakeGS()
    cfg = ArenaConfig(
        players=[PlayerSpec(7, "cli-claude", "")],
        max_puppet_turns=1,
        puppet_ids=[7],
    )
    conn._polls = iter([[ "LOCAL|7", "TURN|2", "ACTIVE|true", "LAST|1" ]])
    pol = NarrowExclusivePolicy()

    result = await run_arena(conn, gs, cfg, policy=pol)

    assert result["puppet_turns_played"] == 1
    assert pol.calls == [
        {"player_id": 7, "turn": 2, "memory_block": "", "task_block": ""}
    ]


@pytest.mark.asyncio
async def test_nonexclusive_policy_without_briefing_kwarg_runs():
    class NarrowPolicy:
        options = CivOptions()

        def __init__(self):
            self.calls = []

        async def __call__(
            self,
            gs,
            player_id,
            turn,
            *,
            memory_block="",
            task_block="",
        ):
            self.calls.append(
                {
                    "player_id": player_id,
                    "turn": turn,
                    "memory_block": memory_block,
                    "task_block": task_block,
                }
            )
            return {"summary": "narrow policy ran", "actions": []}

    conn = FakeConn()
    gs = FakeGS()
    cfg = ArenaConfig(
        players=[PlayerSpec(7, "local", "")],
        max_puppet_turns=1,
        puppet_ids=[7],
    )
    conn._polls = iter([[ "LOCAL|7", "TURN|2", "ACTIVE|true", "LAST|1" ]])
    pol = NarrowPolicy()

    result = await run_arena(conn, gs, cfg, policy=pol)

    assert result["puppet_turns_played"] == 1
    assert pol.calls == [
        {"player_id": 7, "turn": 2, "memory_block": "", "task_block": ""}
    ]


@pytest.mark.asyncio
async def test_task_tracker_only_uses_task_capture_budget_not_memory_default(tmp_path):
    opts = CivOptions(task_tracker=TaskTrackerOptions(enabled=True, max_tasks=8))
    run_id, player_id = "task-capture-budget", 8
    long_plan = (
        "STANDING PLAN:\n"
        + ("- filler line to push task below memory default\n" * 80)
        + "TASK settle unit_id=42 target=10,12\n"
    )
    cfg = ArenaConfig(
        players=[PlayerSpec(player_id, "local", "m")],
        max_puppet_turns=1,
        dry_run=True,
        puppet_ids=[player_id],
        run_id=run_id,
        transcript_dir=str(tmp_path),
    )
    pol = RecordingPolicy(
        {"summary": "ignored", "transcript": {"final_summary": long_plan}},
        options=opts,
    )
    conn = FakeConn()
    conn._polls = iter([[f"LOCAL|{player_id}", "TURN|2", "ACTIVE|true", "LAST|1"]])

    await run_arena(conn, FakeGS(), cfg, policy=pol)

    path = task_path(str(tmp_path), run_id, player_id)
    assert '"unit_id": 42' in path.read_text()


@pytest.mark.asyncio
async def test_memory_save_failure_does_not_abort_arena_turn(monkeypatch, tmp_path):
    from civ_mcp.arena import coordinator

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(coordinator, "save_memory", boom)
    opts = CivOptions(memory=MemoryOptions(enabled=True, max_chars=1200))
    cfg = ArenaConfig(
        players=[PlayerSpec(4, "local", "m")],
        max_puppet_turns=1,
        dry_run=True,
        puppet_ids=[4],
        run_id="mem-save-failure",
        transcript_dir=str(tmp_path),
    )
    pol = RecordingPolicy(
        {
            "summary": "ignored",
            "transcript": {"final_summary": "STANDING PLAN:\n- keep exploring\n"},
        },
        options=opts,
    )
    conn = FakeConn()
    conn._polls = iter([
        ["LOCAL|0", "TURN|1", "ACTIVE|false", "LAST|nil"],
        ["LOCAL|4", "TURN|2", "ACTIVE|true", "LAST|1"],
    ])
    sink = FakeSink()

    result = await run_arena(conn, FakeGS(), cfg, policy=pol, transcript=sink)

    assert result["puppet_turns_played"] == 1
    assert result["log"][0]["standing_memory"]["error"] == "OSError('disk full')"
    assert sink.records[0]["standing_memory"]["error"] == "OSError('disk full')"


@pytest.mark.asyncio
async def test_task_state_save_failure_does_not_abort_arena_turn(monkeypatch, tmp_path):
    from civ_mcp.arena import coordinator

    def boom(*args, **kwargs):
        raise OSError("read only")

    monkeypatch.setattr(coordinator, "save_task_state", boom)
    opts = CivOptions(task_tracker=TaskTrackerOptions(enabled=True, max_tasks=8))
    cfg = ArenaConfig(
        players=[PlayerSpec(5, "local", "m")],
        max_puppet_turns=1,
        dry_run=True,
        puppet_ids=[5],
        run_id="task-save-failure",
        transcript_dir=str(tmp_path),
    )
    pol = RecordingPolicy(
        {
            "summary": "ignored",
            "transcript": {
                "final_summary": "STANDING PLAN:\nTASK settle unit_id=42 target=10,12\n"
            },
        },
        options=opts,
    )
    conn = FakeConn()
    conn._polls = iter([
        ["LOCAL|0", "TURN|1", "ACTIVE|false", "LAST|nil"],
        ["LOCAL|5", "TURN|2", "ACTIVE|true", "LAST|1"],
    ])
    sink = FakeSink()

    result = await run_arena(conn, FakeGS(), cfg, policy=pol, transcript=sink)

    assert result["puppet_turns_played"] == 1
    assert result["log"][0]["task_tracker"]["error"] == "OSError('read only')"
    assert sink.records[0]["task_tracker"]["error"] == "OSError('read only')"


@pytest.mark.asyncio
async def test_pre_model_save_failure_does_not_drop_turn_task_capture(monkeypatch, tmp_path):
    """A transient pre-model save failure must not discard the TASK lines the
    model emits this turn: post-turn capture still parses and persists them."""
    from civ_mcp.arena import coordinator

    real_save = coordinator.save_task_state
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk full")
        return real_save(*args, **kwargs)

    monkeypatch.setattr(coordinator, "save_task_state", flaky)
    opts = CivOptions(task_tracker=TaskTrackerOptions(enabled=True, max_tasks=8))
    cfg = ArenaConfig(
        players=[PlayerSpec(5, "local", "m")],
        max_puppet_turns=1,
        dry_run=True,
        puppet_ids=[5],
        run_id="task-flaky-save",
        transcript_dir=str(tmp_path),
    )
    pol = RecordingPolicy(
        {
            "summary": "ignored",
            "transcript": {
                "final_summary": "STANDING PLAN:\nTASK settle unit_id=42 target=10,12\n"
            },
        },
        options=opts,
    )
    conn = FakeConn()
    conn._polls = iter([
        ["LOCAL|0", "TURN|1", "ACTIVE|false", "LAST|nil"],
        ["LOCAL|5", "TURN|2", "ACTIVE|true", "LAST|1"],
    ])

    result = await run_arena(conn, FakeGS(), cfg, policy=pol)

    path = task_path(str(tmp_path), "task-flaky-save", 5)
    assert path.exists()
    assert '"unit_id": 42' in path.read_text()
    # the transient pre-model error is still surfaced
    assert result["log"][0]["task_tracker"]["error"] == "OSError('disk full')"


@pytest.mark.asyncio
async def test_exclusive_cli_briefing_prebuild_uses_explicit_context_budget(monkeypatch):
    from civ_mcp.arena import coordinator
    from civ_mcp.arena.briefing import Briefing

    captured = {}

    async def fake_briefing(gs, options, *, n_ctx, playbook_chars, tool_schema_chars, supplied=None):
        captured["n_ctx"] = n_ctx
        return Briefing(text="PREBUILT", tokens=1, sections=["overview"])

    monkeypatch.setattr(coordinator, "maybe_build_briefing", fake_briefing)
    opts = CivOptions(context_budget=8192, briefing=BriefingOptions(enabled=True))
    cfg = ArenaConfig(
        players=[PlayerSpec(7, "cli-claude", "")],
        max_puppet_turns=1,
        puppet_ids=[7],
    )
    conn = FakeConn()
    conn._polls = iter([["LOCAL|7", "TURN|2", "ACTIVE|true", "LAST|1"]])
    pol = RecordingPolicy({"summary": "cli ran"}, options=opts, needs_exclusive_tuner=True)

    result = await run_arena(conn, FakeGS(), cfg, policy=pol)

    assert result["puppet_turns_played"] == 1
    assert captured["n_ctx"] == 8192


@pytest.mark.asyncio
async def test_old_signature_policy_without_kwargs_still_runs(tmp_path):
    """A pre-slice-3 policy whose __call__ is (gs, player_id, turn) must not be
    passed memory_block/task_block kwargs it cannot accept."""
    calls = []

    class OldStylePolicy:
        provider = "local"
        options = CivOptions()

        async def __call__(self, gs, player_id, turn):
            calls.append((player_id, turn))
            return {"summary": "old-style", "actions": []}

    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m")], max_puppet_turns=1,
                      dry_run=True, puppet_ids=[1], run_id="oldsig",
                      transcript_dir=str(tmp_path))

    result = await run_arena(FakeConn(), FakeGS(), cfg, policy=OldStylePolicy())

    assert result["puppet_turns_played"] == 1
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_empty_run_id_does_not_share_memory_across_runs(tmp_path):
    """run_id='' must not collapse the memory dir onto transcript_dir, where a
    later unrelated run would inherit this run's standing plan."""
    opts = CivOptions(memory=MemoryOptions(enabled=True, max_chars=1200))
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m")], max_puppet_turns=1,
                      dry_run=True, puppet_ids=[1], run_id="",
                      transcript_dir=str(tmp_path))

    pol1 = RecordingPolicy({
        "summary": "did stuff",
        "transcript": {"final_summary": (
            "TACTICAL: did stuff.\nSTANDING PLAN:\n- march settler to (18,24)\n"
        )},
    }, options=opts)
    await run_arena(FakeConn(), FakeGS(), cfg, policy=pol1)

    assert not (tmp_path / "memory").exists()

    pol2 = RecordingPolicy({"summary": "no plan"}, options=opts)
    await run_arena(FakeConn(), FakeGS(), cfg, policy=pol2)

    assert pol2.calls[0]["memory_block"] == ""


def test_policy_accepts_kwarg_handles_bare_function_signature():
    """A plain-function policy's `.__call__` is a method-wrapper reporting
    (*args, **kwargs); introspecting it would spuriously accept every kwarg and
    then raise TypeError at the call site. Introspecting the callable itself
    must report the real signature."""
    from civ_mcp.arena.coordinator import _policy_accepts_kwarg

    async def bare(gs, player_id, turn):
        return {"summary": ""}

    async def flexible(gs, player_id, turn, **kwargs):
        return {"summary": ""}

    assert _policy_accepts_kwarg(bare, "memory_block") is False
    assert _policy_accepts_kwarg(bare, "briefing") is False
    assert _policy_accepts_kwarg(flexible, "memory_block") is True

    class Explicit:
        async def __call__(self, gs, player_id, turn, memory_block=""):
            return {"summary": ""}

    assert _policy_accepts_kwarg(Explicit(), "memory_block") is True
    assert _policy_accepts_kwarg(Explicit(), "task_block") is False


@pytest.mark.asyncio
async def test_tracker_only_capture_not_reported_as_memory_captured(tmp_path):
    """With memory disabled the extracted plan is never saved or injectable;
    captured_chars must read 0 or analyze counts the tracker-only civ as a
    standing-memory-captured turn."""
    opts = CivOptions(task_tracker=TaskTrackerOptions(enabled=True, max_tasks=8))
    run_id, player_id = "tracker-only-captured", 8
    cfg = ArenaConfig(
        players=[PlayerSpec(player_id, "local", "m")],
        max_puppet_turns=1,
        dry_run=True,
        puppet_ids=[player_id],
        run_id=run_id,
        transcript_dir=str(tmp_path),
    )
    pol = RecordingPolicy(
        {
            "summary": "ignored",
            "transcript": {
                "final_summary": (
                    "STANDING PLAN:\n- keep going\n"
                    "TASK settle unit_id=42 target=10,12\n"
                )
            },
        },
        options=opts,
    )
    conn = FakeConn()
    conn._polls = iter([[f"LOCAL|{player_id}", "TURN|2", "ACTIVE|true", "LAST|1"]])
    sink = FakeSink()

    result = await run_arena(conn, FakeGS(), cfg, policy=pol, transcript=sink)

    # Task capture itself still worked...
    assert '"unit_id": 42' in task_path(str(tmp_path), run_id, player_id).read_text()
    # ...but nothing reads as a standing-memory capture.
    assert result["log"][0]["standing_memory"]["captured_chars"] == 0
    assert sink.records[0]["standing_memory"]["captured_chars"] == 0


@pytest.mark.asyncio
async def test_policy_failure_is_skipped_not_crashed_and_restores_human():
    """A puppet LLM turn whose policy raises -- e.g. the llama.cpp gateway returns
    HTTP 500 on a malformed/truncated tool call (openai.InternalServerError) -- must
    NOT crash the whole run. The coordinator logs it, hands the seat back to the human
    (finish_units + restore_local(0)), consumes the puppet-turn budget, and continues.
    This mirrors the sweep/memory/task/briefing degrade-not-abort guards.

    Goes RED under the unguarded `result = await pol(...)`: the exception propagates
    out of run_arena and kills the watcher mid-round (leaving the human stuck on the
    puppet seat), which is exactly the live-run crash this guards against."""
    class BoomPolicy:
        provider = "local"
        options = CivOptions()

        def __init__(self):
            self.calls = 0

        async def __call__(self, gs, player_id, turn, **kwargs):
            self.calls += 1
            raise RuntimeError(
                "Error code: 500 - Failed to parse tool call arguments as JSON"
            )

    conn, gs = FakeConn(), FakeGS()
    # Two active puppet polls but budget of 1: a failed turn that correctly consumes
    # the budget yields exactly ONE attempt. A guard that forgot to decrement would
    # re-enter and call the policy a second time (or idle-loop) -- pol.calls pins it.
    conn._polls = iter([
        ["LOCAL|1", "TURN|2", "ACTIVE|true", "LAST|1"],
        ["LOCAL|1", "TURN|3", "ACTIVE|true", "LAST|1"],
    ])
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m")], max_puppet_turns=1,
                      puppet_ids=[1])
    pol = BoomPolicy()

    result = await run_arena(conn, gs, cfg, policy=pol)   # must NOT raise

    assert pol.calls == 1              # budget consumed: one attempt, no loop
    assert conn.restored is True       # human handed back despite the failure
    # The failure is surfaced in the log rather than silently swallowed.
    assert any(entry.get("skipped") for entry in result["log"])


# ---------------------------------------------------------------------------
# Task 10 — attention & turn-skipping coordinator integration
# ---------------------------------------------------------------------------

QUIET_SCAN_LINES = [
    "ATTN|THREAT|count=0|nearest=", "ATTN|CITYHP|damaged=", "ATTN|WAR|with=",
    "ATTN|LOYALTY|negative=", "ATTN|WC|turns=5", "ATTN|ERA|index=1",
    "ATTN|POP|total=12", "ATTN|GP|available=0", "ATTN|TRADE|idle=0",
    "ATTN|DIPLO|pending=0", "ATTN|BLOCKERS|types=",
]

# Overview snapshot matching FakeConnWithOverview's canned _OV_BEFORE line
# (score=50, gold=100.0, science=10.0, culture=8.0, faith=20.0, cities=2,
# units=5 -- see overview.py:584-598 field order). Seeding an attention
# baseline with THESE values (rather than zeros) means the freshly-read
# current-turn snapshot equals the seeded baseline, so no hard
# "CITY_COUNT_CHANGED"/"UNITS_LOST" trigger fires from a fixture mismatch
# that has nothing to do with the scenario under test.
_ATTN_BASELINE_SNAPSHOT = {
    "score": 50, "gold": 100.0, "science": 10.0, "culture": 8.0,
    "faith": 20.0, "research": "Mining", "civic": "Drama",
    "cities": 2, "units": 5,
}


class AttnConn(FakeConnWithOverview):
    async def execute_read(self, lua, timeout=5.0):
        if "ATTN" in lua:
            return list(QUIET_SCAN_LINES)
        return await super().execute_read(lua, timeout)


class CountingPolicy:
    def __init__(self, options):
        self.options = options
        self.calls = 0
    async def __call__(self, gs, player_id, turn, *, digest_block="", **kw):
        self.calls += 1
        self.last_digest = digest_block
        return {"summary": "played", "actions": [], "transcript": {"steps": [], "final_summary": "played"}}


@pytest.mark.asyncio
async def test_auto_mode_sleeps_quiet_turn(tmp_path):
    from civ_mcp.arena.attention import AttentionState, save_attention_state
    from civ_mcp.arena.config import AttentionOptions
    conn = AttnConn(); gs = FakeGSWithConn(conn); sink = FakeSink()
    opts = CivOptions(attention=AttentionOptions(mode="auto"))
    pol = CountingPolicy(opts)
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m", options=opts)],
                      max_puppet_turns=3, idle_poll_limit=5,
                      transcript_dir=str(tmp_path), run_id="r1", puppet_ids=[1])
    # seed a baseline so NO_BASELINE doesn't force a wake on the first capture
    # (matching FakeConnWithOverview's _OV_BEFORE fields -- see
    # _ATTN_BASELINE_SNAPSHOT above -- so the freshly-read current-turn
    # snapshot exactly matches it and no hard trigger fires from fixture
    # mismatch alone)
    save_attention_state(str(tmp_path), "r1", 1, AttentionState(
        run_id="r1", player_id=1,
        last_snapshot=dict(_ATTN_BASELINE_SNAPSHOT),
        last_scan={"at_war_with": [], "era_index": 1, "total_population": 12}))
    result = await run_arena(conn, gs, cfg, policy=pol, transcript=sink)
    assert pol.calls == 0                       # no model invocation
    assert result["turns_slept"] == 1
    assert result["puppet_turns_played"] == 0   # max_puppet_turns NOT consumed
    rec = sink.records[-1]
    assert rec["slept"] is True and rec["turn_kind"] == "slept"
    assert rec["step_count"] == 0 and rec["usd"] == 0.0
    assert "skipped" not in rec                 # that key means FAILED
    assert conn.restored                        # handback still happened


@pytest.mark.asyncio
async def test_first_capture_wakes_no_baseline(tmp_path, monkeypatch):
    """No seeded attention state -> load_attention_state returns a fresh state
    whose last_snapshot is None -> evaluate() returns NO_BASELINE -> the model
    runs this turn (a played turn), and the transcript record is annotated
    with the wake cause."""
    from civ_mcp.arena.config import AttentionOptions

    async def noop(_delay): pass
    monkeypatch.setattr(asyncio, "sleep", noop)

    conn = AttnConn(); gs = FakeGSWithConn(conn); sink = FakeSink()
    opts = CivOptions(attention=AttentionOptions(mode="auto"))
    pol = CountingPolicy(opts)
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m", options=opts)],
                      max_puppet_turns=1, idle_poll_limit=5,
                      transcript_dir=str(tmp_path), run_id="r2", puppet_ids=[1])

    result = await run_arena(conn, gs, cfg, policy=pol, transcript=sink)

    assert pol.calls == 1
    assert result["puppet_turns_played"] == 1
    assert result["turns_slept"] == 0
    rec = sink.records[-1]
    assert rec["turn_kind"] == "played" and rec["attention"]["wake_cause"] == "NO_BASELINE"
    assert rec["attention"]["decision"] == "woke"


@pytest.mark.asyncio
async def test_off_mode_bit_for_bit_today(tmp_path, monkeypatch):
    """mode="off" (the default): no ATTN read is ever issued and no "attention"
    key appears on the record -- except "turn_kind" IS added to played records
    unconditionally, regardless of attention mode."""
    async def noop(_delay): pass
    monkeypatch.setattr(asyncio, "sleep", noop)

    conn = AttnConn(); gs = FakeGSWithConn(conn); sink = FakeSink()
    opts = CivOptions()  # attention.mode == "off" by default
    pol = CountingPolicy(opts)
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m", options=opts)],
                      max_puppet_turns=1, idle_poll_limit=5,
                      transcript_dir=str(tmp_path), run_id="r3", puppet_ids=[1])

    result = await run_arena(conn, gs, cfg, policy=pol, transcript=sink)

    assert pol.calls == 1
    assert result["puppet_turns_played"] == 1
    assert result["turns_slept"] == 0
    assert not any("ATTN" in c for c in conn.read_calls)   # scan never issued
    rec = sink.records[-1]
    assert rec["turn_kind"] == "played"     # added unconditionally
    assert "attention" not in rec           # off mode never produces this


@pytest.mark.asyncio
async def test_wake_digest_injected_after_sleep(tmp_path):
    """Two captured turns: the first sleeps (quiet scan); by the second, the
    streak (1, from the first sleep) has reached max_streak=1 -> STREAK_CAP
    wake. The wake digest rendered from the accumulated sleep record must
    reach the policy as digest_block, and the record must show a wake."""
    from civ_mcp.arena.attention import AttentionState, save_attention_state
    from civ_mcp.arena.config import AttentionOptions

    conn = AttnConn(); gs = FakeGSWithConn(conn); sink = FakeSink()
    opts = CivOptions(attention=AttentionOptions(mode="auto", max_streak=1))
    pol = CountingPolicy(opts)
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m", options=opts)],
                      max_puppet_turns=1, idle_poll_limit=5,
                      transcript_dir=str(tmp_path), run_id="r4", puppet_ids=[1])
    # Two consecutive active puppet polls, no idle detour needed: turn 2
    # sleeps (quiet scan matches the seeded baseline); turn 3's streak (1,
    # set by turn 2's sleep) meets max_streak (1) -> STREAK_CAP wake, and
    # remaining (1) is consumed so the loop stops there.
    conn._polls = iter([
        ["LOCAL|1", "TURN|2", "ACTIVE|true", "LAST|1"],
        ["LOCAL|1", "TURN|3", "ACTIVE|true", "LAST|1"],
    ])
    save_attention_state(str(tmp_path), "r4", 1, AttentionState(
        run_id="r4", player_id=1,
        last_snapshot=dict(_ATTN_BASELINE_SNAPSHOT),
        last_scan={"at_war_with": [], "era_index": 1, "total_population": 12}))

    result = await run_arena(conn, gs, cfg, policy=pol, transcript=sink)

    assert result["turns_slept"] == 1
    assert result["puppet_turns_played"] == 1
    assert pol.calls == 1
    assert "WHILE YOU SLEPT" in pol.last_digest
    rec = sink.records[-1]
    assert rec["turn_kind"] == "played"
    assert rec["attention"]["decision"] == "woke"
    assert rec["attention"]["wake_cause"] == "STREAK_CAP"


@pytest.mark.asyncio
async def test_directive_captured_without_memory(tmp_path, monkeypatch):
    """mode="model", memory+tracker disabled (CivOptions defaults): the
    model's final_summary carries a SKIP directive. With no seeded baseline
    the first capture wakes on NO_BASELINE regardless of mode, so the model
    runs; its directive is still parsed and persisted post-turn, and no
    standing-memory file is ever created since memory is disabled."""
    from civ_mcp.arena.attention import load_attention_state
    from civ_mcp.arena.config import AttentionOptions
    from civ_mcp.arena.memory import memory_path

    async def noop(_delay): pass
    monkeypatch.setattr(asyncio, "sleep", noop)

    opts = CivOptions(attention=AttentionOptions(mode="model"))

    class DirectivePolicy:
        provider = "local"
        options = opts
        async def __call__(self, gs, player_id, turn, **kwargs):
            return {
                "summary": "done",
                "transcript": {"steps": [], "final_summary": "done\nSKIP: 3"},
            }

    conn = AttnConn(); gs = FakeGSWithConn(conn)
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m", options=opts)],
                      max_puppet_turns=1, idle_poll_limit=5,
                      transcript_dir=str(tmp_path), run_id="r5", puppet_ids=[1])

    result = await run_arena(conn, gs, cfg, policy=DirectivePolicy())

    assert result["puppet_turns_played"] == 1
    state = load_attention_state(str(tmp_path), "r5", 1)
    assert state.skips_remaining == 3
    assert not memory_path(str(tmp_path), "r5", 1).exists()


@pytest.mark.asyncio
async def test_max_game_turns_caps_run(tmp_path, monkeypatch):
    """max_game_turns=1 caps the run after exactly one slept turn even though
    max_puppet_turns=5 remains almost entirely unused -- game_turns counts
    ALL captured turns (played + slept + failed), not just played ones."""
    from civ_mcp.arena.attention import AttentionState, save_attention_state
    from civ_mcp.arena.config import AttentionOptions

    async def noop(_delay): pass
    monkeypatch.setattr(asyncio, "sleep", noop)

    conn = AttnConn(); gs = FakeGSWithConn(conn); sink = FakeSink()
    opts = CivOptions(attention=AttentionOptions(mode="auto"))
    pol = CountingPolicy(opts)
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m", options=opts)],
                      max_puppet_turns=5, max_game_turns=1, idle_poll_limit=10,
                      transcript_dir=str(tmp_path), run_id="r6", puppet_ids=[1])
    save_attention_state(str(tmp_path), "r6", 1, AttentionState(
        run_id="r6", player_id=1,
        last_snapshot=dict(_ATTN_BASELINE_SNAPSHOT),
        last_scan={"at_war_with": [], "era_index": 1, "total_population": 12}))

    result = await run_arena(conn, gs, cfg, policy=pol, transcript=sink)

    assert pol.calls == 0
    assert result["turns_slept"] == 1
    assert result["puppet_turns_played"] == 0


@pytest.mark.asyncio
async def test_attention_load_crash_degrades_to_wake(tmp_path, monkeypatch):
    """load_attention_state raising must NOT abort the run: the coordinator
    degrades to a fresh in-memory state (== what load returns on any failure)
    with NO second disk read, evaluate() fails open to a wake, and the model
    runs this turn. Goes RED under the unguarded fallback reload, whose
    second load_attention_state call re-raises out of run_arena."""
    from civ_mcp.arena.config import AttentionOptions
    import civ_mcp.arena.coordinator as coord_mod

    async def noop(_delay): pass
    monkeypatch.setattr(asyncio, "sleep", noop)

    def boom(*args, **kwargs):
        raise RuntimeError("attention state dir unreadable")

    monkeypatch.setattr(coord_mod, "load_attention_state", boom)

    conn = AttnConn(); gs = FakeGSWithConn(conn); sink = FakeSink()
    opts = CivOptions(attention=AttentionOptions(mode="auto"))
    pol = CountingPolicy(opts)
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m", options=opts)],
                      max_puppet_turns=1, idle_poll_limit=5,
                      transcript_dir=str(tmp_path), run_id="r7", puppet_ids=[1])

    result = await run_arena(conn, gs, cfg, policy=pol, transcript=sink)  # must NOT raise

    assert pol.calls == 1                      # fail-open: woke, model turn
    assert result["turns_slept"] == 0
    assert result["puppet_turns_played"] == 1
    assert conn.restored


@pytest.mark.asyncio
async def test_partial_snapshot_state_delta_none(tmp_path):
    """A persisted last_snapshot missing one numeric key ("units") must not
    crash the slept-turn transcript write: the KeyError degrades to
    state_delta=None (the record's documented "unknown delta" value).

    The seeding still sleeps honestly: _hard_triggers uses .get() with
    defaults, so prev missing "units" reads as 0 and snapshot.units(5) < 0
    is False (no UNITS_LOST); cities match at 2 (no CITY_COUNT_CHANGED);
    gold equal (no GOLD_CRASH); the quiet scan matches the seeded scan
    scalars. Only the delta arithmetic touches the missing key."""
    from civ_mcp.arena.attention import AttentionState, save_attention_state
    from civ_mcp.arena.config import AttentionOptions

    conn = AttnConn(); gs = FakeGSWithConn(conn); sink = FakeSink()
    opts = CivOptions(attention=AttentionOptions(mode="auto"))
    pol = CountingPolicy(opts)
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m", options=opts)],
                      max_puppet_turns=3, idle_poll_limit=5,
                      transcript_dir=str(tmp_path), run_id="r8", puppet_ids=[1])
    partial = dict(_ATTN_BASELINE_SNAPSHOT)
    del partial["units"]                       # dict-shaped but key-incomplete
    save_attention_state(str(tmp_path), "r8", 1, AttentionState(
        run_id="r8", player_id=1,
        last_snapshot=partial,
        last_scan={"at_war_with": [], "era_index": 1, "total_population": 12}))

    result = await run_arena(conn, gs, cfg, policy=pol, transcript=sink)  # must NOT raise

    assert pol.calls == 0                      # the sleep genuinely happened
    assert result["turns_slept"] == 1
    rec = sink.records[-1]
    assert rec["turn_kind"] == "slept"
    assert rec["state_delta"] is None          # unknown delta, degrade not abort
    assert conn.restored


class RaisingPolicy:
    def __init__(self, options):
        self.options = options
    async def __call__(self, gs, player_id, turn, **kw):
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_failed_wake_cancels_directive_remainder(tmp_path, monkeypatch):
    """Final-review Important 2: a wake decision whose policy call fails never
    reaches note_wake -- the failed-turn branch must still cancel the directive
    remainder (spec section 3: ANY wake cancels), or the seat resumes a stale
    sleep on the next captured turn. The slept accumulator survives so the
    digest reaches the eventual successful wake."""
    from civ_mcp.arena.attention import (
        AttentionState,
        load_attention_state,
        save_attention_state,
    )
    from civ_mcp.arena.config import AttentionOptions

    async def noop(_delay): pass
    monkeypatch.setattr(asyncio, "sleep", noop)

    conn = AttnConn(); gs = FakeGSWithConn(conn); sink = FakeSink()
    opts = CivOptions(attention=AttentionOptions(mode="model"))
    pol = RaisingPolicy(opts)
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m", options=opts)],
                      max_puppet_turns=1, idle_poll_limit=5,
                      transcript_dir=str(tmp_path), run_id="r7", puppet_ids=[1])
    slept_record = {"turn": 1, "snapshot": dict(_ATTN_BASELINE_SNAPSHOT),
                    "task_notes": [], "notifications": []}
    save_attention_state(str(tmp_path), "r7", 1, AttentionState(
        run_id="r7", player_id=1,
        directive={"skip": 3, "wake_if": []},
        skips_remaining=2, streak=1,
        last_snapshot=None,   # no baseline -> NO_BASELINE wake regardless of directive
        last_scan=None,
        slept=[slept_record]))

    result = await run_arena(conn, gs, cfg, policy=pol, transcript=sink)

    assert result["puppet_turns_played"] == 0
    assert any(entry.get("skipped") for entry in result["log"])   # the failed turn
    st = load_attention_state(str(tmp_path), "r7", 1)
    assert st.skips_remaining == 0            # stale sleep cancelled
    assert len(st.slept) == 1                 # digest accumulator survives
    assert st.streak == 1                     # streak keeps bounding model-free turns


@pytest.mark.asyncio
async def test_tampered_slept_record_costs_digest_not_run(tmp_path):
    """Final-review pinhole: a slept record missing "turn" (external tampering
    -- load validates list-of-dicts, not record internals) must degrade to an
    empty digest on the wake turn, never abort run_arena."""
    from civ_mcp.arena.attention import AttentionState, save_attention_state
    from civ_mcp.arena.config import AttentionOptions

    conn = AttnConn(); gs = FakeGSWithConn(conn); sink = FakeSink()
    opts = CivOptions(attention=AttentionOptions(mode="auto", max_streak=1))
    pol = CountingPolicy(opts)
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m", options=opts)],
                      max_puppet_turns=1, idle_poll_limit=5,
                      transcript_dir=str(tmp_path), run_id="r8", puppet_ids=[1])
    save_attention_state(str(tmp_path), "r8", 1, AttentionState(
        run_id="r8", player_id=1,
        streak=1,  # meets max_streak=1 -> STREAK_CAP wake with slept populated
        last_snapshot=dict(_ATTN_BASELINE_SNAPSHOT),
        last_scan={"at_war_with": [], "era_index": 1, "total_population": 12},
        slept=[{"snapshot": dict(_ATTN_BASELINE_SNAPSHOT),
                "task_notes": [], "notifications": []}]))  # no "turn" key

    result = await run_arena(conn, gs, cfg, policy=pol, transcript=sink)

    assert pol.calls == 1                     # the wake still happened
    assert pol.last_digest == ""              # digest lost, run intact
    assert result["puppet_turns_played"] == 1
    assert sink.records[-1]["turn_kind"] == "played"


@pytest.mark.asyncio
async def test_tampered_snapshot_value_degrades_slept_delta(tmp_path):
    """Final-review pinhole: a wrong-TYPED snapshot value in the state file
    ("score": "high") passes load's dict-shape check and the delta triggers
    (which only read units/cities/gold), then TypeErrors in the slept-record
    delta -- must record state_delta None, never abort."""
    from civ_mcp.arena.attention import AttentionState, save_attention_state
    from civ_mcp.arena.config import AttentionOptions

    conn = AttnConn(); gs = FakeGSWithConn(conn); sink = FakeSink()
    opts = CivOptions(attention=AttentionOptions(mode="auto"))
    pol = CountingPolicy(opts)
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m", options=opts)],
                      max_puppet_turns=3, idle_poll_limit=5,
                      transcript_dir=str(tmp_path), run_id="r9", puppet_ids=[1])
    tampered = dict(_ATTN_BASELINE_SNAPSHOT)
    tampered["score"] = "high"
    save_attention_state(str(tmp_path), "r9", 1, AttentionState(
        run_id="r9", player_id=1,
        last_snapshot=tampered,
        last_scan={"at_war_with": [], "era_index": 1, "total_population": 12}))

    result = await run_arena(conn, gs, cfg, policy=pol, transcript=sink)

    assert pol.calls == 0                     # still a quiet sleep
    assert result["turns_slept"] == 1
    rec = sink.records[-1]
    assert rec["turn_kind"] == "slept"
    assert rec["state_delta"] is None         # unknowable delta, not an abort


@pytest.mark.asyncio
async def test_corrupt_snapshot_resets_and_wakes_not_aborts(tmp_path):
    """Review-2 finding 1: a dict-shaped but wrong-typed persisted snapshot
    passes load's shape validation and used to explode inside evaluate()'s
    comparisons, killing run_arena. Contract: reset + wake (STATE_CORRUPT)."""
    from civ_mcp.arena.attention import (
        AttentionState, load_attention_state, save_attention_state,
    )
    from civ_mcp.arena.config import AttentionOptions

    conn = AttnConn(); gs = FakeGSWithConn(conn); sink = FakeSink()
    opts = CivOptions(attention=AttentionOptions(mode="auto"))
    pol = CountingPolicy(opts)
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m", options=opts)],
                      max_puppet_turns=1, idle_poll_limit=5,
                      transcript_dir=str(tmp_path), run_id="rc1", puppet_ids=[1])
    corrupt = dict(_ATTN_BASELINE_SNAPSHOT)
    corrupt["units"] = "5"          # int < str -> TypeError in _hard_triggers
    save_attention_state(str(tmp_path), "rc1", 1, AttentionState(
        run_id="rc1", player_id=1, last_snapshot=corrupt,
        last_scan={"at_war_with": [], "era_index": 1, "total_population": 12}))

    result = await run_arena(conn, gs, cfg, policy=pol, transcript=sink)

    assert pol.calls == 1                       # woke; the run did not die
    assert result["puppet_turns_played"] == 1
    rec = sink.records[-1]
    assert rec["attention"]["wake_cause"] == "STATE_CORRUPT"
    healed = load_attention_state(str(tmp_path), "rc1", 1)
    assert healed.last_snapshot is not None
    # note_wake rewrote the baseline from the post-turn overview snapshot
    # (_OV_AFTER's units field -- see FakeConnWithOverview above), not the
    # corrupt persisted "5" string -- the key assertion is that it's a real
    # int again, healing the file.
    assert healed.last_snapshot["units"] == 6


@pytest.mark.asyncio
async def test_corrupt_directive_resets_and_wakes_not_aborts(tmp_path):
    """Same contract for a corrupt directive: wake_if=5 makes the subscription
    tuple() call raise; must degrade to STATE_CORRUPT wake, not abort."""
    from civ_mcp.arena.attention import AttentionState, save_attention_state
    from civ_mcp.arena.config import AttentionOptions

    conn = AttnConn(); gs = FakeGSWithConn(conn); sink = FakeSink()
    opts = CivOptions(attention=AttentionOptions(mode="hybrid"))
    pol = CountingPolicy(opts)
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m", options=opts)],
                      max_puppet_turns=1, idle_poll_limit=5,
                      transcript_dir=str(tmp_path), run_id="rc2", puppet_ids=[1])
    save_attention_state(str(tmp_path), "rc2", 1, AttentionState(
        run_id="rc2", player_id=1,
        directive={"skip": 2, "wake_if": 5},    # dict-shaped, corrupt value
        skips_remaining=2,
        last_snapshot=dict(_ATTN_BASELINE_SNAPSHOT),
        last_scan={"at_war_with": [], "era_index": 1, "total_population": 12}))

    result = await run_arena(conn, gs, cfg, policy=pol, transcript=sink)

    assert pol.calls == 1
    assert result["puppet_turns_played"] == 1
    assert sink.records[-1]["attention"]["wake_cause"] == "STATE_CORRUPT"


@pytest.mark.asyncio
async def test_wake_baseline_is_post_play_with_transcripts_off(tmp_path, monkeypatch):
    """Review-2 finding 2: state_after was gated on _tx_on only, so with
    transcripts off note_wake stored the PRE-play snapshot as the next wake
    baseline -- the following quiet turn's hard triggers would compare
    against a state that predates the puppet's own actions."""
    from civ_mcp.arena import coordinator as coord
    from civ_mcp.arena.attention import load_attention_state
    from civ_mcp.arena.config import AttentionOptions

    calls = []
    async def fake_snapshot(_gs):
        calls.append(1)
        return {**_ATTN_BASELINE_SNAPSHOT, "units": 5 + len(calls)}
    monkeypatch.setattr(coord, "_overview_snapshot", fake_snapshot)

    conn = AttnConn(); gs = FakeGSWithConn(conn)
    opts = CivOptions(attention=AttentionOptions(mode="auto"))
    pol = CountingPolicy(opts)
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m", options=opts)],
                      max_puppet_turns=1, idle_poll_limit=5,
                      transcript_dir=str(tmp_path), run_id="rt2", puppet_ids=[1])

    # transcripts OFF; no seeded baseline -> NO_BASELINE wake
    result = await run_arena(conn, gs, cfg, policy=pol, transcript=None)

    assert result["puppet_turns_played"] == 1
    assert len(calls) == 2                       # before AND after now taken
    st = load_attention_state(str(tmp_path), "rt2", 1)
    assert st.last_snapshot["units"] == 7        # the POST-play (2nd) snapshot


@pytest.mark.asyncio
async def test_slept_turns_refill_idle_budget(tmp_path):
    """Review-2 finding 8: slept turns burned deadline_polls (never refilled)
    while leaving `remaining` untouched, so a quiet game could end far short
    of its budget. A captured turn is activity: it must refill the budget."""
    from civ_mcp.arena.attention import AttentionState, save_attention_state
    from civ_mcp.arena.config import AttentionOptions

    conn = AttnConn(); gs = FakeGSWithConn(conn); sink = FakeSink()
    opts = CivOptions(attention=AttentionOptions(mode="auto", max_streak=10))
    pol = CountingPolicy(opts)
    cfg = ArenaConfig(players=[PlayerSpec(1, "local", "m", options=opts)],
                      max_puppet_turns=1, max_game_turns=5, idle_poll_limit=3,
                      transcript_dir=str(tmp_path), run_id="r8", puppet_ids=[1])
    conn._polls = iter([
        ["LOCAL|1", f"TURN|{t}", "ACTIVE|true", "LAST|1"] for t in range(2, 9)
    ])
    save_attention_state(str(tmp_path), "r8", 1, AttentionState(
        run_id="r8", player_id=1,
        last_snapshot=dict(_ATTN_BASELINE_SNAPSHOT),
        last_scan={"at_war_with": [], "era_index": 1, "total_population": 12}))

    result = await run_arena(conn, gs, cfg, policy=pol, transcript=sink)

    # idle_poll_limit=3 < 5 slept turns: pre-fix the run died after 3 polls.
    assert result["turns_slept"] == 5     # stopped by max_game_turns, not the idle budget
    assert pol.calls == 0
