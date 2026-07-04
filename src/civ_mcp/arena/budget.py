"""Resolve local model context windows and briefing token budgets."""
from __future__ import annotations

from typing import Any

DEFAULT_N_CTX = 16384
CHARS_PER_TOKEN = 3

_COMPLETION_RESERVE_PER_STEP = 512
_MARGIN_TOKENS = 1024


async def _default_http_get(url: str) -> dict[str, Any] | None:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url)
        if response.status_code != 200:
            return None
        payload = response.json()
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _n_ctx_from(payload: dict[str, Any] | None) -> int | None:
    if payload is None:
        return None
    settings = payload.get("default_generation_settings")
    if not isinstance(settings, dict):
        return None
    try:
        n_ctx = int(settings["n_ctx"])
    except (TypeError, KeyError, ValueError):
        return None
    return n_ctx if n_ctx > 0 else None


async def resolve_n_ctx(
    base_url: str,
    model: str,
    context_budget: int | str,
    http_get=None,
) -> tuple[int, str]:
    if isinstance(context_budget, int):
        return context_budget, "explicit"

    get = http_get or _default_http_get
    origin = base_url.rstrip("/")
    if origin.endswith("/v1"):
        origin = origin[:-3]

    n_ctx = _n_ctx_from(await get(f"{origin}/upstream/{model}/props"))
    if n_ctx is not None:
        return n_ctx, "upstream_props"

    n_ctx = _n_ctx_from(await get(f"{origin}/props"))
    if n_ctx is not None:
        return n_ctx, "props"

    return DEFAULT_N_CTX, "default"


def briefing_budget(
    n_ctx: int,
    options,
    playbook_chars: int,
    tool_schema_chars: int,
) -> int:
    reserve = (
        playbook_chars // CHARS_PER_TOKEN
        + tool_schema_chars // CHARS_PER_TOKEN
        + options.max_steps
        * (options.result_char_cap // CHARS_PER_TOKEN + _COMPLETION_RESERVE_PER_STEP)
        + _MARGIN_TOKENS
    )
    return max(n_ctx - reserve, 0)
