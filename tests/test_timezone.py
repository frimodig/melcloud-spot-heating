"""Everything runs on Helsinki time, whatever timezone the host machine is set to."""
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import ilp_optimizer as o

ROOT = Path(__file__).resolve().parent.parent
CFG = o.load_config(ROOT / "tests" / "logic_config.toml")
TZ = ZoneInfo("Europe/Helsinki")


@pytest.fixture
def host_timezone(monkeypatch):
    """Switch the machine's own timezone, as the Oracle VM (UTC) differs from Helsinki."""
    def switch(name):
        monkeypatch.setenv("TZ", name)
        time.tzset()
    yield switch
    monkeypatch.undo()
    time.tzset()


def test_configured_timezone_is_helsinki():
    assert CFG["timezone"] == "Europe/Helsinki"


@pytest.mark.parametrize("utc,expected", [
    (datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc), "2026-11-10 12:00:00,000"),   # EET, UTC+2
    (datetime(2026, 7, 10, 10, 0, tzinfo=timezone.utc), "2026-07-10 13:00:00,000"),    # EEST, UTC+3
])
@pytest.mark.parametrize("host", ["UTC", "America/Los_Angeles", "Asia/Tokyo"])
def test_log_lines_use_helsinki_time_on_any_host(host_timezone, utc, expected, host):
    host_timezone(host)
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "msg", None, None)
    record.created, record.msecs = utc.timestamp(), 0
    assert o.LocalTimeFormatter("%(asctime)s %(message)s", TZ).format(record) == f"{expected} msg"


def test_the_log_file_written_by_setup_logging_is_in_helsinki_time(host_timezone):
    host_timezone("America/Los_Angeles")
    o.setup_logging(CFG)
    try:
        o.log.info("hello")
    finally:
        for h in list(o.log.handlers):
            o.log.removeHandler(h)
            h.close()
    line = (o.APP_DIR / CFG["log_file"]).read_text().splitlines()[-1]
    stamp = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
    assert abs(datetime.now(TZ) - stamp) < timedelta(minutes=1)


@pytest.mark.parametrize("host", ["UTC", "America/Los_Angeles", "Pacific/Auckland"])
def test_decisions_do_not_depend_on_the_host_timezone(host_timezone, host):
    host_timezone(host)
    tue_0930 = datetime(2026, 11, 10, 9, 30, tzinfo=TZ)
    assert o.desired_setpoint(tue_0930, None, CFG)[2] == 19.0            # a weekday, nobody home
    assert o.desired_setpoint(datetime(2026, 11, 14, 8, 30, tzinfo=TZ), None, CFG)[2] == 21.0
    start, end = o.local_day_bounds(datetime(2026, 10, 25, tzinfo=TZ).date(), TZ)
    assert end - start == timedelta(hours=25)                            # the DST day
    # a UTC instant that is already 'tomorrow' in Helsinki plans for the Helsinki day
    late_utc = datetime(2026, 11, 10, 22, 30, tzinfo=timezone.utc)       # 00:30 on the 11th in Helsinki
    assert o.desired_setpoint(late_utc, None, CFG)[2] == 19.0
    assert "00:00" in o.plan_table(late_utc, None, CFG).splitlines()[2]


def test_parse_now_defaults_to_helsinki_and_reads_naive_text_as_helsinki():
    assert o.parse_now(None, TZ).utcoffset() in (timedelta(hours=2), timedelta(hours=3))
    assert o.parse_now("2026-11-10T09:00", TZ).utcoffset() == timedelta(hours=2)
    assert o.parse_now("2026-07-10T09:00", TZ).utcoffset() == timedelta(hours=3)


def test_state_files_store_helsinki_time_even_when_now_is_utc(tmp_path):
    now_utc = datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc)         # 12:00 in Helsinki
    breaker = o.CircuitBreaker(tmp_path / "b.json", 1, timedelta(minutes=60), TZ)
    breaker.record_failure(now_utc)
    assert "2026-11-10T13:00:00+02:00" in (tmp_path / "b.json").read_text()

    class Unit:
        name, operation_mode, power, target_temperature, room_temperature = "a", "heat", True, 25.0, 21.0

        async def update(self):
            pass

        async def set(self, props):
            self.target_temperature = props["target_temperature"]
    import asyncio
    state = o.UnitState(tmp_path / "u.json")
    asyncio.run(o.apply_to_units([Unit()], now_utc, None, CFG, False, None, None, None, state))
    unit = Unit()
    unit.target_temperature = 22.0                                       # someone changes it by hand
    asyncio.run(o.apply_to_units([unit], now_utc + timedelta(minutes=15), None, CFG, False, None,
                                 None, None, state))
    assert re.search(r"2026-11-10T13:15:00\+02:00", (tmp_path / "u.json").read_text())


def test_requirements_include_the_timezone_database():
    assert "tzdata" in (ROOT / "requirements.txt").read_text()
