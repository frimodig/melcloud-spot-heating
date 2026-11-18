"""The price feeds: porssisahko.net (Finland) and the ENTSO-E Transparency Platform (any European zone)."""
import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

import ilp_optimizer as o

ROOT = Path(__file__).resolve().parent.parent
CFG = o.load_config(ROOT / "config.toml", ROOT / "tests" / "house.toml")

DOC = """<?xml version="1.0" encoding="UTF-8"?>
<Publication_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3">
  <mRID>x</mRID>
  <TimeSeries>
    <mRID>1</mRID>
    <curveType>A03</curveType>
    <Period>
      <timeInterval><start>2026-11-16T23:00Z</start><end>2026-11-17T00:00Z</end></timeInterval>
      <resolution>PT15M</resolution>
      <Point><position>1</position><price.amount>100.00</price.amount></Point>
      <Point><position>3</position><price.amount>-5.5</price.amount></Point>
    </Period>
  </TimeSeries>
  <TimeSeries>
    <mRID>2</mRID>
    <Period>
      <timeInterval><start>2026-11-16T23:00Z</start><end>2026-11-16T23:15Z</end></timeInterval>
      <resolution>PT15M</resolution>
      <Point><position>1</position><price.amount>999</price.amount></Point>
    </Period>
  </TimeSeries>
</Publication_MarketDocument>"""


def test_entsoe_prices_become_c_per_kwh_and_a_left_out_position_repeats_the_one_before():
    got = o.parse_entsoe(DOC)
    start = datetime(2026, 11, 16, 23, 0, tzinfo=timezone.utc)
    assert [i.start for i in got] == [start.replace(minute=m) for m in (0, 15, 30, 45)]
    assert [i.price for i in got] == [10.0, 10.0, -0.55, -0.55]     # 100 EUR/MWh = 10 c/kWh
    assert all(i.seconds == 900 for i in got)


def test_entsoe_vat_and_margin_are_added():
    got = o.parse_entsoe(DOC, vat_pct=25.5, margin=0.5)
    assert got[0].price == 13.05                                      # 10 x 1.255 + 0.5


def test_an_entsoe_answer_without_prices_says_why():
    ack = """<Acknowledgement_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-1:acknowledgementdocument:7:0">
      <Reason><code>999</code><text>No matching data found</text></Reason></Acknowledgement_MarketDocument>"""
    with pytest.raises(ValueError, match="No matching data found"):
        o.parse_entsoe(ack)


def with_sources(*names):
    return {**CFG, "prices": {**CFG["prices"], "sources": list(names)}}


def test_entsoe_needs_its_token(monkeypatch):
    monkeypatch.delenv("ENTSOE_TOKEN", raising=False)
    with pytest.raises(ValueError, match="ENTSOE_TOKEN"):
        asyncio.run(o.fetch_prices(with_sources("entsoe")))


def test_an_unknown_source_is_refused():
    with pytest.raises(ValueError, match="not known"):
        asyncio.run(o.fetch_prices(with_sources("nordpool")))


def test_the_default_source_is_porssisahko():
    assert o.price_sources(CFG) == ["porssisahko"]
    assert o.price_sources({"prices": {"source": "entsoe"}}) == ["entsoe"]      # an older single source


SOME = [o.PriceInterval(datetime(2026, 11, 10, 0, tzinfo=timezone.utc), datetime(2026, 11, 10, 0, 15, tzinfo=timezone.utc), 5.0)]


def fake_sources(monkeypatch, answers):
    asked = []

    async def fetch_from(cfg, source):
        asked.append(source)
        answer = answers[source]
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(o, "fetch_from", fetch_from)
    return asked


def test_the_first_source_that_gives_prices_is_used(monkeypatch):
    asked = fake_sources(monkeypatch, {"porssisahko": SOME, "entsoe": ValueError("not asked")})
    assert asyncio.run(o.fetch_prices(with_sources("porssisahko", "entsoe"))) == SOME and asked == ["porssisahko"]


def test_a_failing_source_falls_back_to_the_next(monkeypatch, caplog):
    asked = fake_sources(monkeypatch, {"porssisahko": ValueError("down"), "entsoe": SOME})
    assert asyncio.run(o.fetch_prices(with_sources("porssisahko", "entsoe"))) == SOME
    assert asked == ["porssisahko", "entsoe"] and "trying entsoe" in caplog.text


def test_a_source_with_no_prices_falls_back_too(monkeypatch):
    fake_sources(monkeypatch, {"porssisahko": [], "entsoe": SOME})
    assert asyncio.run(o.fetch_prices(with_sources("porssisahko", "entsoe"))) == SOME


def test_when_every_source_fails_the_last_error_is_raised_and_the_plan_runs_without_prices(monkeypatch):
    fake_sources(monkeypatch, {"porssisahko": ValueError("down"), "entsoe": ConnectionError("also down")})
    with pytest.raises(ConnectionError):
        asyncio.run(o.fetch_prices(with_sources("porssisahko", "entsoe")))
    assert asyncio.run(o.get_prices_or_none(with_sources("porssisahko", "entsoe"), None)) is None


def test_a_source_is_retried_before_the_next_is_tried(monkeypatch):
    calls = {"porssisahko": 0}

    async def fetch_from(cfg, source):
        if source == "porssisahko":
            calls["porssisahko"] += 1
            if calls["porssisahko"] == 1:
                raise ConnectionError("blip")            # transient: retried
            return SOME
        raise AssertionError("no fallback needed")
    monkeypatch.setattr(o, "fetch_from", fetch_from)
    assert asyncio.run(o.fetch_prices(with_sources("porssisahko", "entsoe"))) == SOME and calls["porssisahko"] == 2
