"""The too-cold / too-hot buttons end to end, in the states the house can be in: a press, then the
controller run the page starts, then what each unit is given. Every press must do something visible,
or say why not."""
import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

import ilp_optimizer as o
import web

ROOT = Path(__file__).resolve().parent.parent
CFG = o.load_config(ROOT / "config.toml", ROOT / "tests" / "house.toml")
CFG = {**CFG, "web": {**CFG["web"], "language": "fi"}}   # these tests prove the Finnish text
TZ = o.ZoneInfo(CFG["timezone"])
TUE = date(2026, 11, 10)


def at(hh, mm=0):
    return datetime(TUE.year, TUE.month, TUE.day, hh, mm, tzinfo=TZ)


def prices(quarters, default=8.0):
    start, _ = o.local_day_bounds(TUE, TZ)
    return [o.PriceInterval(start + timedelta(minutes=15 * k), start + timedelta(minutes=15 * (k + 1)),
                            quarters.get(k, default)) for k in range(96)]


class Unit:
    def __init__(self, name, target=20.0, room=20.0, mode="heat"):
        self.name, self.operation_mode, self.target_temperature = name, mode, target
        self.power, self.room_temperature, self.writes = True, room, []

    async def update(self):
        pass

    async def set(self, props):
        self.writes.append(props)
        self.target_temperature = props.get("target_temperature", self.target_temperature)


class House:
    """Both units, their memory and the history file, run the way cron and the page run them."""

    def __init__(self, p, away=None, season="heat"):
        self.units = [Unit("Unit A"), Unit("Unit B")]
        self.state = o.UnitState(o.APP_DIR / "units.json")
        self.p, self.away, self.season = p, away, season

    def run(self, now):
        asyncio.run(o.apply_to_units(
            self.units, now, self.p, CFG, False, 5.0, o.monthly_path(o.APP_DIR / CFG["history_file"], now, TZ),
            self.away, self.state, self.season, nudge=o.read_nudge(o.NUDGE_FILE, TZ, now)))
        return {u.name: u.target_temperature for u in self.units}


def press(value, now):
    return web.do_action("nudge", value, False, CFG, now)


EVENING_DEAR = {k: 24.0 for k in range(76, 80)}       # 19-20 at 24c: comfort cut capped at 1 C


# ---- comfort hours at the 22 C maximum --------------------------------------------------------

def test_too_cold_in_comfort_hours_at_22_raises_both_units_above_it():
    house = House(prices({}))
    assert house.run(at(19)) == {"Unit A": 22.0, "Unit B": 22.0}
    press("warmer", at(19, 1))
    assert house.run(at(19, 1)) == {"Unit A": 23.0, "Unit B": 23.0}


def test_every_press_is_felt_up_to_the_limit_and_then_says_so():
    house = House(prices({}))
    house.run(at(19))
    press("warmer", at(19, 1))
    house.run(at(19, 1))
    press("warmer", at(19, 2))
    assert house.run(at(19, 2)) == {"Unit A": 24.0, "Unit B": 24.0}
    assert "enimmilleen" in press("warmer", at(19, 3))
    assert house.run(at(19, 3)) == {"Unit A": 24.0, "Unit B": 24.0}


# ---- a saving quarter: lift the floor, leave the rest of the plan alone ------------------------

def test_too_cold_in_a_saving_quarter_lifts_the_floor_only():
    house = House(prices(EVENING_DEAR))
    assert house.run(at(19)) == {"Unit A": 21.0, "Unit B": 21.0}          # 22 - 1 (comfort cap)
    assert "kevennetään" in press("warmer", at(19, 1))
    assert house.run(at(19, 1)) == {"Unit A": 22.0, "Unit B": 22.0}       # the cut is gone
    assert house.run(at(20, 30)) == {"Unit A": 22.0, "Unit B": 22.0}      # uncut later: not raised


def test_a_second_press_once_the_floor_reaches_the_plan_raises_the_plan():
    house = House(prices(EVENING_DEAR))
    house.run(at(19))
    press("warmer", at(19, 1))
    house.run(at(19, 1))
    press("warmer", at(19, 2))
    n = o.read_nudge(o.NUDGE_FILE, TZ, at(19, 2))
    assert (n.floor, n.shift) == (1.0, 1.0)
    assert house.run(at(19, 2)) == {"Unit A": 23.0, "Unit B": 23.0}
    assert house.run(at(20, 30)) == {"Unit A": 23.0, "Unit B": 23.0}


def test_too_cold_in_a_peak_lifts_the_deep_cut_step_by_step():
    house = House(prices({k: 60.0 for k in range(40, 48)}))                   # 10-12 peak: -3 C
    assert house.run(at(10))["Unit B"] == 16.0
    press("warmer", at(10, 1))
    assert house.run(at(10, 1))["Unit B"] == 17.0
    press("warmer", at(10, 2))
    assert house.run(at(10, 2))["Unit B"] == 18.0
    assert "enimmilleen" in press("warmer", at(10, 3))


def test_downstairs_night_dip_is_lifted_back_to_its_base():
    night = {k: 6.0 for k in range(0, 8)} | {k: 22.0 for k in range(8, 20)}
    house = House(prices(night))
    assert house.run(at(3, 15)) == {"Unit A": 18.5, "Unit B": 18.0}   # 03:00 is the hour's boosted quarter
    press("warmer", at(3, 16))
    assert house.run(at(3, 16)) == {"Unit A": 19.0, "Unit B": 19.0}


def test_without_a_fresh_run_the_press_raises_the_plan_so_it_is_never_lost():
    house = House(prices(EVENING_DEAR))
    house.run(at(18))                                                         # an hour old
    press("warmer", at(19, 1))
    assert o.read_nudge(o.NUDGE_FILE, TZ, at(19, 1)).shift == 1.0


# ---- too hot, undo, longer, cancel -----------------------------------------------------------

def test_too_hot_lowers_the_plan_and_too_cold_takes_it_back():
    house = House(prices({}))
    press("cooler", at(19))
    assert house.run(at(19)) == {"Unit A": 21.0, "Unit B": 21.0}
    press("warmer", at(19, 5))
    assert not o.NUDGE_FILE.exists()
    assert house.run(at(19, 5)) == {"Unit A": 22.0, "Unit B": 22.0}


def test_too_hot_takes_back_a_raise_before_the_floor():
    house = House(prices(EVENING_DEAR))
    house.run(at(19))
    press("warmer", at(19, 1)); house.run(at(19, 1))
    press("warmer", at(19, 2))
    press("cooler", at(19, 3))
    n = o.read_nudge(o.NUDGE_FILE, TZ, at(19, 3))
    assert (n.floor, n.shift) == (1.0, 0.0)


def test_longer_adds_an_hour_and_cancel_returns_to_the_plan_at_once():
    house = House(prices({}))
    press("warmer", at(19))
    assert o.read_nudge(o.NUDGE_FILE, TZ, at(19)).until == at(22)
    assert "tunnilla" in press("longer", at(20))
    assert o.read_nudge(o.NUDGE_FILE, TZ, at(20)).until == at(23)
    assert o.read_nudge(o.NUDGE_FILE, TZ, at(22, 30))                        # still on
    press("reset", at(20, 1))
    assert house.run(at(20, 1)) == {"Unit A": 22.0, "Unit B": 22.0}


def test_longer_without_a_nudge_is_refused():
    with pytest.raises(web.Refused):
        press("longer", at(19))


def test_a_nudge_ends_by_itself():
    house = House(prices({}))
    press("cooler", at(17))
    assert house.run(at(19, 59))["Unit B"] == 21.0
    assert house.run(at(20, 1))["Unit B"] == 22.0


# ---- other states -----------------------------------------------------------------------------

def test_too_cold_while_away_lifts_away_toward_the_plan():
    house = House(prices({}), away=o.Away())
    assert house.run(at(19)) == {"Unit A": 17.0, "Unit B": 17.0}
    press("warmer", at(19, 1))                      # away's 17 C is a cut from the plan's 22 C
    assert house.run(at(19, 1)) == {"Unit A": 18.0, "Unit B": 18.0}


def test_too_hot_while_cooling_lowers_the_cooling_setpoint():
    house = House(None, season="cool")
    for u in house.units:
        u.operation_mode, u.target_temperature = "cool", 23.0
    press("cooler", at(14))
    assert house.run(at(14))["Unit A"] == 22.0


def test_too_cold_while_cooling_raises_the_cooling_setpoint():
    house = House(None, season="cool")
    for u in house.units:
        u.operation_mode, u.target_temperature = "cool", 23.0
    house.run(at(14))
    o.SEASON_FILE.write_text("cool\n")
    press("warmer", at(14, 1))
    assert house.run(at(14, 1))["Unit A"] == 24.0


def test_the_page_plan_shows_the_nudge_from_now_until_it_ends():
    o.set_nudge("cooler", TZ, CFG, at(19))
    rows = o.plan_rows(at(19), prices({}), CFG, 5.0, nudge=o.read_nudge(o.NUDGE_FILE, TZ, at(19)))
    by_time = {r["start"].strftime("%H:%M"): r["final"] for r in rows}
    assert by_time["18:45"] == 22.0                                           # before the press
    assert by_time["19:00"] == 21.0 and by_time["21:45"] == 21.0              # the nudge
    assert by_time["22:00"] == 20.5                                           # after: the plan (base 21, first cooldown step)


def test_too_cold_while_away_in_a_peak_lifts_the_cut():
    house = House(prices({k: 60.0 for k in range(40, 48)}), away=o.Away(at(22)))   # away; 10-12 peak
    assert house.run(at(10)) == {"Unit A": 16.0, "Unit B": 16.0}               # the plan is lower
    press("warmer", at(10, 1))
    assert house.run(at(10, 1)) == {"Unit A": 17.0, "Unit B": 17.0}


def test_too_cold_during_normal_now_is_refused_with_a_reason():
    o.set_control("normal", "on", TZ, CFG)
    with pytest.raises(web.Refused, match="Normaali lämpö heti"):
        press("warmer", datetime.now(TZ))
    assert not o.NUDGE_FILE.exists()


def test_too_hot_while_away_goes_down_to_the_one_heating_floor():
    house = House(prices({}), away=o.Away())                         # 8c: away holds 17
    press("cooler", at(10))
    assert house.run(at(10)) == {"Unit A": 16.0, "Unit B": 16.0}


def test_too_cold_at_the_upstairs_night_floor_spends_no_step_on_it():
    night = {k: 22.0 for k in range(0, 20)} | {k: 12.0 for k in range(0, 2)}  # 22c night: upstairs cut to its floor
    house = House(prices(night))
    assert house.run(at(1, 15)) == {"Unit A": 18.5, "Unit B": 18.0}
    press("warmer", at(1, 16))
    assert house.run(at(1, 16)) == {"Unit A": 19.0, "Unit B": 19.0}
    press("warmer", at(1, 17))
    n = o.read_nudge(o.NUDGE_FILE, TZ, at(1, 17))
    assert (n.floor, n.shift) == (1.0, 1.0)                          # the second press raises the plan
    assert house.run(at(1, 17)) == {"Unit A": 20.0, "Unit B": 20.0}
