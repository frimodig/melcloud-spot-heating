import asyncio
import csv
from datetime import datetime, timedelta, timezone

import ilp_optimizer as o


def quarters(start, n, price=5.0):
    return [o.PriceInterval(start + timedelta(minutes=15 * k), start + timedelta(minutes=15 * (k + 1)),
                            price + k) for k in range(n)]


T0 = datetime(2026, 11, 11, 22, 0, tzinfo=timezone.utc)


def rows(path):
    return list(csv.DictReader(open(path)))


def test_first_write_has_a_header_and_every_interval(tmp_path):
    path = tmp_path / "p.csv"
    assert o.record_prices(path, quarters(T0, 96)) == 96
    r = rows(path)
    assert len(r) == 96 and r[0] == {"start_utc": "2026-11-11T22:00:00Z", "minutes": "15", "price": "5.0"}


def test_each_interval_is_stored_once(tmp_path):
    path = tmp_path / "p.csv"
    o.record_prices(path, quarters(T0, 96))
    assert o.record_prices(path, quarters(T0, 96)) == 0
    # The next run sees a window that moved on by a day: only the new day is added.
    assert o.record_prices(path, quarters(T0 + timedelta(days=1), 96)) == 96
    assert len(rows(path)) == 192


def test_hourly_intervals_record_their_length(tmp_path):
    path = tmp_path / "p.csv"
    hourly = [o.PriceInterval(T0, T0 + timedelta(hours=1), 7.0)]
    o.record_prices(path, hourly)
    assert rows(path)[0]["minutes"] == "60"


def test_no_prices_writes_nothing(tmp_path):
    path = tmp_path / "p.csv"
    assert o.record_prices(path, None) == 0 and o.record_prices(path, []) == 0
    assert not path.exists()


def test_write_failure_is_logged_not_raised(tmp_path, caplog):
    bad = tmp_path / "no_such_dir" / "p.csv"
    assert o.record_prices(bad, quarters(T0, 4)) == 0
    assert "price history not written" in caplog.text
