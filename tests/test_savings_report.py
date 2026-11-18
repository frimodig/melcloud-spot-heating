import csv
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import ilp_optimizer as o

ROOT = Path(__file__).resolve().parent.parent
CFG = o.load_config(ROOT / "config.toml", ROOT / "tests" / "house.toml")
TZ = o.ZoneInfo(CFG["timezone"])
DAY = date(2026, 11, 10)  # a Tuesday


def quarters(day=DAY):
    start, end = o.local_day_bounds(day, TZ)
    t = start
    while t < end:
        yield t
        t += timedelta(minutes=15)


def write_history(path, setpoint_of, units=("Unit A",), day=DAY, outdoor=0.0, action="keep"):
    new = not path.exists()
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=o.HISTORY_COLUMNS)
        if new:
            w.writeheader()
        for t in quarters(day):
            for u in units:
                sp = setpoint_of(t)
                w.writerow({"time_local": t.isoformat(timespec="seconds"), "unit": u, "action": action,
                            "room_temp": 20.0, "old_target": sp, "new_target": sp,
                            "outdoor_mean": outdoor})


def write_prices(path, price_of, day=DAY):
    new = not path.exists()
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(o.PRICE_COLUMNS)
        for t in quarters(day):
            w.writerow([t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), 15, price_of(t)])


def baseline_at(t, unit="Unit A"):
    """The baseline's setpoint (the literal one, no unit offset): a constant 22 C from config.toml."""
    return o.base_setpoint(t, {"schedule": CFG["baseline"]})


def report_days(tmp_path):
    sp, out = o.load_actual_setpoints(tmp_path, CFG)
    return o.savings_by_day(sp, o.load_price_quarters(tmp_path / CFG["price_history_file"]), out, CFG)


def setup(tmp_path, setpoint_of, price_of=lambda t: 10.0, **kw):
    write_history(tmp_path / "history-2026-11.csv", setpoint_of, **kw)
    write_prices(tmp_path / CFG["price_history_file"], price_of)
    return report_days(tmp_path)[DAY]


def test_same_as_the_baseline_saves_nothing(tmp_path):
    d = setup(tmp_path, baseline_at)
    assert d.quarters == 96
    assert d.saving_pct == pytest.approx(0.0) and d.heat_pct == pytest.approx(0.0)


def test_lower_setpoint_saves_in_proportion_to_the_heat_demand(tmp_path):
    d = setup(tmp_path, lambda t: baseline_at(t) - 1.0, outdoor=0.0)
    expected = 100 * (1 - sum(baseline_at(t) - 1.0 for t in quarters()) / sum(baseline_at(t) for t in quarters()))
    assert d.saving_pct == pytest.approx(expected)
    assert d.price_pct == pytest.approx(0.0)  # flat price: no timing effect


def test_shifting_heat_to_cheap_hours_shows_as_price_not_less_heat(tmp_path):
    # Same total demand: +1 C in the cheap first half of the day, -1 C in the dear second half.
    first = lambda t: t.astimezone(TZ).hour < 12
    price = lambda t: 5.0 if first(t) else 15.0
    sp = lambda t: baseline_at(t) + (1.0 if first(t) else -1.0)
    d = setup(tmp_path, sp, price)
    assert d.heat_pct == pytest.approx(0.0, abs=1e-9)
    assert d.price_pct < 0 and d.saving_pct > 0


def test_dry_runs_are_not_counted(tmp_path):
    write_history(tmp_path / "history-2026-11.csv", baseline_at, action="dry-run")
    write_prices(tmp_path / CFG["price_history_file"], lambda t: 10.0)
    assert report_days(tmp_path) == {}


def test_hold_uses_the_target_the_unit_really_had(tmp_path):
    path = tmp_path / "history-2026-11.csv"
    t = next(iter(quarters()))
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=o.HISTORY_COLUMNS)
        w.writeheader()
        w.writerow({"time_local": t.isoformat(), "unit": "U", "action": "hold", "old_target": 21.5,
                    "new_target": "", "outdoor_mean": 0.0})
    sp, _ = o.load_actual_setpoints(tmp_path, CFG)
    assert list(sp.values()) == [21.5]


def test_quarters_without_a_price_or_outdoor_mean_are_skipped(tmp_path):
    write_history(tmp_path / "history-2026-11.csv", baseline_at)  # outdoor 0.0 present
    # prices for the morning only
    write_prices(tmp_path / CFG["price_history_file"], lambda t: 10.0)
    p = tmp_path / CFG["price_history_file"]
    rows = p.read_text().splitlines()
    p.write_text("\n".join(rows[:1 + 40]) + "\n")
    assert report_days(tmp_path)[DAY].quarters == 40


def test_last_row_in_a_quarter_wins_and_two_units_add_up(tmp_path):
    d = setup(tmp_path, baseline_at, units=("Unit A", "Unit B"))
    assert d.quarters == 192
    path = tmp_path / "history-2026-11.csv"
    t = next(iter(quarters()))
    with open(path, "a", newline="") as f:  # a manual run in the same quarter, 3 C lower
        w = csv.DictWriter(f, fieldnames=o.HISTORY_COLUMNS)
        w.writerow({"time_local": (t + timedelta(minutes=5)).isoformat(), "unit": "Unit A",
                    "action": "keep", "new_target": 16.0, "old_target": 16.0, "outdoor_mean": 0.0})
    sp, _ = o.load_actual_setpoints(tmp_path, CFG)
    assert sp[("Unit A", o.utc_quarter(t))] == 16.0 and len(sp) == 192


def test_legacy_history_file_counts_and_old_columns_file_does_not(tmp_path):
    write_history(tmp_path / "history.csv", baseline_at)
    (tmp_path / "history-old-columns.csv").write_text("time,unit,old\n2026-11-10T00:00:00+02:00,X,1\n")
    files = [p.name for p in o._history_files(tmp_path, CFG)]
    assert files == ["history.csv"]


def test_month_rollup_and_day_table(tmp_path):
    write_history(tmp_path / "history-2026-11.csv", lambda t: baseline_at(t) - 1.0)
    write_prices(tmp_path / CFG["price_history_file"], lambda t: 10.0)
    text = o.savings_report(tmp_path, CFG)
    assert "ROUGH" in text and "2026-11" in text and "2026-11-10" in text
    assert o.savings_report(tmp_path, CFG, "2026-11").count("2026-11-10") == 1
    assert "No data for 2026-12" in o.savings_report(tmp_path, CFG, "2026-12")


def test_empty_report_explains_what_is_missing(tmp_path):
    assert "No days to compare yet" in o.savings_report(tmp_path, CFG)
