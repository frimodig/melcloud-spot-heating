import asyncio
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import ilp_optimizer as o

ROOT = Path(__file__).resolve().parent.parent
CFG = o.load_config(ROOT / "tests" / "logic_config.toml")
TZ = ZoneInfo("Europe/Helsinki")
DAY = date(2026, 11, 10)  # a Tuesday


def at(hour, minute=0, day=DAY):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)


def hourly(overrides, default=6.0, day=DAY):
    start, end = o.local_day_bounds(day, TZ)
    out, k = [], 0
    while start < end:
        out.append(o.PriceInterval(start, start + timedelta(hours=1), overrides.get(k, default)))
        start += timedelta(hours=1)
        k += 1
    return out


def sp(hour, prices=None, home=False, unit_offset=0.0, mode="heat", minute=0, away=None):
    return o.desired_setpoint(at(hour, minute), prices, CFG, unit_offset, None, None, away, mode,
                              False, None, 1.0, home)


# ---- what home changes ------------------------------------------------------------

def test_home_uses_the_weekend_schedule_on_a_weekday():
    assert sp(9)[2] == 19.0                    # weekday: nobody home from 08:00
    assert sp(9, home=True)[2] == 21.0         # weekend schedule: 21 from 08:00
    assert sp(2, home=True)[2] == 19.0         # still 19 at night


def test_home_caps_price_cuts_and_keeps_the_day_floor():
    prices = hourly({k: 30.0 for k in (10, 11, 12)} | {k: 2.0 for k in (2, 3, 4, 5)})
    assert sp(11, prices)[2] == 17.0                       # normal: extreme cut, base 19 - 3
    base, offset, final, reason = sp(11, prices, home=True)
    assert (base, offset, final) == (21.0, -0.5, 20.5) and "home: cut capped" in reason


def test_home_day_comfort_floor_beats_the_unit_offset_in_the_daytime_only():
    prices = hourly({k: 30.0 for k in (10, 11, 12)} | {k: 2.0 for k in (2, 3, 4, 5)})
    final, reason = sp(11, prices, home=True, unit_offset=-2.0)[2:]
    assert final == 20.0 and "home: day comfort limit" in reason   # 21 - 0.5 - 2 = 18.5 -> 20
    night = {2: 30.0} | {k: 2.0 for k in (10, 11, 12, 13)}         # a 30c hour at 02:00
    assert sp(2, hourly(night), home=True)[2] == 18.5              # 19 - 0.5: outside the day window


def test_boosts_and_pre_heat_still_apply_when_home():
    prices = hourly({18: 30.0, 3: 2.0})
    assert sp(16, prices, home=True)[2] == 23.0            # pre-heat before the 18:00 spike: 21 + 2


def test_home_in_cooling_caps_the_drift_and_the_daytime_ceiling():
    prices = hourly({k: 14.0 for k in (14, 15, 16)}, default=3.0)
    assert sp(15, prices, mode="cool")[2] == 25.0                  # normal: +2 extreme drift
    assert sp(15, prices, mode="cool", home=True)[2] == 23.5       # capped to +0.5
    assert sp(15, prices, mode="cool", home=True, unit_offset=3.0)[2] == 24.5   # day ceiling


def test_away_wins_over_home():
    assert sp(12, home=True, away=o.Away())[2] == 17.0


def test_plan_table_in_home_mode():
    assert "21.0" in o.plan_table(at(12), None, CFG, None, None, "heat", False, None, True)


def test_units_get_the_home_rules():
    class Unit:
        def __init__(self, name):
            self.name, self.operation_mode, self.target_temperature = name, "heat", 25.0
            self.power, self.room_temperature, self.writes = True, 21.0, []

        async def update(self):
            pass

        async def set(self, props):
            self.writes.append(props)
    down, up = Unit("Unit A"), Unit("Unit B")
    asyncio.run(o.apply_to_units([down, up], at(9), None, CFG, False, None, None, None, None, None,
                                 False, None, True))
    assert down.writes == [{"target_temperature": 21.5}] and up.writes == [{"target_temperature": 21.0}]


# ---- the HOME file and the --home command -----------------------------------------

def test_home_file_variants(tmp_path):
    f = tmp_path / "HOME"
    assert not o.holiday_active(f, TZ, at(12))
    f.write_text("")
    assert o.holiday_active(f, TZ, at(12))
    f.write_text("2026-11-10T18:00")
    assert o.holiday_active(f, TZ, at(12)) and not o.holiday_active(f, TZ, at(18, 1))
    f.write_text("sick")
    assert not o.holiday_active(f, TZ, at(12))   # typo: normal schedule


@pytest.mark.parametrize("now,value,expected", [
    (datetime(2026, 11, 10, 12, 0, tzinfo=TZ), "today", "2026-11-11T00:00+02:00"),
    (datetime(2026, 11, 10, 12, 0, tzinfo=TZ), "week", "2026-11-16T00:00+02:00"),   # Tue -> Mon
    (datetime(2026, 11, 15, 12, 0, tzinfo=TZ), "week", "2026-11-16T00:00+02:00"),   # Sunday: tonight
    (datetime(2026, 11, 9, 8, 0, tzinfo=TZ), "week", "2026-11-16T00:00+02:00"),     # Monday: to next Mon
    (datetime(2026, 10, 23, 12, 0, tzinfo=TZ), "week", "2026-10-26T00:00+02:00"),   # DST ends on the 25th
    (datetime(2026, 11, 10, 12, 0, tzinfo=TZ), "2026-11-12T18:30", "2026-11-12T18:30+02:00"),
])
def test_home_command_writes_the_end_time_in_helsinki_time(now, value, expected):
    assert o.set_home(value, TZ, now) == 0
    assert o.HOME_FILE.read_text().strip() == expected
    # and the file is read back as the same instant
    assert o.holiday_active(o.HOME_FILE, TZ, now) and not o.holiday_active(o.HOME_FILE, TZ, datetime.fromisoformat(expected) + timedelta(minutes=1))


def test_home_command_off_and_invalid(capsys):
    o.set_home("today", TZ, at(12))
    assert o.set_home("off", TZ, at(12)) == 0 and not o.HOME_FILE.exists()
    assert o.set_home("next tuesday", TZ, at(12)) == 2 and not o.HOME_FILE.exists()
    assert "today, week, off" in capsys.readouterr().out


def test_the_plan_applies_home_only_until_its_end():
    live = o.load_config(ROOT / "config.toml", ROOT / "tests" / "house.toml")
    tue = datetime(2026, 11, 10, 12, 0, tzinfo=TZ)
    rows = o.plan_rows(tue, None, live, 5.0, home=True, home_until=datetime(2026, 11, 10, 14, 0, tzinfo=TZ))
    by_time = {r["start"].strftime("%H:%M"): r["base"] for r in rows}
    assert by_time["13:45"] == 22.0                       # home: the weekend schedule
    assert by_time["14:00"] == 19.0 and by_time["15:00"] == 20.0   # after its end: the weekday schedule


def house_config(tmp_path):
    """config.toml with the example house's config.local.toml beside it, as an install has it."""
    (tmp_path / "config.toml").write_text((ROOT / "config.toml").read_text())
    (tmp_path / "config.local.toml").write_text((ROOT / "tests" / "house.toml").read_text())
    return str(tmp_path / "config.toml")


def test_tomorrows_plan_shows_a_home_that_ends_tomorrow_morning(capsys, tmp_path):
    o.HOME_FILE.write_text("2030-01-02T10:00\n")         # Wednesday; still ahead by the real clock
    assert o.main(["--config", house_config(tmp_path), "--plan-json", "--now", "2030-01-02T12:00", "--prices-file", str(ROOT / "sample_prices.json"),
                   "--outdoor-temp", "0", "--sun", "0"]) == 0
    rows = {r["start"][11:16]: r for r in json.loads(capsys.readouterr().out)["rows"]}
    assert rows["09:00"]["base"] == 22.0                  # home until 10: the weekend schedule (weekday: 19)
    assert rows["10:00"]["base"] == 19.0                  # then the weekday schedule


def test_tomorrows_plan_shows_a_holiday_that_ends_tomorrow_morning(capsys, tmp_path):
    o.HOLIDAY_FILE.write_text("2030-01-02T10:00\n")      # Wednesday; still ahead by the real clock
    assert o.main(["--config", house_config(tmp_path), "--plan-json", "--now", "2030-01-02T12:00",
                   "--prices-file", str(ROOT / "sample_prices.json"), "--outdoor-temp", "0", "--sun", "0"]) == 0
    rows = {r["start"][11:16]: r for r in json.loads(capsys.readouterr().out)["rows"]}
    assert rows["09:00"]["base"] == 22.0                  # holiday until 10: the weekend schedule (weekday: 19)
    assert rows["10:00"]["base"] == 19.0
