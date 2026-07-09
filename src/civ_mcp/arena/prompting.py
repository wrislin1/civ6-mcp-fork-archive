"""Shared opening-prompt assembly for local (in-process) and CLI arena puppets.

Both `LLMPolicy` (agent.py) and `CLIAgentPolicy` (cli_agent.py) build a per-turn
opening message out of the same ordered blocks: briefing text, standing-memory
recap, task-tracker recap, wake digest, the turn/player announcement, and
(each independently gated) an instruction asking the model to end its response
with a machine-parseable STANDING PLAN block and/or a SKIP/WAKE IF attention
directive. Keeping the ordering and text in one place is what lets both puppet
kinds carry standing memory / task tracking / attention consistently (Slice 3,
attention & turn-skipping slice).
"""
from __future__ import annotations

# The example task lines use non-numeric placeholders (<unit_id>, <x>, <y>)
# on purpose: this instruction is injected into every prompt, and a model that
# echoes the example verbatim must NOT create a real task. Placeholders don't
# match TASK_LINE_RE (which requires digits), so only lines the model fills in
# with real ids/coords parse. Do not replace them with concrete numbers.
STANDING_PLAN_INSTRUCTION = """End your final response with:
STANDING PLAN:
- One to three short bullets for next turn.
- Optional task lines, using your real unit ids and target coordinates, e.g.:
  TASK settle unit_id=<unit_id> target=<x>,<y>
  TASK builder_improve unit_id=<unit_id> target=<x>,<y> improvement=IMPROVEMENT_FARM
"""

# Soft-trigger tokens are duplicated here as literal text on purpose: the
# instruction is a prompt, and importing attention.SOFT_TRIGGERS to format it
# would make prompt text drift with code changes invisibly. The prompting test
# asserts the two stay in sync.
_ATTENTION_INSTRUCTION_TEMPLATE = """If nothing will need your judgment for a few turns, you may ALSO end with:
SKIP: <1-{max_skip}>
WAKE IF: <optional, comma-separated from exactly: GREAT_PERSON_AVAILABLE, CITY_GREW, TRADE_ROUTE_IDLE, GOLD_STOCKPILE_HIGH>
You will be woken early regardless for any threat, blocker, or task event.
Skip during long builds or peacetime consolidation; never skip at war or with unsettled settlers."""


def attention_instruction(max_skip: int) -> str:
    """Render the SKIP/WAKE IF instruction for the run's actual clamp
    (review-3 f8: a non-default max_skip must not misinform the model)."""
    return _ATTENTION_INSTRUCTION_TEMPLATE.format(max_skip=max_skip)


# Default-clamp render: kept as a constant for existing imports and the
# default-drift pin (AttentionOptions.max_skip default == 5).
ATTENTION_INSTRUCTION = attention_instruction(5)


def build_opening_prompt(
    *,
    player_id: int,
    turn: int,
    briefing_text: str = "",
    memory_block: str = "",
    task_block: str = "",
    digest_block: str = "",
    include_standing_plan_instruction: bool = False,
    include_attention_instruction: bool = False,
    attention_max_skip: int = 5,
) -> str:
    """Assemble the opening user-turn message.

    Ordering (fixed): briefing_text, memory_block, task_block, digest_block,
    the turn/player announcement, then STANDING_PLAN_INSTRUCTION and
    attention_instruction(attention_max_skip) (each independently gated) when
    requested. Empty blocks are omitted with no extra blank lines.
    """
    parts: list[str] = []
    if briefing_text:
        parts.append(briefing_text)
    if memory_block:
        parts.append(memory_block)
    if task_block:
        parts.append(task_block)
    if digest_block:
        parts.append(digest_block)
    parts.append(f"It is turn {turn}. You control player {player_id}. Begin.")
    if include_standing_plan_instruction:
        parts.append(STANDING_PLAN_INSTRUCTION)
    if include_attention_instruction:
        parts.append(attention_instruction(attention_max_skip))
    return "\n\n".join(parts)
