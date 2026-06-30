from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class PuppetState:
    local: int
    turn: int
    active: bool
    last: int | None

def build_inject_lua(puppet_ids: list[int]) -> str:
    entries = ", ".join(f"[{i}]=true" for i in puppet_ids)
    return f"""
__pt_enabled = true
__pt_puppets = {{ {entries} }}
if __pt_registered ~= true then
  local function grab(pid)
    __pt_last = pid
    if __pt_enabled and __pt_puppets[pid] then
      __pt_took = pcall(function() PlayerManager.SetLocalPlayerAndObserver(pid) end)
      __pt_active = true
    end
  end
  __pt_fn = function(pid) grab(pid) end
  GameEvents.PlayerTurnStartComplete.Add(__pt_fn)
  __pt_registered = true
end
print("HOOK_OK|" .. tostring(__pt_registered))
print("---END---")
"""

DISABLE_LUA = '__pt_enabled = false __pt_active = false print("DISABLED|true") print("---END---")'

POLL_LUA = """
print("LOCAL|" .. tostring(Game.GetLocalPlayer()))
print("TURN|" .. tostring(Game.GetCurrentGameTurn()))
print("ACTIVE|" .. tostring(__pt_active))
print("LAST|" .. tostring(__pt_last))
print("---END---")
"""

def build_finish_units_lua(pid: int) -> str:
    return f"""
local pp = Players[{pid}]
local n = 0
if pp ~= nil then for _, u in pp:GetUnits():Members() do pcall(function() UnitManager.FinishMoves(u) end); n = n + 1 end end
print("FINISHED|" .. tostring(n)) print("---END---")
"""

def build_restore_local_lua(pid: int) -> str:
    return (f'pcall(function() PlayerManager.SetLocalPlayerAndObserver({pid}) end) '
            f'print("LOCAL|" .. tostring(Game.GetLocalPlayer())) print("---END---")')

def _to_int(v, default=-1):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default

def parse_poll(lines: list[str]) -> PuppetState:
    d = {ln.split("|", 1)[0]: ln.split("|", 1)[1] for ln in lines if "|" in ln}
    last = d.get("LAST")
    return PuppetState(
        local=_to_int(d.get("LOCAL")),
        turn=_to_int(d.get("TURN")),
        active=(d.get("ACTIVE") == "true"),
        last=(_to_int(last, None) if (last not in (None, "nil")) else None),
    )

# All switch/unit ops are GameCore (execute_read); see Global Constraints.
async def inject(conn, ids):       return await conn.execute_read(build_inject_lua(ids))
async def disable(conn):           return await conn.execute_read(DISABLE_LUA)
async def poll(conn):              return parse_poll(await conn.execute_read(POLL_LUA))
async def finish_units(conn, pid): return await conn.execute_read(build_finish_units_lua(pid))
async def restore_local(conn, pid=0): return await conn.execute_read(build_restore_local_lua(pid))
