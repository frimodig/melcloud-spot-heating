import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import pytest

import ilp_optimizer as o

ROOT = Path(__file__).resolve().parent.parent
CFG = o.load_config(ROOT / "config.toml", ROOT / "tests" / "house.toml")
R = CFG["resilience"]


def flaky(failures, exc):
    calls = []

    async def fn():
        calls.append(1)
        if len(calls) <= failures:
            raise exc
        return "ok"
    return fn, calls


def http_error(status):
    return aiohttp.ClientResponseError(None, (), status=status)


def run(fn):
    return asyncio.run(o.with_retry(fn, "test", CFG))


# ---- retry ------------------------------------------------------------------

def test_retries_transient_errors_then_succeeds():
    fn, calls = flaky(2, OSError("dns"))
    assert run(fn) == "ok" and len(calls) == 3


@pytest.mark.parametrize("exc", [OSError("dns"), asyncio.TimeoutError(), http_error(503), http_error(429),
                                 aiohttp.ClientConnectionError()])
def test_transient_kinds(exc):
    assert o.is_transient(exc)


@pytest.mark.parametrize("exc", [AttributeError("bad login"), http_error(401), http_error(400), ValueError()])
def test_non_transient_errors_are_not_retried(exc):
    fn, calls = flaky(99, exc)
    with pytest.raises(type(exc)):
        run(fn)
    assert len(calls) == 1  # never repeat a rejected login


def test_gives_up_after_configured_attempts():
    fn, calls = flaky(99, OSError("down"))
    with pytest.raises(OSError):
        run(fn)
    assert len(calls) == R["retry_attempts"]


def test_backoff_doubles_and_is_capped(monkeypatch):
    delays = []

    async def record(d):
        delays.append(d)
    monkeypatch.setattr(o, "SLEEP", record)
    cfg = {**CFG, "resilience": {**R, "retry_attempts": 6, "retry_base_delay_seconds": 5,
                                  "retry_max_delay_seconds": 30}}
    fn, _ = flaky(99, OSError())
    with pytest.raises(OSError):
        asyncio.run(o.with_retry(fn, "t", cfg))
    assert delays == [5, 10, 20, 30, 30]


def test_hung_operation_times_out_and_is_retried():
    cfg = {**CFG, "resilience": {**R, "operation_timeout_seconds": 0.05, "retry_attempts": 2}}
    calls = []

    async def hang():
        calls.append(1)
        await asyncio.sleep(10)
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(o.with_retry(hang, "t", cfg))
    assert len(calls) == 2


# ---- circuit breaker --------------------------------------------------------

NOW = datetime(2026, 11, 11, 10, 0, tzinfo=timezone.utc)


def breaker(tmp_path, threshold=3, minutes=60):
    return o.CircuitBreaker(tmp_path / "state.json", threshold, timedelta(minutes=minutes))


def test_breaker_opens_after_threshold_consecutive_failures(tmp_path):
    b = breaker(tmp_path)
    b.record_failure(NOW)
    b.record_failure(NOW)
    assert b.open_until(NOW) is None
    b.record_failure(NOW)
    assert b.open_until(NOW) == NOW + timedelta(minutes=60)


def test_breaker_stays_open_through_cool_off_then_allows_a_trial(tmp_path):
    b = breaker(tmp_path)
    for _ in range(3):
        b.record_failure(NOW)
    assert b.open_until(NOW + timedelta(minutes=59)) is not None
    assert b.open_until(NOW + timedelta(minutes=61)) is None  # trial run allowed


def test_failed_trial_reopens_immediately(tmp_path):
    b = breaker(tmp_path)
    for _ in range(3):
        b.record_failure(NOW)
    later = NOW + timedelta(minutes=61)
    b.record_failure(later)  # one failure is enough: the count is still at the threshold
    assert b.open_until(later) == later + timedelta(minutes=60)


def test_success_resets_the_count(tmp_path):
    b = breaker(tmp_path)
    b.record_failure(NOW)
    b.record_failure(NOW)
    b.record_success()
    b.record_failure(NOW)
    b.record_failure(NOW)
    assert b.open_until(NOW) is None  # needs 3 in a row again


def test_corrupt_state_file_means_closed(tmp_path):
    (tmp_path / "state.json").write_text("{not json")
    assert breaker(tmp_path).open_until(NOW) is None


# ---- wired into run() -------------------------------------------------------

def run_cli(tmp_path, monkeypatch, devices_fn):
    monkeypatch.setattr(o, "APP_DIR", tmp_path)
    monkeypatch.setattr(o, "PAUSE_FILE", tmp_path / "PAUSE")
    monkeypatch.setattr(o, "melcloud_devices", devices_fn)

    async def no_prices(cfg):
        return []
    monkeypatch.setattr(o, "fetch_prices", no_prices)
    args = o.argparse.Namespace(config=str(ROOT / "config.toml"), list=False, plan=False, report=None, control=None, lock_wait=0.0, plan_json=False,
                                prices_file=None, now=None, dry_run=True, outdoor_temp=0.0, away=False, away_until=None,
                                season=None, holiday=False, sun=None,
                                home=None, assume_home=False)
    try:
        return asyncio.run(o.run(args))
    finally:
        for h in list(o.log.handlers):
            o.log.removeHandler(h)
            h.close()


def test_run_opens_breaker_then_skips_melcloud_until_cool_off(tmp_path, monkeypatch):
    calls = []

    async def down(session):
        calls.append(1)
        raise OSError("no network")

    for _ in range(3):
        assert run_cli(tmp_path, monkeypatch, down) == 1
    assert len(calls) == 3 * R["retry_attempts"]  # each run retried before failing

    assert run_cli(tmp_path, monkeypatch, down) == 0  # breaker open: skipped
    assert len(calls) == 3 * R["retry_attempts"]
    assert "circuit breaker open" in (tmp_path / "ilp_optimizer.log").read_text()


def test_run_recovers_after_cool_off(tmp_path, monkeypatch):
    b = o.CircuitBreaker(tmp_path / R["state_file"], 3, timedelta(minutes=60))
    long_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    for _ in range(3):
        b.record_failure(long_ago)

    async def up(session):
        return []
    monkeypatch.setattr(o, "APP_DIR", tmp_path)
    assert run_cli(tmp_path, monkeypatch, up) == 0
    assert not (tmp_path / R["state_file"]).exists()  # success closed the breaker


def test_bad_credentials_are_not_retried_but_count_toward_the_breaker(tmp_path, monkeypatch):
    calls = []

    async def rejected(session):
        calls.append(1)
        raise AttributeError("'NoneType' object has no attribute 'get'")
    assert run_cli(tmp_path, monkeypatch, rejected) == 1
    assert len(calls) == 1
    assert '"failures": 1' in (tmp_path / R["state_file"]).read_text()
