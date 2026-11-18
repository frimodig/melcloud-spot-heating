import csv
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import ilp_optimizer as o

ROOT = Path(__file__).resolve().parent.parent
CFG = o.load_config(ROOT / "config.toml", ROOT / "tests" / "house.toml")
TZ = o.ZoneInfo(CFG["timezone"])
DAYS = [date(2026, 11, 8), date(2026, 11, 9), date(2026, 11, 10)]
K_TRUE = 0.0016            # kWh per unit-degree-quarter
UNITS = ("Unit A", "Unit B")
OUTDOOR = 2.0


def quarters(days=DAYS):
    for d in days:
        start, end = o.local_day_bounds(d, TZ)
        t = start
        while t < end:
            yield t.astimezone(TZ)        # local_day_bounds gives UTC datetimes; the files carry local time
            t += timedelta(minutes=15)


def baseline_at(t, unit):
    return o.base_setpoint(t, {"schedule": CFG["baseline"]})       # the literal baseline: a constant 22 C


def price_of(t):
    return 5.0 if t.hour < 6 else 20.0         # a cheap night, an expensive day


def write_files(tmp_path, setpoint_of, update_every=8, outdoor_in_telemetry=True, with_meters=True, days=DAYS):
    """History, prices and telemetry for `days`. The meters hold the true cumulative energy but only
    update every `update_every` quarters (2 h), and the first unit carries 90% of it."""
    hist = tmp_path / "history-2026-11.csv"
    with open(hist, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=o.HISTORY_COLUMNS)
        w.writeheader()
        for t in quarters(days):
            for u in UNITS:
                sp = setpoint_of(t, u)
                w.writerow({"time_local": t.isoformat(timespec="seconds"), "unit": u, "action": "keep",
                            "room_temp": 20.0, "old_target": sp, "new_target": sp, "outdoor_mean": OUTDOOR})
    with open(tmp_path / CFG["price_history_file"], "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(o.PRICE_COLUMNS)
        for t in quarters(days):
            w.writerow([t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), 15, price_of(t)])
    tel = tmp_path / "telemetry-2026-11.csv"
    with open(tel, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=o.TELEMETRY_COLUMNS)
        w.writeheader()
        cum = {u: 1000.0 if u == UNITS[0] else 50.0 for u in UNITS}      # lifetime counters
        shown = dict(cum)
        for i, t in enumerate(quarters(days)):
            for u, share in zip(UNITS, (0.9, 0.1)):
                cum[u] += K_TRUE * share * sum(max(setpoint_of(t, v) - OUTDOOR, 0) for v in UNITS)
            if i % update_every == 0:
                shown = dict(cum)
            for u in UNITS:
                w.writerow({"time_local": t.isoformat(timespec="seconds"), "unit": u, "room_temp": 20.0,
                            "target_temp": 20.0, "power": True, "mode": "heat", "fan_actual": 1,
                            "energy_kwh": round(shown[u], 4) if with_meters else "", "has_meter": with_meters,
                            "outdoor_c": OUTDOOR if outdoor_in_telemetry else ""})


def at_baseline(t, u):
    return baseline_at(t, u)


def lower(t, u):
    return baseline_at(t, u) - 1.0


# ---- the building blocks -----------------------------------------------------------------------

def at(h, m=0, day=DAYS[0]):
    return datetime(day.year, day.month, day.day, h, m, tzinfo=TZ).astimezone(timezone.utc)


def test_only_meter_changes_count_and_a_reset_adds_nothing():
    readings = [(at(0), 100.0), (at(0, 15), 100.0), (at(1), 100.4), (at(1, 15), 100.4), (at(2), 100.9),
                (at(3), 5.0), (at(4), 5.5)]                       # a reset to 5.0 at 03:00
    pts = o.meter_energy_points(readings)
    assert pts == [(at(0), 0.0), (at(1), pytest.approx(0.4)), (at(2), pytest.approx(0.9)),
                   (at(3), pytest.approx(0.9)), (at(4), pytest.approx(1.4))]


def test_the_total_is_the_sum_over_the_time_every_meter_has_data_for():
    a = [(at(0), 10.0), (at(2), 10.5), (at(4), 11.0)]
    b = [(at(1), 5.0), (at(3), 5.2), (at(5), 5.4)]
    total = o.total_meter_energy({"a": a, "b": b})
    assert total[0][0] == at(1) and total[-1][0] == at(4)             # the overlap of the two series
    # a: 0.25 at 01:00 to 1.0 at 04:00 = 0.75; b: 0 at 01:00 to 0.3 at 04:00 (interpolated between 03:00 and 05:00) = 0.3
    assert total[-1][1] - total[0][1] == pytest.approx(0.75 + 0.3)
    assert o.total_meter_energy({"a": a, "b": [(at(1), 5.0)]}) == o.total_meter_energy({"a": a})   # one reading is no series


def test_allocation_conserves_energy_follows_the_weights_and_handles_partial_quarters():
    total = [(at(0), 0.0), (at(1), 1.2)]                              # one hour, 1.2 kWh
    weights = {at(0): 3.0, at(0, 15): 1.0, at(0, 30): 1.0, at(0, 45): 1.0}
    out = o.allocate_energy(total, lambda q: weights[q])
    assert sum(out.values()) == pytest.approx(1.2) and out[at(0)] == pytest.approx(0.6)
    flat = o.allocate_energy(total, lambda q: 0.0)                    # no demand anywhere: spread evenly
    assert all(v == pytest.approx(0.3) for v in flat.values())
    part = o.allocate_energy([(at(0, 5), 0.0), (at(0, 20), 0.3)], lambda q: 1.0)     # 10 min of one quarter, 5 of the next
    assert sum(part.values()) == pytest.approx(0.3)
    assert part[at(0)] == pytest.approx(0.2) and part[at(0, 15)] == pytest.approx(0.1)


# ---- the whole model on data with a known answer ------------------------------------------------

def test_the_calibration_recovers_the_true_energy_per_degree(tmp_path):
    write_files(tmp_path, lower)
    meters, outdoor_q = o.load_telemetry(tmp_path, CFG)
    setpoints, outdoor_day = o.load_actual_setpoints(tmp_path, CFG)
    demand = o.demand_quarters(setpoints, outdoor_q, outdoor_day, CFG)
    total = o.total_meter_energy(meters)
    measured = o.allocate_energy(total, lambda q: demand.get(q, (0, 0))[0])
    model = o.fit_energy_model(demand, measured, TZ, 2)
    assert model.k == pytest.approx(K_TRUE, rel=0.03)
    assert model.days_used >= 2 and model.spread_pct is not None and model.spread_pct < 6


def test_the_report_is_a_saving_when_the_setpoints_are_lower_than_the_baseline(tmp_path):
    write_files(tmp_path, lower)
    text = o.energy_report(tmp_path, CFG)
    assert text and "Wh per unit-degree-quarter" in text and "ROUGH ESTIMATE" in text
    assert "1.60 Wh per unit-degree-quarter" in text and "about\n6.4 W per degree" in text
    sp, od = o.load_actual_setpoints(tmp_path, CFG)
    meters, oq = o.load_telemetry(tmp_path, CFG)
    demand = o.demand_quarters(sp, oq, od, CFG)
    measured = o.allocate_energy(o.total_meter_energy(meters), lambda q: demand.get(q, (0, 0))[0])
    model = o.fit_energy_model(demand, measured, TZ, 2)
    days = o.energy_by_day(demand, measured, o.load_price_quarters(tmp_path / CFG["price_history_file"]), model, TZ)
    month = o.EnergyDay()
    for d in days.values():
        month.add(d)
    assert month.saved_kwh > 0 and month.saved_eur > 0 and 0 < month.saved_pct < 40
    assert month.less_heat_eur + month.cheaper_hours_eur == pytest.approx(month.saved_eur)
    assert month.kwh_model == pytest.approx(month.kwh_measured, rel=0.08)      # the model explains the meters


def test_the_same_setpoints_as_the_baseline_save_nothing(tmp_path):
    write_files(tmp_path, at_baseline)
    sp, od = o.load_actual_setpoints(tmp_path, CFG)
    meters, oq = o.load_telemetry(tmp_path, CFG)
    demand = o.demand_quarters(sp, oq, od, CFG)
    measured = o.allocate_energy(o.total_meter_energy(meters), lambda q: demand.get(q, (0, 0))[0])
    model = o.fit_energy_model(demand, measured, TZ, 2)
    days = o.energy_by_day(demand, measured, o.load_price_quarters(tmp_path / CFG["price_history_file"]), model, TZ)
    assert all(abs(d.saved_eur) < 1e-9 and abs(d.saved_kwh) < 1e-9 for d in days.values())


def test_shifting_the_same_heat_to_cheap_hours_is_cheaper_hours_not_less_heat(tmp_path):
    def shifted(t, u):                                    # +3 C for 6 cheap night hours, -1 C for 18 dear hours: same heat
        return baseline_at(t, u) + (3.0 if t.hour < 6 else -1.0)
    write_files(tmp_path, shifted)
    sp, od = o.load_actual_setpoints(tmp_path, CFG)
    meters, oq = o.load_telemetry(tmp_path, CFG)
    demand = o.demand_quarters(sp, oq, od, CFG)
    measured = o.allocate_energy(o.total_meter_energy(meters), lambda q: demand.get(q, (0, 0))[0])
    model = o.fit_energy_model(demand, measured, TZ, 2)
    days = o.energy_by_day(demand, measured, o.load_price_quarters(tmp_path / CFG["price_history_file"]), model, TZ)
    month = o.EnergyDay()
    for d in days.values():
        month.add(d)
    assert month.saved_eur > 0 and month.cheaper_hours_eur > abs(month.less_heat_eur) * 2


def test_the_telemetry_outdoor_temperature_beats_the_daily_mean(tmp_path):
    write_files(tmp_path, lower, outdoor_in_telemetry=True)
    meters, oq = o.load_telemetry(tmp_path, CFG)
    assert set(oq.values()) == {OUTDOOR}
    sp, od = o.load_actual_setpoints(tmp_path, CFG)
    q = next(iter(sp))[1]
    assert o.demand_quarters({("Unit A", q): 20.0}, {q: 10.0}, {q.astimezone(TZ).date(): 0.0}, CFG)[q][0] == 10.0
    assert o.demand_quarters({("Unit A", q): 20.0}, {}, {q.astimezone(TZ).date(): 0.0}, CFG)[q][0] == 20.0


def test_without_meter_readings_there_is_no_energy_report_and_the_cli_falls_back(tmp_path, monkeypatch, capsys):
    write_files(tmp_path, lower, with_meters=False)
    assert o.energy_report(tmp_path, CFG) is None
    monkeypatch.setattr(o, "APP_DIR", tmp_path)
    assert o.main(["--config", str(ROOT / "config.toml"), "--report"]) == 0
    out = capsys.readouterr().out
    assert "RELATIVE estimate" in out and "No kWh/EUR yet" in out


def test_the_cli_prints_the_energy_report_when_the_meters_are_there(tmp_path, monkeypatch, capsys):
    write_files(tmp_path, lower)
    monkeypatch.setattr(o, "APP_DIR", tmp_path)
    assert o.main(["--config", str(ROOT / "config.toml"), "--report", "2026-11"]) == 0
    out = capsys.readouterr().out
    assert "ROUGH ESTIMATE" in out and "less heat" in out and "2026-11-10" in out and "2026-11" in out


def test_gaps_in_the_meter_data_leave_those_quarters_unmeasured(tmp_path):
    write_files(tmp_path, lower)
    path = tmp_path / "telemetry-2026-11.csv"
    rows = list(csv.DictReader(open(path)))
    day9 = [r for r in rows if r["time_local"].startswith("2026-11-09")]
    keep = [r for r in rows if r not in day9]                         # the whole 9th is missing (the VM was off)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=o.TELEMETRY_COLUMNS)
        w.writeheader()
        w.writerows(keep)
    meters, oq = o.load_telemetry(tmp_path, CFG)
    sp, od = o.load_actual_setpoints(tmp_path, CFG)
    demand = o.demand_quarters(sp, oq, od, CFG)
    measured = o.allocate_energy(o.total_meter_energy(meters), lambda q: demand.get(q, (0, 0))[0])
    model = o.fit_energy_model(demand, measured, TZ, 2)
    assert model.k == pytest.approx(K_TRUE, rel=0.05)                  # the gap is spread, the total energy is conserved


def test_report_can_read_a_separate_data_folder(tmp_path, monkeypatch, capsys):
    data, elsewhere = tmp_path / "vm", tmp_path / "app"
    data.mkdir(); elsewhere.mkdir()
    write_files(data, lower)
    monkeypatch.setattr(o, "APP_DIR", elsewhere)                # the app folder has no files at all
    assert o.main(["--config", str(ROOT / "config.toml"), "--report", "--data-dir", str(data)]) == 0
    assert "ROUGH ESTIMATE" in capsys.readouterr().out


def test_the_baseline_is_a_constant_22_for_every_unit_and_the_report_says_so(tmp_path):
    for t in list(quarters())[::7]:
        assert baseline_at(t, "Unit A") == baseline_at(t, "Unit B") == 22.0      # no unit offsets on the baseline
    write_files(tmp_path, lower)
    assert "against a constant +22 C: a ROUGH ESTIMATE" in o.energy_report(tmp_path, CFG)
    assert "against a constant +22 C" in o.savings_report(tmp_path, CFG)
    assert o.baseline_label({"baseline": {"weekday": [], "weekend": []}}) == "the baseline schedule"
