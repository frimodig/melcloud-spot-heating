import asyncio
import csv
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import ilp_optimizer as o

ROOT = Path(__file__).resolve().parent.parent
CFG = o.load_config(ROOT / "config.toml", ROOT / "tests" / "house.toml")
TZ = ZoneInfo("Europe/Helsinki")
NOW = datetime(2026, 7, 14, 12, 0, tzinfo=TZ)
RETURN = datetime(2026, 7, 17, 18, 0, tzinfo=TZ)  # preheat_hours = 1 -> back on at 17:00


class Unit:
    def __init__(self, name, mode="cool", power=True, target=23.0, fail=None):
        self.name, self.operation_mode, self.power = name, mode, power
        self.target_temperature, self.room_temperature, self.fail = target, 24.0, fail
        self.writes = []

    async def update(self):
        pass

    async def set(self, props):
        if self.fail and ("power" in props):
            raise OSError("power write failed")
        self.writes.append(props)
        if "power" in props:
            self.power = props["power"]
        self.target_temperature = props.get("target_temperature", self.target_temperature)


def go(units, away, path, now=NOW, dry_run=False, hist=None):
    return asyncio.run(o.apply_to_units(units, now, None, CFG, dry_run, 22.0, hist, away,
                                        o.UnitState(path)))


def test_away_while_cooling_switches_running_units_off(tmp_path):
    a, b = Unit("Unit A"), Unit("Unit B")
    assert go([a, b], o.Away(RETURN), tmp_path / "s.json") == 0
    assert a.writes == b.writes == [{"power": False}]
    assert not a.power and not b.power
    data = o.UnitState(tmp_path / "s.json").data["units"]
    assert data["Unit A"]["off_by_us"] and data["Unit B"]["off_by_us"]


def test_stays_off_without_further_writes_until_the_return_window(tmp_path):
    path = tmp_path / "s.json"
    a = Unit("a")
    go([a], o.Away(RETURN), path)
    go([a], o.Away(RETURN), path, NOW + timedelta(days=1))
    go([a], o.Away(RETURN), path, RETURN - timedelta(hours=3, minutes=1))
    assert a.writes == [{"power": False}] and not a.power


def test_switches_on_before_return_with_the_cooling_target(tmp_path):
    path = tmp_path / "s.json"
    down, up = Unit("Unit A"), Unit("Unit B")
    go([down, up], o.Away(RETURN), path)
    go([down, up], o.Away(RETURN), path, RETURN - timedelta(hours=1))
    assert down.writes[-1] == {"power": True, "target_temperature": 23.0}
    assert up.writes[-1] == {"power": True, "target_temperature": 22.5}  # upstairs cooler
    assert down.power and up.power
    assert "off_by_us" not in o.UnitState(path).data["units"]["Unit A"]


def test_open_ended_away_ends_when_the_away_file_is_removed(tmp_path):
    path = tmp_path / "s.json"
    a = Unit("a")
    go([a], o.Away(), path)
    go([a], o.Away(), path, NOW + timedelta(days=5))  # still away, still off
    assert not a.power
    go([a], None, path, NOW + timedelta(days=6))      # AWAY removed
    assert a.power and a.writes[-1]["power"] is True


def test_a_unit_we_did_not_switch_off_stays_off(tmp_path):
    path = tmp_path / "s.json"
    owner_off = Unit("a", power=False)
    go([owner_off], o.Away(RETURN), path)
    go([owner_off], None, path, NOW + timedelta(days=4))
    assert owner_off.writes == [] and not owner_off.power


def test_a_unit_turned_on_during_away_is_left_on(tmp_path):
    path = tmp_path / "s.json"
    a = Unit("a")
    go([a], o.Away(RETURN), path)
    a.power = True  # someone uses the remote
    go([a], o.Away(RETURN), path, NOW + timedelta(hours=2))
    go([a], o.Away(RETURN), path, NOW + timedelta(hours=4))
    assert a.power and a.writes.count({"power": False}) == 1  # not switched off again
    assert o.UnitState(path).data["units"]["a"]["away_override"]
    go([a], None, path, NOW + timedelta(days=4))              # away over: override cleared
    assert "away_override" not in o.UnitState(path).data["units"]["a"]


def test_heating_away_is_unchanged_and_never_touches_power(tmp_path):
    a = Unit("a", mode="heat", target=21.0)
    go([a], o.Away(), tmp_path / "s.json")
    assert a.writes == [{"target_temperature": 17.0}] and a.power


def test_dry_run_changes_nothing(tmp_path):
    path = tmp_path / "s.json"
    a = Unit("a")
    go([a], o.Away(RETURN), path, dry_run=True)
    assert a.writes == [] and a.power and not path.exists()


def test_failed_switch_on_is_retried_at_the_next_run(tmp_path):
    path = tmp_path / "s.json"
    a = Unit("a")
    go([a], o.Away(RETURN), path)
    a.fail = True
    assert go([a], None, path, NOW + timedelta(days=4)) >= 1
    assert not a.power and o.UnitState(path).data["units"]["a"]["off_by_us"]
    a.fail = False
    go([a], None, path, NOW + timedelta(days=4, minutes=15))
    assert a.power


def test_failed_switch_off_is_retried(tmp_path):
    path = tmp_path / "s.json"
    a = Unit("a", fail=True)
    assert go([a], o.Away(RETURN), path) >= 1
    assert a.power
    a.fail = False
    go([a], o.Away(RETURN), path, NOW + timedelta(minutes=15))
    assert not a.power


def test_history_records_away_off_and_on(tmp_path):
    path, hist = tmp_path / "s.json", tmp_path / "h.csv"
    a = Unit("a")
    go([a], o.Away(RETURN), path, hist=hist)
    go([a], o.Away(RETURN), path, RETURN - timedelta(hours=1), hist=hist)
    assert [r["action"] for r in csv.DictReader(open(hist))] == ["away-off", "away-on"]


def test_without_memory_the_controller_never_switches_power():
    a = Unit("a")
    asyncio.run(o.apply_to_units([a], NOW, None, CFG, False, 22.0, None, o.Away(), None))
    assert all("power" not in w for w in a.writes)
