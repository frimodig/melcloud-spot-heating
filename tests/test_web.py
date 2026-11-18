import asyncio
from datetime import datetime
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

import ilp_optimizer as o
import web
from web_text import TEXT

FI = TEXT["fi"]

ROOT = Path(__file__).resolve().parent.parent
CFG = o.load_config(ROOT / "config.toml", ROOT / "tests" / "house.toml")
CFG = {**CFG, "web": {**CFG["web"], "language": "fi"}}   # these tests prove the Finnish text
TZ = o.ZoneInfo(CFG["timezone"])
ME = {web.USER_HEADER: "me@example.com"}
POST = {**ME, web.CSRF_HEADER: web.CSRF_VALUE}


def with_client(fn, tmp_path, users=None):
    """Run `fn(client, runs)` against the page with a fake controller trigger."""
    runs = []

    async def fake_trigger(app_dir):
        runs.append(app_dir)

    async def main():
        app = web.make_app(CFG, users or {"me@example.com": "Anna"}, tmp_path, fake_trigger)

        async def no_plan(a, day_offset=0):
            return None
        web.plan_data = no_plan  # the real one runs the controller
        async with TestClient(TestServer(app)) as client:
            return await fn(client, runs)
    saved = web.plan_data
    try:
        return asyncio.run(main())
    finally:
        web.plan_data = saved


def test_only_listed_tailscale_logins_get_in(tmp_path):
    async def go(c, runs):
        assert (await c.get("/api/status")).status == 403
        assert (await c.get("/api/status", headers={web.USER_HEADER: "other@example.com"})).status == 403
        assert (await c.get("/", headers={web.USER_HEADER: "ME@Example.com"})).status == 200  # case-insensitive
    with_client(go, tmp_path)


def test_parse_users():
    assert web.parse_users("A@x.com=Anna, b@x.com=Ben,junk,=x,y=") == {"a@x.com": "Anna", "b@x.com": "Ben"}


def test_status_shows_the_controls_and_units_in_the_right_mode(tmp_path):
    now = datetime.now(TZ)
    tel = o.monthly_path(tmp_path / CFG["telemetry_file"], now, TZ)
    tel.write_text(",".join(o.TELEMETRY_COLUMNS) + "\n" +
                   f"{now.isoformat()},Unit A,21.5,22.0,True,heat,0,1.0,True\n")
    o.set_nudge("warmer", TZ, CFG, now)

    async def go(c, runs):
        j = await (await c.get("/api/status", headers=ME)).json()
        assert j["mode"] == "heat" and j["user"] == "Anna" and j["nudge"]["shift"] == 1.0 and j["nudge"]["on"]
        assert j["units"][0]["name"] == "Unit A" and j["units"][0]["room"] == "21.5"
        o.set_season("cool")
        j = await (await c.get("/api/status", headers=ME)).json()
        assert j["mode"] == "cool" and j["season"] == "cool"   # the page turns blue
        o.set_control("pause", "hold", TZ, CFG)
        assert (await (await c.get("/api/status", headers=ME)).json())["mode"] == "paused"
    with_client(go, tmp_path)


def post(c, action, value="", confirm=False, headers=POST):
    return c.post("/api/action", json={"action": action, "value": value, "confirm": confirm}, headers=headers)


def test_a_post_needs_the_page_header_and_a_same_site_origin(tmp_path):
    async def go(c, runs):
        assert (await post(c, "nudge", "warmer", headers=ME)).status == 403            # no custom header
        bad = {**POST, "Origin": "https://evil.example"}
        assert (await post(c, "nudge", "warmer", headers=bad)).status == 403
        assert not o.NUDGE_FILE.exists() and runs == []
        ok = {**POST, "Origin": f"http://{c.server.host}:{c.server.port}"}
        assert (await post(c, "nudge", "warmer", headers=ok)).status == 200
    with_client(go, tmp_path)


def test_actions_write_the_files_and_start_a_run(tmp_path):
    async def go(c, runs):
        r = await post(c, "nudge", "cooler")
        assert r.status == 200 and o.read_nudge(o.NUDGE_FILE, TZ, datetime.now(TZ)).shift == -1.0
        assert (await post(c, "away", "on")).status == 200 and o.AWAY_FILE.exists()
        assert (await post(c, "away", "off")).status == 200 and not o.AWAY_FILE.exists()
        assert (await post(c, "home", "today")).status == 200 and o.HOME_FILE.exists()
        await asyncio.sleep(0)
        assert len(runs) == 4
    with_client(go, tmp_path)


def test_away_and_home_take_a_future_time_only(tmp_path):
    async def go(c, runs):
        assert (await post(c, "away", "2020-01-01T10:00")).status == 400
        assert (await post(c, "away", "tomorrow")).status == 400
        assert not o.AWAY_FILE.exists() and runs == []
        assert (await post(c, "away", "2099-01-01T10:00")).status == 200
        assert o.load_away(o.AWAY_FILE, TZ).until.year == 2099
    with_client(go, tmp_path)


def test_season_and_the_off_switch_need_confirmation(tmp_path):
    async def go(c, runs):
        r = await post(c, "season", "cool")
        assert r.status == 400 and not o.SEASON_FILE.exists()
        assert (await post(c, "season", "cool", confirm=True)).status == 200
        assert o.read_season(o.SEASON_FILE) == "cool"
        assert (await post(c, "pause", "on")).status == 400 and not o.PAUSE_FILE.exists()
        assert (await post(c, "pause", "on", confirm=True)).status == 200
        assert o.PAUSE_FILE.read_text().strip() == "settle"
        assert (await post(c, "pause", "off")).status == 200 and not o.PAUSE_FILE.exists()   # on needs no confirm
    with_client(go, tmp_path)


def test_unknown_actions_and_values_are_refused(tmp_path):
    async def go(c, runs):
        for action, value in [("holiday", "on"), ("nudge", "sideways"), ("season", "x"), ("pause", "hold"),
                              ("away", "x" * 80)]:
            assert (await post(c, action, value, confirm=True)).status == 400, (action, value)
        assert (await c.post("/api/action", data="nope", headers=POST)).status == 400
        assert runs == []
    with_client(go, tmp_path)


def test_the_page_is_finnish_and_has_the_state_frame_colours(tmp_path):
    async def go(c, runs):
        html = await (await c.get("/", headers=ME)).text()
        assert '<html lang="fi">' in html
        for text in ("Liian kylmä", "Liian kuuma", "Poissa", "Kotona", "Kausi", "Vaihdetaanko kausi", "Sammutetaanko ohjaus"):
            assert text in html
        assert 'body[data-mode="cool"]' in html and "--heat: #e2523b" in html and "--cool: #2f7fd6" in html
        assert "border: 7px solid var(--frame)" in html          # a state frame, not a full-colour background
        assert (await c.get("/manifest.json", headers=ME)).status == 200
    with_client(go, tmp_path)


# ---- the plan as data: Finnish reasons, price now and the pre-heat / saving blocks ------------

import json

import pytest


def day_prices(overrides, default=10.0):
    start, _ = o.local_day_bounds(datetime(2026, 11, 10, tzinfo=TZ).date(), TZ)
    from datetime import timedelta
    return [o.PriceInterval(start + timedelta(minutes=15 * k), start + timedelta(minutes=15 * (k + 1)),
                            overrides.get(k // 4, default)) for k in range(96)]


@pytest.mark.parametrize("reason,kind", [
    ("cheap 3.4c x0.71", "cheap"), ("expensive 14.0c", "expensive"), ("very expensive 31.0c", "expensive"),
    ("high price 22.1c", "expensive"), ("extreme price 42.8c", "extreme"), ("peak price 55.0c", "peak"),
    ("pre-heat before 40.0c spike", "preheat"), ("pre-heat before peak 55.0c", "peak_preheat"),
    ("pre-heat before return, cheap 3.0c", "return_preheat"), ("mid-priced hour", "normal"),
    ("away until Fri 14.11. 18:00", "away"), ("no prices", "none"),
    ("no shift (short data or small spread)", "none"), ("something new", "other")])
def test_reason_kinds(reason, kind):
    assert o.reason_kind(reason) == kind


def test_every_reason_the_planner_makes_has_a_known_kind():
    # a rich day: cheap night, spike, peak, extreme and expensive hours
    prices = day_prices({2: 3.0, 3: 3.0, 10: 14.0, 11: 22.0, 12: 24.0, 13: 40.0, 18: 55.0, 19: 25.0})
    rows = o.plan_rows(datetime(2026, 11, 10, 12, tzinfo=TZ), prices, CFG)
    assert len(rows) == 96 and {r["kind"] for r in rows} - {"other"} == {r["kind"] for r in rows}
    assert {"cheap", "normal", "peak", "extreme"} <= {r["kind"] for r in rows}
    assert rows[8]["price"] == 3.0 and rows[0]["price"] == 10.0


def test_plan_json_marks_the_quarter_of_now():
    prices = day_prices({})
    raw = json.loads(o.plan_json(datetime(2026, 11, 10, 13, 20, tzinfo=TZ), prices, CFG))
    assert raw["now_index"] == 13 * 4 + 1 and len(raw["rows"]) == 96
    assert raw["rows"][raw["now_index"]]["start"].startswith("2026-11-10T13:15")


def test_blocks_merge_runs_in_each_direction():
    prices = day_prices({2: 3.0, 3: 3.0, 4: 3.0, 10: 14.0, 11: 14.0, 15: 22.0, 16: 40.0})
    raw = json.loads(o.plan_json(datetime(2026, 11, 10, 12, tzinfo=TZ), prices, CFG))
    s = web.summarise_plan(raw)
    kinds = [(b["kind"], b["from"], b["to"]) for b in s["blocks"]]
    # 02:00-03:00 is cheap (boosted) but before the 05:00 step's two-hour lead: a boost, not a pre-heat
    assert not any(k[0] == "preheat" and k[1] < "03:00" for k in kinds)
    assert any(k[0] == "preheat" and "03:00" <= k[1] < "05:00" for k in kinds)
    assert any(k[0] == "save" and k[1] <= "16:00" < k[2] for k in kinds)
    assert s["now"]["price"] == 10.0 and s["now"]["reason_log"]
    assert len(s["times"]) == len(s["price"]) == len(s["target"]) == 96
    assert s["avg"] == round(sum(s["price"]) / 96, 2)


def test_reasons_come_out_in_finnish_once_with_notes():
    fi = web.reason_text(FI, "cheap 3.4c x0.71, comfort hours: cut capped at 0, clamped", "cheap")
    assert fi == {"text": "Lämmitetään etukäteen", "notes": ["mukavuusaika rajaa laskua"]}
    assert web.reason_text(FI, "mid-priced hour", "normal")["notes"] == []
    assert "täysillä" in web.reason_text(FI, "peak price 55.0c", "peak")["text"]
    for kind in ("cheap", "normal", "expensive", "extreme", "peak", "spike", "preheat", "peak_preheat",
                 "return_preheat", "away", "none", "other"):
        assert web.reason_text(FI, "x", kind)["text"]


def test_the_status_carries_the_plan_and_no_per_unit_english(tmp_path):
    async def go(c, runs):
        async def plan(a, day_offset=0):
            return {"times": ["00:00"], "price": [4.2], "target": [19.5], "now_index": 0,
                    "now": {"price": 4.2, "target": 19.5, "reason": {"text": "x", "notes": []}}, "blocks": []}
        web.plan_data = plan
        j = await (await c.get("/api/status", headers=ME)).json()
        assert j["plan"]["now"]["price"] == 4.2
        assert all("reason" not in u for u in j["units"])
    with_client(go, tmp_path)


def test_bands_follow_the_target_line_not_the_price_shift():
    rows = [{"start": f"2026-11-10T{h:02d}:{m:02d}+02:00", "base": 19.0, "final": 19.0, "kind": "normal"}
            for h in range(3) for m in (0, 15, 30, 45)]
    rows[1].update(final=19.5, kind="comfort_preheat")   # a single pre-heated quarter hour is a band
    rows[4].update(final=20.0, kind="cheap")              # a cheap boost raises the line but is no pre-heat
    rows[8]["final"] = 18.0; rows[9]["final"] = 18.5     # two cooler quarters in a row are one band
    rows[10]["final"] = 19.25                            # under half a degree: no band
    assert web.blocks_of(rows, "preheat", +1) == [{"kind": "preheat", "from": "00:15", "to": "00:30"}]
    assert web.blocks_of(rows, "save", -1) == [{"kind": "save", "from": "02:00", "to": "02:30"}]


def test_the_room_line_averages_the_units_per_quarter(tmp_path):
    cfg = {"timezone": "Europe/Helsinki", "telemetry_file": "telemetry.csv"}
    now = datetime(2026, 11, 10, 13, 20, tzinfo=TZ)
    (tmp_path / "telemetry-2026-11.csv").write_text(
        "time_local,unit,room_temp\n"
        "2026-11-10T13:00:05+02:00,A,21.0\n2026-11-10T13:00:06+02:00,B,22.0\n"
        "2026-11-10T13:15:04+02:00,A,21.5\n2026-11-09T13:30:00+02:00,A,10.0\n", encoding="utf-8")
    assert web.room_series(cfg, now, tmp_path, ["12:45", "13:00", "13:15", "13:30"]) == [None, 21.5, 21.5, None]
    assert web.room_series(cfg, now, tmp_path / "nowhere", ["13:00"]) == [None]


def test_normal_now_needs_confirmation_and_shows_in_the_status(tmp_path):
    async def go(c, runs):
        assert (await post(c, "normal", "on")).status == 400 and not o.NORMAL_FILE.exists()
        r = await post(c, "normal", "on", confirm=True)
        assert r.status == 200 and (await r.json())["message"] == "Laitteet asetetaan 21 °C:een." and o.NORMAL_FILE.exists()
        j = await (await c.get("/api/status", headers=ME)).json()
        assert j["normal"]["on"] is True and j["normal"]["temp"] == 21.0 and j["normal"]["until"]
        assert (await post(c, "normal", "off")).status == 200 and not o.NORMAL_FILE.exists()   # cancel: no confirm
        assert (await post(c, "normal", "maybe", confirm=True)).status == 400
        await asyncio.sleep(0)
        assert len(runs) == 2
    with_client(go, tmp_path)


def test_the_page_has_the_normal_now_button_in_finnish(tmp_path):
    async def go(c, runs):
        html = await (await c.get("/", headers=ME)).text()
        assert "Normaali lämpö heti" in html and "vuodenajasta riippumatta" in html
    with_client(go, tmp_path)


def test_tomorrows_plan_is_asked_for_separately_and_summarised_with_its_date(tmp_path):
    seen = []

    class FakeProc:
        def __init__(self, cmd):
            self.cmd = cmd

        async def communicate(self):
            # a plan of tomorrow without prices: the feed has nothing for it before about 14:00
            rows = [{"start": f"2026-11-13T{h:02d}:{m:02d}+02:00", "price": None, "base": 19.0, "offset": 0.0,
                     "final": 19.0, "reason": "no prices", "kind": "none"} for h in range(24) for m in (0, 15, 30, 45)]
            return json.dumps({"mode": "heat", "now_index": None, "rows": rows}).encode(), b""

    async def fake_exec(*cmd, **kw):
        seen.append(cmd)
        return FakeProc(cmd)

    async def go(c, runs):
        web.plan_data = saved_plan
        orig = web.asyncio.create_subprocess_exec
        web.asyncio.create_subprocess_exec = fake_exec
        try:
            app = c.server.app
            today = await web.plan_data(app, 0)
            tomorrow = await web.plan_data(app, 1)
        finally:
            web.asyncio.create_subprocess_exec = orig
        assert "--now" not in seen[0] and "--now" in seen[1]
        assert seen[1][seen[1].index("--now") + 1].endswith("T12:00")
        assert tomorrow["date"] == "2026-11-13" and tomorrow["has_prices"] is False
        assert tomorrow["now"] is None and today["has_prices"] is False
    saved_plan = web.plan_data
    with_client(go, tmp_path)


def test_the_page_has_the_day_tabs_a_clear_now_line_and_the_price_pill(tmp_path):
    async def go(c, runs):
        html = await (await c.get("/", headers=ME)).text()
        for text in ("Tänään", "Huomenna", 'class: "c-now"', "c-nowtag", '"nyt"'):
            assert text in html
    with_client(go, tmp_path)


def test_the_status_has_tomorrows_plan_and_the_pill_text(tmp_path):
    async def go(c, runs):
        async def plan(a, day_offset=0):
            return {"date": "2026-11-1%d" % (2 + day_offset), "has_prices": day_offset == 0, "times": ["00:00"],
                    "price": [4.2], "target": [19.5], "now_index": 0, "blocks": [],
                    "now": {"price": 4.2, "target": 19.5, "kind": "cheap", "pill": "Halpa",
                            "reason": {"text": "x", "notes": []}}}
        web.plan_data = plan
        j = await (await c.get("/api/status", headers=ME)).json()
        assert j["plan"]["date"] == "2026-11-12" and j["plan_tomorrow"]["date"] == "2026-11-13"
    with_client(go, tmp_path)


def test_a_day_with_only_a_few_prices_is_not_shown_as_priced():
    rows = [{"start": f"2026-11-13T{h:02d}:{m:02d}+02:00", "price": 5.0 if h < 2 else None, "base": 19.0,
             "offset": 0.0, "final": 19.0, "reason": "no prices", "kind": "none"}
            for h in range(24) for m in (0, 15, 30, 45)]
    assert web.summarise_plan({"mode": "heat", "now_index": None, "rows": rows})["has_prices"] is False
    full = [{**r, "price": 5.0} for r in rows]
    assert web.summarise_plan({"mode": "heat", "now_index": None, "rows": full})["has_prices"] is True
    most = [{**r, "price": 5.0 if i < 82 else None} for i, r in enumerate(rows)]           # 20.5 h
    assert web.summarise_plan({"mode": "heat", "now_index": None, "rows": most})["has_prices"] is True


def test_tomorrow_is_offered_only_when_its_prices_are_out_and_the_chart_comes_last(tmp_path):
    async def go(c, runs):
        html = await (await c.get("/", headers=ME)).text()
        assert 'id="seg" hidden' in html and "$(\"seg\").hidden = !tomorrowOk" in html
        assert "state.plan_tomorrow.has_prices" in html and 'if (!tomorrowOk) day = "today"' in html
        # the chart card sits below the controls
        assert html.index('id="cold"') < html.index('id="chart"') and html.index('id="msg"') < html.index('id="chart"')
        # nothing may be wider than its column: no bare 1fr grid tracks
        import re
        body = html[html.index("<style>"):html.index("</style>")]
        assert not re.search(r"grid-template-columns:\s*1fr", body)
        assert "overflow-x: hidden" in body
    with_client(go, tmp_path)


def test_the_cheapest_three_hours_still_to_come():
    rows = [{"start": f"2026-11-10T{h:02d}:{m:02d}+02:00", "price": 5.0} for h in range(24) for m in (0, 15, 30, 45)]
    for k in range(4, 16):
        rows[k]["price"] = 1.0                           # 01:00-04:00 is the cheapest
    for k in range(80, 92):
        rows[k]["price"] = 2.0                           # 20:00-23:00 is the next best
    assert web.cheapest_stretch(rows) == {"from": "01:00", "to": "04:00", "avg": 1.0}
    assert web.cheapest_stretch(rows, start=13 * 4) == {"from": "20:00", "to": "23:00", "avg": 2.0}
    assert web.cheapest_stretch(rows, start=22 * 4) is None   # under three hours left
    rows[85]["price"] = None
    assert web.cheapest_stretch(rows, start=13 * 4)["from"] != "20:00"   # a gap breaks a stretch


def test_the_warm_up_notes_say_what_the_heat_is_for():
    ramp = "high price 22.0c, warm-up for comfort 18:00: step 2/4 to 21.5"
    assert web.reason_text(FI, ramp, "expensive", still_cut=True)["notes"] == ["mukavuusaika alkaa pian, laskua rajataan"]
    assert web.reason_text(FI, ramp, "expensive")["notes"] == ["lämpö nousee ennen mukavuusaikaa"]
    home = "nobody home: 17 at 8.0c, warm-up for home 15:00: step 2/3 to 20"
    assert web.reason_text(FI, home, "nobody_home")["notes"] == ["lämpö nousee ennen kotiintuloa"]


def test_the_login_header_is_a_setting(tmp_path):
    global CFG
    saved, CFG = CFG, {**CFG, "web": {**CFG["web"], "user_header": "X-Forwarded-User"}}
    try:
        async def go(c, runs):
            assert (await c.get("/api/status", headers=ME)).status == 403          # Tailscale's header: not read
            assert (await c.get("/", headers={"X-Forwarded-User": "me@example.com"})).status == 200
        with_client(go, tmp_path)
    finally:
        CFG = saved
