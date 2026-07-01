from __future__ import annotations
from dataclasses import dataclass, field
from openai import AsyncOpenAI

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
        kw = dict(model=self.model, messages=messages)
        if tools:
            kw["tools"] = tools
            kw["tool_choice"] = "auto"
        resp = await self._client.chat.completions.create(**kw)
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
