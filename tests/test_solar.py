import asyncio
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import ilp_optimizer as o

ROOT = Path(__file__).resolve().parent.parent
CFG = o.load_config(ROOT / "tests" / "logic_config.toml")
TZ = ZoneInfo("Europe/Helsinki")
TUE = date(2026, 4, 14)  # a spring Tuesday: EEST, UTC+3


def at(hour, minute=0):
    return datetime(TUE.year, TUE.month, TUE.day, hour, minute, tzinfo=TZ)


def sun_at(*pairs):
    """SunForecast from (label hour, W/m2): labels are hour ENDS in local time."""
    return o.SunForecast({at(h).astimezone(timezone.utc): w for h, w in pairs})


def heat(hour, sun, minute=0, room=None, weight=1.0, mode="heat"):
    return o.desired_setpoint(at(hour, minute), None, CFG, 0.0, None, room, None, mode, False, sun, weight)


# ---- the forecast ---------------------------------------------------------------

def test_values_are_labelled_by_the_end_of_their_hour():
    sun = sun_at((13, 500.0))                 # the hour 12:00-13:00
    assert sun.at(at(12, 0)) == 500.0
    assert sun.at(at(12, 59)) == 500.0
    assert sun.at(at(13, 0)) is None          # 13:00-14:00 is a different, missing, hour
    assert sun.at(at(11, 59)) is None


def test_lookahead_takes_the_highest_of_the_coming_hours():
    sun = sun_at((12, 100.0), (13, 300.0), (14, 700.0))
    assert sun.at(at(11, 30), 0) == 100.0
    assert sun.at(at(11, 30), 1) == 300.0
    assert sun.at(at(11, 30), 2) == 700.0
    assert sun.at(at(11, 30), 5) == 700.0     # missing hours are ignored


def test_constant_forecast_for_the_cli():
    assert o.SunForecast({}, constant=250.0).at(at(3)) == 250.0


def test_parse_the_open_meteo_payload():
    payload = {"hourly": {"time": ["2026-04-14T12:00", "2026-04-14T13:00", "2026-04-14T14:00"],
                          "shortwave_radiation": [100.0, None, 450.5]}}
    sun = o.parse_sun(payload, TZ)
    assert sun.at(at(11, 30)) == 100.0        # label 12:00 = the hour 11:00-12:00
    assert sun.at(at(12, 30)) is None         # null value dropped
    assert sun.at(at(13, 30)) == 450.5


# ---- heating --------------------------------------------------------------------

def test_sun_lowers_the_heating_setpoint_in_proportion():
    # Tuesday 14:00 base 21. 100 W/m2 or less: nothing; 300: half of 1.0; 500+: all of it.
    for radiation, expected in ((0.0, 21.0), (100.0, 21.0), (300.0, 20.5), (500.0, 20.0), (900.0, 20.0)):
        sun = sun_at((15, radiation))
        assert heat(14, sun)[2] == expected, radiation


def test_sun_effect_is_shown_in_the_reason():
    base, offset, final, reason = heat(14, sun_at((15, 500.0)))
    assert offset == -1.0 and "sun 500 W/m2 -1.0" in reason


def test_no_forecast_or_disabled_means_no_effect():
    assert heat(14, None)[2] == 21.0
    assert heat(14, o.SunForecast({}))[2] == 21.0
    cfg = {**CFG, "solar": {**CFG["solar"], "enabled": False}}
    assert o.desired_setpoint(at(14), None, cfg, 0.0, None, None, None, "heat", False, sun_at((15, 900.0)))[2] == 21.0


def test_unit_weight_scales_the_effect():
    sun = sun_at((15, 500.0))
    assert heat(14, sun, weight=1.0)[2] == 20.0
    assert heat(14, sun, weight=0.5)[2] == 20.5
    assert heat(14, sun, weight=0.0)[2] == 21.0


def test_sun_adds_to_a_price_shift_instead_of_replacing_it():
    start, end = o.local_day_bounds(TUE, TZ)
    prices = [o.PriceInterval(start + timedelta(hours=k), start + timedelta(hours=k + 1),
                              2.0 if k in (1, 2, 3, 4) else 6.0) for k in range(24)]
    sun = sun_at((4, 500.0))  # synthetic sun at 03:00, an hour the price boosts (+2.0)
    price_only = o.desired_setpoint(at(3), prices, CFG, 0.0, None, None, None, "heat", False, None)[2]
    sun_only = o.desired_setpoint(at(3), None, CFG, 0.0, None, None, None, "heat", False, sun)[2]
    both = o.desired_setpoint(at(3), prices, CFG, 0.0, None, None, None, "heat", False, sun)[2]
    assert (price_only, sun_only, both) == (21.0, 18.0, 20.0)  # base 19: +2, -1, and both


def test_heating_comfort_guard_covers_the_sun_cut():
    sun = sun_at((15, 500.0))
    assert heat(14, sun, room=20.0)[2] == 20.0
    final, reason = heat(14, sun, room=18.0)[2], heat(14, sun, room=18.0)[3]
    assert final == 21.0 and "comfort guard" in reason  # room below 18.5: keep heating


def test_sun_cannot_push_the_setpoint_below_the_limits():
    # base 19 at 03:00 on a weekday... use a unit offset to sit on the floor
    sun = sun_at((4, 900.0))
    final = o.desired_setpoint(at(3), None, CFG, -5.0, None, None, None, "heat", False, sun)[2]
    assert final == CFG["limits"]["min_temp"]


# ---- cooling --------------------------------------------------------------------

def test_cooling_pre_cools_ahead_of_the_sun():
    sun = sun_at((14, 700.0))                         # strong sun 13:00-14:00
    assert heat(11, sun, mode="cool")[2] == 23.0      # 11:00 looks to 12:00 only: no sun yet
    assert heat(12, sun, minute=30, mode="cool")[2] == 22.0   # the next hour is sunny: 23 - 1
    assert heat(13, sun, minute=30, mode="cool")[2] == 22.0   # in the sun itself
    assert heat(14, sun, minute=30, mode="cool")[2] == 23.0   # it has passed


def test_cooling_unit_weight():
    sun = sun_at((14, 500.0))
    assert heat(13, sun, weight=1.0, mode="cool")[2] == 22.0
    assert heat(13, sun, weight=0.5, mode="cool")[2] == 22.5


def test_cooling_comfort_guard_covers_pre_cooling():
    sun = sun_at((14, 500.0))
    assert heat(13, sun, room=22.0, mode="cool")[2] == 22.0
    assert heat(13, sun, room=21.0, mode="cool")[2] == 23.0   # already cool: no pre-cool


# ---- fetching and the plan ------------------------------------------------------

def test_fetch_failure_means_no_solar_effect(monkeypatch):
    async def boom(cfg):
        raise OSError("down")
    monkeypatch.setattr(o, "fetch_sun", boom)
    assert asyncio.run(o.get_sun_or_none(CFG, None)) is None


def test_override_and_disabled_do_not_fetch(monkeypatch):
    async def boom(cfg):
        raise AssertionError("must not fetch")
    monkeypatch.setattr(o, "fetch_sun", boom)
    assert asyncio.run(o.get_sun_or_none(CFG, 300.0)).at(at(3)) == 300.0
    assert asyncio.run(o.get_sun_or_none({**CFG, "solar": {**CFG["solar"], "enabled": False}}, None)) is None


def test_plan_table_shows_the_sun():
    table = o.plan_table(at(12), None, CFG, None, None, "heat", False, o.SunForecast({}, constant=500.0))
    assert "sun 500 W/m2" in table


# ---- units ----------------------------------------------------------------------

class Unit:
    def __init__(self, name, mode="heat", target=25.0):
        self.name, self.operation_mode, self.target_temperature = name, mode, target
        self.power, self.room_temperature, self.writes = True, 21.0, []

    async def update(self):
        pass

    async def set(self, props):
        self.writes.append(props)
        self.target_temperature = props.get("target_temperature", self.target_temperature)


def test_each_unit_gets_its_own_solar_weight():
    down, up = Unit("Unit A"), Unit("Unit B")
    sun = o.SunForecast({}, constant=500.0)
    asyncio.run(o.apply_to_units([down, up], at(14), None, CFG, False, None, None, None, None, None, False, sun))
    # base 21, downstairs +0.5 offset, sun weight 0.5 -> 21 + 0.5 - 0.5; upstairs weight 1.0 -> 20
    assert down.writes == [{"target_temperature": 21.0}]
    assert up.writes == [{"target_temperature": 20.0}]
