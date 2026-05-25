"""Offline coverage for lodestone_probe parsers + fetch-mocked collectors.

Network and browser-cookie paths are exercised by monkeypatching ``probe.fetch``
with a URL->HTML map, so nothing here hits Lodestone.
"""
from __future__ import annotations

import requests
from bs4 import BeautifulSoup

from CharacterScraping import lodestone_probe as probe

BASE = "https://na.finalfantasyxiv.com/lodestone/character/1000/"


def _page(html: str, url: str = BASE) -> probe.Page:
    return probe.Page(url=url, soup=BeautifulSoup(html, "html.parser"))


# --- tiny pure helpers ------------------------------------------------------

def test_clean_and_text_of():
    assert probe.clean("  a\n  b ") == "a b"
    assert probe.text_of(None) == ""
    assert probe.text_of(BeautifulSoup("<p> x  y </p>", "html.parser").p) == "x y"


def test_parse_int():
    assert probe._parse_int("1,234") == 1234
    assert probe._parse_int("--") is None
    assert probe._parse_int("") is None
    assert probe._parse_int("abc") is None


def test_character_root_url():
    assert probe.character_root_url(BASE + "quest/") == BASE
    assert probe.character_root_url("https://example.com/x") == "https://example.com/x/"
    assert probe.character_root_url("") == ""


def test_build_paginated_url_variants():
    tt = probe.build_paginated_url(BASE + "goldsaucer/tripletriad/", "goldsaucer/tripletriad", 2)
    assert "page=2" in tt and "hold=" in tt
    quest = probe.build_paginated_url(BASE + "quest/", "quest", 3)
    assert quest.endswith("#anchor_quest") and "page=3" in quest
    generic = probe.build_paginated_url(BASE + "emote/", "emote", 4)
    assert "page=4" in generic


def test_character_output_stem():
    assert probe.character_output_stem({"profile": {"identity": {"name": "Y'shtola Rhul"}}}) == "Y_shtola_Rhul"
    assert probe.character_output_stem({}) == probe.DEFAULT_OUTPUT.stem
    assert probe.character_output_stem({"profile": {"identity": {"name": "   "}}}) == probe.DEFAULT_OUTPUT.stem


def test_ensure_browser_cookie3_available_returns_bool():
    assert isinstance(probe.ensure_browser_cookie3_available(), bool)


def test_summarize_session_cookies():
    session = probe.build_session()
    session.cookies.set("ldst_sessid", "x", domain="finalfantasyxiv.com")
    session.cookies.set("plain", "y", domain="finalfantasyxiv.com")
    summary = probe.summarize_session_cookies(session)
    assert summary["cookie_count"] == 2
    assert "ldst_sessid" in summary["auth_like_cookie_names"]
    session.close()


# --- profile / jobs / pagination -------------------------------------------

def test_extract_profile():
    html = """
    <div class="frame__chara__title">Adventurer</div>
    <div class="frame__chara__name">Tester McTest</div>
    <div class="frame__chara__world">Behemoth [Primal]</div>
    <div class="character__profile__data__detail">
      <div class="character-block">
        <p class="character-block__title">Race/Clan/Gender</p>
        <p>Hyur</p>
      </div>
    </div>
    <div class="frame__chara__face"><img src="face.png"/></div>
    <meta property="og:image" content="https://img2.finalfantasyxiv.com/p.png"/>
    """
    out = probe.extract_profile(_page(html))
    assert out["identity"]["name"] == "Tester McTest"
    assert out["identity"]["world"] == "Behemoth"
    assert out["identity"]["data_center"] == "Primal"
    assert out["display_attributes"]["race_clan_gender"] == "Hyur"
    assert out["images"]["avatar"] == "face.png"
    assert out["images"]["portrait"].endswith("p.png")


def test_parse_jobs():
    html = """
    <h4 class="heading--lead">Tank</h4>
    <ul class="character__job">
      <li>
        <div class="character__job__name" data-tooltip="Paladin tooltip">Paladin</div>
        <div class="character__job__level">90</div>
        <div class="character__job__exp">1,234 / 5,678</div>
      </li>
    </ul>
    """
    jobs = probe.parse_jobs(_page(html))
    row = jobs["Tank"][0]
    assert row["job"] == "Paladin" and row["level"] == 90
    assert row["xp"] == 1234 and row["xp_max"] == 5678


def test_parse_total_and_pagination():
    assert probe.parse_total(_page('<div class="minion__sort__total"><span>42</span></div>'), "Minions") == 42
    assert probe.parse_total(_page("<p>Total: 7</p>"), "X") == 7

    pager = _page('<div class="btn__pager__current">Page 1 of 3</div>')
    pag = probe.parse_pagination(pager)
    assert pag["current"] == 1 and pag["total_pages"] == 3


def test_parse_quest_history():
    html = """
    <li class="entry__quest">
      <div href="/lodestone/character/1000/quest/detail/9/">
        <p class="entry__quest__name"><span>-</span><span>The Ultimate Weapon</span></p>
      </div>
      <script>ldst_strftime(1609459200, 'YMD');</script>
    </li>
    """
    out = probe.parse_quest_page(_page(html))
    assert out["entries"][0]["title"] == "The Ultimate Weapon"
    assert out["entries"][0]["date"]  # epoch decoded


def test_detect_auth_markers():
    html = "<script>ldst_is_loggedin = true;</script><a class='link_logout'>Log Out</a>"
    markers = probe.detect_auth_markers(_page(html))
    assert markers["likely_authenticated"] is True
    assert markers["ldst_is_loggedin_true"] is True


def test_parse_pvp_and_trust():
    pvp = probe.parse_pvp_page(_page(
        '<div class="character__pvp">'
        '<div class="character__pvp__rank__title">Series Rank</div>'
        '<div class="character__pvp__rank">Wins: 10</div></div>'
    ))
    assert pvp["stats"]["rank_title"] == "Series Rank"
    assert pvp["stats"]["wins"] == "10"
    # no pvp block -> falls back to plain additional page
    assert "stats" not in probe.parse_pvp_page(_page("<div>nothing</div>"))

    trust = probe.parse_trust_page(_page(
        '<ul class="trust__character"><li>'
        '<div class="trust__character__name">Alphinaud</div>'
        '<div class="trust__level">Level 90</div></li></ul>'
    ))
    assert trust["entries"][0]["name"] == "Alphinaud"
    assert trust["entries"][0]["level"] == 90


def test_parse_authenticated_page_by_path_dispatch():
    assert probe.parse_authenticated_page_by_path(_page("<div/>"), "quest")["url"] == BASE
    # unknown path falls through to the generic additional-page parser
    out = probe.parse_authenticated_page_by_path(_page("<div class='heading--md'>Misc</div>"), "blog")
    assert out["heading"] == "Misc"


# --- fetch-mocked collectors ------------------------------------------------

def _install_fetch(monkeypatch, pages: dict[str, str], errors: set[str] = frozenset()):
    def fake_fetch(url, session, **kwargs):
        if url in errors:
            raise requests.RequestException("simulated network error")
        return _page(pages.get(url, "<html></html>"), url=url)

    monkeypatch.setattr(probe, "fetch", fake_fetch)


def test_parse_achievements_multipage_with_error(monkeypatch):
    entry = (
        '<li class="entry">'
        '<a class="entry__achievement" href="/lodestone/character/1000/achievement/detail/1/">'
        '<p class="entry__activity__txt">Battle achievement "First Steps" earned!</p></a></li>'
    )
    first = (
        '<span class="achievement__point">120</span>'
        '<span class="parts__total">2 Total</span>'
        '<div class="btn__pager__current">Page 1 of 2</div>'
        f'<ul>{entry}</ul>'
    )
    p2_url = probe.urljoin(BASE, "?page=2#anchor_achievement")
    _install_fetch(monkeypatch, {BASE: first}, errors={p2_url})
    out = probe.parse_achievements(_page(first), probe.build_session())
    assert out["points"] == 120 and out["total"] == 2
    assert out["entries"][0]["title"] == "First Steps"
    assert out["errors"][0]["page"] == 2  # page 2 failed but was tolerated


def test_parse_collection_with_tooltip(monkeypatch):
    tip_url = probe.urljoin(BASE, "tooltip/m1")
    minion_html = '<div class="minion__list_icon" data-tooltip_href="tooltip/m1"></div>'
    tip_html = (
        '<div class="minion__header__label">Wind-up Cursor</div>'
        '<div class="minion__text">A tiny minion.</div>'
        '<a class="minion__item_icon" href="#" data-tooltip="Minion Whistle"></a>'
    )
    _install_fetch(monkeypatch, {tip_url: tip_html})
    result = probe.parse_collection(_page(minion_html), probe.build_session(), "minion")
    assert result["items"][0]["name"] == "Wind-up Cursor"
    assert result["items"][0]["item_name"] == "Minion Whistle"


def test_collect_paginated_additional_page_multipage(monkeypatch):
    first_url = probe.urljoin(BASE, "emote/")
    first = (
        '<div class="btn__pager__current">Page 1 of 2</div>'
        '<ul class="emote__list"><li class="js__btn_press"><p>/wave</p></li></ul>'
    )
    page2_url = probe.build_paginated_url(first_url, "emote", 2)
    page2 = '<ul class="emote__list"><li class="js__btn_press"><p>/dance</p></li></ul>'
    _install_fetch(monkeypatch, {first_url: first, page2_url: page2})
    out = probe.collect_paginated_additional_page(BASE, "emote", probe.build_session())
    names = {e["name"] for e in out["entries"]}
    assert names == {"/wave", "/dance"}
    assert out["pagination"]["pages_collected"] == 2


def test_collect_authenticated_pages_handles_error(monkeypatch):
    good_url = probe.urljoin(BASE, "emote/")
    bad_url = probe.urljoin(BASE, "currency/")
    _install_fetch(
        monkeypatch,
        {good_url: '<ul class="emote__list"><li class="js__btn_press"><p>/wave</p></li></ul>'},
        errors={bad_url},
    )
    out = probe.collect_authenticated_pages(BASE, probe.build_session(), ["emote", "currency", ""])
    assert out["emote"]["entries"]
    assert "error" in out["currency"]


def test_parse_currency_page():
    html = """
    <ul class="character__currency__list">
      <li>
        <p class="heading--lead">Tomestones</p>
        <div class="currency__box">
          <p class="currency__box__text__name">Allagan Tomestone</p>
          <p class="currency__box__text">Allagan Tomestone 2,000</p>
        </div>
      </li>
    </ul>
    """
    out = probe.parse_currency_page(_page(html))
    group = out["groups"][0]
    assert group["section"] == "Tomestones"
    assert group["items"][0]["label"] == "Allagan Tomestone"
    assert "2,000" in group["items"][0]["value"]


def test_parse_goldsaucer_page():
    html = """
    <h3 class="heading--lead">Manderville Gold Saucer Points</h3>
    <ul class="character__currency__list"><p>12,345</p></ul>
    <span class="character__goldsaucer__text__number">2</span>
    """
    out = probe.parse_goldsaucer_page(_page(html))
    assert out["stats"]["mgp"] == 12345
    assert out["stats"]["jumbo_cactpot_ticket_allowance"] == "2"


# --- fetch retry/transport --------------------------------------------------

class _FakeResp:
    def __init__(self, url, content=b"<html></html>", status=200, fail=False):
        self.url = url
        self.content = content
        self.status_code = status
        self._fail = fail

    def raise_for_status(self):
        if self._fail:
            raise requests.HTTPError("bad status")


class _FakeSession:
    def __init__(self, behaviors):
        # behaviors: list of ("ok"|"raise"|"httperror")
        self._behaviors = list(behaviors)
        self.calls = 0

    def get(self, url, timeout=None):
        self.calls += 1
        action = self._behaviors.pop(0)
        if action == "raise":
            raise requests.ConnectionError("dropped")
        return _FakeResp(url, fail=(action == "httperror"))


def test_fetch_success(monkeypatch):
    logs = []
    page = probe.fetch(BASE, _FakeSession(["ok"]), progress=logs.append)
    assert page.url == BASE
    assert logs


def test_fetch_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(probe.time, "sleep", lambda *_: None)
    logs = []
    session = _FakeSession(["raise", "ok"])
    page = probe.fetch(BASE, session, progress=logs.append)
    assert page.url == BASE
    assert session.calls == 2


def test_fetch_exhausts_retries(monkeypatch):
    monkeypatch.setattr(probe.time, "sleep", lambda *_: None)
    session = _FakeSession(["raise", "raise", "raise"])
    try:
        probe.fetch(BASE, session, retries=3)
        raise AssertionError("expected RequestException")
    except requests.RequestException:
        pass


# --- CLI prompt helpers -----------------------------------------------------

def test_prompt_helpers(monkeypatch):
    answers = iter(["", "x", "yes"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    # first "" is rejected (prints), "x" not valid, "yes" accepted
    assert probe.prompt_choice("? ", {"yes": "y", "no": "n"}) == "yes"

    monkeypatch.setattr("builtins.input", lambda *a: "")
    assert probe.prompt_text("Name", default="Default") == "Default"

    monkeypatch.setattr("builtins.input", lambda *a: "Custom")
    assert probe.prompt_text("Name") == "Custom"


def test_save_payload_roundtrip(tmp_path):
    out = tmp_path / "p.json"
    probe.save_payload({"a": 1, "name": "X"}, out)
    import json
    assert json.loads(out.read_text(encoding="utf-8"))["a"] == 1


def test_collect_character_full(monkeypatch):
    logs: list[str] = []
    pages = {
        BASE: '<div class="frame__chara__name">Hero</div>',
        probe.urljoin(BASE, "class_job/"):
            '<h4 class="heading--lead">Tank</h4>'
            '<ul class="character__job"><li>'
            '<div class="character__job__name" data-tooltip="t">Paladin</div>'
            '<div class="character__job__level">90</div>'
            '<div class="character__job__exp">- / -</div></li></ul>',
        probe.urljoin(BASE, "minion/"): "<div>no minions</div>",
        probe.urljoin(BASE, "mount/"): "<div>no mounts</div>",
        probe.urljoin(BASE, "achievement/"): '<span class="achievement__point">5</span>',
    }
    _install_fetch(monkeypatch, pages)
    out = probe.collect_character(BASE, progress=logs.append)
    assert out["profile"]["identity"]["name"] == "Hero"
    assert out["class_job"]["Tank"][0]["job"] == "Paladin"
    assert out["achievements"]["points"] == 5
    assert logs  # progress callback fired
