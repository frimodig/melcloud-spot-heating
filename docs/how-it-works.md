# How it works

Every 15 minutes the controller works out a target temperature for each unit for the current
quarter hour, and writes it when it has changed by at least 0.5 C. It plans on confirmed day-ahead
prices only, never forecasts, and keeps no state between runs beyond a little per-unit memory.

The numbers on this page are the defaults in [config.toml](../config.toml); "the example house"
marks values from [config.local.example.toml](../config.local.example.toml). Most rules can be
switched off or tuned ([configuration](configuration.md#your-settings-configlocaltoml)).

## How a target is made

1. **The base schedule:** the temperature for this time of day, weekday or weekend (`[schedule]`;
   by default a flat 21 C).
2. **The price shift:** warmer in cheap quarters, cooler in dear ones, much cooler in peaks, with
   pre-heats before the dearest hours.
3. **Who is around:** comfort windows limit the cuts while the house is in use; an empty house and
   the night may run lower.
4. **Warm-ups:** the rooms are brought up before people get up or come home, in the cheapest
   quarters available.
5. **Sun and weather:** sunshine lowers the target; the outdoor temperature scales the shift.
6. **The room and the limits:** each room's own temperature can veto a cut or a boost, and the
   target is kept within the limits (16-22 C, or 16-24 C for the extreme rules) and rounded to
   0.5 C.
7. **People:** "too cold", away, home and the other controls on the [phone page](using-it.md) apply
   last.

## Price rules

All prices are c/kWh. A day is ranked on its own, so the plan for a day stays the same as the day
goes on; only pre-heats before a spike or peak early the next day reach back across midnight. With
fewer than 20 hours of a day's prices the normal shifts stay off, and without prices the base
schedule runs.

**Cheap and dear hours** (`[prices]`). The cheapest 6 hours get +1 C, but only when power is nearly
free (3 c or less, `cheap_max`). So does every other quarter at or below 3 c that is cheaper than
the day's average (`cheap_all`). The 4 dearest hours get -1 C at 12 c or more, and -1.5 C at 30 c
or more. When the day's prices spread less than 4 c these cuts are skipped, but the nearly-free
boost still applies (`flat_day_boost`).

**Extreme prices and spikes** (`[extreme]`). The 6 dearest hours at 20 c or more are cut by 3 C;
other hours over 20 c get the normal cut. A spike, a quarter at three times the day's median and at
least 15 c, is pre-heated for by +2 C in the 2 hours before it, also when it falls in the first 2
hours of the next day (judged on that day's prices, which come out at about 13:00 CET).

**Peaks** (`[extreme] peak_price`, heating only). Any quarter at 50 c or more is cut by 3 C, however
the rest of the day looks and even with only part of the day's prices. The 3 hours before a peak
are pre-heated by +3 C in quarters up to 30 c. During a peak the room guard lets the rooms go down
to 16 C (`peak_room_min`) so it does not undo the cut.

**Single dear quarters.** A quarter at least 1.5 c and 20 % above the median of the 4 quarters on each
side is cut by 0.5 C (`[spikes]`). In every hour dearer than the day's average whose quarters differ
by at least 1 c and 15 %, the dearest quarter is cut by 0.5 C (`[hour_shaping]`). A 15-minute dip is
not felt, so both run at any price, in comfort windows too, and also on days whose prices are too
even for the normal shifts. Heating only. They replace a boost or a smaller cut, never a pre-heat or an extreme or peak cut.

**The outdoor temperature** (`[weather]`) scales the normal shifts: x1.5 at -10 C or colder, x0.5
at +10 C or warmer, linear between, from Open-Meteo's hourly forecast for the quarter. A cold night
and a mild afternoon get different strengths. Without a forecast the day's mean is used, and without
that the factor is 1. The extreme, peak and pre-heat rules ignore the weather. Weather needs the
location ([configuration](configuration.md#credentials-and-location-env)).
`--plan --outdoor-temp -15` previews a temperature offline (with the location set, as `./run.sh`
does from `.env`).

**The price of heat** (`[cop]`, off by default). The rules above can judge the price of a kWh of heat
instead of a kWh of electricity: the spot price times the COP at 0 C over the COP at the forecast
outdoor temperature, a factor between 0.86 and 1.08 with the default points. It moves which quarters
count as cheap or dear, and nothing else: the comfort windows' tiers, the nobody-home floors and the
limits stay on the spot price and the room guard on the room. See
[configuration](configuration.md#the-price-of-heat-cop). Cooling stays on the spot price.

## While people are home

**Comfort windows** (`[comfort_windows]`; by default 06-08 and 18-22 on weekdays, 07-12 and 17-22
at weekends, and the weekend ones on home and holiday days). The price limits how much a cut may
take: nothing under 10 c, at most 0.5 C under 20 c, at most 1 C under 50 c, and in full at 50 c or
more. In a dear hour the cheapest quarter keeps the full target.

**Keep windows** (`[above_average] keep_weekday`, `keep_weekend`; none by default) mark other times
someone is usually home, optionally for some `units` only. With `comfort = true` they cap cuts like
a comfort window. The example house keeps weekdays 16:00-17:00 for its living room unit, the
evenings from 22:30, and weekend daytimes.

**Comfort pre-heat** (`[comfort_preheat]`). When the schedule steps up to its warmest temperature
(in the example house at 05:30 and 17:00, to 21.5 C), the hour before the step already gets the
coming temperature, when the hour after the step costs at least 20 % more than the hour before it. Heat banked earlier
than an hour is lost: the units warm the air in 15-30 minutes, and it cools as fast.

**Warm-ups** (`[comfort_warmup]`, `[warmup]`). Before each comfort window opens, the end of the
nobody-home window, and a return from away, the rooms warm up to the plan's target at that time
(so a dear arrival still gets its price cap). The warm-up takes as many quarters as the units need
at 3 C an hour (`warmup_rate`), starting from the plan before it; after an absence it counts how far
the empty house has cooled, at 0.3 C an hour (`cooling_rate`). Those quarters are the cheapest of the hour before a comfort window, or of the
two hours before a return or the end of the nobody-home window; each lifts the target a step, and the quarters between hold the level
reached. The saving comes from buying the heat cheaply, not from arriving to a colder house. A peak
keeps its cut.

## While nobody is home

**Away and the nobody-home window** (`[nobody_home]`; the weekday window is off by default, the
example house has 08:30-16:00). The house is not heated unless that saves money later: every unit
runs at 17 C, or 16 C in a quarter over 10 c, never above the plan, and the room guard does not lift
it. It goes higher only in a quarter under 5 c that pre-heats before a spike or peak, and in the
warm-up before people come back. Home and holiday days have no window.

**Dear quarters outside comfort time** (`[above_average]`). Outside the comfort windows, the keep
windows and the hours at the schedule's warmest temperature, a quarter at least 10 % and 1 c above
the day's average runs 1 C lower. Pre-heats are not touched.

## The night

**The night shift** (`[night_shift]`, off by default). In the night window (00:00-05:00 on
weekdays, 00:00-06:00 at weekends) the listed `units`, typically the one by the bedrooms, get no
flat cut and follow the night's prices instead:
- a quarter clearly cheaper than the night's median (by 0.5 c and 5 %) runs 0.5 C warmer;
- every other quarter drops to the floor its price allows: the night's base under 20 c, 0.5 C under
  it over 20 c, 1 C under it over 30 c, and no lower for a single dear quarter;
- an hour with a cut and no boosted quarter boosts its cheapest one, so an evenly dear night is not
  spent at the floor;
- a peak still cuts in full; pre-heats and a bigger nearly-free boost stay.

The other units keep the normal rules at night, but go no lower than 18 C (`others_floor`) except in
a peak. The page's chart shows the night shift units' night.

**Evening cooldown and sleeping warmth.** From 22:00 the night shift units run 0.5 C under the
schedule and from 23:00 1 C under (`cooldown`), while the schedule is at least 1.5 C above the
night's temperature; in the example house that is 21 C at 22:00 and 19.5 C at 23:00, then 19 C from
midnight. A cheap boost gives way to it. From 22:00 to the end of the night they never run above
21 C (`max_temp`), except in the comfort pre-heat before the morning.

## The room and the limits

**The room guard** (`[comfort]`). Each unit's own room temperature has the last word: price cuts
stop while the room is below 17 C, boosts and pre-heats while it is at 23 C or more. The reason in
the history says when the guard acted.

**One floor.** 16 C is the lowest target any rule writes. There is no separate frost floor: the
defaults assume other heating (radiators, another heat pump) keeps the house from getting cold in
hard frost. If the heat pumps are your only heating, raise `[limits] min_temp` and
`extreme_min_temp`, `[nobody_home] temp` and `dear_temp`, and `[extreme] peak_room_min`.

**A person's change wins for an hour.** When a unit's target is not what the controller left
(someone used the remote or the MELCloud app), that unit is left alone for 60 minutes
(`manual_hold_minutes`; another change extends it), and then the plan resumes.

**What it touches.** The target temperature, when it changes by 0.5 C or more; each write also sets
the fan back to auto. By default every run also keeps each unit on and in the season's mode, so a
unit someone switches off comes back on at the next run; [the season](using-it.md#season-heating-or-cooling)
says how to change that. "Normal heat now" and away while cooling also change power and mode.

## Sun

Sunshine heats the rooms more directly than the air does, so the forecast solar radiation (from
Open-Meteo, clouds included) lowers the target. When heating, the sun does part of the work: up to
1 C off (`heat_max_cut`). When cooling, the target drops up to 1 C an hour ahead of the heat
(`cool_max_boost`, `cool_lookahead_hours`). The effect grows from 100 to 500 W/m2, so the weak winter
sun does nothing, and it is scaled per unit by `[units.solar_weight]` (the example house: Living room
1.0, Hall 0.5; tune them from the history on sunny days). It is a basic model: no sun angle,
window orientation or shading. `--plan --sun 500` previews a sunny day (with the location set).

## Cooling

A unit in cool mode gets the cooling rules (`[cool]`), mirrored from heating:
- one flat setpoint, 23 C, within 21-26 C;
- the cheapest hours cool 1 C further (at 4 c or less), the dearest let the room drift 1 C warmer
  (at 7 c or more), and 2 C at 12 c or more (up to 27 C); a spike is pre-cooled for by 1.5 C in
  the 2 hours before it;
- hot days count for more: x0.5 at 15 C outdoors or cooler, x1.5 at 25 C or warmer;
- the room guard stops the drift above 25.5 C and the pre-cool below 21.5 C;
- per-unit `[units.cool_offsets]`, such as an upstairs unit 0.5 C cooler since cold air sinks.

The summer thresholds are first guesses: summer prices are far lower than winter ones. `--plan
--mode cool` previews it; without `--mode` the plan follows the season.

## Not controlled

Only the MELCloud air-to-air units are controlled. Other heating, hot water and ventilation are
never touched.
