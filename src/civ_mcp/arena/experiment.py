from __future__ import annotations

from pathlib import Path
from numbers import Integral

import yaml

from civ_mcp.arena.config import (
    DEFAULT_GATEWAY_URL,
    VALID_PLAYBOOKS,
    VALID_SECTIONS,
    ArenaConfig,
    BriefingOptions,
    CivOptions,
    PlayerSpec,
    _VALID_PROVIDERS,
)
from civ_mcp.arena.registry import resolve_tools

_LOCAL_KNOBS = (
    "tools",
    "result_char_cap",
    "max_steps",
    "playbook",
    "context_budget",
    "briefing",
)
_CIV_KEYS = {"player", "provider", "model", "gateway", *_LOCAL_KNOBS}
_TOP_KEYS = {"run_id", "max_puppet_turns", "idle_poll_limit", "gateway_url", "civs"}
_BRIEFING_DEFAULTS = BriefingOptions()
_CIV_DEFAULTS = CivOptions()
_ARENA_DEFAULTS = ArenaConfig(players=[])


class _UniqueKeySafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader: _UniqueKeySafeLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        marker = key if isinstance(key, (str, int, float, bool, type(None))) else repr(key)
        if marker in seen:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        seen.add(marker)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _err(civ_label: str, msg: str) -> ValueError:
    return ValueError(f"experiment config: {civ_label}: {msg}")


def _key_list(keys: set[object]) -> list[str]:
    return sorted(repr(key) for key in keys)


def _validate_mapping_keys(scope: str, raw: dict[object, object], valid_keys: set[str], label: str = "") -> None:
    non_string = {key for key in raw if not isinstance(key, str)}
    if non_string:
        prefix = f"{label}: " if label else ""
        raise ValueError(f"experiment config: {scope}: {prefix}non-string key(s) {_key_list(non_string)}")
    unknown = set(raw) - valid_keys
    if unknown:
        prefix = f"{label}: " if label else ""
        raise ValueError(f"experiment config: {scope}: {prefix}unknown key(s) {_key_list(unknown)}")


def _string(scope: str, field: str, value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"experiment config: {scope}: {field} must be a string, got {value!r}")
    return value


def _non_blank_string(scope: str, field: str, value: object) -> str:
    parsed = _string(scope, field, value)
    if not parsed.strip():
        raise ValueError(f"experiment config: {scope}: {field} must be a non-empty string")
    if parsed != parsed.strip():
        raise ValueError(f"experiment config: {scope}: {field} must not have leading or trailing whitespace")
    return parsed


def _int(civ_label: str, field: str, value: object) -> int:
    if isinstance(value, bool):
        raise _err(civ_label, f"{field} must be an integer, got {value!r}") from None
    if isinstance(value, Integral):
        return int(value)
    raise _err(civ_label, f"{field} must be an integer, got {value!r}") from None


def _positive_int(civ_label: str, field: str, value: object) -> int:
    parsed = _int(civ_label, field, value)
    if parsed <= 0:
        raise _err(civ_label, f"{field} must be positive")
    return parsed


def _parse_briefing(civ_label: str, raw: object) -> BriefingOptions:
    if not isinstance(raw, dict):
        raise _err(civ_label, f"briefing must be a mapping, got {raw!r}")
    _validate_mapping_keys(civ_label, raw, {"enabled", "map_radius", "sections"}, "briefing")
    enabled = raw.get("enabled", _BRIEFING_DEFAULTS.enabled)
    if "enabled" in raw and not isinstance(enabled, bool):
        raise _err(civ_label, f"briefing.enabled must be a boolean, got {enabled!r}")
    sections_raw = raw.get("sections", _BRIEFING_DEFAULTS.sections)
    if not isinstance(sections_raw, (list, tuple)):
        raise _err(civ_label, f"briefing.sections must be a list or tuple of strings, got {sections_raw!r}")
    if any(not isinstance(section, str) for section in sections_raw):
        raise _err(civ_label, f"briefing.sections must contain only strings, got {sections_raw!r}")
    sections = tuple(sections_raw)
    bad = [section for section in sections if section not in VALID_SECTIONS]
    if bad:
        raise _err(civ_label, f"unknown briefing section(s) {bad}; want {VALID_SECTIONS}")
    radius = _int(civ_label, "briefing.map_radius", raw.get("map_radius", _BRIEFING_DEFAULTS.map_radius))
    if not 0 <= radius <= 5:
        raise _err(civ_label, f"briefing.map_radius must be 0..5, got {radius}")
    return BriefingOptions(
        enabled=enabled,
        map_radius=radius,
        sections=sections,
    )


def _parse_tools(civ_label: str, raw: object) -> str | tuple[str, ...]:
    if isinstance(raw, str):
        selector: str | tuple[str, ...] = raw
    elif isinstance(raw, (list, tuple)):
        if any(not isinstance(name, str) for name in raw):
            raise _err(civ_label, f"tools must be a string tier or a list/tuple of strings, got {raw!r}")
        selector = tuple(raw)
    else:
        raise _err(civ_label, f"tools must be a string tier or a list/tuple of strings, got {raw!r}")
    try:
        resolve_tools(selector)
    except ValueError as exc:
        raise _err(civ_label, f"tools {exc}") from None
    return selector


def _parse_civ(raw: dict[object, object]) -> PlayerSpec:
    label = f"player {raw.get('player', '?')}"
    if "player" not in raw:
        raise _err(label, "missing required key 'player'")
    _validate_mapping_keys(label, raw, _CIV_KEYS)
    provider = "" if "provider" not in raw else _string(label, "provider", raw["provider"])
    if provider not in _VALID_PROVIDERS:
        raise _err(label, f"unknown provider {provider!r}; want {sorted(_VALID_PROVIDERS)}")
    player_id = _int(label, "player", raw["player"])
    model = "" if "model" not in raw else _string(label, "model", raw["model"])
    gateway = "" if "gateway" not in raw else _non_blank_string(label, "gateway", raw["gateway"])
    if provider != "local":
        present = [key for key in (*_LOCAL_KNOBS, "gateway") if key in raw]
        if present:
            raise _err(label, f"knob(s) {present} only apply to local civs, not {provider}")
        return PlayerSpec(player_id, provider, model, gateway)
    if not model.strip():
        raise _err(label, "model must be a non-empty string for local civs")
    if model != model.strip():
        raise _err(label, "model must not have leading or trailing whitespace")
    tools = _parse_tools(label, raw.get("tools", _CIV_DEFAULTS.tools))
    playbook = raw.get("playbook", _CIV_DEFAULTS.playbook)
    if playbook not in VALID_PLAYBOOKS:
        raise _err(label, f"unknown playbook {playbook!r}; want {VALID_PLAYBOOKS}")
    budget = raw.get("context_budget", _CIV_DEFAULTS.context_budget)
    if budget != "auto":
        budget = _positive_int(label, "context_budget", budget)
    cap = _positive_int(label, "result_char_cap", raw.get("result_char_cap", _CIV_DEFAULTS.result_char_cap))
    steps = _positive_int(label, "max_steps", raw.get("max_steps", _CIV_DEFAULTS.max_steps))
    opts = CivOptions(
        tools=tools,
        result_char_cap=cap,
        max_steps=steps,
        playbook=playbook,
        context_budget=budget,
        briefing=(
            _BRIEFING_DEFAULTS
            if "briefing" not in raw
            else _parse_briefing(label, raw["briefing"])
        ),
    )
    return PlayerSpec(player_id, provider, model, gateway, opts)


def _top_int(path: Path, field: str, value: object) -> int:
    if value is None:
        raise ValueError(f"experiment config {path}: {field} must be an integer, got {value!r}")
    if isinstance(value, bool):
        raise ValueError(f"experiment config {path}: {field} must be an integer, got {value!r}")
    if isinstance(value, Integral):
        parsed = int(value)
    else:
        raise ValueError(f"experiment config {path}: {field} must be an integer, got {value!r}") from None
    if parsed <= 0:
        raise ValueError(f"experiment config {path}: {field} must be positive")
    return parsed


def load_experiment(path: str | Path) -> ArenaConfig:
    config_path = Path(path)
    try:
        text = config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"experiment config {config_path}: cannot read file: {exc}") from None
    try:
        data = yaml.load(text, Loader=_UniqueKeySafeLoader)
    except yaml.YAMLError as exc:
        raise ValueError(f"experiment config {config_path}: invalid YAML: {exc}") from None
    if not isinstance(data, dict) or "civs" not in data:
        raise ValueError(f"experiment config {config_path}: want a mapping with a 'civs' list")
    _validate_mapping_keys(str(config_path), data, _TOP_KEYS, "top-level")
    civs = data["civs"]
    if not isinstance(civs, list):
        raise ValueError(f"experiment config {config_path}: 'civs' must be a list")
    if not civs:
        raise ValueError(f"experiment config {config_path}: 'civs' must contain at least one civ")
    players = []
    for civ in civs:
        if not isinstance(civ, dict):
            raise ValueError(f"experiment config {config_path}: each civ entry must be a mapping")
        players.append(_parse_civ(civ))
    ids = [player.player_id for player in players]
    if len(ids) != len(set(ids)):
        raise ValueError(f"experiment config {config_path}: duplicate player ids {ids}")
    return ArenaConfig(
        players=players,
        max_puppet_turns=_top_int(
            config_path,
            "max_puppet_turns",
            data.get("max_puppet_turns", _ARENA_DEFAULTS.max_puppet_turns),
        ),
        gateway_url=(
            DEFAULT_GATEWAY_URL
            if "gateway_url" not in data
            else _non_blank_string(str(config_path), "gateway_url", data["gateway_url"])
        ),
        idle_poll_limit=_top_int(
            config_path,
            "idle_poll_limit",
            data.get("idle_poll_limit", _ARENA_DEFAULTS.idle_poll_limit),
        ),
        puppet_ids=ids,
        run_id=(
            _ARENA_DEFAULTS.run_id
            if "run_id" not in data
            else _string(str(config_path), "run_id", data["run_id"])
        ),
    )
