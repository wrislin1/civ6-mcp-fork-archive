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
    "great_people",
    "rivals",
    "threats",
    "victory",
)

VALID_PLAYBOOKS = ("none", "condensed")

STANDING_PLAN_CAPTURE_CHARS = 4000
STANDING_PLAN_BASE_TASK_CAP = 8
STANDING_PLAN_CHARS_PER_EXTRA_TASK = 120

CLI_PROVIDER_COMMANDS = {"cli-claude": "claude", "cli-codex": "codex"}
_CLI_PROVIDERS = set(CLI_PROVIDER_COMMANDS)
_VALID_PROVIDERS = {"local"} | _CLI_PROVIDERS

@dataclass(frozen=True)
class BriefingOptions:
    enabled: bool = False
    map_radius: int = 3
    sections: tuple[str, ...] = ("overview", "units", "cities", "map", "research", "production_options")

@dataclass(frozen=True)
class MemoryOptions:
    enabled: bool = False
    max_chars: int = 1200
    max_age_turns: int = 10


@dataclass(frozen=True)
class TaskTrackerOptions:
    enabled: bool = False
    max_tasks: int = 8

@dataclass(frozen=True)
class AttentionOptions:
    """Quiet-turn attention policy (spec 2026-07-09). mode: off|auto|model|hybrid."""
    mode: str = "off"
    max_skip: int = 5        # upper clamp for a model's SKIP: n
    max_streak: int = 5      # coordinator-side consecutive-sleep cap
    threat_radius: int = 4   # hostile-scan radius around cities/civilians

@dataclass(frozen=True)
class CivOptions:
    tools: str | tuple = "minimal"
    result_char_cap: int = 1500
    max_steps: int = 6
    playbook: str = "none"
    context_budget: int | str = "auto"
    briefing: BriefingOptions = field(default_factory=BriefingOptions)
    memory: MemoryOptions = field(default_factory=MemoryOptions)
    task_tracker: TaskTrackerOptions = field(default_factory=TaskTrackerOptions)
    attention: AttentionOptions = field(default_factory=AttentionOptions)

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
            "memory": {
                "enabled": self.memory.enabled,
                "max_chars": self.memory.max_chars,
                "max_age_turns": self.memory.max_age_turns,
            },
            "task_tracker": {"enabled": self.task_tracker.enabled, "max_tasks": self.task_tracker.max_tasks},
            "attention": {
                "mode": self.attention.mode,
                "max_skip": self.attention.max_skip,
                "max_streak": self.attention.max_streak,
                "threat_radius": self.attention.threat_radius,
            },
        }

    @property
    def standing_plan_enabled(self) -> bool:
        return self.memory.enabled or self.task_tracker.enabled

    @property
    def attention_directives_enabled(self) -> bool:
        return self.attention.mode in ("model", "hybrid")

    @property
    def _standing_plan_task_capture_chars(self) -> int:
        if not self.task_tracker.enabled:
            return 0
        extra_tasks = max(0, self.task_tracker.max_tasks - STANDING_PLAN_BASE_TASK_CAP)
        return STANDING_PLAN_CAPTURE_CHARS + (
            extra_tasks * STANDING_PLAN_CHARS_PER_EXTRA_TASK
        )

    @property
    def standing_plan_capture_chars(self) -> int:
        if not self.standing_plan_enabled:
            return 0
        capture_chars = self.memory.max_chars if self.memory.enabled else 0
        if self.task_tracker.enabled:
            capture_chars = max(capture_chars, self._standing_plan_task_capture_chars)
        return capture_chars

    @property
    def standing_plan_summary_chars(self) -> int:
        if not self.standing_plan_enabled:
            return 500
        return max(1200, self.standing_plan_capture_chars)

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
    max_game_turns: int = 0  # caps ALL captured turns (played+slept+failed); 0 = uncapped
    gateway_url: str = DEFAULT_GATEWAY_URL  # overridden by CLI
    api_key_env: str = "LITELLM_OPENAI_API_KEY"
    dry_run: bool = False
    max_agent_steps: int = 6
    idle_poll_limit: int = 600
    cost_path: str = "arena_cost.jsonl"
    puppet_ids: list[int] = field(default_factory=list)
    run_id: str = ""
    transcript_dir: str = "arena_runs"
