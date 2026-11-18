from datetime import date, datetime, timedelta
from pathlib import Path

import ilp_optimizer as o

ROOT = Path(__file__).resolve().parent.parent
CFG = o.load_config(ROOT / "config.toml", ROOT / "tests" / "house.toml")
TZ = o.ZoneInfo(CFG["timezone"])
TUE = date(2026, 11, 10)


def prices(quarters, default=4.0):
    """One local day of quarter hours at `default`, with quarter index -> price overrides."""
    start, _ = o.local_day_bounds(TUE, TZ)
    return [o.PriceInterval(start + timedelta(minutes=15 * k), start + timedelta(minutes=15 * (k + 1)),
                            quarters.get(k, default)) for k in range(96)]


# The price rules on a weekday without the 08:00-15:00 nobody-home window, which would hold those
# hours at its floor (tested in test_nobody_home.py).
NO_WINDOW = {**CFG, "nobody_home": {**CFG["nobody_home"], "weekday": None}}


def setpoint(hh, mm, p, cfg=NO_WINDOW):
    return o.desired_setpoint(datetime(2026, 11, 10, hh, mm, tzinfo=TZ), p, cfg)


def test_a_short_spike_is_cut_by_half_a_degree_even_in_comfort_hours_and_under_10_cents():
    p = prices({19 * 4 + 1: 9.5})                      # 19:15, comfort window, 9.5c in a 4c evening
    base, offset, final, reason = setpoint(19, 15, p)
    assert offset == -0.5 and final == base - 0.5 and reason.startswith("spike")
    assert o.reason_kind(reason) == "spike"


def test_the_quarters_around_a_spike_are_untouched():
    p = prices({19 * 4 + 1: 9.5})
    assert "spike" not in setpoint(19, 0, p)[3] and "spike" not in setpoint(19, 30, p)[3]


def test_a_gentle_rise_is_not_a_spike():
    p = prices({k: 4.0 + 0.25 * min(k - 76, 8) for k in range(76, 96)})   # 4c up to 6c, then stays
    assert all("spike" not in setpoint(h, m, p)[3] for h in range(24) for m in (0, 15, 30, 45))


def test_the_spike_cut_does_not_override_a_bigger_cut():
    p = prices({19 * 4 + 1: 30.0})                     # an extreme price: the normal rules cut harder
    assert setpoint(19, 15, p)[1] <= -1.0


def test_a_small_but_relative_spike_on_a_cheap_evening_is_cut():
    # today's case: 9.3c among 6.3-8.2c quarters is 2.2c and 30% over its neighbours
    p = prices({19 * 4 + k: v for k, v in enumerate([6.0, 5.3, 4.6, 6.3, 7.9, 9.3, 7.3, 6.9, 8.2])}, default=7.0)
    assert setpoint(20, 15, p)[3].startswith("spike")


def test_a_jump_under_one_and_a_half_cents_is_not_a_spike():
    p = prices({19 * 4 + 1: 5.4}, default=4.0)          # 35 % but only 1.4c
    assert "spike" not in setpoint(19, 15, p)[3]


# ---- comfort pre-heat: the cheapest quarters before the schedule steps up ---------------------

def evening(before, after=9.0):
    """15:30-17:30 at the eight `before` prices, `after` from 17:30, 3.5c otherwise."""
    q = {15 * 4 + 2 + k: v for k, v in enumerate(before)}
    q.update({k: after for k in range(17 * 4 + 2, 96)})
    return prices(q, default=3.5)


def test_the_hour_before_the_step_gets_the_comfort_temperature():
    p = evening([3.8, 4.8, 3.8, 5.0, 6.0, 5.3, 4.6, 6.3])
    got = {f"{h}:{m:02d}": setpoint(h, m, p) for h, m in [(15, 30), (15, 45), (16, 0), (16, 15), (16, 30),
                                                         (16, 45), (17, 0), (17, 15)]}
    heated = sorted(k for k, v in got.items() if v[2] == 22.0)
    assert heated == ["16:30", "16:45", "17:00", "17:15"]           # contiguous: banked heat is not lost in a gap
    assert got["16:30"][3].startswith("pre-heat before comfort 17:30")
    assert o.reason_kind(got["16:30"][3]) == "comfort_preheat"
    assert got["16:00"][2] < 22.0


def test_no_comfort_preheat_when_comfort_time_is_barely_dearer():
    p = evening([4.0] * 8, after=4.7)                  # 17.5 % dearer: under min_ratio
    assert all("pre-heat before comfort" not in setpoint(h, m, p)[3] for h in (15, 16, 17) for m in (0, 15, 30, 45))


def test_no_comfort_preheat_before_the_lead_time_or_without_a_step_up():
    p = evening([3.8] * 8)
    assert "comfort" not in setpoint(15, 15, p)[3]     # more than two hours before 17:30
    assert "comfort" not in setpoint(20, 0, p)[3]      # 22:00 steps down, not up


def test_the_room_guard_still_stops_a_comfort_preheat():
    p = evening([3.8] * 8)
    base, offset, final, reason = o.desired_setpoint(datetime(2026, 11, 10, 16, 30, tzinfo=TZ), p, CFG,
                                                     room_temp=23.5)
    assert final == base and "comfort guard" in reason


# ---- dear hours: the dearest quarter is cut, in comfort hours the cheapest is kept ------------

def hour(h, quarters, default=3.0):
    return prices({h * 4 + k: v for k, v in enumerate(quarters)}, default=default)


def test_the_dearest_quarter_of_a_dear_hour_is_cut_in_comfort_hours():
    p = prices({k: 7.0 for k in range(72, 92)} | {80: 7.0, 81: 7.5, 82: 8.2, 83: 7.2}, default=3.0)  # dear evening
    got = {m: setpoint(20, m, p) for m in (0, 15, 30, 45)}
    assert got[30][3].startswith("dear quarter 8.2c") and got[30][2] == got[30][0] - 0.5
    assert o.reason_kind(got[30][3]) == "dear_quarter"
    assert all(got[m][2] == got[m][0] for m in (0, 15, 45))


def test_in_comfort_hours_the_cheapest_quarter_of_a_cut_hour_keeps_the_full_target():
    p = hour(19, [15.0, 16.0, 17.0, 18.0])              # expensive: every quarter cut, capped at 0.5
    got = {m: setpoint(19, m, p) for m in (0, 15, 30, 45)}
    assert got[0][2] == got[0][0] and "cheapest quarter kept" in got[0][3]
    assert got[45][2] == got[45][0] - 0.5


def test_outside_comfort_hours_the_cheapest_quarter_keeps_its_cut():
    p = hour(15, [15.0, 16.0, 17.0, 18.0])
    base, offset, final, reason = o.desired_setpoint(datetime(2026, 11, 10, 15, tzinfo=TZ), p, CFG, unit="Unit A")
    assert offset == -1.0 and "kept" not in reason       # Unit A: the afternoon keep window is Unit B's only


def test_a_dear_quarter_replaces_a_cheap_boost():
    # day average about 3.3c; 20:00-21:00 (comfort time) averages 3.6c: 2.8c is boosted, 4.4c is its dearest
    p = prices({k: 10.0 for k in range(4)} | {80: 2.8, 81: 3.6, 82: 3.6, 83: 4.4}, default=3.0)
    assert setpoint(20, 0, p)[1] > 0                   # cheap: boosted
    base, offset, final, reason = setpoint(20, 45, p)
    assert offset == -0.5 and reason.startswith("dear quarter")


def test_quarter_rules_work_on_a_day_too_even_for_the_normal_shifts():
    p = hour(20, [3.0, 3.0, 3.0, 4.5], default=2.0)     # day spread 2.5c: no normal boosts or cuts
    assert setpoint(20, 0, p)[1] == 0.0
    assert setpoint(20, 45, p)[1] == -0.5


def test_an_even_hour_or_a_cheap_hour_is_not_shaped():
    flat = hour(20, [7.0, 7.3, 7.6, 7.2])               # 0.6c spread: nothing to gain
    assert all("dear quarter" not in setpoint(20, m, flat)[3] for m in (0, 15, 30, 45))
    under = hour(20, [1.0, 1.5, 2.5, 1.0])              # under the 3c day average
    assert all("dear quarter" not in setpoint(20, m, under)[3] for m in (0, 15, 30, 45))


def test_history_rows_carry_the_price_and_the_kind_and_old_files_get_the_columns(tmp_path):
    import csv
    path = tmp_path / "history.csv"
    path.write_text("time_local,unit,action,room_temp,old_target,new_target,base,offset,outdoor_mean,reason\n"
                    "2026-11-10T19:00:00+02:00,A,keep,21.0,22.0,22.0,22.0,0.0,3.0,mid-priced hour\n",
                    encoding="utf-8")
    o.record_history(path, datetime(2026, 11, 10, 19, 15, tzinfo=TZ), CFG,
                     {"unit": "A", "action": "set", "reason": "spike 9.3c over its neighbours 7.1c",
                      "price": 9.3, "kind": "spike"})
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    assert rows[0]["price"] == "" and rows[1]["price"] == "9.3" and rows[1]["kind"] == "spike"


def test_a_comfort_preheat_only_claims_quarters_it_actually_raises():
    # 14:30 steps 19 -> 20; a cheap boost already lifts the quarters before it to 20
    p = prices({k: 10.0 for k in range(4)} | {k: 4.0 for k in range(58, 66)}, default=3.0)   # spread: boosts on
    cheap_cfg = {**NO_WINDOW, "prices": {**CFG["prices"], "cheap_all_boost": 1.0}}
    base, offset, final, reason = o.desired_setpoint(datetime(2026, 11, 10, 13, tzinfo=TZ), p, cheap_cfg)
    assert final == 20.0 and "comfort" not in reason


def test_the_cheap_boost_needs_a_price_below_the_day_average():
    # every quarter is under cheap_max, but 15:00-16:00 is dearer than the day's average
    p = prices({k: 10.0 for k in range(4)} | {k: 6.0 for k in range(60, 64)}, default=3.0)
    assert setpoint(15, 0, p)[1] <= 0 and "cheap" not in setpoint(15, 0, p)[3]
    assert setpoint(12, 0, p)[1] > 0


def test_no_comfort_preheat_before_a_step_that_is_not_to_the_comfort_temperature():
    # 14:30 steps 19 -> 20 C, the day's warmest is 22 C: no pre-heat for it
    p = prices({k: 10.0 for k in range(4)} | {k: 6.0 for k in range(58, 66)}, default=3.0)
    assert all("comfort" not in setpoint(h, m, p)[3] for h, m in [(12, 30), (13, 0), (13, 30), (14, 0)])


# ---- night and empty house: dearer than the day's average runs lower ---------------------------

def test_outside_comfort_time_a_dearer_than_average_quarter_runs_a_degree_lower():
    p = prices({k: 6.0 for k in range(40, 56)}, default=3.0)       # 10:00-14:00 at 6c, average 3.5c
    base, offset, final, reason = setpoint(11, 0, p)
    assert (base, final) == (19.0, 18.0) and o.reason_kind(reason) == "above_average"
    assert setpoint(9, 0, p)[2] >= 19.0                              # at the average: untouched


def test_comfort_time_and_the_warm_up_to_it_are_not_cut_for_being_above_average():
    p = prices({k: 6.0 for k in range(70, 88)}, default=3.0)       # 17:30-22:00 at 6c
    assert setpoint(17, 45, p)[2] == 22.0 and setpoint(19, 0, p)[2] == 22.0   # 17:45: the warm-up, before the 18:00 window


def test_16_is_the_one_heating_floor():
    extreme = prices({k: 45.0 for k in range(40, 56)}, default=3.0)
    assert setpoint(11, 0, extreme)[2] == 16.0
    assert CFG["limits"]["min_temp"] == CFG["limits"]["extreme_min_temp"] == 16.0


# ---- the warm-up before a comfort window: to the plan at its start, in the cheaper quarters ------

def test_a_dear_night_warms_up_in_steps_to_the_capped_window_temperature():
    p = prices({k: 35.0 for k in range(0, 28)}, default=3.0)       # dear night up to 07:00
    got = [setpoint(5, m, p) for m in (0, 15, 30, 45)]
    assert [g[2] for g in got] == [19.0, 19.5, 20.5, 21.0]          # from the night's 18 to 06:00's 21 (22 - cap 1)
    assert "warm-up for comfort 06:00" in got[1][3]
    assert setpoint(6, 0, p)[2] == 21.0


def test_the_evening_warm_up_starts_from_the_afternoon_plan():
    p = prices({k: 15.0 for k in range(64, 96)}, default=3.0)       # 16:00 onwards at 15c
    assert [setpoint(17, m, p)[2] for m in (0, 15, 30, 45)] == [19.5, 20.0, 21.0, 21.5]
    assert "warm-up for comfort 18:00: 1/3 to 21.5, heating" in setpoint(17, 15, p)[3]


def test_the_warm_up_heats_in_its_cheapest_quarters_and_holds_in_between():
    p = prices({k: 30.0 for k in range(64, 76)} | {68: 20.0, 70: 20.0}, default=3.0)   # 17:00 and 17:30 cheaper
    got = {m: setpoint(17, m, p) for m in (0, 15, 30, 45)}
    # 3 quarters of heating (the afternoon's 19 to 18:00's 21): 17:00, 17:30 and the later of the dear ones
    assert "heating" in got[0][3] and "holding" in got[15][3] and "heating" in got[45][3]
    assert got[15][2] == got[0][2] and got[45][2] == setpoint(18, 0, p)[2]


def test_a_peak_before_the_window_keeps_its_cut():
    p = prices({k: 60.0 for k in range(0, 28)}, default=3.0)
    assert all(setpoint(5, m, p)[2] == 19.0 for m in (0, 15, 30, 45))   # 22 - 3: no warm-up in a peak


def test_the_warm_up_does_not_touch_the_night_or_the_window():
    p = prices({k: 35.0 for k in range(0, 28)}, default=3.0)
    night = o.desired_setpoint(datetime(2026, 11, 10, 3, 0, tzinfo=TZ), p, CFG, unit="Unit B")
    assert night[2] == 18.0                                          # upstairs night: extreme cut, night floor
    assert "warm-up" not in setpoint(6, 0, p)[3]                     # the window has its own cap


# ---- above-average cut: real money only, and not where someone is usually home ----------------

SAT = date(2026, 11, 14)


def test_no_above_average_cut_for_a_fraction_of_a_cent():
    p = prices({k: 3.2 for k in range(40, 56)}, default=2.5)        # 10-14 at 3.2c: 28 % but 0.6c over
    assert "dearer than" not in setpoint(11, 0, p)[3]
    p = prices({k: 4.0 for k in range(40, 56)}, default=2.5)        # 1.3c over: cut
    assert "dearer than" in setpoint(11, 0, p)[3]


def test_the_afternoon_keep_window_covers_only_its_unit():
    p = prices({k: 8.0 for k in range(56, 72)}, default=3.0)        # 14:00-18:00 at 8c
    t = datetime(2026, 11, 10, 15, 30, tzinfo=TZ)
    up = o.desired_setpoint(t, p, CFG, unit="Unit B")
    down = o.desired_setpoint(t, p, CFG, unit="Unit A")
    page = o.desired_setpoint(t, p, CFG)
    assert up[2] == up[0] and "dearer than" not in up[3]
    assert down[2] == down[0] - 1.0 and "dearer than" in down[3]
    assert page[2] == page[0]                                        # the page shows the lived-in view


def test_weekend_daytime_and_evening_are_kept_but_the_upstairs_night_is_not():
    start, _ = o.local_day_bounds(SAT, TZ)
    p = [o.PriceInterval(start + timedelta(minutes=15 * k), start + timedelta(minutes=15 * (k + 1)),
                         8.0 if k in range(48, 64) or k in range(92, 96) or k in range(4, 8) else 3.0)
         for k in range(96)]
    day = o.desired_setpoint(datetime(2026, 11, 14, 13, tzinfo=TZ), p, CFG, unit="Unit A")
    evening = o.desired_setpoint(datetime(2026, 11, 14, 23, 15, tzinfo=TZ), p, CFG, unit="Unit B")
    night = o.desired_setpoint(datetime(2026, 11, 14, 1, 15, tzinfo=TZ), p, CFG, unit="Unit B")
    assert "dearer than" not in day[3]
    assert "dearer than" not in evening[3]                           # still up at midnight
    assert "dearer than" in night[3] and night[2] == 18.0


def test_a_flat_nearly_free_day_still_gets_the_cheap_boost():
    p = prices({k: 0.4 for k in range(48, 60)}, default=1.5)        # spread 1.1c, all nearly free
    base, offset, final, reason = setpoint(12, 0, p)
    assert offset > 0 and reason.startswith("cheap")


def test_the_afternoon_keep_window_caps_the_expensive_cut_like_a_comfort_window():
    p = prices({k: 18.0 for k in range(56, 72)}, default=3.0)       # 14:00-18:00 at 18c: the priciest hours
    t = datetime(2026, 11, 10, 16, 0, tzinfo=TZ)
    up = o.desired_setpoint(t, p, CFG, unit="Unit B")
    down = o.desired_setpoint(t, p, CFG, unit="Unit A")
    assert up[2] == up[0] - 0.5 and "comfort hours" in up[3]         # tier: at most 0.5 under 20c
    assert down[2] <= down[0] - 1.0


# ---- hard frost and weekend daytime --------------------------------------------------------------

def test_hard_frost_no_longer_lifts_a_deep_cut():
    p = prices({k: 35.0 for k in range(32, 56)}, default=5.0)       # 08-14 at 35c: extreme cut to 16
    t = datetime(2026, 11, 10, 10, tzinfo=TZ)
    assert o.desired_setpoint(t, p, CFG, 0.0, -5.0)[2] == 16.0
    assert o.desired_setpoint(t, p, CFG, 0.0, -15.0)[2] == 16.0      # no frost floor since 16.11.
    assert o.desired_setpoint(t, None, CFG, 0.0, -15.0, away=o.Away())[2] == 17.0   # away keeps its own level


def test_weekend_daytime_caps_the_extreme_cut_like_a_comfort_window():
    start, _ = o.local_day_bounds(SAT, TZ)
    p = [o.PriceInterval(start + timedelta(minutes=15 * k), start + timedelta(minutes=15 * (k + 1)),
                         35.0 if k in range(48, 64) else 5.0) for k in range(96)]   # Sat 12-16 at 35c
    base, offset, final, reason = o.desired_setpoint(datetime(2026, 11, 14, 13, tzinfo=TZ), p, CFG, unit="Unit A")
    assert final == base - 1.0 and "comfort hours" in reason          # tier: at most 1 C at 20-50c
