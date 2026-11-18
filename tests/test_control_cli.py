from datetime import datetime, timedelta
from pathlib import Path

import pytest

import ilp_optimizer as o

ROOT = Path(__file__).resolve().parent.parent
TZ = o.ZoneInfo("Europe/Helsinki")


def test_on_off_and_a_return_time_for_away():
    assert o.set_control("away", "on", TZ) == 0
    assert o.AWAY_FILE.read_text() == "" and o.load_away(o.AWAY_FILE, TZ) is not None
    assert o.set_control("away", "2026-11-14T18:00", TZ) == 0
    words = o.AWAY_FILE.read_text().split()
    assert words[0] == "2026-11-14T18:00+02:00"                         # the offset is written out
    assert datetime.fromisoformat(words[1]).tzinfo is not None          # and when it was set
    assert o.load_away(o.AWAY_FILE, TZ).until == datetime(2026, 11, 14, 18, 0, tzinfo=TZ)
    assert o.load_away(o.AWAY_FILE, TZ).since is not None
    assert o.set_control("away", "off", TZ) == 0 and not o.AWAY_FILE.exists()


def test_holiday_and_pause():
    assert o.set_control("holiday", "2026-12-20T12:00", TZ) == 0
    assert o.holiday_active(o.HOLIDAY_FILE, TZ, datetime(2026, 12, 1, tzinfo=TZ))
    assert o.set_control("pause", "on", TZ) == 0 and o.PAUSE_FILE.exists()
    assert o.set_control("pause", "off", TZ) == 0 and not o.PAUSE_FILE.exists()


@pytest.mark.parametrize("name,value", [("away", "tomorrow"), ("holiday", "yes"),
                                        ("pause", "2026-11-14T18:00"), ("pause", "later")])
def test_a_bad_value_is_refused_and_nothing_is_written(name, value):
    assert o.set_control(name, value, TZ) == 2
    assert not getattr(o, f"{name.upper()}_FILE").exists()


def test_the_cli_routes_control_and_rejects_an_unknown_name(capsys):
    assert o.main(["--config", str(ROOT / "config.toml"), "--control", "pause", "on"]) == 0
    assert o.PAUSE_FILE.exists()
    assert o.main(["--config", str(ROOT / "config.toml"), "--control", "heating", "on"]) == 2
    assert "away, holiday, pause" in capsys.readouterr().out


def test_only_one_run_holds_the_lock_and_it_frees_on_close():
    first = o.acquire_run_lock(o.LOCK_FILE)
    assert first is not None
    assert o.acquire_run_lock(o.LOCK_FILE) is None            # a second run skips
    first.close()
    again = o.acquire_run_lock(o.LOCK_FILE)
    assert again is not None
    again.close()


def test_waiting_for_the_lock_gives_up_after_the_wait():
    held = o.acquire_run_lock(o.LOCK_FILE)
    start = o.time_module.monotonic()
    assert o.acquire_run_lock(o.LOCK_FILE, wait_seconds=0.5) is None
    assert 0.4 <= o.time_module.monotonic() - start < 2.0
    held.close()


def test_a_live_run_skips_when_another_run_holds_the_lock(caplog):
    held = o.acquire_run_lock(o.LOCK_FILE)
    with caplog.at_level("INFO"):
        rc = o.main(["--config", str(ROOT / "config.toml"), "--dry-run"])
    held.close()
    assert rc == 0 and "another run is in progress" in caplog.text
