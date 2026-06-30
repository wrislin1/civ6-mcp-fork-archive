from __future__ import annotations
from dataclasses import dataclass, field

# Canonical in-process LLM gateway endpoint; single source of truth for both the
# ArenaConfig default and the --gateway-url CLI default.
DEFAULT_GATEWAY_URL = "http://192.168.20.196:11430/v1"

_CLI_PROVIDERS = {"cli-claude"}
_VALID_PROVIDERS = {"local"} | _CLI_PROVIDERS

@dataclass(frozen=True)
class PlayerSpec:
    player_id: int
    provider: str  # "local" | "cli-claude"
    model: str

    def driver_kind(self) -> str:
        return "cli" if self.provider in _CLI_PROVIDERS else "in_process"

def parse_player_spec(s: str) -> PlayerSpec:
    # "1:local:qwen3-coder:30b" or "2:cli-claude:"
    parts = s.split(":", 2)
    if len(parts) != 3:
        raise ValueError(f"bad --player spec {s!r}; want '<id>:<provider>:<model>'")
    pid, provider, model = parts
    if provider not in _VALID_PROVIDERS:
        raise ValueError(
            f"unknown provider {provider!r} in --player spec {s!r}; "
            f"want one of {sorted(_VALID_PROVIDERS)}")
    return PlayerSpec(int(pid), provider, model)

@dataclass
class ArenaConfig:
    players: list[PlayerSpec]
    max_puppet_turns: int = 1
    gateway_url: str = DEFAULT_GATEWAY_URL  # overridden by CLI
    api_key_env: str = "LITELLM_OPENAI_API_KEY"
    dry_run: bool = False
    max_agent_steps: int = 6
    cost_path: str = "arena_cost.jsonl"
    puppet_ids: list[int] = field(default_factory=list)
