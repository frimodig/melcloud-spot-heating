import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import ilp_optimizer as o

ROOT = Path(__file__).resolve().parent.parent
CFG = o.load_config(ROOT / "config.toml", ROOT / "tests" / "house.toml")
TZ = ZoneInfo("Europe/Helsinki")
NOW = datetime(2026, 11, 10, 20, 0, tzinfo=TZ)           # Tuesday 20:00, plan base 22


class Unit:
    def __init__(self, name, fan="3", target=18.0, room=20.0, mode="heat", speeds=("auto", "1", "2", "3", "4", "5")):
        self.name, self.operation_mode, self.target_temperature = name, mode, target
        self.power, self.room_temperature, self.writes = True, room, []
        self.fan_speed, self.fan_speeds = fan, list(speeds) if speeds else speeds

    async def update(self):
        pass

    async def set(self, props):
        self.writes.append(dict(props))
        self.operation_mode = props.get("operation_mode", self.operation_mode)
        self.target_temperature = props.get("target_temperature", self.target_temperature)
        self.fan_speed = props.get("fan_speed", self.fan_speed)


def run(units, now=NOW, season="heat", state=None, normal=None):
    return asyncio.run(o.apply_to_units(units, now, None, CFG, False, 5.0, None, None, state, season, normal=normal))


def test_a_target_change_also_puts_the_fan_back_to_auto():
    u = Unit("a", fan="5")
    run([u])
    assert u.writes == [{"target_temperature": 22.0, "fan_speed": "auto"}]


def test_no_write_at_all_when_the_target_does_not_change_even_if_the_fan_is_not_auto():
    u = Unit("a", fan="5", target=22.0)
    run([u])
    assert u.writes == [] and u.fan_speed == "5"          # the reset rides on target changes only


def test_a_unit_already_on_auto_gets_a_plain_target_write():
    u = Unit("a", fan="auto")
    run([u])
    assert u.writes == [{"target_temperature": 22.0}]


def test_units_that_do_not_report_or_offer_auto_are_left_alone():
    no_report, no_auto = Unit("a", speeds=None), Unit("b", fan="2", speeds=("1", "2", "3"))
    run([no_report, no_auto])
    assert "fan_speed" not in no_report.writes[0] and "fan_speed" not in no_auto.writes[0]
    plain = type("P", (), {})()                                   # no fan attributes at all
    assert o.fan_to(plain, "auto") == {} and o.fastest_fan(plain) is None


def test_a_season_switch_resets_the_fan_in_the_same_write():
    u = Unit("a", fan="4", mode="cool", target=23.0)
    run([u], season="heat")
    assert u.writes == [{"operation_mode": "heat", "target_temperature": 22.0, "fan_speed": "auto"}]


def normal(hours=4):
    until = NOW + timedelta(hours=hours)
    return {"temp": 21.0, "until": until, "id": until.isoformat()}


def test_normal_now_runs_the_fans_at_the_top_speed_and_ends_with_auto(tmp_path):
    a, b = Unit("a", fan="auto", room=25.0, mode="cool"), Unit("b", fan="auto", room=25.0, mode="cool")
    state = o.UnitState(tmp_path / "s.json")
    run([a, b], state=state, normal=normal())
    assert a.writes == [{"target_temperature": 21.0, "fan_speed": "5"}]   # already cool: only target and fan
    a.writes.clear()
    run([a, b], now=NOW + timedelta(minutes=15), state=state, normal=normal())
    assert a.writes == []                                                # nothing to change while it runs
    a.room_temperature = b.room_temperature = 21.0
    run([a, b], now=NOW + timedelta(minutes=30), state=state, normal=normal())
    assert a.writes[-1]["fan_speed"] == "auto" and a.writes[-1]["operation_mode"] == "heat"
    assert a.fan_speed == b.fan_speed == "auto"


def test_a_fan_a_person_changed_during_normal_now_is_set_back_to_full(tmp_path):
    a, b = Unit("a", fan="auto", room=25.0, mode="cool", target=21.0), Unit("b", fan="auto", room=25.0, mode="cool", target=21.0)
    state = o.UnitState(tmp_path / "s.json")
    run([a, b], state=state, normal=normal())
    a.writes.clear()
    a.fan_speed = "2"
    run([a, b], now=NOW + timedelta(minutes=15), state=state, normal=normal())
    assert a.writes == [{"fan_speed": "5"}]


def test_the_fan_boost_can_be_switched_off(monkeypatch, tmp_path):
    monkeypatch.setitem(CFG["normal"], "fan_full", False)
    a, b = Unit("a", fan="auto", room=25.0, mode="cool"), Unit("b", fan="auto", room=25.0, mode="cool")
    run([a, b], state=o.UnitState(tmp_path / "s.json"), normal=normal())
    assert all("fan_speed" not in w for w in a.writes + b.writes)
