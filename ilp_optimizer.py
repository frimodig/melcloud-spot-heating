#!/usr/bin/env python3
"""Spot-price-aware controller for MELCloud air-to-air heat pumps.

Every run computes one desired setpoint (base schedule + price offset) and
writes it to each controlled unit whose target differs. Stateless, idempotent.
See README.md.
"""
import argparse
import asyncio
import csv
import fcntl
import json
import logging
import logging.handlers
import math
import os
import re
import statistics
import sys
import time as time_module
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

APP_DIR = Path(__file__).resolve().parent
PAUSE_FILE = APP_DIR / "PAUSE"
AWAY_FILE = APP_DIR / "AWAY"
SEASON_FILE = APP_DIR / "SEASON"    # "heat" or "cool"; missing or unreadable = heat (the safe default)
HOLIDAY_FILE = APP_DIR / "HOLIDAY"  # weekend schedule every day (school holidays)
NORMAL_FILE = APP_DIR / "NORMAL"    # expiry date-time: "go to normal temperature now" is active
NUDGE_FILE = APP_DIR / "NUDGE"      # "+1.0 <until>": a temporary too-cold / too-hot correction
LOCK_FILE = APP_DIR / "run.lock"    # held for the length of a live run
RUN_SKIPPED = 75   # exit code of a run that waited (--lock-wait, the page's) but wrote nothing: the lock
                   # stayed taken, PAUSE, or the circuit breaker; cron (no wait) still exits 0
HOME_FILE = APP_DIR / "HOME"        # someone is home: weekend schedule and gentler price cuts
log = logging.getLogger("ilp_optimizer")
SLEEP = asyncio.sleep  # replaced in tests so backoff does not slow them down


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

LOCAL_CONFIG = "config.local.toml"   # an install's own settings, next to config.toml, not in git


def optional(cfg: dict, name: str) -> Optional[dict]:
    """An optional rule's section, or None when it is missing or set `enabled = false` (a local config
    can switch a default rule off that way: the merge cannot remove a section)."""
    sec = cfg.get(name)
    return sec if sec and sec.get("enabled", True) else None


def merged_config(defaults: dict, local: dict, where: str = "") -> dict:
    """`defaults` with `local` laid over it: a table merges key by key, anything else (a number, a
    string, a list) replaces the default whole. A table or key the defaults don't have is kept but
    logged, since it is most likely a typo that silently changes nothing; an empty default table
    (keyed by unit name, such as [units.offsets]) takes any key."""
    out = dict(defaults)
    for key, value in local.items():
        name = f"{where}.{key}" if where else key
        if key not in defaults:
            if defaults or not where:
                log.warning("%s: %s is not a known setting", LOCAL_CONFIG, name)
            out[key] = value
        elif isinstance(value, dict) and isinstance(defaults[key], dict):
            out[key] = merged_config(defaults[key], value, name)
        else:
            out[key] = value
    return out


def load_config(path: Path, local: Optional[Path] = None) -> dict:
    """The settings in `path` (the defaults), with `local` (an install's own config.local.toml, if it
    exists) merged over them; then the location from the environment."""
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    if local is not None and local.exists():
        with open(local, "rb") as f:
            cfg = merged_config(cfg, tomllib.load(f))
    # The house's location is kept out of the repository: WEATHER_LATITUDE and WEATHER_LONGITUDE
    # (set in .env) replace the example coordinates in [weather]. Both or neither.
    lat, lon = os.environ.get("WEATHER_LATITUDE"), os.environ.get("WEATHER_LONGITUDE")
    if (lat is None) != (lon is None):
        raise ValueError("set both WEATHER_LATITUDE and WEATHER_LONGITUDE, or neither")
    if lat is not None and "weather" in cfg:
        cfg["weather"]["latitude"] = float(lat)
        cfg["weather"]["longitude"] = float(lon)
    if "weather" in cfg and "latitude" not in cfg["weather"]:
        # No location: no weather and no sun, rather than someone else's (Open-Meteo needs a place).
        if cfg["weather"].get("enabled") or cfg.get("solar", {}).get("enabled"):
            log.warning("no location set (WEATHER_LATITUDE / WEATHER_LONGITUDE): weather and sun are off")
        cfg["weather"]["enabled"] = False
        cfg.setdefault("solar", {})["enabled"] = False
    return cfg


def parse_schedule(entries) -> List[Tuple[time, float]]:
    parsed = sorted(
        (time.fromisoformat(hhmm), float(temp)) for hhmm, temp in entries
    )
    if not parsed or parsed[0][0] != time(0, 0):
        raise ValueError("every schedule must start with a 00:00 entry")
    return parsed


# --------------------------------------------------------------------------
# Base schedule
# --------------------------------------------------------------------------

def base_setpoint(local_dt: datetime, cfg: dict, holiday: bool = False) -> float:
    """Base setpoint at a local time: the latest entry at or before it.

    On a holiday (no school) every day uses the weekend schedule.
    """
    key = "weekend" if holiday or local_dt.weekday() >= 5 else "weekday"
    entries = parse_schedule(cfg["schedule"][key])
    current = entries[0][1]
    for start, temp in entries:
        if start <= local_dt.time():
            current = temp
    return current


def clock(hhmm: str) -> time:
    """A local wall-clock time from config; "24:00" is the end of the day (a window up to midnight)."""
    return time.max if hhmm == "24:00" else time.fromisoformat(hhmm)


def keep_windows(local_dt: datetime, cfg: dict, holiday: bool = False, unit: Optional[str] = None) -> list:
    """The [above_average] keep_weekday / keep_weekend windows covering this local time for `unit`.
    A window with no `units` covers every unit; `unit` None (the page's plan) is covered by all."""
    aa = optional(cfg, "above_average") or {}
    windows = aa.get("keep_weekend" if holiday or local_dt.weekday() >= 5 else "keep_weekday", [])
    t = local_dt.time()
    return [w for w in windows if clock(w["from"]) <= t < clock(w["to"])
            and (unit is None or not w.get("units") or unit in w["units"])]


def comfort_cap(local_dt: datetime, price: Optional[float], cfg: dict,
                holiday: bool = False, unit: Optional[str] = None) -> Optional[float]:
    """Largest price cut (C) allowed at a local time, or None when no limit applies.

    Inside a comfort window ([comfort_windows]) the cut is limited by the interval's price: the
    cheaper the electricity, the less comfort is worth giving up. `tiers` is a list of
    (price below which, max cut); at or above the last tier there is no limit.
    """
    cw = optional(cfg, "comfort_windows")
    if not cw or price is None:
        return None
    key = "weekend" if holiday or local_dt.weekday() >= 5 else "weekday"
    t = local_dt.time()
    if not any(time.fromisoformat(a) <= t < time.fromisoformat(b) for a, b in cw.get(key, [])) \
            and not any(w.get("comfort") for w in keep_windows(local_dt, cfg, holiday, unit)):
        return None   # a keep window with `comfort = true` counts as a comfort window for its units
    for below, cap in cw["tiers"]:
        if price < below:
            return cap
    return None


COMFORT_PREHEAT_REASON = "pre-heat before comfort"


def comfort_preheat(now: datetime, prices: Optional[List["PriceInterval"]], cfg: dict,
                    holiday: bool = False) -> Optional[Tuple[float, str]]:
    """(target C, reason) when `now` is one of the cheapest quarters before the base schedule steps up
    to a warmer temperature, else None.

    The house is warmed to the coming temperature in the `quarters` cheapest quarter hours of the
    `lead_hours` before the step, so it is warm when the comfort time starts instead of heating up
    at its (dearer) prices. Skipped when the first `lead_hours` after the step cost less than
    (1 + min_ratio) x the average of those cheap quarters: then there is nothing to gain.
    """
    cp = optional(cfg, "comfort_preheat")
    if not cp or not prices:
        return None
    tz = ZoneInfo(cfg["timezone"])
    q = timedelta(minutes=15)
    lead = timedelta(hours=cp["lead_hours"])
    now_q = now.astimezone(timezone.utc)
    now_q = now_q.replace(minute=now_q.minute // 15 * 15, second=0, microsecond=0)
    base = lambda t: base_setpoint(t.astimezone(tz), cfg, holiday)
    step, t = None, now_q + q
    while t <= now_q + lead:                     # the next step up within the lead time
        if base(t) > base(t - q):
            step = t
            break
        t += q
    if step is None or base(step) <= base(now_q):
        return None
    # only steps up to the day's warmest temperature (the comfort time), not e.g. 19 -> 20 C
    key = "weekend" if holiday or step.astimezone(tz).weekday() >= 5 else "weekday"
    if base(step) < max(temp for _, temp in parse_schedule(cfg["schedule"][key])):
        return None
    before = [(price_at(prices, step - lead + k * q), step - lead + k * q) for k in range(int(lead / q))]
    after = [p for p in (price_at(prices, step + k * q) for k in range(int(lead / q))) if p is not None]
    if any(p is None for p, _ in before) or len(after) < len(before) // 2:
        return None
    cheap = sorted(before)[:cp["quarters"]]
    avg_cheap, avg_after = statistics.mean(p for p, _ in cheap), statistics.mean(after)
    if avg_after <= avg_cheap * (1 + cp["min_ratio"]) or now_q not in {t for _, t in cheap}:
        return None
    return base(step), (f"{COMFORT_PREHEAT_REASON} {step.astimezone(tz):%H:%M} "
                        f"({avg_cheap:.1f}c vs {avg_after:.1f}c)")


ABOVE_AVERAGE_REASON = "dearer than the day's average"


def above_average_kept(local_dt: datetime, cfg: dict, holiday: bool = False,
                       unit: Optional[str] = None) -> bool:
    """True when a keep window protects this local time for `unit` from the above-average cut:
    someone is usually home then."""
    return bool(keep_windows(local_dt, cfg, holiday, unit))


def above_average_cut(now: datetime, prices: Optional[List["PriceInterval"]], cfg: dict) -> Optional[Tuple[float, str]]:
    """(cut C, reason) when this quarter costs clearly more than the local day's average
    ([above_average]: more than (1 + min_ratio) x the average and at least min_above c over it),
    else None. Needs most of the day's prices, like the other day-ranked rules. desired_setpoint
    uses it outside the comfort time and the keep windows only."""
    aa = optional(cfg, "above_average")
    price = price_at(prices, now)
    if not aa or price is None:
        return None
    tz = ZoneInfo(cfg["timezone"])
    day_start, day_end = local_day_bounds(now.astimezone(tz).date(), tz)
    todays = [i for i in prices if day_start <= i.start < day_end]
    if sum(i.seconds for i in todays) / 3600 < cfg["prices"]["min_hours_available"]:
        return None
    avg = statistics.mean(i.price for i in todays)
    if price <= avg * (1 + aa["min_ratio"]) or price - avg < aa.get("min_above", 0.0):
        return None
    return aa["cut"], f"{ABOVE_AVERAGE_REASON} ({price:.1f}c vs {avg:.1f}c)"


NIGHT_SHIFT_REASON = "night shift"
RETURN_PREHEAT_REASON = "pre-heat before return"


def night_window(local: datetime, ns: dict, holiday: bool) -> Optional[Tuple[datetime, datetime]]:
    """The [night_shift] window (local start, end) of the night `local` falls in, else None."""
    start, end = ns["weekend" if holiday or local.weekday() >= 5 else "weekday"]
    if not clock(start) <= local.time() < clock(end):
        return None
    return (datetime.combine(local.date(), clock(start), tzinfo=local.tzinfo),
            datetime.combine(local.date(), clock(end), tzinfo=local.tzinfo))


def night_shift(now: datetime, prices: Optional[List["PriceInterval"]], cfg: dict,
                holiday: bool = False, unit: Optional[str] = None) -> Optional[Tuple[float, str]]:
    """(energy C, reason) for a unit that follows the price at night instead of being cut, else None.

    In the [night_shift] window, for the listed units (None, the page's plan, gets this gentler
    night too), a quarter clearly cheaper than the night's median price runs `shift` warmer whatever
    its price ("clearly": min_diff c/kWh and min_ratio of the median; the median, so one spike or
    peak does not make the rest of the night look cheap). Every other quarter drops to the floor its
    price allows: `tiers` is (price over which, cut C), none under the first tier. An hour with a cut
    and no boosted quarter boosts its cheapest quarter, so the rooms never sit at the floor for hours.
    """
    ns = optional(cfg, "night_shift")
    price = price_at(prices, now)
    if not ns or price is None or (unit is not None and unit not in ns.get("units", [])):
        return None
    tz = ZoneInfo(cfg["timezone"])
    local = now.astimezone(tz)
    bounds = night_window(local, ns, holiday)
    if bounds is None:
        return None
    w0, w1 = (b.astimezone(timezone.utc) for b in bounds)
    window = [i for i in prices if w0 <= i.start < w1]
    if sum(i.seconds for i in window) < 0.75 * (w1 - w0).total_seconds():
        return None   # too little of the night is priced to tell cheap from dear
    median = statistics.median(i.price for i in window)
    band = max(ns["min_diff"], ns["min_ratio"] * abs(median))

    def energy_at(p: float) -> float:
        if p <= median - band:
            return ns["shift"]
        # the floor for this price: real money buys a deeper dip, a few cents none
        return -max((cut for over, cut in ns["tiers"] if p > over), default=0.0) or 0.0   # not -0.0

    energy = energy_at(price)
    now_hour = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    # by UTC hour: the autumn clock change repeats a local hour (Helsinki's offset is whole hours)
    hour = [i for i in window if i.start.replace(minute=0, second=0, microsecond=0) == now_hour]
    energies = [energy_at(i.price) for i in hour]
    if hour and any(e < 0 for e in energies) and not any(e >= ns["shift"] for e in energies):
        cheapest = min(hour, key=lambda i: (i.price, i.start))
        if cheapest.start <= now.astimezone(timezone.utc) < cheapest.end:
            energy = ns["shift"]   # the hour's one boosted quarter
    return energy, f"{NIGHT_SHIFT_REASON} {energy:+.1f} ({price:.1f}c vs night median {median:.1f}c)"


NIGHT_COOLDOWN_REASON = "night cooldown"


def night_cooldown(local: datetime, base: float, cfg: dict, holiday: bool,
                   unit: Optional[str]) -> Optional[Tuple[float, str]]:
    """(cut C, reason) for the night shift units' stepped cooldown before the night, else None.

    `cooldown` in [night_shift] is (local time, cut C below the schedule) from that time until the
    night window starts (midnight): a house sitting well above the night temperature is eased down in
    steps, so it reaches the night temperature without a jump. Only when the schedule is at least
    `cooldown_min_gap` above the night's base temperature, for the night_shift units."""
    ns = optional(cfg, "night_shift")
    if not ns or not ns.get("cooldown") or (unit is not None and unit not in ns.get("units", [])):
        return None
    night_start = datetime.combine(local.date(), clock(ns["weekend" if holiday or local.weekday() >= 5 else "weekday"][0]),
                                   tzinfo=local.tzinfo)
    if base - base_setpoint(night_start, cfg, holiday) < ns.get("cooldown_min_gap", 0.0):
        return None
    cut = max((c for start, c in ns["cooldown"] if clock(start) <= local.time()), default=None)
    return None if cut is None else (cut, f"{NIGHT_COOLDOWN_REASON} -{cut:g}")


def night_max_temp(local: datetime, cfg: dict, holiday: bool, unit: Optional[str]) -> Optional[float]:
    """The warmest target a night shift unit may run while people sleep, from the first cooldown step to the end of
    the night (a comfort pre-heat before the morning is exempt), else None."""
    ns = optional(cfg, "night_shift")
    if not ns or "max_temp" not in ns or (unit is not None and unit not in ns.get("units", [])):
        return None
    first = clock(min((s for s, _ in ns.get("cooldown", [])), default="24:00"))
    return ns["max_temp"] if night_window(local, ns, holiday) or local.time() >= first else None


def price_at(prices: Optional[List["PriceInterval"]], now: datetime) -> Optional[float]:
    now_utc = now.astimezone(timezone.utc)
    for i in prices or []:
        if i.start <= now_utc < i.end:
            return i.price
    return None


# --------------------------------------------------------------------------
# Prices
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PriceInterval:
    start: datetime  # aware, UTC
    end: datetime
    price: float  # c/kWh

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


def parse_prices(payload: dict) -> List[PriceInterval]:
    def utc(s: str) -> datetime:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)

    def end_of(p: dict) -> datetime:
        # The 15-minute feed ends each interval at hh:14:59.999, not at the next start. Round
        # to the second so intervals are exactly 900 s and touch: the picker counts durations
        # (16 quarters = 4 h) and spike runs are found by end == next start.
        end = utc(p["endDate"])
        return (end + timedelta(milliseconds=500)).replace(microsecond=0)

    intervals = [
        PriceInterval(utc(p["startDate"]), end_of(p), float(p["price"]))
        for p in payload["prices"]
    ]
    return sorted(intervals, key=lambda i: i.start)


ENTSOE_NS = {"d": "urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3"}


def parse_entsoe(xml_text: str, vat_pct: float = 0.0, margin: float = 0.0) -> List[PriceInterval]:
    """Day-ahead prices from an ENTSO-E Transparency Platform document (documentType A44) as c/kWh:
    EUR/MWh / 10, plus `vat_pct` %, plus `margin` c/kWh. Each Period has a start, a resolution (PT15M,
    PT60M, ...) and numbered Points; a position left out (curve type A03) repeats the price before it
    up to the next given one, or to the Period's end. An interval given twice keeps its first price."""
    import xml.etree.ElementTree as ET

    root = ET.fromstring(xml_text)
    if root.tag.endswith("Acknowledgement_MarketDocument"):
        why = " ".join(t.text or "" for t in root.iter() if t.tag.endswith("text")).strip()
        raise ValueError(f"ENTSO-E sent no prices: {why or 'no reason given'}")

    def utc(text: str) -> datetime:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)

    currencies = {c.text for c in root.iterfind(".//d:TimeSeries/d:currency_Unit.name", ENTSOE_NS)} - {"EUR"}
    if currencies:
        # everything downstream (thresholds, the page, the report) is euro cents
        raise ValueError(f"ENTSO-E prices are in {', '.join(sorted(currencies))}, not EUR")
    seen: Dict[datetime, PriceInterval] = {}
    for period in root.iterfind(".//d:TimeSeries/d:Period", ENTSOE_NS):
        start = utc(period.findtext("d:timeInterval/d:start", namespaces=ENTSOE_NS))
        end = utc(period.findtext("d:timeInterval/d:end", namespaces=ENTSOE_NS))
        m = re.fullmatch(r"PT(\d+)M", period.findtext("d:resolution", namespaces=ENTSOE_NS) or "")
        if not m:
            raise ValueError(f"ENTSO-E resolution not understood: {period.findtext('d:resolution', namespaces=ENTSOE_NS)!r}")
        step = timedelta(minutes=int(m.group(1)))
        points = sorted((int(p.findtext("d:position", namespaces=ENTSOE_NS)),
                         float(p.findtext("d:price.amount", namespaces=ENTSOE_NS)))
                        for p in period.iterfind("d:Point", ENTSOE_NS))
        slots = round((end - start) / step)
        for k, (pos, eur_mwh) in enumerate(points):
            last = points[k + 1][0] - 1 if k + 1 < len(points) else slots
            price = eur_mwh / 10 * (1 + vat_pct / 100) + margin
            for n in range(pos, last + 1):
                t = start + (n - 1) * step
                seen.setdefault(t, PriceInterval(t, t + step, round(price, 3)))
    if not seen:
        raise ValueError("ENTSO-E document holds no prices")
    return sorted(seen.values(), key=lambda i: i.start)


PRICE_SOURCES = ("porssisahko", "entsoe")


def price_sources(cfg: dict) -> List[str]:
    """[prices] sources in priority order; an older config's single `source` still works."""
    pc = cfg["prices"]
    return list(pc.get("sources") or [pc.get("source", "porssisahko")])


async def fetch_prices(cfg: dict) -> List[PriceInterval]:
    """Today's and tomorrow's prices from the first of [prices] sources that gives any. Each source is
    retried (with_retry) before the next is tried; the last one's error is raised when all fail."""
    sources, error = price_sources(cfg), None
    for k, source in enumerate(sources):
        rest = f", trying {sources[k + 1]}" if k + 1 < len(sources) else ""
        try:
            prices = await with_retry(lambda source=source: fetch_from(cfg, source), f"price fetch ({source})", cfg)
        except Exception as exc:
            error = exc
            log.warning("price source %s failed (%s: %s)%s", source, type(exc).__name__, exc, rest)
            continue
        if prices:
            if k:
                log.info("prices from %s, the fallback", source)
            return prices
        log.warning("price source %s gave no prices%s", source, rest)
    raise error or ValueError(f"no price source gave prices: {', '.join(sources)}")


async def fetch_from(cfg: dict, source: str) -> List[PriceInterval]:
    """Today's and tomorrow's prices from one source: "porssisahko" (Finland, no key) or "entsoe" (any
    European bidding zone; ENTSOE_TOKEN in .env)."""
    import aiohttp

    pc = cfg["prices"]
    timeout = aiohttp.ClientTimeout(total=pc["timeout_seconds"])
    async with aiohttp.ClientSession(timeout=timeout) as session:
        if source == "porssisahko":
            async with session.get(pc["url"]) as resp:
                resp.raise_for_status()
                return parse_prices(await resp.json(content_type=None))
        if source == "entsoe":
            es = pc["entsoe"]
            token = os.environ.get("ENTSOE_TOKEN")
            if not token:
                raise ValueError("price source \"entsoe\" needs ENTSOE_TOKEN in the environment (.env, and web.env for the page)")
            tz = ZoneInfo(cfg["timezone"])
            day_start, _ = local_day_bounds(datetime.now(tz).date(), tz)
            params = {"securityToken": token, "documentType": "A44", "in_Domain": es["zone"],
                      "out_Domain": es["zone"], "periodStart": f"{day_start:%Y%m%d%H%M}",
                      "periodEnd": f"{day_start + timedelta(days=2):%Y%m%d%H%M}"}
            # The token is in the URL, and aiohttp's errors can quote the URL: report only the error
            # type and ENTSO-E's own answer. ConnectionError (an OSError) is retried, ValueError is not.
            try:
                async with session.get(es["url"], params=params) as resp:
                    status, text = resp.status, await resp.text()
            except aiohttp.ClientError as exc:
                raise ConnectionError(f"ENTSO-E request failed ({type(exc).__name__})") from None
            if status >= 500 or status == 429:
                raise ConnectionError(f"ENTSO-E answered {status}")
            if status >= 400:
                raise ValueError(f"ENTSO-E answered {status}: {text[:200]!r}")
            return parse_entsoe(text, es.get("vat", 0.0), es.get("margin", 0.0))
    raise ValueError(f"price source {source!r} is not known: {' or '.join(PRICE_SOURCES)}")


def load_prices_file(path: Path) -> List[PriceInterval]:
    with open(path, encoding="utf-8") as f:
        return parse_prices(json.load(f))


def local_day_bounds(day: date, tz: ZoneInfo) -> Tuple[datetime, datetime]:
    """UTC bounds of a local calendar day (23, 24 or 25 hours long)."""
    start = datetime.combine(day, time(0, 0), tzinfo=tz).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=tz).astimezone(
        timezone.utc
    )
    return start, end


@dataclass(frozen=True)
class PriceShift:
    interval: PriceInterval
    offset: float
    reason: str
    extreme: bool = False  # may go beyond the normal temperature limits
    scalable: bool = False  # a normal shift: scaled by the weather factor at the time it applies
    rule: str = "none"      # which price rule made it: cheap, expensive, extreme, peak, peak_preheat, preheat, spike, dear_quarter
    cheapest_kept: bool = False  # the cheapest quarter of a dear hour: comfort hours keep its full target


def profile(cfg: dict, mode: str) -> dict:
    """The rule sections for a mode: top-level for heating, [cool.*] for cooling."""
    src = cfg if mode == "heat" else cfg["cool"]
    return {k: src[k] for k in ("limits", "prices", "extreme", "weather", "comfort")}


@dataclass(frozen=True)
class OutdoorForecast:
    """Hourly outdoor weather from Open-Meteo per hour start (UTC): (temperature C, humidity %,
    wind m/s, WMO weather code, is_day 0/1). Temperature, humidity and wind are interpolated linearly
    to any moment; the weather code and day/night flag are those of the hour containing it. Hours
    with no data are skipped."""
    hours: Dict[datetime, tuple]

    def _at(self, t: datetime, idx: int) -> Optional[float]:
        t = t.astimezone(timezone.utc)
        h0 = t.replace(minute=0, second=0, microsecond=0)
        a, b = self.hours.get(h0), self.hours.get(h0 + timedelta(hours=1))
        va, vb = (a[idx] if a else None), (b[idx] if b else None)
        if va is None or vb is None:
            return va if vb is None else vb
        return va + (vb - va) * (t - h0).total_seconds() / 3600

    def at(self, t: datetime) -> Optional[float]:
        """Outdoor temperature (C) at `t`, None when the forecast has nothing near it."""
        return self._at(t, 0)

    def code_at(self, t: datetime) -> Tuple[Optional[int], Optional[int]]:
        """(WMO weather code, is_day) of the hour containing `t`."""
        v = self.hours.get(t.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0))
        if not v or len(v) < 5:
            return None, None
        return v[3], v[4]

    def sample(self, t: datetime) -> Dict[str, Optional[float]]:
        code, _ = self.code_at(t)
        return {"temp": self._at(t, 0), "humidity": self._at(t, 1), "wind": self._at(t, 2), "code": code}

    def day_mean(self, day: date, tz: ZoneInfo) -> Optional[float]:
        temps = [v[0] for h, v in self.hours.items() if h.astimezone(tz).date() == day]
        return sum(temps) / len(temps) if temps else None


def parse_outdoor(payload: dict, tz: ZoneInfo) -> OutdoorForecast:
    hourly = payload["hourly"]
    n = len(hourly["time"])

    def column(name):
        return hourly.get(name) or [None] * n

    hours = {}
    for stamp, temp, rh, wind, code, day in zip(hourly["time"], hourly["temperature_2m"],
                                                column("relative_humidity_2m"), column("wind_speed_10m"),
                                                column("weather_code"), column("is_day")):
        if temp is not None:
            when = datetime.fromisoformat(stamp).replace(tzinfo=tz).astimezone(timezone.utc)
            hours[when] = (float(temp), None if rh is None else float(rh), None if wind is None else float(wind),
                           None if code is None else int(code), None if day is None else int(day))
    return OutdoorForecast(hours)


def outdoor_temp_at(outdoor, t: datetime) -> Optional[float]:
    """The outdoor temperature at `t` from a forecast, or the plain number given (a daily mean or
    a --outdoor-temp override); None when unknown."""
    return outdoor.at(t) if isinstance(outdoor, OutdoorForecast) else outdoor


def outdoor_day_mean(outdoor, now: datetime, tz: ZoneInfo) -> Optional[float]:
    """One number for the day of `now` (what the history file and the savings report use)."""
    if isinstance(outdoor, OutdoorForecast):
        return outdoor.day_mean(now.astimezone(tz).date(), tz)
    return outdoor


def factor_for(outdoor, cfg: dict, mode: str = "heat"):
    """The weather factor for price_shift_at: one number for a plain temperature, or a function of the
    time for an hourly forecast."""
    if isinstance(outdoor, OutdoorForecast):
        return lambda t: weather_factor(outdoor.at(t), cfg, mode)
    return weather_factor(outdoor, cfg, mode)


def weather_factor(outdoor_mean: Optional[float], cfg: dict, mode: str = "heat") -> float:
    """Scale for the normal price shifts: linear between the configured points."""
    weather = profile(cfg, mode)["weather"]
    if outdoor_mean is None or not weather["enabled"]:
        return 1.0
    points = sorted((float(t), float(f)) for t, f in weather["scale"])
    if outdoor_mean <= points[0][0]:
        return points[0][1]
    for (t0, f0), (t1, f1) in zip(points, points[1:]):
        if outdoor_mean <= t1:
            return f0 + (f1 - f0) * (outdoor_mean - t0) / (t1 - t0)
    return points[-1][1]


PEAK_REASON = "peak price"
SPIKE_REASON = "spike"


def peak_shifts(prices: List[PriceInterval], day_start: datetime, day_end: datetime,
                x: dict) -> Dict[datetime, Tuple[int, "PriceShift"]]:
    """The peak protector, for the intervals of one day: {interval start: (priority, shift)}.

    Every interval at or above peak_price gets the extreme cut, however the rest of the day
    looks (the normal extreme rules only cut the priciest cut_hours). The peak_preheat_hours
    before the first interval of each peak run are pre-heated, in intervals up to
    peak_preheat_max_price, even where the extreme cut would otherwise hold them down. A peak
    just after midnight pre-heats the evening before, because the window is read from all
    prices. Heating only: the [cool.extreme] section has no peak_price, so cooling is unchanged.
    """
    peak_price = x.get("peak_price")
    if peak_price is None:
        return {}
    margin = timedelta(hours=x["peak_preheat_hours"])
    near = sorted((i for i in prices if day_start - timedelta(hours=1) <= i.start < day_end + margin),
                  key=lambda i: i.start)
    is_peak = lambda i: i.price >= peak_price
    out: Dict[datetime, Tuple[int, PriceShift]] = {}
    for i in near:
        if is_peak(i) and day_start <= i.start < day_end:
            out[i.start] = (3, PriceShift(i, -x["cut"], f"{PEAK_REASON} {i.price:.1f}c", True, rule="peak"))
    peak_ends = {i.end for i in near if is_peak(i)}
    for first in (i for i in near if is_peak(i) and i.start not in peak_ends):
        window_start = first.start - margin
        for i in near:
            if (window_start <= i.start and i.end <= first.start and not is_peak(i)
                    and day_start <= i.start < day_end and i.price <= x["peak_preheat_max_price"]):
                cur = out.get(i.start)
                if cur is None or abs(x["peak_preheat_boost"]) > abs(cur[1].offset):
                    out[i.start] = (3, PriceShift(i, x["peak_preheat_boost"],
                                                  f"pre-heat before peak {first.price:.1f}c", True,
                                                  rule="peak_preheat"))
    return out


def next_day_spike_shifts(prices: List[PriceInterval], day: date, cfg: dict, p: dict, x: dict,
                          todays: List[PriceInterval]) -> Dict[datetime, Tuple[int, "PriceShift"]]:
    """Pre-heat for a spike in the first hours of the NEXT day: {interval start: (priority, shift)}
    for the last `preheat_hours` of this one. What counts as a spike is read against that day's own
    median, so the next day needs enough data of its own (tomorrow's prices arrive about 14:00).
    Independent of this day's spread, like the peak protector. Nothing else crosses midnight: the
    cheap/expensive ranking stays per calendar day, so the plan stays stable."""
    tz = ZoneInfo(cfg["timezone"])
    day_end = local_day_bounds(day, tz)[1]
    next_start, next_end = local_day_bounds(day + timedelta(days=1), tz)
    nexts = sorted((i for i in prices if next_start <= i.start < next_end), key=lambda i: i.start)
    if not nexts or sum(i.seconds for i in nexts) / 3600 < p["min_hours_available"]:
        return {}
    median = statistics.median(i.price for i in nexts)
    is_spike = lambda i: i.price >= x["spike_ratio"] * median and i.price >= x["spike_min_price"]
    spike_ends = {i.end for i in nexts if is_spike(i)}
    reach = day_end + timedelta(hours=x["preheat_hours"])
    out: Dict[datetime, Tuple[int, PriceShift]] = {}
    for spike in (i for i in nexts if is_spike(i) and i.start not in spike_ends and i.start < reach):
        window_start = spike.start - timedelta(hours=x["preheat_hours"])
        for i in todays:
            if window_start <= i.start:
                out[i.start] = (1, PriceShift(i, x["preheat_boost"],
                                              f"pre-heat before {spike.price:.1f}c spike", True, rule="preheat"))
    return out


DEAR_QUARTER_REASON = "dear quarter"
CHEAPEST_KEPT = "cheapest of its hour"


def quarter_shaping(todays: List[PriceInterval], best: dict, cfg: dict) -> None:
    """Short cuts at quarter-hour resolution, applied over the day's normal (priority 0) shifts in `best`.

    A 15-minute dip is not felt (the walls and furniture hold the heat); an hour-long one is. So:
    - spikes ([spikes]): a quarter at least min_jump c and min_ratio above the median of the
      `neighbours` quarters on each side gets a `cut`, whatever its absolute price;
    - dear hours ([hour_shaping]): in every clock hour dearer than the day's average whose quarters
      differ by at least min_spread c and min_ratio of the hour's mean, the dearest quarter gets a
      `cut` and the cheapest is marked (CHEAPEST_KEPT) so a cut there is dropped in comfort hours.
    These replace a weaker normal shift (a boost, no change, or a smaller cut), never a pre-heat,
    extreme or peak shift. desired_setpoint lets them through the comfort-window price tiers.
    """
    def undercut(i, cut, reason, rule):
        cur = best.get(i.start)
        if cur is None or (cur[0] == 0 and cur[1].offset > -cut):
            best[i.start] = (0, PriceShift(i, -cut, reason, rule=rule))

    sp = optional(cfg, "spikes")
    if sp:
        for k, i in enumerate(todays):
            around = [todays[j].price for j in range(max(0, k - sp["neighbours"]),
                                                      min(len(todays), k + sp["neighbours"] + 1)) if j != k]
            m = statistics.median(around) if around else None
            if m is not None and i.price - m >= max(sp["min_jump"], sp["min_ratio"] * m):
                undercut(i, sp["cut"], f"{SPIKE_REASON} {i.price:.1f}c over its neighbours {m:.1f}c", "spike")
    hs = optional(cfg, "hour_shaping")
    if hs and todays:
        day_avg = statistics.mean(i.price for i in todays)
        hours: Dict[datetime, List[PriceInterval]] = {}
        for i in todays:   # local hours start on UTC hours in whole-hour zones (all of Europe), also across DST
            hours.setdefault(i.start.replace(minute=0, second=0, microsecond=0), []).append(i)
        for group in hours.values():
            prices = [i.price for i in group]
            mean_h, spread = statistics.mean(prices), max(prices) - min(prices)
            if len(group) < 2 or mean_h <= day_avg or spread < max(hs["min_spread"], hs["min_ratio"] * mean_h):
                continue
            dear = max(group, key=lambda i: (i.price, -i.start.timestamp()))
            cheap = min(group, key=lambda i: (i.price, i.start.timestamp()))
            undercut(dear, hs["cut"], f"{DEAR_QUARTER_REASON} {dear.price:.1f}c in a {mean_h:.1f}c hour "
                                      f"(day {day_avg:.1f}c, spread {spread:.1f}c)", "dear_quarter")
            cur = best.get(cheap.start)
            if cur and cur[0] == 0 and cur[1].offset < 0:
                sh = cur[1]
                best[cheap.start] = (0, PriceShift(sh.interval, sh.offset, f"{sh.reason}, {CHEAPEST_KEPT}",
                                                   sh.extreme, sh.scalable, sh.rule, True))


def plan_day_shifts(
    prices: Optional[List[PriceInterval]],
    day: date,
    cfg: dict,
    factor: float = 1.0,
    mode: str = "heat",
) -> List[PriceShift]:
    """Price offsets for the intervals of one local day; [] means no shifting.

    Offsets are in the energy direction: positive uses more energy (heat up, or cool
    down), negative uses less. desired_setpoint turns them into setpoint changes.

    Ranking is per calendar day so the plan is stable through the day.
    `factor` (weather) scales the normal boost/cut only; extreme rules are
    driven by price alone.
    """
    if not prices:
        return []
    rules = profile(cfg, mode)
    p, x = rules["prices"], rules["extreme"]
    day_start, day_end = local_day_bounds(day, ZoneInfo(cfg["timezone"]))
    todays = sorted((i for i in prices if day_start <= i.start < day_end),
                    key=lambda i: i.start)
    # Peak protector: independent of the rest of the day (no hour cap, no spread or data checks).
    peaks = peak_shifts(prices, day_start, day_end, x)
    carried = next_day_spike_shifts(prices, day, cfg, p, x, todays)   # tomorrow's early spike
    for start, entry in peaks.items():                                # a peak outranks a pre-heat
        carried[start] = max(carried.get(start, entry), entry, key=lambda e: (e[0], abs(e[1].offset)))
    peaks = carried
    hours = sum(i.seconds for i in todays) / 3600
    if hours < p["min_hours_available"]:
        return sorted((sh for _, sh in peaks.values()), key=lambda sh: sh.interval.start)
    spread = max(i.price for i in todays) - min(i.price for i in todays)
    flat_day = spread < p["min_spread"]
    if flat_day and (mode != "heat" or not p.get("flat_day_boost")):
        # Too even a day for the normal shifts, but quarter-hour spikes and dear quarters still count.
        flat = dict(peaks)
        if mode == "heat":
            quarter_shaping(todays, flat, cfg)
        return sorted((sh for _, sh in flat.values()), key=lambda sh: sh.interval.start)

    def pick(candidates, hours_wanted):
        """Take intervals in order until the wanted duration is covered."""
        chosen, covered = [], 0.0
        for i in candidates:
            if covered >= hours_wanted * 3600:
                break
            chosen.append(i)
            covered += i.seconds
        return chosen

    # One shift per interval. Priority: peak > extreme cut > pre-heat > normal shift,
    # so we never pre-heat into an extremely expensive hour (a peak pre-heat is the exception:
    # it may use hours up to peak_preheat_max_price that the extreme cut would otherwise take).
    best = dict(peaks)

    def offer(priority: int, shift: PriceShift) -> None:
        cur = best.get(shift.interval.start)
        if cur is None or (priority, abs(shift.offset)) > (cur[0], abs(cur[1].offset)):
            best[shift.interval.start] = (priority, shift)

    scale = f" x{factor:.2f}" if abs(factor - 1.0) > 1e-9 else ""
    # Ties broken by time so the result is deterministic.
    cheapest = pick(sorted(todays, key=lambda i: (i.price, i.start)), p["cheap_hours"])
    cheap_starts = {i.start for i in cheapest}
    rest = [i for i in todays if i.start not in cheap_starts]
    priciest = pick(sorted(rest, key=lambda i: (-i.price, i.start)), p["expensive_hours"])
    for i in cheapest:
        if i.price <= p["cheap_max"]:
            offer(0, PriceShift(i, p["boost"] * factor, f"cheap {i.price:.1f}c{scale}", scalable=True, rule="cheap"))
    if p.get("cheap_all"):
        # Every interval at or below cheap_max and below the day's average is worth pre-heating, not
        # only the day's cheapest hours: the ranking can put them all in the evening and leave a 4c
        # night cold. Below the average only, so a cheap day does not heat the empty house all day.
        day_avg = statistics.mean(i.price for i in todays)
        for i in todays:
            if i.price <= p["cheap_max"] and i.price < day_avg and i.start not in cheap_starts:
                offer(0, PriceShift(i, p.get("cheap_all_boost", p["boost"]) * factor,
                                    f"cheap {i.price:.1f}c{scale}", scalable=True, rule="cheap"))
    if flat_day:
        # Too even a day for the cuts, but nearly free power is still worth a boost (the PILP
        # recycles part of it), and quarter-hour spikes and dear quarters still count.
        quarter_shaping(todays, best, cfg)
        return sorted((shift for _, shift in best.values()), key=lambda sh: sh.interval.start)
    for i in priciest:
        if i.price >= p["very_expensive"]:
            offer(0, PriceShift(i, -p["cut_deep"] * factor, f"very expensive {i.price:.1f}c{scale}",
                                scalable=True, rule="expensive"))
        elif i.price >= p["expensive_min"]:
            offer(0, PriceShift(i, -p["cut"] * factor, f"expensive {i.price:.1f}c{scale}", scalable=True,
                                rule="expensive"))

    # Extreme: the priciest `cut_hours` at or above cut_price are cut hard. On a day
    # that is expensive all day the other such hours get the normal cut instead, so
    # the house is not held cold for the whole day.
    over = sorted((i for i in todays if i.price >= x["cut_price"]),
                  key=lambda i: (-i.price, i.start))
    extreme_cut = pick(over, x["cut_hours"])
    extreme_starts = {i.start for i in extreme_cut}
    for i in extreme_cut:
        offer(2, PriceShift(i, -x["cut"], f"extreme price {i.price:.1f}c", True, rule="extreme"))
    for i in over:
        if i.start not in extreme_starts:
            deep = i.price >= p["very_expensive"]
            offer(0, PriceShift(i, -(p["cut_deep"] if deep else p["cut"]) * factor,
                                f"high price {i.price:.1f}c{scale}", scalable=True, rule="expensive"))

    # Extreme: pre-heat before a spike (>= spike_ratio x the day's median).
    median = statistics.median(i.price for i in todays)
    is_spike = lambda i: i.price >= x["spike_ratio"] * median and i.price >= x["spike_min_price"]
    spike_ends = {i.end for i in todays if is_spike(i)}
    for spike in (i for i in todays if is_spike(i) and i.start not in spike_ends):
        window_start = spike.start - timedelta(hours=x["preheat_hours"])
        for i in todays:
            if window_start <= i.start and i.end <= spike.start and not is_spike(i):
                offer(1, PriceShift(i, x["preheat_boost"],
                                    f"pre-heat before {spike.price:.1f}c spike", True, rule="preheat"))
    if mode == "heat":
        quarter_shaping(todays, best, cfg)
    return sorted((shift for _, shift in best.values()), key=lambda sh: sh.interval.start)


_DAY_SHIFTS: "OrderedDict[tuple, tuple]" = OrderedDict()


def day_shifts(prices: List[PriceInterval], day: date, cfg: dict, factor: float, mode: str) -> List["PriceShift"]:
    """plan_day_shifts once per day: every quarter of a plan or a run asks for the same day's ranking.
    Keyed on the identity of the price list and config (neither is changed in place); each entry keeps
    those objects alive, so their ids cannot be reused by a new run or feed while it is cached."""
    key = (id(prices), id(cfg), day, factor, mode)
    hit = _DAY_SHIFTS.get(key)
    if hit is not None:
        _DAY_SHIFTS.move_to_end(key)
        return hit[2]
    shifts = plan_day_shifts(prices, day, cfg, factor, mode)
    _DAY_SHIFTS[key] = (prices, cfg, shifts)
    if len(_DAY_SHIFTS) > 16:
        _DAY_SHIFTS.popitem(last=False)
    return shifts


PREHEAT_RULES = {"preheat", "peak_preheat"}   # price rules that bank heat before a spike or a peak
DIP_RULES = {"spike", "dear_quarter"}          # one-quarter dips


def price_shift_at(
    now: datetime,
    prices: Optional[List[PriceInterval]],
    cfg: dict,
    factor=1.0,
    mode: str = "heat",
) -> Tuple[float, str, bool, str, bool]:
    """(offset, reason, extreme, rule, cheapest_kept) at `now`; offset 0 when no shifting applies.

    `factor` is the weather scale for the normal shifts: a number for the whole day, or a function
    of the time (the hourly forecast), applied at `now` to the shift that is active then."""
    tz = ZoneInfo(cfg["timezone"])
    if not prices:
        return 0.0, "no prices", False, "none", False
    timed = callable(factor)
    shifts = day_shifts(prices, now.astimezone(tz).date(), cfg, 1.0 if timed else factor, mode)
    if not shifts:
        return 0.0, "no shift (short data or small spread)", False, "none", False
    now_utc = now.astimezone(timezone.utc)
    for sh in shifts:
        if sh.interval.start <= now_utc < sh.interval.end:
            if timed and sh.scalable:
                f = factor(now)
                return (sh.offset * f, sh.reason + (f" x{f:.2f}" if abs(f - 1.0) > 1e-9 else ""), sh.extreme,
                        sh.rule, sh.cheapest_kept)
            return sh.offset, sh.reason, sh.extreme, sh.rule, sh.cheapest_kept
    return 0.0, "mid-priced hour", False, "none", False


# --------------------------------------------------------------------------
# Setpoint
# --------------------------------------------------------------------------

def round_to_step(value: float, step: float) -> float:
    # floor(x + 0.5) rounds halves up, unlike round() which rounds to even.
    return math.floor(value / step + 0.5) * step


def clamp_and_round(value: float, cfg: dict, extreme: bool = False, mode: str = "heat") -> float:
    lim = profile(cfg, mode)["limits"]
    lo, hi = (lim["extreme_min_temp"], lim["extreme_max_temp"]) if extreme \
        else (lim["min_temp"], lim["max_temp"])
    return round_to_step(min(max(value, lo), hi), lim["step"])


def configured_unit_names(cfg: dict) -> set:
    """Every unit name the settings mention: [units] names, outdoor_groups and the per-unit tables,
    the night shift's units (when on) and the keep windows' units."""
    u = cfg["units"]
    names = set(u.get("names", [])) | {n for g in u.get("outdoor_groups", []) for n in g}
    for table in ("offsets", "cool_offsets", "solar_weight"):
        names |= set(u.get(table, {}))
    names |= set((optional(cfg, "night_shift") or {}).get("units", []))
    aa = optional(cfg, "above_average") or {}
    names |= {n for key in ("keep_weekday", "keep_weekend") for w in aa.get(key, []) for n in w.get("units", [])}
    return names


def outdoor_groups(cfg: dict, names: List[str]) -> List[set]:
    """The units among `names` that share an outdoor unit ([units] outdoor_groups, a multi-split), as
    sets; every other unit has its own outdoor unit and is a group of one."""
    out, seen = [], set()
    for group in cfg["units"].get("outdoor_groups", []):
        members = {n for n in group if n in names}
        if members:
            out.append(members)
            seen |= members
    return out + [{n} for n in names if n not in seen]


def coasted_gap(cfg: dict, target: float, floor: float, empty_for: timedelta) -> float:
    """How far under `target` the rooms are after coasting down toward `floor` for `empty_for`, at
    [warmup] cooling_rate: the units lift the air quickly, but an empty house cools slowly."""
    return min(target - floor, cfg["warmup"]["cooling_rate"] * empty_for.total_seconds() / 3600)


def warmup_lead(cfg: dict, gap: float, longest: timedelta) -> timedelta:
    """How long a warm-up of `gap` C takes at [warmup] warmup_rate: whole quarters, at least one, at most
    `longest`."""
    quarter = timedelta(minutes=15)
    need = timedelta(hours=max(gap, 0.0) / cfg["warmup"]["warmup_rate"])
    return min(longest, max(1, math.ceil(need / quarter)) * quarter)


@dataclass(frozen=True)
class Away:
    """Everyone out (AWAY, the page's Away): the [nobody_home] floors, optionally until a
    return time, with a warm-up before it ([away])."""
    until: Optional[datetime] = None  # aware; None = until the AWAY file is removed
    since: Optional[datetime] = None  # when it was set (the AWAY file records it): sizes the warm-up

    def lead(self, cfg: dict, target: Optional[float] = None, cooling: bool = False) -> timedelta:
        """How long the warm-up before `until` heats: the time the units need to lift the rooms to
        `target` (the plan at the return; the normal maximum when not given) at warmup_rate, from how far
        the rooms coasted down in the absence (cooling_rate per hour, not below the away temperature).
        Whole quarters, at least one, at most preheat_hours; preheat_hours when the absence's start is
        not known, and always while cooling (the units come back on before the return: no measured rate
        for that). Not from the room reading: the quarters it picks must not swing from run to run."""
        longest = timedelta(hours=cfg["away"]["preheat_hours"])
        wu = cfg.get("warmup")
        if not wu or self.until is None or cooling:
            return longest
        if self.since is None:
            return longest
        aim = target if target is not None else cfg["limits"]["max_temp"]
        return warmup_lead(cfg, coasted_gap(cfg, aim, cfg["nobody_home"]["temp"], self.until - self.since), longest)

    def active_at(self, now: datetime, cfg: dict, target: Optional[float] = None,
                  cooling: bool = False) -> bool:
        """Away holds, except in the warm-up before `until` (see lead): the cooling units' switch-on and
        the run's log line. (Heating holds until the warm-up's first bought quarter: desired_setpoint.)"""
        if self.until is None:
            return True
        return now < self.until - self.lead(cfg, target, cooling)


def read_until_file(path: Path, label: str, tz: ZoneInfo) -> Tuple[bool, Optional[datetime]]:
    """(present, until) for an AWAY or HOLIDAY style file.

    Empty file: present with no end. ISO date-time: present until then. Anything
    else is ignored with an error, so a typo can never leave a switch stuck on.
    """
    if not path.exists():
        return False, None
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return True, None
    try:
        until_text, since_text = until_and_since(text)
        until = datetime.fromisoformat(until_text)
        if since_text is not None:
            datetime.fromisoformat(since_text)   # a second word must be the set time too, or the file is refused
    except ValueError:
        log.error("%s file does not hold an ISO date-time, ignoring it: %r", label, text[:40])
        return False, None
    return True, until if until.tzinfo else until.replace(tzinfo=tz)


def load_away(path: Path, tz: ZoneInfo) -> Optional[Away]:
    """AWAY file: empty = away until removed; or an ISO date-time = return time (and, after it, when it
    was set)."""
    present, until = read_until_file(path, "AWAY", tz)
    return Away(until, set_time(path, "AWAY", tz)) if present else None


def until_and_since(text: str) -> Tuple[str, Optional[str]]:
    """Split an AWAY file's text into (end time, set time): the set time follows the end time
    as a second date-time word. A single date-time, also one written with a space ('2026-11-20 18:00'),
    is all end time."""
    words = text.split()
    if len(words) == 2:
        try:
            datetime.fromisoformat(text)
        except ValueError:
            return words[0], words[1]   # two date-times (or a mistake, which the parse of either refuses)
    return text, None


def set_time(path: Path, label: str, tz: ZoneInfo) -> Optional[datetime]:
    """When an AWAY file was set (it sizes the warm-up), or None."""
    since_text = until_and_since(path.read_text(encoding="utf-8").strip())[1]
    if since_text is None:
        return None
    try:
        since = datetime.fromisoformat(since_text)
    except ValueError:
        log.error("%s file: the second word is not the time it was set, ignoring it: %r", label, since_text[:30])
        return None
    return since if since.tzinfo else since.replace(tzinfo=tz)


def remove_if_over(path: Path, label: str, tz: ZoneInfo) -> None:
    """An AWAY file whose return time has passed by the real clock (not a --now test) is
    over: remove it, so the page shows it off. Read right before the removal, so a file written with a
    new return time in the meantime stays."""
    present, until = read_until_file(path, label, tz)
    if present and until is not None and datetime.now(tz) >= until:
        path.unlink(missing_ok=True)
        log.info("%s over (return %s), file removed", label, until.astimezone(tz).isoformat(timespec="minutes"))


def holiday_active(path: Path, tz: ZoneInfo, now: datetime) -> bool:
    present, until = read_until_file(path, "HOLIDAY", tz)
    return present and (until is None or now < until)


def set_home(value: str, tz: ZoneInfo, now: Optional[datetime] = None) -> int:
    """The --home command: write or remove the HOME file. Returns an exit code."""
    now = (now or datetime.now(tz)).astimezone(tz)
    if value == "off":
        HOME_FILE.unlink(missing_ok=True)
        print("HOME removed")
        return 0
    midnight = datetime.combine(now.date(), time(0, 0), tzinfo=tz)
    if value == "today":
        until = midnight + timedelta(days=1)
    elif value == "week":  # until the end of Sunday
        until = midnight + timedelta(days=7 - now.weekday())
    else:
        try:
            until = datetime.fromisoformat(value)
        except ValueError:
            print(f"--home takes today, week, off or an ISO date-time, not {value!r}")
            return 2
        until = until if until.tzinfo else until.replace(tzinfo=tz)
    HOME_FILE.write_text(until.isoformat(timespec="minutes") + "\n", encoding="utf-8")
    print(f"HOME until {until:%a %d.%m. %H:%M}: weekend schedule and gentler price cuts")
    return 0


CONTROLS = ("away", "holiday", "pause", "nudge", "normal")  # NAME -> the NAME_FILE constant, looked up when used


@dataclass(frozen=True)
class Nudge:
    """The too-cold / too-hot buttons. `floor` lifts a quarter the plan cuts by up to that much, never
    above what the plan would hold without the cut ("too cold" while saving: ease the saving, do not
    heat more later). `shift` moves every quarter: above the plan (warmer) or below it (cooler)."""
    floor: float = 0.0
    shift: float = 0.0
    until: Optional[datetime] = None

    def __bool__(self) -> bool:
        return bool(self.floor or self.shift)


def read_nudge(path: Path, tz: ZoneInfo, now: datetime) -> Nudge:
    """NUDGE file: '<shift> <until> floor=<floor>', e.g. '+0.0 2026-11-12T20:30+02:00 floor=1.0' (an
    older file without floor= is a shift). Missing, expired or unreadable means no nudge (a typo must
    not leave the house off-plan)."""
    try:
        text = path.read_text(encoding="utf-8").split()
    except FileNotFoundError:
        return Nudge()
    try:
        shift, until = float(text[0]), datetime.fromisoformat(text[1])
        until = until if until.tzinfo else until.replace(tzinfo=tz)
        floor = float(text[2].removeprefix("floor=")) if len(text) > 2 else 0.0
    except (ValueError, IndexError):
        log.error("NUDGE file is not '<degrees> <date-time> floor=<degrees>', ignoring it: %r", " ".join(text)[:40])
        return Nudge()
    return Nudge(floor, shift, until) if now < until else Nudge()


def current_cut(cfg: dict, now: datetime) -> Optional[float]:
    """How far (C) the plan is below its uncut target right now, for the unit cut deepest: read from
    each unit's latest history row (the last run's base and price offset, before any nudge) when it is
    at most 20 minutes old. None when there is no fresh row (the run has not happened) or in cooling."""
    tz = ZoneInfo(cfg["timezone"])
    if read_season(SEASON_FILE) == "cool":
        return None
    latest: Dict[str, dict] = {}
    try:
        with open(monthly_path(APP_DIR / cfg["history_file"], now, tz), newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("base") and row.get("offset"):
                    latest[row["unit"]] = row
    except FileNotFoundError:
        return None
    offsets = cfg["units"].get("offsets", {})
    gaps = []
    for name, row in latest.items():
        try:
            when = datetime.fromisoformat(row["time_local"])
            base, offset = float(row["base"]), float(row["offset"])
        except ValueError:
            continue
        if not timedelta(0) <= now - when <= timedelta(minutes=20):
            continue
        planned = clamp_and_round(base + offset + offsets.get(name, 0.0), cfg, extreme=True)
        if "night floor" in row.get("reason", ""):
            planned = max(planned, (optional(cfg, "night_shift") or {}).get("others_floor", planned))
        uncut = clamp_and_round(base + max(offset, 0.0) + offsets.get(name, 0.0), cfg)
        if row.get("reason", "").startswith("away"):
            # away's floor is itself the cut; the row does not say how far the plan is above it,
            # so take the normal maximum: the floor stops at the plan anyway (apply_nudge)
            uncut = cfg["limits"]["max_temp"]
        gaps.append(max(uncut - planned, 0.0))
    return max(gaps) if gaps else None


def read_normal(path: Path, tz: ZoneInfo, now: datetime, cfg: dict) -> Optional[dict]:
    """NORMAL file: the expiry date-time. Returns {temp, until, id} while active, None when the file
    is missing, expired or unreadable (a typo must not hold the house at one temperature)."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    try:
        until = datetime.fromisoformat(text)
        until = until if until.tzinfo else until.replace(tzinfo=tz)
    except ValueError:
        log.error("NORMAL file must hold an expiry date-time, ignoring it: %r", text[:30])
        return None
    if now >= until:
        return None
    return {"temp": cfg["normal"]["temp"], "until": until, "id": text}


def set_nudge(value: str, tz: ZoneInfo, cfg: dict, now: Optional[datetime] = None) -> int:
    """warmer / cooler: one more step, lasting `hours` from this press; longer: one more hour; reset
    removes it. "Warmer" first undoes a cooler step, then lifts the floor while the plan is cutting
    (current_cut) and the floor has not reached the uncut target, and only then raises the plan
    itself. "Cooler" takes back a raise, then the floor, then lowers the plan. The floor and the raise
    together stay within `max`, and so does a lowering."""
    now = (now or datetime.now(tz)).astimezone(tz)
    n = cfg["nudge"]
    if value == "reset":
        NUDGE_FILE.unlink(missing_ok=True)
        print("NUDGE removed")
        return 0
    cur = read_nudge(NUDGE_FILE, tz, now)
    if value == "longer":
        if not cur:
            print("NUDGE: nothing to extend")
            return 2
        until = max(cur.until, now) + timedelta(hours=1)
        floor, shift = cur.floor, cur.shift
    elif value in ("warmer", "cooler"):
        floor, shift, step = cur.floor, cur.shift, n["step"]
        if value == "warmer":
            cut = current_cut(cfg, now) if cfg else None
            if shift < 0:
                shift += step
            elif floor + max(shift, 0.0) + step > n["max"]:
                print(f"NUDGE already at +{n['max']:g}")
                return 0
            elif cut is not None and cut > floor:
                floor += step
            else:
                shift += step
        elif shift > 0:
            shift -= step
        elif floor > 0:
            floor -= step
        else:
            shift = max(shift - step, -n["max"])
        until = now + timedelta(hours=n["hours"])
    else:
        print(f"--control nudge takes warmer, cooler, longer or reset, not {value!r}")
        return 2
    floor, shift = round(floor, 1), round(shift, 1)
    if not floor and not shift:
        NUDGE_FILE.unlink(missing_ok=True)
        print("NUDGE back to 0")
        return 0
    NUDGE_FILE.write_text(f"{shift:+.1f} {until.isoformat(timespec='minutes')} floor={floor:.1f}\n", encoding="utf-8")
    print(f"NUDGE floor +{floor:g}, shift {shift:+g} C until {until:%H:%M}")
    return 0


def set_control(name: str, value: str, tz: ZoneInfo, cfg: Optional[dict] = None) -> int:
    """The --control command: write or remove one control file. Returns an exit code.

    away / holiday: `on` (until removed), an ISO date-time, or `off`; pause: `on` (put all units
    on a safe setpoint, then hands off), `hold` (hands off at once) or `off`; nudge: warmer, cooler
    or reset. Anything else is refused: a file with a typo would be ignored anyway.
    """
    if name == "nudge":
        return set_nudge(value, tz, cfg)
    if name == "normal":
        if value == "off":
            NORMAL_FILE.unlink(missing_ok=True)
            print("NORMAL cancelled: the units return to the plan at the next run")
            return 0
        if value != "on":
            print(f"--control normal takes on or off, not {value!r}")
            return 2
        hours = cfg["normal"]["timeout_hours"]
        until = datetime.now(tz) + timedelta(hours=hours)
        NORMAL_FILE.write_text(until.isoformat(timespec="seconds") + "\n", encoding="utf-8")
        print(f"NORMAL on: all units go to the normal temperature now, for at most {hours:g} h")
        return 0
    path = globals()[f"{name.upper()}_FILE"]
    if value == "off":
        path.unlink(missing_ok=True)
        print(f"{name.upper()} removed")
        return 0
    if name == "pause" and value in ("on", "hold"):
        path.write_text("settle\n" if value == "on" else "", encoding="utf-8")
        print("PAUSE on: the next run sets a safe temperature, then the controller leaves the units alone"
              if value == "on" else "PAUSE on: the controller leaves the units as they are")
        return 0
    if name != "pause" and value == "on":
        path.write_text("", encoding="utf-8")
        print(f"{name.upper()} on until removed")
        return 0
    if name != "pause":
        try:
            until = datetime.fromisoformat(value)
        except ValueError:
            until = None
        if until is not None:
            until = until if until.tzinfo else until.replace(tzinfo=tz)
            # away also records when the absence started: the warm-up is sized by it. A new return time
            # keeps the start.
            since = ""
            if name == "away":
                started = datetime.now(tz)
                present, end = read_until_file(AWAY_FILE, "AWAY", tz)
                if present and (end is None or end > datetime.now(tz)):
                    # an open-ended away has no set time in it: it started when the file was written
                    started = set_time(AWAY_FILE, "AWAY", tz) or \
                        datetime.fromtimestamp(AWAY_FILE.stat().st_mtime, tz).replace(second=0, microsecond=0)
                since = f" {started.isoformat(timespec='minutes')}"
            path.write_text(until.isoformat(timespec="minutes") + since + "\n", encoding="utf-8")
            print(f"{name.upper()} until {until.astimezone(tz):%a %d.%m. %H:%M}")
            return 0
    what = "on, hold, off" if name == "pause" else "on, off or an ISO date-time"
    print(f"--control {name} takes {what}, not {value!r}")
    return 2


def acquire_run_lock(path: Path, wait_seconds: float = 0.0):
    """Take the exclusive run lock; returns the open file (keep it) or None if another run
    holds it after waiting `wait_seconds`. The lock goes when the file is closed or the
    process ends, so a crash cannot leave it stuck."""
    handle = open(path, "a+")
    deadline = time_module.monotonic() + wait_seconds
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except BlockingIOError:
            if time_module.monotonic() >= deadline:
                handle.close()
                return None
            time_module.sleep(0.2)


def set_season(value: str) -> int:
    """The --season command: write the SEASON file. Returns an exit code."""
    if value not in ("heat", "cool"):
        print(f"--season takes heat or cool, not {value!r}")
        return 2
    SEASON_FILE.write_text(value + "\n", encoding="utf-8")
    print(f"SEASON set to {value}: the next run switches the units to that mode ([control] season_enforce)")
    return 0


def read_season(path: Path) -> str:
    """SEASON file: 'heat' or 'cool'. A missing or unreadable file means heat: winter is the safe
    default, so a lost or mistyped file never leaves the house cooling."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return "heat"
    if text not in ("heat", "cool"):
        log.error("SEASON file must say heat or cool, using heat: %r", text[:20])
        return "heat"
    return text


@dataclass(frozen=True)
class SunForecast:
    """Hourly shortwave radiation in W/m2 (global horizontal, clouds included).

    Open-Meteo labels each value by the END of the hour it averages, so the key is the
    hour end (UTC). `constant` stands in for a whole day, for --sun.
    """
    by_hour_end: Dict[datetime, float]
    constant: Optional[float] = None

    def at(self, now: datetime, lookahead_hours: int = 0) -> Optional[float]:
        """Highest radiation over the hour containing `now` and the next `lookahead_hours`
        hours. None when there is no data, which means no solar effect."""
        if self.constant is not None:
            return self.constant
        first_end = now.astimezone(timezone.utc).replace(
            minute=0, second=0, microsecond=0) + timedelta(hours=1)
        values = [self.by_hour_end[first_end + timedelta(hours=h)]
                  for h in range(lookahead_hours + 1)
                  if first_end + timedelta(hours=h) in self.by_hour_end]
        return max(values) if values else None


def parse_sun(payload: dict, tz: ZoneInfo) -> SunForecast:
    hourly = payload["hourly"]
    by_end = {}
    for stamp, value in zip(hourly["time"], hourly["shortwave_radiation"]):
        if value is not None:
            by_end[datetime.fromisoformat(stamp).replace(tzinfo=tz).astimezone(timezone.utc)] = float(value)
    return SunForecast(by_end)


def solar_energy(sun: Optional[SunForecast], now: datetime, cfg: dict, mode: str,
                 weight: float) -> Tuple[float, str]:
    """Energy-direction offset and a reason for the sun; (0, "") when it does not matter.

    The sun always lowers the setpoint. Heating: it does part of the work, so the target
    drops. Cooling: the heat is coming, so the target drops ahead of it (the lookahead).
    The effect grows from `low` to `high` W/m2 and is scaled by the unit's weight.
    """
    sc = cfg["solar"]
    if sun is None or not sc["enabled"] or weight == 0:
        return 0.0, ""
    heating = mode == "heat"
    radiation = sun.at(now, 0 if heating else sc["cool_lookahead_hours"])
    if radiation is None:
        return 0.0, ""
    share = min(max((radiation - sc["low"]) / (sc["high"] - sc["low"]), 0.0), 1.0)
    if share == 0:
        return 0.0, ""
    size = sc["heat_max_cut"] if heating else sc["cool_max_boost"]
    energy = (-size if heating else size) * share * weight
    return energy, f"sun {radiation:.0f} W/m2 {-size * share * weight:+.1f}"




NOBODY_HOME_REASON = "nobody home"


def nobody_home_target(price: Optional[float], plan: "Decision", cfg: dict) -> Tuple[float, str]:
    """(setpoint, reason) for an empty house (the [nobody_home] weekday window, and away):
    `temp`, or `dear_temp` in a quarter dearer than `dear_price`, and never above the plan. It is heated
    beyond that only where it saves money later: a quarter under `bank_price` that pre-heats before a
    spike or a peak (the plan's pre-heat rules) keeps the plan."""
    nh = cfg["nobody_home"]
    if NOBODY_HOME_REASON in plan.reason:
        return plan.final, plan.reason   # away in the weekday window: the plan already holds it down
    if price is not None and price < nh["bank_price"] and plan.rule in PREHEAT_RULES:
        return plan.final, f"{plan.reason}, {NOBODY_HOME_REASON}: banking heat at {price:.1f}c"
    floor = nh["dear_temp"] if price is not None and price > nh["dear_price"] else nh["temp"]
    floor = clamp_and_round(floor, cfg, extreme=True)
    if plan.final <= floor:
        return plan.final, f"{plan.reason}, {NOBODY_HOME_REASON}"   # the price rule's own name stays first
    return floor, f"{NOBODY_HOME_REASON}: {floor:g}" + ("" if price is None else f" at {price:.1f}c")


def apply_nudge(final: float, uncut: float, nudge: Nudge, reason: str, cfg: dict, mode: str,
                extreme: bool) -> Tuple[float, str]:
    """The too-cold / too-hot buttons on a setpoint: the floor lifts a cut `final` toward `uncut` (what
    the plan holds without the cut; heating only), then the shift moves it, a warming one up to the
    extreme maximum (a press in comfort hours at the normal maximum must still be felt)."""
    heating = mode == "heat"
    lim = profile(cfg, mode)["limits"]
    if heating and nudge.floor and final < uncut:
        # "Too cold" while saving: ease the cut, but never above what the plan holds without it.
        final = min(round_to_step(final + nudge.floor, lim["step"]), uncut)
        reason += f", nudge floor +{nudge.floor:.1f}"
    shift = nudge.shift + (0.0 if heating else nudge.floor)
    if shift:
        # From the setpoint that would be written, not from the unclamped sum: at the floor of a
        # price cut "too cold" must still lift the unit by the step.
        final = round_to_step(min(final + shift, lim["extreme_max_temp"]), lim["step"]) \
            if heating and shift > 0 else clamp_and_round(final + shift, cfg, extreme, mode)
        reason += f", nudge {shift:+.1f}"
    return final, reason


def desired_setpoint(
    now: datetime,
    prices: Optional[List[PriceInterval]],
    cfg: dict,
    unit_offset: float = 0.0,
    outdoor_mean: Optional[float] = None,
    room_temp: Optional[float] = None,
    away: Optional[Away] = None,
    mode: str = "heat",
    holiday: bool = False,
    sun: Optional[SunForecast] = None,
    solar_weight: float = 1.0,
    home: bool = False,
    nudge: "Nudge | float" = 0.0,
    unit: Optional[str] = None,
) -> Tuple[float, float, float, str]:
    """Return (base, setpoint offset, final, reason) for `now`.

    `unit` (the unit's name) selects per-unit keep windows of [above_average]; None is the page's
    plan, which shows the protected (lived-in) view.

    `nudge` (a Nudge from the NUDGE file; a number is a shift) comes last, after the price, sun,
    comfort-guard and floor effects: its floor lifts a cut quarter toward the uncut target (heating),
    its shift moves the setpoint, a warming one up to the extreme maximum (a press in comfort hours at
    the normal maximum must still be felt), a cooling one within the limits.

    `mode` is the units' operation mode, "heat" or "cool". `room_temp` enables the
    comfort guard. While heating-away holds, the [nobody_home] floors apply, never above the plan.
    Before the return the rooms warm up in its cheapest quarters (shifted_warmup). After the return
    time away is over. (Away while cooling switches the units off instead.)

    The stages: the plan (plan_decision), then the absence (a cap at the away temperature) or the
    warm-up before the return, then the too-cold / too-hot buttons, then Home's day floor.
    """
    heating = mode == "heat"
    if away and away.until is not None and now >= away.until:
        away = None   # back home: the run removes the AWAY file
    plan_args = (prices, cfg, unit_offset, outdoor_mean, room_temp, mode, holiday, sun, solar_weight, home, unit)
    warm = None
    longest = timedelta(hours=cfg["away"]["preheat_hours"])
    if heating and away and away.until is not None and now >= away.until - longest:
        # Near the return: warm up to the plan's setpoint at the return, in the cheapest quarters. Its
        # length comes from the absence, not the room reading, so the quarters chosen do not swing.
        target = home_day_floor(plan_decision(away.until, *plan_args), away.until, cfg, mode, home).final
        warm = shifted_warmup(now, prices, cfg, away.until, away.lead(cfg, target), longest,
                              cfg["nobody_home"]["temp"], target, room_temp)
    nudge = nudge if isinstance(nudge, Nudge) else Nudge(shift=nudge)
    if heating and away and warm is None:   # holding, or no heat bought yet for the return
        plan = home_day_floor(plan_decision(now, *plan_args, warm_ups=False), now, cfg, mode, home)
        until = f" until {away.until.astimezone(ZoneInfo(cfg['timezone'])):%a %d.%m. %H:%M}" if away.until else ""
        # Nobody is home, as on a weekday morning: the price sets the floor.
        final, why = nobody_home_target(price_at(prices, now), plan, cfg)
        d = Decision(final, 0.0, final, f"away{until}, {why}", plan.rule)
        if nudge:
            # Someone is home after all and cold: the floor lifts away toward the plan, a shift moves it.
            uncut = clamp_and_round(plan.base + max(plan.offset, 0.0) + unit_offset, cfg, False, mode)
            d.final, d.reason = apply_nudge(d.final, max(uncut, d.final), nudge, d.reason, cfg, mode, False)
        return d.result()
    d = plan_decision(now, *plan_args, absent=heating and away is not None)
    if warm is not None and d.rule != "peak":   # a peak keeps its cut
        d.final, d.reason = warm[0], f"{RETURN_PREHEAT_REASON}, {warm[1]}"
    if nudge:
        uncut = clamp_and_round(d.base + max(d.offset, 0.0) + unit_offset, cfg, False, mode)
        d.final, d.reason = apply_nudge(d.final, uncut, nudge, d.reason, cfg, mode, d.extreme)
    return home_day_floor(d, now, cfg, mode, home).result()


@dataclass
class Decision:
    """One quarter's setpoint as the stages build it. `rule` names the rule that set the price part
    (a price rule, above_average, night_shift, comfort_preheat): later stages decide by it, never by the
    reason text, which is only for the log, the history and the page."""
    base: float
    offset: float
    final: float
    reason: str
    rule: str = "none"
    extreme: bool = False

    def result(self) -> Tuple[float, float, float, str]:
        return self.base, self.offset, self.final, self.reason


def home_day_floor(d: Decision, now: datetime, cfg: dict, mode: str, home: bool) -> Decision:
    """Home in the daytime: no price or sun effect may make the rooms uncomfortable (heating at least
    heat_day_floor, cooling at most cool_day_ceiling)."""
    local = now.astimezone(ZoneInfo(cfg["timezone"]))
    if home and time.fromisoformat(cfg["home"]["day_start"]) <= local.time() < time.fromisoformat(cfg["home"]["day_end"]):
        limited = max(d.final, cfg["home"]["heat_day_floor"]) if mode == "heat" \
            else min(d.final, cfg["home"]["cool_day_ceiling"])
        if limited != d.final:
            d.final, d.reason = limited, d.reason + ", home: day comfort limit"
    return d


@dataclass(frozen=True)
class Situation:
    """Who is likely around in a quarter, for one unit: decided once, before the plan's rules, which
    read it instead of each testing the windows themselves. `cap` is the comfort tiers' largest cut at
    this price (comfort), `night_floor` the night's lowest target (night_other_units)."""
    regime: str
    cap: Optional[float] = None
    night_floor: Optional[float] = None


# Which of the plan's rules each regime runs (the price shift, the sun, the Home cap, the comfort
# pre-heat and the warm-ups, the room guard and the limits run in every regime). Heating only; cooling has
# none of these.
#   nobody_home       [nobody_home] weekday window: the plan ends at the price's floor (nobody_home_target)
#   comfort           comfort windows and keep windows with `comfort = true`: cuts capped by price
#   at_home           other keep windows, and the hours at the schedule's warmest temperature
#   empty             the rest of the day: a quarter dearer than the day's average runs lower
#   night_shift_units  [night_shift] units in the night window: the heat follows the night's prices
#   night_other_units  the other units in the night window: normal cuts, down to their night floor
# The night window comes first, then the nobody-home window: a comfort or keep window overlapping
# either loses its cap there.
REGIME_RULES = {
    "nobody_home":       {"above_average": False, "night_shift": False},
    "comfort":           {"above_average": False, "night_shift": False},
    "at_home":           {"above_average": False, "night_shift": False},
    "empty":             {"above_average": True,  "night_shift": False},
    "night_shift_units": {"above_average": False, "night_shift": True},
    "night_other_units": {"above_average": True,  "night_shift": False},
    "cooling":           {"above_average": False, "night_shift": False},
}
COOLING = Situation("cooling")


def situation(local: datetime, price: Optional[float], base: float, cfg: dict, holiday: bool,
              unit: Optional[str]) -> Situation:
    """The regime of a quarter for `unit` (None: the page's plan, which shows the lived-in view and
    the night shift units' night). `holiday` is HOLIDAY or Home: the weekend windows apply."""
    ns = optional(cfg, "night_shift")
    if ns and night_window(local, ns, holiday):
        if unit is None or unit in ns.get("units", []):
            return Situation("night_shift_units")
        return Situation("night_other_units", night_floor=ns.get("others_floor"))
    nh = cfg.get("nobody_home")
    if nh and nh.get("weekday") and not holiday and local.weekday() < 5 and clock(nh["weekday"][0]) <= local.time() < clock(nh["weekday"][1]):
        return Situation("nobody_home")
    cap = comfort_cap(local, price, cfg, holiday, unit)
    if cap is not None:
        return Situation("comfort", cap=cap)
    key = "weekend" if holiday or local.weekday() >= 5 else "weekday"
    if above_average_kept(local, cfg, holiday, unit) \
            or base >= max(t for _, t in parse_schedule(cfg["schedule"][key])):
        return Situation("at_home")
    return Situation("empty")


def plan_decision(now: datetime, prices: Optional[List[PriceInterval]], cfg: dict, unit_offset: float,
                  outdoor_mean, room_temp: Optional[float], mode: str, holiday: bool,
                  sun: Optional[SunForecast], solar_weight: float, home: bool, unit: Optional[str],
                  absent: bool = False, warm_ups: bool = True) -> Decision:
    """The plan for one quarter and one unit: the schedule, the price rules, the comfort rules, the
    sun, the room guard, the limits, the night floor and the warm-ups for arrivals. `absent` (heating,
    away set but not holding now: the warm-up before the return) leaves out the comfort pre-heat and the
    warm-ups, and the reason says the return pre-heat. `warm_ups` False (away holding) leaves out the
    warm-ups only."""
    heating = mode == "heat"
    rules = profile(cfg, mode)
    local = now.astimezone(ZoneInfo(cfg["timezone"]))
    # Cooling has one flat setpoint: no weekday schedule in the summer holidays.
    base = base_setpoint(local, cfg, holiday or home) if heating else cfg["cool"]["temp"]
    energy, reason, extreme, rule, cheapest_kept = price_shift_at(
        now, prices, cfg, factor_for(outdoor_mean, cfg, mode), mode)
    where = situation(local, price_at(prices, now), base, cfg, holiday or home, unit) if heating else COOLING
    rules_on = REGIME_RULES[where.regime]
    cap = where.cap
    if cap is not None and rule == "spike":
        cap = max(cap, cfg["spikes"]["cut"])   # a short spike is cut even where the tier protects comfort
    if cap is not None and rule == "dear_quarter":
        cap = max(cap, cfg["hour_shaping"]["cut"])   # so is the dearest quarter of a dear hour
    if cap is not None and cheapest_kept and energy < 0:
        # Comfort hours: the hour's dearest quarter is cut, so its cheapest one keeps the full target.
        energy, extreme = 0.0, False
        reason += ", cheapest quarter kept"
    if cap is not None and energy < -cap:
        # Comfort hours (waking, at home): the price decides how much of a cut is allowed.
        energy, extreme = -cap, False
        reason += f", comfort hours: cut capped at {cap:g}"
    night = night_shift(now, prices, cfg, holiday or home, unit) if rules_on["night_shift"] else None
    # a night too sparsely priced for the night shift (None) is left to the day's rules
    above = rules_on["above_average"] or (rules_on["night_shift"] and night is None)
    dear = above_average_cut(now, prices, cfg) if above else None
    if dear and energy > -dear[0] and rule not in PREHEAT_RULES:
        # Night and empty house: a quarter dearer than the day's average runs lower (down to the
        # floor in [limits]). Boosts and smaller cuts give way; pre-heats do not.
        energy, extreme, reason, rule = -dear[0], False, dear[1], "above_average"
    if night and rule not in PREHEAT_RULES | {"peak"} and not energy > max(night[0], 0.0):
        # In the night window the night shift units follow the price within the night, and the floors hold:
        # no flat cut for hours and no one-quarter dip below the floor for the price. Kept: pre-heats,
        # a peak's cut and a bigger nearly-free boost.
        energy, extreme, reason, rule = night[0], False, night[1], "night_shift"
    cooldown = night_cooldown(local, base, cfg, holiday or home, unit) if heating and not absent else None
    if cooldown and rule not in PREHEAT_RULES | {"peak"} and energy > -cooldown[0]:
        # The evening before the night: ease down in steps, whatever the price (a cheap boost gives way).
        energy, extreme, reason, rule = -cooldown[0], False, cooldown[1], "night_cooldown"
    if home and energy < -cfg["home"]["max_cut"]:
        # Someone is home: price cuts stay small (and inside the normal limits).
        energy, extreme = -cfg["home"]["max_cut"], False
        reason += ", home: cut capped"
    solar, sun_reason = solar_energy(sun, now, cfg, mode, solar_weight)
    if solar:
        energy += solar
        reason += ", " + sun_reason
    pre = comfort_preheat(now, prices, cfg, holiday or home) if heating and not absent else None
    step = rules["limits"]["step"]
    if pre and round_to_step(pre[0], step) > round_to_step(base + energy, step):
        # (only where it raises the target: a quarter the cheap boost already lifts is not a pre-heat)
        # Warm the house to the comfort temperature now, while it is cheaper than at comfort time.
        energy, extreme, reason, rule = pre[0] - base, False, pre[1] + ", " + reason, "comfort_preheat"
    comfort = rules["comfort"]
    if room_temp is not None:
        # The room, not the price, has the last word on comfort.
        if heating:
            # In a peak the rooms may drop further (blankets): otherwise the guard would undo the
            # cut as soon as they reach room_min and the units would swing between cut and base.
            room_min = comfort["room_min"]
            if rule == "peak":
                room_min = rules["extreme"].get("peak_room_min", room_min)
            cut_unwanted = energy < 0 and room_temp < room_min
            boost_unwanted = energy > 0 and room_temp >= comfort["room_max"]
        else:
            cut_unwanted = energy < 0 and room_temp > comfort["room_max"]
            boost_unwanted = energy > 0 and room_temp < comfort["room_min"]
        if cut_unwanted or boost_unwanted:
            energy, extreme = 0.0, False
            reason += f", comfort guard: room {room_temp:.1f}"
    offset = energy if heating else 0.0 - energy
    if absent:
        reason = f"{RETURN_PREHEAT_REASON}, {reason}"
    raw = base + offset + unit_offset
    final = clamp_and_round(raw, cfg, extreme, mode)
    if final != round_to_step(raw, rules["limits"]["step"]):
        reason += ", clamped"
    sleep_max = night_max_temp(local, cfg, holiday or home, unit) if heating and not absent else None
    if sleep_max is not None and final > sleep_max and rule != "comfort_preheat":
        # Sleeping warmth: not above this even when power is cheap, until the morning pre-heat.
        final = sleep_max
        reason += f", night max {sleep_max:g}"
    night_min = where.night_floor
    if night_min is not None and final < night_min and rule != "peak":
        # Upstairs at night: it may sit low, but not under its own night floor (a peak still cuts).
        final = night_min
        reason += f", night floor {night_min:g}"
    d = Decision(base, offset, final, reason, rule, extreme)
    if where.regime == "nobody_home":
        d.final, d.reason = nobody_home_target(price_at(prices, now), d, cfg)
    if heating and warm_ups and not absent and rule != "peak":
        # Warm up for the people arriving (a peak keeps its cut): the plan before and at the arrival,
        # without the room, so the warm-up's length does not swing with each reading.
        plan_at = lambda t: plan_decision(t, prices, cfg, unit_offset, outdoor_mean, None, mode, holiday, sun,
                                          solar_weight, home, unit).final
        for arrival, longest, start_temp, what in arrivals(local, cfg, holiday or home, plan_at):
            lead = warmup_lead(cfg, plan_at(arrival) - start_temp, longest) if start_temp is not None else None
            if lead is None:   # the first arrival at 15:15: the house has coasted down since the window began
                nh = cfg["nobody_home"]
                start_temp = nh["temp"]
                empty_for = arrival - datetime.combine(arrival.date(), clock(nh["weekday"][0]), tzinfo=arrival.tzinfo)
                lead = warmup_lead(cfg, coasted_gap(cfg, plan_at(arrival), start_temp, empty_for), longest)
            warm, why = shifted_warmup(now, prices, cfg, arrival, lead, longest, start_temp, plan_at(arrival),
                                       room_temp) or (None, "")
            if warm is not None and warm > d.final:
                d.final, d.reason = warm, f"{d.reason}, {WARMUP_REASON} {what} {arrival:%H:%M}: {why}"
    return d


WARMUP_REASON = "warm-up for"


def shifted_warmup(now: datetime, prices: Optional[List[PriceInterval]], cfg: dict, arrival: datetime,
                   lead: timedelta, longest: timedelta, start_temp: float, target: float,
                   room: Optional[float] = None) -> Optional[Tuple[float, str]]:
    """(setpoint, reason) in a warm-up for people arriving (the first arrival at 15:15, a comfort window),
    else None. The rooms need lead / 15 min quarters of heating to get from `start_temp` to `target` (the
    plan at the arrival, so the price still has its say there); those quarters are the cheapest of the
    `longest` before the arrival (unpriced ones last, a tie the later one), each lifting the target a step,
    and the quarters between hold the level reached. The saving comes from buying the heat cheaply, not
    from arriving colder: empty rooms coast down slowly ([warmup] cooling_rate), so heat bought early
    is mostly kept."""
    q = timedelta(minutes=15)
    n = max(1, round(lead / q))
    first = arrival - max(longest, lead)
    quarters = [first + j * q for j in range(round((arrival - first) / q))]
    priced = {t: price_at(prices, t) for t in quarters}
    chosen = sorted(quarters, key=lambda t: (priced[t] is None, priced[t] or 0.0, -t.timestamp()))[:n]
    now_q = max((t for t in quarters if t <= now), default=None)
    done = sum(1 for t in chosen if now_q is not None and t <= now_q)
    if done == 0:
        return None   # no heat bought yet: the plan holds
    level = start_temp + (target - start_temp) * done / n
    heating = now_q in chosen
    wu = cfg.get("warmup") or {}
    if room is not None and heating and "warmup_rate" in wu:
        level = max(level, min(target, room + wu["warmup_rate"] / 4))   # a live run: this quarter heats
    final = min(target, round_to_step(level, cfg["limits"]["step"]))
    return final, f"{done}/{n} to {target:g}, " + ("heating" if heating else "holding")


def arrivals(local: datetime, cfg: dict, holiday: bool, plan_at) -> list:
    """The warm-ups `local` may fall in: (arrival, longest lead, start temperature, what), the start None
    for the first arrival at the end of the [nobody_home] window (it is sized by the coasting instead)."""
    out = []
    nh = cfg.get("nobody_home")
    if cfg.get("warmup") and nh and nh.get("weekday") and not holiday and local.weekday() < 5:
        end = datetime.combine(local.date(), clock(nh["weekday"][1]), tzinfo=local.tzinfo)
        longest = timedelta(hours=nh["warmup_hours"])
        if end - longest <= local < end:
            out.append((end, longest, None, "home"))
    cwu, cw = optional(cfg, "comfort_warmup"), optional(cfg, "comfort_windows")
    if cfg.get("warmup") and cwu and cw:
        longest = timedelta(hours=cwu["hours"])
        for start, _ in cw["weekend" if holiday or local.weekday() >= 5 else "weekday"]:
            opens = datetime.combine(local.date(), clock(start), tzinfo=local.tzinfo)
            if opens - longest <= local < opens:
                out.append((opens, longest, plan_at(opens - longest - timedelta(minutes=15)), "comfort"))
    return out


# --------------------------------------------------------------------------
# Plan table
# --------------------------------------------------------------------------

REASON_KINDS = (  # (start of the reason text, kind); the first match wins
    ("peak price", "peak"), ("pre-heat before peak", "peak_preheat"),
    ("pre-heat before return", "return_preheat"), (COMFORT_PREHEAT_REASON, "comfort_preheat"),
    ("pre-heat before", "preheat"),
    ("spike", "spike"), (DEAR_QUARTER_REASON, "dear_quarter"), (NIGHT_SHIFT_REASON, "night_shift"), (NIGHT_COOLDOWN_REASON, "night_cooldown"), (ABOVE_AVERAGE_REASON, "above_average"), ("extreme price", "extreme"), ("very expensive", "expensive"), ("expensive", "expensive"),
    ("high price", "expensive"), ("cheap", "cheap"), ("mid-priced", "normal"), ("away", "away"), (NOBODY_HOME_REASON, "nobody_home"),
    ("no prices", "none"), ("no shift", "none"),
)


def reason_kind(reason: str) -> str:
    """A machine name for the main part of a setpoint reason ('cheap', 'expensive', 'peak', ...),
    so a page can say it in another language without parsing the log text."""
    for start, kind in REASON_KINDS:
        if reason.startswith(start):
            return kind
    return "other"


def nudge_at(nudge: Optional[Nudge], t: datetime, now: datetime) -> "Nudge | float":
    """The nudge as the plan shows it in the quarter starting at `t`: from the quarter of now (the
    real now: tomorrow's plan runs with `now` at tomorrow noon) to the nudge's end, else none."""
    shown_from = min(now, datetime.now(timezone.utc))
    return nudge if nudge and t + timedelta(minutes=15) > shown_from and t < nudge.until else 0.0


def switch_at(on: bool, until: Optional[datetime], t: datetime) -> bool:
    """Whether a HOME or HOLIDAY switch applies to the quarter starting at `t`: on, and before its
    end if it has one."""
    return bool(on) and (until is None or t < until)


def plan_rows(now: datetime, prices: Optional[List[PriceInterval]], cfg: dict,
              outdoor_mean: Optional[float] = None, away: Optional[Away] = None,
              mode: str = "heat", holiday: bool = False,
              sun: Optional[SunForecast] = None, home: bool = False,
              nudge: Optional[Nudge] = None, home_until: Optional[datetime] = None,
              holiday_until: Optional[datetime] = None) -> List[dict]:
    """One row per quarter hour of the local calendar day of `now`: the base schedule, the price
    shift, the setpoint without any unit offset, the reason and the price in c/kWh. An active nudge
    shows from the quarter of `now` to its end: that is what the units get. `home` with `home_until`
    (the HOME file's end) applies only to the quarters before that end, `holiday` with
    `holiday_until` likewise."""
    tz = ZoneInfo(cfg["timezone"])
    day_start, day_end = local_day_bounds(now.astimezone(tz).date(), tz)
    rows, t = [], day_start
    while t < day_end:
        active = nudge_at(nudge, t, now)
        base, offset, final, reason = desired_setpoint(
            t, prices, cfg, 0.0, outdoor_mean, None, away, mode, switch_at(holiday, holiday_until, t), sun, 1.0,
            switch_at(home, home_until, t), active)
        out = outdoor_temp_at(outdoor_mean, t)
        code, is_day = outdoor_mean.code_at(t) if isinstance(outdoor_mean, OutdoorForecast) else (None, None)
        rows.append({"start": t.astimezone(tz), "price": price_at(prices, t), "base": base,
                     "outdoor": None if out is None else round(out, 1),
                     "weather_code": code, "is_day": is_day,
                     "offset": offset, "final": final, "reason": reason, "kind": reason_kind(reason)})
        t += timedelta(minutes=15)
    return rows


def plan_table(now: datetime, prices: Optional[List[PriceInterval]], cfg: dict,
               outdoor_mean: Optional[float] = None, away: Optional[Away] = None,
               mode: str = "heat", holiday: bool = False,
               sun: Optional[SunForecast] = None, home: bool = False,
               nudge: Optional[Nudge] = None, home_until: Optional[datetime] = None,
               holiday_until: Optional[datetime] = None) -> str:
    """Table for the local calendar day of `now`; consecutive equal rows merged."""
    rows_all = plan_rows(now, prices, cfg, outdoor_mean, away, mode, holiday, sun, home, nudge, home_until,
                         holiday_until)
    rows = []
    for r in rows_all:
        row = (r["base"], r["offset"], r["final"], r["reason"])
        if not rows or rows[-1][1] != row:
            rows.append((r["start"], row))
    if isinstance(outdoor_mean, OutdoorForecast):
        tz = ZoneInfo(cfg["timezone"])
        temps = [outdoor_temp_at(outdoor_mean, r["start"]) for r in rows_all]
        temps = [t for t in temps if t is not None]
        mean = outdoor_day_mean(outdoor_mean, now, tz)
        weather = (f"outdoor mean: {mean:.1f}" if mean is not None else "outdoor mean: n/a") + \
                  (f" (hourly {min(temps):.1f} to {max(temps):.1f})" if temps else "")
        factors = [weather_factor(t, cfg, mode) for t in temps]
        factor_text = f"{min(factors):.2f}-{max(factors):.2f}" if factors else "1.00"
    else:
        weather = f"outdoor mean: {outdoor_mean if outdoor_mean is not None else 'n/a'}"
        factor_text = f"{weather_factor(outdoor_mean, cfg, mode):.2f}"
    lines = [f"mode: {mode}  {weather}  weather factor: {factor_text}",
             f"{'local':<6} {'base':>5} {'offset':>6} {'final':>5}  reason"]
    for start, (base, offset, final, reason) in rows:
        lines.append(
            f"{start:%H:%M}  {base:5.1f} {offset:+6.1f} {final:5.1f}  {reason}"
        )
    return "\n".join(lines)


def plan_json(now: datetime, prices: Optional[List[PriceInterval]], cfg: dict,
              outdoor_mean: Optional[float] = None, away: Optional[Away] = None,
              mode: str = "heat", holiday: bool = False,
              sun: Optional[SunForecast] = None, home: bool = False,
              nudge: Optional[Nudge] = None, home_until: Optional[datetime] = None,
              holiday_until: Optional[datetime] = None) -> str:
    """The day's plan as JSON for the remote page: `rows` (see plan_rows) and the quarter hour of `now`.
    For each override in force (nudge, away, home) every row also carries in `without` the target the
    plan would have without it, so the page can estimate what the override costs or saves."""
    rows = plan_rows(now, prices, cfg, outdoor_mean, away, mode, holiday, sun, home, nudge, home_until,
                     holiday_until)
    shown_from = min(now, datetime.now(timezone.utc))
    for r in rows:
        # Only the quarters from now while each override is in force: the rest the page never prices.
        t, r["without"] = r["start"], {}
        if t + timedelta(minutes=15) <= shown_from:
            continue
        n, h, hol = nudge_at(nudge, t, now), switch_at(home, home_until, t), switch_at(holiday, holiday_until, t)
        plan = lambda a, h, nn: desired_setpoint(t, prices, cfg, 0.0, outdoor_mean, None, a, mode, hol,
                                                 sun, 1.0, h, nn)[2]
        if n:
            r["without"]["nudge"] = plan(away, h, 0.0)
        if away and (away.until is None or t < away.until):
            r["without"]["away"] = plan(None, h, n)
        if h:
            r["without"]["home"] = plan(away, False, n)
    now_utc = now.astimezone(timezone.utc)
    index = next((k for k, r in enumerate(rows)
                  if r["start"].astimezone(timezone.utc) <= now_utc
                  < r["start"].astimezone(timezone.utc) + timedelta(minutes=15)), None)
    return json.dumps({"mode": mode, "outdoor_mean": outdoor_day_mean(outdoor_mean, now, ZoneInfo(cfg["timezone"])),
                       "now_index": index,
                       "rows": [{**r, "start": r["start"].isoformat(timespec="minutes")} for r in rows]})


# --------------------------------------------------------------------------
# Resilience: retry with backoff, and a circuit breaker with cool-off
# --------------------------------------------------------------------------

def is_transient(exc: BaseException) -> bool:
    """Worth retrying: connection trouble, timeouts, 5xx and 429.

    Anything else (bad credentials, 4xx, bugs) is not retried: repeating a
    rejected login risks locking the MELCloud account.
    """
    import aiohttp

    if isinstance(exc, aiohttp.ClientResponseError):
        return exc.status >= 500 or exc.status == 429
    return isinstance(exc, (aiohttp.ClientError, asyncio.TimeoutError, OSError))


async def with_retry(fn: Callable, what: str, cfg: dict):
    """Call `await fn()` with a timeout, retrying transient errors with backoff."""
    r = cfg["resilience"]
    attempts = r["retry_attempts"]
    for attempt in range(1, attempts + 1):
        try:
            return await asyncio.wait_for(fn(), r["operation_timeout_seconds"])
        except Exception as exc:
            if attempt == attempts or not is_transient(exc):
                raise
            delay = min(r["retry_base_delay_seconds"] * 2 ** (attempt - 1),
                        r["retry_max_delay_seconds"])
            log.warning("%s failed (%s), retry %d/%d in %.0fs",
                        what, type(exc).__name__, attempt, attempts - 1, delay)
            await SLEEP(delay)


class CircuitBreaker:
    """Skips runs after repeated failures, for a cool-off period.

    State lives in a small JSON file because each cron run is a new process.
    After the cool-off one run is let through: success closes the breaker, and
    a failure re-opens it at once (the failure count is still at the threshold).
    """

    def __init__(self, path: Path, threshold: int, cool_off: timedelta, tz=timezone.utc):
        self.path, self.threshold, self.cool_off, self.tz = path, threshold, cool_off, tz

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            log.exception("unreadable breaker state %s, treating breaker as closed", self.path)
            return {}

    def _save(self, state: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(self.path)  # atomic: a crash never leaves half a file

    def open_until(self, now: datetime) -> Optional[datetime]:
        raw = self._load().get("open_until")
        if not raw:
            return None
        until = datetime.fromisoformat(raw)
        return until if until > now else None

    def record_success(self) -> None:
        if self._load().get("failures"):
            log.info("circuit breaker closed after a successful run")
        self.path.unlink(missing_ok=True)

    def record_failure(self, now: datetime) -> None:
        failures = int(self._load().get("failures", 0)) + 1
        state = {"failures": failures, "open_until": None}
        if failures >= self.threshold:
            state["open_until"] = (now + self.cool_off).astimezone(self.tz).isoformat(timespec="seconds")
            log.error("circuit breaker OPEN after %d failed runs, skipping runs until %s",
                      failures, state["open_until"])
        self._save(state)


class UnitState:
    """What the controller remembers about each unit between runs.

    Cron runs are separate processes, so this lives in a JSON file that only the
    controller writes. A missing or unreadable file means no memory, never a crash.
    """

    def __init__(self, path: Path):
        self.path = path
        self.data = {}
        try:
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            log.exception("unreadable unit state %s, starting without memory", path)

    def unit(self, name: str) -> dict:
        return self.data.setdefault("units", {}).setdefault(name, {})

    def save(self) -> None:
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data), encoding="utf-8")
            tmp.replace(self.path)  # atomic: a crash never leaves half a file
        except OSError:
            log.exception("could not save unit state %s", self.path)


def make_breaker(cfg: dict) -> CircuitBreaker:
    r = cfg["resilience"]
    return CircuitBreaker(APP_DIR / r["state_file"], r["breaker_failure_threshold"],
                          timedelta(minutes=r["breaker_cool_off_minutes"]), ZoneInfo(cfg["timezone"]))


# --------------------------------------------------------------------------
# Units
# --------------------------------------------------------------------------

def fan_to(device, want: str) -> dict:
    """{"fan_speed": want} when the unit offers that speed and is not on it yet, else {}. Units
    that do not report their fan (or lack "auto") are left alone."""
    speeds = getattr(device, "fan_speeds", None)
    if not speeds or want not in speeds or getattr(device, "fan_speed", None) == want:
        return {}
    return {"fan_speed": want}


def fastest_fan(device) -> Optional[str]:
    """The unit's top numbered fan speed, or None when it reports none."""
    numbered = [x for x in (getattr(device, "fan_speeds", None) or []) if x != "auto"]
    return numbered[-1] if numbered else None


def unit_skip_reason(device, cfg: dict) -> Optional[str]:
    if not device.power:
        return "power off"
    if device.operation_mode not in cfg["control"]["allowed_modes"]:
        return f"mode {device.operation_mode}"
    return None


HISTORY_COLUMNS = ["time_local", "unit", "action", "room_temp", "old_target", "new_target",
                   "base", "offset", "outdoor_mean", "reason", "price", "kind"]


def monthly_path(path: Path, now: datetime, tz: ZoneInfo) -> Path:
    """history.csv -> history-2026-11.csv: one file per local calendar month, so no file grows
    without bound and nothing has to be rotated or deleted."""
    return path.with_name(f"{path.stem}-{now.astimezone(tz):%Y-%m}{path.suffix}")


def record_history(path: Path, now: datetime, cfg: dict, row: dict) -> None:
    """Append one row per unit per run, to compare targets with room temperatures. `price` (c/kWh
    of this quarter) and `kind` (reason_kind) make it easy to see how each rule moved the rooms."""
    ensure_header(path, HISTORY_COLUMNS)
    is_new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_COLUMNS)
        if is_new:
            w.writeheader()
        w.writerow({"time_local": now.astimezone(ZoneInfo(cfg["timezone"])).isoformat(timespec="seconds"),
                    **row})


TELEMETRY_COLUMNS = ["time_local", "unit", "room_temp", "target_temp", "power", "mode",
                     "fan_actual", "energy_kwh", "has_meter", "outdoor_c", "humidity", "wind_ms", "weather_code"]


def ensure_header(path: Path, columns: List[str]) -> None:
    """If the CSV exists with an older header (fewer columns), rewrite it with the current one:
    old rows keep their values and get empty cells for the new columns."""
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames == columns:
                return
            rows = list(reader)
    except FileNotFoundError:
        return
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


def record_telemetry(path: Path, now: datetime, cfg: dict, device,
                     weather: Optional[Dict[str, Optional[float]]] = None) -> None:
    """One row per unit per run with what the unit reports, before this run changes anything.

    This is data for working out how fast the rooms cool and heat (fan_actual 0 = stopped, so
    idle stretches can be told from heating ones) and, if the unit has an energy meter, real
    kWh. A property the unit does not have is left empty. `weather` is the outdoor temperature,
    humidity, wind and WMO weather code at this moment from the forecast (it models the conditions,
    it does not measure them), for working out how the weather drives heat loss and power draw.
    """
    def read(name):
        try:
            value = getattr(device, name)
        except Exception:
            return ""
        return "" if value is None else value

    ensure_header(path, TELEMETRY_COLUMNS)
    weather = weather or {}

    def wx(key, digits):
        value = weather.get(key)
        if value is None:
            return ""
        return round(value, digits) if digits else int(round(value))

    is_new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TELEMETRY_COLUMNS)
        if is_new:
            w.writeheader()
        w.writerow({"time_local": now.astimezone(ZoneInfo(cfg["timezone"])).isoformat(timespec="seconds"),
                    "unit": device.name, "room_temp": read("room_temperature"),
                    "target_temp": read("target_temperature"), "power": read("power"),
                    "mode": read("operation_mode"), "fan_actual": read("actual_fan_speed"),
                    "energy_kwh": read("total_energy_consumed"),
                    "has_meter": read("has_energy_consumed_meter"),
                    "outdoor_c": wx("temp", 1), "humidity": wx("humidity", 0), "wind_ms": wx("wind", 1),
                    "weather_code": wx("code", 0)})


async def apply_to_units(
    devices: list,
    now: datetime,
    prices: Optional[List[PriceInterval]],
    cfg: dict,
    dry_run: bool,
    outdoor_mean: Optional[float] = None,
    history: Optional[Path] = None,
    away: Optional[Away] = None,
    unit_state: Optional[UnitState] = None,
    season: Optional[str] = None,
    holiday: bool = False,
    sun: Optional[SunForecast] = None,
    home: bool = False,
    telemetry: Optional[Path] = None,
    nudge: "Nudge | float" = 0.0,
    normal: Optional[dict] = None,
) -> int:
    """Set the desired target on each unit that needs it. Returns failure count.

    One unit failing never stops the others. With `history`, every unit that
    could be read gets a row each run: action set, keep, skip, hold, dry-run or
    set-failed, mode-switch or mode-mismatch. With `season` ("heat" or "cool") the
    running units are put in that mode together. With `unit_state`, a target changed by someone else (wall remote,
    MELCloud app) holds that unit for `manual_hold_minutes`.
    """
    names = cfg["units"]["names"]
    offsets = cfg["units"].get("offsets", {})
    cool_offsets = cfg["units"].get("cool_offsets", {})
    failures = 0
    # The outdoor weather arrives as an hourly forecast (or one plain number): the history file gets
    # the day's mean, the telemetry gets the conditions at this moment.
    day_outdoor = outdoor_day_mean(outdoor_mean, now, ZoneInfo(cfg["timezone"]))
    if isinstance(outdoor_mean, OutdoorForecast):
        weather_now = outdoor_mean.sample(now)
        day_outdoor = None if day_outdoor is None else round(day_outdoor, 1)
    else:
        weather_now = {} if outdoor_mean is None else {"temp": outdoor_mean}

    def unit_offset(name, mode):
        return (offsets if mode == "heat" else cool_offsets).get(name, 0.0)

    def solar_weight(name):  # how much this unit's rooms feel the sun (windows, orientation)
        return cfg["units"].get("solar_weight", {}).get(name, 1.0)

    def record(device, action, current, base="", offset="", final="", reason=""):
        if not history:
            return
        try:  # a history problem must never stop heating control
            record_history(history, now, cfg, {
                "unit": device.name, "action": action, "room_temp": device.room_temperature,
                "old_target": current, "new_target": final, "base": base,
                "offset": round(offset, 2) if offset != "" else "",
                "outdoor_mean": day_outdoor, "reason": reason,
                "price": "" if (p := price_at(prices, now)) is None else round(p, 3),
                "kind": reason_kind(reason) if reason else ""})
        except Exception:
            log.exception("%s: could not write history", device.name)

    unknown = configured_unit_names(cfg) - {d.name for d in devices}
    if unknown:
        # A misspelt or stale name silently does nothing (an offset, a night unit, a multi-split).
        log.warning("config names units MELCloud doesn't have (see --list): %s", ", ".join(sorted(unknown)))

    # Read every unit first, controlled or not: a multi-split's mode check needs them all.
    readable = []
    for device in devices:
        try:
            await with_retry(device.update, f"{device.name} update", cfg)
            readable.append(device)
        except Exception:
            failures += 1
            log.exception("%s: failed", device.name)
            continue
        if telemetry:
            try:  # telemetry must never stop heating control
                record_telemetry(telemetry, now, cfg, device, weather_now)
            except Exception:
                log.exception("%s: could not write telemetry", device.name)

    # "Normal now" (the NORMAL file, the page's reset button): every unit goes to the normal
    # temperature at once, whatever the season and the plan say. Units on one outdoor unit cannot heat
    # and cool at once, so each outdoor group gets one mode from its rooms' average temperature (heat
    # if below the temperature, else cool); a self-contained unit goes by its own room. It ends when
    # every room has reached the temperature, when the file is removed, or when it expires; then each
    # unit returns to the season's mode and the plan. State lives in unit_state so a session survives
    # from run to run.
    handled = set()

    async def normal_step() -> int:
        fails = 0
        controlled = [d for d in readable if not names or d.name in names]
        handled.update(d.name for d in controlled)
        sess = unit_state.data.get("normal")
        if normal and (not sess or sess["id"] != normal["id"]):
            modes = {}
            for group in outdoor_groups(cfg, [d.name for d in controlled]):
                rooms = [d.room_temperature for d in controlled
                         if d.name in group and d.room_temperature is not None]
                mode = "heat" if not rooms or sum(rooms) / len(rooms) < normal["temp"] else "cool"
                modes.update({name: mode for name in group})
            sess = {"id": normal["id"], "modes": modes, "temp": normal["temp"]}
        temp = sess["temp"]
        # (a session saved before the modes were per unit has one "mode" for all)
        mode_of = lambda d: sess.get("modes", {}).get(d.name, sess.get("mode", "heat"))
        reached = [d.room_temperature is None
                   or (d.room_temperature >= temp if mode_of(d) == "heat" else d.room_temperature <= temp)
                   for d in controlled]
        if normal and not all(reached):
            if not dry_run:
                unit_state.data["normal"] = sess
            for d in controlled:
                mode = mode_of(d)
                props = {}
                if not d.power:
                    props["power"] = True
                if d.operation_mode != mode:
                    props["operation_mode"] = mode
                if d.target_temperature is None or abs(d.target_temperature - temp) >= 0.25:
                    props["target_temperature"] = temp
                fast = fastest_fan(d) if cfg.get("normal", {}).get("fan_full", True) else None
                if fast and d.fan_speed != fast:
                    props["fan_speed"] = fast      # to get there quickly; auto again when it ends
                if not props:
                    log.info("%s: normal %s %.1f, room %s", d.name, mode, temp, d.room_temperature)
                    continue
                if dry_run:
                    log.info("%s: DRY-RUN would write %s for normal", d.name, props)
                    continue
                try:
                    await with_retry(lambda: d.set(props), f"{d.name} normal temperature", cfg)
                except Exception:
                    fails += 1
                    log.exception("%s: normal temperature write failed", d.name)
                    record(d, "normal-failed", d.target_temperature, reason=f"{mode} {temp}")
                    continue
                log.info("%s: normal temperature, %s %.1f (room %s)", d.name, mode, temp, d.room_temperature)
                record(d, "normal-set", d.target_temperature, final=temp, reason=f"normal now, {mode}")
                memory = unit_state.unit(d.name)
                memory["last_target"] = temp
            return fails
        # Over: reached (or already there), cancelled, or timed out. Back to the season / the plan.
        why = "reached" if normal else "cancelled or expired"
        for d in controlled:
            back = season or "heat"   # the season's mode; heat when nothing says otherwise
            base, offset, final, reason = desired_setpoint(
                now, prices, cfg, unit_offset(d.name, back), outdoor_mean, d.room_temperature,
                away, back, holiday, sun, solar_weight(d.name), home, nudge, d.name)
            props = {"target_temperature": final}
            if d.operation_mode != back:
                props["operation_mode"] = back
            props.update(fan_to(d, "auto"))
            if dry_run:
                log.info("%s: DRY-RUN normal is over (%s), would write %s", d.name, why, props)
                continue
            try:
                await with_retry(lambda: d.set(props), f"{d.name} back to plan", cfg)
            except Exception:
                fails += 1
                log.exception("%s: could not return to the plan", d.name)
                record(d, "normal-failed", d.target_temperature, reason=f"back to {back}")
                continue
            log.info("%s: normal is over (%s), back to %s, target %.1f (%s)", d.name, why, back, final, reason)
            record(d, "normal-end", d.target_temperature, base, offset, final, f"{why}, {back}, {reason}")
            memory = unit_state.unit(d.name)
            memory["last_target"] = final
            memory.pop("hold_until", None)
        if not fails and not dry_run:
            unit_state.data.pop("normal", None)
            unit_state.data["normal_end"] = True
        elif fails:
            unit_state.data["normal"] = sess  # keep the session: the next run retries the return
        return fails

    if unit_state is not None and (normal or unit_state.data.get("normal")):
        failures += await normal_step()

    # Season: the owner chooses heating or cooling, and the controller puts every unit in that
    # mode together (one season for the house). [control] season_enforce says how hard:
    #   "always"     every run: a unit switched off or put in another mode (dry, fan, auto) is
    #                brought back to the season's mode and turned on;
    #   "running"    every run, but a unit someone switched off stays off;
    #   "on_change"  only when the season changes: each unit remembers the season it was last put in
    #                (unit_state "season") and is switched when that differs, so a unit that could not
    #                be read, or that away had switched off, gets its turn later; between changes the
    #                units are left as people put them. A unit's first run only records the season.
    # Two exceptions, both for away while cooling: a unit the controller itself switched off is left
    # to the away logic, and nothing is switched on while that away is active.
    switched = set()
    away_cooling = bool(away and away.active_at(now, cfg, cooling=True) and season == "cool")
    enforce = cfg["control"].get("season_enforce", "always")
    if enforce not in ("always", "running", "on_change"):
        log.error("[control] season_enforce must be always, running or on_change, using always: %r", enforce)
        enforce = "always"
    if enforce == "on_change" and unit_state is None:
        enforce = "always"   # nowhere to remember what each unit was given (only tests run without one)

    def season_given(d) -> Optional[str]:
        """on_change: the season this unit was last put in. A unit seen for the first time counts as
        given its own mode when it is on in heat or cool (so one in the wrong one is switched once),
        else the season (one someone switched off, or put in dry or fan, is left as it is)."""
        memory = unit_state.unit(d.name)
        if "season" not in memory:
            first = d.operation_mode if d.power and d.operation_mode in ("heat", "cool") else season
            if not dry_run:
                memory["season"] = first
            return first
        return memory["season"]

    def needs_season(d) -> bool:
        if not season or d.name in handled or (names and d.name not in names):
            return False
        if enforce == "on_change" and season_given(d) == season:
            return False
        if not d.power:
            memory = unit_state.unit(d.name) if unit_state else {}
            return enforce != "running" and not away_cooling and not memory.get("off_by_us")
        return d.operation_mode != season

    to_switch = [d for d in readable if needs_season(d)]
    if unit_state is not None and season and not dry_run:
        # units already on in the season's mode need no write, only the record (kept under every
        # setting, so a later switch to "on_change" doesn't take an old record for a change)
        for d in readable:
            if d not in to_switch and d.name not in handled and (not names or d.name in names) \
                    and d.power and d.operation_mode == season:
                unit_state.unit(d.name)["season"] = season
    for d in to_switch:
        old_mode, old_target, was_off = d.operation_mode, d.target_temperature, not d.power
        base, offset, final, reason = desired_setpoint(
            now, prices, cfg, unit_offset(d.name, season), outdoor_mean,
            d.room_temperature, away, season, holiday, sun, solar_weight(d.name), home, nudge, d.name)
        was = "off" if was_off else old_mode
        if dry_run:   # what the live run would write; the other units are still planned below
            log.info("%s: DRY-RUN would switch %s -> %s, target %.1f (%s)", d.name, was, season, final, reason)
            record(d, "dry-run", old_target, base, offset, final, f"would switch {was} -> {season}, {reason}")
            switched.add(d.name)
            continue
        props = {"operation_mode": season, "target_temperature": final}
        if was_off:
            props["power"] = True
        props.update(fan_to(d, "auto"))
        try:  # mode (and power) and target in one write, so the unit never idles on a stale target
            await with_retry(lambda: d.set(props), f"{d.name} mode switch", cfg)
        except Exception:
            failures += 1
            log.exception("%s: mode switch failed", d.name)
            record(d, "switch-failed", old_target, base, offset, final, f"{was} -> {season}")
            continue
        log.info("%s: switched %s -> %s, target %s -> %.1f (%s)",
                 d.name, was, season, old_target, final, reason)
        record(d, "mode-switch", old_target, base, offset, final, f"{was} -> {season}, {reason}")
        if unit_state:
            unit_state.unit(d.name).pop("off_by_us", None)
            unit_state.unit(d.name)["last_target"] = final
            unit_state.unit(d.name)["season"] = season
        switched.add(d.name)

    # Units on one outdoor unit ([units] outdoor_groups) cannot heat and cool at once. Differing
    # modes in a group mean something outside this controller changed one of them, and the
    # controller never changes modes except to follow the season, so it leaves that group alone until
    # a person fixes it. The other groups and the self-contained units carry on.
    running = {d.name: (season if d.name in switched else d.operation_mode)
               for d in readable if d.power or d.name in switched}
    mismatched = set()
    for group in outdoor_groups(cfg, [d.name for d in readable]):
        modes = {n: running[n] for n in group if n in running}
        if len(set(modes.values())) > 1:
            detail = ", ".join(f"{n}={m}" for n, m in sorted(modes.items()))
            for device in readable:
                if device.name in group and (not names or device.name in names):
                    record(device, "mode-mismatch", device.target_temperature, reason=detail)
            mismatched |= group
            if enforce == "on_change" and all(unit_state.unit(n).get("season") == season for n in modes):
                # left as people put them on purpose: worth a line, not a failed run (no heartbeat alert)
                log.warning("units on one outdoor unit are in different modes (%s): no changes to them", detail)
            else:
                log.error("running units on one outdoor unit are in different operating modes (%s): "
                          "no changes to them", detail)
                failures += 1

    for device in readable:
        name = device.name
        if (names and name not in names) or name in switched or name in handled or name in mismatched:
            continue
        try:
            current = device.target_temperature
            memory = unit_state.unit(name) if unit_state else {}

            # Away while cooling: the units go off and come back before the return time.
            # The only place the controller touches power, so it stays narrow: it switches
            # off running cool-mode units, switches on only units it switched off itself,
            # and leaves a unit alone that someone turned on during the trip. It needs the
            # memory file to know what it did.
            if unit_state is not None:
                away_now = bool(away and away.active_at(now, cfg, cooling=True))   # only cooling units switch
                if not away_now:
                    memory.pop("away_override", None)
                    if memory.get("off_by_us") and not device.power:
                        target_mode = device.operation_mode if device.operation_mode in ("heat", "cool") else "cool"
                        base, offset, final, reason = desired_setpoint(
                            now, prices, cfg, unit_offset(name, target_mode), outdoor_mean,
                            device.room_temperature, away, target_mode, holiday, sun,
                            solar_weight(name), home, nudge, name)
                        if dry_run:
                            log.info("%s: DRY-RUN would switch on after away, target %.1f", name, final)
                            record(device, "dry-run", current, base, offset, final, "away ended: switch on")
                            continue
                        try:
                            await with_retry(lambda: device.set({"power": True, "target_temperature": final, **fan_to(device, "auto")}),
                                             f"{name} switch on", cfg)
                        except Exception:
                            record(device, "away-on-failed", current, base, offset, final, "away ended")
                            raise  # the flag stays, so the next run tries again
                        memory.pop("off_by_us", None)
                        memory["last_target"] = final
                        log.info("%s: switched on after away, target %.1f (%s)", name, final, reason)
                        record(device, "away-on", current, base, offset, final, f"away ended, {reason}")
                        continue
                    memory.pop("off_by_us", None)  # it is on again: nothing to restore
                else:
                    if memory.get("off_by_us") and device.power:
                        memory.pop("off_by_us", None)
                        memory["away_override"] = True
                        log.info("%s: turned on during away, leaving it on", name)
                    if memory.get("off_by_us"):
                        continue  # still off from our switch-off; nothing to do or record
                    if device.power and device.operation_mode == "cool" and not memory.get("away_override"):
                        if dry_run:
                            log.info("%s: DRY-RUN would switch off for away", name)
                            record(device, "dry-run", current, reason="away while cooling: switch off")
                            continue
                        try:
                            await with_retry(lambda: device.set({"power": False}), f"{name} switch off", cfg)
                        except Exception:
                            record(device, "away-off-failed", current, reason="away while cooling")
                            raise
                        memory["off_by_us"] = True
                        memory.pop("last_target", None)
                        log.info("%s: switched off for away while cooling", name)
                        record(device, "away-off", current, reason="away while cooling")
                        continue

            skip = unit_skip_reason(device, cfg)
            if skip:
                log.info("%s: skip (%s), room %s", name, skip, device.room_temperature)
                record(device, "skip", current, reason=skip)
                memory.pop("last_target", None)  # a change while skipped is not manual
                continue

            def remember(target):
                if unit_state and not dry_run:
                    memory["last_target"] = target

            hold_minutes = cfg["control"]["manual_hold_minutes"]
            last = memory.get("last_target")
            if hold_minutes and last is not None and current is not None \
                    and abs(current - last) >= cfg["control"]["min_change"]:
                until = now + timedelta(minutes=hold_minutes)
                memory["hold_until"] = until.astimezone(ZoneInfo(cfg["timezone"])).isoformat(timespec="seconds")
                log.info("%s: manual change detected (%.1f -> %.1f), holding until %s",
                         name, last, current, until.astimezone(ZoneInfo(cfg["timezone"])).strftime("%H:%M"))
            held_until = memory.get("hold_until")
            if held_until and now < datetime.fromisoformat(held_until):
                hold_text = datetime.fromisoformat(held_until).astimezone(
                    ZoneInfo(cfg["timezone"])).strftime("%H:%M")
                log.info("%s: hold until %s after a manual change, target %.1f",
                         name, hold_text, current)
                record(device, "hold", current, reason=f"manual change, hold until {hold_text}")
                remember(current)
                continue
            memory.pop("hold_until", None)
            mode = device.operation_mode  # allowed_modes guarantees heat or cool
            memory.pop("warmup", None)   # an older version remembered a started warm-up here
            base, offset, final, reason = desired_setpoint(
                now, prices, cfg, unit_offset(name, mode), outdoor_mean,
                device.room_temperature, away, mode, holiday, sun, solar_weight(name), home, nudge, name)
            if current is not None and abs(current - final) < cfg["control"]["min_change"]:
                log.info("%s: keep %.1f, room %s (base %.1f %+.1f, %s)",
                         name, current, device.room_temperature, base, offset, reason)
                record(device, "keep", current, base, offset, final, reason)
                remember(current)
                continue
            if dry_run:
                log.info("%s: DRY-RUN would set %s -> %.1f, room %s (base %.1f %+.1f, %s)",
                         name, current, final, device.room_temperature, base, offset, reason)
                record(device, "dry-run", current, base, offset, final, reason)
                continue
            try:
                await with_retry(lambda: device.set({"target_temperature": final, **fan_to(device, "auto")}),
                                 f"{name} set", cfg)
            except Exception:
                record(device, "set-failed", current, base, offset, final, reason)
                remember(current)
                raise
            log.info("%s: set %s -> %.1f, room %s (base %.1f %+.1f, %s)",
                     name, current, final, device.room_temperature, base, offset, reason)
            record(device, "set", current, base, offset, final, reason)
            remember(final)
        except Exception:
            failures += 1
            log.exception("%s: failed", name)
    if unit_state and not dry_run:
        unit_state.save()
    return failures


async def melcloud_devices(session) -> list:
    import pymelcloud

    # Credentials come from the environment only and are never logged.
    token = await pymelcloud.login(
        os.environ["MELCLOUD_EMAIL"], os.environ["MELCLOUD_PASSWORD"], session
    )
    found = await pymelcloud.get_devices(token, session)
    return found[pymelcloud.DEVICE_TYPE_ATA]


async def list_units() -> None:
    import aiohttp

    async with aiohttp.ClientSession() as session:
        for d in await melcloud_devices(session):
            await d.update()
            print(f"{d.name}: power={d.power} mode={d.operation_mode} "
                  f"room={d.room_temperature} target={d.target_temperature} "
                  f"fan_set={d.fan_speed} fan_speeds={d.fan_speeds} fan_actual={d.actual_fan_speed} "
                  f"energy_meter={d.has_energy_consumed_meter} "
                  f"energy_kwh={d.total_energy_consumed}")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

class LocalTimeFormatter(logging.Formatter):
    """Log lines carry local (Helsinki) time, not the host's: the VM runs on UTC."""

    def __init__(self, fmt: str, tz: ZoneInfo):
        super().__init__(fmt)
        self.tz = tz

    def formatTime(self, record, datefmt=None):
        stamp = datetime.fromtimestamp(record.created, self.tz)
        return f"{stamp:%Y-%m-%d %H:%M:%S},{int(record.msecs):03d}"


def setup_logging(cfg: dict) -> None:
    fmt = LocalTimeFormatter("%(asctime)s %(levelname)s %(message)s", ZoneInfo(cfg["timezone"]))
    handlers = [
        logging.handlers.RotatingFileHandler(
            APP_DIR / cfg["log_file"],
            maxBytes=cfg["log_max_bytes"],
            backupCount=cfg["log_backup_count"],
        ),
        logging.StreamHandler(sys.stdout),
    ]
    for h in handlers:
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(logging.INFO)


def parse_now(text: Optional[str], tz: ZoneInfo) -> datetime:
    if not text:
        return datetime.now(tz)
    dt = datetime.fromisoformat(text)
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt


async def send_heartbeat(cfg: dict) -> None:
    """Ping the dead-man check (HEARTBEAT_URL) after a healthy run.

    No URL means no ping. A failed ping is only logged: the alert service notices the
    silence, and the heartbeat must never make a good run fail.
    """
    url = os.environ.get("HEARTBEAT_URL", "").strip()
    if not url:
        return
    import aiohttp

    try:
        timeout = aiohttp.ClientTimeout(total=cfg["heartbeat"]["timeout_seconds"])
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
    except Exception as exc:
        # The URL is a secret: log only the error type, never the exception text.
        log.warning("heartbeat ping failed (%s)", type(exc).__name__)


async def get_prices_or_none(cfg: dict, prices_file: Optional[Path]):
    try:
        if prices_file:
            return load_prices_file(prices_file)
        return await fetch_prices(cfg)   # each source has its own retries
    except Exception:
        log.exception("price fetch failed, using base schedule only")
        return None


PRICE_COLUMNS = ["start_utc", "minutes", "price"]


def record_prices(path: Path, prices: Optional[List[PriceInterval]]) -> int:
    """Append the intervals not yet stored to the price history; returns how many were added.

    The price feed only covers today and tomorrow, so this builds the history that the
    source does not offer. Day-ahead prices do not change, so an interval is written once:
    only those starting after the last stored one are appended. Never raises: a full disk
    or a bad file must not stop the controller.
    """
    if not prices:
        return 0
    try:
        last = None
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                lines = [ln for ln in f.read().splitlines() if ln.strip()]
            if len(lines) > 1:
                last = datetime.fromisoformat(lines[-1].split(",")[0].replace("Z", "+00:00"))
        new = [i for i in sorted(prices, key=lambda i: i.start) if last is None or i.start > last]
        if not new:
            return 0
        is_new = not path.exists() or path.stat().st_size == 0
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if is_new:
                w.writerow(PRICE_COLUMNS)
            for i in new:
                w.writerow([i.start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            round(i.seconds / 60), i.price])
        return len(new)
    except Exception as exc:
        log.warning("price history not written (%s)", type(exc).__name__)
        return 0


async def fetch_outdoor_mean(cfg: dict, day: date) -> float:
    """Forecast mean outdoor temperature (C) for a local day, from Open-Meteo."""
    import aiohttp

    w = cfg["weather"]
    params = {"latitude": w["latitude"], "longitude": w["longitude"],
              "daily": "temperature_2m_mean", "timezone": cfg["timezone"],
              "forecast_days": 2}
    timeout = aiohttp.ClientTimeout(total=w["timeout_seconds"])
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(w["url"], params=params) as resp:
            resp.raise_for_status()
            daily = (await resp.json(content_type=None))["daily"]
    return float(daily["temperature_2m_mean"][daily["time"].index(day.isoformat())])


async def fetch_outdoor_forecast(cfg: dict) -> OutdoorForecast:
    """Hourly outdoor temperature, humidity and wind for today and tomorrow, from Open-Meteo."""
    import aiohttp

    w = cfg["weather"]
    params = {"latitude": w["latitude"], "longitude": w["longitude"],
              "hourly": "temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code,is_day",
              "wind_speed_unit": "ms",
              "timezone": cfg["timezone"], "forecast_days": 2}
    timeout = aiohttp.ClientTimeout(total=w["timeout_seconds"])
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(w["url"], params=params) as resp:
            resp.raise_for_status()
            return parse_outdoor(await resp.json(content_type=None), ZoneInfo(cfg["timezone"]))


async def get_outdoor_or_none(cfg: dict, now: datetime, override: Optional[float]):
    """The outdoor weather for the price shifts: an OutdoorForecast (hourly), or if that fails the
    day's mean temperature as a plain number, or None (a neutral weather factor of 1.0).
    A --outdoor-temp override is a plain number."""
    if override is not None or not cfg["weather"]["enabled"]:
        return override
    try:
        forecast = await with_retry(lambda: fetch_outdoor_forecast(cfg), "weather fetch", cfg)
        if forecast.hours:
            return forecast
        log.warning("hourly weather forecast was empty, using the daily mean")
    except Exception:
        log.warning("hourly weather forecast failed, using the daily mean", exc_info=True)
    try:
        day = now.astimezone(ZoneInfo(cfg["timezone"])).date()
        return await with_retry(lambda: fetch_outdoor_mean(cfg, day), "weather fetch", cfg)
    except Exception:
        log.exception("weather fetch failed, using neutral weather factor")
        return None


async def fetch_sun(cfg: dict) -> SunForecast:
    """Hourly shortwave radiation forecast for today and tomorrow, from Open-Meteo."""
    import aiohttp

    w = cfg["weather"]
    params = {"latitude": w["latitude"], "longitude": w["longitude"],
              "hourly": "shortwave_radiation", "timezone": cfg["timezone"], "forecast_days": 2}
    timeout = aiohttp.ClientTimeout(total=w["timeout_seconds"])
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(w["url"], params=params) as resp:
            resp.raise_for_status()
            return parse_sun(await resp.json(content_type=None), ZoneInfo(cfg["timezone"]))


async def get_sun_or_none(cfg: dict, override: Optional[float]) -> Optional[SunForecast]:
    """None means no solar effect."""
    if override is not None:
        return SunForecast({}, constant=override)
    if not cfg["solar"]["enabled"]:
        return None
    try:
        return await with_retry(lambda: fetch_sun(cfg), "solar forecast fetch", cfg)
    except Exception:
        log.exception("solar forecast fetch failed, using no solar effect")
        return None


def away_from(args, tz: ZoneInfo, now: datetime) -> Optional[Away]:
    """CLI flags win over the AWAY file."""
    if args.away_until:
        until = datetime.fromisoformat(args.away_until)
        return Away(until if until.tzinfo else until.replace(tzinfo=tz))
    if args.away:
        return Away()
    return load_away(AWAY_FILE, tz)


# --------------------------------------------------------------------------
# Savings estimate (read-only report)
# --------------------------------------------------------------------------
# A rough, relative estimate: heat demand is taken as proportional to (setpoint - outdoor mean)
# and its cost as price x demand, for the setpoints the controller actually held versus the old
# MELCloud timer ([baseline]). No thermal memory, no heat-pump efficiency, no real usage.

def utc_quarter(dt: datetime) -> datetime:
    u = dt.astimezone(timezone.utc)
    return u.replace(minute=u.minute // 15 * 15, second=0, microsecond=0)


def _history_files(app_dir: Path, cfg: dict) -> List[Path]:
    base = Path(cfg["history_file"])
    files = sorted(app_dir.glob(f"{base.stem}-????-??{base.suffix}"))
    legacy = app_dir / base.name  # written before the monthly files existed
    return ([legacy] if legacy.exists() else []) + files


HELD_TARGET_FIELD = {"keep": "new_target", "set": "new_target", "normal-set": "new_target",
                     "normal-end": "new_target", "away-on": "new_target",
                     "hold": "old_target", "set-failed": "old_target"}


def held_target(row: dict) -> Optional[float]:
    """The target a unit really had after a history row: keep/set (and the normal-now and away
    switch-on writes) mean new_target, hold/set-failed mean it stayed at old_target; a dry run or
    any other row says nothing. Raises ValueError on a malformed number."""
    field = HELD_TARGET_FIELD.get(row.get("action"))
    return float(row[field]) if field and row.get(field) else None


def load_actual_setpoints(app_dir: Path, cfg: dict):
    """({(unit, quarter UTC): setpoint held}, {local date: outdoor mean}) from the history files.

    keep/set mean the unit is at new_target; hold/set-failed mean it stays at old_target.
    Dry runs, skips, away on/off and mode mismatches carry no usable setpoint. The last row in a
    quarter wins (manual runs add extra rows).
    """
    tz = ZoneInfo(cfg["timezone"])
    setpoints, outdoor = {}, {}
    for path in _history_files(app_dir, cfg):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    when = datetime.fromisoformat(row["time_local"])
                    if row.get("outdoor_mean"):
                        outdoor[when.astimezone(tz).date()] = float(row["outdoor_mean"])
                    target = held_target(row)
                    if target is not None:
                        setpoints[(row["unit"], utc_quarter(when))] = target
                except (KeyError, ValueError):
                    continue
    return setpoints, outdoor


def load_price_quarters(path: Path) -> Dict[datetime, float]:
    """{quarter start UTC: c/kWh} from price_history.csv; hourly rows cover four quarters."""
    out: Dict[datetime, float] = {}
    if not path.exists():
        return out
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                start = datetime.fromisoformat(row["start_utc"].replace("Z", "+00:00"))
                for k in range(max(int(row["minutes"]) // 15, 1)):
                    out[start + timedelta(minutes=15 * k)] = float(row["price"])
            except (KeyError, ValueError):
                continue
    return out


@dataclass
class DaySaving:
    quarters: int = 0     # unit-quarters with a setpoint, a price and an outdoor mean
    cost_actual: float = 0.0
    cost_baseline: float = 0.0
    heat_actual: float = 0.0
    heat_baseline: float = 0.0

    def add(self, other: "DaySaving") -> None:
        self.quarters += other.quarters
        self.cost_actual += other.cost_actual
        self.cost_baseline += other.cost_baseline
        self.heat_actual += other.heat_actual
        self.heat_baseline += other.heat_baseline

    @property
    def saving_pct(self) -> Optional[float]:
        return 100 * (1 - self.cost_actual / self.cost_baseline) if self.cost_baseline > 0 else None

    @property
    def heat_pct(self) -> Optional[float]:
        """Less (negative) or more heat demand than the baseline."""
        return 100 * (self.heat_actual / self.heat_baseline - 1) if self.heat_baseline > 0 else None

    @property
    def price_pct(self) -> Optional[float]:
        """Average price paid per unit of heat, against the baseline's (negative = cheaper)."""
        if self.heat_actual <= 0 or self.heat_baseline <= 0:
            return None
        return 100 * ((self.cost_actual / self.heat_actual) / (self.cost_baseline / self.heat_baseline) - 1)


def baseline_label(cfg: dict) -> str:
    """What the saving is measured against, as the report words it ([baseline] label)."""
    return cfg["baseline"].get("label", "the baseline schedule")


def savings_by_day(setpoints, prices, outdoor, cfg: dict) -> Dict[date, DaySaving]:
    tz = ZoneInfo(cfg["timezone"])
    baseline_cfg = {"schedule": cfg["baseline"]}
    days: Dict[date, DaySaving] = {}
    for (_unit, quarter), actual in setpoints.items():
        local = quarter.astimezone(tz)
        price, out = prices.get(quarter), outdoor.get(local.date())
        if price is None or out is None:
            continue
        base = base_setpoint(local, baseline_cfg)
        d = days.setdefault(local.date(), DaySaving())
        d.quarters += 1
        d.heat_actual += max(actual - out, 0.0)
        d.heat_baseline += max(base - out, 0.0)
        d.cost_actual += price * max(actual - out, 0.0)
        d.cost_baseline += price * max(base - out, 0.0)
    return days


# --------------------------------------------------------------------------
# Energy model: kWh and EUR from the heat pump meters (a rough estimate)
# --------------------------------------------------------------------------

def _telemetry_files(app_dir: Path, cfg: dict) -> List[Path]:
    base = Path(cfg["telemetry_file"])
    files = sorted(app_dir.glob(f"{base.stem}-????-??{base.suffix}"))
    legacy = app_dir / base.name
    return ([legacy] if legacy.exists() else []) + files


def load_telemetry(app_dir: Path, cfg: dict):
    """({unit: [(time UTC, lifetime kWh)]}, {quarter UTC: outdoor C}) from the telemetry files.

    The meter readings are sorted by time; the outdoor temperature is the mean of the rows in a
    quarter (empty for rows written before the weather columns existed)."""
    meters: Dict[str, List[Tuple[datetime, float]]] = {}
    outdoor_sum: Dict[datetime, List[float]] = {}
    for path in _telemetry_files(app_dir, cfg):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    when = datetime.fromisoformat(row["time_local"]).astimezone(timezone.utc)
                except (KeyError, ValueError):
                    continue
                if row.get("energy_kwh"):
                    try:
                        meters.setdefault(row["unit"], []).append((when, float(row["energy_kwh"])))
                    except ValueError:
                        pass
                if row.get("outdoor_c"):
                    try:
                        outdoor_sum.setdefault(utc_quarter(when), []).append(float(row["outdoor_c"]))
                    except ValueError:
                        pass
    for series in meters.values():
        series.sort()
    return meters, {q: sum(v) / len(v) for q, v in outdoor_sum.items()}


def meter_energy_points(readings: List[Tuple[datetime, float]]) -> List[Tuple[datetime, float]]:
    """Cumulative energy (kWh since the first reading) at each moment the meter's value CHANGED.

    The meter updates only every 1.5 to 3 hours, so only the changes carry information; a drop in
    the value (a reset) adds nothing and the count continues from the new value."""
    points: List[Tuple[datetime, float]] = []
    last, total = None, 0.0
    for when, value in readings:
        if last is None:
            points.append((when, 0.0))
        elif value != last:
            total += max(value - last, 0.0)
            points.append((when, total))
        last = value
    return points


def _cumulative_at(points: List[Tuple[datetime, float]], when: datetime) -> float:
    if when <= points[0][0]:
        return points[0][1]
    if when >= points[-1][0]:
        return points[-1][1]
    for (t0, c0), (t1, c1) in zip(points, points[1:]):
        if t0 <= when <= t1:
            return c0 + (c1 - c0) * (when - t0).total_seconds() / max((t1 - t0).total_seconds(), 1e-9)
    return points[-1][1]


def total_meter_energy(meters: Dict[str, List[Tuple[datetime, float]]]) -> List[Tuple[datetime, float]]:
    """The sum of all units' meters as cumulative kWh at the moments any of them changed, over the
    time every meter has data for. The user's assumption, to be checked against the data: the
    outdoor unit's draw is counted on one meter and the other counts only its indoor unit, so the
    sum is the total."""
    per_unit = [meter_energy_points(r) for r in meters.values() if len(r) >= 2]
    per_unit = [p for p in per_unit if len(p) >= 2]
    if not per_unit:
        return []
    lo, hi = max(p[0][0] for p in per_unit), min(p[-1][0] for p in per_unit)
    times = sorted({t for p in per_unit for t, _ in p if lo <= t <= hi} | {lo, hi})
    return [(t, sum(_cumulative_at(p, t) for p in per_unit)) for t in times] if lo < hi else []


def allocate_energy(total: List[Tuple[datetime, float]], weight) -> Dict[datetime, float]:
    """Spread each interval's energy over its quarter hours in proportion to weight(quarter) times
    the part of the quarter the interval covers (equally, when every weight is zero)."""
    out: Dict[datetime, float] = {}
    for (t0, c0), (t1, c1) in zip(total, total[1:]):
        energy = c1 - c0
        quarter, shares = utc_quarter(t0), []
        while quarter < t1:
            covered = (min(quarter + timedelta(minutes=15), t1) - max(quarter, t0)).total_seconds() / 900
            if covered > 0:
                shares.append((quarter, covered))
            quarter += timedelta(minutes=15)
        weights = [(q, weight(q) * c) for q, c in shares]
        if sum(w for _, w in weights) <= 0:
            weights = list(shares)
        total_w = sum(w for _, w in weights)
        for q, w in weights:
            out[q] = out.get(q, 0.0) + energy * w / total_w
    return out


@dataclass
class EnergyDay:
    quarters: int = 0          # quarter hours with a setpoint, a price and an outdoor temperature
    kwh_model: float = 0.0     # what the model says the heat pumps used
    kwh_baseline: float = 0.0  # ... and would have used under the baseline ([baseline])
    eur_model: float = 0.0
    eur_baseline: float = 0.0
    measured_quarters: int = 0
    kwh_measured: float = 0.0  # the meters, spread over the quarters they cover
    eur_measured: float = 0.0

    def add(self, o: "EnergyDay") -> None:
        for f in ("quarters", "kwh_model", "kwh_baseline", "eur_model", "eur_baseline",
                  "measured_quarters", "kwh_measured", "eur_measured"):
            setattr(self, f, getattr(self, f) + getattr(o, f))

    @property
    def saved_kwh(self) -> float:
        return self.kwh_baseline - self.kwh_model

    @property
    def saved_eur(self) -> float:
        return self.eur_baseline - self.eur_model

    @property
    def saved_pct(self) -> Optional[float]:
        return 100 * self.saved_eur / self.eur_baseline if self.eur_baseline > 0 else None

    @property
    def less_heat_eur(self) -> float:
        """The saving from using less energy, valued at the baseline's average price."""
        return self.saved_kwh * (self.eur_baseline / self.kwh_baseline) if self.kwh_baseline > 0 else 0.0

    @property
    def cheaper_hours_eur(self) -> float:
        """The rest: the same energy bought in cheaper hours."""
        return self.saved_eur - self.less_heat_eur


def demand_quarters(setpoints, outdoor_q, outdoor_day, cfg: dict):
    """{quarter: (demand now, demand under the baseline)} summed over the units, in degree-quarters:
    setpoint minus outdoor temperature (never below 0). The outdoor temperature is the telemetry's
    for that quarter when there is one, else the day's mean from the history file."""
    tz = ZoneInfo(cfg["timezone"])
    baseline_cfg = {"schedule": cfg["baseline"]}
    out: Dict[datetime, List[float]] = {}
    for (unit, quarter), actual in setpoints.items():
        local = quarter.astimezone(tz)
        temp = outdoor_q.get(quarter)
        if temp is None:
            temp = outdoor_day.get(local.date())
        if temp is None:
            continue
        base = base_setpoint(local, baseline_cfg)
        cur = out.setdefault(quarter, [0.0, 0.0])
        cur[0] += max(actual - temp, 0.0)
        cur[1] += max(base - temp, 0.0)
    return {q: (v[0], v[1]) for q, v in out.items()}


@dataclass
class EnergyModel:
    k: float                    # kWh per degree-quarter (all units together)
    days_used: int
    spread_pct: Optional[float]  # how far the days stray from the fit, on average
    units: int


def fit_energy_model(demand, measured: Dict[datetime, float], tz: ZoneInfo, units: int) -> Optional[EnergyModel]:
    """kWh per degree-quarter from the metered quarters (ratio of sums). Needs metered quarters that
    also have a demand; the spread over local days says how far to trust it."""
    both = [q for q in measured if q in demand and demand[q][0] > 0]
    if not both:
        return None
    total_d = sum(demand[q][0] for q in both)
    if total_d <= 0:
        return None
    k = sum(measured[q] for q in both) / total_d
    per_day: Dict[date, List[float]] = {}
    for q in both:
        cell = per_day.setdefault(q.astimezone(tz).date(), [0.0, 0.0])
        cell[0] += measured[q]
        cell[1] += demand[q][0]
    full = [kwh / d for kwh, d in per_day.values() if d > 0 and len(both) > 0]
    spread = 100 * sum(abs(r / k - 1) for r in full) / len(full) if len(full) >= 2 and k > 0 else None
    return EnergyModel(k, len(per_day), spread, units)


def energy_by_day(demand, measured, prices, model: EnergyModel, tz: ZoneInfo) -> Dict[date, EnergyDay]:
    days: Dict[date, EnergyDay] = {}
    for q, (now_d, base_d) in demand.items():
        price = prices.get(q)
        if price is None:
            continue
        day = days.setdefault(q.astimezone(tz).date(), EnergyDay())
        day.quarters += 1
        day.kwh_model += model.k * now_d
        day.kwh_baseline += model.k * base_d
        day.eur_model += model.k * now_d * price / 100
        day.eur_baseline += model.k * base_d * price / 100
        if q in measured:
            day.measured_quarters += 1
            day.kwh_measured += measured[q]
            day.eur_measured += measured[q] * price / 100
    return days


def energy_report(app_dir: Path, cfg: dict, month: str = "") -> Optional[str]:
    """The kWh and EUR report, or None when there is not yet enough meter data to calibrate it."""
    tz = ZoneInfo(cfg["timezone"])
    setpoints, outdoor_day = load_actual_setpoints(app_dir, cfg)
    meters, outdoor_q = load_telemetry(app_dir, cfg)
    prices = load_price_quarters(app_dir / cfg["price_history_file"])
    total = total_meter_energy(meters)
    demand = demand_quarters(setpoints, outdoor_q, outdoor_day, cfg)
    if len(total) < 2 or not demand:
        return None
    measured = allocate_energy(total, lambda q: demand.get(q, (0.0, 0.0))[0])
    units = max(len({u for (u, _q) in setpoints}), 1)
    model = fit_energy_model(demand, measured, tz, units)
    if model is None:
        return None
    days = energy_by_day(demand, measured, prices, model, tz)
    if not days:
        return None
    watts = model.k * 4 * 1000                  # electrical W per degree of (setpoint - outdoor), per unit
    lines = [
        f"Energy and cost against {baseline_label(cfg)}: a ROUGH ESTIMATE.",
        f"Calibrated from the heat pump meters: {model.k * 1000:.2f} Wh per unit-degree-quarter, i.e. about",
        f"{watts:.1f} W per degree of (setpoint - outdoor) per unit, from {model.days_used} day(s)"
        + (f"; the days stray {model.spread_pct:.0f}% from the fit." if model.spread_pct is not None
           else "; too few days to judge the fit."),
        "The model takes heat demand as (setpoint - outdoor) and has no thermal memory or efficiency curve;",
        "'measured' is the meters spread over the quarter hours they cover (they update every 1.5 to 3 h).",
        "Saved = baseline - actual, both from the model; positive = cheaper than the baseline.", ""]
    months: Dict[str, EnergyDay] = {}
    for d, s in sorted(days.items()):
        months.setdefault(f"{d:%Y-%m}", EnergyDay()).add(s)

    def row(label, s, n_days):
        cover = 100 * s.measured_quarters / max(s.quarters, 1)
        pct = f"{s.saved_pct:+6.1f}%" if s.saved_pct is not None else "    n/a"
        return (f"{label:10} {n_days:>3} {cover:5.0f}% {s.kwh_measured:8.1f} {s.eur_measured:7.2f} "
                f"{s.kwh_model:8.1f} {s.kwh_baseline:8.1f} {s.saved_kwh:+8.1f} {s.eur_baseline:7.2f} "
                f"{s.saved_eur:+7.2f} {pct}")

    head = (f"{'':10} {'d':>3} {'meter':>6} {'meas kWh':>8} {'meas EUR':>7} {'mod kWh':>8} {'base kWh':>8} "
            f"{'saved':>8} {'base EUR':>7} {'saved EUR':>7} {'saved%':>7}")
    lines.append(head)
    for m, s in months.items():
        lines.append(row(m, s, sum(1 for d in days if f"{d:%Y-%m}" == m)))
        lines.append(f"{'':10} of which less heat {s.less_heat_eur:+.2f} EUR, cheaper hours {s.cheaper_hours_eur:+.2f} EUR")
    shown = [d for d in sorted(days) if f"{d:%Y-%m}" == month] if month else sorted(days)[-7:]
    if month and not shown:
        lines += ["", f"No data for {month}."]
    elif shown:
        lines += ["", head]
        for d in shown:
            lines.append(row(f"{d:%Y-%m-%d}", days[d], 1))
    return "\n".join(lines)


def savings_report(app_dir: Path, cfg: dict, month: str = "") -> str:
    """Text report: a monthly roll-up, plus the days of one month (or the last 7 days)."""
    setpoints, outdoor = load_actual_setpoints(app_dir, cfg)
    prices = load_price_quarters(app_dir / cfg["price_history_file"])
    days = savings_by_day(setpoints, prices, outdoor, cfg)
    if not days:
        return ("No days to compare yet: the report needs live rows in the history files, prices in "
                f"{cfg['price_history_file']} and an outdoor mean for the same quarter-hours.")
    units = max(len({u for (u, _q) in setpoints}), 1)
    fmt = lambda v: f"{v:+6.1f}%" if v is not None else "     n/a"
    lines = [f"Estimated saving against {baseline_label(cfg)}. A ROUGH, RELATIVE estimate: heat demand is",
             "taken as (setpoint - outdoor mean) and cost as price x demand. It has no thermal memory or",
             "heat-pump efficiency and does not use real consumption. Positive saving = cheaper than the baseline.",
             "Away, holiday and home days count as they ran. Dry runs are excluded.", ""]
    months: Dict[str, DaySaving] = {}
    for d, s in sorted(days.items()):
        months.setdefault(f"{d:%Y-%m}", DaySaving()).add(s)
    lines.append(f"{'month':8} {'days':>4} {'coverage':>8} {'saving':>7} {'less heat':>9} {'price paid':>10}")
    for m, s in months.items():
        n = sum(1 for d in days if f"{d:%Y-%m}" == m)
        lines.append(f"{m:8} {n:4d} {100 * s.quarters / (n * 96 * units):7.0f}% {fmt(s.saving_pct)} "
                     f"{fmt(s.heat_pct)} {fmt(s.price_pct)}")
    shown = [d for d in sorted(days) if f"{d:%Y-%m}" == month] if month else sorted(days)[-7:]
    if month and not shown:
        lines += ["", f"No data for {month}."]
    elif shown:
        lines += ["", f"{'day':10} {'coverage':>8} {'saving':>7} {'less heat':>9} {'price paid':>10}"]
        for d in shown:
            s = days[d]
            lines.append(f"{d:%Y-%m-%d} {100 * s.quarters / (96 * units):7.0f}% {fmt(s.saving_pct)} "
                         f"{fmt(s.heat_pct)} {fmt(s.price_pct)}")
    return "\n".join(lines)


async def run(args) -> int:
    cfg = load_config(Path(args.config), Path(args.config).with_name(LOCAL_CONFIG))
    tz = ZoneInfo(cfg["timezone"])
    if args.list:
        await list_units()
        return 0
    if args.report is not None:
        data = Path(getattr(args, "data_dir", None) or APP_DIR)    # e.g. a folder of files copied from the VM
        text = energy_report(data, cfg, args.report)
        if text is None:  # no usable meter data yet: the relative estimate in percent
            text = savings_report(data, cfg, args.report) + (
                "\n\n(No kWh/EUR yet: the telemetry files need a few days of meter readings "
                "that overlap the history files.)")
        print(text)
        return 0
    if args.home:
        return set_home(args.home, tz)
    if args.season:
        return set_season(args.season)

    if args.control:
        name, value = args.control
        if name not in CONTROLS:
            print(f"--control NAME is one of {', '.join(CONTROLS)}, not {name!r}")
            return 2
        return set_control(name, value, tz, cfg)

    if args.plan or args.plan_json:
        now = parse_now(args.now, tz)
        prices = await get_prices_or_none(cfg, args.prices_file)
        outdoor = await get_outdoor_or_none(cfg, now, args.outdoor_temp)
        sun = await get_sun_or_none(cfg, args.sun)
        show = plan_json if args.plan_json else plan_table
        # HOME, HOLIDAY and the nudge as they stand now (tomorrow's plan runs with `now` at tomorrow
        # noon), each applied to the quarters before its end
        real_now = min(now, datetime.now(tz))
        home = args.assume_home or holiday_active(HOME_FILE, tz, real_now)
        holiday = args.holiday or holiday_active(HOLIDAY_FILE, tz, real_now)
        home_until = read_until_file(HOME_FILE, "HOME", tz)[1]
        holiday_until = read_until_file(HOLIDAY_FILE, "HOLIDAY", tz)[1]
        print(show(now, prices, cfg, outdoor, away_from(args, tz, real_now), args.mode or read_season(SEASON_FILE),
                   holiday, sun, home, read_nudge(NUDGE_FILE, tz, real_now),
                   None if args.assume_home else home_until, None if args.holiday else holiday_until))
        return 0

    setup_logging(cfg)
    lock = acquire_run_lock(LOCK_FILE, args.lock_wait)
    if lock is None:
        log.info("another run is in progress, skipping this one")
        # cron (no wait) skips quietly; a run that waited (the page's) reports it, so the page can say
        # the change takes effect at the next run instead of claiming the units were updated
        return RUN_SKIPPED if args.lock_wait else 0
    try:
        return await run_live(args, cfg, tz)
    finally:
        lock.close()  # not left to the garbage collector: a held traceback would keep the lock


async def settle_for_pause(args, cfg: dict, tz: ZoneInfo) -> int:
    """PAUSE says `settle`: one flat write of [pause] heat_temp (cool: the cooling setpoint) to
    each unit, with no price, weather, sun, nudge or per-unit offset, then the file becomes a plain
    PAUSE and the controller leaves the units alone. A failed write is retried by the next run."""
    now = parse_now(args.now, tz)
    flat_heat = cfg["pause"]["heat_temp"]
    flat = {**cfg, "schedule": {"weekday": [["00:00", flat_heat]], "weekend": [["00:00", flat_heat]]},
            "units": {**cfg["units"], "offsets": {}, "cool_offsets": {}}}
    import aiohttp

    try:
        async with aiohttp.ClientSession() as session:
            devices = await with_retry(lambda: melcloud_devices(session), "MELCloud login", cfg)
            failures = await apply_to_units(
                devices, now, None, flat, args.dry_run, None, None, None,
                UnitState(APP_DIR / cfg["unit_state_file"]), None, False, None, False)
    except Exception:
        log.exception("could not settle the units for PAUSE")
        return 1
    if failures:
        return 1
    if not args.dry_run:
        PAUSE_FILE.write_text("", encoding="utf-8")
        log.info("PAUSE: units set to a safe temperature, hands off from now on")
    await send_heartbeat(cfg)
    return 0


async def run_live(args, cfg: dict, tz: ZoneInfo) -> int:
    """One live run: read the control files and prices, then steer the units."""
    if PAUSE_FILE.exists():
        if PAUSE_FILE.read_text(encoding="utf-8").strip() == "settle":
            return await settle_for_pause(args, cfg, tz)
        log.info("PAUSE file present, doing nothing")
        await send_heartbeat(cfg)  # paused on purpose, not dead
        return RUN_SKIPPED if args.lock_wait else 0
    breaker = make_breaker(cfg)
    open_until = breaker.open_until(datetime.now(timezone.utc))
    if open_until:
        log.warning("circuit breaker open until %s, skipping this run",
                    open_until.astimezone(tz).isoformat(timespec="minutes"))
        return RUN_SKIPPED if args.lock_wait else 0

    now = parse_now(args.now, tz)
    if not args.dry_run:
        remove_if_over(AWAY_FILE, "AWAY", tz)
    away = away_from(args, tz, now)
    if away and away.until is not None and now >= away.until:
        away = None   # over by the run's time (a --now test): ignored, the file stays
    if away:
        state = "flat" if away.active_at(now, cfg) else "ending: pre-heat or expired"
        log.info("AWAY (%s), return %s", state, away.until.astimezone(tz).isoformat(timespec="minutes") if away.until else "not set")
    season = read_season(SEASON_FILE)
    normal = read_normal(NORMAL_FILE, tz, now, cfg)
    if normal:
        log.info("NORMAL until %s: going to %.1f C", normal["until"].astimezone(tz).strftime("%H:%M"), normal["temp"])
    nudge = read_nudge(NUDGE_FILE, tz, now)
    if nudge:
        log.info("NUDGE floor +%.1f, shift %+.1f C until %s", nudge.floor, nudge.shift,
                 nudge.until.astimezone(tz).strftime("%H:%M"))
    holiday = args.holiday or holiday_active(HOLIDAY_FILE, tz, now)
    home = args.assume_home or holiday_active(HOME_FILE, tz, now)
    if SEASON_FILE.exists() or holiday or home:
        log.info("SEASON %s, HOLIDAY %s, HOME %s", season,
                 "on" if holiday else "off", "on" if home else "off")
    prices = await get_prices_or_none(cfg, args.prices_file)
    if not args.prices_file:  # a test file must not pollute the real history
        record_prices(APP_DIR / cfg["price_history_file"], prices)
    outdoor = await get_outdoor_or_none(cfg, now, args.outdoor_temp)
    sun = await get_sun_or_none(cfg, args.sun)

    import aiohttp

    try:
        async with aiohttp.ClientSession() as session:
            devices = await with_retry(lambda: melcloud_devices(session), "MELCloud login", cfg)
            state = UnitState(APP_DIR / cfg["unit_state_file"])
            failures = await apply_to_units(
                devices, now, prices, cfg, args.dry_run, outdoor,
                monthly_path(APP_DIR / cfg["history_file"], now, tz), away,
                state, season, holiday, sun, home,
                telemetry=monthly_path(APP_DIR / cfg["telemetry_file"], now, tz),
                nudge=nudge, normal=normal)
            if state.data.pop("normal_end", None) or (normal is None and "normal" not in state.data):
                NORMAL_FILE.unlink(missing_ok=True)  # the override is over: remove its file
                if not args.dry_run:   # a dry run changes nothing it remembers
                    state.save()
    except Exception:
        # Under cron stderr goes nowhere, so a login failure must reach the log file.
        log.exception("run failed")
        breaker.record_failure(datetime.now(timezone.utc))
        return 1
    # Per-unit failures do not trip the breaker: it guards login and connectivity.
    breaker.record_success()
    if not failures:
        await send_heartbeat(cfg)
    return 1 if failures else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(APP_DIR / "config.toml"))
    ap.add_argument("--list", action="store_true", help="print MELCloud units")
    ap.add_argument("--plan", action="store_true", help="print the day's plan, touch nothing")
    ap.add_argument("--plan-json", action="store_true",
                    help="like --plan, as JSON with the price of each quarter hour (for the remote page)")
    ap.add_argument("--prices-file", type=Path, help="read prices from a file instead of the API")
    ap.add_argument("--now", help="ISO time to plan for (naive = local)")
    ap.add_argument("--outdoor-temp", type=float,
                    help="outdoor daily mean in C instead of fetching the forecast")
    ap.add_argument("--sun", type=float, metavar="W_M2",
                    help="solar radiation in W/m2 for the whole day instead of fetching the forecast")
    ap.add_argument("--mode", choices=["heat", "cool"],
                    help="mode to plan for with --plan; default: the season (SEASON file, heat without one), as live runs")
    ap.add_argument("--season", choices=["heat", "cool"],
                    help="write the SEASON file and exit: all units are switched to this mode at "
                         "the next run (no file = heat)")
    ap.add_argument("--home", metavar="WHEN",
                    help="write the HOME file and exit: someone is home (sick, working from home, "
                         "school closed) until today / week (end of Sunday) / an ISO date-time; "
                         "'off' removes it")
    ap.add_argument("--control", nargs=2, metavar=("NAME", "VALUE"),
                    help="write or remove a control file and exit: away|holiday on|off|ISO, "
                         "pause on|hold|off, nudge warmer|cooler|longer|reset (see also --home, --season)")
    ap.add_argument("--lock-wait", type=float, default=0.0, metavar="SECONDS",
                    help="when another run is in progress, wait this long for it instead of "
                         "skipping this run (cron skips; a person or a page waits)")
    ap.add_argument("--assume-home", action="store_true",
                    help="plan or run as if the HOME file existed")
    ap.add_argument("--holiday", action="store_true",
                    help="weekend schedule every day (the HOLIDAY file does the same)")
    ap.add_argument("--away", action="store_true",
                    help="away with no return time (the AWAY file does the same)")
    ap.add_argument("--away-until", metavar="ISO",
                    help="away until this local date-time, with pre-heat before it")
    ap.add_argument("--report", nargs="?", const="", metavar="YYYY-MM",
                    help="print the estimated saving against the [baseline] schedule, per month and per day, "
                         "and exit (read-only; no network); a month shows its days")
    ap.add_argument("--data-dir", metavar="DIR",
                    help="with --report: read the history, telemetry and price files from this folder "
                         "(for example files copied from the VM) instead of the app folder")
    ap.add_argument("--dry-run", action="store_true", help="log decisions, no writes")
    return asyncio.run(run(ap.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
