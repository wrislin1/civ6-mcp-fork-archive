from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
import openai
from openai import AsyncOpenAI

# A turn-step is "reason, then emit one tool call". Observed legit steps reach
# ~1,900 completion tokens; one live step hit the old 3072 cap (truncated tool
# JSON -> gateway 500 -> the 37a48ef crash). 6144 is ~3x observed max. At local
# speeds (~25-35 tok/s on a 3090) a full 6144-token generation runs 3-4 minutes,
# so the timeout rises with it: the token cap, not the clock, bounds a legit
# long step. A timeout at this cap means runaway generation - it is re-raised
# without retry (see chat()) so one seat stalls at most ~5 min before the
# coordinator's degrade guard skips the turn.
MAX_COMPLETION_TOKENS = 6144
REQUEST_TIMEOUT_S = 300.0

# A single chat step can fail transiently: the gateway 500s on a malformed/truncated
# tool call (which at temp>0 usually differs when resampled), llama-swap 503s while it
# loads the model, or a network blip drops the request. A bounded retry recovers these
# without falling through to the coordinator's skip-the-turn guard. A PERSISTENT failure
# exhausts the retries and re-raises, so the coordinator still degrades that one turn
# rather than the run. Kept small so a truly-wedged upstream is surfaced quickly.
MAX_ATTEMPTS = 3
RETRY_BACKOFF_S = 1.0

@dataclass
class Reply:
    text: str | None
    tool_calls: list[dict] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0

class OpenAICompatBackend:
    def __init__(self, base_url: str, api_key: str, model: str):
        self.model = model
        self.base_url = base_url
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key)

    async def chat(self, messages: list[dict], tools: list[dict]) -> Reply:
        kw = dict(
            model=self.model,
            messages=messages,
            max_tokens=MAX_COMPLETION_TOKENS,
            timeout=REQUEST_TIMEOUT_S,
        )
        if tools:
            kw["tools"] = tools
            kw["tool_choice"] = "auto"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = await self._client.chat.completions.create(**kw)
                break
            except openai.APITimeoutError:
                # Runaway generation, not a transient: resampling would repeat it.
                raise
            except Exception:
                if attempt >= MAX_ATTEMPTS:
                    raise
                await asyncio.sleep(RETRY_BACKOFF_S * attempt)
        msg = resp.choices[0].message
        tcs = []
        for tc in (msg.tool_calls or []):
            tcs.append({"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments})
        u = resp.usage
        return Reply(text=msg.content, tool_calls=tcs,
                     prompt_tokens=getattr(u, "prompt_tokens", 0),
                     completion_tokens=getattr(u, "completion_tokens", 0))

    async def reachable(self) -> bool:
        try:
            await self._client.models.list()
            return True
        except Exception:
            return False
