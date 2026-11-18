# Contributing

Bug reports, fixes and support for other price sources are welcome. For a larger change, open an issue
first so we can agree on the shape before you write it.

## Where it works

Read the [limitations](README.md#limitations-read-first) first. Most reports are about a setup outside
them: another heat pump brand, MELCloud Home, or a country without a price source. A new price source is a
good contribution (see `fetch_from` in [configuration](docs/configuration.md)); another brand is out of scope.

## Running the tests

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

The tests need no credentials and no network: MELCloud, the price feeds and the weather are faked, and any
test that reaches the network fails. Node is needed for the page tests; without it they are skipped. CI runs
the same command on every push and pull request.

## Changing code

- Add or change a test with the code. Tests run on the made-up house in `tests/house.toml`.
- A change to a rule or a setting updates `config.toml`, `config.local.example.toml` where relevant, and the
  page in `docs/` that describes it, in the same commit.
- One change per pull request, with a message that says why.
- Keep personal data out: no real names, addresses, locations, logins or log lines in code, tests, docs or
  issues.

## Reporting a bug

Use the bug report form. It asks for your country and price source, the MELCloud account type and the log
for the run, with anything personal removed. For security problems see [SECURITY.md](SECURITY.md).
