"""What a new install gets from config.toml alone: neutral defaults, the household rules off."""
import asyncio
from datetime import datetime
from pathlib import Path

import ilp_optimizer as o

ROOT = Path(__file__).resolve().parent.parent
CFG = o.load_config(ROOT / "config.toml")
TZ = o.ZoneInfo(CFG["timezone"])
TUE = datetime(2026, 11, 10, 12, 0, tzinfo=TZ)


def at(hh, mm=0):
    return TUE.replace(hour=hh, minute=mm)


def test_a_new_install_holds_a_flat_21_without_prices():
    assert {o.desired_setpoint(at(h), None, CFG, unit="Living room")[2] for h in range(24)} == {21.0}


def test_the_household_rules_are_off_by_default():
    assert o.optional(CFG, "night_shift") is None
    assert CFG["nobody_home"]["weekday"] == []
    assert CFG["above_average"]["keep_weekday"] == CFG["above_average"]["keep_weekend"] == []
    assert CFG["units"]["outdoor_groups"] == [] and o.configured_unit_names(CFG) == set()


def test_the_page_plan_and_a_unit_agree_at_night_by_default():
    for t in (at(2), at(23)):
        assert o.desired_setpoint(t, None, CFG)[2] == o.desired_setpoint(t, None, CFG, unit="Bedroom")[2]


def test_a_rule_switched_off_in_the_local_config_is_off(tmp_path):
    local = tmp_path / "config.local.toml"
    local.write_text('[spikes]\nenabled = false\n')
    assert o.optional(o.load_config(ROOT / "config.toml", local), "spikes") is None
    assert o.optional(CFG, "spikes") is not None


class Unit:
    def __init__(self, name):
        self.name, self.operation_mode, self.power = name, "heat", True
        self.target_temperature, self.room_temperature, self.writes = 21.0, 20.0, []

    async def update(self):
        pass

    async def set(self, props):
        self.writes.append(props)


def test_a_unit_name_the_config_mentions_but_melcloud_lacks_is_logged(caplog):
    cfg = {**CFG, "units": {**CFG["units"], "solar_weight": {"Livingroom": 1.0}}}
    asyncio.run(o.apply_to_units([Unit("Living room")], at(12), None, cfg, True))
    assert "config names units MELCloud doesn't have" in caplog.text and "Livingroom" in caplog.text
