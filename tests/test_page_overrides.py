"""The page after a button press: the run's outcome, the plan redrawn at once, the past as it ran,
the override card's estimate, and the cheapest hours at home today against tomorrow."""
import asyncio
import json
from datetime import date, datetime, timedelta
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

import ilp_optimizer as o
import web

ROOT = Path(__file__).resolve().parent.parent
CFG = o.load_config(ROOT / "config.toml", ROOT / "tests" / "house.toml")
CFG = {**CFG, "web": {**CFG["web"], "language": "fi"}}   # these tests prove the Finnish text
TZ = o.ZoneInfo(CFG["timezone"])
ME = {web.USER_HEADER: "me@example.com"}
POST = {**ME, web.CSRF_HEADER: web.CSRF_VALUE}
TUE = date(2026, 11, 10)


def rows_of(day="2026-11-10", price=5.0, final=20.0, **extra):
    return [{"start": f"{day}T{h:02d}:{m:02d}+02:00", "price": price, "base": 20.0, "offset": 0.0,
             "final": final, "reason": "mid-priced hour", "kind": "normal", **extra}
            for h in range(24) for m in (0, 15, 30, 45)]


def client(fn, tmp_path, trigger):
    async def main():
        app = web.make_app(CFG, {"me@example.com": "Anna"}, tmp_path, trigger)
        async with TestClient(TestServer(app)) as c:
            return await fn(c, app)
    saved = web.plan_data

    async def no_plan(a, day_offset=0):
        return None
    web.plan_data = no_plan
    try:
        return asyncio.run(main())
    finally:
        web.plan_data = saved


# ---- the run a press starts ---------------------------------------------------------------------

def run_outcome(tmp_path, code):
    gate = asyncio.Event()

    async def trigger(app_dir):
        await gate.wait()
        return code

    async def go(c, app):
        app["plan_cache"][0] = (0.0, {"stale": True})
        await c.post("/api/action", json={"action": "nudge", "value": "warmer"}, headers=POST)
        assert app["plan_cache"] == {}                                  # the new plan is drawn at once
        running = (await (await c.get("/api/status", headers=ME)).json())["run"]
        gate.set()
        for _ in range(20):
            await asyncio.sleep(0)
        done = (await (await c.get("/api/status", headers=ME)).json())["run"]
        return running, done
    return client(go, tmp_path, trigger)


def test_the_status_says_the_run_is_going_and_then_that_the_units_were_updated(tmp_path):
    running, done = run_outcome(tmp_path, 0)
    assert running == {"state": "running"} and done["state"] == "done" and done["at"]


def test_a_run_that_could_not_get_the_lock_is_reported_as_skipped(tmp_path):
    assert run_outcome(tmp_path, o.RUN_SKIPPED)[1]["state"] == "skipped"


def test_a_failed_run_is_reported(tmp_path):
    assert run_outcome(tmp_path, 1)[1]["state"] == "failed"


def test_a_page_run_that_gives_up_on_the_lock_exits_with_its_own_code(monkeypatch):
    monkeypatch.setattr(o, "acquire_run_lock", lambda path, wait: None)
    monkeypatch.setattr(o, "setup_logging", lambda cfg: None)
    args = o.argparse.Namespace(config=str(ROOT / "config.toml"), list=False, plan=False, plan_json=False,
                                report=None, control=None, season=None, home=None, lock_wait=60.0)
    assert asyncio.run(o.run(args)) == o.RUN_SKIPPED
    args.lock_wait = 0.0
    assert asyncio.run(o.run(args)) == 0                                # cron skips quietly, as before


def test_the_unit_card_shows_the_target_the_run_wrote_not_the_one_it_read(tmp_path):
    now = datetime.now(TZ)
    o.monthly_path(tmp_path / CFG["telemetry_file"], now, TZ).write_text(
        ",".join(o.TELEMETRY_COLUMNS) + "\n" + f"{now.isoformat()},Unit B,21.0,21.0,True,heat,0,1.0,True\n")
    hist = o.monthly_path(tmp_path / CFG["history_file"], now, TZ)
    hist.write_text(",".join(o.HISTORY_COLUMNS) + "\n" +
                    f"{(now + timedelta(seconds=5)).isoformat()},Unit B,set,21.0,21.0,22.0,22,0,5,x,5,normal\n")
    assert web.build_status(CFG, now, tmp_path)["units"][0]["target"] == "22.0"


# ---- the past as it ran -------------------------------------------------------------------------

def test_quarters_before_now_show_what_the_controller_set():
    hist = (",".join(o.HISTORY_COLUMNS) + "\n"
            "2026-11-10T08:00:03+02:00,Unit A,set,20,19,19.0,19,0,5,mid-priced hour,5,normal\n"
            "2026-11-10T08:00:04+02:00,Unit B,set,20,19,19.0,19,0,5,mid-priced hour,5,normal\n"
            "2026-11-10T08:15:03+02:00,Unit B,keep,20,19,17.0,17,0,5,away,5,away\n")
    rows = rows_of(final=17.0, reason="away", kind="away")              # away set later: the plan says 17
    return_rows(hist, rows)


def return_rows(hist, rows, tmp=None):
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        Path(d, "history-2026-11.csv").write_text(hist)
        now = datetime(2026, 11, 10, 9, 0, tzinfo=TZ)
        out = web.actual_past(CFG, now, Path(d), rows, 36)
    assert out[32]["final"] == 19.0 and out[32]["kind"] == "normal"     # 08:00: what ran
    assert out[33]["final"] == 17.0 and out[33]["kind"] == "away"
    assert out[34]["final"] is None                                     # no run: a gap, not the plan
    assert out[36]["final"] == 17.0 and out[40] == rows[40]             # now and later: the plan
    summary = web.summarise_plan({"rows": out, "now_index": 36})
    assert summary["target"][34] is None and summary["blocks"] is not None


# ---- the estimate on the override card -------------------------------------------------------------

def test_the_estimate_counts_the_degrees_the_override_adds_at_the_price_until_it_ends():
    rows = rows_of(price=10.0, final=21.0, without={"nudge": 20.0})
    now = datetime(2026, 11, 10, 19, 5, tzinfo=TZ)
    e = web.estimate(rows, "nudge", now, "2026-11-10T22:00+02:00", CFG, 2)
    # 12 quarters x 1 C x 2.0 Wh x 2 units x 10 c/kWh = 0.48 c
    assert e == {"cents": 0.5, "until_end": True}


def test_when_cooling_a_warmer_target_is_a_saving_and_a_cooler_one_a_cost():
    now = datetime(2026, 7, 14, 12, 0, tzinfo=TZ)
    warmer = rows_of(day="2026-07-14", price=10.0, final=24.0, without={"nudge": 23.0})
    cooler = rows_of(day="2026-07-14", price=10.0, final=22.0, without={"nudge": 23.0})
    assert web.estimate(warmer, "nudge", now, "2026-07-14T15:00+03:00", CFG, 2, "cool")["cents"] < 0
    assert web.estimate(cooler, "nudge", now, "2026-07-14T15:00+03:00", CFG, 2, "cool")["cents"] > 0


def test_away_shows_a_saving_and_says_when_the_prices_run_out_before_the_return():
    rows = rows_of(price=20.0, final=17.0, without={"away": 21.0})
    now = datetime(2026, 11, 10, 12, 0, tzinfo=TZ)
    e = web.estimate(rows, "away", now, "2026-11-12T18:00+02:00", CFG, 2)
    assert e["cents"] == round(-4 * 2.0 * 2 * 20.0 / 1000 * 48, 1) and e["until_end"] is False
    assert web.estimate(rows_of(price=None, without={"away": 21.0}), "away", now, None, CFG, 2) is None


def test_the_plan_carries_the_targets_without_each_override():
    start, _ = o.local_day_bounds(TUE, TZ)
    p = [o.PriceInterval(start + timedelta(minutes=15 * k), start + timedelta(minutes=15 * (k + 1)), 8.0)
         for k in range(96)]
    now = datetime(2026, 11, 10, 19, 0, tzinfo=TZ)
    nudge = o.Nudge(shift=1.0, until=now + timedelta(hours=3))
    raw = json.loads(o.plan_json(now, p, CFG, 5.0, o.Away(), nudge=nudge))
    row = raw["rows"][19 * 4]
    assert row["final"] == 18.0 and row["without"] == {"nudge": 17.0, "away": 23.0}
    assert raw["rows"][18 * 4]["without"] == {}                          # before now: never priced
    assert raw["rows"][22 * 4]["without"] == {"away": 20.5}              # the nudge is over by then


# ---- the cheapest hours at home -----------------------------------------------------------------------

def test_the_cheapest_three_hours_are_looked_for_in_the_usual_hours_at_home():
    rows = rows_of()
    for k in range(4, 16):
        rows[k]["price"] = 1.0                                          # 01-04: cheapest, but asleep
    for k in range(80, 92):
        rows[k]["price"] = 2.0
    weekday = web.summarise_plan({"rows": rows, "now_index": 0}, CFG)["cheapest"]
    assert weekday == {"from": "20:00", "to": "23:00", "avg": 2.0}
    assert web.home_hours(CFG, "2026-11-10") == ("17:00", "24:00")
    assert web.home_hours(CFG, "2026-11-14") == ("07:00", "24:00")
    assert web.cheapest_stretch(rows, 12, 0, ("17:00", "24:00"))["to"] == "23:00"
    assert web.cheapest_stretch(rows, 12, 22 * 4, ("17:00", "24:00")) is None   # under 3 h left at home


def test_the_page_has_the_override_card_the_chip_and_the_comparison(tmp_path):
    async def trigger(app_dir):
        return 0

    async def go(c, app):
        html = await (await c.get("/", headers=ME)).text()
        assert html.index('id="units"') < html.index('id="ovCard"') < html.index('id="cold"')
        for text in ("Käsisäätö", '"+1 h"', "Peru nyt", "Normaali ohjelma jatkuu klo ", "Huomenna: ",
                     "kotona-aikaan", "Arvioitu säästö", "Laitteet päivitetty."):
            assert text in html, text
    client(go, tmp_path, trigger)


def test_a_failed_plan_is_tried_again_at_the_next_poll(tmp_path):
    calls = []

    class Proc:
        async def communicate(self):
            return json.dumps({"mode": "heat", "now_index": None, "rows": rows_of()}).encode(), b""

    async def flaky_exec(*cmd, **kw):
        calls.append(cmd)
        if len(calls) == 1:
            raise OSError("price feed down")
        return Proc()

    async def trigger(app_dir):
        return 0

    async def go(c, app):
        web.plan_data = saved
        orig, retry = web.asyncio.create_subprocess_exec, web.PLAN_RETRY_SECONDS
        web.asyncio.create_subprocess_exec = flaky_exec
        try:
            assert await web.plan_data(app, 1) is None
            assert await web.plan_data(app, 1) is None and len(calls) == 1     # a moment later: no new process
            web.PLAN_RETRY_SECONDS = 0
            assert await web.plan_data(app, 1) is not None and len(calls) == 2  # after the pause: planned again
        finally:
            web.asyncio.create_subprocess_exec, web.PLAN_RETRY_SECONDS = orig, retry
    saved = web.plan_data
    client(go, tmp_path, trigger)


def test_a_kotona_day_looks_for_the_cheapest_hours_in_the_daytime():
    rows = rows_of()
    for k in range(40, 52):
        rows[k]["price"] = 1.0                                          # Tuesday 10-13
    assert web.summarise_plan({"rows": rows, "now_index": 0}, CFG)["cheapest"]["from"] != "10:00"
    o.HOME_FILE.write_text("2026-11-10T23:00\n")
    assert web.home_day(CFG, "2026-11-10") and not web.home_day(CFG, "2026-11-11")
    assert web.summarise_plan({"rows": rows, "now_index": 0}, CFG, True)["cheapest"]["from"] == "10:00"


def test_the_cards_say_what_away_does_in_each_season_and_why_normal_has_no_figure(tmp_path):
    async def trigger(app_dir):
        return 0

    async def go(c, app):
        html = await (await c.get("/", headers=ME)).text()
        for text in ("laitteet pois päältä", "Laitteet käynnistyvät", "s.away.temp", "kesto riippuu huonelämpötiloista"):
            assert text in html, text
        o.AWAY_FILE.write_text("")
        j = await (await c.get("/api/status", headers=ME)).json()
        assert j["away"]["temp"] == CFG["nobody_home"]["temp"] and j["away"]["preheat_hours"] == 2.0
    client(go, tmp_path, trigger)


def test_polls_that_arrive_together_share_one_plan_process(tmp_path):
    calls, gate = [], None

    class Proc:
        async def communicate(self):
            await gate.wait()
            return json.dumps({"mode": "heat", "now_index": None, "rows": rows_of()}).encode(), b""

    async def slow_exec(*cmd, **kw):
        calls.append(cmd)
        return Proc()

    async def trigger(app_dir):
        return 0

    async def go(c, app):
        nonlocal gate
        gate = asyncio.Event()
        web.plan_data = saved
        orig = web.asyncio.create_subprocess_exec
        web.asyncio.create_subprocess_exec = slow_exec
        try:
            polls = [asyncio.ensure_future(web.plan_data(app, 1)) for _ in range(3)]
            for _ in range(5):
                await asyncio.sleep(0)
            gate.set()
            results = await asyncio.gather(*polls)
            assert len(calls) == 1 and all(r is not None for r in results)
        finally:
            web.asyncio.create_subprocess_exec = orig
    saved = web.plan_data
    client(go, tmp_path, trigger)


# ---- review findings ----------------------------------------------------------------------------------

def test_the_cached_plan_of_tomorrow_keeps_its_rows_for_the_next_poll(tmp_path):
    cached = {"date": "2026-11-11", "has_prices": True, "rows": rows_of(day="2026-11-11")}

    async def trigger(app_dir):
        return 0

    async def go(c, app):
        async def plan(a, day_offset=0):
            return cached if day_offset else None
        web.plan_data = plan
        await c.get("/api/status", headers=ME)
        await c.get("/api/status", headers=ME)
        assert "rows" in cached and len(cached["rows"]) == 96
    client(go, tmp_path, trigger)


def test_a_failed_write_or_a_dry_run_is_not_shown_as_the_target():
    hist = (",".join(o.HISTORY_COLUMNS) + "\n"
            "2026-11-10T08:00:03+02:00,Unit B,set-failed,20,19.0,22.0,22,0,5,x,5,normal\n"
            "2026-11-10T08:15:03+02:00,Unit B,dry-run,20,19.0,22.0,22,0,5,x,5,normal\n"
            "2026-11-10T08:30:03+02:00,Unit B,hold,20,19.0,,,,5,manual change,5,\n")
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        Path(d, "history-2026-11.csv").write_text(hist)
        out = web.actual_past(CFG, datetime(2026, 11, 10, 9, 0, tzinfo=TZ), Path(d), rows_of(), 36)
    assert out[32]["final"] == 19.0 and out[33]["final"] is None and out[34]["final"] == 19.0


def test_the_unit_card_keeps_the_old_target_after_a_failed_write(tmp_path):
    now = datetime.now(TZ)
    o.monthly_path(tmp_path / CFG["telemetry_file"], now, TZ).write_text(
        ",".join(o.TELEMETRY_COLUMNS) + "\n" + f"{now.isoformat()},Unit B,21.0,19.0,True,heat,0,1.0,True\n")
    o.monthly_path(tmp_path / CFG["history_file"], now, TZ).write_text(
        ",".join(o.HISTORY_COLUMNS) + "\n" +
        f"{(now + timedelta(seconds=5)).isoformat()},Unit B,set-failed,21.0,19.0,22.0,22,0,5,x,5,normal\n")
    assert web.build_status(CFG, now, tmp_path)["units"][0]["target"] == "19.0"


def test_the_past_line_uses_the_cooling_offsets_when_cooling():
    o.SEASON_FILE.write_text("cool\n")
    hist = (",".join(o.HISTORY_COLUMNS) + "\n"
            "2026-07-14T08:00:03+03:00,Unit A,keep,24,23.0,23.0,23,0,20,x,5,normal\n"
            "2026-07-14T08:00:04+03:00,Unit B,keep,24,22.5,22.5,23,0,20,x,5,normal\n")
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        Path(d, "history-2026-07.csv").write_text(hist)
        rows = [{**r, "start": r["start"].replace("+02:00", "+03:00")} for r in rows_of(day="2026-07-14")]
        out = web.actual_past(CFG, datetime(2026, 7, 14, 9, 0, tzinfo=TZ), Path(d), rows, 36)
    assert out[32]["final"] == 23.0


def test_cooling_away_shows_no_cost_figure(tmp_path):
    o.SEASON_FILE.write_text("cool\n")
    o.AWAY_FILE.write_text("")

    async def trigger(app_dir):
        return 0

    async def go(c, app):
        j = await (await c.get("/api/status", headers=ME)).json()
        assert j["away"]["no_estimate"] is True and "estimate" not in j["away"]
    client(go, tmp_path, trigger)


def test_a_page_run_during_pause_or_an_open_breaker_reports_skipped(monkeypatch, tmp_path):
    args = o.argparse.Namespace(now=None, dry_run=False, away=False, away_until=None, holiday=False,
                                assume_home=False, prices_file=None, outdoor_temp=1.0, sun=0.0, lock_wait=180.0)
    sent = []

    async def beat(cfg):
        sent.append(1)
    monkeypatch.setattr(o, "send_heartbeat", beat)
    o.PAUSE_FILE.write_text("")
    assert asyncio.run(o.run_live(args, CFG, TZ)) == o.RUN_SKIPPED
    args.lock_wait = 0.0
    assert asyncio.run(o.run_live(args, CFG, TZ)) == 0
    o.PAUSE_FILE.unlink()
    state = tmp_path / "state.json"
    cfg = {**CFG, "resilience": {**CFG["resilience"], "state_file": str(state)}}
    breaker = o.make_breaker(cfg)
    for _ in range(cfg["resilience"]["breaker_failure_threshold"]):
        breaker.record_failure(datetime.now(o.timezone.utc))
    args.lock_wait = 180.0
    assert asyncio.run(o.run_live(args, cfg, TZ)) == o.RUN_SKIPPED


def test_tomorrows_plan_shows_a_nudge_that_runs_past_midnight():
    day = date(2030, 1, 2)                                              # in the future: nothing is past yet
    noon = datetime(2030, 1, 2, 12, 0, tzinfo=TZ)
    nudge = o.Nudge(shift=-1.0, until=datetime(2030, 1, 2, 1, 30, tzinfo=TZ))
    rows = o.plan_rows(noon, None, CFG, 5.0, nudge=nudge)
    by_time = {r["start"].strftime("%H:%M"): r["final"] for r in rows}
    assert by_time["00:00"] == 18.0 and by_time["01:15"] == 18.0 and by_time["01:30"] == 19.0


def test_the_page_plans_again_only_when_something_it_depends_on_changed(tmp_path):
    calls = []

    class Proc:
        async def communicate(self):
            return json.dumps({"mode": "heat", "now_index": None, "rows": rows_of()}).encode(), b""

    async def fake_exec(*cmd, **kw):
        calls.append(cmd)
        return Proc()

    async def trigger(app_dir):
        return 0

    async def go(c, app):
        web.plan_data = saved
        orig = web.asyncio.create_subprocess_exec
        web.asyncio.create_subprocess_exec = fake_exec
        try:
            await web.plan_data(app, 1)
            await web.plan_data(app, 1)
            assert len(calls) == 1                                      # same facts: the cached plan
            o.NUDGE_FILE.write_text("-1.0 2099-01-01T00:00 floor=0.0\n")
            await web.plan_data(app, 1)
            assert len(calls) == 2                                      # a control changed: plan again
        finally:
            web.asyncio.create_subprocess_exec = orig
    saved = web.plan_data
    client(go, tmp_path, trigger)


def test_cooling_bands_save_above_the_base_and_pre_cool_below_it():
    rows = rows_of(day="2026-07-14", final=23.0)
    for r in rows:
        r["base"] = 23.0
    rows[8].update(final=21.5, kind="preheat")            # 02:00 pre-cool before a spike
    rows[12].update(final=21.5, kind="cheap")             # 03:00 cheap pre-cool all hour: no band
    rows[64].update(final=25.0, kind="extreme")           # 16:00 drifting warmer: the saving
    blocks = web.summarise_plan({"mode": "cool", "now_index": None, "rows": rows})["blocks"]
    assert blocks == [{"kind": "preheat", "from": "02:00", "to": "02:15"},
                      {"kind": "save", "from": "16:00", "to": "16:15"}]
    heat = web.summarise_plan({"mode": "heat", "now_index": None, "rows": rows})["blocks"]
    assert {"kind": "save", "from": "02:00", "to": "02:15"} in heat   # the heating reading of the same line


def test_the_plan_follows_the_season_unless_a_mode_is_asked_for(capsys):
    o.SEASON_FILE.write_text("cool\n")
    common = ["--config", str(ROOT / "config.toml"), "--plan-json", "--now", "2026-11-10T12:00",
              "--prices-file", str(ROOT / "sample_prices.json"), "--outdoor-temp", "20", "--sun", "0"]
    assert o.main(common) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "cool"
    assert o.main(common + ["--mode", "heat"]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "heat"


def test_the_cheapest_stretch_is_as_long_as_configured():
    rows = rows_of()
    for k in range(80, 92):
        rows[k]["price"] = 2.0
    two = {**CFG, "web": {**CFG["web"], "cheapest_hours": 2}}
    got = web.summarise_plan({"rows": rows, "now_index": 0}, two)["cheapest"]
    assert (got["from"], got["to"]) == ("20:00", "22:00")
    assert "Cheapest 2 h while home" in web.render_page({**two, "web": {**two["web"], "language": "en"}})


def test_kotona_counts_from_the_configured_start_of_the_day():
    o.HOME_FILE.write_text("2026-11-10T06:30\n")
    late = {**CFG, "home": {**CFG["home"], "day_start": "06:00"}}
    assert web.home_day(late, "2026-11-10") and not web.home_day(CFG, "2026-11-10")   # CFG: from 07:00
