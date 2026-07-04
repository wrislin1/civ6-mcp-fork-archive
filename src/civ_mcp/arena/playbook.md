# Civ VI Strategy Digest (arena local civ)

## Every turn, in order
1. get_overview — turn, yields, what you are researching.
2. get_units — every unit acts every turn: move, attack, improve, fortify, or skip.
3. get_cities — no city may have an empty production queue.
4. If you have a settler: settle good land fast (see Expansion). If you have a builder:
   improve tiles (see Builders). If military: scout, escort, or clear barbarians.

## Expansion (the strongest lever)
- More cities = more science, gold, and production. Aim for a new city every ~10 turns
  early; 4+ cities by turn 60.
- Settle on flat land near fresh water (river/lake), 3+ tiles from another city, with
  hills and resources nearby. Coastal is fine if the land is good.
- A settler caught alone is captured: keep a warrior adjacent or ahead on the path.
- Production priority in a new empire: Scout -> Settler -> Settler/Builder, adding a
  Warrior when barbarians threaten and a Monument when safe.

## Growth
- Fix any city with food surplus <= 0 immediately: Farm, Granary, or switch production.
- Housing caps growth: settle near fresh water, build farms in pairs/triangles.

## Research and civics
- Early tech order that rarely fails: what your terrain needs (Mining for hills/woods,
  Animal Husbandry for pastures), then Pottery, Writing, Bronze Working (reveals Iron).
- Set a civic every time one finishes: Code of Laws -> Foreign Trade -> Craftsmanship ->
  Early Empire (boosts from settling/improving accelerate these).
- Anything flagged as completable in <= 2 turns is usually worth grabbing first.

## Builders
- 3 charges each. Improve bonus/luxury resources first (Plantation, Mine, Pasture,
  Camp), then Farms on flat river tiles, Mines on bare hills.
- Forest/jungle blocks Farms: remove_feature first, or build a Lumber Mill on forest.
- Never walk a builder into unexplored or enemy-visible tiles unescorted.

## Combat basics
- Warrior 20 CS melee; Slinger 15 RS ranged (range 1); Archer 25 RS (range 2).
  Barbarian warriors are 20 CS.
- Ranged units take no damage attacking; melee units do. Soften with ranged, finish
  with melee. Fortified units get +4 and heal each turn.
- Clear barbarian camps near your cities within a few turns of spotting them, or they
  will spawn endless raiders. One warrior + one slinger/archer clears an early camp.
- Keep one military unit in or beside each city.

## Districts (unlock with population)
- Campus (science) next to mountains; Commercial Hub (gold) on rivers; Holy Site
  (faith) next to mountains/forest. Place with set_city_production once available.

## Using the map
- The briefing shows tiles around your units and cities: terrain, resources, rivers,
  hills, and any visible foreign units. Unexplored area means threats you cannot see —
  move scouts toward it.
- Hills and forest cost 2 movement each (stacking); plan multi-turn moves accordingly.

## Priorities when unsure
1. Empty production queue -> fix it. 2. Idle unit -> use it. 3. Settler ready and a
spot known -> settle. 4. Barbarian camp near a city -> clear it. 5. Otherwise: improve
tiles, scout, and keep research/civics running.
