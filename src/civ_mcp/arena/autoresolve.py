"""Deterministic end-of-turn auto-resolution for arena puppets.

Slice 1 covers unit promotions: the model turn runs first (and may promote as
part of a combat plan), then `sweep_promotions` is the safety net that spends any
promotion still pending. `get_units().needs_promotion` is dead (lua/units.py:105);
the authoritative signal is a non-empty `get_unit_promotions(unit_id).promotions`.
"""

from __future__ import annotations

from typing import Any

from civ_mcp.lua import PromotionOption, UnitPromotionStatus

# Ordered global preference of promotion *types*. The type identifies its own tree
# (VOLLEY = ranged, BATTLECRY = melee, SENTRY/SPYGLASS/RANGER/ALPINE = recon), so no
# unit-class field is needed. This is a preference, not a whitelist -- anything not
# listed is still taken via the first-available fallback. Identifiers are best-guess
# Civ VI strings; a wrong one simply misses the preference and falls through.
PREFERRED_PROMOTIONS: tuple[str, ...] = (
    "PROMOTION_VOLLEY",
    "PROMOTION_BATTLECRY",
    "PROMOTION_SENTRY",
    "PROMOTION_SPYGLASS",
    "PROMOTION_RANGER",
    "PROMOTION_ALPINE",
    "PROMOTION_TORTOISE",
    "PROMOTION_GARRISON",
)


def pick_promotion(status: UnitPromotionStatus) -> PromotionOption | None:
    """Choose a promotion for a unit that has one pending.

    Returns the offered `PromotionOption` whose type is highest in
    `PREFERRED_PROMOTIONS`; else the first offered option (any promotion unfreezes
    XP and heals -- a suboptimal pick still beats none); else `None` if none offered.
    """
    promos = list(getattr(status, "promotions", None) or [])
    if not promos:
        return None
    by_type = {p.promotion_type: p for p in promos}
    for pref in PREFERRED_PROMOTIONS:
        if pref in by_type:
            return by_type[pref]
    return promos[0]


async def sweep_promotions(gs: Any) -> list[dict]:
    """Spend any pending promotion on every puppet unit. Best-effort: never raises.

    Enumerates units, then for EACH unit checks `get_unit_promotions` (do not
    pre-filter on `needs_promotion` -- it is always false). A non-empty
    `status.promotions` means a promotion is pending; pick one and apply it.
    """
    swept: list[dict] = []
    try:
        units = await gs.get_units()
    except Exception:
        return swept
    if isinstance(units, str):
        return swept

    for u in units:
        try:
            status = await gs.get_unit_promotions(u.unit_id)
        except Exception:
            continue  # e.g. civilians have no experience object
        if not getattr(status, "promotions", None):
            continue
        pick = pick_promotion(status)
        if pick is None:
            continue
        entry: dict = {
            "unit_id": u.unit_id,
            "unit_type": getattr(u, "unit_type", ""),
            "promotion_type": pick.promotion_type,
            "ok": False,
        }
        try:
            result = await gs.promote_unit(u.unit_id, pick.promotion_type)
            entry["ok"] = not (isinstance(result, str) and result.startswith("Error"))
        except Exception as e:  # never let a single unit break the sweep
            entry["error"] = repr(e)
        swept.append(entry)
    return swept
