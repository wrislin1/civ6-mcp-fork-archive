from __future__ import annotations

from typing import Any

from civ_mcp.arena.briefing import Briefing, build_briefing
from civ_mcp.arena.budget import briefing_budget


async def maybe_build_briefing(
    gs: Any,
    options: Any,
    *,
    n_ctx: int,
    playbook_chars: int,
    tool_schema_chars: int,
    supplied: Briefing | None = None,
) -> Briefing:
    if supplied is not None:
        return supplied
    if not options.briefing.enabled:
        return Briefing()
    budget = briefing_budget(
        n_ctx,
        options,
        playbook_chars,
        tool_schema_chars,
    )
    return await build_briefing(gs, options.briefing, budget)
