import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import ilp_optimizer as o

ROOT = Path(__file__).resolve().parent.parent
CFG = o.load_config(ROOT / "tests" / "logic_config.toml")
TZ = ZoneInfo("Europe/Helsinki")
NOW = datetime(2026, 7, 14, 12, 0, tzinfo=TZ)  # Tuesday, summer


class Unit:
    def __init__(self, name, mode="heat", target=20.0, power=True, fail=False, room=22.0):
        self.name, self.operation_mode, self.target_temperature = name, mode, target
        self.power, self.fail, self.room_temperature, self.writes = power, fail, room, []

    async def update(self):
        pass

    async def set(self, props):
        if self.fail:
            raise OSError("switch failed")
        self.writes.append(props)
        self.operation_mode = props.get("operation_mode", self.operation_mode)
        self.target_temperature = props.get("target_temperature", self.target_temperature)


def apply(units, season, now=NOW, outdoor=20.0, state=None, dry_run=False, hist=None, cfg=CFG):
    return asyncio.run(o.apply_to_units(units, now, None, cfg, dry_run, outdoor, hist, None,
                                        state, season))


def test_season_cool_switches_both_units_together_with_cooling_targets():
    down, up = Unit("Unit A"), Unit("Unit B")
    assert apply([down, up], "cool") == 0
    assert down.writes == [{"operation_mode": "cool", "target_temperature": 23.0}]
    assert up.writes == [{"operation_mode": "cool", "target_temperature": 22.5}]  # upstairs cooler
    assert down.operation_mode == up.operation_mode == "cool"


def test_season_heat_switches_back_with_heating_targets():
    down, up = Unit("Unit A", "cool", 23.0), Unit("Unit B", "cool", 23.0)
    apply([down, up], "heat", outdoor=12.0)
    assert down.writes == [{"operation_mode": "heat", "target_temperature": 19.5}]  # Tuesday 12:00 base 19
    assert up.writes == [{"operation_mode": "heat", "target_temperature": 19.0}]


def test_no_season_never_switches_modes():
    a, b = Unit("a", "heat"), Unit("b", "heat")
    apply([a, b], None)
    assert all("operation_mode" not in w for w in a.writes + b.writes)


def test_units_already_in_the_season_mode_are_just_controlled():
    a, b = Unit("a", "cool", 20.0), Unit("b", "cool", 20.0)
    apply([a, b], "cool")
    assert a.writes == [{"target_temperature": 23.0}]  # plain target write, no mode in it


def test_a_mismatch_is_fixed_by_following_the_season():
    heat, cool = Unit("a", "heat"), Unit("b", "cool", 23.0)
    assert apply([heat, cool], "cool") == 0
    assert heat.operation_mode == "cool" and cool.writes[-1:] != [{"operation_mode": "cool"}]


def test_a_cooling_switch_is_never_blocked_by_cold_weather():
    a, b = Unit("a"), Unit("b")
    apply([a, b], "cool", outdoor=3.0)   # the owner chose it; no guard on the outdoor temperature
    assert a.operation_mode == b.operation_mode == "cool"


def test_unknown_outdoor_temperature_does_not_block_the_owners_choice():
    a, b = Unit("a"), Unit("b")
    apply([a, b], "cool", outdoor=None)
    assert a.operation_mode == b.operation_mode == "cool"


def test_off_units_are_switched_on_in_the_season_mode_in_one_write():
    on, off = Unit("a"), Unit("b", power=False)
    assert apply([on, off], "cool") == 0
    assert on.operation_mode == "cool"
    assert off.writes == [{"operation_mode": "cool", "target_temperature": 23.0, "power": True}]


def test_units_in_other_modes_are_brought_to_the_season_mode():
    a, b, c = Unit("a", "heat"), Unit("b", "fan_only"), Unit("c", "dry")
    assert apply([a, b, c], "cool") == 0
    assert a.operation_mode == b.operation_mode == c.operation_mode == "cool"
    assert apply([Unit("x", "auto"), Unit("y", "heat")], "heat") == 0  # a stray mode doesn't block heating either


def test_the_season_can_be_flipped_back_and_forth_at_once(tmp_path):
    path = tmp_path / "s.json"
    a, b = Unit("a"), Unit("b")
    for season, minutes in (("cool", 0), ("heat", 1), ("cool", 2), ("heat", 3)):
        apply([a, b], season, now=NOW + timedelta(minutes=minutes), state=o.UnitState(path))
        assert a.operation_mode == b.operation_mode == season   # no waiting between switches


def test_a_unit_the_controller_switched_off_for_away_is_left_to_the_away_logic(tmp_path):
    path = tmp_path / "s.json"
    state = o.UnitState(path)
    state.unit("b")["off_by_us"] = True
    on, off = Unit("a", "cool"), Unit("b", "cool", power=False)
    away = o.Away(NOW + timedelta(days=2))
    asyncio.run(o.apply_to_units([on, off], NOW, None, CFG, False, 20.0, None, away, state, "cool"))
    assert off.writes == [] and not off.power


def test_nothing_is_switched_on_during_a_cooling_away():
    off = Unit("b", "heat", power=False)
    away = o.Away(NOW + timedelta(days=2))
    asyncio.run(o.apply_to_units([Unit("a", "cool"), off], NOW, None, CFG, False, 20.0, None, away, None, "cool"))
    assert off.writes == []


def test_failed_switch_is_retried_at_the_next_run_and_blocks_other_writes(tmp_path):
    path = tmp_path / "s.json"
    ok, bad = Unit("a"), Unit("b", fail=True)
    assert apply([ok, bad], "cool", state=o.UnitState(path)) >= 1
    assert ok.operation_mode == "cool" and bad.operation_mode == "heat"
    bad.fail = False
    apply([ok, bad], "cool", now=NOW + timedelta(minutes=15), state=o.UnitState(path))
    assert bad.operation_mode == "cool"


def test_dry_run_switches_nothing(tmp_path):
    a, b = Unit("a"), Unit("b")
    apply([a, b], "cool", dry_run=True, state=o.UnitState(tmp_path / "s.json"))
    assert a.writes == b.writes == [] and a.operation_mode == "heat"


def test_mode_switch_is_recorded_in_history(tmp_path):
    import csv
    hist = tmp_path / "h.csv"
    apply([Unit("a"), Unit("b")], "cool", hist=hist)
    rows = list(csv.DictReader(open(hist)))
    assert [r["action"] for r in rows] == ["mode-switch", "mode-switch"]
    assert rows[0]["reason"].startswith("heat -> cool")


# ---- files and holiday ------------------------------------------------------------

def test_read_season(tmp_path):
    f = tmp_path / "SEASON"
    assert o.read_season(f) == "heat"          # no file: winter, the safe default
    f.write_text("cool\n")
    assert o.read_season(f) == "cool"
    f.write_text("summer")
    assert o.read_season(f) == "heat"          # a typo is not summer either


def test_holiday_uses_the_weekend_schedule_on_weekdays():
    tue = datetime(2026, 11, 10, 9, 0, tzinfo=TZ)  # weekday 08:00 entry is 19, weekend is 21
    assert o.desired_setpoint(tue, None, CFG)[2] == 19.0
    assert o.desired_setpoint(tue, None, CFG, 0.0, None, None, None, "heat", True)[2] == 21.0
    assert o.base_setpoint(tue, CFG, True) == 21.0


def test_holiday_file(tmp_path):
    f = tmp_path / "HOLIDAY"
    assert not o.holiday_active(f, TZ, NOW)
    f.write_text("")
    assert o.holiday_active(f, TZ, NOW)
    f.write_text("2026-08-10T00:00")
    assert o.holiday_active(f, TZ, NOW) and not o.holiday_active(f, TZ, datetime(2026, 8, 11, tzinfo=TZ))
    f.write_text("until august")
    assert not o.holiday_active(f, TZ, NOW)  # typo: normal schedule


def test_season_cli_writes_the_file_and_refuses_anything_else(tmp_path):
    ns = lambda s: o.argparse.Namespace(config=str(ROOT / "tests" / "logic_config.toml"), list=False, plan=False, report=None, control=None, lock_wait=0.0, plan_json=False,
                                        season=s, prices_file=None, now=None, dry_run=False,
                                        outdoor_temp=None, away=False, away_until=None,
                                        holiday=False, mode="heat", sun=None,
                                        home=None, assume_home=False)
    assert asyncio.run(o.run(ns("cool"))) == 0 and o.SEASON_FILE.read_text().strip() == "cool"
    assert asyncio.run(o.run(ns("heat"))) == 0 and o.SEASON_FILE.read_text().strip() == "heat"
    assert o.set_season("units") == 2 and o.SEASON_FILE.read_text().strip() == "heat"


# ---- [control] season_enforce: how hard the season is kept --------------------------------------

def enforcing(how):
    return {**CFG, "control": {**CFG["control"], "season_enforce": how}}


def test_by_default_every_run_switches_units_back_on_and_into_the_season():
    assert CFG["control"].get("season_enforce", "always") == "always"
    off, dry = Unit("a", power=False), Unit("b", "dry")
    apply([off, dry], "heat")
    assert off.writes[0]["power"] is True and dry.operation_mode == "heat"


def test_running_leaves_a_unit_someone_switched_off_but_keeps_the_running_ones_in_the_season():
    off, dry = Unit("a", power=False), Unit("b", "dry")
    apply([off, dry], "heat", cfg=enforcing("running"))
    assert off.writes == [] and dry.operation_mode == "heat"


def test_on_change_only_switches_when_the_season_changes(tmp_path):
    cfg, path = enforcing("on_change"), tmp_path / "s.json"
    off, dry = Unit("a", power=False), Unit("b", "dry")
    apply([off, dry], "heat", state=o.UnitState(path), cfg=cfg)               # first seen: only recorded
    assert off.writes == [] and dry.operation_mode == "dry"
    apply([off, dry], "cool", now=NOW + timedelta(minutes=15), state=o.UnitState(path), cfg=cfg)
    assert off.writes[0]["power"] is True and dry.operation_mode == "cool"     # the change: switched once
    off.power, dry.operation_mode = False, "dry"                                # people change them again
    apply([off, dry], "cool", now=NOW + timedelta(minutes=30), state=o.UnitState(path), cfg=cfg)
    assert dry.operation_mode == "dry" and len(off.writes) == 1                 # and that stays


def test_on_change_retries_a_failed_switch(tmp_path):
    cfg, path = enforcing("on_change"), tmp_path / "s.json"
    a = Unit("a")
    apply([a], "heat", state=o.UnitState(path), cfg=cfg)
    a.fail = True
    apply([a], "cool", now=NOW + timedelta(minutes=15), state=o.UnitState(path), cfg=cfg)
    a.fail = False
    apply([a], "cool", now=NOW + timedelta(minutes=30), state=o.UnitState(path), cfg=cfg)
    assert a.operation_mode == "cool"


def test_on_change_keeps_a_season_change_made_during_a_pause(tmp_path):
    cfg, path = enforcing("on_change"), tmp_path / "s.json"
    a = Unit("a")
    apply([a], "heat", state=o.UnitState(path), cfg=cfg)
    apply([a], None, now=NOW + timedelta(minutes=15), state=o.UnitState(path), cfg=cfg)   # the pause's safe write
    apply([a], "cool", now=NOW + timedelta(minutes=30), state=o.UnitState(path), cfg=cfg)
    assert a.operation_mode == "cool"


def test_on_change_switches_a_unit_that_could_not_be_read_once_it_can(tmp_path):
    cfg, path = enforcing("on_change"), tmp_path / "s.json"
    a, b = Unit("a"), Unit("b")
    apply([a, b], "heat", state=o.UnitState(path), cfg=cfg)

    async def unreadable():
        raise OSError("no answer")
    b.update = unreadable
    apply([a, b], "cool", now=NOW + timedelta(minutes=15), state=o.UnitState(path), cfg=cfg)
    assert a.operation_mode == "cool" and b.operation_mode == "heat"
    del b.update                                                                # back online
    apply([a, b], "cool", now=NOW + timedelta(minutes=30), state=o.UnitState(path), cfg=cfg)
    assert b.operation_mode == "cool"


def test_on_change_corrects_a_unit_away_kept_off_through_the_change(tmp_path):
    cfg, path = enforcing("on_change"), tmp_path / "s.json"
    state = o.UnitState(path)
    a = Unit("a", "cool")
    apply([a], "cool", state=state, cfg=cfg)
    state.unit("a")["off_by_us"] = True
    a.power = False
    away = o.Away(NOW + timedelta(days=2))
    asyncio.run(o.apply_to_units([a], NOW + timedelta(minutes=15), None, cfg, False, 20.0, None, away, state, "heat"))
    assert a.writes == [] or not a.writes[-1].get("power")                      # left to away
    a.power, a.operation_mode = True, "cool"                                    # away over: back on, old mode
    state.unit("a").pop("off_by_us")
    apply([a], "heat", now=NOW + timedelta(days=3), state=state, cfg=cfg)
    assert a.operation_mode == "heat"


def test_on_change_counts_a_deliberate_multi_split_mismatch_as_no_failure(tmp_path):
    cfg = {**enforcing("on_change"), "units": {**CFG["units"], "outdoor_groups": [["a", "b"]]}}
    path = tmp_path / "s.json"
    a, b = Unit("a"), Unit("b")
    apply([a, b], "heat", state=o.UnitState(path), cfg=cfg)
    b.operation_mode = "fan_only"                                               # someone's choice
    assert apply([a, b], "heat", now=NOW + timedelta(minutes=15), state=o.UnitState(path), cfg=cfg) == 0
    assert apply([a, b], "heat", now=NOW + timedelta(minutes=15), state=o.UnitState(path),
                 cfg={**cfg, "control": {**cfg["control"], "season_enforce": "always"}}) == 0   # always: fixed


def test_a_dry_run_with_units_to_switch_still_plans_every_unit(tmp_path):
    hist = tmp_path / "h.csv"
    off, on = Unit("a", power=False), Unit("b", target=25.0)
    apply([off, on], "heat", dry_run=True, hist=hist)
    assert off.writes == on.writes == []
    import csv
    rows = {r["unit"]: r for r in csv.DictReader(open(hist))}
    assert set(rows) == {"a", "b"} and "would switch off -> heat" in rows["a"]["reason"]


def test_an_unknown_season_enforce_falls_back_to_always(caplog):
    off = Unit("a", power=False)
    apply([off], "heat", cfg=enforcing("sometimes"))
    assert off.writes and "season_enforce" in caplog.text


def test_on_change_switches_a_unit_first_seen_in_the_wrong_one_of_heat_or_cool(tmp_path):
    cfg, path = enforcing("on_change"), tmp_path / "s.json"
    a, off = Unit("a", "cool"), Unit("b", power=False)
    apply([a, off], "heat", state=o.UnitState(path), cfg=cfg)                   # e.g. a lost state file
    assert a.operation_mode == "heat" and off.writes == []


def test_changing_to_on_change_later_does_not_take_an_old_record_for_a_change(tmp_path):
    path = tmp_path / "s.json"
    a = Unit("a", "cool")
    apply([a], "cool", state=o.UnitState(path), cfg=enforcing("on_change"))
    apply([a], "heat", now=NOW + timedelta(minutes=15), state=o.UnitState(path), cfg=enforcing("running"))
    assert a.operation_mode == "heat"
    a.power, before = False, len(a.writes)                                      # someone switches it off
    apply([a], "heat", now=NOW + timedelta(minutes=30), state=o.UnitState(path), cfg=enforcing("on_change"))
    assert len(a.writes) == before                                              # not taken for a change
