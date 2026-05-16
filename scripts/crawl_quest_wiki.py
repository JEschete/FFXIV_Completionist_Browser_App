#!/usr/bin/env python3
"""Crawl the FFXIV Console Games Wiki and cache its pages locally.

Two modes:

* ``--mode allpages`` (recommended for whole-site mirroring) uses the
  MediaWiki API's ``list=allpages`` enumeration. Completeness is guaranteed by
  the server, memory is O(1), and the run is fully resumable — every page
  goes into a content-addressed bucket on disk and subsequent runs skip what
  they already have.
* ``--mode bfs`` (default) does a breadth-first walk from a seed page,
  honoring ``--depth``. Better for targeted exploration ("everything within
  two hops of /wiki/Quest").

After fetching, ``parse_quest_page`` mines the local cache for infobox data
and emits ``GameDataReferences/quests.jsonl``; ``build_graph`` then resolves
the ``Required Quest`` / ``Next Quest`` references into ``chains.json``.

USAGE
    # whole site, default 1.2s/request:
    python scripts/crawl_quest_wiki.py --mode allpages

    # targeted seed, depth 2:
    python scripts/crawl_quest_wiki.py --mode bfs --seed https://.../wiki/Quest

    # use a locally saved HTML page as the seed (zero remote fetches):
    python scripts/crawl_quest_wiki.py --mode bfs --seed-file GameDataReferences/MSQList.html

    # bounded test:
    python scripts/crawl_quest_wiki.py --mode allpages --limit 30

    # parse-only / graph-only (no network):
    python scripts/crawl_quest_wiki.py --parse-only
    python scripts/crawl_quest_wiki.py --build-graph-only

POLITENESS
* Sequential client, configurable delay (default 1.2s/request between LIVE
  fetches; cache hits are free and uncounted).
* Descriptive User-Agent.
* ``robots.txt`` check on startup; aborts if disallowed for our UA.
* Retries (3) with exponential backoff on transient failures.

CACHE LAYOUT
``GameDataReferences/cache/<first-letter>/<page>.html`` — distributes thousands
of files across 27 buckets so the OS / file explorer stays usable. Files
saved by the old flat layout are migrated to buckets automatically on startup.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator
from urllib.parse import quote, unquote, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup, Tag

# --- configuration ----------------------------------------------------------

WIKI = "https://ffxiv.consolegameswiki.com"
# Candidates probed in order at startup; first one that answers a siteinfo
# query gets used. Different MediaWiki installs put api.php in different
# places, and some disable it entirely (we fall back to Special:AllPages
# HTML scraping in that case).
API_CANDIDATES = (
    f"{WIKI}/mediawiki/api.php",     # consolegameswiki: install root is /mediawiki/
    f"{WIKI}/api.php",
    f"{WIKI}/w/api.php",
    f"{WIKI}/wiki/api.php",
)
DEFAULT_SEED = f"{WIKI}/wiki/Quest"
SPECIAL_ALLPAGES = f"{WIKI}/wiki/Special:AllPages"
# Use index.php with a large limit to grab every category in one response —
# avoids paginating through ~10 chunks of the default Special:Categories view.
SPECIAL_CATEGORIES = (
    f"{WIKI}/mediawiki/index.php?title=Special:Categories&offset=&limit=6000"
)

USER_AGENT = (
    "FFXIVCompletionTrackerBot/0.2 "
    "(+https://github.com/JEschete/FFXIV_Complitionist_Browser_App; "
    "research crawler; respects robots.txt; rate-limited)"
)

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "GameDataReferences"
CACHE_DIR = OUT_DIR / "cache"
DATA_FILE = OUT_DIR / "quests.jsonl"
GRAPH_FILE = OUT_DIR / "chains.json"
SEEN_FILE = OUT_DIR / "_seen.txt"
CATEGORIES_FILE = OUT_DIR / "categories.json"
# Streaming checkpoints written during targeted-mode enumeration. Surviving a
# Ctrl-C or rate-limit means the next run resumes without re-enumerating
# categories that were already fully walked.
TARGETED_TITLES_FILE = OUT_DIR / "_targeted_titles.txt"
TARGETED_CATS_DONE_FILE = OUT_DIR / "_targeted_cats_done.txt"

# When the wiki rate-limits us (HTTP 403/429), wait progressively longer
# before retrying. After the last entry is exhausted on a single API call,
# the iterator raises RateLimitedError so the outer loop can save state and
# exit cleanly instead of churning through "+0 titles" for hundreds of cats.
RATE_LIMIT_BACKOFFS_SEC = (30, 120, 600)  # 30s, 2min, 10min


class RateLimitedError(Exception):
    """Raised when the wiki has rate-limited us persistently (403/429 chain)."""

SKIP_NS = (
    "File:", "Image:", "Template:", "User:", "User_talk:", "Talk:",
    "Special:", "Help:", "Category_talk:", "Module:", "MediaWiki:",
    "FFXIVCGW:", "Forum:",
)

CHAIN_FIELDS = {
    "required quest": "required_quest",
    "required": "required_quest",
    "prerequisite": "required_quest",
    "prerequisite quest": "required_quest",
    "previous quest": "required_quest",
    "next quest": "next_quest",
    "followed by": "next_quest",
    "unlocks": "unlocks",
    "unlocked by": "required_quest",
    "leads to": "next_quest",
}

INFO_FIELDS = {
    "level": "level",
    "patch": "patch",
    "expansion": "expansion",
    "type": "type",
    "quest type": "type",
    "starting npc": "npc",
    "npc": "npc",
    "location": "location",
    "starting location": "location",
    "issuing npc": "npc",
    "experience": "experience",
    "gil": "gil",
    "reward": "reward",
    "rewards": "reward",
    "source": "source",
}

# When discover_categories runs for the first time, these category names get
# crawl=True by default so the user doesn't have to flip 1000+ flags by hand.
# Match is case-insensitive against the category name (without the "Category:"
# prefix). Everything not listed here defaults to crawl=False.
DEFAULT_CRAWL_CATEGORIES = frozenset(s.lower() for s in {
    "Mounts", "Minions", "Achievements", "Emotes", "Titles",
    "Hairstyles", "Bardings", "Fashion Accessories", "Facewear",
    "Triple Triad Cards", "Triple Triad Opponents",
    "Orchestrion Rolls",
    "Quests", "Sidequests", "Main Scenario Quests",
    "Job Quests", "Class Quests", "Role Quests",
    "Beast Tribe Quests", "Tribal Quests", "Allied Society Quests",
    "Seasonal Quests", "Hildibrand Quests", "Custom Deliveries",
    "Dungeons", "Trials", "Raids", "Alliance Raids", "Variant Dungeons",
    "FATEs", "Hunts", "Notorious Monsters",
})


def _preset_crawl(category_name: str) -> bool:
    return category_name.strip().lower() in DEFAULT_CRAWL_CATEGORIES


# --- progress reporting -----------------------------------------------------

@dataclass
class CrawlStats:
    queued: int = 0          # for BFS; for allpages we update as we learn the total
    processed: int = 0
    cache_hits: int = 0
    fetched: int = 0
    failed: int = 0
    started_at: float = field(default_factory=time.monotonic)
    fetch_times: list[float] = field(default_factory=list)  # rolling, last ~50

    def push_fetch_time(self, dt: float) -> None:
        self.fetch_times.append(dt)
        if len(self.fetch_times) > 50:
            self.fetch_times.pop(0)

    def avg_fetch(self) -> float:
        return sum(self.fetch_times) / len(self.fetch_times) if self.fetch_times else 0.0

    def eta_str(self, delay: float) -> str:
        remaining = max(self.queued - self.processed, 0)
        if not remaining:
            return ""
        per_req = max(self.avg_fetch(), delay)
        secs = int(remaining * per_req)
        h, secs = divmod(secs, 3600)
        m, s = divmod(secs, 60)
        return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def progress_line(stats: CrawlStats, kind: str, slug: str, size_kb: float | None,
                  delay: float, extra: str = "") -> str:
    if stats.queued:
        pct = stats.processed / stats.queued * 100
        head = f"[{stats.processed:>5}/{stats.queued:>5}] {pct:5.1f}%"
    else:
        head = f"[{stats.processed:>5}        ]      "
    size = f"{size_kb:6.1f}KB" if size_kb is not None else "       "
    rate = f"avg {stats.avg_fetch():4.2f}s"
    eta = stats.eta_str(delay)
    eta_str = f" ETA {eta}" if eta else ""
    return f"{head}  {kind:<3}  {slug:<58}  {size}  {rate}{eta_str}  {extra}"


# --- politeness layer -------------------------------------------------------

class PoliteFetcher:
    def __init__(self, delay: float, cache_dir: Path, stats: CrawlStats,
                 quiet: bool = False):
        self.delay = delay
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.stats = stats
        self.quiet = quiet
        self._last_request = 0.0
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=30.0,
        )

    def cache_path(self, key: str) -> Path:
        """Hierarchical cache path: cache/<bucket>/<safe-name>.html.

        ``key`` may be a URL or a wiki page title; we extract a sensible slug
        and bucket by its first alphanumeric character (lowercased) so the
        directory stays browseable at scale.
        """
        slug = re.sub(r"/wiki/", "", urlsplit(key).path) if "/" in key else key
        slug = unquote(slug).replace("/", "_")
        slug = re.sub(r"[<>:\"|?*]", "_", slug)[:180] or "_root"
        first = slug[:1].lower()
        bucket = first if first.isalnum() else "_"
        return self.cache_dir / bucket / (slug + ".html")

    def has(self, url_or_title: str) -> bool:
        return self.cache_path(url_or_title).exists()

    def get(self, url: str, retries: int = 3) -> tuple[str | None, bool]:
        """Return (html, was_cache_hit). Live fetches honor the delay and
        retry transient errors with exponential backoff."""
        cached = self.cache_path(url)
        if cached.exists():
            self.stats.cache_hits += 1
            return cached.read_text(encoding="utf-8", errors="replace"), True

        # rate limit only applies to *live* requests
        wait = self.delay - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)

        backoff = 1.0
        for attempt in range(1, retries + 1):
            t0 = time.monotonic()
            try:
                r = self._client.get(url)
                self._last_request = time.monotonic()
                self.stats.push_fetch_time(self._last_request - t0)
                r.raise_for_status()
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_text(r.text, encoding="utf-8")
                self.stats.fetched += 1
                return r.text, False
            except httpx.HTTPError as exc:
                if attempt == retries:
                    self.stats.failed += 1
                    if not self.quiet:
                        print(f"  ! fetch failed after {retries} tries "
                              f"{url}: {exc}", file=sys.stderr)
                    return None, False
                if not self.quiet:
                    print(f"  ~ retry {attempt}/{retries} after {backoff:.1f}s "
                          f"({type(exc).__name__})", file=sys.stderr)
                time.sleep(backoff)
                backoff *= 2

    def close(self) -> None:
        self._client.close()


# --- enumeration ------------------------------------------------------------

def discover_api_url(client: httpx.Client, delay: float) -> str | None:
    """Probe each ``API_CANDIDATES`` URL with a tiny siteinfo query; return
    the first one that responds with valid JSON. Returns None if all fail —
    in that case the caller falls back to HTML enumeration."""
    last = 0.0
    for url in API_CANDIDATES:
        wait = delay - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        try:
            r = client.get(url, params={
                "action": "query", "meta": "siteinfo", "format": "json",
            })
            last = time.monotonic()
            if r.is_success and r.headers.get("content-type", "").startswith("application/json"):
                _ = r.json()  # sanity: it really is JSON
                print(f"  API discovered at {url}")
                return url
            print(f"  ! API probe failed at {url} (HTTP {r.status_code})")
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            print(f"  ! API probe failed at {url}: {type(exc).__name__}")
    return None


def allpages_iter_api(client: httpx.Client, api_url: str, delay: float,
                      namespace: int = 0) -> Iterator[str]:
    """Yield every page title in ``namespace`` via the MediaWiki API.
    Paginates with the ``apcontinue`` token. Honors the polite delay."""
    params: dict[str, str | int] = {
        "action": "query",
        "list": "allpages",
        "aplimit": 500,
        "apnamespace": namespace,
        "format": "json",
    }
    last = 0.0
    while True:
        wait = delay - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        r = client.get(api_url, params=params)
        last = time.monotonic()
        r.raise_for_status()
        payload = r.json()
        for entry in payload.get("query", {}).get("allpages", []):
            yield entry["title"]
        cont = payload.get("continue")
        if not cont:
            return
        params.update(cont)


def allpages_iter_html(client: httpx.Client, delay: float) -> Iterator[str]:
    """Fallback: scrape ``Special:AllPages`` HTML and yield page titles.

    MediaWiki's Special:AllPages renders an alphabetical index with a
    pagination control. We follow the "Next page" link until exhaustion,
    printing per-index-page progress so the user knows enumeration is alive.
    """
    url: str | None = SPECIAL_ALLPAGES
    last = 0.0
    seen_urls: set[str] = set()
    pages_visited = 0
    titles_so_far = 0
    while url and url not in seen_urls:
        seen_urls.add(url)
        wait = delay - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        try:
            r = client.get(url)
        except httpx.HTTPError as exc:
            print(f"  ! Special:AllPages fetch failed: {exc}", file=sys.stderr)
            return
        last = time.monotonic()
        if not r.is_success:
            print(f"  ! Special:AllPages returned HTTP {r.status_code} at {url}",
                  file=sys.stderr)
            return
        soup = BeautifulSoup(r.text, "html.parser")

        # the alphabetical index of pages — try the modern class first,
        # then the legacy table layout some installs still use
        body = (
            soup.select_one(".mw-allpages-body")
            or soup.select_one("table.mw-allpages-table-chunk")
            or soup.select_one("#mw-content-text")
        )
        new_titles = 0
        if body is not None:
            for a in body.find_all("a", href=True):
                raw = a.get("href")
                href = (raw[0] if isinstance(raw, list) and raw
                        else str(raw) if raw else "")
                if not is_internal_wiki_link(href):
                    continue
                title = unquote(href[len("/wiki/"):]).replace("_", " ").split("#", 1)[0]
                if title:
                    new_titles += 1
                    titles_so_far += 1
                    yield title

        # follow "Next page (Foo)" link if present. Different MW versions
        # use different containers / link wording, so cast a wide net.
        next_url: str | None = None
        nav_candidates: list[Tag] = []
        for sel in (".mw-allpages-nav", ".mw-allpages-chunk-numbers",
                    ".allpages-prevnext"):
            nav_candidates.extend(soup.select(sel))
        for nav in nav_candidates:
            for a in nav.find_all("a", href=True):
                txt = a.get_text(" ", strip=True).lower()
                if txt.startswith("next page") or txt.startswith("next"):
                    raw = a.get("href")
                    href = (raw[0] if isinstance(raw, list) and raw
                            else str(raw) if raw else "")
                    if href:
                        next_url = absolute(href)
                        break
            if next_url:
                break

        pages_visited += 1
        cont_label = "(continuing)" if next_url else "(end of index)"
        print(f"    index page {pages_visited:>4}: +{new_titles} titles "
              f"-> {titles_so_far} total  {cont_label}")
        if new_titles == 0 and body is not None:
            print("    ! 0 titles parsed from this page — the wiki may use "
                  "non-standard markup; check selectors in allpages_iter_html",
                  file=sys.stderr)

        url = next_url


def title_to_url(title: str) -> str:
    return f"{WIKI}/wiki/{quote(title.replace(' ', '_'), safe=':/_()%')}"


# --- category-member enumeration -------------------------------------------

def _get_json_with_backoff(client: httpx.Client, url: str, *,
                            params: dict | None = None, label: str = "(request)"
                            ) -> dict | None:
    """GET ``url`` and return parsed JSON. On HTTP 403/429 ("you've been
    punched"), sleep per the RATE_LIMIT_BACKOFFS_SEC schedule and retry.
    Honors a ``Retry-After`` header if present. Returns None for non-
    rate-limit failures; raises RateLimitedError if retries are exhausted
    on rate-limit responses."""
    for attempt in range(len(RATE_LIMIT_BACKOFFS_SEC) + 1):
        try:
            r = client.get(url, params=params)
        except httpx.HTTPError as exc:
            print(f"  ! {label}: {type(exc).__name__}: {exc}", file=sys.stderr)
            return None
        if r.status_code in (403, 429):
            ra = r.headers.get("retry-after")
            wait: int | None = None
            if ra and ra.strip().isdigit():
                wait = int(ra.strip())
            elif attempt < len(RATE_LIMIT_BACKOFFS_SEC):
                wait = RATE_LIMIT_BACKOFFS_SEC[attempt]
            if wait is None:
                raise RateLimitedError(
                    f"HTTP {r.status_code} on {label} — exhausted "
                    f"{len(RATE_LIMIT_BACKOFFS_SEC)} backoff retries"
                )
            print(f"  ! HTTP {r.status_code} on {label}; sleeping {wait}s "
                  f"(retry {attempt + 1}/{len(RATE_LIMIT_BACKOFFS_SEC)})...",
                  file=sys.stderr)
            time.sleep(wait)
            continue
        if not r.is_success:
            print(f"  ! HTTP {r.status_code} on {label}", file=sys.stderr)
            return None
        try:
            return r.json()
        except json.JSONDecodeError as exc:
            print(f"  ! invalid JSON from {label}: {exc}", file=sys.stderr)
            return None
    return None  # unreachable but mollifies the type checker


def categorymembers_iter_api(client: httpx.Client, api_url: str,
                              category_title: str, delay: float
                              ) -> Iterator[tuple[str, int]]:
    """Yield ``(member_title, namespace)`` for every page in a category via
    the MediaWiki API. ``category_title`` may include or omit the "Category:"
    prefix. Paginates with the ``cmcontinue`` token.

    Namespace 0 = content page, 14 = subcategory; callers decide what to do
    with each. Raises ``RateLimitedError`` if 403/429 persists past all
    backoff retries, so the outer enumerator can save state and bail."""
    if not category_title.lower().startswith("category:"):
        category_title = f"Category:{category_title}"
    params: dict[str, str | int] = {
        "action": "query",
        "list": "categorymembers",
        "cmtitle": category_title,
        "cmlimit": 500,
        "cmprop": "title|ns",
        "format": "json",
    }
    last = 0.0
    while True:
        wait = delay - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        payload = _get_json_with_backoff(
            client, api_url, params=params,
            label=f"categorymembers {category_title}",
        )
        last = time.monotonic()
        if payload is None:
            return
        for entry in payload.get("query", {}).get("categorymembers", []):
            yield entry["title"], int(entry.get("ns", 0))
        cont = payload.get("continue")
        if not cont:
            return
        params.update(cont)


def categorymembers_iter_html(client: httpx.Client, category_title: str,
                              delay: float) -> Iterator[tuple[str, int]]:
    """HTML fallback: scrape ``/wiki/Category:Foo`` (and its pagination)
    yielding the same ``(title, namespace)`` shape as the API variant."""
    if not category_title.lower().startswith("category:"):
        category_title = f"Category:{category_title}"
    url: str | None = title_to_url(category_title)
    visited: set[str] = set()
    last = 0.0
    while url and url not in visited:
        visited.add(url)
        wait = delay - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        try:
            r = client.get(url)
        except httpx.HTTPError as exc:
            print(f"  ! category page fetch failed: {exc}", file=sys.stderr)
            return
        last = time.monotonic()
        if not r.is_success:
            print(f"  ! HTTP {r.status_code} at {url}", file=sys.stderr)
            return
        soup = BeautifulSoup(r.text, "html.parser")

        def harvest(div: Tag | None, ns: int) -> None:
            if div is None:
                return
            for a in div.find_all("a", href=True):
                raw = a.get("href")
                href = (raw[0] if isinstance(raw, list) and raw
                        else str(raw) if raw else "")
                if not is_internal_wiki_link(href):
                    continue
                page = unquote(href[len("/wiki/"):]).replace("_", " ").split("#", 1)[0]
                if not page:
                    continue
                # for the subcategory list, keep only Category:* entries; for
                # the page list, drop them
                if ns == 14 and not page.lower().startswith("category:"):
                    continue
                if ns == 0 and page.lower().startswith("category:"):
                    continue
                yield_title = page
                # MediaWiki ns=0 titles don't carry a prefix; ns=14 titles do.
                yield_results.append((yield_title, ns))

        yield_results: list[tuple[str, int]] = []
        harvest(soup.select_one("#mw-pages"), 0)
        harvest(soup.select_one("#mw-subcategories"), 14)
        for item in yield_results:
            yield item

        # next-page link inside #mw-pages
        next_url: str | None = None
        pages_div = soup.select_one("#mw-pages")
        if pages_div is not None:
            for a in pages_div.find_all("a", href=True):
                txt = a.get_text(" ", strip=True).lower()
                if "next page" in txt or (txt.startswith("next") and len(txt) < 30):
                    raw = a.get("href")
                    href = (raw[0] if isinstance(raw, list) and raw
                            else str(raw) if raw else "")
                    if href:
                        next_url = urljoin(WIKI, href)
                        break
        url = next_url


def _load_targeted_titles() -> set[str]:
    if not TARGETED_TITLES_FILE.exists():
        return set()
    return {ln.strip() for ln in
            TARGETED_TITLES_FILE.read_text(encoding="utf-8").splitlines()
            if ln.strip()}


def _load_targeted_cats_done() -> set[str]:
    if not TARGETED_CATS_DONE_FILE.exists():
        return set()
    return {ln.strip() for ln in
            TARGETED_CATS_DONE_FILE.read_text(encoding="utf-8").splitlines()
            if ln.strip()}


def _append_lines(path: Path, lines: list[str]) -> None:
    if not lines:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def gather_titles_from_categories(
    flagged: list[dict], delay: float, recurse: bool = True, max_depth: int = 3
) -> list[str]:
    """Walk every flagged category (and its subcategories up to ``max_depth``)
    and return the union of member titles. Streams progress to disk after
    every completed category so a Ctrl-C or rate-limit doesn't lose work —
    the next run resumes from the checkpoint and skips categories already
    fully enumerated.

    Prefers the MediaWiki API; falls back to HTML scraping per-category if
    no API is reachable."""
    if not flagged:
        return []

    title_set = _load_targeted_titles()
    cats_done = _load_targeted_cats_done()
    if title_set or cats_done:
        print(f"  resuming from checkpoint: {len(title_set)} titles, "
              f"{len(cats_done)} categories already enumerated")

    pending = [c for c in flagged if c["page"] not in cats_done]
    if not pending and title_set:
        print(f"  all {len(flagged)} flagged categories already enumerated; "
              f"using cached {len(title_set)} titles")
        return sorted(title_set)
    print(f"  {len(pending)} of {len(flagged)} flagged categories left to enumerate")

    with httpx.Client(headers={"User-Agent": USER_AGENT},
                      follow_redirects=True, timeout=60.0) as client:
        api_url = discover_api_url(client, delay)
        if api_url:
            print(f"  using MediaWiki API for category enumeration ({api_url})")
        else:
            print("  no API; falling back to HTML scraping per category")

        cat_queue: deque[tuple[str, int]] = deque(
            (c["page"], 0) for c in pending
        )
        cats_processed = 0

        try:
            while cat_queue:
                cat_page, depth = cat_queue.popleft()
                if cat_page in cats_done or depth > max_depth:
                    continue
                cats_processed += 1

                before = len(title_set)
                added_titles: list[str] = []

                if api_url:
                    stream = categorymembers_iter_api(client, api_url, cat_page, delay)
                else:
                    stream = categorymembers_iter_html(client, cat_page, delay)

                for member_title, ns in stream:
                    if ns == 0:
                        if member_title not in title_set:
                            title_set.add(member_title)
                            added_titles.append(member_title)
                    elif ns == 14 and recurse and member_title not in cats_done:
                        cat_queue.append((member_title, depth + 1))

                # checkpoint AFTER the category fully iterates without raising.
                # If we raised mid-iteration (rate limit), the cat isn't marked
                # done and gets retried next run.
                _append_lines(TARGETED_TITLES_FILE, added_titles)
                cats_done.add(cat_page)
                _append_lines(TARGETED_CATS_DONE_FILE, [cat_page])

                added = len(title_set) - before
                print(f"    [{cats_processed:>4}/{cats_processed + len(cat_queue):>4}]  "
                      f"{cat_page:<60}  +{added} titles  (total {len(title_set)})")
        except RateLimitedError as exc:
            print(f"\n  ! persistent rate limit: {exc}")
            print(f"  ! checkpoint saved: {len(title_set)} titles, "
                  f"{len(cats_done)} categories done")
            print(f"  ! files: {TARGETED_TITLES_FILE.name}, "
                  f"{TARGETED_CATS_DONE_FILE.name}")
            print("  ! wait 30-60 min, raise --delay, then re-run Option 1 "
                  "to resume from this checkpoint.")

    return sorted(title_set)


# --- category discovery ----------------------------------------------------

def _parse_category_count(text: str) -> int:
    """Pull the member count out of a Special:Categories list item.

    The text looks like ``Mounts ‎(347 members)`` or sometimes
    ``Some Category ‎(1 member)``. We accept commas in the number for wikis
    with very large categories."""
    m = re.search(r"\(\s*([\d,]+)\s+members?\s*\)", text)
    return int(m.group(1).replace(",", "")) if m else 0


def crawl_categories(delay: float, verbose: bool = True) -> list[dict]:
    """Walk ``Special:Categories`` (with its pagination) and return every
    category the wiki advertises, as records of {name, members, page, url}.

    Cheap by comparison to ``allpages``: ~10 index pages total, no per-item
    fetches. Does not use the page-cache (the result is in ``categories.json``
    instead, so re-fetching is intentional)."""
    out: list[dict] = []
    next_url: str | None = SPECIAL_CATEGORIES
    seen_urls: set[str] = set()
    pages_visited = 0
    last = 0.0
    with httpx.Client(headers={"User-Agent": USER_AGENT},
                      follow_redirects=True, timeout=30.0) as client:
        while next_url and next_url not in seen_urls:
            seen_urls.add(next_url)
            wait = delay - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
            try:
                r = client.get(next_url)
            except httpx.HTTPError as exc:
                print(f"  ! fetch failed: {exc}", file=sys.stderr)
                break
            last = time.monotonic()
            if not r.is_success:
                print(f"  ! HTTP {r.status_code} at {next_url}", file=sys.stderr)
                break
            soup = BeautifulSoup(r.text, "html.parser")
            body = soup.select_one("#mw-content-text") or soup

            added = 0
            for li in body.find_all("li"):
                a = li.find("a", href=True)
                if not isinstance(a, Tag):
                    continue
                raw = a.get("href")
                href = (raw[0] if isinstance(raw, list) and raw
                        else str(raw) if raw else "")
                if not href.startswith("/wiki/Category:"):
                    continue
                name = a.get_text(strip=True)
                count = _parse_category_count(li.get_text(" ", strip=True))
                out.append({
                    "name": name,
                    "members": count,
                    "page": unquote(href[len("/wiki/"):]),
                    "url": urljoin(WIKI, href),
                })
                added += 1

            pages_visited += 1
            # Find the "next page" link. MediaWiki's Special:Categories uses
            # ``?from=...`` or ``?pagefrom=...`` for pagination, in a footer
            # navigation block. Cast a wide net for selectors / link text.
            new_next: str | None = None
            for a in body.find_all("a", href=True):
                txt = a.get_text(" ", strip=True).lower()
                if not txt:
                    continue
                if "next page" in txt or (txt.startswith("next") and len(txt) < 30):
                    raw = a.get("href")
                    href = (raw[0] if isinstance(raw, list) and raw
                            else str(raw) if raw else "")
                    if "Special:Categories" in href or "from=" in href or "pagefrom=" in href:
                        new_next = urljoin(WIKI, href)
                        break

            if verbose:
                cont = "(continuing)" if new_next else "(end)"
                print(f"  index page {pages_visited:>3}: +{added} categories "
                      f"-> {len(out)} total  {cont}")
            next_url = new_next
    return out


def _load_existing_category_flags() -> dict[str, bool]:
    """Read crawl=true/false flags from a prior categories.json so a re-run
    of ``discover_categories`` doesn't blow away the user's selections."""
    if not CATEGORIES_FILE.exists():
        return {}
    try:
        doc = json.loads(CATEGORIES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, bool] = {}
    for c in doc.get("categories", []):
        page = c.get("page")
        if not page or "crawl" not in c:
            continue
        out[page] = bool(c["crawl"])
    return out


def discover_categories(delay: float) -> None:
    """Crawl Special:Categories, save to ``categories.json``, print a summary.

    Each record carries a ``crawl`` boolean: true if the category is selected
    for Option-1 enumeration, false otherwise. On re-runs of this command,
    any flags the user has manually edited are preserved; new categories get
    the preset default (true if their name matches DEFAULT_CRAWL_CATEGORIES,
    false otherwise)."""
    print(f"Discovering categories from {SPECIAL_CATEGORIES} ...")
    existing_flags = _load_existing_category_flags()
    cats = crawl_categories(delay)
    if not cats:
        print("  no categories extracted (selectors may need updating)")
        return

    # dedupe (same category can appear at chunk boundaries)
    seen_keys: set[str] = set()
    deduped: list[dict] = []
    for c in cats:
        if c["page"] in seen_keys:
            continue
        seen_keys.add(c["page"])
        # Preserve a previously-set crawl flag; otherwise apply preset defaults.
        if c["page"] in existing_flags:
            c["crawl"] = existing_flags[c["page"]]
        else:
            c["crawl"] = _preset_crawl(c["name"])
        deduped.append(c)
    deduped.sort(key=lambda c: (-c["members"], c["name"].lower()))

    crawl_count = sum(1 for c in deduped if c["crawl"])
    crawl_members = sum(c["members"] for c in deduped if c["crawl"])
    total_members = sum(c["members"] for c in deduped)
    preserved = sum(1 for c in deduped if c["page"] in existing_flags)

    CATEGORIES_FILE.write_text(
        json.dumps({
            "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": SPECIAL_CATEGORIES,
            "count": len(deduped),
            "crawl_count": crawl_count,
            "categories": deduped,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"\nSaved {len(deduped)} categories ({total_members} member entries) "
          f"to {CATEGORIES_FILE}")
    print(f"  flagged for crawl: {crawl_count} categories "
          f"({crawl_members} member entries)")
    if preserved:
        print(f"  preserved {preserved} pre-existing crawl flags from prior run")

    top = deduped[:30]
    print("\nTop 30 categories by member count:")
    for c in top:
        mark = "[x]" if c["crawl"] else "[ ]"
        print(f"  {mark} {c['members']:>6}  {c['name']}")
    if len(deduped) > 30:
        print(f"  ...  ({len(deduped) - 30} more — see {CATEGORIES_FILE.name})")
    print('\nEdit "crawl": true/false in categories.json to control which '
          "categories Option 1 enumerates.")


# --- link / page helpers ----------------------------------------------------

def is_internal_wiki_link(href: str) -> bool:
    if not href or not href.startswith("/wiki/"):
        return False
    page = href[len("/wiki/"):].split("#", 1)[0]
    if not page:
        return False
    return not any(page.startswith(ns) for ns in SKIP_NS)


def absolute(href: str) -> str:
    return urljoin(WIKI, href.split("#", 1)[0])


def page_title_from_url(url: str) -> str:
    path = urlsplit(url).path
    m = re.search(r"/wiki/(.+)$", path)
    return unquote(m.group(1)) if m else url


def extract_links(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    body = soup.select_one("#mw-content-text") or soup
    seen: set[str] = set()
    out: list[str] = []
    for a in body.find_all("a", href=True):
        href = a.get("href", "")
        if isinstance(href, list):
            href = href[0] if href else ""
        if is_internal_wiki_link(href):
            url = absolute(href)
            if url not in seen:
                seen.add(url)
                out.append(url)
    return out


# --- infobox parsing --------------------------------------------------------

def _clean_text(node: Tag) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def _links_in(node: Tag) -> list[dict]:
    out: list[dict] = []
    for a in node.find_all("a", href=True):
        href = a.get("href", "")
        if isinstance(href, list):
            href = href[0] if href else ""
        if not is_internal_wiki_link(href):
            continue
        out.append({
            "title": _clean_text(a),
            "page": page_title_from_url(absolute(href)),
        })
    return out


def parse_quest_page(html: str, url: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.select_one("#firstHeading")
    if not h1:
        return None
    title = _clean_text(h1)

    record: dict = {
        "title": title,
        "page": page_title_from_url(url),
        "url": url,
    }
    chain: dict[str, list[dict]] = {}
    info: dict[str, str] = {}

    candidates: list[Tag] = []
    for sel in ("table.infobox", ".infobox", "table.mw-collapsible.infobox",
                "table.questbox", "table.notice"):
        candidates.extend(soup.select(sel))
    seen_rows: set[int] = set()
    for box in candidates:
        for row in box.find_all("tr"):
            if id(row) in seen_rows:
                continue
            seen_rows.add(id(row))
            cells = row.find_all(["th", "td"])
            if len(cells) < 2:
                continue
            label = re.sub(r"\s+", " ",
                           _clean_text(cells[0]).rstrip(":").lower()).strip()
            value_cell = cells[-1]
            value_text = _clean_text(value_cell)
            if not value_text or value_text == "—":
                continue
            if label in CHAIN_FIELDS:
                bucket = CHAIN_FIELDS[label]
                refs = _links_in(value_cell)
                if not refs:
                    refs = [{"title": v.strip(), "page": None}
                            for v in re.split(r",|;|\n", value_text) if v.strip()]
                chain.setdefault(bucket, []).extend(refs)
            elif label in INFO_FIELDS:
                info[INFO_FIELDS[label]] = value_text
            elif label and len(label) < 60:
                info.setdefault(label, value_text)

    record["info"] = info
    record["chain"] = chain
    cats = [_clean_text(li) for li in soup.select("#mw-normal-catlinks ul li")]
    if cats:
        record["categories"] = cats

    looks_like_quest = bool(chain) or any(
        k in info for k in ("level", "type", "expansion", "patch", "npc")
    ) or any("Quest" in c for c in cats)
    return record if looks_like_quest else None


# --- robots / migration -----------------------------------------------------

def check_robots(seed_url: str) -> bool:
    parts = urlsplit(seed_url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    parser = RobotFileParser()
    try:
        with httpx.Client(timeout=10.0, headers={"User-Agent": USER_AGENT}) as c:
            r = c.get(robots_url)
            parser.parse(r.text.splitlines() if r.is_success else [])
    except httpx.HTTPError:
        print(f"  ! could not load {robots_url}; proceeding cautiously",
              file=sys.stderr)
        return True
    ok = parser.can_fetch(USER_AGENT, seed_url)
    if not ok:
        print(f"robots.txt disallows {seed_url} for our User-Agent.",
              file=sys.stderr)
    return ok


def migrate_flat_cache() -> int:
    """Move legacy ``cache/*.html`` into ``cache/<letter>/*.html`` buckets."""
    if not CACHE_DIR.exists():
        return 0
    moved = 0
    for f in CACHE_DIR.glob("*.html"):
        if not f.is_file():
            continue
        first = f.stem[:1].lower()
        bucket = first if first.isalnum() else "_"
        target_dir = CACHE_DIR / bucket
        target_dir.mkdir(exist_ok=True)
        try:
            f.rename(target_dir / f.name)
            moved += 1
        except OSError:
            pass
    if moved:
        print(f"  migrated {moved} legacy flat-cache files into buckets")
    return moved


def cache_size_summary() -> str:
    if not CACHE_DIR.exists():
        return "(no cache yet)"
    total = 0
    count = 0
    for f in CACHE_DIR.rglob("*.html"):
        try:
            total += f.stat().st_size
            count += 1
        except OSError:
            pass
    mb = total / 1024 / 1024
    return f"{count} pages, {mb:.1f} MB"


# --- crawl orchestration ----------------------------------------------------

def load_seen() -> set[str]:
    if SEEN_FILE.exists():
        return {ln for ln in SEEN_FILE.read_text(encoding="utf-8").splitlines() if ln}
    return set()


def save_seen(seen: Iterable[str]) -> None:
    SEEN_FILE.write_text("\n".join(sorted(seen)), encoding="utf-8")


def _load_flagged_categories() -> list[dict]:
    """Return the categories.json entries with crawl=true, or [] if no
    flagged categories / no file exists."""
    if not CATEGORIES_FILE.exists():
        return []
    try:
        doc = json.loads(CATEGORIES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [c for c in doc.get("categories", []) if c.get("crawl")]


def crawl_allpages(args: argparse.Namespace, fetcher: PoliteFetcher,
                   stats: CrawlStats, seen: set[str]) -> None:
    """Enumerate pages to cache. Two paths:

    1. If ``categories.json`` exists and has any entries with ``crawl: true``,
       enumerate only the union of those categories' members (recursive into
       subcategories). This is the "smart" mode driven by the user's
       curation.
    2. Otherwise fall back to enumerating every page on the wiki via the
       MediaWiki API, with Special:AllPages HTML as the last resort.
    """
    flagged = _load_flagged_categories()
    if flagged:
        flagged_names = ", ".join(c["name"] for c in flagged[:5])
        more = f" (+{len(flagged) - 5} more)" if len(flagged) > 5 else ""
        print(f"Targeted mode: {len(flagged)} flagged categories — "
              f"{flagged_names}{more}")
        titles = gather_titles_from_categories(flagged, args.delay)
        print(f"  enumerated {len(titles)} unique titles across flagged "
              f"categories")
    else:
        print("Discovering pages via Special:AllPages (no flagged categories "
              "in categories.json — running full-wiki mode)")
        with httpx.Client(headers={"User-Agent": USER_AGENT},
                          timeout=60.0, follow_redirects=True) as api:
            api_url = discover_api_url(api, args.delay)
            if api_url:
                print(f"  using MediaWiki API at {api_url}")
                titles = list(allpages_iter_api(api, api_url, args.delay))
            else:
                print("  API unavailable; falling back to Special:AllPages HTML")
                titles = list(allpages_iter_html(api, args.delay))
        # dedupe while preserving order (HTML may repeat across boundaries)
        seen_titles: set[str] = set()
        deduped: list[str] = []
        for t in titles:
            if t not in seen_titles:
                seen_titles.add(t)
                deduped.append(t)
        titles = deduped
        print(f"  enumerated {len(titles)} pages")

    stats.queued = len(titles)
    flush_every = 100

    try:
        for idx, title in enumerate(titles, 1):
            if args.limit is not None and stats.processed >= args.limit:
                print(f"  hit --limit {args.limit}; stopping")
                break
            url = title_to_url(title)
            if url in seen:
                stats.processed += 1
                continue
            seen.add(url)
            html, hit = fetcher.get(url)
            stats.processed += 1
            kind = "HIT" if hit else ("GET" if html else "ERR")
            size = (len(html) / 1024) if html else None
            print(progress_line(stats, kind, title[:58], size, args.delay))
            if stats.processed % flush_every == 0:
                save_seen(seen)
    finally:
        save_seen(seen)


def crawl_bfs(args: argparse.Namespace, fetcher: PoliteFetcher,
              stats: CrawlStats, seen: set[str]) -> None:
    """Breadth-first walk from ``--seed`` (or ``--seed-file``)."""
    if args.seed_file:
        text = args.seed_file.read_text(encoding="utf-8", errors="replace")
        print(f"Seed: {args.seed_file} (local)")
    else:
        print(f"Seed: {args.seed}  delay={args.delay}s depth={args.depth} "
              f"limit={args.limit}")
        text_and_hit = fetcher.get(args.seed)
        text = text_and_hit[0]
        if text is None:
            sys.exit(f"Failed to fetch seed {args.seed}")

    queue: deque[tuple[str, int]] = deque()
    for link in extract_links(text):
        if link not in seen:
            queue.append((link, 1))
    stats.queued = len(queue)
    print(f"  discovered {stats.queued} links from seed")

    flush_every = 100
    try:
        while queue:
            if args.limit is not None and stats.processed >= args.limit:
                print(f"  hit --limit {args.limit}; stopping")
                break
            url, depth = queue.popleft()
            if url in seen:
                continue
            seen.add(url)
            html, hit = fetcher.get(url)
            stats.processed += 1
            kind = "HIT" if hit else ("GET" if html else "ERR")
            size = (len(html) / 1024) if html else None
            slug = page_title_from_url(url)[:58]
            print(progress_line(stats, kind, slug, size, args.delay,
                                extra=f"d={depth}"))
            if html and depth < args.depth:
                added = 0
                for nxt in extract_links(html):
                    if nxt not in seen:
                        queue.append((nxt, depth + 1))
                        added += 1
                stats.queued += added
            if stats.processed % flush_every == 0:
                save_seen(seen)
    finally:
        save_seen(seen)


def parse_cache() -> int:
    """Scan the local cache for quest pages and (re)write quests.jsonl."""
    if not CACHE_DIR.exists():
        print("No cache to parse.")
        return 0
    files = list(CACHE_DIR.rglob("*.html"))
    print(f"Parsing {len(files)} cached pages...")
    written = 0
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with DATA_FILE.open("w", encoding="utf-8") as out:
        for i, f in enumerate(files, 1):
            try:
                html = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            url = f"{WIKI}/wiki/{f.stem}"
            rec = parse_quest_page(html, url)
            if rec:
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                written += 1
            if i % 500 == 0:
                print(f"  ...{i}/{len(files)} scanned, {written} quest records")
    print(f"Parsed {len(files)} files; {written} quest records written to {DATA_FILE}")
    return written


def build_graph() -> None:
    if not DATA_FILE.exists():
        sys.exit(f"No {DATA_FILE}; run a crawl + parse first.")
    by_page: dict[str, dict] = {}
    by_title: dict[str, str] = {}
    with DATA_FILE.open(encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            page = rec.get("page") or rec.get("title")
            if not page:
                continue
            by_page[page] = rec
            by_title.setdefault((rec.get("title") or "").lower(), page)

    def resolve(ref: dict) -> str | None:
        if ref.get("page") and ref["page"] in by_page:
            return ref["page"]
        return by_title.get((ref.get("title") or "").strip().lower())

    edges: list[dict] = []
    for page, rec in by_page.items():
        for src in (rec.get("chain") or {}).get("required_quest", []):
            r = resolve(src)
            edges.append({"type": "requires", "from": page,
                          "to": r or src.get("title"), "resolved": r is not None})
        for nxt in (rec.get("chain") or {}).get("next_quest", []):
            r = resolve(nxt)
            edges.append({"type": "unlocks", "from": page,
                          "to": r or nxt.get("title"), "resolved": r is not None})

    graph = {
        "quest_count": len(by_page),
        "edge_count": len(edges),
        "resolved_edges": sum(1 for e in edges if e["resolved"]),
        "edges": edges,
    }
    GRAPH_FILE.write_text(json.dumps(graph, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    print(f"Graph: {graph['quest_count']} quests, {graph['edge_count']} edges, "
          f"{graph['resolved_edges']} resolved "
          f"({100 * graph['resolved_edges'] / max(graph['edge_count'], 1):.1f}%)"
          f" -> {GRAPH_FILE}")


# --- interactive menu ------------------------------------------------------

def _settings_line(s: dict) -> str:
    return (f"delay={s['delay']}s, "
            f"limit={s['limit'] or 'unlimited'}, "
            f"depth={s['depth']}")


def _summarize_outputs() -> list[str]:
    out: list[str] = [f"Cache    : {cache_size_summary()}"]
    if DATA_FILE.exists():
        try:
            with DATA_FILE.open(encoding="utf-8") as f:
                n = sum(1 for _ in f)
            out.append(f"JSONL    : {n} quest records ({DATA_FILE.name})")
        except OSError:
            pass
    if GRAPH_FILE.exists():
        try:
            g = json.loads(GRAPH_FILE.read_text(encoding="utf-8"))
            res = g.get("resolved_edges", 0)
            tot = g.get("edge_count", 0)
            pct = (100 * res / tot) if tot else 0.0
            out.append(f"Graph    : {g.get('quest_count', 0)} quests, "
                       f"{tot} edges, {res} resolved ({pct:.1f}%)")
        except (OSError, json.JSONDecodeError):
            pass
    if CATEGORIES_FILE.exists():
        try:
            doc = json.loads(CATEGORIES_FILE.read_text(encoding="utf-8"))
            total = doc.get("count", len(doc.get("categories", [])))
            flagged = sum(1 for c in doc.get("categories", []) if c.get("crawl"))
            mode = ("Option 1 will use TARGETED mode" if flagged
                    else "Option 1 will use FULL-WIKI mode")
            out.append(f"Cats     : {total} total, {flagged} flagged crawl=true   "
                       f"({mode})")
        except (OSError, json.JSONDecodeError):
            pass
    if TARGETED_TITLES_FILE.exists() or TARGETED_CATS_DONE_FILE.exists():
        titles = _load_targeted_titles()
        cats_done = _load_targeted_cats_done()
        out.append(f"Resume   : {len(titles)} titles + {len(cats_done)} categories "
                   "checkpointed (Option 1 will resume from here)")
    return out


def show_buckets() -> None:
    if not CACHE_DIR.exists():
        print("  (no cache yet)")
        return
    buckets = sorted([d for d in CACHE_DIR.iterdir() if d.is_dir()])
    if not buckets:
        print("  (cache is empty)")
        return
    print()
    print(f"  {'bucket':<8} {'files':>8} {'size':>10}")
    print("  " + "-" * 30)
    total_files = total_size = 0
    for b in buckets:
        files = list(b.glob("*.html"))
        size = sum(f.stat().st_size for f in files if f.is_file())
        total_files += len(files)
        total_size += size
        print(f"  {b.name:<8} {len(files):>8} {size / 1024 / 1024:>8.2f} MB")
    print("  " + "-" * 30)
    print(f"  {'TOTAL':<8} {total_files:>8} {total_size / 1024 / 1024:>8.2f} MB")


def wipe_all() -> None:
    DATA_FILE.unlink(missing_ok=True)
    GRAPH_FILE.unlink(missing_ok=True)
    SEEN_FILE.unlink(missing_ok=True)
    TARGETED_TITLES_FILE.unlink(missing_ok=True)
    TARGETED_CATS_DONE_FILE.unlink(missing_ok=True)
    if CACHE_DIR.exists():
        import shutil
        shutil.rmtree(CACHE_DIR)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)


def edit_settings(settings: dict) -> None:
    print(f"\n  Current: {_settings_line(settings)}")
    raw = input("  Delay seconds [Enter to keep]: ").strip()
    if raw:
        try:
            settings["delay"] = max(0.0, float(raw))
        except ValueError:
            print("    (not a number — kept old value)")
    raw = input("  Page limit [Enter for unlimited, 0 = unlimited]: ").strip()
    if raw:
        try:
            v = int(raw)
            settings["limit"] = v if v > 0 else None
        except ValueError:
            print("    (not a number — kept old value)")
    raw = input("  BFS depth [Enter to keep]: ").strip()
    if raw:
        try:
            settings["depth"] = max(1, int(raw))
        except ValueError:
            print("    (not a number — kept old value)")


def _run_crawl_phase(args: argparse.Namespace) -> None:
    """Run a crawl + parse + graph build with the given argparse-shaped args.
    Shared by the CLI and the menu so behavior stays identical."""
    if args.mode == "bfs":
        if not args.seed_file and not check_robots(args.seed):
            return
    else:
        if not check_robots(WIKI + "/api.php"):
            return

    stats = CrawlStats()
    fetcher = PoliteFetcher(args.delay, CACHE_DIR, stats)
    seen = load_seen()
    try:
        if args.mode == "allpages":
            crawl_allpages(args, fetcher, stats, seen)
        else:
            crawl_bfs(args, fetcher, stats, seen)
    except KeyboardInterrupt:
        print("\n  Interrupted — partial progress saved.")
    finally:
        fetcher.close()

    elapsed = int(time.monotonic() - stats.started_at)
    print(f"\nDone. processed={stats.processed} "
          f"(cache_hits={stats.cache_hits} live={stats.fetched} "
          f"failed={stats.failed}) in {elapsed // 60}m{elapsed % 60:02d}s")
    print(f"Cache now: {cache_size_summary()}")

    parse_cache()
    build_graph()


def _menu_namespace(mode: str, settings: dict, *, seed: str = DEFAULT_SEED,
                    seed_file: Path | None = None) -> argparse.Namespace:
    return argparse.Namespace(
        mode=mode, seed=seed, seed_file=seed_file,
        depth=settings["depth"], limit=settings["limit"],
        delay=settings["delay"], reset=False,
        parse_only=False, build_graph_only=False, menu=False,
    )


MENU_OPTIONS = """\
  [1] Crawl whole wiki              (allpages mode, recommended)
  [2] Crawl from a seed URL         (BFS mode)
  [3] Crawl from a local HTML file  (BFS, no remote seed fetch)
  [4] Re-parse cache -> quests.jsonl
  [5] Rebuild chains.json from quests.jsonl
  [6] Show cache breakdown by bucket
  [7] List all wiki categories      (writes categories.json)
  [8] Edit settings (delay / limit / depth)
  [9] Reset everything (cache + outputs)
  [q] Quit
"""


def run_menu() -> None:
    settings = {"delay": 1.2, "limit": None, "depth": 2}
    print("\nFFXIV Wiki Crawler — interactive menu")
    print("Tip: any crawl can be interrupted with Ctrl-C; progress is "
          "saved and the next run resumes from cache.")

    while True:
        print()
        print("=" * 64)
        for line in _summarize_outputs():
            print(f"  {line}")
        print(f"  Settings : {_settings_line(settings)}")
        print("=" * 64)
        print(MENU_OPTIONS)
        try:
            choice = input("  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if choice in ("q", "quit", "exit"):
            return
        if choice == "1":
            _run_crawl_phase(_menu_namespace("allpages", settings))
        elif choice == "2":
            url = input(f"  Seed URL [Enter for {DEFAULT_SEED}]: ").strip()
            _run_crawl_phase(_menu_namespace(
                "bfs", settings, seed=(url or DEFAULT_SEED)))
        elif choice == "3":
            raw = input("  Path to local HTML file: ").strip().strip('"')
            if not raw:
                continue
            p = Path(raw)
            if not p.exists():
                print(f"  not found: {p}")
                continue
            _run_crawl_phase(_menu_namespace("bfs", settings, seed_file=p))
        elif choice == "4":
            parse_cache()
            build_graph()
        elif choice == "5":
            build_graph()
        elif choice == "6":
            show_buckets()
        elif choice == "7":
            discover_categories(settings["delay"])
        elif choice == "8":
            edit_settings(settings)
        elif choice == "9":
            confirm = input('  Type "RESET" to confirm wiping cache + outputs: ').strip()
            if confirm == "RESET":
                wipe_all()
                print("  All cache and outputs wiped.")
            else:
                print("  Cancelled.")
        else:
            print(f"  unknown choice: {choice!r}")


# --- entry point ------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--mode", choices=("bfs", "allpages"), default="bfs",
                    help="bfs: seed-driven walk; allpages: full-site enumeration via API")
    ap.add_argument("--seed", default=DEFAULT_SEED,
                    help="BFS mode: starting URL")
    ap.add_argument("--seed-file", type=Path, default=None,
                    help="BFS mode: use this local HTML file as the seed")
    ap.add_argument("--depth", type=int, default=2,
                    help="BFS mode: max link depth (default 2)")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap the number of pages processed this run")
    ap.add_argument("--delay", type=float, default=1.2,
                    help="seconds between live requests (default 1.2)")
    ap.add_argument("--reset", action="store_true",
                    help="wipe cache + outputs and start fresh")
    ap.add_argument("--parse-only", action="store_true",
                    help="skip crawling; re-parse the existing cache to JSONL")
    ap.add_argument("--build-graph-only", action="store_true",
                    help="skip crawling/parsing; rebuild chains.json from JSONL")
    ap.add_argument("--menu", action="store_true",
                    help="open the interactive text menu (also the default when "
                         "the script is run with no arguments)")
    ap.add_argument("--list-categories", action="store_true",
                    help="walk Special:Categories and write categories.json")
    return ap.parse_args()


def main() -> None:
    # Bare invocation: open the interactive menu (the easy-to-discover path).
    # Explicit flags still bypass the menu for scripted use.
    if len(sys.argv) == 1:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        migrate_flat_cache()
        run_menu()
        return

    args = parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if args.reset:
        wipe_all()

    migrate_flat_cache()

    if args.menu:
        run_menu()
        return

    if args.list_categories:
        discover_categories(args.delay)
        return

    print(f"Cache so far: {cache_size_summary()}")

    if args.build_graph_only:
        build_graph()
        return
    if args.parse_only:
        parse_cache()
        build_graph()
        return

    _run_crawl_phase(args)


if __name__ == "__main__":
    main()
