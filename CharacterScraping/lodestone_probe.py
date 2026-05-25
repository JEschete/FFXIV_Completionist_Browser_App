from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup, Tag

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)

FIELD_LABELS = {
    "Race/Clan/Gender": "race_clan_gender",
    "Nameday": "nameday",
    "Guardian": "guardian",
    "City-state": "city_state",
    "Grand Company": "grand_company",
}

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = SCRIPT_DIR / "probe_output.json"
STANDARD_AUTH_PAGES = {
    "following": "following/",
    "blog": "blog/",
    "events": "event/",
    "emotes": "emote/",
    "currency": "currency/",
    "quests": "quest/",
    "orchestrion": "orchestrion/",
    "pvp": "pvp/",
    "blue_mage": "bluemage/",
    "trust": "trust/",
    "goldsaucer": "goldsaucer/",
    "tripletriad": "goldsaucer/tripletriad/",
}

FETCH_RETRIES = 3
FETCH_RETRY_DELAY_SECONDS = 1.2
AUTH_COOKIE_NAME_HINTS = (
    "session",
    "token",
    "auth",
    "sqex",
    "mog",
    "ldst",
    "cis",
)


@dataclass
class Page:
    url: str
    soup: BeautifulSoup


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def fetch(
    url: str,
    session: requests.Session,
    *,
    retries: int = FETCH_RETRIES,
    progress: Callable[[str], None] | None = None,
) -> Page:
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, timeout=30)
            response.raise_for_status()
            if progress:
                progress(
                    f"Fetched {url} -> {response.status_code} ({len(response.content)} bytes)"
                )
            return Page(
                url=response.url,
                soup=BeautifulSoup(response.content, "html.parser", from_encoding="utf-8"),
            )
        except requests.RequestException as exc:
            last_exc = exc
            if progress:
                progress(
                    f"Fetch error on {url} (attempt {attempt}/{retries}): {exc.__class__.__name__}: {exc}"
                )
            if progress and attempt < retries:
                progress(f"Retrying {url} after backoff")
            if attempt < retries:
                time.sleep(FETCH_RETRY_DELAY_SECONDS * attempt)
                continue
            raise
    assert last_exc is not None
    raise last_exc


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def text_of(node: Tag | None) -> str:
    if not node:
        return ""
    return clean(node.get_text(" ", strip=True))


def character_root_url(character_url: str) -> str:
    """Return canonical Lodestone character root URL.

    Accepts URLs that may point at subpages (e.g. /quest/) and normalizes them
    to /lodestone/character/<id>/ for stable relative URL joins.
    """
    stripped = (character_url or "").strip()
    if not stripped:
        return stripped

    parsed = urlsplit(stripped)
    path = parsed.path or ""
    m = re.search(r"(/lodestone/character/\d+/)", path)
    if not m:
        return stripped.rstrip("/") + "/"

    root_path = m.group(1)
    return urlunsplit((parsed.scheme, parsed.netloc, root_path, "", ""))


def extract_profile(page: Page) -> dict[str, Any]:
    soup = page.soup
    identity = {
        "title": text_of(soup.select_one(".frame__chara__title")) or None,
        "name": text_of(soup.select_one(".frame__chara__name")) or None,
        "world": None,
        "data_center": None,
    }
    world_text = text_of(soup.select_one(".frame__chara__world"))
    world_match = re.match(r"(?P<world>.+?)\s*\[(?P<dc>[^\]]+)\]$", world_text)
    if world_match:
        identity["world"] = clean(world_match.group("world"))
        identity["data_center"] = clean(world_match.group("dc"))

    display: dict[str, str] = {}
    for block in soup.select(".character__profile__data__detail .character-block"):
        title_nodes = block.select(".character-block__title")
        for title_node in title_nodes:
            label = text_of(title_node)
            key = FIELD_LABELS.get(label)
            if not key:
                continue
            value_node = title_node.find_next_sibling(["p", "h4"])
            value = text_of(value_node)
            if value:
                display[key] = value

    fc_link = soup.select_one(".character__freecompany__name h4 a[href*='/lodestone/freecompany/']")
    free_company = text_of(fc_link) or None
    free_company_id = None
    if fc_link and fc_link.get("href"):
        match = re.search(r"/freecompany/(\d+)/", fc_link["href"])
        free_company_id = match.group(1) if match else None

    avatar = None
    frame_face = soup.select_one(".frame__chara__face img[src]")
    if frame_face:
        avatar = frame_face.get("src")

    portrait = avatar
    for meta in soup.find_all("meta"):
        prop = meta.get("property") or meta.get("name")
        content = meta.get("content")
        if prop == "og:image" and content and "finalfantasyxiv.com" in content:
            portrait = content
            break

    return {
        "source_url": page.url,
        "identity": identity,
        "display_attributes": display,
        "free_company": {
            "name": free_company,
            "id": free_company_id,
        },
        "images": {
            "avatar": avatar,
            "portrait": portrait,
        },
    }


def _parse_int(value: str | None) -> int | None:
    """Parse a Lodestone numeric cell, returning None for dash placeholders ('-', '--')."""
    if not value:
        return None
    cleaned = value.replace(",", "").strip()
    if not cleaned or set(cleaned) <= {"-"}:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def parse_jobs(page: Page) -> dict[str, list[dict[str, Any]]]:
    sections: dict[str, list[dict[str, Any]]] = {}
    for heading_node in page.soup.select("h4.heading--lead"):
        job_list = heading_node.find_next_sibling("ul", class_="character__job")
        if not job_list:
            continue
        if not job_list.select_one(".character__job__name"):
            continue
        heading = text_of(heading_node)
        if not heading:
            continue
        rows: list[dict[str, Any]] = []
        for item in job_list.find_all("li", recursive=False):
            name = text_of(item.select_one(".character__job__name"))
            level_raw = text_of(item.select_one(".character__job__level"))
            exp_raw = text_of(item.select_one(".character__job__exp"))
            if not name:
                continue
            exp = None
            exp_max = None
            if "/" in exp_raw:
                left, right = [part.strip() for part in exp_raw.split("/", 1)]
                exp = _parse_int(left)
                exp_max = _parse_int(right)
            rows.append(
                {
                    "job": name,
                    "level": _parse_int(level_raw),
                    "xp": exp,
                    "xp_max": exp_max,
                    "tooltip": item.select_one(".character__job__name").get("data-tooltip"),
                }
            )
        sections[heading] = rows
    return sections


def parse_total(page: Page, label: str) -> int | None:
    total_node = page.soup.select_one(".minion__sort__total span")
    if total_node:
        total_text = text_of(total_node)
        if total_text.isdigit():
            return int(total_text)
    text = clean(page.soup.get_text(" ", strip=True))
    match = re.search(r"Total:\s+(\d+)", text)
    return int(match.group(1)) if match else None


def parse_pagination(page: Page) -> dict[str, Any]:
    current = None
    total = None
    current_label = page.soup.select_one(".btn__pager__current")
    if current_label:
        match = re.search(r"Page\s+(\d+)\s+of\s+(\d+)", text_of(current_label))
        if match:
            current = int(match.group(1))
            total = int(match.group(2))
    next_page = None
    prev_page = None
    page_links: list[str] = []
    for link in page.soup.find_all("a", href=True):
        href = urljoin(page.url, link["href"])
        if href.startswith(page.url.split("#", 1)[0]) and ("page=" in href or "/page/" in href):
            page_links.append(href)
    page_links = sorted(set(page_links))
    for href in page_links:
        parsed = urlparse(href)
        page_num = parse_qs(parsed.query).get("page", [None])[0]
        if page_num is not None and page_num.isdigit():
            if current is not None and int(page_num) == current + 1 and next_page is None:
                next_page = href
            if current is not None and int(page_num) == current - 1 and prev_page is None:
                prev_page = href
    return {
        "current": current,
        "total_pages": total,
        "page_links": page_links[:5],
        "next_page": next_page,
        "previous_page": prev_page,
    }


def parse_achievement_entries(page: Page) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for entry in page.soup.select("li.entry a.entry__achievement[href*='/achievement/detail/']"):
        href = entry.get("href")
        text = text_of(entry.select_one(".entry__activity__txt"))
        script_text = " ".join(script.get_text(" ", strip=True) for script in entry.select("script"))
        date = None
        epoch_match = re.search(r"ldst_strftime\((\d+),\s*'YMD'\)", script_text)
        if epoch_match:
            import datetime as dt
            date = dt.datetime.fromtimestamp(int(epoch_match.group(1)), dt.timezone.utc).strftime("%m/%d/%Y")
        hit = re.match(
            r"(?P<category>.+?)\s+achievement\s+\"(?P<title>.+)\"\s+earned!",
            text,
        )
        if hit:
            entries.append(
                {
                    "date": date,
                    "category": clean(hit.group("category")),
                    "title": clean(hit.group("title")),
                    "detail_url": urljoin(page.url, href or ""),
                }
            )
        else:
            entries.append(
                {
                    "date": date,
                    "raw": text,
                    "detail_url": urljoin(page.url, href or ""),
                }
            )
    return entries


def parse_achievements(
    page: Page,
    session: requests.Session,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    points_text = text_of(page.soup.select_one(".achievement__point"))
    points = int(points_text) if points_text.isdigit() else None
    total_text = text_of(page.soup.select_one(".parts__total"))
    total_match = re.search(r"(\d+)\s+Total", total_text)
    total = int(total_match.group(1)) if total_match else None
    pagination = parse_pagination(page)
    entries = parse_achievement_entries(page)
    total_pages = pagination["total_pages"] or 1
    page_errors: list[dict[str, Any]] = []

    for page_num in range(2, total_pages + 1):
        page_url = urljoin(page.url, f"?page={page_num}#anchor_achievement")
        try:
            extra_page = fetch(page_url, session, progress=progress)
        except requests.RequestException as exc:
            page_errors.append({"page": page_num, "url": page_url, "error": str(exc)})
            if progress:
                progress(f"Achievements page {page_num} failed; continuing: {exc}")
            continue
        entries.extend(parse_achievement_entries(extra_page))

    deduped: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for entry in entries:
        detail_url = str(entry.get("detail_url") or "")
        if not detail_url or detail_url in seen_urls:
            continue
        seen_urls.add(detail_url)
        deduped.append(entry)

    payload = {
        "points": points,
        "total": total,
        "pagination": pagination,
        "entries": deduped,
    }
    if page_errors:
        payload["errors"] = page_errors
    return payload


def parse_collection(page: Page, session: requests.Session, kind: str) -> dict[str, Any]:
    selector = ".minion__list_icon" if kind == "minion" else ".mount__list_icon"
    label_selector = ".minion__header__label" if kind == "minion" else ".mount__header__label"
    text_selector = ".minion__text" if kind == "minion" else ".mount__text"
    item_selector = ".minion__item_icon" if kind == "minion" else ".mount__item_icon"

    items: list[dict[str, Any]] = []
    for node in page.soup.select(selector):
        href = node.get("data-tooltip_href")
        if not href:
            continue
        tooltip_page = fetch(urljoin(page.url, href), session)
        label = text_of(tooltip_page.soup.select_one(label_selector))
        item_link = tooltip_page.soup.select_one(f"{item_selector}[href]")
        item_name = item_link.get("data-tooltip") if item_link else None
        items.append(
            {
                "name": label,
                "description": text_of(tooltip_page.soup.select_one(text_selector)),
                "tooltip_url": tooltip_page.url,
                "item_name": item_name,
            }
        )

    return {
        "total": parse_total(page, kind.title() + "s"),
        "pagination": parse_pagination(page),
        "items": items,
    }


def parse_additional_page(page: Page) -> dict[str, Any]:
    heading = None
    for selector in (".heading--md", ".heading--lead", "h3", "h4", "title"):
        node = page.soup.select_one(selector)
        text = text_of(node)
        if text:
            heading = text
            break

    empty_message = None
    full_text = clean(page.soup.get_text(" ", strip=True))
    for marker in (
        "No entries to display.",
        "This character is not following anyone.",
        "No results were found.",
    ):
        if marker in full_text:
            empty_message = marker
            break

    entries: list[dict[str, str | None]] = []
    seen_urls: set[str] = set()
    for anchor in page.soup.select("li.entry a[href], .entry a[href], .character__list a[href], .base__link a[href]"):
        href = urljoin(page.url, anchor.get("href", ""))
        label = text_of(anchor)
        if not href or href in seen_urls or not label:
            continue
        seen_urls.add(href)
        container = anchor.find_parent(["li", "div", "article"]) or anchor
        summary = None
        for selector in (".entry__activity__txt", ".entry__blog__text", ".entry__txt", ".txt"):
            node = container.select_one(selector)
            value = text_of(node)
            if value and value != label:
                summary = value
                break
        entries.append(
            {
                "label": label,
                "url": href,
                "summary": summary,
            }
        )
        if len(entries) >= 50:
            break

    key_values: list[dict[str, str]] = []
    for row in page.soup.select("table tr"):
        key = text_of(row.select_one("th"))
        value = text_of(row.select_one("td"))
        if key and value:
            key_values.append({"key": key, "value": value})

    for row in page.soup.select("dl"):
        terms = row.select("dt")
        values = row.select("dd")
        for term, value_node in zip(terms, values):
            key = text_of(term)
            value = text_of(value_node)
            if key and value:
                key_values.append({"key": key, "value": value})

    snippets: list[str] = []
    for node in page.soup.select(
        ".parts__zero, .parts__total, .character__reputation, .character__quest, "
        ".character__currency, .character__content__text, .character__pvp"
    ):
        text = text_of(node)
        if text:
            snippets.append(text)

    deduped_snippets: list[str] = []
    seen_snippets: set[str] = set()
    for snippet in snippets:
        if snippet in seen_snippets:
            continue
        seen_snippets.add(snippet)
        deduped_snippets.append(snippet)

    return {
        "url": page.url,
        "heading": heading,
        "empty_message": empty_message,
        "pagination": parse_pagination(page),
        "entries": entries,
        "key_values": key_values,
        "snippets": deduped_snippets,
    }


def parse_quest_page(page: Page) -> dict[str, Any]:
    payload = parse_additional_page(page)
    entries: list[dict[str, Any]] = []
    for item in page.soup.select("li.entry__quest"):
        container = item.select_one("div[href]") or item
        href = container.get("href") if isinstance(container, Tag) else None
        name_block = item.select_one(".entry__quest__name")
        title = None
        if name_block:
            text_parts = [clean(s) for s in name_block.stripped_strings if clean(s) not in {"-"}]
            if text_parts:
                title = text_parts[-1]
        script_text = " ".join(s.get_text(" ", strip=True) for s in item.select("script"))
        epoch_match = re.search(r"ldst_strftime\((\d+),\s*'YMD'\)", script_text)
        date = None
        if epoch_match:
            import datetime as dt
            date = dt.datetime.fromtimestamp(int(epoch_match.group(1)), dt.timezone.utc).strftime("%m/%d/%Y")
        if title:
            entries.append(
                {
                    "title": title,
                    "date": date,
                    "detail_url": urljoin(page.url, str(href or "")),
                }
            )

    if entries:
        payload["entries"] = entries
        payload["heading"] = payload.get("heading") or "Quest History"
    return payload


def detect_auth_markers(page: Page) -> dict[str, Any]:
    script_blob = "\n".join(script.get_text(" ", strip=True) for script in page.soup.select("script"))
    full_text = clean(page.soup.get_text(" ", strip=True))
    markers = {
        "ldst_is_loggedin_true": "ldst_is_loggedin = true" in script_blob,
        "mychara_marker": "mychara" in script_blob,
        "logout_ui": bool(page.soup.select_one(".link_logout, .bt_logout")),
        "login_prompt_visible": "Log In" in full_text and "Square Enix" in full_text,
    }
    markers["likely_authenticated"] = bool(
        markers["ldst_is_loggedin_true"]
        or markers["mychara_marker"]
        or markers["logout_ui"]
    )
    return markers


def summarize_session_cookies(session: requests.Session) -> dict[str, Any]:
    names: list[str] = []
    domains: set[str] = set()
    auth_like: list[str] = []
    for cookie in session.cookies:
        name = str(getattr(cookie, "name", "") or "")
        domain = str(getattr(cookie, "domain", "") or "")
        if not name:
            continue
        names.append(name)
        if domain:
            domains.add(domain)
        lower_name = name.lower()
        if any(hint in lower_name for hint in AUTH_COOKIE_NAME_HINTS):
            auth_like.append(name)

    return {
        "cookie_count": len(names),
        "domains": sorted(domains),
        "auth_like_cookie_names": sorted(set(auth_like)),
        "sample_cookie_names": sorted(set(names))[:20],
    }


def parse_tripletriad_page(page: Page) -> dict[str, Any]:
    payload = parse_additional_page(page)
    entries: list[dict[str, Any]] = []
    for item in page.soup.select("ul.tripletriad-card_list > li"):
        number = text_of(item.select_one(".num span"))
        name = text_of(item.select_one(".name_inner"))
        rarity = len(item.select("p.rarity img"))
        card_type = text_of(item.select_one("p.type")) or None
        item_link = item.select_one(".tripletriad-tooltip__item__text a[href]")
        card_image = item.select_one(".tripletriad-tooltip__card img.card")
        if not name:
            continue
        entries.append(
            {
                "number": int(number) if number.isdigit() else None,
                "name": name,
                "rarity": rarity if rarity > 0 else None,
                "type": card_type,
                "item_url": urljoin(page.url, str(item_link.get("href", ""))) if item_link else None,
                "item_name": text_of(item_link) if item_link else None,
                "card_image": card_image.get("src") if card_image else None,
            }
        )

    total_node = page.soup.select_one(".tripletriad-settings__total")
    total_text = text_of(total_node)
    total_match = re.search(
        r"Displaying\s+results\s*\(\s*([\d,]+)\s*out\s*of\s*([\d,]+)\s*cards\)",
        total_text,
        re.IGNORECASE,
    )
    if total_match:
        payload["totals"] = {
            "shown": int(total_match.group(1).replace(",", "")),
            "all_cards": int(total_match.group(2).replace(",", "")),
        }
    if entries:
        payload["entries"] = entries
        payload["heading"] = payload.get("heading") or "Triple Triad"
    return payload


def parse_currency_page(page: Page) -> dict[str, Any]:
    payload = parse_additional_page(page)
    groups: list[dict[str, Any]] = []
    for item in page.soup.select(".character__currency__list > li"):
        section = text_of(item.select_one(".heading--lead")) or None
        rows: list[dict[str, str]] = []
        for box in item.select(".currency__box"):
            label = text_of(box.select_one(".currency__box__text__name"))
            if not label:
                icon = box.select_one("img")
                label = clean(str(icon.get("data-tooltip", "") or "")) if icon else ""
            value = text_of(box.select_one(".currency__box__text"))
            value = value.replace(label, "", 1).strip() if label and value.startswith(label) else value
            if label or value:
                rows.append({"label": label or section or "", "value": value})
        if rows:
            groups.append({"section": section, "items": rows})

    if groups:
        payload["groups"] = groups
        payload["entries"] = []
        payload["heading"] = payload.get("heading") or "Currencies/Reputation"
    return payload


def parse_goldsaucer_page(page: Page) -> dict[str, Any]:
    payload = parse_additional_page(page)
    mgp = None
    mgp_label = None
    for node in page.soup.select("h3.heading--lead"):
        if "Manderville Gold Saucer Points" in text_of(node):
            mgp_label = node
            break
    if mgp_label:
        mgp_block = mgp_label.find_next("ul", class_="character__currency__list")
        if mgp_block:
            mgp_text = text_of(mgp_block.select_one("p"))
            mgp = int(mgp_text.replace(",", "")) if mgp_text.replace(",", "").isdigit() else None
    ticket_allowance = None
    ticket_node = page.soup.select_one(".character__goldsaucer__text__number")
    if ticket_node:
        ticket_allowance = text_of(ticket_node)

    if mgp is not None or ticket_allowance:
        payload["stats"] = {
            "mgp": mgp,
            "jumbo_cactpot_ticket_allowance": ticket_allowance,
        }
        payload["entries"] = []
        payload["heading"] = payload.get("heading") or "The Gold Saucer"
    return payload


def parse_bluemage_page(page: Page) -> dict[str, Any]:
    payload = parse_additional_page(page)
    entries: list[dict[str, Any]] = []
    learned_count = 0
    visible_count = 0

    for item in page.soup.select("li.bluemage-action__list__item"):
        classes = {str(c).strip().lower() for c in (item.get("class") or [])}
        name = text_of(item.select_one(".bluemage-action__name"))
        if not name:
            header = text_of(item.select_one(".bluemage-tooltip__header"))
            name = re.sub(r"^\s*No\.\s*\d+\s*", "", header).strip() if header else ""
        if not name:
            continue

        visible_count += 1
        learned = "sys-reward" in classes and "sys-no_reward" not in classes
        if learned:
            learned_count += 1

        num_text = text_of(item.select_one(".bluemage-action__index"))
        m = re.search(r"(\d+)", num_text)
        number = int(m.group(1)) if m else None

        entries.append(
            {
                "number": number,
                "name": name,
                "learned": learned,
            }
        )

    if entries:
        payload["entries"] = [
            entry
            for entry in entries
            if entry.get("learned") and str(entry.get("name") or "").strip(" ?")
        ]
        payload["totals"] = {
            "learned": learned_count,
            "visible": visible_count,
        }
        payload["heading"] = payload.get("heading") or "Blue Magic"
    return payload


def parse_emote_page(page: Page) -> dict[str, Any]:
    payload = parse_additional_page(page)
    entries: list[dict[str, Any]] = []

    for item in page.soup.select("ul.emote__list > li.js__btn_press"):
        name = text_of(item.select_one("p"))
        if not name:
            continue
        category = clean(str(item.get("data-category") or "")) or None
        entries.append(
            {
                "name": name,
                "category": category,
            }
        )

    if entries:
        payload["entries"] = entries
        payload["heading"] = payload.get("heading") or "Emotes"
    return payload


def parse_orchestrion_page(page: Page) -> dict[str, Any]:
    payload = parse_additional_page(page)
    entries: list[dict[str, Any]] = []
    acquired_count = 0
    visible_count = 0

    for item in page.soup.select("ul.orchestrion-list > li"):
        classes = {str(c).strip().lower() for c in (item.get("class") or [])}
        name = text_of(item.select_one(".orchestrion-list__name"))
        if not name:
            continue

        visible_count += 1
        acquired = "unacquired" not in classes
        if acquired:
            acquired_count += 1

        num_text = text_of(item.select_one(".orchestrion-list__num"))
        number = int(num_text) if num_text.isdigit() else None

        if acquired:
            entries.append(
                {
                    "number": number,
                    "name": name,
                }
            )

    if entries or visible_count:
        payload["entries"] = entries
        payload["totals"] = {
            "acquired": acquired_count,
            "visible": visible_count,
        }
        payload["heading"] = payload.get("heading") or "Orchestrion List"
    return payload


def parse_pvp_page(page: Page) -> dict[str, Any]:
    payload = parse_additional_page(page)
    if not page.soup.select_one(".character__pvp"):
        return payload

    def key_name(raw: str) -> str:
        key = clean(raw).lower()
        key = re.sub(r"[^a-z0-9]+", "_", key).strip("_")
        return key

    stats: dict[str, Any] = {}
    rank_title = text_of(page.soup.select_one(".character__pvp__rank__title"))
    if rank_title:
        stats["rank_title"] = rank_title
    for node in page.soup.select(".character__pvp__rank"):
        text = text_of(node)
        if ":" not in text:
            continue
        key, value = text.split(":", 1)
        key = key_name(key)
        if key and value.strip():
            stats[key] = value.strip()

    if stats:
        payload["stats"] = stats
    payload["entries"] = []
    payload["heading"] = payload.get("heading") or "PvP Profile"
    return payload


def parse_trust_page(page: Page) -> dict[str, Any]:
    payload = parse_additional_page(page)
    entries: list[dict[str, Any]] = []

    for item in page.soup.select("ul.trust__character > li"):
        name = text_of(item.select_one(".trust__character__name"))
        if not name:
            continue

        level_text = text_of(item.select_one(".trust__level"))
        level_match = re.search(r"(\d+)", level_text)
        level = int(level_match.group(1)) if level_match else None
        exp = text_of(item.select_one(".trust__data__exp")) or None
        next_level = text_of(item.select_one(".trust__data__next")) or None

        entries.append(
            {
                "name": name,
                "level": level,
                "exp": exp,
                "next": next_level,
            }
        )

    if entries:
        payload["entries"] = entries
        payload["heading"] = payload.get("heading") or "Trust"
    return payload


def parse_authenticated_page_by_path(page: Page, path: str) -> dict[str, Any]:
    normalized = path.strip().strip("/").lower()
    if normalized == "quest":
        return parse_quest_page(page)
    if normalized == "goldsaucer/tripletriad":
        return parse_tripletriad_page(page)
    if normalized == "currency":
        return parse_currency_page(page)
    if normalized == "goldsaucer":
        return parse_goldsaucer_page(page)
    if normalized == "bluemage":
        return parse_bluemage_page(page)
    if normalized == "emote":
        return parse_emote_page(page)
    if normalized == "orchestrion":
        return parse_orchestrion_page(page)
    if normalized == "pvp":
        return parse_pvp_page(page)
    if normalized == "trust":
        return parse_trust_page(page)
    return parse_additional_page(page)


def build_paginated_url(first_url: str, path: str, page_num: int) -> str:
    """Build a deterministic page URL when Lodestone doesn't emit a clean
    next-page link for some authenticated views."""
    normalized = path.strip().strip("/").lower()
    parts = urlsplit(first_url)
    query = parse_qs(parts.query, keep_blank_values=True)
    query["page"] = [str(page_num)]

    if normalized == "goldsaucer/tripletriad":
        query.setdefault("hold", [""])
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), ""))

    if normalized == "quest":
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), "anchor_quest"))

    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), parts.fragment))


def collect_paginated_additional_page(
    base: str,
    path: str,
    session: requests.Session,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    def log(msg: str) -> None:
        if progress:
            progress(msg)

    log(f"Fetching {path} page 1")
    first_page = fetch(urljoin(base, path + "/"), session, progress=progress)
    auth_markers = detect_auth_markers(first_page)
    log(f"Auth markers on {path} first page: {auth_markers}")
    first_payload = parse_authenticated_page_by_path(first_page, path)
    first_payload["auth_markers"] = auth_markers

    pages: list[dict[str, Any]] = [first_payload]
    seen_page_urls: set[str] = {first_page.url}

    total_pages_reported = first_payload.get("pagination", {}).get("total_pages")
    if isinstance(total_pages_reported, int) and total_pages_reported > 1:
        for page_num in range(2, total_pages_reported + 1):
            page_url = build_paginated_url(first_page.url, path, page_num)
            if page_url in seen_page_urls:
                continue
            seen_page_urls.add(page_url)
            log(f"Fetching {path} page {len(pages) + 1}")
            next_page = fetch(page_url, session, progress=progress)
            next_payload = parse_authenticated_page_by_path(next_page, path)
            pages.append(next_payload)
    else:
        next_url = first_payload.get("pagination", {}).get("next_page")
        while isinstance(next_url, str) and next_url and next_url not in seen_page_urls:
            seen_page_urls.add(next_url)
            log(f"Fetching {path} page {len(pages) + 1}")
            next_page = fetch(next_url, session, progress=progress)
            next_payload = parse_authenticated_page_by_path(next_page, path)
            pages.append(next_payload)
            next_url = next_payload.get("pagination", {}).get("next_page")

    combined_entries: list[dict[str, str | None]] = []
    seen_entry_keys: set[str] = set()
    for page_payload in pages:
        for entry in page_payload.get("entries", []):
            if not isinstance(entry, dict):
                continue
            dedupe_key = (
                str(entry.get("detail_url") or "")
                or str(entry.get("url") or "")
                or str(entry.get("item_url") or "")
                or (
                    f"{entry.get('number')}|{entry.get('name')}"
                    if entry.get("number") is not None or entry.get("name")
                    else ""
                )
                or str(entry.get("title") or "")
                or str(entry.get("label") or "")
                or json.dumps(entry, ensure_ascii=False, sort_keys=True)
            )
            if dedupe_key in seen_entry_keys:
                continue
            seen_entry_keys.add(dedupe_key)
            combined_entries.append(entry)

    combined_kv: list[dict[str, str]] = []
    seen_kv: set[tuple[str, str]] = set()
    for page_payload in pages:
        for row in page_payload.get("key_values", []):
            if not isinstance(row, dict):
                continue
            key = str(row.get("key") or "")
            value = str(row.get("value") or "")
            dedupe_key = (key, value)
            if not key or not value or dedupe_key in seen_kv:
                continue
            seen_kv.add(dedupe_key)
            combined_kv.append({"key": key, "value": value})

    combined_snippets: list[str] = []
    seen_snippets: set[str] = set()
    for page_payload in pages:
        for snippet in page_payload.get("snippets", []):
            text = str(snippet or "").strip()
            if not text or text in seen_snippets:
                continue
            seen_snippets.add(text)
            combined_snippets.append(text)

    total_pages_reported = pages[0].get("pagination", {}).get("total_pages") if pages else None

    return {
        "path": path,
        "url": first_payload.get("url"),
        "heading": first_payload.get("heading"),
        "empty_message": first_payload.get("empty_message"),
        "pagination": {
            "total_pages_reported": total_pages_reported,
            "pages_collected": len(pages),
        },
        "entries": combined_entries,
        "key_values": combined_kv,
        "snippets": combined_snippets,
        "pages": [
            {
                "url": page_payload.get("url"),
                "heading": page_payload.get("heading"),
                "empty_message": page_payload.get("empty_message"),
                "entry_count": len(page_payload.get("entries", [])),
                "key_value_count": len(page_payload.get("key_values", [])),
                "snippet_count": len(page_payload.get("snippets", [])),
            }
            for page_payload in pages
        ],
    }


def collect_authenticated_pages(
    character_url: str,
    session: requests.Session,
    extra_paths: list[str],
    progress: Callable[[str], None] | None = None,
) -> dict[str, dict[str, Any]]:
    base = character_root_url(character_url)
    out: dict[str, dict[str, Any]] = {}

    def log(msg: str) -> None:
        if progress:
            progress(msg)

    for raw_path in extra_paths:
        path = raw_path.strip().strip("/")
        if not path:
            continue
        try:
            out[path] = collect_paginated_additional_page(base, path, session, progress=progress)
            result = out[path]
            entries_count = len(result.get("entries", [])) if isinstance(result, dict) else 0
            log(f"Collected {path}: {entries_count} entries")
        except requests.RequestException as exc:
            response = getattr(exc, "response", None)
            status_code = response.status_code if response is not None else None
            log(f"Error on {path}: {exc}")
            out[path] = {
                "path": path,
                "error": str(exc),
                "status_code": status_code,
            }
    return out


def collect_character(
    character_url: str,
    session: requests.Session | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    base = character_root_url(character_url)
    owns_session = session is None
    active_session = session or build_session()
    def log(msg: str) -> None:
        if progress:
            progress(msg)
    try:
        log("Fetching profile page")
        profile = fetch(base, active_session, progress=progress)
        profile_auth = detect_auth_markers(profile)
        log(f"Profile auth markers: {profile_auth}")

        log("Fetching class/job page")
        class_job = fetch(urljoin(base, "class_job/"), active_session, progress=progress)
        log("Fetching minion page")
        minion = fetch(urljoin(base, "minion/"), active_session, progress=progress)
        log("Fetching mount page")
        mount = fetch(urljoin(base, "mount/"), active_session, progress=progress)
        log("Fetching achievement page")
        achievement = fetch(urljoin(base, "achievement/"), active_session, progress=progress)
        return {
            "profile": extract_profile(profile),
            "auth_diagnostics": {
                "profile_auth_markers": profile_auth,
            },
            "class_job": parse_jobs(class_job),
            "minions": parse_collection(minion, active_session, "minion"),
            "mounts": parse_collection(mount, active_session, "mount"),
            "achievements": parse_achievements(achievement, active_session, progress=progress),
        }
    finally:
        if owns_session:
            active_session.close()


def ensure_browser_cookie3_available() -> bool:
    try:
        import browser_cookie3  # noqa: F401
    except ImportError:
        print("browser-cookie3 is not installed in the project virtual environment.")
        print("Install it with:")
        print(f"  {sys.executable} -m pip install browser-cookie3")
        return False
    return True


def session_from_installed_browser_cookies(
    browser_name: str,
    progress: Callable[[str], None] | None = None,
) -> requests.Session:
    import browser_cookie3

    loaders: dict[str, Any] = {
        "edge": browser_cookie3.edge,
        "chrome": browser_cookie3.chrome,
        "firefox": browser_cookie3.firefox,
    }
    loader = loaders.get(browser_name)
    if loader is None:
        raise ValueError(f"Unsupported browser cookie source: {browser_name}")

    cookie_jar = loader(domain_name="finalfantasyxiv.com")
    session = build_session()
    imported = 0
    for cookie in cookie_jar:
        domain = getattr(cookie, "domain", None)
        if isinstance(domain, str) and "finalfantasyxiv.com" not in domain:
            continue
        session.cookies.set(
            cookie.name,
            cookie.value,
            domain=domain,
            path=getattr(cookie, "path", "/") or "/",
        )
        imported += 1

    if imported == 0:
        session.close()
        raise RuntimeError(
            f"No Lodestone cookies found in {browser_name}. "
            "Log into Lodestone in that browser first, then retry."
        )

    if progress:
        summary = summarize_session_cookies(session)
        progress(
            "Cookie import diagnostics: "
            f"count={summary['cookie_count']}, "
            f"domains={summary['domains']}, "
            f"auth_like_cookie_names={summary['auth_like_cookie_names']}"
        )
    return session


def prompt_choice(prompt: str, valid: dict[str, str]) -> str:
    while True:
        choice = input(prompt).strip().lower()
        if choice in valid:
            return choice
        print("Please choose one of:", ", ".join(valid))


def prompt_text(prompt: str, default: str | None = None) -> str:
    while True:
        suffix = f" [{default}]" if default else ""
        value = input(f"{prompt}{suffix}: ").strip()
        if value:
            return value
        if default is not None:
            return default
        print("A value is required.")


def prompt_output_path(default_name: str) -> Path:
    value = prompt_text("Output file", default_name)
    path = Path(value)
    if not path.is_absolute():
        path = SCRIPT_DIR / path
    return path


def character_output_stem(payload: dict[str, Any]) -> str:
    profile = payload.get("profile") if isinstance(payload, dict) else None
    identity = profile.get("identity") if isinstance(profile, dict) else None
    name = identity.get("name") if isinstance(identity, dict) else None
    if not isinstance(name, str) or not name.strip():
        return DEFAULT_OUTPUT.stem
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", name.strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned or DEFAULT_OUTPUT.stem


def prompt_additional_paths() -> list[str]:
    print()
    print("Authenticated page presets:")
    for key, path in STANDARD_AUTH_PAGES.items():
        print(f"  - {key}: {path}")
    include_standard = prompt_choice(
        "Include standard social pages? [y/n]: ",
        {"y": "yes", "n": "no"},
    )
    extras: list[str] = list(STANDARD_AUTH_PAGES.values()) if include_standard == "y" else []
    deduped: list[str] = []
    seen: set[str] = set()
    for item in extras:
        normalized = item.strip().strip("/")
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


def save_payload(payload: dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Saved: {output_path}")


def run_public_scrape() -> None:
    print()
    url = prompt_text("Character profile URL")
    data = collect_character(url)
    output_path = prompt_output_path(f"{character_output_stem(data)}.json")
    save_payload(data, output_path)


def run_authenticated_scrape() -> None:
    print()
    url = prompt_text("Character profile URL")
    extra_paths = prompt_additional_paths()

    print()
    if not ensure_browser_cookie3_available():
        return

    source_key = prompt_choice(
        "Cookie source browser [e/c/f]: ",
        {"e": "edge", "c": "chrome", "f": "firefox"},
    )
    source_map = {"e": "edge", "c": "chrome", "f": "firefox"}
    source_browser = source_map[source_key]

    try:
        session = session_from_installed_browser_cookies(source_browser)
    except Exception as exc:
        print(f"Cookie import failed: {exc}")
        return

    auth_info: dict[str, Any] = {
        "method": "installed_browser_cookies",
        "source_browser": source_browser,
    }
    print(f"Imported Lodestone cookies from {source_browser}.")

    try:
        data = collect_character(url, session=session)
        data["authenticated_pages"] = collect_authenticated_pages(url, session, extra_paths)
        auth_info["extra_paths"] = extra_paths
        data["auth"] = auth_info
        output_path = prompt_output_path(f"{character_output_stem(data)}_authenticated.json")
        save_payload(data, output_path)
    finally:
        session.close()


def menu_loop() -> None:
    options = {
        "1": run_public_scrape,
        "2": run_authenticated_scrape,
        "3": None,
    }
    while True:
        print()
        print("FFXIV Lodestone Scraper")
        print("1. Public scrape")
        print("2. Authenticated scrape")
        print("3. Exit")
        choice = prompt_choice("Select an option: ", {key: key for key in options})
        if choice == "3":
            return
        handler = options[choice]
        assert handler is not None
        handler()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    menu_loop()


if __name__ == "__main__":
    main()
