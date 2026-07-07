"""Per-turn capability snapshot for era/state-gated tool exposure (spec §1).

One cheap GameCore (execute_read) query per puppet turn emits one CAPS| line
of flag=0/1 fields; parse_caps() turns it into the dict consumed by
registry.filter_tools(). Action tools gate on *executable-now* state (unlock
AND required game objects). Every failure path fails OPEN (flag stays true /
parse returns None -> full toolset): an ungated tool costs invalid-call
churn; an over-closed gate silently removes an ability.
"""
from __future__ import annotations

CAP_FLAGS: tuple[str, ...] = (
    "spies", "government", "religious_unit", "gp_unit",
    "corps", "army", "air", "archaeology", "great_works",
)

# Plain string (lua braces break f-strings), __PID__ substituted at build time.
_CAPS_LUA = """
local p = Players[__PID__]
-- fail-open defaults: a check that errors leaves its flag exposed
local flags = {spies=true, government=true, religious_unit=true, gp_unit=true,
               corps=true, army=true, air=true, archaeology=true, great_works=true}
local function civ(name)
    local row = GameInfo.Civics[name]
    if row == nil then return true end
    return p:GetCulture():HasCivic(row.Index)
end
pcall(function() flags.spies = civ("CIVIC_DIPLOMATIC_SERVICE") end)
pcall(function() flags.government = civ("CIVIC_CODE_OF_LAWS") end)
local natl, mob = true, true
pcall(function() natl = civ("CIVIC_NATIONALISM") end)
pcall(function() mob = civ("CIVIC_MOBILIZATION") end)
pcall(function()
    -- PROBE(live): unit-scan APIs (GetSpreadCharges, GetGreatPerson():IsGreatPerson(),
    -- GetBuildCharges/ExtractsArtifacts, GetMilitaryFormation + MilitaryFormationTypes/
    -- FormationClass enums) validated by the Task 15 checklist. On error all six
    -- flags in this block stay fail-open true.
    local rel, gpu, air, arch, corpsOwned, pair = false, false, false, false, false, false
    local counts = {}
    for i, u in p:GetUnits():Members() do
        local info = GameInfo.Units[u:GetType()]
        local okC, charges = pcall(function() return u:GetSpreadCharges() end)
        if okC and charges and charges > 0 then rel = true end
        local okG, isGP = pcall(function() return u:GetGreatPerson():IsGreatPerson() end)
        if okG and isGP then gpu = true end
        if info then
            if info.Domain == "DOMAIN_AIR" then air = true end
            local okB, bc = pcall(function() return u:GetBuildCharges() end)
            if info.ExtractsArtifacts and okB and bc and bc > 0 then arch = true end
        end
        local okF, mf = pcall(function() return u:GetMilitaryFormation() end)
        if okF and mf then
            if mf == MilitaryFormationTypes.CORPS_FORMATION then corpsOwned = true end
            if info and info.FormationClass == "FORMATION_CLASS_LAND_COMBAT"
                    and mf == MilitaryFormationTypes.STANDARD_FORMATION then
                counts[info.UnitType] = (counts[info.UnitType] or 0) + 1
                if counts[info.UnitType] >= 2 then pair = true end
            end
        end
    end
    flags.religious_unit = rel
    flags.gp_unit = gpu
    flags.air = air
    flags.archaeology = arch
    flags.corps = natl and pair
    flags.army = mob and corpsOwned
end)
pcall(function()
    -- PROBE(live): great-work count API (Task 15). On error flag stays true.
    local own = false
    for _, c in p:GetCities():Members() do
        local b = c:GetBuildings()
        for row in GameInfo.Buildings() do
            if b:HasBuilding(row.Index) and b:GetNumGreatWorksInBuilding(row.Index) > 0 then
                own = true
            end
        end
    end
    flags.great_works = own
end)
local function b2i(v) if v then return 1 end return 0 end
print(string.format(
    "CAPS|spies=%d|government=%d|religious_unit=%d|gp_unit=%d|corps=%d|army=%d|air=%d|archaeology=%d|great_works=%d",
    b2i(flags.spies), b2i(flags.government), b2i(flags.religious_unit),
    b2i(flags.gp_unit), b2i(flags.corps), b2i(flags.army), b2i(flags.air),
    b2i(flags.archaeology), b2i(flags.great_works)))
print("---END---")
"""


def build_caps_query(player_id: int) -> str:
    return _CAPS_LUA.replace("__PID__", str(int(player_id)))


def parse_caps(lines: list[str] | None) -> dict[str, bool] | None:
    """CAPS| line -> {flag: bool}. Any malformed input returns None (fail open)."""
    if not lines:
        return None
    for line in lines:
        if not line.startswith("CAPS|"):
            continue
        flags: dict[str, bool] = {}
        for field in line[5:].split("|"):
            key, sep, val = field.partition("=")
            if sep and key in CAP_FLAGS and val.strip() in ("0", "1"):
                flags[key] = val.strip() == "1"
        return flags or None
    return None
