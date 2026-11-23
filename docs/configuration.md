# Configuration

An install's own values live in three files, none of them in git:

| File | Holds | Read by |
|---|---|---|
| `.env` | the MELCloud login, the location, and optionally `ENTSOE_TOKEN` and `HEARTBEAT_URL` | the controller (`run.sh`) |
| `web.env` | `WEB_USERS` (who may use the phone page), and again the location and, with ENTSO-E, `ENTSOE_TOKEN` (the page plans its chart itself) | the phone page |
| `config.local.toml` | every setting you change from the defaults | both |

## Your settings: `config.local.toml`

[config.toml](../config.toml) holds the defaults and explains every setting. Don't edit it: put what
you change in `config.local.toml` next to it, and updates will merge cleanly. Your file overrides only
what it names: a table merges key by key, and a value or a list replaces the default whole. A
misspelt setting is logged at every run, and so is a unit name MELCloud doesn't have.

The defaults run on very little: a flat 21 C that the price rules shift around, weather and sun off
until the location is set, and the rules that depend on a household's routine off (the night shift,
the nobody-home window, keep windows). Two starting points:
- [config.local.minimal.toml](../config.local.minimal.toml) switches every optional rule off,
  leaving the price shifts, the peak protection and the limits; add back what fits.
- [config.local.example.toml](../config.local.example.toml) is a made-up house with most rules on: a
  cooler night and empty weekdays, the night shift on the unit by the bedrooms, keep windows, and a
  multi-split.

Most optional rules have `enabled` in their section, and `enabled = false` switches one off. The
exceptions: the nobody-home window is off while `[nobody_home] weekday = []` (the default), keep
windows while their lists are empty or `[above_average]` is off; the room guard (`[comfort]`) and the limits are always on; and
the extreme and peak rules have no switch, so raise `[extreme] cut_price` and `peak_price` to make
them rarer.
[How it works](how-it-works.md) says what each rule does.

## Credentials and location: `.env`

Credentials are read from environment variables; `run.sh` loads them from `.env`.
Keep the **single quotes** around both values: `run.sh` sources the file as shell,
so an unquoted password containing `?`, `$`, `;`, `&` or `]` is mangled and login fails.
Never commit `.env` (it is in `.gitignore`) and never log it. Put your real values
only in `.env`, never in `env.example`, which is tracked by git.

The weather location is `WEATHER_LATITUDE` and `WEATHER_LONGITUDE`, in `.env` and again in `web.env`
for the page's chart. Without them weather and sun stay off.

## Units

`./run.sh --list` prints your units' names. By default every unit MELCloud has is controlled; list
some in `[units] names` to control only those.

One unit, several self-contained units, a multi-split (several indoor units on one outdoor unit) and
a mix all work: list each multi-split's units in `outdoor_groups`, and every unit not listed is taken
to have its own outdoor unit. Units on one outdoor unit cannot heat and cool at once. By default every run
puts the units in the season's mode, so a group found in different modes is fixed at the next run.
If that switch fails, the group is left alone, a `mode-mismatch` is logged and the run counts as
failed, so the heartbeat stops until it is fixed. With `[control] season_enforce = "on_change"` a
group someone put in different modes is also left alone and logged, but the run doesn't count as
failed. Other units carry on. The season
(heat or cool) is one setting for the whole house.

Per unit, `[units.offsets]` and `[units.cool_offsets]` move a unit's target, and
`[units.solar_weight]` says how much its rooms feel the sun ([how it works](how-it-works.md#sun)).

## Prices

`[prices] sources` lists the feeds in priority order: the first that gives prices is used, and the
next ones are fallbacks, each tried after the one before has failed its retries or given no prices.
`["porssisahko", "entsoe"]`, for example, uses ENTSO-E only while porssisahko.net is down. The
feeds:
- `"porssisahko"` (the default): porssisahko.net's 15-minute prices for Finland, VAT included. No key.
- `"entsoe"`: the ENTSO-E Transparency Platform's day-ahead prices for any European bidding zone
  (`[prices.entsoe] zone`, the zone's EIC code as listed on the platform, such as `10YFI-1--------U`
  for Finland). Register at https://transparency.entsoe.eu and ask for API access (its help pages
  say how); the token goes in `.env` as `ENTSOE_TOKEN`, and in `web.env` too for the page's chart. EUR/MWh become c/kWh, plus `vat`
  percent (Finland's 25.5 by default, to match porssisahko.net) and an optional `margin` in c/kWh.

Both feeds cover only today and tomorrow. Another source goes into `fetch_from` in
`ilp_optimizer.py`.

### Price thresholds

Where a price counts as cheap or dear is a matter of taste and of your market, so every threshold is
a setting. The defaults are tuned to Finnish winter prices including VAT; all are in c/kWh.

| Setting | Default | Meaning |
|---|---|---|
| `[prices] cheap_max` | 3 | boost only at or below this (nearly free) |
| `[prices] expensive_min` | 12 | cut the dearest hours from this |
| `[prices] very_expensive` | 30 | deeper cut from this |
| `[prices] min_spread` | 4 | a day spread less than this gets no cuts |
| `[extreme] cut_price` | 20 | the extreme cut from this |
| `[extreme] spike_min_price` | 15 | a spike must cost at least this |
| `[extreme] peak_price` | 50 | the peak cut from this |
| `[extreme] peak_preheat_max_price` | 30 | pre-heat for a peak only in quarters up to this |
| `[comfort_windows] tiers` | `[[10, 0], [20, 0.5], [50, 1]]` | (price below which, largest cut in C) in comfort windows |
| `[nobody_home] dear_price`, `bank_price` | 10, 5 | 16 C instead of 17 C over this; banking heat for a spike only under this |
| `[night_shift] tiers` | `[[20, 0.5], [30, 1]]` | (price over which, cut in C) for the night shift units' floors |
| `[above_average] min_above` | 1 | a quarter must be this much over the day's average to be cut |
| `[spikes] min_jump`, `[hour_shaping] min_spread` | 1.5, 1 | how far a single quarter or an hour must stand out |

To change one, put it in `config.local.toml` under its section, for example `[prices]` and then
`expensive_min = 15.0`. A list, such as `tiers`, replaces the default whole.

Cooling has its own, lower thresholds (`[cool.prices]`, `[cool.extreme]`), since summer prices are
far lower. The temperatures the rules may reach are settings too: `[limits]` and `[cool.limits]`.

### The price of heat (`[cop]`)

Off by default. With `[cop] enabled = true` the cheap, dear, spike, above-average, pre-heat and night
rules judge what a kWh of *heat* costs, not of electricity: each quarter's spot price is multiplied by
the COP at `reference_c` (default 0 C) and divided by the COP at that quarter's forecast outdoor
temperature, so cold quarters count as dearer and mild ones as cheaper, and the thresholds above keep
their meaning at the reference temperature. `points` are `[outdoor C, COP]` pairs, interpolated and held
flat beyond the ends. The defaults are one pump's (the Mitsubishi Electric MXZ-2F53VFH4, below):
replace them with your pump's, or leave `[cop]` off. Without the weather forecast the spot price is used.

The minimum and comfort rules are not touched: the comfort windows' tiers, the nobody-home floors, the
limits and the room guard never look at the COP, so the COP changes when heat is bought, not how
warm the rooms may get. The page, the history file and the reports keep the spot price; the page adds
a "price of heat" line.

Sources: the points come from Mitsubishi Electric's technical manual OBH790P (the MXZ-2F53VF4/VFH4
heating capacity and total input correction curves, p. 123, and the rated COP 4.10 at 7 C outdoor, p. 122),
read off the charts at full load, so with no defrost and no part-load gain; the wet bulb of the curves is read
as dry bulb less 1 C. The idea of a COP that depends on the outdoor temperature is common to
open-source tools, which we read for background and copied nothing from:
[hplib](https://github.com/FZJ-IEK3-VSA/hplib) (a linear regression of the COP on temperatures, fitted to
Keymark certification data) and the Home Assistant integration
[heating_curve_optimizer](https://bvweerd.github.io/heating_curve_optimizer/algorithm/cop-calculation/).

## Language and time

The phone page is in English, or Finnish with `[web] language = "fi"`. That is the server's default: each
person can switch language with the button in the page's header, and the choice is kept for their login
(`web_prefs.json` in the app directory), so it follows them to every device. Someone who has not chosen gets
the default.

Everything runs on local time in `timezone` (Europe/Helsinki by default): the schedules, the price
days (including the 23- and 25-hour days at the clock changes), the logs and data files, and every
time you give a command or write in a file. None of it depends on the machine's own time zone.
