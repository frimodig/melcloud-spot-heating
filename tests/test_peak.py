from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import ilp_optimizer as o

ROOT = Path(__file__).resolve().parent.parent
CFG = o.load_config(ROOT / "tests" / "logic_config.toml")
TZ = o.ZoneInfo(CFG["timezone"])
DAY = date(2026, 11, 10)  # a Tuesday: base 21 from 14:00 on
X = CFG["extreme"]


def prices_for(overrides, default=8.0, days=1, hours=None):
    """Hourly intervals for `days` local days from DAY; overrides maps hour index -> c/kWh."""
    start, _ = o.local_day_bounds(DAY, TZ)
    n = hours if hours is not None else 24 * days
    return [o.PriceInterval(start + timedelta(hours=k), start + timedelta(hours=k + 1),
                            overrides.get(k, default)) for k in range(n)]


def shifts(prices, day=DAY, mode="heat"):
    return {s.interval.start.astimezone(TZ).hour + 24 * (s.interval.start.astimezone(TZ).date() - DAY).days: s
            for s in o.plan_day_shifts(prices, day, CFG, 1.0, mode)}


def test_every_peak_interval_is_cut_not_only_the_six_priciest_hours():
    prices = prices_for({h: 55.0 for h in range(10, 18)})  # eight peak hours
    cut = {h: s for h, s in shifts(prices).items() if s.reason.startswith("peak price")}
    assert sorted(cut) == list(range(10, 18))
    assert {s.offset for s in cut.values()} == {-X["cut"]} and all(s.extreme for s in cut.values())


def test_peak_rule_works_on_short_data_and_a_flat_day():
    ten_hours = prices_for({9: 60.0}, hours=10)  # far fewer than min_hours_available
    got = shifts(ten_hours)
    assert got[9].reason.startswith("peak price")
    assert sorted(h for h in got if h != 9) == [6, 7, 8]  # the pre-heat window, nothing else
    assert all(got[h].reason.startswith("pre-heat before peak") for h in (6, 7, 8))
    assert shifts(prices_for({}, default=60.0)) != {}  # a day at 60c all day has no spread, still cut


def test_pre_heat_before_a_peak_overrides_the_extreme_cut_up_to_the_price_limit():
    # 15:00 20c and 16:00 29c would be extreme-cut hours; 17:00 35c is too dear to pre-heat.
    prices = prices_for({15: 20.0, 16: 29.0, 17: 35.0, 18: 55.0})
    got = shifts(prices)
    assert got[15].reason.startswith("pre-heat before peak") and got[15].offset == X["peak_preheat_boost"]
    assert got[16].reason.startswith("pre-heat before peak")
    assert got[17].reason.startswith("extreme price") and got[17].offset == -X["cut"]
    assert got[18].reason.startswith("peak price")


def test_pre_heat_window_is_only_before_the_first_interval_of_a_peak_run():
    prices = prices_for({18: 55.0, 19: 55.0, 20: 55.0, 21: 25.0})
    got = shifts(prices)
    pre = sorted(h for h, s in got.items() if s.reason.startswith("pre-heat before peak"))
    assert pre == [15, 16, 17]  # the 3 hours before 18:00, none after the run, none inside it


def test_peak_just_after_midnight_pre_heats_the_evening_before():
    prices = prices_for({25: 60.0}, days=2)  # 01:00 the next day
    got = shifts(prices, DAY)
    pre = sorted(h for h, s in got.items() if s.reason.startswith("pre-heat before peak"))
    assert pre == [22, 23]  # 22:00 and 23:00; 00:00 belongs to the next day's plan
    nxt = shifts(prices, DAY + timedelta(days=1))
    assert nxt[24].reason.startswith("pre-heat before peak") and nxt[25].reason.startswith("peak price")


def test_cooling_has_no_peak_rule_and_does_not_break():
    prices = prices_for({10: 60.0})
    assert not any("peak" in s.reason for s in shifts(prices, mode="cool").values())


def test_setpoints_follow_the_peak_rule():
    prices = prices_for({16: 29.0, 18: 55.0})
    at = lambda h: datetime(2026, 11, 10, h, 0, tzinfo=TZ)
    # Pre-heat: base 21 + 3 capped at the extreme ceiling (24); weather does not scale it.
    assert o.desired_setpoint(at(16), prices, CFG, 0.0, 10.0)[2] == 24.0
    # Peak: base 21 - 3 = 18.
    assert o.desired_setpoint(at(18), prices, CFG, 0.0, -20.0)[2] == 18.0


def test_home_keeps_its_small_cap_on_the_peak_cut():
    prices = prices_for({18: 55.0})
    at = datetime(2026, 11, 10, 18, 0, tzinfo=TZ)
    base, offset, final, reason = o.desired_setpoint(at, prices, CFG, 0.0, None, None, None, "heat", False, None, 1.0, True)
    assert offset == -CFG["home"]["max_cut"] and "home" in reason


def test_no_peak_means_the_plan_is_unchanged():
    prices = prices_for({10: 49.9})  # just below peak_price
    assert not any("peak" in s.reason for s in shifts(prices).values())


def guarded(prices, hour, room):
    at = datetime(2026, 11, 10, hour, 0, tzinfo=TZ)
    return o.desired_setpoint(at, prices, CFG, 0.0, None, room)


def test_comfort_guard_lets_rooms_drop_further_during_a_peak():
    peak = prices_for({18: 55.0})
    # Room 18.0 is under [comfort] room_min (18.5) but a peak uses peak_room_min (17.0): cut stays.
    final, reason = guarded(peak, 18, 18.0)[2:]
    assert final == 18.0 and "comfort guard" not in reason
    # Below peak_room_min the guard still wins and the setpoint goes back to the base (21).
    final, reason = guarded(peak, 18, 16.5)[2:]
    assert final == 21.0 and "comfort guard" in reason


def test_comfort_guard_is_unchanged_outside_a_peak():
    extreme = prices_for({18: 25.0})  # an ordinary extreme cut, not a peak
    final, reason = guarded(extreme, 18, 18.0)[2:]
    assert final == 21.0 and "comfort guard" in reason
