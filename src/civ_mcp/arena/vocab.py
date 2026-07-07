"""Shared tool-name vocabulary constants for the arena driver and analysis pipeline.

Pure constants — no heavy imports — so analyze.py stays offline-pure.
"""
from __future__ import annotations

MCP_CIV6_PREFIX = "mcp__civ6__"

# Maps a local tool name to its analysis verb. This is an offline-pure MIRROR of
# the ``verb`` field on each action tool in ``arena.registry.TOOL_REGISTRY`` (the
# single source of truth). It is duplicated here — rather than derived — so that
# analyze.py can import it without pulling in the registry (and its narrate/game
# imports). ``tests/arena/test_analyze.py`` asserts this dict stays in exact sync
# with the registry, so a new action tool that forgets to update this table fails
# the suite instead of silently dropping out of rubric coverage.
LOCAL_TOOL_VERBS: dict[str, str] = {
    "move_unit": "move",
    "found_city": "found_city",
    "fortify_unit": "fortify",
    "skip_unit": "skip",
    "attack_unit": "attack",
    "improve_tile": "improve",
    "remove_feature": "remove_feature",
    "purchase_item": "purchase",
    "heal_unit": "heal",
    "alert_unit": "alert",
    "set_civic": "set_civic",
    "send_envoy": "send_envoy",
    "set_policies": "set_policies",
    "appoint_governor": "appoint_governor",
    "assign_governor": "assign_governor",
    "choose_pantheon": "choose_pantheon",
    "upgrade_unit": "upgrade",
    "promote_unit": "promote",
    "automate_explore": "automate",
    "skip_remaining_units": "skip",
    "purchase_tile": "purchase_tile",
    "set_city_focus": "set_city_focus",
    "respond_to_diplomacy": "respond_to_diplomacy",
    "respond_to_trade": "respond_to_trade",
    "propose_trade": "propose_trade",
    "propose_peace": "propose_peace",
    "send_diplomatic_action": "send_diplomatic_action",
    "form_alliance": "form_alliance",
    "promote_governor": "promote_governor",
    "choose_dedication": "choose_dedication",
    "found_religion": "found_religion",
    "recruit_great_person": "recruit_great_person",
    "patronize_great_person": "patronize_great_person",
    "reject_great_person": "reject_great_person",
    "start_trade_route": "start_trade_route",
    "teleport_trader": "teleport_trader",
    "queue_wc_votes": "queue_wc_votes",
    "city_attack": "city_attack",
    "resolve_city_capture": "resolve_city_capture",
    "spy_action": "spy_action",
    "change_government": "change_government",
    "spread_religion": "spread_religion",
    "activate_great_person": "activate_great_person",
}
