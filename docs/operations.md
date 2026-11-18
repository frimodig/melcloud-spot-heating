# Running it

How to update the controller, what it records, how it copes with failures, and how to see what it
saves.

## Updating

```sh
cd ~/melcloud-spot-heating && git pull && .venv/bin/pip install -r requirements.txt
sudo systemctl restart spot-heating-web    # only if you run the phone page
```

Cron picks up the new code at its next run. Your `.env`, `web.env`, `config.local.toml` and data
files are not in git, so an update leaves them alone. Look at the changes to `config.toml` and
`config.local.example.toml` for new settings you may want.

## What it writes

All in the app directory, none of it in git. Only the two logs are rotated; copy old months off the
machine when you like.

| File | What | Size |
|---|---|---|
| `ilp_optimizer.log` | what each run did and why | rotated at 1 MB, 3 kept (`log_max_bytes`, `log_backup_count`) |
| `history-YYYY-MM.csv` | one row per unit per run: the action, room temperature, old and new target, base, shift, outdoor temperature, price, the rule that set it and the reason | about 10 KB a day per unit |
| `telemetry-YYYY-MM.csv` | what each unit reported before the run: room, target, power, mode, fan, energy meter, and the forecast's outdoor temperature, humidity, wind and weather | about the same |
| `price_history.csv` | every price interval seen, once (the price feeds only cover today and tomorrow) | a few KB a day |
| `web.log` | who pressed which button on the phone page, and its errors | rotated like the controller's log |
| `web_prefs.json` | each phone page user's own settings (the language), by login | a few lines |
| `state.json`, `unit_state.json` | the circuit breaker's count; each unit's last target, holds and, with `season_enforce = "on_change"`, the season it was last put in | small |

The history's action is `set`, `keep`, `skip`, `dry-run`, `hold`, `set-failed`, `mode-switch`,
`switch-failed`, `mode-mismatch`, `away-off`, `away-on` (and their `-failed`), `normal-set`,
`normal-end` or `normal-failed`. With each quarter's room temperature in the next row, it
shows whether a cut was felt. The telemetry is for working out how fast the rooms cool and heat:
`fan_actual` 0 means the fan was stopped, so idle stretches can be told from heating ones. The
energy meter (`energy_kwh`) updates every 1.5 to 3 hours and exists only on some units
(`./run.sh --list` shows `energy_meter`).

## When something fails

A failed connection, DNS lookup, timeout or a 5xx or 429 answer is retried after 5, 10 and 20
seconds, and every call gives up after 60 seconds, so a hung MELCloud write cannot stall a run. A
rejected login is never retried, since repeating it can lock the account. One failing unit does not
stop the others.

After 3 runs in a row fail to log in or connect, a circuit breaker skips runs for 60 minutes (logged
as "circuit breaker open"); the next run is a trial that closes it on success. Delete `state.json`
to reset it. Settings: `[resilience]`.

**The heartbeat.** With `HEARTBEAT_URL` in `.env`, each healthy run pings that URL, and the service
behind it (such as Healthchecks.io) emails you when the pings stop. That catches a dead cron, a
stopped or lost machine, and a controller that fails every run: a run with failures, a failed login
or an open breaker does not ping, while a paused one still does. A failed ping is logged without the
URL. Setting up the check: [hosting](hosting.md), section 4.

## The savings estimate

`./run.sh --report` estimates what the controller saves against how the house ran before
(`[baseline]`, a schedule like `[schedule]`), per month and for the last 7 days; `--report 2026-11`
shows every day of that month. It reads only the history, telemetry and price files, so it runs
anywhere they are: `scripts/pull-telemetry.sh` copies them from the machine (set `ILP_VM_HOST`, and
`ILP_VM_DIR` if the app is not in `~/melcloud-spot-heating`) into a folder of their own. In a clone
of this repository with its `.venv` and your `config.local.toml` (for `[baseline]`),
`.venv/bin/python ilp_optimizer.py --report --data-dir ~/vm-data` reads them; no `.env` is needed.

**With energy meters**, the report is in kWh and euros. The meter readings, summed over the units,
calibrate one number, the kWh per unit-degree-quarter of heating demand (target minus outdoor
temperature), and that turns the model into energy and money for both the controller and the
baseline. It shows what was measured, what the model gives for the controller and for the baseline,
and what was saved, split into **less heat** (the energy saved, at the baseline's average price) and
**cheaper hours** (the same energy bought in cheaper quarters). Most of a saving against a constant
temperature comes from the lower average temperature; the price shifting adds a little. The header
says how many days the calibration rests on and how far the days stray from it. On a multi-split
it assumes the units' meters add up to the whole (the outdoor unit's draw on one of them).

**Without meters** (or where they don't overlap the history) it gives percentages only: saving,
less heat and price paid, with the cost as price times demand.

Both are rough: they don't model the heat a pre-heat stores or the heat pumps' lower efficiency in
hard frost, and the baseline is how the house is remembered to have run, not a measurement. Treat
the first weeks as an order of magnitude. Quarters missing a target, a price or an outdoor
temperature are skipped and counted under coverage; dry runs are left out; away, holiday and home
days count as they ran.

## Tests

```sh
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

The tests need no network or credentials: MELCloud, the price feeds and the weather are faked, and
the page's script runs in Node when it is installed. GitHub Actions runs them on every push.
