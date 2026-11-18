"""An install's own config.local.toml laid over the tracked defaults in config.toml."""
from pathlib import Path

import ilp_optimizer as o

ROOT = Path(__file__).resolve().parent.parent


def test_a_local_file_overrides_only_what_it_names(tmp_path):
    local = tmp_path / o.LOCAL_CONFIG
    local.write_text('[units]\nnames = ["Living room"]\n\n[limits]\nmax_temp = 23.0\n')
    defaults = o.load_config(ROOT / "config.toml")
    cfg = o.load_config(ROOT / "config.toml", local)
    assert cfg["units"]["names"] == ["Living room"] and cfg["limits"]["max_temp"] == 23.0
    assert cfg["limits"]["min_temp"] == defaults["limits"]["min_temp"]       # the rest of the table stays
    assert cfg["units"].get("offsets") == defaults["units"].get("offsets")
    assert cfg["schedule"] == defaults["schedule"]


def test_a_list_replaces_the_default_whole():
    out = o.merged_config({"schedule": {"weekday": [["00:00", 19.0], ["06:00", 21.0]]}},
                          {"schedule": {"weekday": [["00:00", 20.0]]}})
    assert out["schedule"]["weekday"] == [["00:00", 20.0]]


def test_no_local_file_means_the_defaults(tmp_path):
    assert o.load_config(ROOT / "config.toml", tmp_path / "missing.toml") == o.load_config(ROOT / "config.toml")


def test_an_unknown_setting_is_kept_but_logged(caplog):
    out = o.merged_config({"limits": {"min_temp": 16.0}}, {"limits": {"min_tmep": 15.0}, "limitz": {}})
    assert out["limits"]["min_tmep"] == 15.0 and out["limits"]["min_temp"] == 16.0
    assert "limits.min_tmep is not a known setting" in caplog.text and "limitz is not a known" in caplog.text


def test_a_table_keyed_by_unit_name_takes_any_name(caplog):
    out = o.merged_config({"units": {"offsets": {}}}, {"units": {"offsets": {"Living room": 0.5}}})
    assert out["units"]["offsets"] == {"Living room": 0.5} and "not a known setting" not in caplog.text


def test_the_example_local_config_is_valid_and_switches_the_rules_on(caplog):
    cfg = o.load_config(ROOT / "config.toml", ROOT / "config.local.example.toml")
    assert "not a known setting" not in caplog.text
    assert o.optional(cfg, "night_shift") and cfg["nobody_home"]["weekday"]
    units = {n for g in cfg["units"]["outdoor_groups"] for n in g}
    assert o.configured_unit_names(cfg) == units                    # every name it uses is a unit it lists
    for key in ("weekday", "weekend"):
        o.parse_schedule(cfg["schedule"][key])                         # starts at 00:00, in order


def test_the_minimal_local_config_switches_every_optional_rule_off(caplog):
    cfg = o.load_config(ROOT / "config.toml", ROOT / "config.local.minimal.toml")
    assert "not a known setting" not in caplog.text
    for name in ("comfort_windows", "comfort_warmup", "comfort_preheat", "spikes", "hour_shaping",
                 "above_average", "night_shift"):
        assert o.optional(cfg, name) is None, name
    assert not cfg["weather"]["enabled"] and not cfg["solar"]["enabled"]


def test_every_optional_rule_says_it_is_on_or_off_in_the_defaults():
    import tomllib
    raw = tomllib.load(open(ROOT / "config.toml", "rb"))
    for name in ("comfort_windows", "comfort_warmup", "comfort_preheat", "spikes", "hour_shaping",
                 "above_average", "night_shift", "weather", "solar"):
        assert "enabled" in raw[name], name                            # so a local enabled = false is no typo
