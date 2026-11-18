import argparse
import asyncio
from pathlib import Path

import pytest

import ilp_optimizer as o

ROOT = Path(__file__).resolve().parent.parent
CFG = o.load_config(ROOT / "config.toml", ROOT / "tests" / "house.toml")


class FakeResp:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError("secret-url-in-message")


@pytest.fixture
def pings(monkeypatch):
    """Capture heartbeat GETs; no test may reach the network."""
    import aiohttp
    class Calls(list):
        status = {"code": 200}
    calls = Calls()
    status = Calls.status

    class FakeSession:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def get(self, url):
            calls.append(url)
            return FakeResp(status["code"])

    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)
    return calls


def beat():
    asyncio.run(o.send_heartbeat(CFG))


def test_no_url_means_no_ping(monkeypatch, pings):
    monkeypatch.delenv("HEARTBEAT_URL", raising=False)
    beat()
    assert pings == []


def test_pings_the_configured_url(monkeypatch, pings):
    monkeypatch.setenv("HEARTBEAT_URL", "https://hc.example/abc")
    beat()
    assert pings == ["https://hc.example/abc"]


def test_failed_ping_is_logged_without_the_url(monkeypatch, pings, caplog):
    monkeypatch.setenv("HEARTBEAT_URL", "https://hc.example/abc")
    pings.status["code"] = 500
    beat()  # must not raise
    assert "heartbeat ping failed (RuntimeError)" in caplog.text
    assert "hc.example" not in caplog.text and "secret-url" not in caplog.text


# ---- when run() pings ----------------------------------------------------------

def args(**kw):
    base = dict(config=str(ROOT / "config.toml"), list=False, plan=False, report=None, control=None, lock_wait=0.0, plan_json=False, prices_file=None,
                now=None, outdoor_temp=5.0, sun=0.0, mode="heat", season=None, home=None,
                assume_home=False, holiday=False, away=False, away_until=None, dry_run=True)
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def quiet_run(monkeypatch, tmp_path):
    monkeypatch.setenv("HEARTBEAT_URL", "https://hc.example/abc")
    monkeypatch.setattr(o, "setup_logging", lambda cfg: None)

    async def no_prices(cfg, f):
        return None

    async def fake_login(session):
        return []
    monkeypatch.setattr(o, "get_prices_or_none", no_prices)
    monkeypatch.setattr(o, "melcloud_devices", fake_login)
    # state.json for the breaker must not be the real one
    monkeypatch.setitem(CFG["resilience"], "state_file", str(tmp_path / "state.json"))


def run_main(monkeypatch, failures=0):
    async def fake_apply(*a, **k):
        return failures
    monkeypatch.setattr(o, "apply_to_units", fake_apply)
    return asyncio.run(o.run(args()))


def test_healthy_run_pings(monkeypatch, pings, quiet_run):
    assert run_main(monkeypatch) == 0
    assert len(pings) == 1


def test_run_with_unit_failures_does_not_ping(monkeypatch, pings, quiet_run):
    assert run_main(monkeypatch, failures=1) == 1
    assert pings == []


def test_login_failure_does_not_ping(monkeypatch, pings, quiet_run):
    async def boom(session):
        raise ValueError("bad credentials")
    monkeypatch.setattr(o, "melcloud_devices", boom)
    assert asyncio.run(o.run(args())) == 1
    assert pings == []


def test_paused_run_pings(monkeypatch, pings, quiet_run):
    o.PAUSE_FILE.write_text("")
    assert asyncio.run(o.run(args())) == 0
    assert len(pings) == 1


def test_open_breaker_does_not_ping(monkeypatch, pings, quiet_run):
    class Open:
        def open_until(self, now):
            return now

    monkeypatch.setattr(o, "make_breaker", lambda cfg: Open())
    assert asyncio.run(o.run(args())) == 0
    assert pings == []
