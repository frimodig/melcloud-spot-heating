"""The evening and night (22-24 kept, after midnight downstairs follows the price) and away's end."""
import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path

import ilp_optimizer as o
import web

ROOT = Path(__file__).resolve().parent.parent
CFG = o.load_config(ROOT / "config.toml", ROOT / "tests" / "house.toml")
TZ = o.ZoneInfo(CFG["timezone"])
TUE = date(2026, 11, 10)


def at(hh, mm=0, day=TUE):
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ)


def prices(quarters, default=10.0, day=TUE):
    """One local day of quarter hours at `default`, with quarter index -> price overrides."""
    start, _ = o.local_day_bounds(day, TZ)
    return [o.PriceInterval(start + timedelta(minutes=15 * k), start + timedelta(minutes=15 * (k + 1)),
                            quarters.get(k, default)) for k in range(96)]


def sp(t, p, **kw):
    return o.desired_setpoint(t, p, CFG, **kw)


# ---- 22-24: still up at midnight -----------------------------------------------------------------

def test_the_late_evening_is_capped_like_comfort_hours_for_both_units():
    p = prices({k: 24.0 for k in range(88, 96)}, default=8.0)       # 22-24 at 24c: extreme cut before
    for unit in ("Unit A", "Unit B", None):
        base, offset, final, reason = sp(at(22, 30), p, unit=unit)
        assert base == 21.0 and offset == -1.0, (unit, reason)         # tier: at most 1 C under 50c
        assert "comfort hours" in reason


def test_a_peak_in_the_late_evening_is_still_cut_in_full():
    p = prices({k: 60.0 for k in range(88, 92)}, default=8.0)
    assert sp(at(22, 0), p, unit="Unit A")[1] == -3.0


# ---- after midnight: upstairs may sit at 18, downstairs follows the price -------------------------

NIGHT = {k: 6.0 for k in range(0, 8)} | {k: 14.0 for k in range(8, 16)}   # 00-02 cheap, 02-04 dear, day 10c


def test_downstairs_night_runs_half_a_degree_cooler_in_dear_quarters_instead_of_the_flat_cut():
    p = prices({k: 6.0 for k in range(0, 8)} | {k: 22.0 for k in range(8, 20)})   # 02-05 at 22c
    base, offset, final, reason = sp(at(2, 30), p, unit="Unit A")
    assert reason.startswith("night shift") and offset == -0.5
    assert final == 18.5                                            # the 20c floor, not the flat extreme cut
    assert o.reason_kind(reason) == "night_shift"


def test_downstairs_night_runs_warmer_in_the_cheap_quarters_and_holds_in_between():
    p = prices(NIGHT | {k: 10.0 for k in range(16, 20)})            # 04-05 at the night's average
    assert sp(at(0, 30), p, unit="Unit A")[1] == 0.5
    assert sp(at(4, 15), p, unit="Unit A")[1] == 0.0


def test_upstairs_keeps_the_normal_night_and_may_sit_at_18():
    p = prices(NIGHT)
    base, offset, final, reason = sp(at(2, 30), p, unit="Unit B")
    assert final == 18.0 and not reason.startswith("night shift")


def test_the_page_plan_shows_the_downstairs_night():
    assert sp(at(2, 30), prices(NIGHT))[3].startswith("night shift")


def test_a_peak_still_cuts_downstairs_at_night():
    p = prices({k: 60.0 for k in range(8, 12)}, default=8.0)
    base, offset, final, reason = sp(at(2, 15), p, unit="Unit A")
    assert offset == -3.0 and reason.startswith("peak")


def test_a_dear_night_quarter_drops_to_the_floor_its_price_allows():
    # 00-02 at 4c, 02-05 at the dear price: the dear quarters are clearly over the night's average
    for dear, want in [(8.0, 19.0), (12.0, 19.0), (20.5, 18.5), (30.5, 18.0), (45.0, 18.0)]:
        p = prices({k: 4.0 for k in range(0, 8)} | {k: dear for k in range(8, 20)}, default=8.0)
        assert sp(at(3, 15), p, unit="Unit A")[2] == want, dear   # 03:00: the hour's boost


def test_a_cheaper_night_quarter_is_boosted_whatever_its_price():
    p = prices({k: 22.0 for k in range(0, 8)} | {k: 32.0 for k in range(8, 20)}, default=8.0)
    assert sp(at(0, 30), p, unit="Unit A")[2] == 19.5                     # 22c, cheap for this night
    assert sp(at(3, 15), p, unit="Unit A")[2] == 18.0                     # 32c: the 30c floor


def test_an_evenly_dear_night_drops_to_the_floor_with_one_boosted_quarter_each_hour():
    p = prices({k: 35.0 for k in range(0, 20)}, default=8.0)
    finals = [sp(at(h, m), p, unit="Unit A")[2] for h in range(5) for m in (0, 15, 30, 45)]
    assert finals == [19.5, 18.0, 18.0, 18.0] * 5


def test_one_peak_quarter_does_not_make_the_rest_of_the_night_look_cheap():
    p = prices({k: 12.0 for k in range(0, 20)} | {8: 55.0}, default=8.0)  # 12c night, 02:00 a 55c peak
    assert sp(at(3, 15), p, unit="Unit A")[2] == 19.0                     # 12c: holds, not +0.5
    assert sp(at(2, 0), p, unit="Unit A")[1] == -3.0                      # the peak still cuts in full


def test_upstairs_never_goes_under_its_night_floor_except_in_a_peak():
    p = prices({k: 25.0 for k in range(0, 20)} | {18: 60.0}, default=8.0)  # 25c night: extreme cut to 16
    assert sp(at(0, 30), p, unit="Unit B")[2] == 18.0                     # (the peak pre-heat starts 01:30)
    assert "night floor 18" in sp(at(0, 30), p, unit="Unit B")[3]
    assert sp(at(4, 30), p, unit="Unit B")[2] == 16.0                     # 04:30 peak: full cut
    assert sp(at(6), p, unit="Unit B")[2] >= 16.0                         # after the night: no floor


def test_the_night_shift_ends_with_the_night():
    p = prices(NIGHT)
    assert not sp(at(5, 30), p, unit="Unit A")[3].startswith("night shift")
    weekend = prices(NIGHT, day=date(2026, 11, 14))
    assert sp(at(5, 30, date(2026, 11, 14)), weekend, unit="Unit A")[3].startswith("night shift")


def test_an_unpriced_night_is_left_to_the_other_rules():
    p = [i for i in prices(NIGHT) if i.start >= o.local_day_bounds(TUE, TZ)[0] + timedelta(hours=3)]
    assert o.night_shift(at(4, 0), p, CFG, unit="Unit A") is None


def test_the_hours_boost_wins_over_a_spike_on_the_same_quarter():
    p = prices({k: 6.0 for k in range(0, 4)} | {k: 22.0 for k in range(4, 20)})   # 00-01 6c, then 22c
    finals = [sp(at(1, m), p, unit="Unit A")[2] for m in (0, 15, 30, 45)]
    assert finals == [19.5, 18.5, 18.5, 18.5]                        # 01:00 is a spike and the hour's boost


def test_a_peak_during_the_return_pre_heat_is_not_lifted_by_the_upstairs_night_floor():
    p = prices({k: 60.0 for k in range(4, 8)} | {k: 8.0 for k in range(0, 4)}, default=8.0)  # 01-02 peak
    got = sp(at(1, 15), p, unit="Unit B", away=o.Away(at(2)))
    assert got[2] == 16.0 and got[3].startswith("pre-heat before return, peak")


def test_the_repeated_hour_of_the_autumn_clock_change_gets_its_own_boost():
    day = date(2026, 10, 25)                                         # 03:00-04:00 happens twice
    start, end = o.local_day_bounds(day, TZ)
    n = int((end - start).total_seconds() // 900)
    p = [o.PriceInterval(start + timedelta(minutes=15 * k), start + timedelta(minutes=15 * (k + 1)),
                         35.0 if k < 6 * 4 + 4 else 8.0) for k in range(n)]
    second = [i.start for i in p if i.start.astimezone(TZ).hour == 3][4]   # 03:00 the second time
    assert o.desired_setpoint(second, p, CFG, unit="Unit A")[1] == 0.5


def test_a_cheap_night_quarter_reads_as_plus_zero_not_minus_zero():
    p = prices({k: 6.0 for k in range(0, 20)})
    assert "night shift +0.0" in sp(at(2, 15), p, unit="Unit A")[3]



# ---- away: over at the return time, and never warmer than the plan -------------------------------

def test_away_is_over_after_the_return_time():
    back = at(14)
    p = prices({})
    after = sp(at(16), p, away=o.Away(back))
    assert after == sp(at(16), p)
    assert o.reason_kind(after[3]) != "return_preheat"


def test_away_holds_the_flat_temperature_where_the_plan_is_warmer():
    assert sp(at(19), prices({}), away=o.Away())[2] == 17.0


def test_away_never_heats_more_than_the_plan():
    p = prices({k: 60.0 for k in range(40, 44)}, default=8.0)       # 10:00 peak: plan cuts to 16
    plan = sp(at(10), p, unit="Unit B")
    away = sp(at(10), p, unit="Unit B", away=o.Away())
    assert plan[2] == 16.0 and away[2] == 16.0
    assert away[3].startswith("away") and "peak price" in away[3]
    assert o.reason_kind(away[3]) == "away"


def test_without_a_start_time_the_return_pre_heat_takes_the_full_two_hours():
    back = at(18)
    p = prices({})
    assert sp(at(15, 45), p, away=o.Away(back))[2] == 17.0
    assert sp(at(16), p, away=o.Away(back))[3].startswith("pre-heat before return")


def test_a_live_run_removes_the_away_file_once_the_return_time_has_passed(monkeypatch, tmp_path):
    o.AWAY_FILE.write_text("2026-11-10T08:00\n")
    seen = {}

    async def fake_prices(cfg, path=None):
        return None

    async def fake_devices(session):
        return []

    async def fake_apply(devices, now, p, cfg, dry_run, outdoor, history, away, *a, **k):
        seen["away"] = away
        return 0
    monkeypatch.setattr(o, "get_prices_or_none", fake_prices)
    monkeypatch.setattr(o, "melcloud_devices", fake_devices)
    monkeypatch.setattr(o, "apply_to_units", fake_apply)
    args = o.argparse.Namespace(now="2026-11-10T09:00", dry_run=False, away=False, away_until=None,
                                holiday=False, assume_home=False, prices_file=None, outdoor_temp=1.0, sun=0.0)
    cfg = {**CFG, "resilience": {**CFG["resilience"], "state_file": str(tmp_path / "state.json")},
           "unit_state_file": str(tmp_path / "units.json")}
    try:
        assert asyncio.run(o.run_live(args, cfg, TZ)) == 0
    finally:
        for h in list(o.log.handlers):
            o.log.removeHandler(h)
            h.close()
    assert seen["away"] is None and not o.AWAY_FILE.exists()


def test_the_page_shows_away_off_after_the_return_time(tmp_path):
    now = datetime.now(TZ)
    o.AWAY_FILE.write_text((now - timedelta(minutes=5)).isoformat(timespec="minutes") + "\n")
    assert web.build_status(CFG, now, tmp_path)["away"]["on"] is False
    o.AWAY_FILE.write_text((now + timedelta(hours=5)).isoformat(timespec="minutes") + "\n")
    assert web.build_status(CFG, now, tmp_path)["away"]["on"] is True


def test_a_one_quarter_dip_at_night_gives_way_to_a_deeper_price_floor():
    # with the live tiers a dip (0.5) and a deeper floor rarely meet, so the floor is set deep here
    cfg = {**CFG, "night_shift": {**CFG["night_shift"], "tiers": [[10.0, 1.0]]}}
    night = {k: 6.0 for k in range(0, 20)} | {9: 11.0}                  # 02:15 a spike over a 6c night
    p = prices(night, default=15.0)                                     # a dear day: no above-average cut
    assert o.desired_setpoint(at(2, 15), p, CFG, unit="Unit A")[2] == 19.0   # live floors: it holds
    base, offset, final, reason = o.desired_setpoint(at(2, 15), p, cfg, unit="Unit A")
    assert offset == -1.0 and reason.startswith("night shift")
    assert o.desired_setpoint(at(2, 0), p, cfg, unit="Unit A")[1] == 0.5   # the hour's boosted quarter


def test_too_cold_while_away_is_read_as_a_saving_quarter(tmp_path):
    hist = o.monthly_path(o.APP_DIR / CFG["history_file"], at(16), TZ)
    hist.write_text(",".join(o.HISTORY_COLUMNS) + "\n" +
                    "2026-11-10T16:00:02+02:00,Unit B,keep,20,17.0,17.0,17.0,0.0,5,away until Tue 10.11. 18:00,5,away\n")
    assert o.current_cut(CFG, at(16, 5)) > 0
    o.set_nudge("warmer", TZ, CFG, at(16, 5))
    n = o.read_nudge(o.NUDGE_FILE, TZ, at(16, 5))
    assert (n.floor, n.shift) == (1.0, 0.0)


def test_a_simulated_now_never_removes_a_real_away(monkeypatch, tmp_path):
    o.AWAY_FILE.write_text("2030-01-02T18:00\n")                      # still ahead by the real clock

    async def fake_prices(cfg, path=None):
        return None

    async def fake_devices(session):
        return []

    async def fake_apply(*a, **k):
        return 0
    monkeypatch.setattr(o, "get_prices_or_none", fake_prices)
    monkeypatch.setattr(o, "melcloud_devices", fake_devices)
    monkeypatch.setattr(o, "apply_to_units", fake_apply)
    args = o.argparse.Namespace(now="2030-01-03T09:00", dry_run=False, away=False, away_until=None,
                                holiday=False, assume_home=False, prices_file=None, outdoor_temp=1.0, sun=0.0,
                                lock_wait=0.0)
    cfg = {**CFG, "resilience": {**CFG["resilience"], "state_file": str(tmp_path / "state.json")},
           "unit_state_file": str(tmp_path / "units.json")}
    try:
        assert asyncio.run(o.run_live(args, cfg, TZ)) == 0
    finally:
        for h in list(o.log.handlers):
            o.log.removeHandler(h)
            h.close()
    assert o.AWAY_FILE.exists()


def test_a_one_quarter_dip_never_goes_below_the_night_floor_downstairs():
    night = {k: 4.0 for k in range(0, 20)} | {9: 11.6}                  # 02:15 a spike in a 4c night
    p = prices(night, default=15.0)                                    # a dear day: no above-average cut
    base, offset, final, reason = sp(at(2, 15), p, unit="Unit A")
    assert final == 19.0 and reason.startswith("night shift")          # under 20c the night holds 19


def test_a_sparsely_priced_night_downstairs_still_gets_the_above_average_cut():
    start, _ = o.local_day_bounds(TUE, TZ)
    p = [i for i in prices({k: 11.0 for k in range(16, 20)}, default=6.0) if i.start >= start + timedelta(hours=3)]
    got = sp(at(4, 15), p, unit="Unit A")                              # 00-03 unpriced: no night shift
    assert got[3].startswith("dearer than") and got[1] == -1.0


# ---- the stepped cooldown before the night, and the sleeping warmth cap ----------------------------

def test_the_downstairs_cools_in_steps_at_22_and_23_before_the_night_temperature_at_midnight():
    p = prices({})
    got = {t: sp(at(*t), p, unit="Unit A") for t in [(21, 45), (22, 0), (22, 45), (23, 0), (23, 45), (0, 0)]}
    assert [got[t][2] for t in got] == [22.0, 20.5, 20.5, 20.0, 20.0, 19.0]
    assert got[(22, 0)][3].startswith("night cooldown -0.5") and o.reason_kind(got[(23, 0)][3]) == "night_cooldown"


def test_the_cooldown_beats_a_cheap_boost_downstairs_and_leaves_upstairs_alone():
    p = prices({k: -1.0 for k in range(88, 96)}, default=15.0)      # nearly free from 22:00
    assert sp(at(22, 30), p, unit="Unit A")[2] == 20.5            # the boost does not keep it warm
    assert sp(at(22, 30), p, unit="Unit B")[2] == 22.0            # upstairs still takes the cheap boost


def test_a_peak_cuts_deeper_than_the_cooldown_and_a_price_pre_heat_is_held_at_the_sleeping_warmth():
    p = prices({k: 60.0 for k in range(92, 96)}, default=8.0)       # a peak 23-24
    assert sp(at(23, 30), p, unit="Unit A")[2] == 18.0
    pre = sp(at(22, 0), p, unit="Unit A")                         # banks heat for the peak: upstairs 24
    assert pre[2] == 21.0 and "night max 21" in pre[3] and sp(at(22, 0), p, unit="Unit B")[2] == 24.0


def test_no_cooldown_when_the_schedule_is_already_near_the_night_temperature():
    cfg = {**CFG, "night_shift": {**CFG["night_shift"], "cooldown_min_gap": 3.0}}    # 21 vs 19: gap 2
    assert o.desired_setpoint(at(22, 30), prices({}), cfg, unit="Unit A")[2] == 21.0


def test_the_morning_pre_heat_may_go_over_the_sleeping_warmth():
    p = prices({k: 6.0 for k in range(16, 20)}, default=20.0)       # 04-05 cheap, the day dear
    assert sp(at(4, 30), p, unit="Unit A")[2] == 22.0
