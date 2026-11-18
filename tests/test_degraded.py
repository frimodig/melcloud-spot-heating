"""With no price, weather or solar data the controller must still hold the base schedule."""
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import ilp_optimizer as o

ROOT = Path(__file__).resolve().parent.parent
CFG = o.load_config(ROOT / "tests" / "logic_config.toml")
TZ = ZoneInfo("Europe/Helsinki")


class Unit:
    def __init__(self, name, mode="heat", target=25.0):
        self.name, self.operation_mode, self.target_temperature = name, mode, target
        self.power, self.room_temperature, self.writes = True, 21.0, []

    async def update(self):
        pass

    async def set(self, props):
        self.writes.append(props)
        self.target_temperature = props.get("target_temperature", self.target_temperature)


def every_quarter_hour(day):
    start, end = o.local_day_bounds(day, TZ)
    t = start
    while t < end:
        yield t
        t += timedelta(minutes=15)


@pytest.mark.parametrize("mode", ["heat", "cool"])
def test_whole_day_without_any_external_data_is_the_plain_schedule(mode):
    for day_number in range(10, 17):  # a full week, Tue..Mon
        day = datetime(2026, 11, day_number, tzinfo=TZ).date()
        for t in every_quarter_hour(day):
            base, offset, final, reason = o.desired_setpoint(t, None, CFG, 0.0, None, None, None, mode)
            expected = o.base_setpoint(t.astimezone(TZ), CFG) if mode == "heat" else CFG["cool"]["temp"]
            assert (offset, final) == (0.0, expected), (t, reason)


def test_short_or_stale_prices_are_the_plain_schedule():
    start, _ = o.local_day_bounds(datetime(2026, 11, 10, tzinfo=TZ).date(), TZ)
    yesterday = [o.PriceInterval(start - timedelta(hours=24 - k), start - timedelta(hours=23 - k),
                                 45.0 if k > 20 else 1.0) for k in range(24)]  # only yesterday's data
    ten_hours = [o.PriceInterval(start + timedelta(hours=k), start + timedelta(hours=k + 1),
                                 45.0 if k > 7 else 1.0) for k in range(10)]  # below peak_price: that rule ignores data length
    for prices in (yesterday, ten_hours, []):
        for t in every_quarter_hour(datetime(2026, 11, 10, tzinfo=TZ).date()):
            assert o.desired_setpoint(t, prices, CFG)[1] == 0.0


def test_negative_and_zero_prices_do_not_break_the_spike_rule():
    start, _ = o.local_day_bounds(datetime(2026, 11, 10, tzinfo=TZ).date(), TZ)
    for median_price in (-5.0, 0.0):
        prices = [o.PriceInterval(start + timedelta(hours=k), start + timedelta(hours=k + 1),
                                  median_price if k != 18 else 3.0) for k in range(24)]
        shifts = o.plan_day_shifts(prices, datetime(2026, 11, 10, tzinfo=TZ).date(), CFG)
        assert not any(s.reason.startswith("pre-heat") for s in shifts)  # 3c is not a spike


def test_run_with_every_data_source_down_still_writes_the_schedule(monkeypatch):
    async def down(*args, **kwargs):
        raise OSError("no network for prices, weather or sun")
    monkeypatch.setattr(o, "fetch_prices", down)
    monkeypatch.setattr(o, "fetch_outdoor_mean", down)
    monkeypatch.setattr(o, "fetch_sun", down)
    down_unit, up_unit = Unit("Unit A"), Unit("Unit B")

    async def devices(session):
        return [down_unit, up_unit]
    monkeypatch.setattr(o, "melcloud_devices", devices)
    args = o.argparse.Namespace(config=str(ROOT / "tests" / "logic_config.toml"), list=False, plan=False, report=None, control=None, lock_wait=0.0, plan_json=False,
                                prices_file=None, now="2026-11-10T09:00", dry_run=False,
                                outdoor_temp=None, away=False, away_until=None, season=None,
                                holiday=False, sun=None, mode="heat", home=None, assume_home=False)
    try:
        assert asyncio.run(o.run(args)) == 0
    finally:
        for h in list(o.log.handlers):
            o.log.removeHandler(h)
            h.close()
    # Tuesday 09:00: base 19 (nobody home); downstairs +0.5 offset
    assert down_unit.writes == [{"target_temperature": 19.5}]
    assert up_unit.writes == [{"target_temperature": 19.0}]
    log = (o.APP_DIR / CFG["log_file"]).read_text()
    assert "price fetch failed" in log and "weather fetch failed" in log
    assert "solar forecast fetch failed" in log


def test_a_failed_price_fetch_returns_a_unit_from_a_price_cut_to_the_schedule(monkeypatch):
    """Comfort first: if the prices vanish while a unit sits on a cut, it goes back to base."""
    unit = Unit("Unit B", target=17.0)  # left on an extreme cut by the previous run
    asyncio.run(o.apply_to_units([unit], datetime(2026, 11, 10, 17, 0, tzinfo=TZ), None, CFG, False))
    assert unit.writes == [{"target_temperature": 21.0}]
