"""Phase 3 — offline Lodestone HTML parsing.

No network, no browser cookies: every parser is fed a BeautifulSoup built from
a small synthetic HTML snippet that mirrors the real Lodestone DOM selectors.
The saved ``CharacterScraping/ExampleHTMLs`` corpus is git-ignored, so a second
opportunistic test exercises it only when the files happen to be present.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from CharacterScraping import lodestone_probe as probe

pytestmark = pytest.mark.scraper

_URL = "https://na.finalfantasyxiv.com/lodestone/character/1000/x/"


def _page(html: str) -> probe.Page:
    return probe.Page(url=_URL, soup=BeautifulSoup(html, "html.parser"))


def test_parse_emote_page():
    page = _page(
        """
        <ul class="emote__list">
          <li class="js__btn_press" data-category="General"><p>/wave</p></li>
          <li class="js__btn_press" data-category="Special"><p>/huzzah</p></li>
        </ul>
        """
    )
    payload = probe.parse_emote_page(page)
    names = {e["name"] for e in payload["entries"]}
    assert names == {"/wave", "/huzzah"}
    assert payload["heading"] == "Emotes"


def test_parse_orchestrion_filters_unacquired():
    page = _page(
        """
        <ul class="orchestrion-list">
          <li class="orchestrion-list__item">
            <div class="orchestrion-list__num">1</div>
            <div class="orchestrion-list__name">Song A</div>
          </li>
          <li class="orchestrion-list__item unacquired">
            <div class="orchestrion-list__num">2</div>
            <div class="orchestrion-list__name">Song B</div>
          </li>
        </ul>
        """
    )
    payload = probe.parse_orchestrion_page(page)
    assert payload["totals"] == {"acquired": 1, "visible": 2}
    assert [e["name"] for e in payload["entries"]] == ["Song A"]


def test_parse_bluemage_keeps_only_learned():
    page = _page(
        """
        <ul>
          <li class="bluemage-action__list__item sys-reward">
            <div class="bluemage-action__index">No. 1</div>
            <div class="bluemage-action__name">Water Cannon</div>
          </li>
          <li class="bluemage-action__list__item sys-no_reward">
            <div class="bluemage-action__index">No. 2</div>
            <div class="bluemage-action__name">Flame Thrower</div>
          </li>
        </ul>
        """
    )
    payload = probe.parse_bluemage_page(page)
    assert payload["totals"] == {"learned": 1, "visible": 2}
    assert [e["name"] for e in payload["entries"]] == ["Water Cannon"]


def test_parse_tripletriad_entries_and_totals():
    page = _page(
        """
        <ul class="tripletriad-card_list">
          <li>
            <div class="num"><span>1</span></div>
            <div class="name_inner">Dodo</div>
            <p class="rarity"><img src="star.png"/></p>
            <p class="type">Beastman</p>
          </li>
        </ul>
        <div class="tripletriad-settings__total">Displaying results (1 out of 380 cards)</div>
        """
    )
    payload = probe.parse_tripletriad_page(page)
    assert payload["totals"] == {"shown": 1, "all_cards": 380}
    entry = payload["entries"][0]
    assert entry["number"] == 1
    assert entry["name"] == "Dodo"
    assert entry["rarity"] == 1
    assert entry["type"] == "Beastman"


def test_parse_additional_page_handles_empty_marker():
    page = _page("<div class='heading--md'>Following</div><p>This character is not following anyone.</p>")
    payload = probe.parse_additional_page(page)
    assert payload["empty_message"] == "This character is not following anyone."
    assert payload["entries"] == []


_EXAMPLE_DIR = Path(__file__).resolve().parent.parent / "CharacterScraping" / "ExampleHTMLs"


@pytest.mark.skipif(not _EXAMPLE_DIR.exists(), reason="example HTML corpus not present")
@pytest.mark.parametrize(
    "filename, parser, key",
    [
        ("emotes.html", "parse_emote_page", "entries"),
        ("orchestration.html", "parse_orchestrion_page", "entries"),
        ("tripletriad.html", "parse_tripletriad_page", "entries"),
    ],
)
def test_parse_real_example_html(filename, parser, key):
    path = _EXAMPLE_DIR / filename
    if not path.exists():
        pytest.skip(f"{filename} not available")
    page = _page(path.read_text(encoding="utf-8", errors="ignore"))
    payload = getattr(probe, parser)(page)
    assert isinstance(payload.get(key), list)
    assert payload[key], f"{parser} extracted no {key} from real {filename}"
