import asyncio
import csv
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import ilp_optimizer as o
import web

ROOT = Path(__file__).resolve().parent.parent
CFG = o.load_config(ROOT / "tests" / "logic_config.toml")     # plain rules: no cheap_all, scale -10..+10 = 1.5..0.5
LIVE = o.load_config(ROOT / "config.toml", ROOT / "tests" / "house.toml")
FI_PAGE = web.render_page({**LIVE, "web": {**LIVE["web"], "language": "fi"}})   # these tests prove the Finnish text
TZ = ZoneInfo("Europe/Helsinki")
DAY = date(2026, 11, 10)


def forecast(temps_by_local_hour, rh=80.0, wind=3.0):
    """An OutdoorForecast for DAY from {local hour: temp}; other hours are left out."""
    hours = {}
    for h, t in temps_by_local_hour.items():
        hours[datetime(2026, 11, 10, h, tzinfo=TZ).astimezone(timezone.utc)] = (t, rh, wind, 3, 1)
    return o.OutdoorForecast(hours)


def at(h, m=0):
    return datetime(2026, 11, 10, h, m, tzinfo=TZ)


def test_parse_outdoor_reads_open_meteo_hourly_arrays():
    payload = {"hourly": {"time": ["2026-11-10T00:00", "2026-11-10T01:00", "2026-11-10T02:00"],
                          "temperature_2m": [4.0, None, 2.0], "relative_humidity_2m": [90, 88, 85],
                          "wind_speed_10m": [1.5, 2.0, None], "weather_code": [71, 3, 73], "is_day": [0, 0, 1]}}
    f = o.parse_outdoor(payload, TZ)
    assert sorted(v[0] for v in f.hours.values()) == [2.0, 4.0]          # a missing temperature is skipped
    assert f.sample(at(2)) == {"temp": 2.0, "humidity": 85.0, "wind": None, "code": 73}
    assert f.code_at(at(0, 40)) == (71, 0) and f.code_at(at(2)) == (73, 1) and f.code_at(at(5)) == (None, None)
    assert f.at(at(0)) == 4.0


def test_the_temperature_is_interpolated_between_hours_and_held_at_the_ends():
    f = forecast({10: 0.0, 11: 4.0})
    assert f.at(at(10, 30)) == pytest.approx(2.0) and f.at(at(10, 15)) == pytest.approx(1.0)
    assert f.at(at(11, 45)) == 4.0                    # after the last known hour: its value
    assert f.at(at(5)) is None                        # nothing near it
    assert o.outdoor_temp_at(7.5, at(5)) == 7.5 and o.outdoor_temp_at(None, at(5)) is None


def test_day_mean_uses_the_local_day():
    f = forecast({0: 2.0, 12: 4.0, 23: 6.0})
    assert f.day_mean(DAY, TZ) == pytest.approx(4.0) and f.day_mean(DAY + timedelta(days=1), TZ) is None


def day_prices():
    start, _ = o.local_day_bounds(DAY, TZ)
    cheap = {2, 3, 4, 13, 14, 15}                     # three cheap hours at night, three in the afternoon
    return [o.PriceInterval(start + timedelta(minutes=15 * k), start + timedelta(minutes=15 * (k + 1)),
                            3.0 if k // 4 in cheap else 10.0) for k in range(96)]


def test_the_weather_factor_follows_the_hour_not_the_day():
    f = forecast({h: (-10.0 if h < 8 else 10.0) for h in range(24)})        # cold night, mild day
    prices = day_prices()
    night = o.desired_setpoint(at(3), prices, CFG, 0.0, f)
    noon = o.desired_setpoint(at(14), prices, CFG, 0.0, f)
    assert night[1] == pytest.approx(CFG["prices"]["boost"] * 1.5) and "x1.50" in night[3]
    assert noon[1] == pytest.approx(CFG["prices"]["boost"] * 0.5) and "x0.50" in noon[3]
    # a plain day mean of 0 C would have given both the same factor
    mean = o.desired_setpoint(at(3), prices, CFG, 0.0, 0.0)
    assert mean[1] == o.desired_setpoint(at(14), prices, CFG, 0.0, 0.0)[1]


def test_a_plain_number_still_scales_the_whole_day_as_before():
    prices = day_prices()
    got = o.desired_setpoint(at(3), prices, CFG, 0.0, -10.0)
    assert got[1] == pytest.approx(CFG["prices"]["boost"] * 1.5) and "x1.50" in got[3]


def test_extreme_and_peak_rules_ignore_the_weather_either_way():
    start, _ = o.local_day_bounds(DAY, TZ)
    prices = [o.PriceInterval(start + timedelta(hours=k), start + timedelta(hours=k + 1), 60.0 if k == 12 else 8.0)
              for k in range(24)]
    cold, mild = forecast({h: -15.0 for h in range(24)}), forecast({h: 15.0 for h in range(24)})
    a = o.desired_setpoint(at(12), prices, CFG, 0.0, cold)
    b = o.desired_setpoint(at(12), prices, CFG, 0.0, mild)
    assert a[1] == b[1] == -CFG["extreme"]["cut"] and a[3].startswith("peak price")


# ---- fetching with fallbacks -------------------------------------------------------------------

def run_get(monkeypatch, hourly, daily, override=None):
    async def fh(cfg):
        if isinstance(hourly, Exception):
            raise hourly
        return hourly

    async def fd(cfg, day):
        if isinstance(daily, Exception):
            raise daily
        return daily
    monkeypatch.setattr(o, "fetch_outdoor_forecast", fh)
    monkeypatch.setattr(o, "fetch_outdoor_mean", fd)
    return asyncio.run(o.get_outdoor_or_none(CFG, at(12), override))


def test_the_chain_hourly_then_daily_mean_then_neutral(monkeypatch):
    f = forecast({12: 3.0})
    assert run_get(monkeypatch, f, 9.9) is f
    assert run_get(monkeypatch, OSError("down"), 5.5) == 5.5                  # hourly failed: daily mean
    assert run_get(monkeypatch, o.OutdoorForecast({}), 5.5) == 5.5            # empty hourly: daily mean
    assert run_get(monkeypatch, OSError("down"), OSError("down too")) is None  # neutral factor
    assert run_get(monkeypatch, f, 9.9, override=-3.0) == -3.0                # --outdoor-temp wins


# ---- what gets logged --------------------------------------------------------------------------

class Unit:
    def __init__(self, name):
        self.name, self.operation_mode, self.target_temperature = name, "heat", 20.0
        self.power, self.room_temperature, self.fan_speed, self.fan_speeds = True, 20.5, "auto", ["auto", "1"]

    async def update(self):
        pass

    async def set(self, props):
        self.target_temperature = props.get("target_temperature", self.target_temperature)


def test_telemetry_and_history_carry_the_weather(tmp_path):
    f = forecast({12: 2.0, 13: 4.0}, rh=87.4, wind=3.26)
    hist, tel = tmp_path / "h.csv", tmp_path / "t.csv"
    asyncio.run(o.apply_to_units([Unit("a")], at(12, 30), None, LIVE, False, f, hist, None, None, "heat",
                                 telemetry=tel))
    row = list(csv.DictReader(open(tel)))[0]
    assert (row["outdoor_c"], row["humidity"], row["wind_ms"], row["weather_code"]) == ("3.0", "87", "3.3", "3")
    assert list(csv.DictReader(open(hist)))[0]["outdoor_mean"] == "3.0"       # the history keeps the day's mean


def test_an_old_telemetry_file_is_migrated_not_corrupted(tmp_path):
    old_cols = o.TELEMETRY_COLUMNS[:9]
    tel = tmp_path / "t.csv"
    tel.write_text(",".join(old_cols) + "\n2026-11-10T11:00:00+02:00,a,20.5,20.0,True,heat,1,100.0,True\n")
    asyncio.run(o.apply_to_units([Unit("a")], at(12), None, LIVE, False, 3.0, None, None, None, "heat", telemetry=tel))
    rows = list(csv.DictReader(open(tel)))
    assert list(rows[0].keys()) == o.TELEMETRY_COLUMNS
    assert rows[0]["room_temp"] == "20.5" and rows[0]["outdoor_c"] == ""      # old row intact, new cells blank
    assert rows[1]["outdoor_c"] == "3.0"                                      # a plain number is logged as the temperature


# ---- the plan output ---------------------------------------------------------------------------

def test_plan_table_and_json_with_an_hourly_forecast():
    f = forecast({h: float(h - 12) for h in range(24)})
    prices = day_prices()
    text = o.plan_table(at(12), prices, CFG, f)
    assert "outdoor mean: -0.5" in text and "hourly -12.0 to 11.0" in text and "weather factor:" in text
    raw = json.loads(o.plan_json(at(12), prices, CFG, f))
    assert len(raw["rows"]) == 96 and raw["rows"][12 * 4]["outdoor"] == 0.0 and raw["outdoor_mean"] == pytest.approx(-0.5)
    s = web.summarise_plan(raw)
    assert s["has_outdoor"] and len(s["outdoor"]) == 96 and s["now"]["outdoor"] == 0.0
    assert s["weather"][12 * 4] == 3 and s["is_day"][12 * 4] == 1 and s["now"]["weather"] == 3
    plain = json.loads(o.plan_json(at(12), prices, CFG, None))
    assert web.summarise_plan(plain)["has_outdoor"] is False


def test_the_page_shows_the_outdoor_temperature_now_and_a_strip_in_the_chart():
    html = FI_PAGE
    for text in ('id="outNow"', 'id="outSym"', "Ulkolämpötila", "c-out", "p.has_outdoor", "n.outdoor", "WX", "c-wx"):
        assert text in html


@pytest.mark.skipif(__import__("shutil").which("node") is None, reason="node is not installed")
def test_the_page_maps_weather_codes_to_symbols_and_finnish_text():
    import re
    import subprocess
    html = FI_PAGE
    wx = re.search(r"// WMO weather codes.*?\n};\n", html, re.S).group(0)
    script = re.search(r"const T = .*;\n", html).group(0) + wx + """
const cases = [[0,1],[0,0],[1,1],[2,1],[3,0],[45,1],[53,1],[61,1],[71,1],[75,1],[85,1],[95,1],[99,1],[null,1],[123,1]];
console.log(JSON.stringify(cases.map(([c,d]) => WX(c,d))));
"""
    out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    assert out[0] == ["☀️", "Selkeää"] and out[1] == ["🌙", "Selkeää"] and out[2][1] == "Enimmäkseen selkeää"
    assert out[3] == ["⛅", "Puolipilvistä"] and out[4] == ["☁️", "Pilvistä"] and out[5][1] == "Sumua"
    assert out[7][1] == "Sadetta" and out[8] == ["❄️", "Heikkoa lumisadetta"] and out[9] == ["🌨️", "Lumisadetta"]
    assert out[11][0] == "⛈️" and out[12][0] == "⛈️" and out[13] is None and out[14][1] == "Sää"
