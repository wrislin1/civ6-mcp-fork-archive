"""Climate & disasters domain (Gathering Storm systems) — greenfield Lua.

Every API call is pcall-guarded; a base game or missing API prints
CLIMATE|-1|-1|-1 so the tool degrades to an explicit "unavailable" readout.
-- PROBE(live): all APIs in this module are validated by the Task 15
live-probe checklist before the tool reaches a live run.
"""
from __future__ import annotations

from civ_mcp.lua._helpers import SENTINEL, _int
from civ_mcp.lua.models import ClimateStatus, DisasterEvent

_CLIMATE_LUA = """
local phase, sea, co2 = -1, -1, -1
pcall(function() phase = GameClimate.GetClimateChangeLevel() end)
pcall(function() sea = GameClimate.GetSeaLevel() end)
pcall(function() co2 = GameClimate.GetTotalCO2Footprint() end)
print("CLIMATE|" .. phase .. "|" .. sea .. "|" .. co2)
pcall(function()
    -- PROBE(live): recent random events / active storms query (Task 15)
    local events = Game.GetRandomEventsManager():GetActiveEvents()
    for _, ev in ipairs(events) do
        print("DISASTER|" .. tostring(ev.Type) .. "|" .. ev.X .. "|" .. ev.Y
              .. "|" .. Game.GetCurrentGameTurn())
    end
end)
print("{SENTINEL}")
"""


def build_climate_query() -> str:
    """InGame context: climate phase, sea level, CO2, active disasters."""
    return _CLIMATE_LUA.replace("{SENTINEL}", SENTINEL)


def parse_climate_response(lines: list[str]) -> ClimateStatus:
    status = ClimateStatus(phase=-1, sea_level=-1, co2_total=-1)
    for line in lines:
        parts = line.split("|")
        try:
            if parts[0] == "CLIMATE" and len(parts) >= 4:
                # parse all three before assigning any — a bad field must not
                # leave a half-updated status.
                phase, sea, co2 = _int(parts[1]), _int(parts[2]), _int(parts[3])
                status.phase, status.sea_level, status.co2_total = phase, sea, co2
            elif parts[0] == "DISASTER" and len(parts) >= 5:
                status.disasters.append(DisasterEvent(
                    kind=parts[1], x=_int(parts[2]), y=_int(parts[3]),
                    turn=_int(parts[4])))
        except (ValueError, IndexError):
            continue
    return status
