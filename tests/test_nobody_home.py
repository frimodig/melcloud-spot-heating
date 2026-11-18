"""The test house's weekday window and Poissa: nobody is home, so the house is not heated unless that saves
money later: 17 C, 16 C in a quarter over 10 c, more only under 5 c before a spike or a peak."""
from datetime import date, datetime, timedelta
from pathlib import Path

import ilp_optimizer as o

ROOT = Path(__file__).resolve().parent.parent
CFG = o.load_config(ROOT / "config.toml", ROOT / "tests" / "house.toml")
TZ = o.ZoneInfo(CFG["timezone"])
TUE = date(2026, 11, 10)


def at(hh, mm=0, day=TUE):
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ)


def prices(quarters, default=8.0, day=TUE):
    start, _ = o.local_day_bounds(day, TZ)
    return [o.PriceInterval(start + timedelta(minutes=15 * k), start + timedelta(minutes=15 * (k + 1)),
                            quarters.get(k, default)) for k in range(96)]


def sp(t, p, **kw):
    return o.desired_setpoint(t, p, CFG, **kw)


SPIKE_AT_NOON = {k: 30.0 for k in range(48, 52)}


def test_the_weekday_window_holds_17_and_16_over_10_cents():
    assert sp(at(10), prices({}))[2] == 17.0
    hi = sp(at(10), prices({}, default=12.0))
    assert hi[2] == 16.0 and o.reason_kind(hi[3]) == "nobody_home"
    assert sp(at(10), None)[2] == 17.0                               # no prices: the ordinary floor


def test_a_cheap_quarter_alone_does_not_heat_the_empty_house():
    assert sp(at(10), prices({}, default=3.0))[2] == 17.0            # no dearer quarter coming: no boost


def test_under_5_cents_before_a_spike_the_heat_is_banked():
    banked = sp(at(10, 30), prices(SPIKE_AT_NOON, default=3.0))
    assert banked[2] > 17.0 and "banking heat" in banked[3]
    assert sp(at(11), prices(SPIKE_AT_NOON, default=8.0))[2] == 17.0   # 8c is too dear to bank
    assert sp(at(12), prices(SPIKE_AT_NOON, default=3.0))[2] == 16.0   # the spike itself


def test_the_room_does_not_lift_the_empty_house_over_its_floor():
    assert sp(at(10), prices({}), room_temp=15.0)[2] == 17.0


def test_the_window_runs_08_to_15_on_weekdays_only():
    p = prices({})
    assert sp(at(7, 45), p)[2] == 22.0
    assert sp(at(14, 0), p, unit="Unit B")[2] == 17.0
    assert sp(at(15), p, unit="Unit B")[2] == 20.0             # someone is home
    assert sp(at(10), p, home=True)[2] == 22.0 and sp(at(10), p, holiday=True)[2] == 22.0
    sat = date(2026, 11, 14)
    assert sp(at(10, day=sat), prices({}, day=sat))[2] == 22.0


def test_poissa_has_the_same_floors_at_any_hour():
    assert sp(at(19), prices({}), away=o.Away())[2] == 17.0
    assert sp(at(19), prices({}, default=12.0), away=o.Away())[2] == 16.0
    assert sp(at(19), prices({}, default=3.0), away=o.Away())[2] == 17.0


def test_poissa_banks_heat_before_a_peak_under_5_cents():
    p = prices({k: 60.0 for k in range(84, 88)}, default=3.0)        # a 21:00 peak
    got = sp(at(19, 30), p, away=o.Away())
    assert got[2] > 17.0 and got[3].startswith("away") and "banking heat" in got[3]



def test_the_house_warms_up_for_the_first_arrival_at_15():
    p = prices({})
    got = [sp(at(h, m), p, unit="Unit B") for h, m in [(14, 0), (14, 15), (14, 30), (14, 45)]]
    # 7 h empty: the rooms coasted about 2.2 C, 45 minutes' heating, in steps to the 15:00 plan
    assert [g[2] for g in got] == [17.0, 18.0, 19.0, 20.0] and "warm-up for home 15:00" in got[1][3]


def test_the_warm_up_for_the_first_arrival_buys_its_heat_in_the_cheapest_quarters_of_two_hours():
    p = prices({k: 25.0 for k in range(52, 60)} | {52: 12.0, 53: 12.0})   # 13:00-15:00 dear but 13:00-13:30
    early = sp(at(13, 15), p, unit="Unit B")
    assert early[2] > 17.0 and "heating" in early[3]                 # bought early, where it is cheap
    assert "holding" in sp(at(14, 15), p, unit="Unit B")[3]
    assert sp(at(14, 45), p, unit="Unit B")[2] == sp(at(15), p, unit="Unit B")[2]   # arrives warm


def test_no_warm_up_for_the_first_arrival_while_away_or_on_a_kotona_day():
    p = prices({})
    assert sp(at(15), p, unit="Unit B", away=o.Away())[2] == 17.0
    assert "warm-up for home" not in sp(at(15), p, unit="Unit B", home=True)[3]
