"""The page's script runs: render() and the chart, on a real plan, in both languages (with a stand-in
DOM in node). A script error shows on the phone only as "no connection" and a page without a chart."""
import asyncio
import json
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

import ilp_optimizer as o
import web

ROOT = Path(__file__).resolve().parent.parent
CFG = o.load_config(ROOT / "config.toml", ROOT / "tests" / "house.toml")
TZ = o.ZoneInfo(CFG["timezone"])
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=TZ)


def day_prices(day):
    start, _ = o.local_day_bounds(day, TZ)
    return [o.PriceInterval(start + timedelta(minutes=15 * k), start + timedelta(minutes=15 * (k + 1)),
                            5.0 + 20.0 * (32 <= k < 52)) for k in range(96)]


def page_and_status(cfg, tmp_path):
    prices = day_prices(NOW.date()) + day_prices(NOW.date() + timedelta(days=1))

    async def plan(app, day_offset=0):
        now = NOW + timedelta(days=day_offset)
        return web.summarise_plan(json.loads(o.plan_json(now, prices, cfg, 5.0, o.Away())), cfg)

    async def trigger(app_dir):
        return 0

    async def main():
        app = web.make_app(cfg, {"me@example.com": "Anna"}, tmp_path, trigger)
        async with TestClient(TestServer(app)) as c:
            h = {cfg["web"]["user_header"]: "me@example.com"}
            return await (await c.get("/", headers=h)).text(), await (await c.get("/api/status", headers=h)).text()
    saved, web.plan_data = web.plan_data, plan
    try:
        return asyncio.run(main())
    finally:
        web.plan_data = saved


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("language", ["en", "fi"])
def test_the_page_renders_with_its_chart(tmp_path, language):
    cfg = {**CFG, "web": {**CFG["web"], "language": language}}
    page, status = page_and_status(cfg, tmp_path)
    assert json.loads(status)["plan"] and json.loads(status)["plan_tomorrow"]
    (tmp_path / "page.html").write_text(page)
    (tmp_path / "status.json").write_text(status)
    out = subprocess.run(["node", str(ROOT / "tests" / "page_render.js"), str(tmp_path / "page.html"),
                          str(tmp_path / "status.json")], capture_output=True, text=True, timeout=60).stdout
    assert out.strip() == "render OK, tomorrow OK", out


def test_the_wide_layout_places_elements_the_page_has():
    import re
    page = (ROOT / "web_page.html").read_text()
    wide = page[page.index("@media (min-width: 900px)"):]
    wide = wide[:wide.index("\n  }\n")]
    placed = set(re.findall(r"#(\w+) \{ grid-area: (\w+)", wide))
    assert {name for name, _ in placed} == {"priceCard", "units", "ovCard", "controls", "msg", "chartCard"}
    areas = set(" ".join(re.findall(r'"([a-z ]+)"', wide)).split())
    assert {area for _, area in placed} | {"head"} == areas            # every area has an element, and back
    for name, _ in placed:
        assert f'id="{name}"' in page
