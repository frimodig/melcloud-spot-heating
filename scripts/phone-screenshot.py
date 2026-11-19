"""Mock screenshot of the phone page for the docs, captured by headless Firefox at phone width. All of it
is made up, and none of this install's own data is read (control files, history, config.local.toml):
the test house (tests/house.toml), two days of prices from sample_prices.json, an invented November
forecast, history and room readings for the day so far in which the units followed the plan, and a
"Too cold" press 35 minutes ago.

    .venv/bin/python scripts/phone-screenshot.py [docs/phone-page.png]

Set FIREFOX to the browser binary if it is not on PATH or in /Applications."""
import asyncio
import csv
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ilp_optimizer as o  # noqa: E402
import web  # noqa: E402

PHONE_WIDTH = 390   # CSS pixels, a common phone; Firefox captures the whole page height
# The state frame is fixed to the window: pinned to the page instead, it frames the whole capture.
WHOLE_PAGE = "<style>html { scrollbar-width: none; } body { position: relative; } body::before { position: absolute; }</style>\n"
UNITS = {"Unit A": "Downstairs", "Unit B": "Upstairs"}   # the test house's units, renamed for the picture
USER = "Anna"
TZ = o.ZoneInfo("Europe/Helsinki")
NOW = datetime(2026, 11, 9, 16, 5, tzinfo=TZ)   # Monday of the sample prices, as the evening peak nears
PRESSED = NOW - timedelta(minutes=35)            # someone pressed "Too cold"


class FrozenTime(datetime):
    """The page's server code asks for the time itself: it gets NOW."""
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


def house(tmp: Path) -> dict:
    local = tmp / "house.toml"
    text = (ROOT / "tests" / "house.toml").read_text(encoding="utf-8")
    for old, new in UNITS.items():
        text = text.replace(old, new)
    local.write_text(text, encoding="utf-8")
    return o.load_config(ROOT / "config.toml", local)


def forecast() -> "o.OutdoorForecast":
    """Two November days: -3 C before dawn, +2 C at 14, clear nights, cloud by day, snow in the evening."""
    hours, start = {}, datetime(2026, 11, 9, tzinfo=TZ)
    for h in range(48):
        t = start + timedelta(hours=h)
        temp = round(-0.5 + 2.5 * math.sin((t.hour - 8) / 24 * 2 * math.pi), 1)
        code = 0 if t.hour < 7 or t.hour >= 23 else 2 if t.hour < 11 else 3 if t.hour < 17 else 71
        hours[t.astimezone(timezone.utc)] = (temp, 85.0, 3.0, code, int(8 <= t.hour < 16))
    return o.OutdoorForecast(hours)


def plan(cfg: dict, now: datetime, prices, outdoor) -> dict:
    return json.loads(o.plan_json(now, prices, cfg, outdoor, nudge=o.read_nudge(o.NUDGE_FILE, TZ, now)))


def morning(cfg: dict, app_dir: Path, rows: list, now_index: int) -> None:
    """History rows (the targets set) and telemetry (the rooms) for every quarter so far, each room
    easing a third of the way towards its target per quarter."""
    hist = o.monthly_path(app_dir / cfg["history_file"], NOW, TZ)
    tel = o.monthly_path(app_dir / cfg["telemetry_file"], NOW, TZ)
    rooms = {name: 20.6 + 0.3 * k for k, name in enumerate(UNITS.values())}
    with open(hist, "w", newline="", encoding="utf-8") as h, open(tel, "w", newline="", encoding="utf-8") as t:
        hw = csv.DictWriter(h, fieldnames=o.HISTORY_COLUMNS)
        tw = csv.DictWriter(t, fieldnames=o.TELEMETRY_COLUMNS)
        hw.writeheader()
        tw.writeheader()
        for r in rows[:now_index + 1]:
            stamp = datetime.fromisoformat(r["start"]).isoformat()
            for name in UNITS.values():
                target = r["final"] + cfg["units"].get("offsets", {}).get(name, 0.0)
                if r is not rows[now_index]:
                    hw.writerow({"time_local": stamp, "unit": name, "action": "set", "room_temp": round(rooms[name], 1),
                                 "old_target": target, "new_target": target, "base": r["base"], "offset": r["offset"],
                                 "reason": r["reason"], "price": r["price"], "kind": r["kind"]})
                tw.writerow({"time_local": stamp, "unit": name, "room_temp": round(rooms[name], 1),
                             "target_temp": target, "power": True, "mode": "heat", "fan_actual": 0,
                             "energy_kwh": 0.2, "has_meter": True, "outdoor_c": r["outdoor"]})
                rooms[name] += (target - rooms[name]) / 3


def mock_page(tmp: Path) -> str:
    """The page with its status built in, so it renders from a file with no server."""
    app_dir = tmp / "app"
    app_dir.mkdir()
    for name in ("PAUSE", "AWAY", "SEASON", "HOLIDAY", "HOME", "NUDGE", "NORMAL", "LOCK"):
        setattr(o, f"{name}_FILE", app_dir / name)   # never the real control files of this checkout
    web.datetime = FrozenTime
    cfg = house(tmp)
    prices = o.parse_prices(json.loads((ROOT / "sample_prices.json").read_text(encoding="utf-8")))
    outdoor = forecast()
    o.set_nudge("warmer", TZ, cfg, PRESSED)
    today = plan(cfg, NOW, prices, outdoor)
    morning(cfg, app_dir, today["rows"], today["now_index"])
    today["rows"] = web.actual_past(cfg, NOW, app_dir, today["rows"], today["now_index"])
    tomorrow = plan(cfg, (NOW + timedelta(days=1)).replace(hour=12, minute=0), prices, outdoor)
    plans = {0: web.summarise_plan(today, cfg), 1: web.summarise_plan(tomorrow, cfg)}

    async def plan_data(app, day_offset=0):
        return plans[day_offset]
    web.plan_data = plan_data

    async def status():
        app = web.make_app(cfg, {"me@example.com": USER}, app_dir)
        async with TestClient(TestServer(app)) as c:
            r = await c.get("/api/status", headers={cfg["web"]["user_header"]: "me@example.com"})
            r.raise_for_status()
            return await r.text()
    page = web.render_page(cfg)
    # the status instead of the server, and the browser's clock at NOW (the page counts the time left)
    stub = (f"<script>const MOCK_STATUS = {asyncio.run(status())}; window.fetch = async () => ({{ok: true, json: async () => MOCK_STATUS}});\n"
            f"const NOW_MS = {int(NOW.timestamp() * 1000)}, RealDate = Date;\n"
            "Date = class extends RealDate { constructor(...a) { super(...(a.length ? a : [NOW_MS])); } static now() { return NOW_MS; } };</script>\n")
    dark = "@media (prefers-color-scheme: dark)"
    if dark not in page:
        sys.exit(f"the page has no '{dark}' rule to switch off: update the light-theme pin")
    page = page.replace(dark, "@media not all", 1)   # light, whatever the machine's theme
    return page.replace("</head>", WHOLE_PAGE + "</head>", 1).replace("<script>", stub + "<script>", 1)


def firefox() -> str:
    for candidate in (os.environ.get("FIREFOX"), shutil.which("firefox"),
                      "/Applications/Firefox.app/Contents/MacOS/firefox"):
        if candidate and Path(candidate).exists():
            return candidate
    sys.exit("Firefox not found: set FIREFOX to its binary")


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "docs" / "phone-page.png").resolve()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "profile").mkdir()
        html = tmp / "phone-page.html"
        html.write_text(mock_page(tmp), encoding="utf-8")
        subprocess.run([firefox(), "--headless", "--no-remote", "--profile", str(tmp / "profile"),
                        "--screenshot", str(out), f"--window-size={PHONE_WIDTH}", html.as_uri()],
                       check=True, timeout=120, env={**os.environ, "TZ": str(TZ)})   # the page shows local times
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
