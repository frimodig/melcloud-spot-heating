"""Poissa's warm-up before the return: sized by the absence, bought in the cheapest quarters."""
import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path

import ilp_optimizer as o

ROOT = Path(__file__).resolve().parent.parent
CFG = o.load_config(ROOT / "config.toml", ROOT / "tests" / "house.toml")
TZ = o.ZoneInfo(CFG["timezone"])
TUE = date(2026, 11, 10)


def at(hh, mm=0, day=TUE):
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ)


def prices(quarters, default=8.0):
    start, _ = o.local_day_bounds(TUE, TZ)
    return [o.PriceInterval(start + timedelta(minutes=15 * k), start + timedelta(minutes=15 * (k + 1)),
                            quarters.get(k, default)) for k in range(96)]


BACK = at(22)
AWAY = o.Away(BACK)


def test_poissa_holds_its_floor_until_two_hours_before_the_return():
    base, offset, final, reason = o.desired_setpoint(at(12), prices({}), CFG, away=AWAY)
    assert final == 17.0 and reason.startswith("away until") and o.reason_kind(reason) == "away"
    assert o.desired_setpoint(at(19, 45), prices({}), CFG, away=AWAY)[2] == 17.0
    warm = o.desired_setpoint(at(20), prices({}), CFG, away=AWAY)
    assert warm[2] == 17.5 and warm[3].startswith("pre-heat before return")    # the first of 8 steps
    assert o.desired_setpoint(at(21, 45), prices({}), CFG, away=AWAY)[2] == 20.5   # the plan at 22:00 (first cooldown step)


class Unit:
    def __init__(self, name):
        self.name, self.operation_mode, self.power = name, "cool", True
        self.target_temperature, self.room_temperature, self.writes = 23.0, 24.0, []

    async def update(self):
        pass

    async def set(self, props):
        self.writes.append(props)
        self.power = props.get("power", self.power)


def test_cooling_switches_the_units_off_and_on_again_before_the_return(tmp_path):
    units, state = [Unit("Unit A"), Unit("Unit B")], o.UnitState(tmp_path / "s.json")
    summer, back = datetime(2026, 7, 14, 12, 0, tzinfo=TZ), datetime(2026, 7, 16, 18, 0, tzinfo=TZ)
    run = lambda now: asyncio.run(o.apply_to_units(units, now, None, CFG, False, 22.0, None,
                                                   o.Away(back), state, "cool"))
    run(summer)
    assert all(not u.power for u in units)
    run(back - timedelta(hours=2, minutes=15))
    assert all(not u.power for u in units)
    run(back - timedelta(hours=2))
    assert all(u.power for u in units)


def test_the_warm_up_climbs_in_steps_and_buys_its_heat_in_the_cheapest_quarters():
    flat = [o.desired_setpoint(at(20, 0) + timedelta(minutes=15 * k), prices({}), CFG, away=AWAY)[2]
            for k in range(8)]
    assert flat == sorted(flat) and flat[0] < 18.0 and flat[-1] == 20.5          # a climb to the 22:00 plan
    day = o.Away(BACK, BACK - timedelta(hours=24))                               # 3.5 C: 5 quarters of heat
    cheap = prices({k: 2.0 for k in range(80, 84)} | {k: 20.0 for k in range(84, 88)})   # 20-21 cheap, 21-22 dear
    got = [o.desired_setpoint(at(20, 0) + timedelta(minutes=15 * k), cheap, CFG, away=day) for k in range(8)]
    assert all("heating" in g[3] for g in got[:4])                              # the cheap hour first
    assert [g[2] for g in got[4:7]] == [got[3][2]] * 3 and "holding" in got[4][3]
    assert got[7][2] == 20.5                                                     # the last step: arrives warm


def test_a_peak_in_the_warm_up_keeps_its_cut():
    p = prices({k: 60.0 for k in range(80, 84)})                                # 20:00-21:00 peak
    got = o.desired_setpoint(at(20, 15), p, CFG, unit="Unit B", away=AWAY)
    assert got[2] == 19.0 and "peak" in got[3]                                  # 22 - 3, as the plan cuts


def test_a_short_absence_gets_a_short_warm_up():
    two_hours = o.Away(BACK, BACK - timedelta(hours=2))                # 0.6 C coasted: one quarter
    assert o.desired_setpoint(at(21, 30), prices({}), CFG, away=two_hours)[2] == 17.0
    assert o.desired_setpoint(at(21, 45), prices({}), CFG, away=two_hours)[2] == 20.5
    day = o.Away(BACK, BACK - timedelta(hours=24))                     # 3.5 C to the 22:00 plan: 1 h 15 min
    assert o.desired_setpoint(at(20, 30), prices({}), CFG, away=day)[2] == 17.0
    assert o.desired_setpoint(at(20, 45), prices({}), CFG, away=day)[3].startswith("pre-heat before return")


def test_in_a_live_run_a_heating_quarter_lifts_the_room_but_the_absence_sizes_the_warm_up():
    long_ago = o.Away(BACK, BACK - timedelta(days=3))                  # 3.5 C to climb: 20:45-22:00
    assert o.desired_setpoint(at(20, 30), prices({}), CFG, room_temp=20.5, away=long_ago)[2] == 17.0
    cold_room = o.desired_setpoint(at(20, 45), prices({}), CFG, room_temp=17.0, away=long_ago)
    assert cold_room[2] == 18.0 and cold_room[3].startswith("pre-heat before return")   # a quarter's lift


def test_poissa_sizes_its_warm_up_the_same_way_within_its_hour():
    short = o.Away(at(18), at(16))                             # 2 h out: a quarter
    assert o.desired_setpoint(at(17, 30), prices({}), CFG, away=short)[2] == 17.0
    assert o.desired_setpoint(at(17, 45), prices({}), CFG, away=short)[3].startswith("pre-heat before return")
    long = o.Away(at(18), at(18) - timedelta(days=2))         # 17 -> 22 C: 1 h 45 min
    assert o.desired_setpoint(at(16, 15), prices({}), CFG, away=long)[3].startswith("pre-heat before return")
    assert o.desired_setpoint(at(16, 0), prices({}), CFG, away=long)[2] == 17.0


def test_the_file_records_when_it_was_set():
    o.set_control("away", "2099-01-01T10:00", TZ, CFG)
    assert o.load_away(o.AWAY_FILE, TZ).since is not None
    o.AWAY_FILE.write_text("2099-01-01T10:00\n")                       # an older file: no start
    assert o.load_away(o.AWAY_FILE, TZ).since is None
    assert o.load_away(o.AWAY_FILE, TZ).lead(CFG) == timedelta(hours=2)


def test_a_live_warm_up_heats_the_room_in_every_quarter_and_arrives_warm():
    away = o.Away(BACK, BACK - timedelta(days=3))
    room, t = 17.0, at(19)
    while t < BACK:
        final = o.desired_setpoint(t, prices({}), CFG, room_temp=room, away=away)[2]
        if final > room:                                           # heating: 3 C an hour, capped at the setpoint
            room = min(final, room + 0.75)
        t += timedelta(minutes=15)
    assert room >= 20.5 - 0.25                                     # the plan at 22:00 is 20.5


def test_a_hand_written_time_with_a_space_is_still_that_time():
    o.AWAY_FILE.write_text("2099-01-01 18:00\n")
    assert o.load_away(o.AWAY_FILE, TZ).until == datetime(2099, 1, 1, 18, 0, tzinfo=TZ)
    assert o.load_away(o.AWAY_FILE, TZ).since is None
    o.AWAY_FILE.write_text("2099-01-01T18:00 garbage\n")
    assert o.load_away(o.AWAY_FILE, TZ) is None


def test_cooling_switches_back_on_by_the_fixed_lead():
    away = o.Away(BACK, BACK - timedelta(hours=2))
    assert o.desired_setpoint(at(21, 30), prices({}), CFG, away=away)[2] == 17.0      # heating: a quarter
    assert not away.active_at(at(20, 0), CFG, cooling=True)                            # cooling: 2 h before
    assert away.active_at(at(19, 45), CFG, cooling=True)


def test_a_new_return_time_keeps_when_the_absence_started():
    o.set_control("away", "2099-01-01T10:00", TZ, CFG)
    first = o.load_away(o.AWAY_FILE, TZ).since
    o.AWAY_FILE.write_text(f"2099-01-01T10:00 {(first - timedelta(days=2)).isoformat(timespec='minutes')}\n")
    o.set_control("away", "2099-01-02T10:00", TZ, CFG)
    assert o.load_away(o.AWAY_FILE, TZ).since == first - timedelta(days=2)


class HeatUnit:
    def __init__(self, room):
        self.name, self.operation_mode, self.power = "Unit B", "heat", True
        self.target_temperature, self.room_temperature, self.writes = 16.0, room, []

    async def update(self):
        pass

    async def set(self, props):
        self.writes.append(props)
        self.target_temperature = props.get("target_temperature", self.target_temperature)


def test_a_started_warm_up_keeps_going_when_the_room_warms_fast(tmp_path):
    away = o.Away(at(18), at(18) - timedelta(days=1))
    unit, state = HeatUnit(18.0), o.UnitState(tmp_path / "s.json")
    run = lambda t: asyncio.run(o.apply_to_units([unit], t, prices({}), CFG, False, 5.0, None, away, state, "heat"))
    run(at(16, 30))
    assert unit.target_temperature > 18.0                              # the warm-up has begun
    unit.room_temperature = 19.0                                       # +1 C in a quarter: faster than 3 C/h
    run(at(16, 45))
    assert unit.target_temperature > 19.0                              # it carries on, not back to 17


def test_an_open_ended_poissa_passes_its_start_on():
    import os
    o.set_control("away", "on", TZ, CFG)
    week_ago = (datetime.now(TZ) - timedelta(days=7)).timestamp()
    os.utime(o.AWAY_FILE, (week_ago, week_ago))
    o.set_control("away", "2099-01-01T10:00", TZ, CFG)                 # a return time: the start stays
    assert datetime.now(TZ) - o.load_away(o.AWAY_FILE, TZ).since > timedelta(days=6)


def test_a_refused_file_passes_no_start_on():
    o.AWAY_FILE.write_text(f"garbage {(datetime.now(TZ) - timedelta(days=9)).isoformat(timespec='minutes')}\n")
    o.set_control("away", "2099-01-01T10:00", TZ, CFG)
    assert datetime.now(TZ) - o.load_away(o.AWAY_FILE, TZ).since < timedelta(minutes=5)


def test_the_end_of_normal_now_keeps_a_started_warm_up(tmp_path):
    away = o.Away(at(18), at(18) - timedelta(days=1))
    state = o.UnitState(tmp_path / "s.json")
    state.unit("Unit B")["warmup"] = at(18).isoformat()
    unit = HeatUnit(21.0)
    asyncio.run(o.apply_to_units([unit], at(17), prices({}), CFG, False, 5.0, None, away, state, "heat"))
    state.data["normal"] = {"id": "x", "mode": "heat", "temp": 21.0}   # normal now has just been reached
    asyncio.run(o.apply_to_units([unit], at(17, 15), prices({}), CFG, False, 5.0, None, away, state, "heat"))
    assert unit.target_temperature > 17.0


def test_a_return_half_an_hour_ahead_heats_from_the_first_run():
    """The page's way out of Away ('I am on my way'): the return is set 30 minutes ahead, and the house
    heats at once, whatever the prices before and after, not at the floor until a cheap quarter."""
    now = at(17)
    for cheap in ({}, {k: 2.0 for k in range(60, 66)}, {k: 2.0 for k in range(69, 72)}):
        away = o.Away(now + timedelta(minutes=30), now - timedelta(hours=8))
        final = o.desired_setpoint(now, prices(cheap), CFG, away=away, room_temp=18.0)[2]
        assert final > CFG["nobody_home"]["temp"], cheap
