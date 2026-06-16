"""Shared parsing helpers and per-site extraction functions.

Adding a new institution should require only:
  1. a new entry in ``config/sites.yaml``, and
  2. one ``extract_<site>`` generator here (plus its two phase helpers),
     registered in ``SITE_EXTRACTORS``.

Two-phase pattern (per site):
  Phase 1 (discovery): ``_discover_<site>(config, fetcher) -> Iterable[dict]``
      yields article-metadata dicts with keys
      ``url, title, author, publication_date, section``.
  Phase 2 (full text): ``_extract_text_<site>(url, fetcher) -> dict``
      fetches one article page and returns a UNIFORM dict
      ``{"text", "author", "publication_date"}``.

Why phase 2 returns a dict (not a bare string): for Yale and Duke the author
and date live on the article page, not in the listing, so phase 2 must surface
them. Northwestern gets author/date from RSS, so its phase-2 dict leaves those
two keys empty and only fills ``text``. Keeping all three phase-2 helpers on the
SAME return shape avoids a subtle return-type wart -- this is intentional, not a
bug. ``extract_<site>`` merges phase-2 values over phase-1 values, preferring
non-empty data.

Access notes (see recon/RECON.md):
  - Northwestern: open WordPress RSS + static HTML. Uses the shared Fetcher.
  - Duke: SNWorks WAF (awselb) returns 403 for the honest research UA, so this
    module uses a browser UA via a dedicated requests.Session (the shared
    Fetcher is left untouched) while keeping the same politeness delays.
  - Yale: behind a Vercel bot-detection checkpoint requiring JS, so this module
    renders with Playwright (headless Chromium) and falls back to a browser-UA
    requests GET.
"""

from __future__ import annotations

import logging
import os
import random
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Iterator
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

from .schema import Article

if TYPE_CHECKING:
    from .fetcher import Fetcher

logger = logging.getLogger(__name__)

# Browser User-Agent used ONLY where a site's bot protection blocks the honest
# research UA (Duke WAF, Yale Vercel checkpoint). Documented in RECON.md.
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ----------------------------------------------------------------------
# Shared helpers
# ----------------------------------------------------------------------

def make_soup(markup: str) -> BeautifulSoup:
    """Parse HTML markup into a BeautifulSoup tree (lxml backend)."""
    return BeautifulSoup(markup, "lxml")


def clean_text(raw: str) -> str:
    """Normalize free text before it enters an Article record.

    Steps:
      - strip any residual HTML tags via BeautifulSoup,
      - Unicode NFKC normalization,
      - replace non-breaking spaces (\\xa0) with regular spaces,
      - collapse runs of whitespace into a single space,
      - strip leading/trailing whitespace.

    Must be applied to all text and author fields.
    """
    if not raw:
        return ""
    # Strip residual tags (handles cases where raw still contains markup).
    text = BeautifulSoup(raw, "lxml").get_text(separator=" ")
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_date(raw: str, url: str) -> str:
    """Normalize a date string to ISO 8601 (YYYY-MM-DD).

    The ``url`` argument exists so a parse failure can be logged with the
    offending article's URL for auditing -- it MUST be passed through to the
    warning call, not merely accepted.

    On failure or ambiguity we never write a silent null: we return
    ``"UNPARSED:<raw>"`` so the bad value is visible in the CSV output.
    """
    if not raw or not raw.strip():
        logger.warning("Empty/missing date for article %s", url)
        return f"UNPARSED:{raw}"
    try:
        parsed = date_parser.parse(raw)
    except (ValueError, OverflowError, TypeError):
        logger.warning("Could not parse date %r for article %s", raw, url)
        return f"UNPARSED:{raw}"
    return parsed.date().isoformat()


def _polite_sleep(config: dict) -> None:
    """Sleep a randomized delay using the site's rate_limit config.

    Used by site modules that fetch outside the shared Fetcher (Duke/Yale) so
    they remain just as polite as the requests routed through it.
    """
    rl = config.get("rate_limit", {}) or {}
    time.sleep(random.uniform(rl.get("delay_min", 1.0), rl.get("delay_max", 3.0)))


def _empty_page() -> dict:
    """Uniform empty phase-2 result."""
    return {"text": "", "author": "", "publication_date": ""}


# ----------------------------------------------------------------------
# Northwestern (The Daily Northwestern) -- RSS discovery + static HTML
# ----------------------------------------------------------------------

NW_DEFAULT_FEED = "https://dailynorthwestern.com/feed/rss/"


def _discover_northwestern(config: dict, fetcher: "Fetcher") -> Iterable[dict]:
    """Phase 1: parse the WordPress RSS feed into metadata dicts."""
    feed_url = config.get("rss_url") or NW_DEFAULT_FEED
    max_articles = config.get("max_articles", 100)
    parsed = feedparser.parse(feed_url)
    if parsed.bozo:
        logger.warning("Northwestern feed parse warning: %s", parsed.bozo_exception)

    results: list[dict] = []
    for entry in parsed.entries[:max_articles]:
        url = entry.get("link", "")
        if not url:
            continue
        # feedparser maps <dc:creator> -> author and <category> -> tags[].term
        tags = entry.get("tags", []) or []
        section = tags[0].get("term", "") if tags else ""
        results.append(
            {
                "url": url,
                "title": clean_text(entry.get("title", "")),
                "author": clean_text(entry.get("author", "")),
                "publication_date": entry.get("published", ""),  # normalized later
                "section": section,
            }
        )
    return results


def _extract_text_northwestern(url: str, fetcher: "Fetcher") -> dict:
    """Phase 2: fetch the article and return the SNO/FLEX story body text."""
    try:
        resp = fetcher.get(url)
    except requests.RequestException as exc:
        # Non-200 after retries (raise_for_status) or connection failure.
        logger.warning("Northwestern fetch failed for %s: %s", url, exc)
        return _empty_page()
    if resp is None:
        # robots.txt disallowed the URL (Fetcher returns None).
        logger.warning("Northwestern fetch skipped (robots.txt) for %s", url)
        return _empty_page()

    soup = make_soup(resp.text)
    # SNO/FLEX Pro theme renders the full body inside #sno-story-body-content.
    body = soup.find(id="sno-story-body-content")
    if body is None:
        logger.warning("Northwestern body container not found for %s", url)
        return _empty_page()
    paragraphs = [p.get_text(" ") for p in body.find_all("p")]
    text = clean_text(" ".join(paragraphs))
    # Author/date come from the RSS feed in phase 1.
    return {"text": text, "author": "", "publication_date": ""}


def extract_northwestern(config: dict, fetcher: "Fetcher") -> Iterator[Article]:
    """Yield Daily Northwestern Article records (RSS + full-text fetch)."""
    institution = "The Daily Northwestern"
    max_articles = config.get("max_articles", 100)
    count = 0
    for meta in _discover_northwestern(config, fetcher):
        if count >= max_articles:
            break
        url = meta.get("url", "")
        if not url:
            continue  # never yield an Article with an empty url
        page = _extract_text_northwestern(url, fetcher)
        raw_date = meta.get("publication_date") or page.get("publication_date", "")
        yield Article(
            institution=institution,
            title=meta.get("title", ""),
            author=clean_text(meta.get("author", "") or page.get("author", "")),
            publication_date=normalize_date(raw_date, url),
            section=meta.get("section", ""),
            url=url,
            text=page.get("text", ""),
            scraped_at=datetime.now(timezone.utc).isoformat(),
        )
        count += 1


# ----------------------------------------------------------------------
# Duke (The Chronicle) -- SNWorks HTML, browser-UA requests Session
# ----------------------------------------------------------------------

def _duke_session() -> requests.Session:
    """A requests.Session with a browser UA for Duke only.

    The SNWorks WAF (server: awselb/2.0) returns HTTP 403 for our honest
    research UA on every path (robots.txt included), so we identify as a browser
    to read publicly available article pages. The shared Fetcher is NOT modified;
    this override is scoped to the Duke module, and we keep the same politeness
    delays via ``_polite_sleep``.
    """
    session = requests.Session()
    session.headers.update({"User-Agent": BROWSER_UA})
    return session


def _discover_duke(config: dict, fetcher: "Fetcher") -> Iterable[dict]:
    """Phase 1: walk the /section/news listing (+ pagination) for article links."""
    base_url = config.get("base_url", "https://www.dukechronicle.com")
    section_url = config.get("section_url") or f"{base_url}/section/news"
    max_articles = config.get("max_articles", 100)
    max_pages = config.get("max_pages", 5)

    session = _duke_session()
    results: list[dict] = []
    seen: set[str] = set()
    next_url: str | None = section_url
    pages = 0

    while next_url and pages < max_pages and len(results) < max_articles:
        _polite_sleep(config)
        try:
            resp = session.get(next_url, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("Duke discovery failed for %s: %s", next_url, exc)
            break

        soup = make_soup(resp.text)
        # SNWorks article URLs look like /article/<slug>-YYYYMMDD.
        for a in soup.select("a[href^='/article/']"):
            href = a.get("href", "")
            if not href:
                continue
            full = urljoin(base_url, href)
            if full in seen:
                continue
            seen.add(full)
            results.append(
                {
                    "url": full,
                    "title": clean_text(a.get_text(" ")),
                    "author": "",
                    "publication_date": "",
                    "section": "",
                }
            )
            if len(results) >= max_articles:
                break

        # Pagination lives in <ol class="index-pagination"> as a "Next" anchor.
        next_url = None
        pagination = soup.select_one("ol.index-pagination")
        if pagination:
            for a in pagination.select("a[href]"):
                if "next" in a.get_text(" ").strip().lower():
                    next_url = urljoin(base_url, a["href"])
                    break
        pages += 1

    return results[:max_articles]


def _extract_text_duke(url: str, fetcher: "Fetcher") -> dict:
    """Phase 2: fetch a Duke article (browser UA) -> text + author + date."""
    session = _duke_session()
    # Politeness: same spirit as the shared Fetcher's randomized delay.
    time.sleep(random.uniform(1.0, 3.0))
    try:
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Duke article fetch failed for %s: %s", url, exc)
        return _empty_page()

    soup = make_soup(resp.text)
    # Body container: div.article-content (within .full-article.prose).
    body = soup.select_one(".article-content")
    if body is None:
        logger.warning("Duke body container not found for %s", url)
        text = ""
    else:
        text = clean_text(" ".join(p.get_text(" ") for p in body.find_all("p")))

    # Author byline: .article--byline (strip a leading "By ").
    byline = soup.select_one(".article--byline")
    author = clean_text(byline.get_text(" ")) if byline else ""
    author = re.sub(r"^by\s+", "", author, flags=re.IGNORECASE)

    # Date: first <time datetime="..."> on the page; fallback to URL suffix.
    time_el = soup.select_one("time[datetime]")
    pub = time_el.get("datetime", "") if time_el else ""
    if not pub:
        m = re.search(r"-(\d{8})$", url)  # trailing -YYYYMMDD in SNWorks URLs
        if m:
            pub = m.group(1)

    return {"text": text, "author": author, "publication_date": pub}


def extract_duke(config: dict, fetcher: "Fetcher") -> Iterator[Article]:
    """Yield Duke Chronicle Article records (HTML listing + full-text fetch)."""
    institution = "The Chronicle (Duke University)"
    max_articles = config.get("max_articles", 100)
    count = 0
    for meta in _discover_duke(config, fetcher):
        if count >= max_articles:
            break
        url = meta.get("url", "")
        if not url:
            continue
        page = _extract_text_duke(url, fetcher)
        text = page.get("text", "")
        if not text:
            logger.warning("Skipping Duke article with empty body: %s", url)
            continue
        raw_date = page.get("publication_date") or meta.get("publication_date", "")
        yield Article(
            institution=institution,
            title=meta.get("title", ""),
            author=clean_text(page.get("author", "") or meta.get("author", "")),
            publication_date=normalize_date(raw_date, url),
            section=meta.get("section", ""),
            url=url,
            text=text,
            scraped_at=datetime.now(timezone.utc).isoformat(),
        )
        count += 1


# ----------------------------------------------------------------------
# Yale (Yale Daily News) -- Playwright render past Vercel checkpoint
# ----------------------------------------------------------------------

# Markers that indicate we got the Vercel bot-detection page, not content.
_CHECKPOINT_MARKERS = (
    "Vercel Security Checkpoint",
    "Enable JavaScript to continue",
    "We're verifying your browser",
)


def _is_checkpoint(html: str) -> bool:
    return any(marker in html for marker in _CHECKPOINT_MARKERS)


def _render_yale(url: str) -> str | None:
    """Render ``url`` with headless Chromium to clear the JS checkpoint.

    Falls back to a browser-UA requests GET if Playwright is unavailable or the
    nav fails. Returns rendered HTML, or None if still challenged/blocked.
    """
    # Keep Playwright's browser binaries inside the gitignored project folder.
    os.environ.setdefault(
        "PLAYWRIGHT_BROWSERS_PATH", str(PROJECT_ROOT / "playwright-browsers")
    )

    html: str | None = None
    try:
        from playwright.sync_api import sync_playwright  # lazy import

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=BROWSER_UA)
            page = context.new_page()
            page.goto(url, wait_until="networkidle", timeout=30000)
            # Poll for the checkpoint to clear (it self-resolves after JS runs).
            html = page.content()
            for _ in range(6):
                if not _is_checkpoint(html):
                    break
                page.wait_for_timeout(1500)
                html = page.content()
            browser.close()
    except Exception as exc:  # Playwright missing/broken, or navigation failed.
        logger.warning("Playwright render failed for %s: %s", url, exc)
        html = None

    if html is not None and not _is_checkpoint(html):
        return html

    # Fallback: plain requests with a browser UA (often still challenged).
    try:
        resp = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=20)
        if resp.status_code == 200 and not _is_checkpoint(resp.text):
            return resp.text
        logger.warning("Yale still behind Vercel checkpoint for %s", url)
    except requests.RequestException as exc:
        logger.warning("Yale fallback fetch failed for %s: %s", url, exc)
    return None


# Defensive: a date-stamped path is the strongest signal of an article URL on a
# custom CMS whose exact pattern is JS-gated (e.g. /YYYY/MM/DD/slug/).
_YALE_ARTICLE_PATH = re.compile(r"/20\d{2}/\d{2}/\d{2}/[^/]")


def _discover_yale(config: dict, fetcher: "Fetcher") -> Iterable[dict]:
    """Phase 1: render listing pages and collect date-stamped article links."""
    base_url = config.get("base_url", "https://yaledailynews.com")
    discovery_urls = config.get("discovery_urls") or [f"{base_url}/"]
    max_articles = config.get("max_articles", 100)

    results: list[dict] = []
    seen: set[str] = set()
    for durl in discovery_urls:
        if len(results) >= max_articles:
            break
        _polite_sleep(config)
        html = _render_yale(durl)
        if not html:
            logger.warning("Yale discovery blocked/empty for %s", durl)
            continue
        soup = make_soup(html)
        for a in soup.find_all("a", href=True):
            full = urljoin(base_url, a["href"])
            if not _YALE_ARTICLE_PATH.search(full):
                continue
            if full in seen:
                continue
            seen.add(full)
            results.append(
                {
                    "url": full,
                    "title": clean_text(a.get_text(" ")),
                    "author": "",
                    "publication_date": "",
                    "section": "",
                }
            )
            if len(results) >= max_articles:
                break
    return results[:max_articles]


def _extract_text_yale(url: str, fetcher: "Fetcher") -> dict:
    """Phase 2: render an article and return text + author + date."""
    html = _render_yale(url)
    if not html:
        return _empty_page()
    soup = make_soup(html)

    # Try candidate body containers in order; custom CMS, so be defensive.
    body = None
    for selector in ("article", ".article-content", ".entry-content", ".post-content", "main"):
        candidate = soup.select_one(selector)
        if candidate and candidate.find_all("p"):
            body = candidate
            break
    if body is None:
        logger.warning("Yale body container not found for %s", url)
        return _empty_page()
    text = clean_text(" ".join(p.get_text(" ") for p in body.find_all("p")))

    # Byline candidates (class/rel vary on a custom CMS).
    byline = soup.select_one(".byline, .author, [rel='author'], .article-author")
    author = clean_text(byline.get_text(" ")) if byline else ""
    author = re.sub(r"^by\s+", "", author, flags=re.IGNORECASE)

    time_el = soup.select_one("time[datetime]")
    pub = time_el.get("datetime", "") if time_el else ""
    return {"text": text, "author": author, "publication_date": pub}


def extract_yale(config: dict, fetcher: "Fetcher") -> Iterator[Article]:
    """Yield Yale Daily News Article records (Playwright render + full text).

    If Yale's Vercel checkpoint persists under headless automation, discovery
    yields nothing and this generator produces zero articles -- a documented
    result, not a failure (see recon/RECON.md and README).
    """
    institution = "Yale Daily News"
    max_articles = config.get("max_articles", 100)
    count = 0
    for meta in _discover_yale(config, fetcher):
        if count >= max_articles:
            break
        url = meta.get("url", "")
        if not url:
            continue
        page = _extract_text_yale(url, fetcher)
        text = page.get("text", "")
        if not text:
            logger.warning("Skipping Yale article with empty body: %s", url)
            continue
        raw_date = page.get("publication_date") or meta.get("publication_date", "")
        yield Article(
            institution=institution,
            title=meta.get("title", ""),
            author=clean_text(page.get("author", "") or meta.get("author", "")),
            publication_date=normalize_date(raw_date, url),
            section=meta.get("section", ""),
            url=url,
            text=text,
            scraped_at=datetime.now(timezone.utc).isoformat(),
        )
        count += 1


# ----------------------------------------------------------------------
# Registry: site key -> extraction generator
# ----------------------------------------------------------------------

SITE_EXTRACTORS = {
    "duke": extract_duke,
    "yale": extract_yale,
    "northwestern": extract_northwestern,
}
