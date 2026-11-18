import asyncio
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import ilp_optimizer as o

ROOT = Path(__file__).resolve().parent.parent
LIVE = o.load_config(ROOT / "config.toml", ROOT / "tests" / "house.toml")
# These tests are about writing to the units: noon on a weekday at base 19, without the nobody-home
# window that would hold it at its floor.
CFG = {**LIVE, "nobody_home": {**LIVE["nobody_home"], "weekday": None}}
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=ZoneInfo("Europe/Helsinki"))  # base 19, no prices

# a and b on one outdoor unit (a multi-split)
SPLIT = {**CFG, "units": {**CFG["units"], "outdoor_groups": [["a", "b"]]}}


class FakeUnit:
    def __init__(self, name, target, power=True, mode="heat", fail=False):
        self.name, self.target_temperature = name, target
        self.power, self.operation_mode, self.fail = power, mode, fail
        self.writes = []
        self.room_temperature = 20.5

    async def update(self):
        if self.fail == "update":
            raise OSError("update failed")

    async def set(self, props):
        if self.fail == "set":
            raise OSError("set failed")
        self.writes.append(props)
        self.target_temperature = props.get("target_temperature", self.target_temperature)
        self.operation_mode = props.get("operation_mode", self.operation_mode)


def run(devices, cfg=CFG, dry_run=False):
    return asyncio.run(o.apply_to_units(devices, NOW, None, cfg, dry_run))


def test_no_write_when_at_target():
    u = FakeUnit("up", 19.0)
    assert run([u]) == 0 and u.writes == []


def test_no_write_when_within_half_degree_noise():
    u = FakeUnit("up", 19.25)  # |diff| < 0.5
    run([u])
    assert u.writes == []


def test_writes_when_different():
    u = FakeUnit("up", 21.0)
    run([u])
    assert u.writes == [{"target_temperature": 19.0}]


def test_skips_off_units_and_unsupported_modes():
    for kwargs in ({"power": False}, {"mode": "fan_only"}, {"mode": "dry"}, {"mode": "heat_cool"}):
        u = FakeUnit("a", 25, **kwargs)  # one unit per run: mixed running modes block instead
        run([u])
        assert u.writes == [], kwargs


def test_auto_mode_is_not_controlled():
    u = FakeUnit("a", 25, mode="heat_cool")
    run([u])
    assert u.writes == []


def test_only_changed_units_written():
    a, b = FakeUnit("a", 19.0), FakeUnit("b", 22.0)
    run([a, b])
    assert a.writes == [] and b.writes == [{"target_temperature": 19.0}]


def test_failure_on_one_unit_does_not_stop_the_other():
    bad_set, bad_update, good = FakeUnit("x", 22, fail="set"), FakeUnit("y", 22, fail="update"), FakeUnit("z", 22)
    assert run([bad_set, bad_update, good]) == 2
    assert good.writes == [{"target_temperature": 19.0}]


def test_dry_run_never_writes():
    u = FakeUnit("a", 22)
    run([u], dry_run=True)
    assert u.writes == []


def test_unit_names_filter_and_offset():
    cfg = {**CFG, "units": {"names": ["a"], "offsets": {"a": 1.0}}}
    a, b = FakeUnit("a", 25), FakeUnit("b", 25)
    run([a, b], cfg)
    assert a.writes == [{"target_temperature": 20.0}] and b.writes == []


def test_only_target_temperature_is_ever_set():
    u = FakeUnit("a", 25)
    run([u])
    assert all(set(w) == {"target_temperature"} for w in u.writes)


def test_pause_file_disables_control(tmp_path, monkeypatch):
    (tmp_path / "PAUSE").write_text("")
    monkeypatch.setattr(o, "APP_DIR", tmp_path)
    monkeypatch.setattr(o, "PAUSE_FILE", tmp_path / "PAUSE")

    async def must_not_login(session):
        raise AssertionError("MELCloud must not be contacted while paused")
    monkeypatch.setattr(o, "melcloud_devices", must_not_login)
    args = o.argparse.Namespace(config=str(ROOT / "config.toml"), list=False, plan=False, report=None, control=None, lock_wait=0.0, plan_json=False,
                                prices_file=None, now=None, dry_run=False, outdoor_temp=None, away=False, away_until=None,
                                season=None, holiday=False, sun=None,
                                home=None, assume_home=False)
    try:
        assert asyncio.run(o.run(args)) == 0
    finally:
        for h in list(o.log.handlers):
            o.log.removeHandler(h)
            h.close()
    assert "PAUSE" in (tmp_path / "ilp_optimizer.log").read_text()


def history_rows(path):
    import csv
    return list(csv.DictReader(open(path)))


def test_history_has_a_row_per_unit_per_run_with_room_temperature(tmp_path):
    hist = tmp_path / "history.csv"
    changed, same = FakeUnit("a", 22.0), FakeUnit("b", 19.0)
    asyncio.run(o.apply_to_units([changed, same], NOW, None, CFG, False, -3.0, hist))
    rows = {r["unit"]: r for r in history_rows(hist)}
    assert (rows["a"]["action"], rows["a"]["old_target"], rows["a"]["new_target"]) == ("set", "22.0", "19.0")
    assert (rows["b"]["action"], rows["b"]["old_target"], rows["b"]["new_target"]) == ("keep", "19.0", "19.0")
    assert all(r["room_temp"] == "20.5" and r["outdoor_mean"] == "-3.0" for r in rows.values())
    assert rows["a"]["time_local"].startswith("2026-11-10T12:00:00")
    asyncio.run(o.apply_to_units([same], NOW, None, CFG, False, -3.0, hist))
    assert len(history_rows(hist)) == 3  # appended, not overwritten


def test_history_records_skipped_dry_run_and_failed_writes(tmp_path):
    hist = tmp_path / "history.csv"
    off, cool, dry, bad = (FakeUnit("off", 25, power=False), FakeUnit("cool", 25, mode="fan_only"),
                           FakeUnit("dry", 22.0), FakeUnit("bad", 22.0, fail="set"))
    # separate runs: units in different running modes would (rightly) block each other
    asyncio.run(o.apply_to_units([off, dry], NOW, None, CFG, True, None, hist))
    asyncio.run(o.apply_to_units([cool], NOW, None, CFG, False, None, hist))
    asyncio.run(o.apply_to_units([bad], NOW, None, CFG, False, None, hist))
    assert [(r["unit"], r["action"]) for r in history_rows(hist)] == [
        ("off", "skip"), ("dry", "dry-run"), ("cool", "skip"), ("bad", "set-failed")]
    assert [u.writes for u in (off, cool, dry, bad)] == [[], [], [], []]


def test_history_failure_does_not_stop_control(tmp_path):
    u = FakeUnit("a", 22.0)
    # a directory in place of the file makes every history write fail
    assert asyncio.run(o.apply_to_units([u], NOW, None, CFG, False, None, tmp_path)) == 0
    assert u.writes == [{"target_temperature": 19.0}]



def test_comfort_guard_applies_per_unit_room_temperature():
    from datetime import timedelta
    tz = ZoneInfo("Europe/Helsinki")
    start, end = o.local_day_bounds(NOW.astimezone(tz).date(), tz)
    prices = []
    for k in range(24):  # 25c at 12:00 (extreme cut), 2c at 03:00, 9c otherwise
        price = 25.0 if k == 12 else (2.0 if k == 3 else 9.0)
        prices.append(o.PriceInterval(start + timedelta(hours=k), start + timedelta(hours=k + 1), price))
    cold, warm = FakeUnit("cold", 19.0), FakeUnit("warm", 19.0)
    cold.room_temperature, warm.room_temperature = 16.5, 20.5
    asyncio.run(o.apply_to_units([cold, warm], NOW, prices, CFG, False))
    assert cold.writes == []                              # guard: no cut, stays at 19
    assert warm.writes == [{"target_temperature": 16.0}]  # extreme cut applies


def test_away_sets_the_nobody_home_17_without_prices_and_records_it(tmp_path):
    hist = tmp_path / "history.csv"
    u = FakeUnit("a", 21.0)
    asyncio.run(o.apply_to_units([u], NOW, None, CFG, False, None, hist, o.Away()))
    assert u.writes == [{"target_temperature": 17.0}]
    assert history_rows(hist)[0]["reason"] == "away, nobody home: 17"


# ---- units share one outdoor unit: running units must be in the same mode ----

def test_mismatched_modes_on_one_outdoor_unit_block_its_changes(tmp_path):
    hist = tmp_path / "history.csv"
    heat, cool = FakeUnit("a", 25.0, mode="heat"), FakeUnit("b", 25.0, mode="cool")
    assert asyncio.run(o.apply_to_units([heat, cool], NOW, None, SPLIT, False, None, hist)) >= 1
    assert heat.writes == cool.writes == []
    assert {r["action"] for r in history_rows(hist)} == {"mode-mismatch"}


def test_any_difference_in_running_modes_blocks(tmp_path):
    for other in ("heat_cool", "fan_only", "dry"):
        heat, u = FakeUnit("a", 25.0), FakeUnit("b", 25.0, mode=other)
        assert run([heat, u], SPLIT) >= 1
        assert heat.writes == u.writes == []


def test_a_unit_that_is_off_does_not_count_as_a_different_mode():
    heat, off = FakeUnit("a", 25.0), FakeUnit("b", 25.0, power=False, mode="cool")
    assert run([heat, off]) == 0
    assert heat.writes == [{"target_temperature": 19.0}] and off.writes == []


def test_same_mode_still_controlled():
    a, b = FakeUnit("a", 25.0), FakeUnit("b", 25.0)
    assert run([a, b]) == 0 and a.writes and b.writes


def test_uncontrolled_unit_on_the_same_outdoor_unit_in_another_mode_still_blocks():
    cfg = {**CFG, "units": {"names": ["a"], "offsets": {}, "outdoor_groups": [["a", "other"]]}}
    a, other = FakeUnit("a", 25.0), FakeUnit("other", 25.0, mode="cool")
    assert run([a, other], cfg) >= 1 and a.writes == []


def test_self_contained_units_in_different_modes_are_both_controlled():
    cfg = {**CFG, "units": {**CFG["units"], "outdoor_groups": []}}
    heat, cool = FakeUnit("a", 25.0, mode="heat"), FakeUnit("b", 25.0, mode="cool")
    assert run([heat, cool], cfg) == 0
    assert heat.writes and cool.writes


def test_a_mismatched_multi_split_does_not_stop_a_self_contained_unit():
    cfg = {**CFG, "units": {**CFG["units"], "outdoor_groups": [["a", "b"]]}}
    a, b, c = FakeUnit("a", 25.0, mode="heat"), FakeUnit("b", 25.0, mode="cool"), FakeUnit("c", 25.0)
    assert run([a, b, c], cfg) == 1
    assert a.writes == b.writes == [] and c.writes == [{"target_temperature": 19.0}]


def test_a_single_unit_is_controlled():
    cfg = {**CFG, "units": {**CFG["units"], "outdoor_groups": []}}
    only = FakeUnit("solo", 25.0)
    assert run([only], cfg) == 0 and only.writes == [{"target_temperature": 19.0}]


def test_unreadable_unit_does_not_stop_the_readable_one():
    bad, good = FakeUnit("x", 25.0, fail="update"), FakeUnit("y", 25.0)
    assert run([bad, good]) == 1
    assert good.writes == [{"target_temperature": 19.0}]


def test_without_offsets_both_units_get_the_plan():
    assert CFG["units"]["offsets"] == {}                     # the downstairs +0.5 went on 16.11.
    down, up = FakeUnit("Unit A", 25.0), FakeUnit("Unit B", 25.0)
    assert run([down, up]) == 0
    assert down.writes == [{"target_temperature": 19.0}]
    assert up.writes == [{"target_temperature": 19.0}]


# ---- manual-change hold ----------------------------------------------------

from datetime import timedelta


def hold_run(units, state, now=NOW, dry_run=False):
    return asyncio.run(o.apply_to_units(units, now, None, CFG, dry_run, None, None, None, state))


def test_our_own_write_does_not_start_a_hold(tmp_path):
    state = o.UnitState(tmp_path / "s.json")
    u = FakeUnit("a", 22.0)
    hold_run([u], state)                       # run 1: we set 19.0
    hold_run([u], o.UnitState(tmp_path / "s.json"), NOW + timedelta(minutes=15))
    assert u.writes == [{"target_temperature": 19.0}]  # run 2: keep, no hold, no extra write


def test_manual_change_holds_the_unit_then_control_resumes(tmp_path):
    path = tmp_path / "s.json"
    u = FakeUnit("a", 22.0)
    hold_run([u], o.UnitState(path))                          # we set 19.0
    u.target_temperature = 22.0                               # owner turns it up at the wall
    hold_run([u], o.UnitState(path), NOW + timedelta(minutes=15))
    hold_run([u], o.UnitState(path), NOW + timedelta(minutes=45))
    assert u.writes == [{"target_temperature": 19.0}]         # held: not overwritten
    hold_run([u], o.UnitState(path), NOW + timedelta(minutes=80))  # hold from 12:15 ended 13:15
    assert u.writes[-1] == {"target_temperature": 19.0} and len(u.writes) == 2


def test_a_further_manual_change_extends_the_hold(tmp_path):
    path = tmp_path / "s.json"
    u = FakeUnit("a", 22.0)
    hold_run([u], o.UnitState(path))
    u.target_temperature = 22.0
    hold_run([u], o.UnitState(path), NOW + timedelta(minutes=15))   # hold until 13:15
    u.target_temperature = 23.0
    hold_run([u], o.UnitState(path), NOW + timedelta(minutes=50))   # hold until 13:50
    hold_run([u], o.UnitState(path), NOW + timedelta(minutes=80))   # still held
    assert len(u.writes) == 1


def test_hold_is_per_unit(tmp_path):
    path = tmp_path / "s.json"
    a, b = FakeUnit("a", 22.0), FakeUnit("b", 22.0)
    hold_run([a, b], o.UnitState(path))
    a.target_temperature = 21.0  # only a is changed by hand; b stays at our 19.0
    hold_run([a, b], o.UnitState(path), NOW + timedelta(minutes=15))
    assert len(a.writes) == 1 and len(b.writes) == 1  # a held, b kept at 19.0


def test_dry_run_does_not_change_state(tmp_path):
    path = tmp_path / "s.json"
    hold_run([FakeUnit("a", 22.0)], o.UnitState(path), dry_run=True)
    assert not path.exists()


def test_skipped_unit_forgets_its_last_target(tmp_path):
    path = tmp_path / "s.json"
    u = FakeUnit("a", 22.0)
    hold_run([u], o.UnitState(path))
    u.power = False
    hold_run([u], o.UnitState(path), NOW + timedelta(minutes=15))
    u.power, u.target_temperature = True, 25.0  # changed while off: not a manual hold
    hold_run([u], o.UnitState(path), NOW + timedelta(minutes=30))
    assert u.writes[-1] == {"target_temperature": 19.0}


def test_hold_can_be_switched_off(tmp_path):
    cfg = {**CFG, "control": {**CFG["control"], "manual_hold_minutes": 0}}
    path = tmp_path / "s.json"
    u = FakeUnit("a", 22.0)
    asyncio.run(o.apply_to_units([u], NOW, None, cfg, False, None, None, None, o.UnitState(path)))
    u.target_temperature = 22.0
    asyncio.run(o.apply_to_units([u], NOW + timedelta(minutes=15), None, cfg, False, None, None, None, o.UnitState(path)))
    assert len(u.writes) == 2


def test_hold_is_recorded_in_history(tmp_path):
    path, hist = tmp_path / "s.json", tmp_path / "h.csv"
    u = FakeUnit("a", 22.0)
    asyncio.run(o.apply_to_units([u], NOW, None, CFG, False, None, hist, None, o.UnitState(path)))
    u.target_temperature = 22.0
    asyncio.run(o.apply_to_units([u], NOW + timedelta(minutes=15), None, CFG, False, None, hist, None, o.UnitState(path)))
    assert [r["action"] for r in history_rows(hist)] == ["set", "hold"]
