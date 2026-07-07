"""Great Works domain — slots, contents, and moves.

-- PROBE(live): slot/query APIs follow GreatWorksOverview.lua conventions
(GetNumGreatWorkSlots / GetGreatWorkSlotType / GetGreatWorkInSlot /
Game.GetGreatWorkDataFromIndex) and the move API is a best-guess; both are
validated by the Task 15 checklist before the tools reach a live run.
"""
from __future__ import annotations

from civ_mcp.lua._helpers import SENTINEL
from civ_mcp.lua.models import GreatWorkSlot

_GW_QUERY_LUA = """
local me = Game.GetLocalPlayer()
for _, c in Players[me]:GetCities():Members() do
    pcall(function()
        local b = c:GetBuildings()
        local cname = Locale.Lookup(c:GetName())
        for row in GameInfo.Buildings() do
            if b:HasBuilding(row.Index) then
                local nSlots = 0
                pcall(function() nSlots = b:GetNumGreatWorkSlots(row.Index) end)
                for s = 0, nSlots - 1 do
                    pcall(function()
                        local slotType = "UNKNOWN"
                        pcall(function()
                            local st = b:GetGreatWorkSlotType(row.Index, s)
                            slotType = GameInfo.GreatWorkSlotTypes[st].GreatWorkSlotType
                        end)
                        local gwIndex = b:GetGreatWorkInSlot(row.Index, s)
                        local gwName = ""
                        if gwIndex and gwIndex >= 0 then
                            pcall(function()
                                local data = Game.GetGreatWorkDataFromIndex(gwIndex)
                                gwName = Locale.Lookup(data.Name)
                            end)
                        else
                            gwIndex = -1
                        end
                        print("GWSLOT|" .. c:GetID() .. "|" .. cname .. "|"
                              .. row.BuildingType .. "|" .. s .. "|" .. slotType
                              .. "|" .. gwIndex .. "|" .. gwName)
                    end)
                end
            end
        end
    end)
end
print("{SENTINEL}")
"""

_GW_MOVE_LUA = """
local me = Game.GetLocalPlayer()
local workIndex = __WORK__
local targetCityId = __CITY__
local targetBuilding = "__BUILDING__"
local targetSlot = __SLOT__
local ok, err = pcall(function()
    -- locate the work's current slot
    local fromCity, fromBuildingIdx, fromSlot = nil, nil, nil
    for _, c in Players[me]:GetCities():Members() do
        local b = c:GetBuildings()
        for row in GameInfo.Buildings() do
            if b:HasBuilding(row.Index) then
                local n = 0
                pcall(function() n = b:GetNumGreatWorkSlots(row.Index) end)
                for s = 0, n - 1 do
                    if b:GetGreatWorkInSlot(row.Index, s) == workIndex then
                        fromCity, fromBuildingIdx, fromSlot = c, row.Index, s
                    end
                end
            end
        end
    end
    if fromCity == nil then
        print("ERR:great work " .. workIndex .. " not found in any of your slots")
        return
    end
    local toCity = CityManager.GetCity(me, targetCityId % 65536)
    local toRow = GameInfo.Buildings[targetBuilding]
    if toCity == nil or toRow == nil then
        print("ERR:target city or building not found")
        return
    end
    -- PROBE(live): move request API (Task 15)
    UI.MoveGreatWork(fromCity:GetID(), fromBuildingIdx, fromSlot,
                     toCity:GetID(), toRow.Index, targetSlot)
    print("OK:requested move of work " .. workIndex .. " to " .. targetBuilding
          .. " slot " .. targetSlot)
end)
if not ok then print("ERR:" .. tostring(err)) end
print("{SENTINEL}")
"""


def build_great_works_query() -> str:
    """InGame context: every great-work slot you own, with contents."""
    return _GW_QUERY_LUA.replace("{SENTINEL}", SENTINEL)


def build_move_great_work(
    work_index: int, target_city_id: int, building: str, slot: int
) -> str:
    """InGame context: move a great work into a target building slot."""
    if not building.replace("_", "").isalnum():
        raise ValueError(f"suspicious building id: {building!r}")
    return (_GW_MOVE_LUA
            .replace("__WORK__", str(int(work_index)))
            .replace("__CITY__", str(int(target_city_id)))
            .replace("__BUILDING__", building)
            .replace("__SLOT__", str(int(slot)))
            .replace("{SENTINEL}", SENTINEL))


def parse_great_works_response(lines: list[str]) -> list[GreatWorkSlot]:
    slots: list[GreatWorkSlot] = []
    for line in lines:
        parts = line.split("|")
        try:
            if parts[0] == "GWSLOT" and len(parts) >= 8:
                slots.append(GreatWorkSlot(
                    city_id=int(parts[1]), city_name=parts[2], building=parts[3],
                    slot_index=int(parts[4]), slot_type=parts[5],
                    work_index=int(parts[6]), work_name=parts[7]))
        except (ValueError, IndexError):
            continue
    return slots
