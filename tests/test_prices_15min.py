from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import ilp_optimizer as o

ROOT = Path(__file__).resolve().parent.parent
CFG = o.load_config(ROOT / "config.toml", ROOT / "tests" / "house.toml")
TZ = o.ZoneInfo(CFG["timezone"])


def feed(price_of_quarter, day=date(2026, 11, 10)):
    """A payload shaped like api.porssisahko.net/v2: 15-minute entries that end at hh:14:59.999."""
    start, end = o.local_day_bounds(day, TZ)
    out, t, k = [], start.astimezone(timezone.utc), 0
    while t < end:
        stop = t + timedelta(minutes=15) - timedelta(milliseconds=1)
        out.append({"price": price_of_quarter(k),
                    "startDate": t.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "endDate": stop.strftime("%Y-%m-%dT%H:%M:%S") + ".999Z"})
        t += timedelta(minutes=15)
        k += 1
    return {"prices": out}


def test_quarter_hour_feed_becomes_exact_contiguous_900_second_intervals():
    prices = o.parse_prices(feed(lambda k: 5.0))
    assert {i.seconds for i in prices} == {900.0}
    assert all(a.end == b.start for a, b in zip(prices, prices[1:]))


def test_hourly_feed_still_parses():
    payload = {"prices": [{"price": 5.0, "startDate": "2026-11-10T00:00:00.000Z",
                           "endDate": "2026-11-10T01:00:00.000Z"}]}
    assert o.parse_prices(payload)[0].seconds == 3600.0


def test_four_cheapest_hours_are_16_quarters_not_17():
    # 6 hours of 2c: only the configured number of cheap hours (in quarters) get the boost.
    prices = o.parse_prices(feed(lambda k: 2.0 if k < 24 else 9.0))
    shifts = o.plan_day_shifts(prices, date(2026, 11, 10), CFG)
    wanted = int(CFG["prices"]["cheap_hours"] * 4)
    assert sum(1 for s in shifts if s.reason.startswith("cheap")) == wanted


def test_spike_preheat_window_with_quarter_hour_prices():
    # Spike 17:00-18:00 (4 quarters of 40c) on a 9c day: pre-heat is 15:00-17:00 (8 quarters),
    # not stretched by each later spike quarter.
    spike = range(17 * 4, 18 * 4)
    prices = o.parse_prices(feed(lambda k: 40.0 if k in spike else 9.0))
    shifts = o.plan_day_shifts(prices, date(2026, 11, 10), CFG)
    pre = sorted(s.interval.start.astimezone(TZ) for s in shifts if s.reason.startswith("pre-heat"))
    assert len(pre) == 8
    assert pre[0].hour == 15 and pre[0].minute == 0 and pre[-1].hour == 16 and pre[-1].minute == 45


def test_setpoint_follows_the_quarter_hour():
    # Within one hour the price flips between quarters; the setpoint must follow each quarter.
    prices = o.parse_prices(feed(lambda k: 30.0 if k == 10 * 4 + 1 else 9.0))
    at = lambda h, m: datetime(2026, 11, 10, h, m, tzinfo=TZ)
    cut = o.price_shift_at(at(10, 20), prices, CFG)[0]
    normal = o.price_shift_at(at(10, 40), prices, CFG)[0]
    assert cut < 0 and normal == 0.0
