from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

import ilp_optimizer as o

ROOT = Path(__file__).resolve().parent.parent
CFG = o.load_config(ROOT / "config.toml", ROOT / "tests" / "house.toml")  # the live schedule and windows
TZ = o.ZoneInfo(CFG["timezone"])
TUE, SAT = date(2026, 11, 10), date(2026, 11, 14)


def at(day, hh, mm=0):
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ)


def flat_day(day, price, spikes=None):
    """Quarter-hour prices for one local day at one price (spikes: hour -> price)."""
    start, _ = o.local_day_bounds(day, TZ)
    return [o.PriceInterval(start + timedelta(minutes=15 * k), start + timedelta(minutes=15 * (k + 1)),
                            (spikes or {}).get(k // 4, price)) for k in range(96)]


def final(now, prices, **kw):
    return o.desired_setpoint(now, prices, CFG, **kw)[2]


@pytest.mark.parametrize("hh,mm,want", [(3, 0, 19.0), (5, 0, 22.0), (7, 59, 22.0), (8, 0, 19.0),
                                         (14, 29, 19.0), (14, 30, 20.0), (17, 30, 22.0),
                                         (21, 59, 22.0), (22, 0, 21.0), (23, 45, 21.0)])
def test_weekday_schedule(hh, mm, want):
    assert o.base_setpoint(at(TUE, hh, mm), CFG) == want


@pytest.mark.parametrize("hh,mm,want", [(5, 59, 19.0), (6, 0, 22.0), (12, 0, 22.0), (16, 30, 22.0),
                                         (22, 0, 21.0), (23, 45, 21.0)])
def test_weekend_schedule(hh, mm, want):
    assert o.base_setpoint(at(SAT, hh, mm), CFG) == want


def test_a_cut_in_the_evening_window_is_limited_by_the_price_tier():
    # 20:00 is the one dear hour in a day that is otherwise cheap, so the normal cut picks it.
    for price, want_cut in [(9.0, 0.0), (15.0, 0.5), (25.0, 1.0)]:
        prices = flat_day(TUE, 8.0, {20: price})  # 9c over 8c is under the spike jump
        base, offset, fin, reason = o.desired_setpoint(at(TUE, 20, 0), prices, CFG)
        assert fin == base - want_cut, (price, reason)
        assert offset >= -want_cut


def test_the_cap_applies_to_extreme_cuts_too_and_a_peak_is_not_capped():
    got = final(at(TUE, 19, 0), flat_day(TUE, 3.0, {19: 40.0}))
    assert got == 21.0                                   # extreme cut would be -3, capped at -1
    peak = final(at(TUE, 19, 0), flat_day(TUE, 3.0, {19: 60.0}))
    assert peak == 19.0                                  # peak protector: full cut in comfort hours


def test_outside_the_windows_cuts_are_unlimited():
    got = final(at(TUE, 16, 0), flat_day(TUE, 3.0, {16: 40.0}), unit="Unit A")
    assert got == 17.0                                   # base 20 - extreme 3 (downstairs afternoon: no window)


def test_weekend_windows_follow_the_weekend_shape():
    prices = flat_day(SAT, 3.0, {9: 25.0, 14: 25.0})
    assert final(at(SAT, 9, 0), prices) == 21.0          # 22 - 1, inside 07-12
    assert final(at(SAT, 14, 0), prices) == 21.0         # out for the day is Poissa: the keep window caps it too


def test_cheap_intervals_are_boosted_even_when_they_are_not_the_cheapest_six_hours():
    # The evening is the cheapest part of the day, but the 2.5c night is nearly as cheap.
    spikes = {h: 1.0 for h in range(18, 24)}
    spikes.update({h: 2.5 for h in range(0, 6)})
    prices = flat_day(TUE, 7.0, spikes)
    boosted = [h for h in range(0, 6) if "cheap" in o.desired_setpoint(at(TUE, h), prices, CFG)[3]]
    assert boosted == [0, 1, 2, 3, 4, 5]
    # 01:00, not 03:00: the two hours before the 05:00 step up belong to the comfort pre-heat
    assert o.desired_setpoint(at(TUE, 1), prices, CFG)[1] == CFG["prices"]["cheap_all_boost"]
    off = {**CFG, "prices": {**CFG["prices"], "cheap_all": False}}
    assert "cheap" not in o.desired_setpoint(at(TUE, 1), prices, off)[3]
