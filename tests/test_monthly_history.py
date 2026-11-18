import asyncio
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import ilp_optimizer as o

ROOT = Path(__file__).resolve().parent.parent
TZ = o.ZoneInfo("Europe/Helsinki")


def test_path_names_the_local_month():
    base = Path("/x/history.csv")
    assert o.monthly_path(base, datetime(2026, 11, 11, 15, 0, tzinfo=TZ), TZ) == Path("/x/history-2026-11.csv")


def test_month_changes_at_local_midnight_not_utc():
    base = Path("history.csv")
    # 23:30 UTC on 30 Nov is already 1 Dec in Helsinki (UTC+2).
    late = datetime(2026, 11, 30, 23, 30, tzinfo=timezone.utc)
    assert o.monthly_path(base, late, TZ).name == "history-2026-12.csv"
    assert o.monthly_path(base, late - timedelta(hours=3), TZ).name == "history-2026-11.csv"


def test_run_writes_to_the_monthly_file(monkeypatch):
    seen = {}

    async def fake_apply(devices, now, prices, cfg, dry_run, outdoor, history, *a, **k):
        seen["history"] = history
        return 0

    async def no_prices(cfg, f):
        return None

    async def no_devices(session):
        return []
    monkeypatch.setattr(o, "apply_to_units", fake_apply)
    monkeypatch.setattr(o, "get_prices_or_none", no_prices)
    monkeypatch.setattr(o, "melcloud_devices", no_devices)
    monkeypatch.setattr(o, "setup_logging", lambda cfg: None)
    monkeypatch.delenv("HEARTBEAT_URL", raising=False)
    args = argparse.Namespace(config=str(ROOT / "config.toml"), list=False, plan=False, report=None, control=None, lock_wait=0.0, plan_json=False,
                              prices_file=None, now="2026-12-01T00:05", outdoor_temp=5.0, sun=0.0,
                              mode="heat", season=None, home=None, assume_home=False,
                              holiday=False, away=False, away_until=None, dry_run=True)
    assert asyncio.run(o.run(args)) == 0
    assert seen["history"] == o.APP_DIR / "history-2026-12.csv"
