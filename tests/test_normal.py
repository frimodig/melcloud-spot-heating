import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import ilp_optimizer as o

ROOT = Path(__file__).resolve().parent.parent
CFG = o.load_config(ROOT / "config.toml", ROOT / "tests" / "house.toml")
TZ = ZoneInfo("Europe/Helsinki")
NOW = datetime(2026, 11, 10, 20, 0, tzinfo=TZ)        # Tuesday evening, plan 22 at 20:00 (weekday)


class Unit:
    def __init__(self, name, mode="heat", target=22.0, room=22.0, power=True):
        self.name, self.operation_mode, self.target_temperature = name, mode, target
        self.power, self.room_temperature, self.writes = power, room, []

    async def update(self):
        pass

    async def set(self, props):
        self.writes.append(dict(props))
        self.power = props.get("power", self.power)
        self.operation_mode = props.get("operation_mode", self.operation_mode)
        self.target_temperature = props.get("target_temperature", self.target_temperature)


def normal(now=NOW, hours=4):
    until = now + timedelta(hours=hours)
    return {"temp": 21.0, "until": until, "id": until.isoformat()}


def step(units, state, now=NOW, norm="default", season=None, dry_run=False, away=None):
    if norm == "default":
        norm = normal(NOW)
    return asyncio.run(o.apply_to_units(units, now, None, CFG, dry_run, 5.0, None, away, state, season,
                                        normal=norm))


def test_a_hot_house_is_taken_to_21_by_cooling_whatever_the_season(tmp_path):
    a, b = Unit("Unit A", room=25.0), Unit("Unit B", room=24.0)
    state = o.UnitState(tmp_path / "s.json")
    assert step([a, b], state, season="heat") == 0
    assert a.writes == [{"operation_mode": "cool", "target_temperature": 21.0}]
    assert b.writes == [{"operation_mode": "cool", "target_temperature": 21.0}]   # no unit offset, no plan
    assert state.data["normal"]["modes"] == {"Unit A": "cool", "Unit B": "cool"}


def test_a_cold_house_is_taken_to_21_by_heating_even_in_cool_season(tmp_path):
    a, b = Unit("a", "cool", 23.0, room=18.0), Unit("b", "cool", 23.0, room=19.0)
    state = o.UnitState(tmp_path / "s.json")
    step([a, b], state, season="cool")
    assert a.operation_mode == b.operation_mode == "heat" and a.target_temperature == 21.0


def test_one_mode_for_a_multi_split_chosen_by_the_average_room(tmp_path):
    a, b = Unit("Unit A", room=19.0), Unit("Unit B", room=24.0)   # mean 21.5: cool for both
    step([a, b], o.UnitState(tmp_path / "s.json"))
    assert a.operation_mode == b.operation_mode == "cool"    # they share one outdoor unit (config.toml)


def test_self_contained_units_each_go_by_their_own_room(tmp_path):
    a, b = Unit("a", room=19.0), Unit("b", room=24.0)        # not in an outdoor group
    step([a, b], o.UnitState(tmp_path / "s.json"))
    assert (a.operation_mode, b.operation_mode) == ("heat", "cool")


def test_a_session_saved_with_one_mode_still_runs(tmp_path):
    state = o.UnitState(tmp_path / "s.json")
    a = Unit("Unit A", room=25.0)
    step([a], state)
    sid = state.data["normal"]["id"]
    state.data["normal"] = {"id": sid, "mode": "cool", "temp": 21.0}   # the format before modes per unit
    a.room_temperature = 24.0
    step([a], state)
    assert a.operation_mode == "cool"


def test_off_units_are_switched_on_for_it(tmp_path):
    a, b = Unit("a", room=24.0), Unit("b", room=24.0, power=False)
    step([a, b], o.UnitState(tmp_path / "s.json"))
    assert b.power and b.writes[0]["power"] is True


def test_it_keeps_going_until_every_room_has_reached_21_then_returns_to_the_plan(tmp_path):
    a, b = Unit("Unit A", room=25.0), Unit("Unit B", room=24.0)
    state = o.UnitState(tmp_path / "s.json")
    step([a, b], state, season="heat")
    a.writes.clear(); b.writes.clear()
    a.room_temperature, b.room_temperature = 22.0, 21.0      # b is there, a is not
    step([a, b], state, now=NOW + timedelta(minutes=15), season="heat")
    assert a.writes == b.writes == [] and "normal" in state.data      # still overriding, nothing to write
    a.room_temperature = 21.0                                        # now both are at 21
    step([a, b], state, now=NOW + timedelta(minutes=30), season="heat")
    assert a.operation_mode == b.operation_mode == "heat"           # back to the season's mode
    assert a.writes == [{"operation_mode": "heat", "target_temperature": 22.0}]   # 20:30 plan 22 (+0.5 clamped)
    assert b.writes == [{"operation_mode": "heat", "target_temperature": 22.0}]
    assert "normal" not in state.data and state.data["normal_end"] is True


def test_cancelling_or_expiry_returns_the_units_at_once(tmp_path):
    a, b = Unit("a", room=25.0), Unit("b", room=25.0)
    state = o.UnitState(tmp_path / "s.json")
    step([a, b], state)                                              # session starts (cool)
    step([a, b], state, now=NOW + timedelta(minutes=15), norm=None)  # file gone: cancelled
    assert a.operation_mode == b.operation_mode == "heat" and "normal" not in state.data


def test_with_no_season_given_it_returns_to_winter_heating(tmp_path):
    a, b = Unit("a", "cool", 23.0, room=24.0), Unit("b", "cool", 23.0, room=24.0)
    state = o.UnitState(tmp_path / "s.json")
    step([a, b], state)                                              # cool to 21 first
    a.room_temperature = b.room_temperature = 21.0
    step([a, b], state, now=NOW + timedelta(minutes=15))
    assert a.operation_mode == b.operation_mode == "heat"           # the default season is heat


def test_in_the_cool_season_it_returns_to_cooling(tmp_path):
    a, b = Unit("a", "cool", 23.0, room=18.0), Unit("b", "cool", 23.0, room=19.0)
    state = o.UnitState(tmp_path / "s.json")
    step([a, b], state, season="cool")                               # a cold room is heated to 21 even in summer
    assert a.operation_mode == b.operation_mode == "heat"
    a.room_temperature = b.room_temperature = 21.0
    step([a, b], state, now=NOW + timedelta(minutes=15), season="cool")
    assert a.operation_mode == b.operation_mode == "cool"


def test_already_at_21_changes_nothing_but_the_plan_resumes(tmp_path):
    a, b = Unit("a", room=21.0), Unit("b", room=21.0)
    state = o.UnitState(tmp_path / "s.json")
    step([a, b], state)
    assert all("operation_mode" not in w for w in a.writes + b.writes)
    assert state.data.get("normal_end") is True


def test_a_second_press_starts_a_new_session(tmp_path):
    a, b = Unit("a", room=25.0), Unit("b", room=25.0)
    state = o.UnitState(tmp_path / "s.json")
    step([a, b], state)
    a.room_temperature = b.room_temperature = 21.0
    step([a, b], state, now=NOW + timedelta(minutes=15))
    state.data.pop("normal_end")
    a.room_temperature = b.room_temperature = 26.0
    again = normal(NOW + timedelta(hours=1))
    step([a, b], state, now=NOW + timedelta(hours=1), norm=again)
    assert state.data["normal"]["id"] == again["id"] and a.operation_mode == "cool"


def test_a_failed_write_keeps_the_session_for_the_next_run(tmp_path):
    a, b = Unit("a", room=25.0), Unit("b", room=25.0)
    state = o.UnitState(tmp_path / "s.json")
    step([a, b], state)
    b.room_temperature = a.room_temperature = 21.0

    async def boom(props):
        raise OSError("down")
    b.set = boom
    assert step([a, b], state, now=NOW + timedelta(minutes=15)) >= 1
    assert "normal" in state.data and not state.data.get("normal_end")


def test_normal_does_not_trip_the_manual_change_hold(tmp_path):
    a, b = Unit("a", room=25.0), Unit("b", room=25.0)
    state = o.UnitState(tmp_path / "s.json")
    step([a, b], state)
    assert state.unit("a")["last_target"] == 21.0
    a.room_temperature = b.room_temperature = 22.0
    step([a, b], state, now=NOW + timedelta(minutes=15))
    assert "hold_until" not in state.unit("a")


def test_dry_run_writes_and_remembers_nothing(tmp_path):
    a, b = Unit("a", room=25.0), Unit("b", room=25.0)
    state = o.UnitState(tmp_path / "s.json")
    step([a, b], state, dry_run=True)
    assert a.writes == b.writes == [] and "normal" not in state.data


# ---- the file and the commands ---------------------------------------------------------------

def test_read_normal_handles_missing_expired_and_bad_files():
    assert o.read_normal(o.NORMAL_FILE, TZ, NOW, CFG) is None
    o.NORMAL_FILE.write_text((NOW + timedelta(hours=1)).isoformat() + "\n")
    got = o.read_normal(o.NORMAL_FILE, TZ, NOW, CFG)
    assert got["temp"] == 21.0 and got["until"] == NOW + timedelta(hours=1)
    assert o.read_normal(o.NORMAL_FILE, TZ, NOW + timedelta(hours=2), CFG) is None
    o.NORMAL_FILE.write_text("hot\n")
    assert o.read_normal(o.NORMAL_FILE, TZ, NOW, CFG) is None


def test_control_normal_on_and_off():
    assert o.set_control("normal", "on", TZ, CFG) == 0
    until = datetime.fromisoformat(o.NORMAL_FILE.read_text().strip())
    assert timedelta(hours=3.9) < until - datetime.now(TZ) <= timedelta(hours=4)
    assert o.set_control("normal", "off", TZ, CFG) == 0 and not o.NORMAL_FILE.exists()
    assert o.set_control("normal", "maybe", TZ, CFG) == 2


def test_a_live_run_removes_the_file_when_the_override_is_over(monkeypatch):
    import argparse
    until = datetime.now(TZ) + timedelta(hours=2)
    o.NORMAL_FILE.write_text(until.isoformat() + "\n")
    a, b = Unit("a", room=21.0), Unit("b", room=21.0)

    async def login(session):
        return [a, b]
    monkeypatch.setattr(o, "melcloud_devices", login)

    async def no_prices(cfg, f):
        return None
    monkeypatch.setattr(o, "get_prices_or_none", no_prices)
    monkeypatch.setattr(o, "setup_logging", lambda cfg: None)
    monkeypatch.setitem(CFG["resilience"], "state_file", str(o.APP_DIR / "state.json"))
    args = argparse.Namespace(config=str(ROOT / "config.toml"), list=False, plan=False, plan_json=False,
                              report=None, control=None, lock_wait=0.0, prices_file=None, now=None,
                              outdoor_temp=5.0, sun=0.0, mode="heat", season=None, home=None,
                              assume_home=False, holiday=False, away=False, away_until=None, dry_run=False)
    assert asyncio.run(o.run(args)) == 0
    assert not o.NORMAL_FILE.exists()
