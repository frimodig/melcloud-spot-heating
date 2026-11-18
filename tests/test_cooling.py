from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import asyncio

import pytest

import ilp_optimizer as o

ROOT = Path(__file__).resolve().parent.parent
CFG = o.load_config(ROOT / "config.toml", ROOT / "tests" / "house.toml")
TZ = ZoneInfo("Europe/Helsinki")
DAY = date(2026, 7, 14)  # a Tuesday in summer
COOL = CFG["cool"]


def at(hour, minute=0, day=DAY):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)


def hourly(overrides, default=3.0, day=DAY):
    start, end = o.local_day_bounds(day, TZ)
    out, k = [], 0
    while start < end:
        out.append(o.PriceInterval(start, start + timedelta(hours=1), overrides.get(k, default)))
        start += timedelta(hours=1)
        k += 1
    return out


def cool(hour, prices=None, outdoor=None, room=None, unit_offset=0.0, minute=0, day=DAY):
    return o.desired_setpoint(at(hour, minute, day), prices, CFG, unit_offset, outdoor, room, None, "cool")


def test_flat_setpoint_every_day_and_hour():
    for day in (date(2026, 7, 13), date(2026, 7, 14), date(2026, 7, 18), date(2026, 7, 19)):  # Mon..Sun
        for hour in (0, 8, 14, 23):
            assert cool(hour, None, day=day)[2] == 23.0


def test_cheap_hours_pre_cool_and_expensive_hours_drift_warmer():
    # 4 cheap hours at 1c (night), 4 expensive hours at 9c (afternoon), rest 3c
    prices = hourly({**{k: 1.0 for k in (2, 3, 4, 5)}, **{k: 9.0 for k in (15, 16, 17, 18)}})
    assert cool(3, prices)[2] == 22.0      # 23 - 1
    assert cool(16, prices)[2] == 24.0     # 23 + 1
    assert cool(11, prices)[2] == 23.0
    assert cool(16, prices)[1] == 1.0 and cool(3, prices)[1] == -1.0  # setpoint-direction offsets


def test_winter_scale_prices_do_not_trigger_summer_rules_wrongly():
    # cheapest 5c is above cool cheap_max 4c: no pre-cool; priciest 6c is below expensive_min 7c.
    prices = hourly({k: 5.0 for k in (2, 3, 4, 5)} | {k: 6.0 for k in (15, 16, 17, 18)}, default=5.5)
    assert all(cool(h, prices)[2] == 23.0 for h in range(24))


def test_small_summer_spread_means_no_shifting():
    prices = hourly({k: 1.0 for k in (2, 3, 4, 5)} | {k: 2.5 for k in (15, 16, 17, 18)}, default=2.0)
    assert all(cool(h, prices)[2] == 23.0 for h in range(24))  # spread 1.5 < 2.0


def test_extreme_price_coasting_and_ceiling():
    prices = hourly({k: 14.0 for k in (14, 15, 16)}, default=3.0)  # >= cut_price 12c
    final = cool(15, prices)[2]
    assert final == 25.0  # 23 + 2
    cfg = {**CFG, "cool": {**COOL, "extreme": {**COOL["extreme"], "cut": 9.0}}}
    assert o.desired_setpoint(at(15), prices, cfg, 0.0, None, None, None, "cool")[2] == 27.0  # ceiling


def test_spike_pre_cools_the_hours_before_it():
    prices = hourly({18: 20.0}, default=3.0)  # >= 3x median and >= 8c
    assert cool(16, prices)[2] == 21.5 and cool(17, prices)[2] == 21.5  # 23 - 1.5
    assert cool(15, prices)[2] == 23.0
    assert cool(18, prices)[2] == 25.0  # the spike itself: extreme price coasting


def test_weather_scaling_is_reversed_for_cooling():
    # hot day: factor 1.5, cool day: 0.5; neutral/no data: 1.0
    assert o.weather_factor(28.0, CFG, "cool") == pytest.approx(1.5)
    assert o.weather_factor(15.0, CFG, "cool") == pytest.approx(0.5)
    assert o.weather_factor(20.0, CFG, "cool") == pytest.approx(1.0)
    assert o.weather_factor(None, CFG, "cool") == 1.0
    prices = hourly({k: 1.0 for k in (2, 3, 4, 5)} | {k: 9.0 for k in (15, 16, 17, 18)})
    assert cool(3, prices, outdoor=28.0)[1] == -1.5 and cool(3, prices, outdoor=10.0)[1] == -0.5


def test_comfort_guard_for_cooling():
    prices = hourly({k: 1.0 for k in (2, 3, 4, 5)} | {k: 9.0 for k in (15, 16, 17, 18)})
    assert cool(16, prices, room=25.0)[2] == 24.0           # drift allowed
    assert cool(16, prices, room=26.0)[2] == 23.0           # room already too warm: no drift
    assert "comfort guard" in cool(16, prices, room=26.0)[3]
    assert cool(3, prices, room=22.0)[2] == 22.0            # pre-cool allowed
    assert cool(3, prices, room=21.0)[2] == 23.0            # room already cool: no pre-cool


def test_unit_offset_and_limits_for_cooling():
    assert cool(12, None, unit_offset=-0.5)[2] == 22.5
    assert cool(12, None, unit_offset=-5.0)[2] == 21.0      # normal floor 21
    assert cool(12, None, unit_offset=+5.0)[2] == 26.0      # normal ceiling 26


def test_cool_plan_table_header_names_the_mode():
    table = o.plan_table(at(12), None, CFG, None, None, "cool")
    assert table.splitlines()[0].startswith("mode: cool") and "23.0" in table


# ---- units in cool mode ---------------------------------------------------------

class Unit:
    def __init__(self, name, target, mode="cool", room=24.0):
        self.name, self.target_temperature, self.operation_mode = name, target, mode
        self.power, self.room_temperature, self.writes = True, room, []

    async def update(self):
        pass

    async def set(self, props):
        self.writes.append(props)
        self.target_temperature = props.get("target_temperature", self.target_temperature)
        self.operation_mode = props.get("operation_mode", self.operation_mode)


def apply(units, now=None):
    return asyncio.run(o.apply_to_units(units, now or at(12), None, CFG, False))


def test_units_in_cool_mode_get_cooling_rules_and_cool_offsets():
    down, up = Unit("Unit A", 20.0), Unit("Unit B", 20.0)
    assert apply([down, up]) == 0
    assert down.writes == [{"target_temperature": 23.0}]    # no cool offset downstairs
    assert up.writes == [{"target_temperature": 22.5}]      # upstairs 0.5 C cooler


def test_heat_mode_still_uses_heating_rules_and_heat_offsets():
    down, up = Unit("Unit A", 25.0, "heat"), Unit("Unit B", 25.0, "heat")
    apply([down, up])
    # a weekday noon with no prices: the heating plan's nobody-home floor, for both units
    assert down.writes == [{"target_temperature": 17.0}] and up.writes == [{"target_temperature": 17.0}]


def test_heat_and_cool_together_on_one_outdoor_unit_are_still_blocked():
    heat, cool_unit = Unit("Unit A", 25.0, "heat"), Unit("Unit B", 25.0, "cool")   # a multi-split
    assert apply([heat, cool_unit]) >= 1
    assert heat.writes == cool_unit.writes == []
