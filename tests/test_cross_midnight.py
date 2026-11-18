from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import ilp_optimizer as o

ROOT = Path(__file__).resolve().parent.parent
CFG = o.load_config(ROOT / "tests" / "logic_config.toml")
TZ = ZoneInfo("Europe/Helsinki")
TODAY = date(2026, 11, 10)
X = CFG["extreme"]


def two_days(tomorrow_overrides, today_overrides=None, tomorrow_hours=24):
    """Hourly prices for TODAY (8 c) and the next day (8 c) with overrides by local hour."""
    start, _ = o.local_day_bounds(TODAY, TZ)
    out = []
    for k in range(24):
        out.append(o.PriceInterval(start + timedelta(hours=k), start + timedelta(hours=k + 1),
                                   (today_overrides or {}).get(k, 8.0)))
    nxt, _ = o.local_day_bounds(TODAY + timedelta(days=1), TZ)
    for k in range(tomorrow_hours):
        out.append(o.PriceInterval(nxt + timedelta(hours=k), nxt + timedelta(hours=k + 1),
                                   tomorrow_overrides.get(k, 8.0)))
    return out


def shifts(prices):
    return {s.interval.start.astimezone(TZ).hour: s for s in o.plan_day_shifts(prices, TODAY, CFG, 1.0)}


def test_a_spike_just_after_midnight_pre_heats_the_end_of_today():
    got = shifts(two_days({0: 40.0, 1: 40.0}))                       # 00:00-02:00 tomorrow at 40 c
    pre = {h for h, s in got.items() if s.reason.startswith("pre-heat before 40.0c spike")}
    assert pre == {22, 23}                                            # preheat_hours = 2
    assert all(got[h].offset == X["preheat_boost"] and got[h].extreme for h in pre)
    assert 21 not in got or not got[21].reason.startswith("pre-heat")


def test_a_spike_a_little_later_pre_heats_only_the_hours_inside_the_window():
    got = shifts(two_days({1: 40.0}))                                 # spike 01:00: window 23:00-01:00
    assert {h for h, s in got.items() if s.reason.startswith("pre-heat before")} == {23}


def test_a_spike_beyond_the_window_does_nothing_today():
    got = shifts(two_days({5: 40.0}))                                 # 05:00 is more than 2 h after midnight
    assert not any(s.reason.startswith("pre-heat before") for s in got.values())


def test_without_enough_prices_for_tomorrow_nothing_is_guessed():
    got = shifts(two_days({0: 40.0}, tomorrow_hours=6))               # tomorrow is still mostly unknown
    assert not any(s.reason.startswith("pre-heat before") for s in got.values())


def test_the_pre_heat_does_not_run_into_an_expensive_hour_of_today():
    got = shifts(two_days({0: 40.0}, today_overrides={23: 22.0}))     # 23:00 is itself dear (an extreme cut)
    assert got[23].reason.startswith("extreme price") and got[23].offset < 0
    assert got[22].reason.startswith("pre-heat before 40.0c spike")


def test_it_also_works_when_today_itself_is_flat_and_gets_no_plan():
    prices = two_days({0: 40.0})
    assert o.plan_day_shifts(two_days({}), TODAY, CFG, 1.0) == []     # flat both days: nothing at all
    assert {h for h, s in shifts(prices).items()} == {22, 23}         # but tomorrow's spike pre-heats tonight


def test_a_peak_of_today_outranks_a_carried_pre_heat():
    got = shifts(two_days({0: 40.0}, today_overrides={22: 60.0}))     # 22:00 is a 60 c peak today
    assert got[22].reason.startswith("peak price") and got[22].offset == -X["cut"]


def test_the_setpoint_at_23_00_follows_the_pre_heat():
    prices = two_days({0: 40.0, 1: 40.0})
    now = datetime(2026, 11, 10, 23, 0, tzinfo=TZ)
    base, offset, final, reason = o.desired_setpoint(now, prices, CFG)
    assert offset == X["preheat_boost"] and "pre-heat before 40.0c spike" in reason and final == base + offset


def test_tomorrows_own_plan_is_unchanged_by_this():
    prices = two_days({0: 40.0, 1: 40.0})
    tomorrow = o.plan_day_shifts(prices, TODAY + timedelta(days=1), CFG, 1.0)
    assert not any(s.reason.startswith("pre-heat before 40.0c spike") for s in tomorrow)
