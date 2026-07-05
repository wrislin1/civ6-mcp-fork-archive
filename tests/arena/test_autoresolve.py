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
