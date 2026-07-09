import pytest

from civ_mcp.arena.prompting import (
    ATTENTION_INSTRUCTION,
    STANDING_PLAN_INSTRUCTION,
    build_opening_prompt,
)
from civ_mcp.arena.task_tracker import parse_task_lines


def test_standing_plan_instruction_examples_are_echo_safe():
    """The instruction is injected into every prompt; a model that echoes the
    example task lines verbatim must create NO tasks. The examples use
    non-numeric placeholders (<unit_id>, <x>) that TASK_LINE_RE cannot match."""
    assert parse_task_lines(STANDING_PLAN_INSTRUCTION, turn=1) == []


# ---------------------------------------------------------------------------
# build_opening_prompt — block order, empty-block omission, standing plan gate
# ---------------------------------------------------------------------------

def test_block_order_all_present():
    prompt = build_opening_prompt(
        player_id=2,
        turn=5,
        briefing_text="BRIEFING",
        memory_block="MEMORY",
        task_block="TASKS",
        include_standing_plan_instruction=True,
    )
    turn_line = "It is turn 5. You control player 2. Begin."
    assert prompt.index("BRIEFING") < prompt.index("MEMORY")
    assert prompt.index("MEMORY") < prompt.index("TASKS")
    assert prompt.index("TASKS") < prompt.index(turn_line)
    assert prompt.index(turn_line) < prompt.index("STANDING PLAN:")
    assert STANDING_PLAN_INSTRUCTION in prompt


def test_empty_blocks_omitted_cleanly():
    prompt = build_opening_prompt(player_id=3, turn=7)
    assert prompt == "It is turn 7. You control player 3. Begin."


def test_briefing_only_no_stray_blank_lines():
    prompt = build_opening_prompt(player_id=3, turn=7, briefing_text="BRIEFING BODY")
    assert prompt == "BRIEFING BODY\n\nIt is turn 7. You control player 3. Begin."
    # no leading/trailing whitespace, no triple-newline gaps from skipped blocks
    assert "\n\n\n" not in prompt
    assert prompt == prompt.strip()


def test_standing_plan_instruction_only_when_requested():
    without = build_opening_prompt(player_id=1, turn=1)
    assert "STANDING PLAN" not in without

    with_it = build_opening_prompt(player_id=1, turn=1, include_standing_plan_instruction=True)
    assert with_it.endswith(STANDING_PLAN_INSTRUCTION)


def test_partial_blocks_no_stray_blank_lines():
    """memory_block present, briefing/task absent — no extra separators around gaps."""
    prompt = build_opening_prompt(player_id=4, turn=9, memory_block="MEM RECAP")
    assert prompt == "MEM RECAP\n\nIt is turn 9. You control player 4. Begin."
    assert "\n\n\n" not in prompt


# ---------------------------------------------------------------------------
# Task 9 — digest_block ordering + ATTENTION_INSTRUCTION
# ---------------------------------------------------------------------------

def test_digest_block_ordered_after_task_block():
    out = build_opening_prompt(
        player_id=1, turn=5, briefing_text="B", memory_block="M",
        task_block="T", digest_block="== WHILE YOU SLEPT ==",
    )
    assert out.index("T") < out.index("WHILE YOU SLEPT") < out.index("It is turn 5")


def test_attention_instruction_appended_when_requested():
    out = build_opening_prompt(player_id=1, turn=5, include_attention_instruction=True)
    assert out.endswith(ATTENTION_INSTRUCTION)
    assert "SKIP:" in ATTENTION_INSTRUCTION and "WAKE IF:" in ATTENTION_INSTRUCTION


def test_attention_instruction_lists_exact_soft_enum():
    from civ_mcp.arena.attention import SOFT_TRIGGERS
    for token in SOFT_TRIGGERS:
        assert token in ATTENTION_INSTRUCTION


def test_attention_independent_of_standing_plan():
    out = build_opening_prompt(
        player_id=1, turn=5,
        include_standing_plan_instruction=False, include_attention_instruction=True,
    )
    assert "STANDING PLAN" not in out and "SKIP:" in out


def test_attention_instruction_skip_range_matches_default_max_skip():
    """The '1-5' range is literal prompt text on purpose (no invisible
    drift); this pin breaks loudly if AttentionOptions.max_skip's default
    changes without the prompt following (review-2 scope note)."""
    from civ_mcp.arena.agent import load_playbook
    from civ_mcp.arena.config import AttentionOptions

    default = AttentionOptions().max_skip
    assert f"<1-{default}>" in ATTENTION_INSTRUCTION
    assert f"SKIP n (1-{default})" in load_playbook()


def test_attention_instruction_renders_configured_max_skip():
    """Review-3 f8: the prompt's stated SKIP range must match the run's
    actual clamp, not the default."""
    from civ_mcp.arena.prompting import attention_instruction

    assert "<1-3>" in attention_instruction(3)
    assert "<1-10>" in attention_instruction(10)
    out = build_opening_prompt(
        player_id=1, turn=5,
        include_attention_instruction=True, attention_max_skip=3,
    )
    assert "<1-3>" in out and "<1-5>" not in out


def test_attention_instruction_constant_matches_default_max_skip():
    from civ_mcp.arena.prompting import attention_instruction
    from civ_mcp.arena.config import AttentionOptions

    default = AttentionOptions().max_skip
    assert ATTENTION_INSTRUCTION == attention_instruction(default)


# ---------------------------------------------------------------------------
# Local policy uses build_opening_prompt via a fake backend
# ---------------------------------------------------------------------------

class _SpyBackend:
    model = "fake"

    def __init__(self):
        self.calls = []

    async def chat(self, messages, tools):
        self.calls.append(messages)
        from civ_mcp.arena.backends import Reply
        return Reply(text="done", tool_calls=[], prompt_tokens=1, completion_tokens=1)


class _FakeCost:
    def record(self, **kw):
        pass


@pytest.mark.asyncio
async def test_local_policy_opening_uses_build_opening_prompt(monkeypatch):
    from civ_mcp.arena import agent as agent_mod
    from civ_mcp.arena.briefing import Briefing
    from civ_mcp.arena.config import BriefingOptions, CivOptions

    async def fake_build(gs, opts, budget_tokens):
        return Briefing(text="BRIEFING TEXT", tokens=2, sections=["overview"])

    monkeypatch.setattr("civ_mcp.arena.prompt_context.build_briefing", fake_build)

    be = _SpyBackend()
    opts = CivOptions(briefing=BriefingOptions(enabled=True))
    pol = agent_mod.LLMPolicy(be, _FakeCost(), options=opts)
    await pol(None, player_id=6, turn=11)

    user_msg = [m for m in be.calls[0] if m["role"] == "user"][0]
    expected = build_opening_prompt(
        player_id=6, turn=11, briefing_text="BRIEFING TEXT",
        include_standing_plan_instruction=False,
    )
    assert user_msg["content"] == expected


@pytest.mark.asyncio
async def test_local_policy_transcript_carries_prompt_injections(monkeypatch):
    from civ_mcp.arena import agent as agent_mod
    from civ_mcp.arena.config import CivOptions

    be = _SpyBackend()
    pol = agent_mod.LLMPolicy(be, _FakeCost(), options=CivOptions())
    out = await pol(None, player_id=1, turn=1)

    assert out["transcript"]["prompt_injections"] == {
        "memory": False,
        "task_tracker": False,
        "standing_plan_instruction": False,
        "digest": False,
        "attention_instruction": False,
    }


# ---------------------------------------------------------------------------
# CLI _build_argv(prompt) preserves the full prompt for both providers
# ---------------------------------------------------------------------------

def test_cli_build_argv_preserves_prompt_claude():
    from civ_mcp.arena.cli_agent import CLIAgentPolicy

    pol = CLIAgentPolicy("cli-claude", _FakeCost(), project_dir="/x", max_turns=20)
    prompt = "SOME FULL PROMPT WITH\n\nMULTIPLE LINES"
    argv = pol._build_argv(prompt)
    assert prompt in argv


def test_cli_build_argv_preserves_prompt_codex():
    from civ_mcp.arena.cli_agent import CLIAgentPolicy

    pol = CLIAgentPolicy("cli-codex", _FakeCost(), project_dir="/x", model="gpt-5.5", max_turns=20)
    prompt = "ANOTHER FULL PROMPT\n\nWITH BLOCKS"
    argv = pol._build_argv(prompt)
    assert argv[-1] == prompt
