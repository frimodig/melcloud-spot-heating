# melcloud-spot-heating

[![tests](https://github.com/frimodig/melcloud-spot-heating/actions/workflows/test.yml/badge.svg)](https://github.com/frimodig/melcloud-spot-heating/actions/workflows/test.yml)

Shifts the heating of Mitsubishi Electric air-to-air heat pumps (MELCloud) into the cheapest spot-price
quarter hours: a little warmer when power is cheap, a little cooler when it is dear, and the house
warm when people are home. It runs every 15 minutes from cron, plans on the confirmed day-ahead
prices, and comes with a small phone page for "too cold", "away" and the like.

- **Price rules:** cheap quarters are boosted, dear ones cut, peaks cut hard and pre-heated for; the
  heat for a warm-up is bought in the cheapest quarters before people arrive.
- **Price of heat (optional):** a cold quarter makes heat dearer, so with a COP curve for your pump
  the rules can judge the price of a kWh of heat instead of electricity; the comfort and minimum rules
  stay on the spot price.
- **Comfort first:** comfort windows cap the cuts, each room's own temperature has the last word,
  and "too cold" or "too hot" on the phone moves the plan for a few hours.
- **Fits the house:** one unit, several, or a multi-split; your schedule, windows and rules in one
  small local file; weather and sun from Open-Meteo.
- **Prices** from porssisahko.net (Finland) or ENTSO-E (any European zone).

Not affiliated with or endorsed by Mitsubishi Electric. MELCloud is their trademark.

## Limitations: read first

It only works where all three of its outside services do:

- **Heat pumps: Mitsubishi Electric air-to-air units on classic MELCloud**, through the
  unofficial `pymelcloud` library, which uses MELCloud's private API: Mitsubishi can change or block
  it at any time, so check MELCloud's terms of use before relying on it. Units need MELCloud's
  Wi-Fi adapter. "MELCloud Home" accounts and other brands are not supported: if
  `./run.sh --list` can't log in, the account is probably on MELCloud Home.
- **Prices: a day-ahead spot price in euro cents per kWh** from porssisahko.net (Finland, VAT
  included; the default) or the ENTSO-E Transparency Platform (any European bidding zone; needs a
  free API token), or both, one as the other's fallback. Elsewhere, add a source to `fetch_from`. The price thresholds are tuned to
  Finnish winter prices; they are all settings. The ENTSO-E feed has only been tested against sample
  documents.
- **Weather: Open-Meteo's forecast**, which covers the whole world, for the location you set.
  Without one the controller still runs, with no weather or sun scaling.
- **The phone page** relies on Tailscale (or another proxy that sets the login header named in
  `[web] user_header`); without it everyone gets 403. See [hosting](docs/hosting.md).

The defaults are deliberately plain: a flat 21 C that the price rules shift around, with the rules
that depend on a household's routine switched off ([configuration](docs/configuration.md)).

## Quick start

On any always-on Linux machine with Python 3.11 or newer. [Hosting](docs/hosting.md) walks through a
free hosted setup: a cloud VM, the phone page over Tailscale, and an alert when the controller stops.

```sh
sudo apt update && sudo apt install -y python3 python3-venv git
git clone https://github.com/frimodig/melcloud-spot-heating.git && cd melcloud-spot-heating
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp env.example .env && chmod 600 .env && nano .env   # MELCloud login and location
nano config.local.toml                                # your settings: docs/configuration.md
```

The controller's files are named `ilp_optimizer` after the Finnish for an air-to-air heat pump
(ilmalämpöpumppu).

### Before enabling writes: turn off MELCloud's own automation

The controller sets the target temperature every 15 minutes, so anything else that
changes it will fight with it and the units will flip back and forth. In the MELCloud
app or website, disable for every unit: weekly schedules/timers, holiday mode, and any
other app or automation (Homey, Home Assistant, IFTTT, Alexa routines) that sets
temperatures. Check the same for the wall remote's own timer settings.

### Validate in this order

1. `./run.sh --list` prints each unit: name, power, mode, room and target temperature
   ([units](docs/configuration.md#units)).
2. `./run.sh --plan` prints today's plan, quarter by quarter, without touching anything. Offline:
   `.venv/bin/python ilp_optimizer.py --plan --prices-file sample_prices.json --now 2026-11-10T12:00`.
3. Add the cron line below with `crontab -e` (with your own path), still with `--dry-run`, and read
   `ilp_optimizer.log` for a few days. If a unit is off or not in heat mode, the log says it would be
   switched on: going live will do so ([the season](docs/using-it.md#season-heating-or-cooling)).
4. Remove `--dry-run` to let it write.

```cron
*/15 * * * * /home/ubuntu/melcloud-spot-heating/run.sh --dry-run >/dev/null 2>&1
```

## Documentation

- [How it works](docs/how-it-works.md): every rule, with its defaults.
- [Configuration](docs/configuration.md): your settings, units, price sources, location, language.
- [Using it](docs/using-it.md): the phone page, away, home, holiday, season, too cold, pause.
- [Hosting](docs/hosting.md): a free always-on VM, the page over Tailscale, alerting.
- [Running it](docs/operations.md): updating, the logs and data, failures and alerts, the savings report, tests.

## License

MIT, see [LICENSE](LICENSE).
