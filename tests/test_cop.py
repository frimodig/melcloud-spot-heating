"""The price of heat ([cop]): the spot price scaled by how much worse the COP is in the cold."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import ilp_optimizer as o

ROOT = Path(__file__).resolve().parent.parent
CFG = o.load_config(ROOT / "config.toml", ROOT / "tests" / "house.toml")
ON = {**CFG, "cop": {**CFG["cop"], "enabled": True}}
TZ = o.ZoneInfo(CFG["timezone"])
NOON = datetime(2026, 11, 10, 12, 0, tzinfo=TZ)
COP = ON["cop"]


def day_prices(price=lambda k: 8.0):
    start, _ = o.local_day_bounds(NOON.date(), TZ)
    return [o.PriceInterval(start + timedelta(minutes=15 * k), start + timedelta(minutes=15 * (k + 1)), price(k))
            for k in range(96)]


def forecast(temp_at_hour):
    start, _ = o.local_day_bounds(NOON.date(), TZ)
    return o.OutdoorForecast({(start + timedelta(hours=h)).astimezone(timezone.utc): (temp_at_hour(h), 80.0, 3.0, 0, 1)
                              for h in range(24)})


def test_the_cop_is_interpolated_between_the_points_and_flat_beyond_them():
    assert o.cop_at(COP, -15) == 3.5 and o.cop_at(COP, -30) == 3.5     # flat below the first point
    assert o.cop_at(COP, 7) == 4.1 and o.cop_at(COP, 30) == 4.4       # and above the last
    assert o.cop_at(COP, 4) == pytest.approx(3.8 + 0.3 * 3 / 6)        # between 1 and 7


def test_a_cold_quarter_is_dearer_a_warm_one_cheaper_and_the_reference_unchanged():
    ref = o.cop_at(COP, COP["reference_c"])
    prices = day_prices()
    for temp, expect in ((COP["reference_c"], 8.0), (-7.0, 8.0 * ref / 3.6), (7.0, 8.0 * ref / 4.1)):
        heat = o.heat_prices(prices, temp, ON)
        assert o.price_at(heat, NOON) == pytest.approx(expect)
        assert o.spot_price_at(heat, NOON) == 8.0          # the page, history and reports keep the spot price
    assert o.price_at(o.heat_prices(prices, -7.0, ON), NOON) > 8.0 > o.price_at(o.heat_prices(prices, 7.0, ON), NOON)


def test_the_hourly_forecast_gives_each_quarter_its_own_cop():
    cold_night = forecast(lambda h: -8.0 if h < 6 else 2.0)
    heat = o.heat_prices(day_prices(), cold_night, ON)
    night, day = o.price_at(heat, NOON.replace(hour=3)), o.price_at(heat, NOON)
    assert night > day and night == pytest.approx(8.0 * o.cop_at(COP, 0) / o.cop_at(COP, -8.0), rel=0.01)


def test_nothing_changes_when_off_or_without_weather():
    prices = day_prices()
    assert o.heat_prices(prices, -7.0, CFG) is prices            # [cop] is off by default
    assert o.heat_prices(prices, None, ON) is prices             # no forecast: the spot price
    assert o.heat_prices(None, -7.0, ON) is None


def test_cooling_without_cool_cop_judges_the_spot_price_and_the_page_shows_no_price_of_cooling():
    prices, hot = day_prices(), forecast(lambda h: 28.0)
    assert o.heat_prices(prices, 28.0, ON, "cool") is prices     # [cop] is a heating curve
    rows = json.loads(o.plan_json(NOON, o.heat_prices(prices, hot, ON, "cool"), ON, hot, None, "cool"))["rows"]
    assert all(r["heat_price"] is None and r["heat_factor"] is None for r in rows)


COOL = {**CFG, "cool": {**CFG["cool"], "cop": {**CFG["cool"]["cop"], "enabled": True}}}


def test_cooling_is_dearer_the_hotter_it_is_outside_and_nearly_free_when_it_is_cooler_than_the_room():
    c, target = COOL["cool"]["cop"], COOL["cool"]["temp"]
    f = lambda t: o.price_factor(COOL, "cool", t)
    assert f(c["reference_c"]) == pytest.approx(1.0)
    assert f(35.0) == pytest.approx((35.0 - target + c["approach_k"]) / (c["reference_c"] - target + c["approach_k"]))
    assert f(35.0) > 1.0 > f(23.0) > f(10.0) > f(5.0) == f(-5.0)   # held at min_lift_k when cool outside
    assert f(5.0) == pytest.approx(c["min_lift_k"] / (c["reference_c"] - target + c["approach_k"]))
    assert o.price_factor(CFG, "cool", 35.0) is None and o.price_factor(COOL, "heat", 35.0) is None


def test_the_plan_shows_the_price_of_cooling_with_its_factor():
    hot = forecast(lambda h: 33.0)
    rows = json.loads(o.plan_json(NOON, o.heat_prices(day_prices(), hot, COOL, "cool"), COOL, hot, None, "cool"))["rows"]
    r = rows[48]
    assert r["price"] == 8.0 and r["heat_factor"] > 1.0 and r["heat_price"] == pytest.approx(8.0 * r["heat_factor"], abs=0.05)


def test_the_plan_shows_the_spot_price_and_the_heat_price_beside_it():
    cold = forecast(lambda h: -8.0)
    heat = o.heat_prices(day_prices(), cold, ON)
    rows = json.loads(o.plan_json(NOON, heat, ON, cold, None))["rows"]
    assert {r["price"] for r in rows} == {8.0} and all(r["heat_price"] > 8.0 for r in rows)
    off = json.loads(o.plan_json(NOON, day_prices(), CFG, cold, None))["rows"]
    assert all(r["heat_price"] is None for r in off)


def test_the_plan_row_carries_the_factor_of_the_heat_price():
    cold = forecast(lambda h: -8.0)
    r = json.loads(o.plan_json(NOON, o.heat_prices(day_prices(), cold, ON), ON, cold, None))["rows"][48]
    assert r["heat_factor"] == pytest.approx(o.cop_at(COP, 0) / o.cop_at(COP, -8.0), abs=0.01)
    assert r["heat_price"] == pytest.approx(r["price"] * r["heat_factor"], abs=0.05)
    assert all(r["heat_factor"] is None for r in json.loads(o.plan_json(NOON, day_prices(), CFG, cold, None))["rows"])


def test_the_cold_hours_lose_a_cheap_quarters_boost_that_the_warm_ones_keep():
    """The same spot prices: in mild weather the six cheap hours (just under cheap_max) get their boost,
    in a hard frost the heat in them costs more than cheap_max and they do not."""
    cheap_max = CFG["prices"]["cheap_max"]
    prices = day_prices(lambda k: cheap_max - 0.2 if k < 24 else 15.0)
    kinds = lambda outdoor: [r["kind"] for r in json.loads(
        o.plan_json(NOON, o.heat_prices(prices, outdoor, ON), ON, outdoor, None))["rows"]]
    assert kinds(7.0).count("cheap") == 24 and kinds(-15.0).count("cheap") == 0


def test_comfort_and_minimum_heating_rules_judge_the_spot_price_whatever_the_cop():
    """The COP moves when heat is bought, never the minimums: the comfort windows' cut tiers and the
    nobody-home floors read the spot price, so a freezing forecast cannot deepen a cut past what that spot
    price allows, nor drop a floor."""
    nh = CFG["nobody_home"]
    for spot in (2.0, 9.0, 15.0, 19.0, 35.0, 49.0, 80.0):
        for temp in (-15.0, 0.0, 7.0):
            prices = o.heat_prices(day_prices(lambda k: spot), temp, ON)
            for hour in (7, 19, 21):   # inside the weekday comfort windows
                t = NOON.replace(hour=hour)
                base = o.base_setpoint(t, ON)
                cap = o.situation(t, spot, base, ON, False, None).cap
                final = o.desired_setpoint(t, prices, ON, 0.0, temp, None, None, "heat", False, None, 1.0, False, 0.0)[2]
                assert cap is None or final >= base - cap - 1e-9, (spot, temp, hour, final)
            away = o.Away(NOON + timedelta(days=1), NOON - timedelta(hours=8))
            floor = o.desired_setpoint(NOON.replace(hour=14), prices, ON, 0.0, temp, None, away, "heat", False,
                                       None, 1.0, False, 0.0)[2]
            assert floor >= nh["dear_temp"], (spot, temp, floor)
            assert o.desired_setpoint(NOON.replace(hour=3), prices, ON, 0.0, temp, None, None, "heat", False,
                                      None, 1.0, False, 0.0)[2] >= ON["limits"]["min_temp"]
