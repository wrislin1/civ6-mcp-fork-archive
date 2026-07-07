# Civ VI Strategy Digest (arena local civ)

## Every turn, in order
1. get_overview — turn, yields, what you are researching.
2. get_units — every unit acts every turn: move, attack, improve, fortify, or skip.
3. get_cities — no city may have an empty production queue.
4. If you have a settler: settle good land fast (see Expansion). If you have a builder:
   improve tiles (see Builders). If military: scout, escort, or clear barbarians.
5. If the map shows a visible goody hut (`IMPROVEMENT_GOODY_HUT`), send any safe unit
   onto it quickly. Huts are free rewards and first-come.

## Expansion (the strongest lever)
- More cities = more science, gold, and production. Aim for a new city every ~10 turns
  early; 4+ cities by turn 60.
- Prefer fresh water (river/lake) and a flat or plains-hills city center, 3+ tiles
  from another city, with hills and resources nearby. Coastal is fine if the land is good.
- A settler caught alone is captured: escort settlers with a warrior adjacent or ahead
  on the path.
- Production priority in a new empire: Scout -> Settler -> Settler/Builder, adding a
  Warrior when barbarians threaten and a Monument when safe.

## Growth
- Fix any city with food surplus <= 0 immediately: Farm, Granary, or switch production.
- Housing caps growth: settle near fresh water, build farms in pairs/triangles.

## Research and civics
- Early tech order that rarely fails: what your terrain needs (Mining for hills/woods,
  Animal Husbandry for pastures), then Pottery, Writing, Bronze Working (reveals Iron).
- Set a civic every time one finishes: Code of Laws -> Foreign Trade -> Craftsmanship ->
  Early Empire -> Political Philosophy. Tier-1 governments give 4 policy slots; do not
  stay in the starting 2-slot government once a real government is available.
- Eurekas and inspirations are half-cost accelerators. If a near-term unit, improvement,
  kill, or civic action unlocks one, prefer it over blind beelining.
- Anything flagged as completable in <= 2 turns is usually worth grabbing first.

## Pantheon and city-states
- Around turn 20, check whether you can choose a pantheon. Favor practical early beliefs
  that improve growth, production, faith, or a nearby resource cluster.
- Meeting city-states early matters. First-meet yields and envoy leverage compound; send
  envoys when tokens are available and the city-state bonus fits your plan.

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
- A barbarian scout that sees your city can report back to its camp and trigger raids.
  Intercept or kill it before it returns home.
- Keep one military unit in or beside each city.
- For war, position units while still at peace. The combat engine recognizes a newly
  declared enemy only on the next turn, so attack after the declaration turn.

## Districts (unlock with population)
- Campus (science) next to mountains; Commercial Hub (gold) on rivers; Holy Site
  (faith) next to mountains/forest. Place with set_city_production once available.
- Specialize early cities instead of making every city generic. Focus the first districts
  around your best adjacency and victory path, and preserve future district discount
  opportunities by not overbuilding one district type everywhere.

## Using the map
- The briefing shows tiles around your units and cities: terrain, resources, rivers,
  hills, and any visible foreign units. Unexplored area means threats you cannot see —
  move scouts toward it.
- Hills and forest cost 2 movement each (stacking); plan multi-turn moves accordingly.

## Priorities when unsure
1. Empty production queue -> fix it. 2. Idle unit -> use it. 3. Settler ready and a
spot known -> settle. 4. Barbarian camp near a city -> clear it. 5. Otherwise: improve
tiles, scout, and keep research/civics running.

## Unit promotions
Units earn XP by surviving combat; ranged units earn XP without taking damage. A unit
with an unspent promotion earns NO more XP until you spend it. When the promotions briefing appears,
or when get_unit_promotions(unit_id).promotions is non-empty, promote immediately.
Promoting also heals the unit; use it as mid-fight sustain.
Strong early picks: melee -> Battlecry (+7 attacking); ranged -> Volley (+5 vs land);
recon -> prefer a vision/mobility promotion when offered (Sentry, Spyglass, Ranger,
Alpine). Use promote_unit(unit_id, promotion_type).

## Unit upgrades
Upgrade obsolete units when you have the tech + resources + gold: Slinger->Archer with
Archery, Warrior->Swordsman with Iron Working. Units fall behind rivals fast if not
upgraded. Use upgrade_unit(unit_id).

## Signals to watch
Loyalty below 75 penalizes a city's yields -- assign a governor or fix amenities. Each
new DISTINCT luxury = +1 amenity; duplicates are worthless, so save them to trade
later. Watch era score against the Golden/Dark thresholds shown in the overview.

## Diplomacy, trades, and peace
If a leader screen or deal blocks progress, inspect it first: use get_pending_diplomacy
for spoken encounters and respond_to_diplomacy(other_player_id, POSITIVE/NEGATIVE)
for 2-3 rounds until it closes. Use get_pending_trades for incoming deals, then
respond_to_trade only if the exchange helps you; reject bad deals quickly.

On first meeting, use send_diplomatic_action(action="DIPLOMATIC_DELEGATION") if
you can afford 25 gold. Friendly neighbors are worth converting with
send_diplomatic_action(action="DECLARE_FRIENDSHIP"), then alliances after
friendship matures and Diplomatic Service is available; alliances generate favor and reduce war risk.
Embassies use send_diplomatic_action(action="RESIDENT_EMBASSY") after Writing.
Use get_trade_options before propose_trade or form_alliance so you know gold,
resources, favor, open borders, and alliance eligibility.

Surplus luxuries beyond one copy give no extra amenities. Sell duplicates or spare
diplomatic favor for gold or gold-per-turn, preferably testing the deal first with
propose_trade(mode="test") before committing with mode="send". Open borders are also
trade deals.

If a war is going badly or has stopped producing gains, propose_peace after the
10-turn cooldown. To start a war, send_diplomatic_action(action="DECLARE_SURPRISE_WAR")
or another valid DECLARE_*_WAR token declares it, but attacks usually work next
turn; declare, position safely, then attack on the following turn.

## Standing plan
End every turn's reasoning with a line starting `STANDING PLAN:` summarizing your
intent for the next several turns. Use `TASK` lines under it for a single
settle or builder-improve follow-through in progress (e.g. a settler walking to a
site, a builder improving one tile) so you can pick the plan back up next turn
without re-deriving it from scratch. The tracker only executes the `settle` and
`builder_improve` moves it parses from these lines -- it does not choose targets,
sequence multiple actions, or make any other strategic decision for you.

## Great People
Check get_great_people regularly and recruit as soon as `[CAN RECRUIT]` appears --
rivals will claim what you pass on. Use get_gp_advisor to pick the best completed
district to activate a recruited Great Person in. Never delete a Great Person unit;
it shows 0 builder charges but is a different system and is only consumed on activation.

## Trade routes
Check get_trade_routes for idle traders -- an idle route is free yields going
uncollected. Use get_trade_destinations(unit_id) to see options, then
start_trade_route to the best one (domestic for food/production to new cities,
international for gold).

## World Congress and religion
Monitor World Congress and religion state, but only cast votes or lock in belief
choices after reading the relevant tool's actual output (get_world_congress,
get_religion_beliefs, etc.) for that turn -- do not guess resolutions or beliefs
from memory.

## Espionage and government
Once spies unlock, get_spies shows missions; travel first, end turn, then launch.
Change government when a new tier unlocks (first switch per tier is free).

## Grievances, loyalty, climate
get_gossip shows grievances - high grievances against you predict war. get_loyalty
flags cities trending to revolt: assign a governor or fix amenities the same turn.
get_climate warns of floods/storms - avoid settling low coast in late game.

## Formations, air, archaeology, great works
Two same-type units adjacent -> form_corps (+10 CS) once Nationalism unlocks; a
corps + one more -> form_army after Mobilization. Rebase air units forward as the
front moves. Archaeologists dig antiquity sites (excavate_artifact) to fill museum
slots; group matching great works in one building (move_great_work) for theming.
