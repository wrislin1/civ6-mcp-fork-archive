import pytest
from civ_mcp.arena.coordinator import run_arena
from civ_mcp.arena.config import ArenaConfig, PlayerSpec

class FakeConn:
    def __init__(self):
        self.events = []; self.restored = False; self._connected = True
        self._polls = iter([
            ["LOCAL|2", "TURN|3", "ACTIVE|true", "LAST|2"],
        ])
    @property
    def is_connected(self): return self._connected
    async def connect(self): self._connected = True; self.events.append("connect")
    async def disconnect(self): self._connected = False; self.events.append("disconnect")
    async def execute_read(self, lua, timeout=5.0):
        if "GetCurrentGameTurn" in lua and "ACTIVE" in lua:
            try: return next(self._polls)
            except StopIteration: return ["LOCAL|0", "TURN|3", "ACTIVE|false", "LAST|2"]
        if "SetLocalPlayerAndObserver(0)" in lua:
            self.restored = True; return ["LOCAL|0"]
        if "HOOK_OK" in lua or "__pt_registered" in lua: return ["HOOK_OK|true"]
        if "DISABLED" in lua: return ["DISABLED|true"]
        if "FINISHED" in lua: return ["FINISHED|1"]
        return []
    async def execute_write(self, lua, timeout=5.0): return []

class ExclusivePolicy:
    needs_exclusive_tuner = True
    def __init__(self): self.called_with_events = None
    async def __call__(self, gs, player_id, turn, **kwargs):
        self.called_with_events = list(gs.conn.events)  # snapshot at call time
        return {"summary": "cli ran", "actions": []}

class FakeGS:
    def __init__(self, conn): self.conn = conn
    async def get_game_overview(self): return "OV"
    async def get_units(self): return []

@pytest.mark.asyncio
async def test_exclusive_policy_releases_then_reclaims_tuner():
    conn = FakeConn(); gs = FakeGS(conn); pol = ExclusivePolicy()
    cfg = ArenaConfig(players=[PlayerSpec(2, "cli-claude", "")], max_puppet_turns=1, puppet_ids=[2])
    result = await run_arena(conn, gs, cfg, policy_for=lambda pid: pol)
    assert result["puppet_turns_played"] == 1
    # disconnect happened BEFORE the policy ran; reconnect happened before restore
    assert "disconnect" in pol.called_with_events
    assert conn.events.index("disconnect") < conn.events.index("connect")
    assert conn.restored is True
