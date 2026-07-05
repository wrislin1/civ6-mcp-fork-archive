import pytest

from civ_mcp import lua as lq
from civ_mcp.arena import autoresolve


def _status(*types):
    return lq.UnitPromotionStatus(
        unit_id=65537,
        unit_index=1,
        unit_type="UNIT_WARRIOR",
        promotions=[
            lq.PromotionOption(
                promotion_type=t,
                name=t.replace("PROMOTION_", "").title(),
                description="d",
            )
            for t in types
        ],
    )


def test_pick_prefers_earliest_preferred_type():
    # BATTLECRY is earlier in PREFERRED_PROMOTIONS than GARRISON; offered order is reversed.
    pick = autoresolve.pick_promotion(_status("PROMOTION_GARRISON", "PROMOTION_BATTLECRY"))
    assert pick.promotion_type == "PROMOTION_BATTLECRY"


def test_pick_falls_back_to_first_available_when_none_preferred():
    pick = autoresolve.pick_promotion(_status("PROMOTION_UNKNOWN_A", "PROMOTION_UNKNOWN_B"))
    assert pick.promotion_type == "PROMOTION_UNKNOWN_A"


def test_pick_returns_none_on_empty():
    assert autoresolve.pick_promotion(_status()) is None


class _Unit:
    def __init__(self, unit_id, unit_type="UNIT_WARRIOR", needs_promotion=False):
        self.unit_id = unit_id
        self.unit_type = unit_type
        self.needs_promotion = needs_promotion  # always false in reality; must be ignored


class _GS:
    def __init__(self, units, promo_by_id, promote_impl=None):
        self._units = units
        self._promo = promo_by_id
        self._promote_impl = promote_impl
        self.promoted = []

    async def get_units(self):
        return self._units

    async def get_unit_promotions(self, unit_id):
        v = self._promo[unit_id]
        if isinstance(v, Exception):
            raise v
        return v

    async def promote_unit(self, unit_id, promotion_type):
        self.promoted.append((unit_id, promotion_type))
        if self._promote_impl is not None:
            return self._promote_impl(unit_id, promotion_type)
        return f"Promoted {unit_id} to {promotion_type}"


@pytest.mark.asyncio
async def test_sweep_promotes_units_with_pending_promotions():
    gs = _GS(
        units=[_Unit(1, needs_promotion=False), _Unit(2)],
        promo_by_id={1: _status("PROMOTION_BATTLECRY"), 2: _status()},  # unit 2 has none
    )
    swept = await autoresolve.sweep_promotions(gs)
    assert gs.promoted == [(1, "PROMOTION_BATTLECRY")]  # unit 2 skipped despite no filter on needs_promotion
    assert swept == [
        {
            "unit_id": 1,
            "unit_type": "UNIT_WARRIOR",
            "promotion_type": "PROMOTION_BATTLECRY",
            "ok": True,
        }
    ]


@pytest.mark.asyncio
async def test_sweep_swallows_get_promotions_error():
    gs = _GS(units=[_Unit(1)], promo_by_id={1: RuntimeError("no experience")})
    swept = await autoresolve.sweep_promotions(gs)
    assert swept == []
    assert gs.promoted == []


@pytest.mark.asyncio
async def test_sweep_records_ok_false_on_promote_error_string():
    gs = _GS(
        units=[_Unit(1)],
        promo_by_id={1: _status("PROMOTION_BATTLECRY")},
        promote_impl=lambda uid, pt: "Error: CANNOT_PROMOTE",
    )
    swept = await autoresolve.sweep_promotions(gs)
    assert swept[0]["ok"] is False


@pytest.mark.asyncio
async def test_sweep_swallows_promote_exception():
    def _raise(uid, pt):
        raise RuntimeError("boom")

    gs = _GS(units=[_Unit(1)], promo_by_id={1: _status("PROMOTION_BATTLECRY")}, promote_impl=_raise)
    swept = await autoresolve.sweep_promotions(gs)  # must not raise
    assert swept[0]["ok"] is False and "error" in swept[0]
