"""Shared tool-name vocabulary constants for the arena driver and analysis pipeline.

Pure constants — no heavy imports — so analyze.py stays offline-pure.
"""
from __future__ import annotations

MCP_CIV6_PREFIX = "mcp__civ6__"

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
}
