"""Shared opening-prompt assembly for local (in-process) and CLI arena puppets.

Both `LLMPolicy` (agent.py) and `CLIAgentPolicy` (cli_agent.py) build a per-turn
opening message out of the same ordered blocks: briefing text, standing-memory
recap, task-tracker recap, the turn/player announcement, and (when memory or
task tracking is enabled) an instruction asking the model to end its response
with a machine-parseable STANDING PLAN block. Keeping the ordering and text in
one place is what lets both puppet kinds carry standing memory / task tracking
consistently (Slice 3).
"""
from __future__ import annotations

STANDING_PLAN_INSTRUCTION = """End your final response with:
STANDING PLAN:
- One to three short bullets for next turn.
- Optional task lines, for example:
  TASK settle unit_id=123 target=18,24
  TASK builder_improve unit_id=456 target=12,19 improvement=IMPROVEMENT_FARM
"""


def build_opening_prompt(
    *,
    player_id: int,
    turn: int,
    briefing_text: str = "",
    memory_block: str = "",
    task_block: str = "",
    include_standing_plan_instruction: bool = False,
) -> str:
    """Assemble the opening user-turn message.

    Ordering (fixed): briefing_text, memory_block, task_block, the turn/player
    announcement, then STANDING_PLAN_INSTRUCTION when requested. Empty blocks
    are omitted with no extra blank lines.
    """
    parts: list[str] = []
    if briefing_text:
        parts.append(briefing_text)
    if memory_block:
        parts.append(memory_block)
    if task_block:
        parts.append(task_block)
    parts.append(f"It is turn {turn}. You control player {player_id}. Begin.")
    if include_standing_plan_instruction:
        parts.append(STANDING_PLAN_INSTRUCTION)
    return "\n\n".join(parts)
