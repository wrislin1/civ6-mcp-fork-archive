import pytest

from civ_mcp.arena.budget import (
    CHARS_PER_TOKEN,
    DEFAULT_N_CTX,
    briefing_budget,
    resolve_n_ctx,
)
from civ_mcp.arena.config import CivOptions


def test_constants_are_exposed():
    assert DEFAULT_N_CTX == 16384
    assert CHARS_PER_TOKEN == 3


@pytest.mark.asyncio
async def test_explicit_budget_skips_probe():
    async def boom(url):
        raise AssertionError("must not probe")

    assert await resolve_n_ctx("http://h:1/v1", "m", 65536, http_get=boom) == (
        65536,
        "explicit",
    )


@pytest.mark.asyncio
async def test_auto_uses_upstream_props_first():
    seen = []

    async def fake(url):
        seen.append(url)
        if "/upstream/" in url:
            return {"default_generation_settings": {"n_ctx": 131072}}
        return None

    n, src = await resolve_n_ctx("http://h:11440/v1", "gemma4-26b", "auto", http_get=fake)

    assert (n, src) == (131072, "upstream_props")
    assert seen == ["http://h:11440/upstream/gemma4-26b/props"]


@pytest.mark.asyncio
async def test_auto_falls_back_to_bare_props_then_default():
    async def only_bare(url):
        if url == "http://h:1/props":
            return {"default_generation_settings": {"n_ctx": 32768}}
        return None

    n, src = await resolve_n_ctx("http://h:1/v1", "m", "auto", http_get=only_bare)
    assert (n, src) == (32768, "props")

    async def nothing(url):
        return None

    n, src = await resolve_n_ctx("http://h:1/v1", "m", "auto", http_get=nothing)
    assert (n, src) == (DEFAULT_N_CTX, "default")


def test_briefing_budget_formula():
    opts = CivOptions(max_steps=10, result_char_cap=6000)

    got = briefing_budget(131072, opts, playbook_chars=12000, tool_schema_chars=4000)

    reserve = 12000 // 3 + 4000 // 3 + 10 * (6000 // 3 + 512) + 1024
    assert got == 131072 - reserve


def test_briefing_budget_floors_at_zero():
    opts = CivOptions(max_steps=50, result_char_cap=20000)

    assert briefing_budget(8192, opts, playbook_chars=0, tool_schema_chars=0) == 0
