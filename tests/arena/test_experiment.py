import pytest

from civ_mcp.arena.config import CivOptions
from civ_mcp.arena.experiment import load_experiment


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
