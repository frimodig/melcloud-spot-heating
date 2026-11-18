import asyncio
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import ilp_optimizer as o

ROOT = Path(__file__).resolve().parent.parent
CFG = o.load_config(ROOT / "tests" / "logic_config.toml")
TZ = ZoneInfo("Europe/Helsinki")
SAMPLE = o.load_prices_file(ROOT / "sample_prices.json")


def local(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=TZ)


def day_prices(day, hourly):
    """Hourly intervals covering a local day; `hourly` is a list or a function."""
    start, end = o.local_day_bounds(day, TZ)
    out, k = [], 0
    while start < end:
        price = hourly(k) if callable(hourly) else hourly[k]
        out.append(o.PriceInterval(start, start + timedelta(hours=1), price))
        start += timedelta(hours=1)
        k += 1
    return out


# ---- schedule -------------------------------------------------------------

def test_weekday_schedule_entries():
    # 2026-11-10 is a Tuesday
    expected = {(0, 0): 19, (4, 59): 19, (5, 0): 21, (8, 29): 21, (8, 30): 19,
                (13, 59): 19, (14, 0): 21, (22, 29): 21, (22, 30): 20, (23, 59): 20}
    for (h, m), temp in expected.items():
        assert o.base_setpoint(local(2026, 11, 10, h, m), CFG) == temp


def test_weekend_schedule():
    assert o.base_setpoint(local(2026, 11, 14, 6, 59), CFG) == 19  # Saturday
    assert o.base_setpoint(local(2026, 11, 14, 7, 0), CFG) == 21
    assert o.base_setpoint(local(2026, 11, 15, 22, 29), CFG) == 21  # Sunday
    assert o.base_setpoint(local(2026, 11, 15, 22, 30), CFG) == 20
    assert o.base_setpoint(local(2026, 11, 15, 23, 59), CFG) == 20


def test_midnight_carry_over_uses_the_new_days_00_entry():
    assert o.base_setpoint(local(2026, 11, 10, 23, 59), CFG) == 20  # Tue late evening
    assert o.base_setpoint(local(2026, 11, 11, 0, 0), CFG) == 19    # Wed starts at 19
    assert o.base_setpoint(local(2026, 11, 13, 23, 59), CFG) == 20  # Fri late evening
    assert o.base_setpoint(local(2026, 11, 14, 0, 0), CFG) == 19    # Sat starts at 19


def test_schedule_without_midnight_entry_is_rejected():
    with pytest.raises(ValueError):
        o.parse_schedule([["08:00", 20]])


# ---- clamping and rounding ------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (17.0, 18.0), (23.5, 22.0), (19.74, 19.5), (19.75, 20.0), (19.25, 19.5),
    (20.0, 20.0), (19.5, 19.5),
])
def test_clamp_and_round(value, expected):
    assert o.clamp_and_round(value, CFG) == expected


# ---- price shifting -------------------------------------------------------

def test_sample_day_plan():
    now = local(2026, 11, 10, 4, 0)
    shifts = o.plan_day_shifts(SAMPLE, now.date(), CFG)
    hour = lambda s: s.interval.start.astimezone(TZ).hour
    assert sorted(hour(s) for s in shifts if s.reason.startswith("cheap")) == [1, 2, 3, 4, 5, 6]
    assert sorted(hour(s) for s in shifts if s.reason.startswith("pre-heat")) == [15, 16]
    cut = [s for s in shifts if s.offset < 0]
    assert sorted(hour(s) for s in cut) == [17, 18, 19, 20]
    assert {s.offset for s in cut} == {-3.0}  # 20c+ extreme cut replaces the -1.5 deep cut


def test_sample_day_final_setpoints():
    def final(h, m=0):
        return o.desired_setpoint(local(2026, 11, 10, h, m), SAMPLE, CFG)[2]
    assert final(2, 30) == 21     # base 19 + 2: the night pre-heat
    assert final(4, 0) == 21      # base 19 + 2 (no fixed 03:30 entry any more)
    assert final(5, 30) == 22     # base 21 + 2 capped at 22
    assert final(12, 0) == 19     # untouched base
    assert final(15, 30) == 23    # pre-heat before the 30.5c spike: 21 + 2
    assert final(18, 0) == 18     # base 21 - 3 (extreme cut)
    assert final(22, 0) == 21
    assert final(23, 0) == 20     # 22:30 step down


def test_ranking_is_per_local_day_not_sliding():
    # Same decision for every minute of the day.
    plans = {tuple(o.plan_day_shifts(SAMPLE, date(2026, 11, 10), CFG)) for _ in range(2)}
    assert len(plans) == 1


def test_boost_requires_absolute_threshold():
    # Cheapest hours are 9 c/kWh, above cheap_max 8: no boost. Peak still cut.
    hourly = lambda k: 9.0 + (k % 24) * 1.0
    shifts = o.plan_day_shifts(day_prices(date(2026, 11, 10), hourly), date(2026, 11, 10), CFG)
    assert all(s.offset < 0 for s in shifts) and shifts


def test_cut_requires_absolute_threshold():
    hourly = lambda k: 1.0 + 0.3 * k  # max 8.9 < expensive_min 12
    shifts = o.plan_day_shifts(day_prices(date(2026, 11, 10), hourly), date(2026, 11, 10), CFG)
    assert shifts and all(s.offset > 0 for s in shifts)


def test_normal_cut_between_thresholds():
    hourly = lambda k: 5.0 if k < 20 else 15.0  # 4 hours at 15 -> -1.0 each
    shifts = o.plan_day_shifts(day_prices(date(2026, 11, 10), hourly), date(2026, 11, 10), CFG)
    assert {s.offset for s in shifts if s.offset < 0} == {-1.0}


def test_small_spread_means_no_shifting():
    hourly = lambda k: 5.0 + (k % 2) * 3.9  # spread 3.9 < 4
    assert o.plan_day_shifts(day_prices(date(2026, 11, 10), hourly), date(2026, 11, 10), CFG) == []


# ---- fallback -------------------------------------------------------------

def test_fallback_when_prices_missing_or_short():
    now = local(2026, 11, 10, 4, 0)
    base = o.base_setpoint(now, CFG)
    assert o.desired_setpoint(now, None, CFG)[2] == base
    assert o.desired_setpoint(now, [], CFG)[2] == base
    short = [i for i in SAMPLE if i.start.astimezone(TZ).date() == date(2026, 11, 10)][:19]
    assert o.desired_setpoint(now, short, CFG)[2] == base


def test_fetch_failure_falls_back_to_base(monkeypatch):
    async def boom(cfg):
        raise OSError("network down")
    monkeypatch.setattr(o, "fetch_prices", boom)
    assert asyncio.run(o.get_prices_or_none(CFG, None)) is None


def test_only_todays_prices_are_used():
    # Full data for the 9th only: the 10th has no prices, so base schedule.
    prices = [i for i in SAMPLE if i.start.astimezone(TZ).date() == date(2026, 11, 9)]
    assert o.plan_day_shifts(prices, date(2026, 11, 10), CFG) == []


def test_interval_length_is_handled_by_duration():
    # 15-minute intervals: 4 cheap hours = 16 intervals.
    start, end = o.local_day_bounds(date(2026, 11, 10), TZ)
    prices, t, k = [], start, 0
    while t < end:
        hour = k // 4
        price = 2.0 if hour < 4 else (40.0 if 18 <= hour < 22 else 9.0)
        prices.append(o.PriceInterval(t, t + timedelta(minutes=15), price))
        t += timedelta(minutes=15)
        k += 1
    shifts = o.plan_day_shifts(prices, date(2026, 11, 10), CFG)
    assert sum(1 for s in shifts if s.reason.startswith("cheap")) == 16
    assert sum(1 for s in shifts if s.offset < 0) == 16


# ---- DST ------------------------------------------------------------------

def test_dst_day_lengths():
    for day, hours in [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25), (date(2026, 11, 10), 24)]:
        s, e = o.local_day_bounds(day, TZ)
        assert (e - s) == timedelta(hours=hours)


@pytest.mark.parametrize("day,hours", [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)])
def test_dst_day_ranking_and_midnight_entry(day, hours):
    prices = day_prices(day, lambda k: 3.0 if k in (1, 2, 3, 4) else (35.0 if k in (hours - 6, hours - 5, hours - 4, hours - 3) else 9.0))
    assert len(prices) == hours
    shifts = o.plan_day_shifts(prices, day, CFG)
    assert sum(1 for s in shifts if s.reason.startswith("cheap")) == 4
    assert sum(1 for s in shifts if s.offset < 0) == 4
    # Sunday: weekend schedule, 00:00 entry applies at local midnight
    assert o.desired_setpoint(datetime.combine(day, datetime.min.time(), tzinfo=TZ), None, CFG)[0] == 19


def test_dst_day_uses_wall_clock_for_schedule():
    # 08:00 local on the 25-hour day is still the 08:00 entry
    assert o.base_setpoint(local(2026, 10, 25, 8, 0), CFG) == 21


def test_plan_table_covers_dst_day():
    table = o.plan_table(local(2026, 10, 25, 12), None, CFG)
    assert table.splitlines()[2].startswith("00:00")


# ---- extreme rules and weather ---------------------------------------------

DAY = date(2026, 11, 10)


def hourly_prices(overrides, default=9.0):
    return day_prices(DAY, lambda k: overrides.get(k, default))


def setpoint(hour, prices, outdoor=None, minute=0):
    return o.desired_setpoint(local(2026, 11, 10, hour, minute), prices, CFG, 0.0, outdoor)


def test_cheap_night_before_an_expensive_day_is_pre_heated():
    # Night 3c, day 14c, evening peak 25c: the house stores heat at night (base 19 -> 21 at
    # 0 C outdoors, 22 once the 05:00 entry raises the base) and is cut in the peak.
    prices = hourly_prices({**{k: 3.0 for k in range(0, 6)}, **{k: 14.0 for k in range(6, 22)},
                            **{k: 25.0 for k in (17, 18, 19)}})
    assert [setpoint(h, prices, outdoor=0.0)[2] for h in (0, 2, 4)] == [21.0, 21.0, 21.0]
    assert setpoint(5, prices, outdoor=0.0)[2] == 22.0
    assert setpoint(18, prices, outdoor=0.0)[2] == 18.0
    assert setpoint(2, prices, outdoor=-10.0)[2] == 22.0  # colder: stronger pre-heat


def test_extreme_cut_may_go_below_normal_floor():
    # Tuesday 08:00-14:00 base is 19; a 25c hour at 10:00 cuts by 3 -> 16, floor 17.
    prices = hourly_prices({10: 25.0, 3: 2.0})
    assert setpoint(10, prices)[2] == 17.0


def test_extreme_cut_is_capped_to_the_priciest_hours():
    # Eight 22c hours 08-15 plus a cheap night: only the 6 priciest get the extreme cut.
    prices = hourly_prices({k: 22.0 + k * 0.01 for k in range(8, 16)} | {3: 2.0})
    cut = o.plan_day_shifts(prices, DAY, CFG)
    extreme = [s for s in cut if s.extreme and s.offset < 0]
    assert len(extreme) == CFG["extreme"]["cut_hours"] == 6
    others = [s for s in cut if s.reason.startswith("high price")]
    assert len(others) == 2 and all(s.offset == -1.0 for s in others)
    assert min(s.interval.price for s in extreme) > max(s.interval.price for s in others)


def test_extreme_cut_needs_the_absolute_price():
    prices = hourly_prices({10: 19.9, 3: 2.0})  # below cut_price 20: normal cut only
    final = setpoint(10, prices)[2]
    assert final >= CFG["limits"]["min_temp"]


def test_spike_preheat_goes_above_normal_ceiling():
    # median 9, spike 30 (>= 3x median and >= 15c) at 18:00; base 21 + 2 = 23 > 22.
    prices = hourly_prices({18: 30.0, 3: 2.0})
    assert setpoint(16, prices)[2] == 23.0 and setpoint(17, prices)[2] == 23.0
    assert setpoint(15, prices)[2] == 21.0  # outside the 2-hour window
    assert setpoint(18, prices)[2] == 18.0  # the spike itself is cut


def test_no_spike_when_below_ratio_or_min_price():
    prices = hourly_prices({18: 20.0, 3: 2.0})  # 2.2x median: not a spike
    assert setpoint(17, prices)[1] <= 0
    low = hourly_prices({18: 14.0}, default=3.0)  # 4.7x median but under 15c
    assert setpoint(17, low)[1] == 0


def test_preheat_never_heats_into_an_extreme_price_hour():
    # 17:00 costs 25c (extreme cut) right before the 40c spike at 18:00.
    prices = hourly_prices({17: 25.0, 18: 40.0, 3: 2.0})
    assert setpoint(17, prices)[2] == 18.0
    assert setpoint(16, prices)[2] == 23.0


def test_upper_extreme_limit_is_24():
    prices = hourly_prices({10: 30.0, 3: 2.0})
    cfg = {**CFG, "extreme": {**CFG["extreme"], "preheat_boost": 5.0}}
    # base 19 + 5 = 24 at 09:00
    assert o.desired_setpoint(local(2026, 11, 10, 9), prices, cfg)[2] == 24.0


@pytest.mark.parametrize("outdoor,factor", [
    (None, 1.0), (-25.0, 1.5), (-10.0, 1.5), (0.0, 1.0), (5.0, 0.75), (10.0, 0.5), (20.0, 0.5),
])
def test_weather_factor(outdoor, factor):
    assert o.weather_factor(outdoor, CFG) == pytest.approx(factor)


def test_weather_disabled_is_neutral():
    cfg = {**CFG, "weather": {**CFG["weather"], "enabled": False}}
    assert o.weather_factor(-20.0, cfg) == 1.0


def test_cold_scales_normal_shifts_up_and_mild_down():
    # 15:00 base is 21; a 12.5c hour gets the normal cut (-1.0 before scaling).
    prices = hourly_prices({15: 12.5, 3: 3.0, 4: 3.0, 5: 3.0, 6: 3.0})
    cold, mild, neutral = (setpoint(15, prices, outdoor=t) for t in (-20.0, 15.0, 0.0))
    assert (cold[1], neutral[1], mild[1]) == (-1.5, -1.0, -0.5)
    assert (cold[2], neutral[2], mild[2]) == (19.5, 20.0, 20.5)
    assert setpoint(4, prices, outdoor=-20.0)[1] == 3.0  # boost (2.0) scales too


def test_weather_does_not_scale_extreme_rules():
    prices = hourly_prices({10: 25.0, 3: 2.0})
    assert setpoint(10, prices, outdoor=-20.0)[1] == setpoint(10, prices, outdoor=20.0)[1] == -3.0


def test_weather_never_pushes_normal_shifts_beyond_normal_limits():
    # cold boost 3.0 at base 21 would be 24 and stays at 22
    prices = hourly_prices({6: 2.0})
    assert setpoint(6, prices, outdoor=-20.0)[2] == 22.0


def test_weather_fetch_failure_means_neutral(monkeypatch):
    async def boom(cfg, day):
        raise OSError("down")
    monkeypatch.setattr(o, "fetch_outdoor_mean", boom)
    assert asyncio.run(o.get_outdoor_or_none(CFG, local(2026, 11, 10, 12), None)) is None
    assert asyncio.run(o.get_outdoor_or_none(CFG, local(2026, 11, 10, 12), -5.0)) == -5.0


# ---- comfort guard and away -------------------------------------------------

def sp(hour, prices, room=None, away=None, minute=0):
    return o.desired_setpoint(local(2026, 11, 10, hour, minute), prices, CFG, 0.0, None, room, away)


def test_cut_is_ignored_when_the_room_is_too_cold():
    prices = hourly_prices({10: 25.0, 3: 2.0})  # extreme cut at 10:00 (base 19 -> 17)
    assert sp(10, prices, room=20.0)[2] == 17.0
    base, offset, final, reason = sp(10, prices, room=18.0)
    assert (offset, final) == (0.0, 19.0) and "comfort guard" in reason
    assert sp(10, prices, room=18.5)[2] == 17.0  # at the limit: guard needs strictly below


def test_boost_is_ignored_when_the_room_is_already_warm():
    prices = hourly_prices({18: 30.0, 3: 2.0})  # pre-heat at 17:00: base 21 + 2
    assert sp(17, prices, room=21.0)[2] == 23.0
    assert sp(17, prices, room=23.0)[2] == 21.0
    assert "comfort guard" in sp(17, prices, room=23.5)[3]


def test_guard_does_nothing_without_a_room_reading_or_without_a_shift():
    prices = hourly_prices({10: 25.0, 3: 2.0})
    assert sp(10, prices, room=None)[2] == 17.0
    assert sp(12, None, room=10.0)[2] == 19.0  # no price shift, nothing to guard


TUE_NOON = local(2026, 11, 10, 12)
AWAY = o.Away()  # no return time


def test_away_is_flat_17_with_no_price_shifting():
    prices = hourly_prices({15: 12.5, 17: 40.0, 4: 2.0})
    for hour in (4, 10, 15, 17, 22):
        base, offset, final, reason = o.desired_setpoint(local(2026, 11, 10, hour), prices, CFG, 0.0, -20.0, None, AWAY)
        assert (final, offset) == (17.0, 0.0) and reason.startswith("away")
    assert sp(15, None, away=False)[2] == 21.0


def test_away_until_return_time_preheats_before_return():
    ret = local(2026, 11, 10, 18, 0)
    away = o.Away(ret)  # preheat_hours = 3 -> normal heating from 15:00
    at = lambda h, m=0: o.desired_setpoint(local(2026, 11, 10, h, m), None, CFG, 0.0, None, None, away)
    assert at(14, 59)[2] == 17.0
    final, reason = at(15, 0)[2], at(15, 0)[3]
    assert 17.0 < final < 21.0 and reason.startswith("pre-heat before return")   # the first step up
    assert at(17, 59)[2] == 21.0


def test_away_expired_means_normal_schedule():
    away = o.Away(local(2026, 11, 9, 18, 0))
    assert o.desired_setpoint(TUE_NOON, None, CFG, 0.0, None, None, away)[2] == 19.0


def test_away_file_variants(tmp_path):
    f = tmp_path / "AWAY"
    assert o.load_away(f, TZ) is None
    f.write_text("")
    assert o.load_away(f, TZ) == o.Away(None)
    f.write_text("2026-11-14T18:00\n")
    assert o.load_away(f, TZ) == o.Away(local(2026, 11, 14, 18, 0))
    f.write_text("friday evening")  # typo: comfort first, ignore AWAY
    assert o.load_away(f, TZ) is None


def test_plan_table_in_away_mode():
    table = o.plan_table(TUE_NOON, None, CFG, None, AWAY)
    assert "away" in table and "17.0" in table
