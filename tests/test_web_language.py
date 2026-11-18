import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

import ilp_optimizer as o
import web
from web_text import EN, FI, TEXT, text

ROOT = Path(__file__).resolve().parent.parent
LIVE = o.load_config(ROOT / "config.toml")
ME = {web.USER_HEADER: "me@example.com"}


def in_language(language):
    return {**LIVE, "web": {**LIVE["web"], "language": language}}


def get(cfg, path):
    async def main():
        app = web.make_app(cfg, {"me@example.com": "Anna"}, Path("."))
        async with TestClient(TestServer(app)) as client:
            r = await client.get(path, headers=ME)
            return await r.text()
    return asyncio.run(main())


def test_the_shipped_config_asks_for_english():
    assert LIVE["web"]["language"] == "en"


def test_the_page_is_english_by_default():
    cfg = {**LIVE, "web": {k: v for k, v in LIVE["web"].items() if k != "language"}}   # no language set
    for html in (get(cfg, "/"), get(LIVE, "/")):
        assert '<html lang="en">' in html and "<title>Heat pumps</title>" in html
        assert ">Too cold<" in html and "Normal heat now" in html
        assert "Liian kylmä" not in html and "Lämmitys" not in html


def test_finnish_is_the_same_page_as_before():
    html = get(in_language("fi"), "/")
    assert '<html lang="fi">' in html and "<title>Ilmalämpöpumput</title>" in html
    assert 'content="Lämpö"' in html and ">Liian kylmä<" in html and ">Huomenna<" in html
    assert 'aria-label="Hinta ja tavoitelämpötila"' in html


def test_both_catalogs_have_the_same_keys():
    assert set(EN) == set(FI)
    assert TEXT == {"en": EN, "fi": FI}


def test_a_text_has_the_same_placeholders_in_both_languages():
    names = lambda v: sorted(re.findall(r"\{(\w+)\}", v))
    assert [k for k in EN if names(EN[k]) != names(FI[k])] == []


def test_english_has_no_finnish_letters():
    assert not [k for k, v in EN.items() if re.search("[äöÄÖ]", v)]


def test_the_page_uses_only_keys_the_catalog_has_and_leaves_no_placeholder():
    source = web.PAGE
    used = set(re.findall(r"\{\{(\w+)\}\}", source)) | set(re.findall(r"\bT\.(\w+)", source)) \
        | set(re.findall(r'\bT\[[^\]]*"(\w+)"', source))
    assert used and used <= set(EN)
    for language in ("en", "fi"):
        html = web.render_page(in_language(language))
        assert not re.search(r"\{\{\w+\}\}|/\*TEXT\*/", html)
        assert html.count("const T = {") == 1


def test_the_catalog_in_the_page_cannot_close_the_script_tag():
    assert "</" not in web.page_text_json({"x": "</script><b>"})


def test_an_unknown_language_falls_back_to_english_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="ilp_web"):
        assert text(in_language("sv")) is EN
    assert "sv" in caplog.text


def test_the_manifest_and_the_server_messages_follow_the_language():
    assert json.loads(get(in_language("en"), "/manifest.json"))["name"] == "Heat pumps"
    assert json.loads(get(in_language("fi"), "/manifest.json"))["name"] == "Ilmalämpöpumput"
    now = datetime(2026, 11, 10, 12, 0, tzinfo=o.ZoneInfo(LIVE["timezone"]))
    assert web.do_action("pause", "off", False, in_language("en"), now) == "Control on."
    assert web.do_action("pause", "off", False, in_language("fi"), now) == "Ohjaus päällä."
    try:
        web.do_action("season", "heat", False, in_language("en"), now)
    except web.Refused as e:
        assert str(e) == "Changing the season needs confirmation."
    else:
        raise AssertionError("a season change without confirmation must be refused")


def test_english_reasons_and_pills():
    plan = web.reason_text(EN, "cheap 3.4c x0.71, comfort hours: cut capped at 0, clamped", "cheap")
    assert plan == {"text": "Pre-heating", "notes": ["comfort hours limit the cut"]}
    assert EN["pill_cheap"] == "Cheap" and web.reason_text(EN, "x", "other")["text"] == "Basic schedule"


def test_the_page_quotes_the_configured_values_not_fixed_numbers():
    cfg = o.load_config(ROOT / "config.toml")
    cfg = {**cfg, "pause": {"heat_temp": 19.0}, "nudge": {**cfg["nudge"], "step": 0.5, "hours": 2.0},
           "nobody_home": {**cfg["nobody_home"], "temp": 18.0, "dear_temp": 17.0},
           "web": {**cfg["web"], "language": "fi"}}
    html = web.render_page(cfg)
    assert "+0,5 °C, 2 h" in html and "17–18 °C" in html and "asetetaan 19 °C:een" in html
    assert "{pause_temp}" not in html and "{nudge_step}" not in html


# --- a person's own language: kept by login, the server's [web] language is the default ---------------

ANNA, BEN = {web.USER_HEADER: "anna@example.com"}, {web.USER_HEADER: "ben@example.com"}
POST = {"X-Requested-With": web.CSRF_VALUE}
USERS = {"anna@example.com": "Anna", "ben@example.com": "Ben"}


def session(cfg, app_dir, steps):
    """Run `steps(client)` against a fresh app on `app_dir` (a new app is a restarted server)."""
    async def main():
        async with TestClient(TestServer(web.make_app(cfg, USERS, app_dir))) as client:
            return await steps(client)
    return asyncio.run(main())


def choose(client, who, language, headers=POST):
    return client.post("/api/language", json={"language": language}, headers={**who, **headers})


def test_the_choice_is_kept_for_that_login_only_and_survives_a_restart(tmp_path):
    async def anna_picks(c):
        r = await choose(c, ANNA, "fi")
        assert r.status == 200 and await r.json() == {"language": "fi"}
    session(LIVE, tmp_path, anna_picks)

    async def after_restart(c):
        anna = await (await c.get("/", headers=ANNA)).text()
        ben = await (await c.get("/", headers=BEN)).text()
        assert '<html lang="fi">' in anna and '<html lang="en">' in ben   # Ben still has the server default
        manifests = [json.loads(await (await c.get("/manifest.json", headers=h)).text())["name"] for h in (ANNA, BEN)]
        assert manifests == ["Ilmalämpöpumput", "Heat pumps"]
    session(LIVE, tmp_path, after_restart)


def test_a_person_can_choose_against_the_server_default_and_back(tmp_path):
    async def go(c):
        await choose(c, BEN, "en")
        await choose(c, ANNA, "en")
        await choose(c, ANNA, "fi")
        assert '<html lang="fi">' in await (await c.get("/", headers=ANNA)).text()
        await choose(c, ANNA, "en")
        assert '<html lang="en">' in await (await c.get("/", headers=ANNA)).text()
    session(in_language("fi"), tmp_path, go)
    assert json.loads((tmp_path / web.PREFS_FILE).read_text()) == {
        "anna@example.com": {"language": "en"}, "ben@example.com": {"language": "en"}}
    assert [p.name for p in tmp_path.iterdir()] == [web.PREFS_FILE]   # no temp file left behind


def test_the_page_offers_the_other_language(tmp_path):
    async def go(c):
        en = await (await c.get("/", headers=ANNA)).text()
        await choose(c, ANNA, "fi")
        fi = await (await c.get("/", headers=ANNA)).text()
        return en, fi
    en, fi = session(LIVE, tmp_path, go)
    assert 'data-lang="fi" lang="fi" aria-label="Language">Suomi<' in en
    assert 'data-lang="en" lang="en" aria-label="Kieli">English<' in fi


def test_the_plan_words_in_the_status_follow_the_person_not_the_cached_plan(tmp_path):
    async def go(c):
        async def plan(a, day_offset=0):
            return {"date": "2026-11-12", "has_prices": True, "times": ["00:00"], "price": [4.2], "target": [19.5],
                    "now_index": 0, "blocks": [], "now": {"price": 4.2, "target": 19.5, "kind": "cheap",
                                                          "reason_log": "cheap 3.4c x0.71", "below_base": False}}
        web.plan_data = plan
        await choose(c, ANNA, "fi")
        pills = [(await (await c.get("/api/status", headers=h)).json())["plan"]["now"]["pill"] for h in (ANNA, BEN)]
        assert pills == [FI["pill_cheap"], EN["pill_cheap"]] and FI["pill_cheap"] != EN["pill_cheap"]
    saved = web.plan_data
    try:
        session(LIVE, tmp_path, go)
    finally:
        web.plan_data = saved


def test_server_messages_follow_the_persons_language(tmp_path):
    async def go(c):
        await choose(c, ANNA, "fi")
        body = {"action": "pause", "value": "off"}
        return [(await (await c.post("/api/action", json=body, headers={**h, **POST})).json())["message"]
                for h in (ANNA, BEN)]
    assert session(LIVE, tmp_path, go) == ["Ohjaus päällä.", "Control on."]


def test_a_wrong_request_is_refused_and_stores_nothing(tmp_path):
    async def go(c):
        assert (await choose(c, ANNA, "sv")).status == 400
        assert (await choose(c, ANNA, None)).status == 400
        assert (await c.post("/api/language", data="not json", headers={**ANNA, **POST})).status == 400
        assert (await choose(c, ANNA, "fi", headers={})).status == 403                     # no CSRF header
        assert (await choose(c, {web.USER_HEADER: "eve@example.com"}, "fi")).status == 403   # not a listed login
    session(LIVE, tmp_path, go)
    assert not (tmp_path / web.PREFS_FILE).exists()


def test_a_missing_damaged_or_odd_prefs_file_gives_the_default_and_the_next_choice_repairs_it(tmp_path, caplog):
    path = tmp_path / web.PREFS_FILE
    for content in (None, "{not json", "[1, 2]", b"\xff\xfe", '{"anna@example.com": "fi"}',
                    '{"anna@example.com": {"language": "sv"}}'):
        if content is not None:
            path.write_bytes(content if isinstance(content, bytes) else content.encode())

        async def go(c):
            assert '<html lang="en">' in await (await c.get("/", headers=ANNA)).text()
            assert (await choose(c, ANNA, "fi")).status == 200
            assert '<html lang="fi">' in await (await c.get("/", headers=ANNA)).text()
        session(LIVE, tmp_path, go)
        path.unlink()
    assert "unreadable" in caplog.text or "not a JSON object" in caplog.text


def test_a_language_that_cannot_be_saved_says_so(tmp_path, caplog):
    async def go(c):
        with caplog.at_level(logging.ERROR, logger="ilp_web"):
            r = await choose(c, ANNA, "fi")
        assert r.status == 500 and (await r.json())["error"] == EN["language_not_saved"]
    session(LIVE, tmp_path / "gone", go)   # the directory does not exist
    assert "language not saved" in caplog.text


def test_two_choices_at_once_both_survive(tmp_path):
    async def go(c):
        await asyncio.gather(choose(c, ANNA, "fi"), choose(c, BEN, "fi"))
    session(LIVE, tmp_path, go)
    assert set(json.loads((tmp_path / web.PREFS_FILE).read_text())) == set(USERS)
