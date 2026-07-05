from __future__ import annotations
from dataclasses import dataclass, field

# Canonical in-process LLM gateway endpoint; single source of truth for both the
# ArenaConfig default and the --gateway-url CLI default.
DEFAULT_GATEWAY_URL = "http://192.168.20.196:11444/v1"

VALID_SECTIONS = (
    "promotions",
    "overview",
    "units",
    "cities",
    "map",
    "research",
    "production_options",
    "empire_resources",
    "rivals",
    "threats",
    "victory",
)

VALID_PLAYBOOKS = ("none", "condensed")

CLI_PROVIDER_COMMANDS = {"cli-claude": "claude", "cli-codex": "codex"}
_CLI_PROVIDERS = set(CLI_PROVIDER_COMMANDS)
_VALID_PROVIDERS = {"local"} | _CLI_PROVIDERS

@dataclass(frozen=True)
class BriefingOptions:
    enabled: bool = False
    map_radius: int = 3
    sections: tuple[str, ...] = ("overview", "units", "cities", "map", "research", "production_options")

@dataclass(frozen=True)
class CivOptions:
    tools: str | tuple = "minimal"
    result_char_cap: int = 1500
    max_steps: int = 6
    playbook: str = "none"
    context_budget: int | str = "auto"
    briefing: BriefingOptions = field(default_factory=BriefingOptions)

    def fingerprint(self) -> dict:
        return {
            "tools": list(self.tools) if not isinstance(self.tools, str) else self.tools,
            "result_char_cap": self.result_char_cap,
            "max_steps": self.max_steps,
            "playbook": self.playbook,
            "context_budget": self.context_budget,
            "briefing": {
                "enabled": self.briefing.enabled,
                "map_radius": self.briefing.map_radius,
                "sections": list(self.briefing.sections),
            },
        }

@dataclass(frozen=True)
class PlayerSpec:
    player_id: int
    provider: str  # "local" | "cli-claude" | "cli-codex"
    model: str
    gateway: str = ""  # optional per-civ gateway override (in-process local civs only)
    options: CivOptions = field(default_factory=CivOptions)

    def driver_kind(self) -> str:
        return "cli" if self.provider in _CLI_PROVIDERS else "in_process"

def parse_player_spec(s: str) -> PlayerSpec:
    # "1:local:qwen3-coder:30b", "2:cli-claude:", "2:cli-codex:gpt-5.5", or a local civ
    # pinned to its own gateway: "3:local:gemma4-26b@http://192.168.20.196:11440/v1".
    parts = s.split(":", 2)
    if len(parts) != 3:
        raise ValueError(f"bad --player spec {s!r}; want '<id>:<provider>:<model>[@<gateway>]'")
    pid, provider, model = parts
    if provider not in _VALID_PROVIDERS:
        raise ValueError(
            f"unknown provider {provider!r} in --player spec {s!r}; "
            f"want one of {sorted(_VALID_PROVIDERS)}")
    # A trailing '@<url>' pins this local civ to a specific gateway (e.g. a per-GPU
    # llama-swap instance). URLs contain ':' but not '@', so rsplit is unambiguous.
    gateway = ""
    if "@" in model:
        model, gateway = model.rsplit("@", 1)
    return PlayerSpec(int(pid), provider, model, gateway)

@dataclass
class ArenaConfig:
    players: list[PlayerSpec]
    max_puppet_turns: int = 1
    gateway_url: str = DEFAULT_GATEWAY_URL  # overridden by CLI
    api_key_env: str = "LITELLM_OPENAI_API_KEY"
    dry_run: bool = False
    max_agent_steps: int = 6
    idle_poll_limit: int = 600
    cost_path: str = "arena_cost.jsonl"
    puppet_ids: list[int] = field(default_factory=list)
    run_id: str = ""
    transcript_dir: str = "arena_runs"
