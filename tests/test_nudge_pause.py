import argparse
import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import ilp_optimizer as o

ROOT = Path(__file__).resolve().parent.parent
CFG = o.load_config(ROOT / "config.toml", ROOT / "tests" / "house.toml")
TZ = o.ZoneInfo(CFG["timezone"])
TUE_19 = datetime(2026, 11, 10, 19, 0, tzinfo=TZ)   # weekday evening, base 22 in the config


class Unit:
    def __init__(self, name, mode="heat", target=17.0, room=20.0):
        self.name, self.operation_mode, self.target_temperature = name, mode, target
        self.power, self.room_temperature, self.writes = True, room, []

    async def update(self):
        pass

    async def set(self, props):
        self.writes.append(props)
        self.target_temperature = props.get("target_temperature", self.target_temperature)


def press(value, now):
    return o.set_nudge(value, TZ, CFG, now)


def test_nudges_stack_to_the_limit_and_come_back_to_zero():
    press("warmer", TUE_19)
    assert o.read_nudge(o.NUDGE_FILE, TZ, TUE_19).shift == 1.0
    press("warmer", TUE_19 + timedelta(minutes=10))
    press("warmer", TUE_19 + timedelta(minutes=20))
    assert o.read_nudge(o.NUDGE_FILE, TZ, TUE_19 + timedelta(minutes=21)).shift == 2.0   # capped at max
    press("cooler", TUE_19 + timedelta(minutes=30))
    assert o.read_nudge(o.NUDGE_FILE, TZ, TUE_19 + timedelta(minutes=31)).shift == 1.0
    press("cooler", TUE_19 + timedelta(minutes=40))
    assert not o.NUDGE_FILE.exists()                                               # back to 0 = removed


def test_a_nudge_fades_after_three_hours_from_the_last_press_and_reset_clears_it():
    press("cooler", TUE_19)
    assert o.read_nudge(o.NUDGE_FILE, TZ, TUE_19 + timedelta(hours=2, minutes=59)).shift == -1.0
    assert o.read_nudge(o.NUDGE_FILE, TZ, TUE_19 + timedelta(hours=3, minutes=1)).shift == 0.0
    assert press("reset", TUE_19) == 0 and not o.NUDGE_FILE.exists()
    assert press("sideways", TUE_19) == 2


def test_a_bad_nudge_file_is_ignored(caplog):
    o.NUDGE_FILE.write_text("warm please\n")
    assert o.read_nudge(o.NUDGE_FILE, TZ, TUE_19).shift == 0.0
    assert "NUDGE file is not" in caplog.text


def test_the_nudge_moves_the_setpoint_and_the_limits_still_apply():
    plain = o.desired_setpoint(TUE_19, None, CFG)[2]                    # 22.0 = max_temp
    assert o.desired_setpoint(TUE_19, None, CFG, nudge=-1.0)[2] == plain - 1.0
    assert o.desired_setpoint(TUE_19, None, CFG, nudge=+1.0)[2] == plain + 1.0   # a press above 22 C is felt
    assert o.desired_setpoint(TUE_19, None, CFG, nudge=+3.0)[2] == CFG["limits"]["extreme_max_temp"]
    late = datetime(2026, 11, 10, 3, 0, tzinfo=TZ)                          # base 19
    base, offset, final, reason = o.desired_setpoint(late, None, CFG, nudge=2.0)
    assert final == 21.0 and "nudge +2.0" in reason


def test_a_too_cold_press_during_a_peak_lifts_the_cut_by_the_step():
    # a 16 C extreme cut becomes 17 C
    from datetime import timedelta as td
    start, _ = o.local_day_bounds(TUE_19.date(), TZ)
    prices = [o.PriceInterval(start + td(hours=k), start + td(hours=k + 1), 60.0 if k == 12 else 5.0)
              for k in range(24)]
    noon = datetime(2026, 11, 10, 12, 0, tzinfo=TZ)
    assert o.desired_setpoint(noon, prices, CFG)[2] == 16.0
    assert o.desired_setpoint(noon, prices, CFG, nudge=1.0)[2] == 17.0


def test_nudge_reaches_the_units_through_apply_to_units():
    unit = Unit("Unit B", target=22.0, room=21.0)
    asyncio.run(o.apply_to_units([unit], TUE_19, None, CFG, False, nudge=-1.0))
    assert unit.writes == [{"target_temperature": 21.0}]


def settle(unit_list, monkeypatch, dry_run=False):
    async def login(session):
        return unit_list
    monkeypatch.setattr(o, "melcloud_devices", login)
    args = argparse.Namespace(now=TUE_19.isoformat(), dry_run=dry_run)
    return asyncio.run(o.settle_for_pause(args, CFG, TZ))


def test_pause_on_settles_both_units_to_the_safe_temperature_then_hands_off(monkeypatch):
    assert o.set_control("pause", "on", TZ, CFG) == 0
    cut = Unit("Unit A", target=17.0)        # left on a price cut
    hot = Unit("Unit B", target=23.5)
    assert settle([cut, hot], monkeypatch) == 0
    assert cut.writes == [{"target_temperature": 21.0}] and hot.writes == [{"target_temperature": 21.0}]
    assert o.PAUSE_FILE.exists() and o.PAUSE_FILE.read_text().strip() == ""   # now a plain pause


def test_a_failed_settle_keeps_the_pause_open_for_the_next_run(monkeypatch):
    o.set_control("pause", "on", TZ, CFG)
    broken = Unit("Unit A")

    async def boom(props):
        raise OSError("down")
    broken.set = boom
    assert settle([broken], monkeypatch) == 1
    assert o.PAUSE_FILE.read_text().strip() == "settle"


def test_pause_hold_leaves_the_units_alone_and_a_settled_pause_does_nothing(monkeypatch):
    assert o.set_control("pause", "hold", TZ, CFG) == 0
    assert o.PAUSE_FILE.read_text() == ""
    assert o.set_control("pause", "off", TZ, CFG) == 0 and not o.PAUSE_FILE.exists()


def test_the_live_run_routes_a_settle_pause(monkeypatch):
    o.set_control("pause", "on", TZ, CFG)
    called = []

    async def fake_settle(args, cfg, tz):
        called.append(1)
        return 0
    monkeypatch.setattr(o, "settle_for_pause", fake_settle)
    monkeypatch.setattr(o, "setup_logging", lambda cfg: None)
    assert o.main(["--config", str(ROOT / "config.toml"), "--dry-run"]) == 0
    assert called == [1]
