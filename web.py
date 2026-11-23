#!/usr/bin/env python3
"""Remote control page for the heat pump controller (English or Finnish: [web] language).

Meant to sit behind `tailscale serve` on the VM: the page listens on loopback only, and
Tailscale adds the logged-in user's login in a header. Only logins listed in WEB_USERS
(web.env, `login=nickname,login=nickname`) get in. The page only writes the control files
through the same functions the command line uses, then starts a normal controller run; it
never reads the MELCloud credentials.
"""
import asyncio
import contextlib
import csv
import io
import json
import logging
import logging.handlers
import os
import re
import sys
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path
from typing import Dict, Optional

from aiohttp import web

import ilp_optimizer as o
from web_text import TEXT, text

USER_HEADER = "Tailscale-User-Login"   # the default of [web] user_header
CSRF_HEADER, CSRF_VALUE = "X-Requested-With", "ilp-page"

PREFS_FILE = "web_prefs.json"   # in the app dir: {login: {"language": "fi"}}, written only by this page

log = logging.getLogger("ilp_web")


def parse_users(text: str) -> Dict[str, str]:
    """`login=nickname,login=nickname` -> {login (lower case): nickname}."""
    users = {}
    for part in (text or "").split(","):
        login, _, nick = part.strip().partition("=")
        if login.strip() and nick.strip():
            users[login.strip().lower()] = nick.strip()
    return users


# --------------------------------------------------------------------------
# Per-person settings: kept by login (the identity the proxy vouches for), the server default otherwise
# --------------------------------------------------------------------------

def read_prefs(path: Path) -> Dict[str, dict]:
    """The stored settings by login (lower case); none when the file is missing, and, logged, when it
    is unreadable: a damaged file must not lock anyone out of the page."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if isinstance(v, dict)}
        log.warning("%s is not a JSON object: ignored", path.name)
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        log.warning("%s is unreadable: ignored", path.name, exc_info=True)
    return {}


def save_pref(path: Path, login: str, key: str, value: str) -> None:
    """Store one setting of one login, replacing the file in one step so a crash leaves the old one.
    The read and the write run without yielding to the event loop, so two requests cannot lose each other's."""
    prefs = read_prefs(path)
    prefs[login] = {**prefs.get(login, {}), key: value}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(prefs, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def person_cfg(app: web.Application, login: str) -> dict:
    """The config with the page language this person chose, else the server's `[web] language`. A stored
    language the page no longer has is ignored."""
    cfg = app["cfg"]
    language = read_prefs(app["app_dir"] / PREFS_FILE).get(login, {}).get("language")
    if language not in TEXT:
        return cfg
    return {**cfg, "web": {**cfg.get("web", {}), "language": language}}


# --------------------------------------------------------------------------
# Actions: each validates its value, writes the control file(s), returns a message in the page's language
# --------------------------------------------------------------------------

class Refused(Exception):
    """A request that is wrong, with the text for the person in the page's language."""


def quiet(fn, *args) -> int:
    with contextlib.redirect_stdout(io.StringIO()):  # the setters print for the command line
        return fn(*args)


def future_time(value: str, tz, now: datetime, t: dict) -> datetime:
    try:
        until = datetime.fromisoformat(value)
    except ValueError:
        raise Refused(t["time_invalid"])
    until = until if until.tzinfo else until.replace(tzinfo=tz)
    if until <= now:
        raise Refused(t["time_past"])
    return until


RETURN_WARMUP = timedelta(minutes=30)   # the page's way out of Away: the return is this far ahead


def away_holds(tz, now: datetime) -> bool:
    present, until = o.read_until_file(o.AWAY_FILE, "AWAY", tz)
    return present and (until is None or until > now)


def return_from_away(tz, now: datetime, cfg: dict, t: dict) -> str:
    """Away to Normal or Home: the return time becomes now + RETURN_WARMUP, so the house warms up before
    the absence ends (the warm-up before a return time does it). A return that is already sooner stays."""
    if not away_holds(tz, now):
        return t["away_ended"]
    until = now + RETURN_WARMUP
    _, current = o.read_until_file(o.AWAY_FILE, "AWAY", tz)
    if current is not None and current < until:
        return t["away_returning"]
    quiet(o.set_control, "away", until.astimezone(tz).replace(tzinfo=None).isoformat(timespec="minutes"), tz, cfg)
    return t["away_returning"]


def do_action(action: str, value: str, confirm: bool, cfg: dict, now: datetime) -> str:
    tz = o.ZoneInfo(cfg["timezone"])
    t = text(cfg)
    if not isinstance(value, str) or len(value) > 40:
        raise Refused(t["bad_request"])
    if action == "nudge":
        if value in ("warmer", "cooler") and o.read_normal(o.NORMAL_FILE, tz, now, cfg):
            # "normal now" sets every unit itself and the nudge would only start when it ends
            raise Refused(t["normal_running"])
        before = o.read_nudge(o.NUDGE_FILE, tz, now)
        if value not in ("warmer", "cooler", "longer", "reset"):
            raise Refused(t["bad_request"])
        if value == "longer" and not before:
            raise Refused(t["nothing_to_extend"])
        if quiet(o.set_nudge, value, tz, cfg, now):
            raise Refused(t["bad_request"])
        after = o.read_nudge(o.NUDGE_FILE, tz, now)
        if value == "warmer" and after.floor > before.floor:
            return t["nudge_eased"]
        if value == "warmer" and (after.floor, after.shift) == (before.floor, before.shift):
            return t["nudge_at_max"].format(total=f"{after.floor + after.shift:g}")
        return t[f"nudge_{value}"]
    if action == "away":
        if value == "return":
            return return_from_away(tz, now, cfg, t)
        if value not in ("on", "off"):
            future_time(value, tz, now, t)
        quiet(o.set_control, "away", value, tz, cfg)
        if value != "off":
            o.HOME_FILE.unlink(missing_ok=True)   # the page's switch holds one of Away, Normal and Home
        return t["away_ended"] if value == "off" else t["away_on"]
    if action == "home":
        if value not in ("today", "week", "off"):
            future_time(value, tz, now, t)
        if quiet(o.set_home, value, tz, now):
            raise Refused(t["bad_request"])
        if value != "off" and away_holds(tz, now):
            return_from_away(tz, now, cfg, t)   # Home from Away: the house warms up first
            return t["home_after_warmup"]
        return t["home_mode_off"] if value == "off" else t["home_mode_on"]
    if action == "season":
        if value not in ("heat", "cool"):
            raise Refused(t["bad_request"])
        if not confirm:
            raise Refused(t["season_needs_confirm"])
        quiet(o.set_season, value)
        return t[f"season_set_{value}"]
    if action == "normal":
        if value not in ("on", "off"):
            raise Refused(t["bad_request"])
        if value == "on" and not confirm:
            raise Refused(t["normal_needs_confirm"])
        quiet(o.set_control, "normal", value, tz, cfg)
        temp = cfg["normal"]["temp"]
        return t["normal_set"].format(temp=f"{temp:g}") if value == "on" else t["normal_cancelled"]
    if action == "pause":
        if value not in ("on", "off"):
            raise Refused(t["bad_request"])
        if value == "on" and not confirm:
            raise Refused(t["pause_needs_confirm"])
        quiet(o.set_control, "pause", value, tz, cfg)
        return t["pause_set_on"] if value == "on" else t["pause_set_off"]
    raise Refused(t["unknown_action"])


async def trigger_run(app_dir: Path) -> Optional[int]:
    """Start a normal controller run so the change takes effect now, and return its exit code. It
    waits for the lock while cron's run is in progress (a run with MELCloud retries can take a couple
    of minutes). The page never reads .env: run.sh does, in the child."""
    proc = await asyncio.create_subprocess_exec(
        str(app_dir / "run.sh"), "--lock-wait", "180",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, cwd=app_dir)
    return await proc.wait()


RUN_STATES = {0: "done", o.RUN_SKIPPED: "skipped"}


# Seconds of quiet after a page action before its run: presses in a row, or a press and its undo,
# reach MELCloud as one write instead of one each. Presses that keep coming hold it back at most
# RUN_MAX_WAIT from the first one waiting.
RUN_DELAY = 5.0
RUN_MAX_WAIT = 15.0


def schedule_run(app: web.Application) -> None:
    """Run the controller `run_delay` after the last action, so the change reaches the units. Actions
    while a run is going add one more run after it, not one each."""
    runs = app["runs"]   # one dict changed in place: an app's own keys are fixed once it runs
    app["plan_cache"].clear()   # the new plan is drawn at once, before the run
    now = asyncio.get_running_loop().time()
    if runs["due"] is None:
        runs["first"] = now
    runs["due"] = min(now + app["run_delay"], runs["first"] + app["run_max_wait"])
    if runs["worker"] is None:
        task = asyncio.ensure_future(run_when_quiet(app))
        runs["worker"] = task
        app["tasks"].add(task)  # keep a reference until it finishes
        task.add_done_callback(app["tasks"].discard)


async def run_when_quiet(app: web.Application) -> None:
    """Wait until `due`, which each action moves later, then run; again if an action came during the run."""
    runs, loop = app["runs"], asyncio.get_running_loop()
    try:
        while runs["due"] is not None:
            wait = runs["due"] - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
                continue
            runs["due"] = None
            await run_and_report(app)
    finally:
        runs["worker"] = None


async def run_and_report(app: web.Application) -> None:
    """Run the controller for changes made on the page and keep its outcome for the status, so the
    page can say whether the units were updated."""
    try:
        code = await app["trigger"](app["app_dir"])
        state = RUN_STATES.get(code, "failed") if code is not None else "done"
    except Exception:
        log.exception("controller run for a page action failed to start")
        state = "failed"
    app["runs"]["last"] = {"state": state, "at": datetime.now(o.ZoneInfo(app["cfg"]["timezone"])).isoformat(timespec="seconds")}
    app["plan_cache"].clear()


# --------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------

def last_rows(path: Path) -> Dict[str, dict]:
    """The newest row per unit of a history or telemetry CSV (empty when there is none)."""
    rows: Dict[str, dict] = {}
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rows[row["unit"]] = row
    except FileNotFoundError:
        pass
    return rows


def until_text(until: Optional[datetime], tz) -> Optional[str]:
    return until.astimezone(tz).isoformat(timespec="minutes") if until else None


def nudge_status(n: "o.Nudge", tz) -> dict:
    return {"on": bool(n), "floor": n.floor, "shift": n.shift, "until": until_text(n.until, tz) if n else None}


def build_status(cfg: dict, now: datetime, app_dir: Path) -> dict:
    tz = o.ZoneInfo(cfg["timezone"])
    away_present, away_until = o.read_until_file(o.AWAY_FILE, "AWAY", tz)
    if away_until is not None and now >= away_until:
        away_present = False   # over; the next run removes the file
    home_on = o.holiday_active(o.HOME_FILE, tz, now)
    _, home_until = o.read_until_file(o.HOME_FILE, "HOME", tz)
    season = o.read_season(o.SEASON_FILE)
    pause = None
    if o.PAUSE_FILE.exists():
        pause = "settle" if o.PAUSE_FILE.read_text(encoding="utf-8").strip() == "settle" else "hold"
    telemetry = last_rows(o.monthly_path(app_dir / cfg["telemetry_file"], now, tz))
    history = last_rows(o.monthly_path(app_dir / cfg["history_file"], now, tz))
    normal = o.read_normal(o.NORMAL_FILE, tz, now, cfg)
    units = []
    for name in sorted(telemetry):
        t, h = telemetry[name], history.get(name, {})
        # telemetry is read before the run writes; a newer history row has the target it left
        try:
            held = o.held_target(h) if h.get("time_local", "") >= t.get("time_local", "") else None
        except ValueError:
            held = None
        target = f"{held:.1f}" if held is not None else t.get("target_temp")
        units.append({"name": name, "room": t.get("room_temp"), "target": target,
                      "mode": t.get("mode"), "power": t.get("power"), "time": t.get("time_local"),
                      "guarded": "comfort guard" in h.get("reason", "")})
    mode = season   # always heat or cool: heat when there is no SEASON file
    return {"mode": "paused" if pause else mode, "season": season, "season_set": o.SEASON_FILE.exists(), "pause": pause,
            "away": {"on": away_present, "until": until_text(away_until, tz), "temp": cfg["nobody_home"]["temp"], "dear_temp": cfg["nobody_home"]["dear_temp"],
                     "dear_price": cfg["nobody_home"]["dear_price"],
                     "preheat_hours": cfg["away"]["preheat_hours"]},
            "home": {"on": home_on, "until": until_text(home_until, tz)},
            "nudge": nudge_status(o.read_nudge(o.NUDGE_FILE, tz, now), tz), "units": units,
            "normal": {"on": normal is not None, "temp": cfg["normal"]["temp"],
                       "until": until_text(normal["until"], tz) if normal else None}}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

@web.middleware
async def guard(request: web.Request, handler):
    users = request.app["users"]
    header = request.app["cfg"].get("web", {}).get("user_header", USER_HEADER)
    login = request.headers.get(header, "").strip().lower()
    nick = users.get(login)
    if nick is None:
        return web.Response(status=403, text=text(request.app["cfg"])["no_access"], content_type="text/plain")
    request["nick"] = nick
    request["login"] = login
    request["cfg"] = person_cfg(request.app, login)
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if request.headers.get(CSRF_HEADER) != CSRF_VALUE or \
                (origin and origin.split("://", 1)[-1] != request.host):
            return web.json_response({"error": text(request.app["cfg"])["request_rejected"]}, status=403)
    return await handler(request)


async def page(request: web.Request) -> web.Response:
    return web.Response(text=render_page(request["cfg"]), content_type="text/html",
                        headers={"Cache-Control": "no-store"})


async def manifest(request: web.Request) -> web.Response:
    t = text(request["cfg"])
    return web.json_response({"name": t["app_name"], "short_name": t["app_short"], "start_url": "/",
                              "display": "standalone", "background_color": "#f3f1ee",
                              "theme_color": "#f3f1ee"})


async def status(request: web.Request) -> web.Response:
    app = request.app
    now = datetime.now(o.ZoneInfo(app["cfg"]["timezone"]))
    data = build_status(app["cfg"], now, app["app_dir"])
    data["user"] = request["nick"]
    data["run"] = {"state": "running"} if app["runs"]["worker"] else app["runs"]["last"]   # waiting counts too
    data["plan"] = await plan_data(app)
    if data["plan"]:
        data["plan"] = {**data["plan"], "room": room_series(app["cfg"], now, app["app_dir"], data["plan"]["times"])}
    tomorrow = await plan_data(app, 1)
    data["plan_tomorrow"] = dict(tomorrow) if tomorrow else None   # a copy: the cached plan keeps its rows
    t = text(request["cfg"])
    for key in ("plan", "plan_tomorrow"):
        if data[key] and data[key].get("now"):
            data[key]["now"] = localise_now(data[key]["now"], t)   # the cached plan is shared; its words are per person
    rows = [r for p in (data["plan"], data["plan_tomorrow"]) if p for r in p.get("rows", [])]
    units = max(len(data["units"]), 1)
    if data["season"] == "cool":
        data["away"]["no_estimate"] = True   # cooling: the units are switched off, the plan cannot price that
    for key in ("nudge", "away", "home"):
        if data[key]["on"] and not data[key].get("no_estimate"):
            data[key]["estimate"] = estimate(rows, key, now, data[key]["until"], app["cfg"], units,
                                             data["season"])
    for p in (data["plan"], data["plan_tomorrow"]):
        if p:
            p.pop("rows", None)   # only for the estimates: the page does not need them
    return web.json_response(data)


def localise_now(now: dict, t: dict) -> dict:
    """`now` of a summarised plan with the pill and the reason in the language of catalog `t`; the
    plan itself is cached for everybody."""
    if "reason_log" not in now:
        return now
    return {**now, "pill": t.get(f"pill_{now['kind']}", t["pill_normal"]),
            "reason": reason_text(t, now["reason_log"], now["kind"], now["below_base"])}


def estimate(rows: list, key: str, now: datetime, until: Optional[str], cfg: dict, units: int,
             mode: str = "heat") -> Optional[dict]:
    """A rough cost (+) or saving (-) in cents of the override `key` from now to its end, over the
    quarters whose price is known: (target - target without it) x Wh per unit-degree-quarter x units x
    price, the other way round when cooling (a warmer target saves). `until_end` says whether the
    known prices reach the override's end; when not, the figure covers only part of it."""
    sign = -1.0 if mode == "cool" else 1.0
    wh = cfg["estimate"]["wh_per_degree_quarter"]
    end = datetime.fromisoformat(until) if until else None
    now_q = now.replace(minute=now.minute // 15 * 15, second=0, microsecond=0)
    cents, last, counted = 0.0, None, False
    for r in rows:
        start = datetime.fromisoformat(r["start"])
        if start < now_q or (end and start >= end) or r.get("price") is None or key not in r.get("without", {}):
            continue
        cents += sign * (r["final"] - r["without"][key]) * wh * units * r["price"] / 1000
        last, counted = start, True
    if not counted:
        return None
    covered = end is not None and last + timedelta(minutes=15) >= end
    return {"cents": round(cents, 1), "until_end": covered}


# the log-style reason fragments that get a note, and the catalog key of the note, in display order
NOTE_KEYS = (("comfort hours", "note_comfort_hours"), ("comfort guard", "note_comfort_guard"),
             ("home", "note_home"), ("cheapest quarter kept", "note_cheapest_quarter"),
             ("nudge", "note_nudge"), ("sun", "note_sun"))


def reason_text(t: dict, reason: str, kind: str, still_cut: bool = False) -> dict:
    """The controller's log-style reason in the page's language: a headline and short notes.
    `still_cut`: the target is under the base schedule, so the warm-up before a comfort window only
    limits the cut."""
    parts = [p.strip() for p in reason.split(", ")[1:]]
    notes = [t[key] for prefix, key in NOTE_KEYS if any(p.startswith(prefix) for p in parts)]
    if any(p.startswith(f"{o.WARMUP_REASON} comfort") for p in parts):
        notes.insert(0, t["note_ramp_cut" if still_cut else "note_ramp_free"])
    if any(p.startswith(f"{o.WARMUP_REASON} home") for p in parts):
        notes.insert(0, t["note_warmup_home"])
    return {"text": t.get(f"main_{kind}", t["main_none"]), "notes": notes}


PREHEAT_KINDS = {"comfort_preheat", "preheat", "peak_preheat", "return_preheat"}


def blocks_of(rows: list, key: str, sign: int, min_delta: float = 0.5,
              kinds: Optional[set] = None) -> list:
    """Runs of quarter hours whose target is at least min_delta above (sign +1) or below (-1) the
    base schedule, as {kind, from, to} ('to' is the end of the last quarter hour). The bands are
    read off the target line itself, so they always match what the chart shows. A pre-heat band
    needs a pre-heat rule too (`kinds`, by default for the "preheat" key): the cheap-price boost
    raises the line all day but is not a pre-heat."""
    kinds = PREHEAT_KINDS if kinds is None and key == "preheat" else kinds
    out, run = [], None
    for k, r in enumerate(rows + [None]):
        on = r is not None and r["final"] is not None and sign * (r["final"] - r["base"]) >= min_delta \
            and (kinds is None or r.get("kind") in kinds)
        if on and run is None:
            run = k
        if not on and run is not None:
            end = datetime.fromisoformat(rows[k - 1]["start"]) + timedelta(minutes=15)
            out.append({"kind": key, "from": rows[run]["start"][11:16], "to": end.strftime("%H:%M")})
            run = None
    return out


def room_series(cfg: dict, now: datetime, app_dir: Path, times: list) -> list:
    """The measured room temperature (mean of the units that reported) for each quarter hour of
    `times` ('HH:MM' of today) that has a telemetry reading, else None."""
    tz = o.ZoneInfo(cfg["timezone"])
    slots: Dict[str, list] = {}
    try:
        with open(o.monthly_path(app_dir / cfg["telemetry_file"], now, tz), newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                t = row.get("time_local", "")
                if not t.startswith(f"{now:%Y-%m-%d}") or not row.get("room_temp"):
                    continue
                slot = f"{t[11:13]}:{int(t[14:16]) // 15 * 15:02d}"
                slots.setdefault(slot, []).append(float(row["room_temp"]))
    except (FileNotFoundError, ValueError):
        pass
    return [round(sum(slots[t]) / len(slots[t]), 1) if t in slots else None for t in times]


def actual_past(cfg: dict, now: datetime, app_dir: Path, rows: list, now_index: Optional[int]) -> list:
    """Today's rows with every quarter before now's taken from the history: the targets the controller
    really set, not the plan recomputed with today's controls (an away set at noon must not redraw the
    morning). Of the units, the one highest over its own offset stands for the quarter (the page's
    line has no unit offset; a night shift unit keeps the warmer night). A past quarter without a history row
    has no target (a gap in the line)."""
    if now_index is None:
        return rows
    tz = o.ZoneInfo(cfg["timezone"])
    offsets = cfg["units"].get("cool_offsets" if o.read_season(o.SEASON_FILE) == "cool" else "offsets", {})
    slots: Dict[str, Dict[str, dict]] = {}
    try:
        with open(o.monthly_path(app_dir / cfg["history_file"], now, tz), newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                t = row.get("time_local", "")
                try:
                    held = o.held_target(row)
                except ValueError:
                    held = None
                if t.startswith(f"{now:%Y-%m-%d}") and held is not None:
                    slots.setdefault(f"{t[:14]}{int(t[14:16]) // 15 * 15:02d}", {})[row["unit"]] = {**row, "held": held}
    except FileNotFoundError:
        pass
    out = []
    for k, r in enumerate(rows):
        if k >= now_index:
            out.append(r)
            continue
        units = slots.get(r["start"][:16], {})
        if not units:
            out.append({**r, "final": None, "kind": "none", "reason": "no run"})
            continue
        try:
            name, h = max(units.items(), key=lambda u: u[1]["held"] - offsets.get(u[0], 0.0))
            final = h["held"] - offsets.get(name, 0.0)
            base = float(h["base"]) if h.get("base") else r["base"]
        except ValueError:
            out.append({**r, "final": None, "kind": "none", "reason": "no run"})
            continue
        out.append({**r, "final": final, "base": base, "reason": h.get("reason", ""),
                    "kind": o.reason_kind(h.get("reason", "")), "without": {}})
    return out


def cheapest_stretch(rows: list, quarters: int = 12, start: int = 0, hours: Optional[tuple] = None) -> Optional[dict]:
    """The cheapest run of `quarters` consecutive priced quarter hours starting at or after index
    `start` (for today: now, a stretch already gone is no use), inside the local `hours`
    ('HH:MM', 'HH:MM', '24:00' = midnight) when given, as {from, to, avg}, or None."""
    best = None
    for k in range(start, len(rows) - quarters + 1):
        window = [r["price"] for r in rows[k:k + quarters]]
        if any(p is None for p in window):
            continue
        if hours and not all(hours[0] <= r["start"][11:16] < hours[1] for r in rows[k:k + quarters]):
            continue
        avg = sum(window) / quarters
        if best is None or avg < best[0]:
            best = (avg, k)
    if best is None:
        return None
    avg, k = best
    end = datetime.fromisoformat(rows[k + quarters - 1]["start"]) + timedelta(minutes=15)
    return {"from": rows[k]["start"][11:16], "to": end.strftime("%H:%M"), "avg": round(avg, 2)}


def home_hours(cfg: Optional[dict], day: str, home_day: bool = False) -> Optional[tuple]:
    """The usual at-home hours of a date ('YYYY-MM-DD'), [web] home_hours_weekday / _weekend; a
    Home or HOLIDAY day (someone home in the daytime) gets the weekend hours."""
    w = (cfg or {}).get("web", {})
    weekend = home_day or datetime.fromisoformat(day).weekday() >= 5
    key = "home_hours_weekend" if weekend else "home_hours_weekday"
    return tuple(w[key]) if key in w else None


def home_day(cfg: dict, day: str) -> bool:
    """Whether HOME or HOLIDAY covers the daytime of a date: on, and not over by its morning."""
    tz = o.ZoneInfo(cfg["timezone"])
    start = o.clock(cfg["home"]["day_start"])   # the day as Home counts it
    morning = datetime.combine(date.fromisoformat(day), start, tzinfo=tz)
    for path, label in ((o.HOME_FILE, "HOME"), (o.HOLIDAY_FILE, "HOLIDAY")):
        present, until = o.read_until_file(path, label, tz)
        if present and (until is None or until > morning):
            return True
    return False


def summarise_plan(raw: dict, cfg: Optional[dict] = None, at_home: bool = False) -> dict:
    """What the page needs from `--plan-json`: series for the chart, the price now and why."""
    rows, idx = raw["rows"], raw.get("now_index")
    t = text(cfg)
    now_row = rows[idx] if idx is not None else None
    return {
        "date": rows[0]["start"][:10],
        # a day counts as priced only when most of it is (the controller needs 20 h): tomorrow's feed
        # fills in about 14:00, and a few early hours must not look like a plan
        "has_prices": sum(r["price"] is not None for r in rows)
                      >= len(rows) * (cfg or {}).get("prices", {}).get("min_hours_available", 20) / 24,
        "avg": (round(sum(known) / len(known), 2) if (known := [r["price"] for r in rows if r["price"] is not None])
                else None),
        "times": [r["start"][11:16] for r in rows],
        "outdoor": [r.get("outdoor") for r in rows],
        "has_outdoor": any(r.get("outdoor") is not None for r in rows),
        "weather": [r.get("weather_code") for r in rows], "is_day": [r.get("is_day") for r in rows],
        "price": [r["price"] for r in rows], "target": [r["final"] for r in rows],
        "now_index": idx,
        # for laundry and the like, in the hours someone is usually home to start it
        "cheapest": cheapest_stretch(rows, round(4 * (cfg or {}).get("web", {}).get("cheapest_hours", 3)), idx or 0,
                                     home_hours(cfg, rows[0]["start"][:10], at_home)),
        "rows": rows,   # for the override estimates; status() removes them
        "now": None if now_row is None else {
            "price": now_row["price"], "target": now_row["final"], "kind": now_row["kind"],
            "outdoor": now_row.get("outdoor"), "weather": now_row.get("weather_code"),
            "is_day": now_row.get("is_day"),
            "reason_log": now_row["reason"],
            "below_base": now_row["final"] is not None and now_row["final"] < now_row["base"]},
        # heating saves below the base and pre-heats above it; cooling the other way round: a warmer
        # setpoint saves and a pre-cool runs below the base
        "mode": raw.get("mode", "heat"),
        "blocks": sorted(blocks_of(rows, "preheat", -1 if raw.get("mode") == "cool" else +1)
                         + blocks_of(rows, "save", +1 if raw.get("mode") == "cool" else -1),
                         key=lambda b: b["from"]),
    }


async def plan_data(app: web.Application, day_offset: int = 0) -> Optional[dict]:
    """`--plan-json` for today (0) or tomorrow (1), cached; it fetches prices and weather but
    touches nothing. Tomorrow's prices exist only after about 14:00 (the plan then has none)."""
    cache, loop = app["plan_cache"], asyncio.get_running_loop()
    facts = plan_facts(app, day_offset)
    cached = cache.get(day_offset)
    if cached and cached[0] == facts and (cached[1] is not None or loop.time() - cached[2] < PLAN_RETRY_SECONDS):
        return cached[1]   # nothing the plan depends on has changed (or it failed a moment ago)
    running = app["plan_runs"].get(day_offset)
    if running and running[0] == facts:
        return await asyncio.shield(running[1])   # another poll is already planning these facts
    task = asyncio.ensure_future(compute_plan(app, day_offset))
    app["plan_runs"][day_offset] = (facts, task)
    try:
        data = await asyncio.shield(task)
    finally:
        if app["plan_runs"].get(day_offset, (None, None))[1] is task:
            del app["plan_runs"][day_offset]
    cache[day_offset] = (facts, data, loop.time())
    return data


PLAN_RETRY_SECONDS = 30   # a failed plan (price or weather feed down) is tried again after this


async def compute_plan(app: web.Application, day_offset: int) -> Optional[dict]:
    """Run `--plan-json` for the day and summarise it; None (logged) when it fails."""
    cmd = [sys.executable, str(app["app_dir"] / "ilp_optimizer.py"), "--plan-json"]
    if day_offset:
        tz = o.ZoneInfo(app["cfg"]["timezone"])
        day = (datetime.now(tz) + timedelta(days=day_offset)).date()
        cmd += ["--now", f"{day.isoformat()}T12:00"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, cwd=app["app_dir"])
        out, _ = await asyncio.wait_for(proc.communicate(), 60)
        raw = json.loads(out.decode("utf-8"))
        if not day_offset:
            raw["rows"] = actual_past(app["cfg"], datetime.now(o.ZoneInfo(app["cfg"]["timezone"])),
                                      app["app_dir"], raw["rows"], raw.get("now_index"))
        data = summarise_plan(raw, app["cfg"], home_day(app["cfg"], raw["rows"][0]["start"][:10]))
    except Exception:
        log.warning("plan for day +%d failed", day_offset, exc_info=True)
        return None
    return data


PLAN_INPUT_FILES = ("AWAY", "HOME", "HOLIDAY", "NUDGE", "NORMAL", "SEASON", "PAUSE")


def plan_facts(app: web.Application, day_offset: int) -> tuple:
    """What a plan depends on that can change between two polls: the quarter hour (prices and the
    forecast move on with it, tomorrow's prices arrive about 14:00), the control files and, for today,
    the history the past is drawn from. The same facts give the same plan."""
    cfg = app["cfg"]
    tz = o.ZoneInfo(cfg["timezone"])
    now = datetime.now(tz)
    controls = []
    for name in PLAN_INPUT_FILES:
        try:
            controls.append(getattr(o, f"{name}_FILE").read_text(encoding="utf-8"))
        except (FileNotFoundError, UnicodeDecodeError):   # a garbled file counts as none: its reader ignores it too
            controls.append(None)
    history = None
    if not day_offset:
        try:
            history = o.monthly_path(app["app_dir"] / cfg["history_file"], now, tz).stat().st_mtime_ns
        except FileNotFoundError:
            pass
    return day_offset, now.strftime("%Y-%m-%d %H"), now.minute // 15, tuple(controls), history


async def act(request: web.Request) -> web.Response:
    app = request.app
    try:
        body = await request.json()
        action, value, confirm = body.get("action"), body.get("value", ""), body.get("confirm") is True
    except Exception:
        return web.json_response({"error": text(app["cfg"])["bad_request"]}, status=400)
    now = datetime.now(o.ZoneInfo(app["cfg"]["timezone"]))
    try:
        message = do_action(str(action), value, confirm, request["cfg"], now)
    except Refused as exc:
        log.info("user=%s action=%s value=%s refused", request["nick"], action, str(value)[:40])
        return web.json_response({"error": str(exc)}, status=400)
    log.info("user=%s action=%s value=%s ok", request["nick"], action, value)
    schedule_run(app)
    return web.json_response({"message": message})


async def set_language(request: web.Request) -> web.Response:
    """Remember the page language of the person who asks (by login), for every device they use."""
    app = request.app
    t = text(request["cfg"])
    try:
        language = (await request.json()).get("language")
    except Exception:
        return web.json_response({"error": t["bad_request"]}, status=400)
    if language not in TEXT:
        return web.json_response({"error": t["bad_request"]}, status=400)
    try:
        save_pref(app["app_dir"] / PREFS_FILE, request["login"], "language", language)
    except OSError:
        log.exception("user=%s language not saved", request["nick"])
        return web.json_response({"error": t["language_not_saved"]}, status=500)
    log.info("user=%s language=%s", request["nick"], language)
    return web.json_response({"language": language})


def make_app(cfg: dict, users: Dict[str, str], app_dir: Path = o.APP_DIR, trigger=trigger_run,
             run_delay: float = RUN_DELAY, run_max_wait: float = RUN_MAX_WAIT) -> web.Application:
    app = web.Application(middlewares=[guard])
    app.update(cfg=cfg, users=users, app_dir=app_dir, trigger=trigger, run_delay=run_delay,
               run_max_wait=run_max_wait, tasks=set(), plan_cache={},
               runs={"due": None, "first": None, "worker": None, "last": None}, plan_runs={})
    app.add_routes([web.get("/", page), web.get("/manifest.json", manifest),
                    web.get("/api/status", status), web.post("/api/action", act),
                    web.post("/api/language", set_language)])
    return app


def setup_web_logging(cfg: dict) -> None:
    handler = logging.handlers.RotatingFileHandler(
        o.APP_DIR / "web.log", maxBytes=cfg["log_max_bytes"], backupCount=cfg["log_backup_count"],
        encoding="utf-8")
    handler.setFormatter(o.LocalTimeFormatter("%(asctime)s %(levelname)s %(message)s", o.ZoneInfo(cfg["timezone"])))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


PAGE = Path(__file__).with_name("web_page.html").read_text(encoding="utf-8")


def page_text_json(t: dict) -> str:
    """The catalog as a JSON object for the page's script: `</` is escaped so no text can close the tag."""
    return json.dumps(t, ensure_ascii=False).replace("</", "<\\/")


SETTING_PLACEHOLDERS = ("away_temp", "away_dear_temp", "pause_temp", "nudge_step", "nudge_hours", "cheapest_hours")


def with_settings(t: dict, cfg: dict) -> dict:
    """The catalog with the texts that quote a setting filled in from the config ({pause_temp} and the
    like); the placeholders the page fills itself are left alone."""
    nh, nudge = cfg["nobody_home"], cfg["nudge"]
    values = {"away_temp": nh["temp"], "away_dear_temp": nh["dear_temp"],
              "pause_temp": cfg["pause"]["heat_temp"],
              "nudge_step": nudge["step"], "nudge_hours": nudge["hours"],
              "cheapest_hours": cfg["web"]["cheapest_hours"]}
    shown = {k: f"{v:g}".replace(".", t.get("decimal_mark", ".")) for k, v in values.items()}
    pattern = re.compile(r"\{(" + "|".join(SETTING_PLACEHOLDERS) + r")\}")
    return {k: pattern.sub(lambda m: shown[m.group(1)], v) if isinstance(v, str) else v for k, v in t.items()}


def render_page(cfg: dict) -> str:
    """The page in the configured language: `{{key}}` placeholders filled (HTML-escaped), the
    `/*TEXT*/` marker replaced by the catalog for the script."""
    t = with_settings(text(cfg), cfg)
    html = re.sub(r"\{\{(\w+)\}\}", lambda m: escape(t[m.group(1)]), PAGE)
    return html.replace("/*TEXT*/", f"const T = {page_text_json(t)};")


LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def main() -> int:
    cfg = o.load_config(o.APP_DIR / "config.toml", o.APP_DIR / o.LOCAL_CONFIG)
    users = parse_users(os.environ.get("WEB_USERS", ""))
    if not users:
        print("WEB_USERS is empty: set login=nickname pairs in web.env", file=sys.stderr)
        return 2
    setup_web_logging(cfg)
    w = cfg["web"]
    if w["host"] not in LOOPBACK:
        # The login header is believed as sent: off loopback, anyone who reaches the port can claim a login.
        log.warning("[web] host is %s, not loopback: anyone who can reach port %s can control the units",
                    w["host"], w["port"])
    web.run_app(make_app(cfg, users), host=w["host"], port=w["port"], print=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
