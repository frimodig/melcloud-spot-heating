from pathlib import Path

import pytest

import ilp_optimizer as o

ROOT = Path(__file__).resolve().parent.parent


def test_the_environment_sets_the_weather_location(monkeypatch):
    """The house's location lives in .env, not in the repository: it replaces the example."""
    monkeypatch.setenv("WEATHER_LATITUDE", "61.5")
    monkeypatch.setenv("WEATHER_LONGITUDE", "23.75")
    w = o.load_config(ROOT / "config.toml", ROOT / "tests" / "house.toml")["weather"]
    assert w["latitude"] == 61.5
    assert w["longitude"] == 23.75


def test_without_a_location_weather_and_sun_are_off(monkeypatch, caplog):
    monkeypatch.delenv("WEATHER_LATITUDE", raising=False)
    monkeypatch.delenv("WEATHER_LONGITUDE", raising=False)
    cfg = o.load_config(ROOT / "config.toml", ROOT / "tests" / "house.toml")
    assert "latitude" not in cfg["weather"] and not cfg["weather"]["enabled"] and not cfg["solar"]["enabled"]
    assert "no location set" in caplog.text


def test_half_a_location_is_refused(monkeypatch):
    monkeypatch.setenv("WEATHER_LATITUDE", "61.5")
    monkeypatch.delenv("WEATHER_LONGITUDE", raising=False)
    with pytest.raises(ValueError):
        o.load_config(ROOT / "config.toml", ROOT / "tests" / "house.toml")
