import argparse
import asyncio
import csv
from datetime import datetime
from pathlib import Path

import ilp_optimizer as o

ROOT = Path(__file__).resolve().parent.parent
CFG = o.load_config(ROOT / "config.toml", ROOT / "tests" / "house.toml")
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=o.ZoneInfo("Europe/Helsinki"))


class Unit:
    """A unit like pymelcloud's AtaDevice: the extra properties are optional."""
    def __init__(self, name, extra=None, fail=False):
        self.name, self.target_temperature, self.power = name, 19.0, True
        self.operation_mode, self.room_temperature, self.fail = "heat", 20.5, fail
        for k, v in (extra or {}).items():
            setattr(self, k, v)

    async def update(self):
        if self.fail:
            raise OSError("update failed")

    async def set(self, props):
        self.target_temperature = props.get("target_temperature", self.target_temperature)


def run(devices, path):
    return asyncio.run(o.apply_to_units(devices, NOW, None, CFG, False, telemetry=path))


def rows(path):
    return list(csv.DictReader(open(path)))


def test_one_row_per_unit_with_what_the_unit_reports(tmp_path):
    path = tmp_path / "t.csv"
    meter = {"actual_fan_speed": "0", "total_energy_consumed": 1234.5, "has_energy_consumed_meter": True}
    assert run([Unit("up", meter), Unit("down", meter)], path) == 0
    r = rows(path)
    assert [x["unit"] for x in r] == ["up", "down"]
    assert r[0] == {"time_local": "2026-11-10T12:00:00+02:00", "unit": "up", "room_temp": "20.5",
                    "target_temp": "19.0", "power": "True", "mode": "heat", "fan_actual": "0",
                    "energy_kwh": "1234.5", "has_meter": "True",
                    "outdoor_c": "", "humidity": "", "wind_ms": "", "weather_code": ""}


def test_properties_a_unit_lacks_are_left_empty(tmp_path):
    path = tmp_path / "t.csv"
    run([Unit("up")], path)
    r = rows(path)[0]
    assert r["fan_actual"] == "" and r["energy_kwh"] == "" and r["has_meter"] == "" and r["room_temp"] == "20.5"


def test_none_from_the_unit_is_empty_not_the_word_none(tmp_path):
    path = tmp_path / "t.csv"
    run([Unit("up", {"total_energy_consumed": None})], path)
    assert rows(path)[0]["energy_kwh"] == ""


def test_a_unit_that_cannot_be_read_gets_no_row(tmp_path):
    path = tmp_path / "t.csv"
    assert run([Unit("up", fail=True), Unit("down")], path) == 1
    assert [x["unit"] for x in rows(path)] == ["down"]


def test_telemetry_failure_does_not_stop_control(tmp_path):
    bad = tmp_path / "is_a_dir.csv"
    bad.mkdir()  # a directory in place of the file makes every write fail
    u = Unit("up")
    u.target_temperature = 25.0  # far from the plan, so a write is needed
    assert run([u], bad) == 0 and u.target_temperature != 25.0


def test_run_writes_to_the_monthly_telemetry_file(monkeypatch):
    seen = {}

    async def fake_apply(*a, **k):
        seen["telemetry"] = k.get("telemetry")
        return 0

    async def none(cfg, f):
        return None

    async def no_devices(session):
        return []
    monkeypatch.setattr(o, "apply_to_units", fake_apply)
    monkeypatch.setattr(o, "get_prices_or_none", none)
    monkeypatch.setattr(o, "melcloud_devices", no_devices)
    monkeypatch.setattr(o, "setup_logging", lambda cfg: None)
    monkeypatch.delenv("HEARTBEAT_URL", raising=False)
    args = argparse.Namespace(config=str(ROOT / "config.toml"), list=False, plan=False, report=None, control=None, lock_wait=0.0, plan_json=False,
                              prices_file=None, now="2026-12-01T00:05", outdoor_temp=5.0, sun=0.0,
                              mode="heat", season=None, home=None, assume_home=False,
                              holiday=False, away=False, away_until=None, dry_run=True)
    assert asyncio.run(o.run(args)) == 0
    assert seen["telemetry"] == o.APP_DIR / "telemetry-2026-12.csv"


def test_list_shows_the_meter_and_fan(monkeypatch, capsys):
    class Listed(Unit):
        actual_fan_speed, has_energy_consumed_meter, total_energy_consumed = "0", True, 12.3
        fan_speed, fan_speeds = "auto", ["auto", "1", "2", "3"]

    async def devices(session):
        return [Listed("up")]
    monkeypatch.setattr(o, "melcloud_devices", devices)
    asyncio.run(o.list_units())
    out = capsys.readouterr().out
    assert "fan_set=auto" in out and "fan_speeds=['auto', '1', '2', '3']" in out
    assert "fan_actual=0" in out and "energy_meter=True" in out and "energy_kwh=12.3" in out
