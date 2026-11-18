import pytest

import ilp_optimizer as o


@pytest.fixture(autouse=True)
def no_backoff_sleep(monkeypatch):
    """Retry backoff must not slow the tests down."""
    async def instant(_delay):
        return None
    monkeypatch.setattr(o, "SLEEP", instant)


@pytest.fixture(autouse=True)
def isolated_control_files(monkeypatch, tmp_path_factory):
    """Tests must never read or write the real AWAY, SEASON, HOLIDAY or PAUSE files."""
    d = tmp_path_factory.mktemp("app")
    monkeypatch.setattr(o, "APP_DIR", d)
    for name in ("PAUSE", "AWAY", "SEASON", "HOLIDAY", "HOME", "NUDGE", "NORMAL", "LOCK"):
        monkeypatch.setattr(o, f"{name}_FILE", d / name)


@pytest.fixture(autouse=True)
def no_network_sun(monkeypatch):
    """The solar forecast fetch must never reach the real API from a test."""
    async def empty(cfg):
        return o.SunForecast({})
    monkeypatch.setattr(o, "fetch_sun", empty)


@pytest.fixture(autouse=True)
def no_network_outdoor(monkeypatch):
    """The hourly outdoor forecast fetch must never reach the real API from a test: failing it makes
    the daily-mean path (or an override) take over, as it does when the service is down."""
    async def down(cfg):
        raise OSError("no network in tests")
    monkeypatch.setattr(o, "fetch_outdoor_forecast", down)


LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """No test reaches a real service (price feeds, weather, MELCloud, the heartbeat): any HTTP request
    to anything but the tests' own local server is refused, and fails the test when it ends, even if the
    code under test catches the error and falls back. Fake the fetch instead."""
    import aiohttp
    from yarl import URL

    real, attempts = aiohttp.ClientSession._request, []

    async def local_only(self, method, url, *args, **kwargs):
        host = URL(str(url)).host
        if host not in LOCAL_HOSTS:
            attempts.append(f"{method} {host}")
            raise AssertionError(f"a test tried to reach the network: {method} {host}")
        return await real(self, method, url, *args, **kwargs)
    monkeypatch.setattr(aiohttp.ClientSession, "_request", local_only)
    yield
    assert not attempts, f"a test tried to reach the network: {attempts}"
