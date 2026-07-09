from pathlib import Path

import pytest

from civ_mcp.arena.config import CivOptions, MemoryOptions, TaskTrackerOptions
from civ_mcp.arena.experiment import load_experiment
from civ_mcp.arena.registry import resolve_tools


REPO_ROOT = Path(__file__).resolve().parents[2]
SLICE1_GEMMA_STRATEGY_AB = REPO_ROOT / "experiments" / "gemma-strategy-ab-slice1.yaml"
SLICE3_BEHAVIOR_3LLM = REPO_ROOT / "experiments" / "arena-behavior-3llm-slice3.yaml"

GOOD = """
run_id: exp-1
max_puppet_turns: 80
idle_poll_limit: 3600
gateway_url: http://gw:11444/v1
civs:
  - player: 3
    provider: local
    model: gemma4-26b
    gateway: http://gw:11440/v1
    tools: standard
    result_char_cap: 6000
    max_steps: 10
    playbook: condensed
    context_budget: auto
    briefing: {enabled: true, map_radius: 4, sections: [overview, units, map]}
  - player: 1
    provider: cli-claude
    model: ""
"""


def _write(tmp_path, text):
    p = tmp_path / "exp.yaml"
    p.write_text(text)
    return p


def test_loads_gemma_strategy_ab_slice1_artifact():
    cfg = load_experiment(SLICE1_GEMMA_STRATEGY_AB)

    assert cfg.run_id == ""
    assert cfg.max_puppet_turns == 140
    assert cfg.idle_poll_limit == 3600
    assert cfg.puppet_ids == [1, 2, 3, 4, 5, 6, 7]

    by_player = {player.player_id: player for player in cfg.players}
    assert set(by_player) == {1, 2, 3, 4, 5, 6, 7}

    gateway = "http://192.168.20.196:11440/v1"
    treatment_sections = (
        "promotions",
        "overview",
        "units",
        "cities",
        "map",
        "research",
        "production_options",
        "threats",
        "rivals",
        "empire_resources",
    )

    for player_id in (1, 3, 5, 7):
        player = by_player[player_id]
        assert player.provider == "local"
        assert player.model == "gemma4-26b"
        assert player.gateway == gateway
        assert player.options.tools == "full"
        assert player.options.result_char_cap == 6000
        assert player.options.max_steps == 10
        assert player.options.playbook == "condensed"
        assert player.options.context_budget == "auto"
        assert player.options.briefing.enabled is True
        assert player.options.briefing.map_radius == 3
        assert player.options.briefing.sections == treatment_sections
        assert "victory" not in player.options.briefing.sections

    for player_id in (2, 4, 6):
        player = by_player[player_id]
        assert player.provider == "local"
        assert player.model == "gemma4-26b"
        assert player.gateway == gateway
        assert player.options.tools == "minimal"
        assert player.options.result_char_cap == 1500
        assert player.options.max_steps == 6
        assert player.options.playbook == "none"
        assert player.options.context_budget == "auto"
        assert player.options.briefing.enabled is False


def test_slice1_treatment_full_tier_has_diplomacy_tools_and_control_does_not():
    cfg = load_experiment(SLICE1_GEMMA_STRATEGY_AB)
    by_player = {player.player_id: player for player in cfg.players}
    diplomacy_tools = {
        "get_pending_diplomacy",
        "respond_to_diplomacy",
        "get_pending_trades",
        "respond_to_trade",
        "get_trade_options",
        "propose_trade",
        "propose_peace",
        "send_diplomatic_action",
        "form_alliance",
    }

    for player_id in (1, 3, 5, 7):
        assert diplomacy_tools <= set(resolve_tools(by_player[player_id].options.tools))

    for player_id in (2, 4, 6):
        assert diplomacy_tools.isdisjoint(set(resolve_tools(by_player[player_id].options.tools)))


def test_loads_arena_behavior_3llm_slice3_artifact():
    assert SLICE3_BEHAVIOR_3LLM.exists(), f"missing fixture: {SLICE3_BEHAVIOR_3LLM}"

    cfg = load_experiment(SLICE3_BEHAVIOR_3LLM)

    assert len(cfg.players) == 3
    assert [player.player_id for player in cfg.players] == [1, 3, 5]

    for player in cfg.players:
        assert player.options.memory.enabled is True
        assert player.options.task_tracker.enabled is True
        assert player.options.briefing.enabled is True
        assert "great_people" in player.options.briefing.sections


def test_playbook_covers_promotions_and_expansion_doctrine():
    text = (REPO_ROOT / "src" / "civ_mcp" / "arena" / "playbook.md").read_text()

    for header in ("## Unit promotions", "## Unit upgrades", "## Signals to watch"):
        assert header in text
    assert "promotions briefing appears" in text
    assert "get_unit_promotions(unit_id).promotions" in text


def test_playbook_covers_diplomacy_trade_and_peace_doctrine():
    text = (REPO_ROOT / "src" / "civ_mcp" / "arena" / "playbook.md").read_text()

    assert "## Diplomacy, trades, and peace" in text
    assert "get_pending_diplomacy" in text
    assert "respond_to_diplomacy" in text
    assert "get_pending_trades" in text
    assert "respond_to_trade" in text
    assert "get_trade_options" in text
    assert "propose_trade" in text
    assert "propose_peace" in text
    assert "form_alliance" in text
    assert "send_diplomatic_action" in text
    assert "DIPLOMATIC_DELEGATION" in text
    assert "DECLARE_FRIENDSHIP" in text
    assert "RESIDENT_EMBASSY" in text
    assert "DECLARE_SURPRISE_WAR" in text


def test_load_good(tmp_path):
    cfg = load_experiment(_write(tmp_path, GOOD))
    assert cfg.run_id == "exp-1" and cfg.max_puppet_turns == 80
    assert cfg.gateway_url == "http://gw:11444/v1"
    assert cfg.puppet_ids == [3, 1]
    local = cfg.players[0]
    assert local.gateway == "http://gw:11440/v1"
    assert local.options.tools == "standard"
    assert local.options.max_steps == 10
    assert local.options.briefing.enabled and local.options.briefing.map_radius == 4
    assert local.options.briefing.sections == ("overview", "units", "map")
    cli = cfg.players[1]
    assert cli.provider == "cli-claude" and cli.options == CivOptions()


def test_briefing_accepts_great_people_section(tmp_path):
    text = GOOD.replace(
        "sections: [overview, units, map]",
        "sections: [overview, units, map, great_people]",
    )

    cfg = load_experiment(_write(tmp_path, text))

    assert cfg.players[0].options.briefing.sections == (
        "overview",
        "units",
        "map",
        "great_people",
    )


def test_non_empty_briefing_block_defaults_enabled_true(tmp_path):
    text = GOOD.replace(
        "briefing: {enabled: true, map_radius: 4, sections: [overview, units, map]}",
        "briefing: {map_radius: 4, sections: [overview, map, rivals]}",
    )

    cfg = load_experiment(_write(tmp_path, text))
    briefing = cfg.players[0].options.briefing

    assert briefing.enabled is True
    assert briefing.map_radius == 4
    assert briefing.sections == ("overview", "map", "rivals")


def test_briefing_block_explicit_enabled_false_stays_disabled(tmp_path):
    text = GOOD.replace(
        "briefing: {enabled: true, map_radius: 4, sections: [overview, units, map]}",
        "briefing: {enabled: false, map_radius: 4, sections: [overview, map]}",
    )

    cfg = load_experiment(_write(tmp_path, text))
    briefing = cfg.players[0].options.briefing

    assert briefing.enabled is False
    assert briefing.map_radius == 4
    assert briefing.sections == ("overview", "map")


def test_load_experiment_uses_supplied_defaults_for_omitted_run_controls(tmp_path):
    from civ_mcp.arena.config import ArenaConfig

    p = _write(
        tmp_path,
        """
civs:
  - player: 3
    provider: local
    model: gemma4-26b
""",
    )
    cfg = load_experiment(
        p,
        defaults=ArenaConfig(
            players=[],
            max_puppet_turns=8,
            idle_poll_limit=3600,
            gateway_url="http://launcher.example/v1",
        ),
    )

    assert cfg.max_puppet_turns == 8
    assert cfg.idle_poll_limit == 3600
    assert cfg.gateway_url == "http://launcher.example/v1"


def test_load_experiment_preserves_all_supplied_defaults_for_omitted_arena_fields(tmp_path):
    from civ_mcp.arena.config import ArenaConfig

    p = _write(
        tmp_path,
        """
civs:
  - player: 3
    provider: local
    model: gemma4-26b
""",
    )

    cfg = load_experiment(
        p,
        defaults=ArenaConfig(
            players=[],
            max_puppet_turns=8,
            gateway_url="http://launcher.example/v1",
            api_key_env="LOCAL_ARENA_KEY",
            dry_run=True,
            max_agent_steps=3,
            idle_poll_limit=3600,
            cost_path="custom-cost.jsonl",
            run_id="default-run",
            transcript_dir="custom-runs",
        ),
    )

    assert [p.player_id for p in cfg.players] == [3]
    assert cfg.puppet_ids == [3]
    assert cfg.max_puppet_turns == 8
    assert cfg.gateway_url == "http://launcher.example/v1"
    assert cfg.api_key_env == "LOCAL_ARENA_KEY"
    assert cfg.dry_run is True
    assert cfg.max_agent_steps == 3
    assert cfg.idle_poll_limit == 3600
    assert cfg.cost_path == "custom-cost.jsonl"
    assert cfg.run_id == "default-run"
    assert cfg.transcript_dir == "custom-runs"


def test_load_experiment_yaml_values_override_supplied_defaults(tmp_path):
    from civ_mcp.arena.config import ArenaConfig

    p = _write(
        tmp_path,
        """
max_puppet_turns: 12
idle_poll_limit: 7200
gateway_url: http://yaml.example/v1
civs:
  - player: 3
    provider: local
    model: gemma4-26b
""",
    )
    cfg = load_experiment(
        p,
        defaults=ArenaConfig(
            players=[],
            max_puppet_turns=8,
            idle_poll_limit=3600,
            gateway_url="http://launcher.example/v1",
        ),
    )

    assert cfg.max_puppet_turns == 12
    assert cfg.idle_poll_limit == 7200
    assert cfg.gateway_url == "http://yaml.example/v1"


def test_rejects_duplicate_players(tmp_path):
    bad = GOOD.replace("player: 1", "player: 3")
    with pytest.raises(ValueError, match="duplicate"):
        load_experiment(_write(tmp_path, bad))


def test_rejects_empty_civ_list(tmp_path):
    with pytest.raises(ValueError, match=r"civs.*at least one"):
        load_experiment(_write(tmp_path, "civs: []\n"))


def test_rejects_unknown_tier(tmp_path):
    with pytest.raises(ValueError, match=r"player 3.*tools"):
        load_experiment(_write(tmp_path, GOOD.replace("tools: standard", "tools: mega")))


def test_rejects_unknown_tool_name_in_list(tmp_path):
    with pytest.raises(ValueError, match=r"player 3.*tools"):
        load_experiment(
            _write(
                tmp_path,
                GOOD.replace("tools: standard", "tools: [get_units, launch_nuke]"),
            )
        )


def test_rejects_unknown_section(tmp_path):
    with pytest.raises(ValueError, match="player 3"):
        load_experiment(
            _write(
                tmp_path,
                GOOD.replace("[overview, units, map]", "[overview, minimap]"),
            )
        )


def test_rejects_local_knobs_on_cli_civ(tmp_path):
    bad = GOOD + "    max_steps: 9\n"
    with pytest.raises(ValueError, match="cli-claude"):
        load_experiment(_write(tmp_path, bad))


def test_explicit_tool_list(tmp_path):
    cfg = load_experiment(
        _write(tmp_path, GOOD.replace("tools: standard", "tools: [get_units, move_unit]"))
    )
    assert cfg.players[0].options.tools == ("get_units", "move_unit")


def test_rejects_missing_player_key(tmp_path):
    with pytest.raises(ValueError, match="player"):
        load_experiment(_write(tmp_path, "civs:\n  - {provider: local, model: m}\n"))


def test_rejects_missing_local_model(tmp_path):
    text = """
civs:
  - player: 3
    provider: local
"""
    with pytest.raises(ValueError, match=r"player 3.*model"):
        load_experiment(_write(tmp_path, text))


def test_rejects_empty_local_model(tmp_path):
    with pytest.raises(ValueError, match=r"player 3.*model"):
        load_experiment(_write(tmp_path, GOOD.replace("model: gemma4-26b", 'model: ""')))


def test_rejects_whitespace_local_model(tmp_path):
    with pytest.raises(ValueError, match=r"player 3.*model"):
        load_experiment(_write(tmp_path, GOOD.replace("model: gemma4-26b", 'model: "   "')))


def test_rejects_surrounding_whitespace_local_model(tmp_path):
    with pytest.raises(ValueError, match=r"player 3.*model"):
        load_experiment(_write(tmp_path, GOOD.replace("model: gemma4-26b", 'model: " gemma4-26b "')))


@pytest.mark.parametrize(
    ("good", "bad", "field"),
    [
        ("max_steps: 10", "max_steps: nope", "max_steps"),
        ("context_budget: auto", "context_budget: nope", "context_budget"),
        ("map_radius: 4", "map_radius: nope", "briefing.map_radius"),
    ],
)
def test_rejects_malformed_ints_with_civ_named(tmp_path, good, bad, field):
    # bare int() would raise "invalid literal..." without naming the civ or field
    with pytest.raises(ValueError, match=f"player 3.*{field}"):
        load_experiment(_write(tmp_path, GOOD.replace(good, bad)))


def test_rejects_out_of_range_map_radius(tmp_path):
    with pytest.raises(ValueError, match="map_radius must be 0..5"):
        load_experiment(_write(tmp_path, GOOD.replace("map_radius: 4", "map_radius: 9")))


def test_rejects_non_positive_result_char_cap_with_civ_named(tmp_path):
    with pytest.raises(ValueError, match=r"player 3: result_char_cap must be positive$"):
        load_experiment(_write(tmp_path, GOOD.replace("result_char_cap: 6000", "result_char_cap: 0")))


def test_rejects_non_positive_max_steps_with_civ_named(tmp_path):
    with pytest.raises(ValueError, match=r"player 3: max_steps must be positive$"):
        load_experiment(_write(tmp_path, GOOD.replace("max_steps: 10", "max_steps: 0")))


def test_rejects_boolean_player_id(tmp_path):
    with pytest.raises(ValueError, match=r"player .*player"):
        load_experiment(_write(tmp_path, GOOD.replace("player: 3", "player: true", 1)))


def test_rejects_boolean_max_steps(tmp_path):
    with pytest.raises(ValueError, match=r"player 3.*max_steps"):
        load_experiment(_write(tmp_path, GOOD.replace("max_steps: 10", "max_steps: true")))


def test_rejects_boolean_context_budget(tmp_path):
    with pytest.raises(ValueError, match=r"player 3.*context_budget"):
        load_experiment(_write(tmp_path, GOOD.replace("context_budget: auto", "context_budget: true")))


@pytest.mark.parametrize("field", ["max_puppet_turns", "idle_poll_limit"])
def test_rejects_boolean_top_level_ints(tmp_path, field):
    with pytest.raises(ValueError, match=field):
        load_experiment(_write(tmp_path, GOOD.replace(f"{field}: ", f"{field}: true # ")))


@pytest.mark.parametrize("field", ["max_puppet_turns", "idle_poll_limit"])
def test_rejects_null_top_level_ints(tmp_path, field):
    with pytest.raises(ValueError, match=field):
        load_experiment(_write(tmp_path, GOOD.replace(f"{field}: ", f"{field}: null # ")))


@pytest.mark.parametrize("bad", ["briefing: []", "briefing: false"])
def test_rejects_non_mapping_briefing(tmp_path, bad):
    with pytest.raises(ValueError, match=r"player 3.*briefing"):
        load_experiment(
            _write(
                tmp_path,
                GOOD.replace(
                    "briefing: {enabled: true, map_radius: 4, sections: [overview, units, map]}",
                    bad,
                ),
            )
        )


def test_rejects_null_briefing(tmp_path):
    with pytest.raises(ValueError, match=r"player 3.*briefing"):
        load_experiment(
            _write(
                tmp_path,
                GOOD.replace(
                    "briefing: {enabled: true, map_radius: 4, sections: [overview, units, map]}",
                    "briefing: null",
                ),
            )
        )


def test_rejects_non_boolean_briefing_enabled(tmp_path):
    with pytest.raises(ValueError, match=r"player 3.*briefing.enabled"):
        load_experiment(_write(tmp_path, GOOD.replace("enabled: true", 'enabled: "false"')))


@pytest.mark.parametrize("bad", ["sections: overview", "sections: [overview, 2]"])
def test_rejects_bad_briefing_sections_shape(tmp_path, bad):
    with pytest.raises(ValueError, match=r"player 3.*briefing.sections"):
        load_experiment(
            _write(
                tmp_path,
                GOOD.replace("sections: [overview, units, map]", bad),
            )
        )


def test_rejects_non_string_or_sequence_tools(tmp_path):
    with pytest.raises(ValueError, match=r"player 3.*tools"):
        load_experiment(_write(tmp_path, GOOD.replace("tools: standard", "tools: 5")))


@pytest.mark.parametrize(
    ("good", "bad", "field"),
    [
        ("max_steps: 10", "max_steps: 1.5", "max_steps"),
        ("context_budget: auto", "context_budget: 1.5", "context_budget"),
        ("result_char_cap: 6000", "result_char_cap: 1.5", "result_char_cap"),
        ("map_radius: 4", "map_radius: 1.5", "briefing.map_radius"),
    ],
)
def test_rejects_floats_for_civ_int_fields(tmp_path, good, bad, field):
    with pytest.raises(ValueError, match=rf"player 3.*{field}"):
        load_experiment(_write(tmp_path, GOOD.replace(good, bad)))


@pytest.mark.parametrize(
    ("good", "bad", "field"),
    [
        ("max_steps: 10", "max_steps: null", "max_steps"),
        ("context_budget: auto", "context_budget: null", "context_budget"),
        ("result_char_cap: 6000", "result_char_cap: null", "result_char_cap"),
        ("map_radius: 4", "map_radius: null", "briefing.map_radius"),
    ],
)
def test_rejects_nulls_for_civ_int_fields(tmp_path, good, bad, field):
    with pytest.raises(ValueError, match=rf"player 3.*{field}"):
        load_experiment(_write(tmp_path, GOOD.replace(good, bad)))


@pytest.mark.parametrize("field,bad", [("max_puppet_turns", "2.7"), ("idle_poll_limit", "2.7")])
def test_rejects_floats_for_top_level_int_fields(tmp_path, field, bad):
    with pytest.raises(ValueError, match=field):
        load_experiment(_write(tmp_path, GOOD.replace(f"{field}: ", f"{field}: {bad} # ")))


@pytest.mark.parametrize(
    ("good", "bad", "field"),
    [
        ("max_steps: 10", 'max_steps: "10"', "max_steps"),
        ("context_budget: auto", 'context_budget: "10"', "context_budget"),
        ("result_char_cap: 6000", 'result_char_cap: "6000"', "result_char_cap"),
    ],
)
def test_rejects_quoted_numeric_strings_for_civ_int_fields(tmp_path, good, bad, field):
    with pytest.raises(ValueError, match=rf"player 3.*{field}"):
        load_experiment(_write(tmp_path, GOOD.replace(good, bad)))


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("max_puppet_turns", '"80"'),
        ("idle_poll_limit", '"3600"'),
    ],
)
def test_rejects_quoted_numeric_strings_for_top_level_int_fields(tmp_path, field, bad):
    with pytest.raises(ValueError, match=field):
        load_experiment(_write(tmp_path, GOOD.replace(f"{field}: ", f"{field}: {bad} # ")))


def test_rejects_float_player_id(tmp_path):
    with pytest.raises(ValueError, match="player"):
        load_experiment(_write(tmp_path, GOOD.replace("player: 3", "player: 3.5", 1)))


def test_rejects_gateway_on_cli_civ(tmp_path):
    bad = GOOD + "    gateway: http://gw:11441/v1\n"
    with pytest.raises(ValueError, match=r"(cli-claude.*gateway|player 1.*gateway)"):
        load_experiment(_write(tmp_path, bad))


def test_rejects_non_string_model(tmp_path):
    with pytest.raises(ValueError, match=r"player 3.*model"):
        load_experiment(_write(tmp_path, GOOD.replace("model: gemma4-26b", "model: 123")))


def test_rejects_non_string_gateway(tmp_path):
    with pytest.raises(ValueError, match=r"player 3.*gateway"):
        load_experiment(_write(tmp_path, GOOD.replace("gateway: http://gw:11440/v1", "gateway: 123")))


def test_rejects_whitespace_local_gateway(tmp_path):
    with pytest.raises(ValueError, match=r"player 3.*gateway"):
        load_experiment(_write(tmp_path, GOOD.replace("gateway: http://gw:11440/v1", 'gateway: "   "')))


def test_rejects_surrounding_whitespace_local_gateway(tmp_path):
    with pytest.raises(ValueError, match=r"player 3.*gateway"):
        load_experiment(_write(tmp_path, GOOD.replace("gateway: http://gw:11440/v1", 'gateway: " http://gw:11440/v1 "')))


def test_rejects_non_string_gateway_url(tmp_path):
    with pytest.raises(ValueError, match="gateway_url"):
        load_experiment(_write(tmp_path, GOOD.replace("gateway_url: http://gw:11444/v1", "gateway_url: [a, b]")))


@pytest.mark.parametrize("bad", ['gateway_url: ""', 'gateway_url: "   "'])
def test_rejects_blank_gateway_url(tmp_path, bad):
    with pytest.raises(ValueError, match="gateway_url"):
        load_experiment(_write(tmp_path, GOOD.replace("gateway_url: http://gw:11444/v1", bad)))


def test_rejects_surrounding_whitespace_gateway_url(tmp_path):
    with pytest.raises(ValueError, match="gateway_url"):
        load_experiment(_write(tmp_path, GOOD.replace("gateway_url: http://gw:11444/v1", 'gateway_url: " http://gw:11444/v1 "')))


def test_rejects_non_string_run_id(tmp_path):
    with pytest.raises(ValueError, match="run_id"):
        load_experiment(_write(tmp_path, GOOD.replace("run_id: exp-1", "run_id: [a, b]")))


@pytest.mark.parametrize(
    "bad",
    [
        'run_id: ""',
        'run_id: "   "',
        "run_id: ../outside",
        "run_id: nested/path",
        r"run_id: nested\path",
        "run_id: bad id",
        "run_id: .",
        "run_id: ..",
    ],
)
def test_rejects_unsafe_run_id_values(tmp_path, bad):
    with pytest.raises(ValueError, match="run_id"):
        load_experiment(_write(tmp_path, GOOD.replace("run_id: exp-1", bad)))


@pytest.mark.parametrize("run_id", ["exp-1", "exp_1", "EXP.20260704T000000Z"])
def test_accepts_safe_run_id_values(tmp_path, run_id):
    cfg = load_experiment(_write(tmp_path, GOOD.replace("run_id: exp-1", f"run_id: {run_id}")))
    assert cfg.run_id == run_id


def test_rejects_non_string_unknown_top_level_key(tmp_path):
    with pytest.raises(ValueError, match=r"experiment config: .*top-level"):
        load_experiment(_write(tmp_path, "5: x\nfoo: y\ncivs: []\n"))


def test_rejects_non_string_unknown_civ_key(tmp_path):
    bad = """
civs:
  - player: 3
    provider: local
    model: gemma4-26b
    5: x
    foo: y
"""
    with pytest.raises(ValueError, match=r"player 3"):
        load_experiment(_write(tmp_path, bad))


def test_rejects_non_string_unknown_briefing_key(tmp_path):
    bad = GOOD.replace(
        "briefing: {enabled: true, map_radius: 4, sections: [overview, units, map]}",
        "briefing: {enabled: true, map_radius: 4, sections: [overview, units, map], 5: x, foo: y}",
    )
    with pytest.raises(ValueError, match=r"player 3.*briefing"):
        load_experiment(_write(tmp_path, bad))


def test_rejects_null_provider(tmp_path):
    with pytest.raises(ValueError, match=r"player .*provider"):
        load_experiment(_write(tmp_path, GOOD.replace("provider: local", "provider: null", 1)))


def test_rejects_null_model(tmp_path):
    with pytest.raises(ValueError, match=r"player 3.*model"):
        load_experiment(_write(tmp_path, GOOD.replace("model: gemma4-26b", "model: null")))


def test_rejects_null_gateway(tmp_path):
    with pytest.raises(ValueError, match=r"player 3.*gateway"):
        load_experiment(_write(tmp_path, GOOD.replace("gateway: http://gw:11440/v1", "gateway: null")))


def test_rejects_null_gateway_url(tmp_path):
    with pytest.raises(ValueError, match="gateway_url"):
        load_experiment(_write(tmp_path, GOOD.replace("gateway_url: http://gw:11444/v1", "gateway_url: null")))


def test_rejects_null_run_id(tmp_path):
    with pytest.raises(ValueError, match="run_id"):
        load_experiment(_write(tmp_path, GOOD.replace("run_id: exp-1", "run_id: null")))


def test_rejects_invalid_yaml_with_config_context(tmp_path):
    with pytest.raises(ValueError, match=r"experiment config .*invalid YAML"):
        load_experiment(_write(tmp_path, "civs:\n  - player: [\n"))


def test_rejects_duplicate_top_level_key(tmp_path):
    text = """
run_id: first
run_id: second
civs:
  - player: 3
    provider: local
    model: gemma4-26b
"""
    with pytest.raises(ValueError, match=r"experiment config .*duplicate"):
        load_experiment(_write(tmp_path, text))


def test_rejects_duplicate_civ_key(tmp_path):
    text = """
civs:
  - player: 3
    provider: local
    model: gemma4-26b
    model: qwen
"""
    with pytest.raises(ValueError, match=r"experiment config .*duplicate"):
        load_experiment(_write(tmp_path, text))


def test_rejects_duplicate_briefing_key(tmp_path):
    text = GOOD.replace(
        "briefing: {enabled: true, map_radius: 4, sections: [overview, units, map]}",
        "briefing: {enabled: true, enabled: false, map_radius: 4, sections: [overview, units, map]}",
    )
    with pytest.raises(ValueError, match=r"experiment config .*duplicate"):
        load_experiment(_write(tmp_path, text))


def test_rejects_missing_file_with_config_context(tmp_path):
    with pytest.raises(ValueError, match=r"experiment config .*missing\.yaml"):
        load_experiment(tmp_path / "missing.yaml")


def test_omitted_string_defaults_still_apply(tmp_path):
    text = """
civs:
  - player: 3
    provider: cli-claude
"""
    cfg = load_experiment(_write(tmp_path, text))
    assert cfg.players[0].model == ""
    assert cfg.players[0].gateway == ""
    assert cfg.run_id == ""
    assert cfg.gateway_url == "http://192.168.20.196:11444/v1"


def _load(tmp_path, text):
    """Helper to write YAML text and load as experiment."""
    return load_experiment(_write(tmp_path, text))


def test_attention_yaml_parsed(tmp_path):
    cfg = _load(tmp_path, """
run_id: t1
civs:
  - player: 1
    provider: local
    model: m
    attention:
      mode: hybrid
      max_skip: 3
""")
    assert cfg.players[0].options.attention.mode == "hybrid"
    assert cfg.players[0].options.attention.max_skip == 3
    assert cfg.players[0].options.attention.max_streak == 5  # default preserved


def test_attention_bad_mode_rejected(tmp_path):
    with pytest.raises(ValueError, match="attention.mode"):
        _load(tmp_path, """
run_id: t1
civs:
  - {player: 1, provider: local, model: m, attention: {mode: sometimes}}
""")


def test_attention_unknown_subkey_rejected(tmp_path):
    with pytest.raises(ValueError, match="attention"):
        _load(tmp_path, """
run_id: t1
civs:
  - {player: 1, provider: local, model: m, attention: {mode: auto, nap_time: 9}}
""")


def test_max_game_turns_top_level(tmp_path):
    cfg = _load(tmp_path, """
run_id: t1
max_game_turns: 200
civs:
  - {player: 1, provider: local, model: m}
""")
    assert cfg.max_game_turns == 200


def test_max_game_turns_negative_rejected(tmp_path):
    with pytest.raises(ValueError, match="max_game_turns"):
        _load(tmp_path, """
run_id: t1
max_game_turns: -1
civs:
  - {player: 1, provider: local, model: m}
""")


def test_local_civ_parses_memory_and_task_tracker(tmp_path):
    text = GOOD.replace(
        "briefing: {enabled: true, map_radius: 4, sections: [overview, units, map]}",
        "briefing: {enabled: true, map_radius: 4, sections: [overview, units, map]}\n"
        "    memory: {enabled: true, max_chars: 800, max_age_turns: 6}\n"
        "    task_tracker: {enabled: true, max_tasks: 5}",
    )
    cfg = load_experiment(_write(tmp_path, text))
    local = cfg.players[0]
    assert local.options.memory == MemoryOptions(
        enabled=True,
        max_chars=800,
        max_age_turns=6,
    )
    assert local.options.task_tracker == TaskTrackerOptions(enabled=True, max_tasks=5)


def test_cli_civ_parses_shared_behavior_knobs(tmp_path):
    text = """
civs:
  - player: 1
    provider: cli-claude
    playbook: condensed
    briefing: {enabled: true, map_radius: 2, sections: [overview, units]}
    memory: {enabled: true, max_chars: 900}
    task_tracker: {enabled: true, max_tasks: 4}
"""
    cfg = load_experiment(_write(tmp_path, text))
    cli = cfg.players[0]
    assert cli.provider == "cli-claude"
    assert cli.options.playbook == "condensed"
    assert cli.options.briefing.enabled is True
    assert cli.options.briefing.map_radius == 2
    assert cli.options.briefing.sections == ("overview", "units")
    assert cli.options.memory == MemoryOptions(enabled=True, max_chars=900)
    assert cli.options.task_tracker == TaskTrackerOptions(enabled=True, max_tasks=4)
    # local-only knobs stay at defaults for CLI providers
    assert cli.options.tools == CivOptions().tools
    assert cli.options.result_char_cap == CivOptions().result_char_cap
    assert cli.options.max_steps == CivOptions().max_steps


@pytest.mark.parametrize(
    "knob_line",
    [
        "    tools: standard\n",
        "    result_char_cap: 6000\n",
        "    max_steps: 10\n",
        "    gateway: http://gw:11441/v1\n",
    ],
)
def test_cli_civ_still_rejects_local_only_knobs_and_gateway(tmp_path, knob_line):
    text = (
        "civs:\n"
        "  - player: 1\n"
        "    provider: cli-claude\n"
        + knob_line
    )
    with pytest.raises(ValueError, match=r"player 1.*cli-claude"):
        load_experiment(_write(tmp_path, text))


def test_rejects_non_boolean_memory_enabled(tmp_path):
    bad = GOOD.replace(
        "briefing: {enabled: true, map_radius: 4, sections: [overview, units, map]}",
        "briefing: {enabled: true, map_radius: 4, sections: [overview, units, map]}\n"
        '    memory: {enabled: "true"}',
    )
    with pytest.raises(ValueError, match=r"player 3.*memory\.enabled"):
        load_experiment(_write(tmp_path, bad))


def test_memory_max_age_turns_must_be_positive(tmp_path):
    text = """
civs:
  - player: 1
    provider: cli-claude
    memory: {enabled: true, max_age_turns: 0}
"""

    with pytest.raises(ValueError, match="memory.max_age_turns must be a positive integer"):
        load_experiment(_write(tmp_path, text))


def test_rejects_non_positive_task_tracker_max_tasks(tmp_path):
    bad = GOOD.replace(
        "briefing: {enabled: true, map_radius: 4, sections: [overview, units, map]}",
        "briefing: {enabled: true, map_radius: 4, sections: [overview, units, map]}\n"
        "    task_tracker: {enabled: true, max_tasks: 0}",
    )
    with pytest.raises(ValueError, match=r"player 3.*task_tracker\.max_tasks must be positive"):
        load_experiment(_write(tmp_path, bad))


def test_civ_options_fingerprint_contains_memory_and_task_tracker():
    fp = CivOptions(
        memory=MemoryOptions(enabled=True, max_chars=900),
        task_tracker=TaskTrackerOptions(enabled=True, max_tasks=4),
    ).fingerprint()
    assert fp["memory"] == {"enabled": True, "max_chars": 900, "max_age_turns": 10}
    assert fp["task_tracker"] == {"enabled": True, "max_tasks": 4}


def test_attention_yaml_on_cli_civ(tmp_path):
    """Final-review triage (T2): CLI civs are the expensive seats -- pin that
    the attention knob reaches CivOptions through the cli-provider branch too,
    not just the local one."""
    cfg = _load(tmp_path, """
run_id: t1
civs:
  - player: 2
    provider: cli-claude
    attention:
      mode: hybrid
      threat_radius: 6
""")
    assert cfg.players[0].provider == "cli-claude"
    assert cfg.players[0].options.attention.mode == "hybrid"
    assert cfg.players[0].options.attention.threat_radius == 6
