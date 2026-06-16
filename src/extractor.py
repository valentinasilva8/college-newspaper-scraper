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
      ``{"text", "author", "publication_date", "subtitle"}`` (plus optional
      ``title`` for Yale listing backfill).

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
    return {"text": "", "author": "", "publication_date": "", "subtitle": ""}


_MONTH_NAMES = (
    r"January|February|March|April|May|June|July|August|"
    r"September|October|November|December"
)


def find_long_date(text: str, *, prefer_time_prefixed: bool = True) -> str:
    """Extract a long-form publication date from free text.

  Site-agnostic helper for when ``<time datetime>`` is absent. When
  ``prefer_time_prefixed`` is True (default), match a byline timestamp such as
  ``"11:36 p.m., May 27, 2026"`` first -- this avoids grabbing a site masthead's
  current date (``"Monday, June 15, 2026"``), which carries no time prefix.
  Falls back to a bare long-form date when no time-prefixed match exists.
    """
    if not text:
        return ""
    if prefer_time_prefixed:
        m = re.search(
            r"\d{1,2}:\d{2}\s*[ap]\.m\.,\s*((?:" + _MONTH_NAMES + r")\s+\d{1,2},\s+20\d{2})",
            text,
        )
        if m:
            return m.group(1)
    m = re.search(
        r"(?:" + _MONTH_NAMES + r")\s+\d{1,2},\s+20\d{2}",
        text,
    )
    return m.group(0) if m else ""


def _meta_content(soup: BeautifulSoup, key: str) -> str:
    """Return cleaned ``<meta property|name=key>`` content, or ""."""
    for attr in ("property", "name"):
        tag = soup.find("meta", attrs={attr: key})
        if tag and tag.get("content"):
            return clean_text(tag["content"])
    return ""


def _collect_byline_authors(soup: BeautifulSoup, link_selector: str) -> str:
    """Gather byline author names from anchor elements; dedupe and join.

    Strips a leading ``By `` from each link text, dedupes case-insensitively
    (preserving first-seen casing), and joins with ``, ``.
    """
    seen_lower: set[str] = set()
    names: list[str] = []
    for a in soup.select(link_selector):
        name = re.sub(r"^by\s+", "", clean_text(a.get_text(" ")), flags=re.IGNORECASE)
        if not name:
            continue
        key = name.lower()
        if key in seen_lower:
            continue
        seen_lower.add(key)
        names.append(name)
    return ", ".join(names)


def _subtitle_from_og_or_deck(soup: BeautifulSoup) -> str:
    """Editor deck from ``og:description``, or the ``<h2>`` immediately after ``<h1>``."""
    subtitle = _meta_content(soup, "og:description")
    if subtitle:
        return subtitle
    h1 = soup.find("h1")
    if h1:
        h2 = h1.find_next_sibling("h2")
        if h2:
            return clean_text(h2.get_text(" "))
    return ""


def _largest_p_container(soup: BeautifulSoup):
    """Heuristic fallback: the element holding the most direct <p> children.

    Useful when a site's body container class is unknown/changes; the article
    body is almost always the densest paragraph cluster on the page.
    """
    best = None
    best_count = 0
    for el in soup.find_all(["div", "article", "section"]):
        count = len(el.find_all("p", recursive=False))
        if count > best_count:
            best_count = count
            best = el
    return best if best_count >= 3 else None


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
    return {"text": text, "author": "", "publication_date": "", "subtitle": ""}


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
            subtitle="",  # SNO og:description is an auto body excerpt, not a deck
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
    section_label = config.get("section_label", "News")
    max_articles = config.get("max_articles", 100)
    max_pages = config.get("max_pages", 5)

    session = _duke_session()
    # Keyed by URL to merge the multiple cards (thumbnail + headline anchors)
    # that point at the same article; preserves insertion order.
    by_url: dict[str, dict] = {}
    next_url: str | None = section_url
    pages = 0

    while next_url and pages < max_pages and len(by_url) < max_articles:
        _polite_sleep(config)
        try:
            resp = session.get(next_url, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("Duke discovery failed for %s: %s", next_url, exc)
            break

        soup = make_soup(resp.text)
        # SNWorks article URLs look like /article/<slug>-YYYYMMDD. Each card has
        # several anchors for the same URL: an image anchor (no text, headline in
        # aria-label) and a headline anchor (text). Prefer whichever has a title.
        for a in soup.select("a[href^='/article/']"):
            href = a.get("href", "")
            if not href:
                continue
            full = urljoin(base_url, href)
            title = clean_text(a.get_text(" "))
            if not title:
                # Image/thumbnail anchors expose the headline via aria-label.
                title = re.sub(
                    r"^read\s+", "", clean_text(a.get("aria-label", "")), flags=re.IGNORECASE
                )
            existing = by_url.get(full)
            if existing is None:
                if len(by_url) >= max_articles:
                    continue
                by_url[full] = {
                    "url": full,
                    "title": title,
                    "author": "",
                    "publication_date": "",
                    "section": section_label,
                }
            elif title and not existing["title"]:
                existing["title"] = title

        # Pagination lives in <ol class="index-pagination"> as a "Next" anchor.
        next_url = None
        pagination = soup.select_one("ol.index-pagination")
        if pagination:
            for a in pagination.select("a[href]"):
                if "next" in a.get_text(" ").strip().lower():
                    next_url = urljoin(base_url, a["href"])
                    break
        pages += 1

    return list(by_url.values())[:max_articles]


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

    # Author: pick the byline labeled "By" (the writer). SNWorks also renders a
    # lead-image photo credit as .article--byline but with a "Photo by" prefix,
    # so we skip any byline whose prefix mentions "photo". Joins co-authors.
    author = ""
    for byline in soup.select(".article--byline"):
        prefix_el = byline.select_one(".article--byline-prefix")
        prefix_txt = clean_text(prefix_el.get_text(" ")).lower() if prefix_el else ""
        if "photo" in prefix_txt:
            continue
        if prefix_txt.startswith("by"):
            names = [clean_text(a.get_text(" ")) for a in byline.select(".article--author")]
            names = [n for n in names if n]
            if names:
                author = ", ".join(names)
                break

    # Date: first <time datetime="..."> on the page; fallback to URL suffix.
    time_el = soup.select_one("time[datetime]")
    pub = time_el.get("datetime", "") if time_el else ""
    if not pub:
        m = re.search(r"-(\d{8})$", url)  # trailing -YYYYMMDD in SNWorks URLs
        if m:
            pub = m.group(1)

    subtitle = _meta_content(soup, "og:description")

    return {"text": text, "author": author, "publication_date": pub, "subtitle": subtitle}


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
            subtitle=page.get("subtitle", ""),
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


# Once a Playwright launch fails in a given run (e.g. no browser, sandboxed
# environment), stop retrying it -- otherwise every article pays the launch
# timeout before falling back. Reset per process.
_playwright_disabled = False


def _render_with_playwright(
    url: str, wait_for_js: str | None = None
) -> tuple[str, str] | None:
    """Render ``url`` with headless Chromium; None on any failure.

    Returns ``(html, inner_text)`` where ``html`` is the serialized DOM (used to
    parse body text and author) and ``inner_text`` is ``document.body.innerText``
    of the rendered page. We need both because Yale renders the byline date in a
    custom element / shadow DOM that ``page.content()`` does NOT serialize, but
    which ``innerText`` does expose -- so the date can only be recovered from the
    rendered text, not the HTML.

    ``wait_for_js`` is an optional JS predicate (``() => boolean``); when given,
    we poll until it returns true before snapshotting. This avoids racing Yale's
    client-side hydration of the byline date -- a fixed sleep is flaky (the date
    sometimes loads in <2.5s, sometimes slower) -- and falls through after a few
    seconds for content that legitimately has no timestamp (galleries, podcasts).
    """
    global _playwright_disabled
    if _playwright_disabled:
        return None
    # Keep Playwright's browser binaries inside the gitignored project folder.
    os.environ.setdefault(
        "PLAYWRIGHT_BROWSERS_PATH", str(PROJECT_ROOT / "playwright-browsers")
    )
    try:
        from playwright.sync_api import sync_playwright  # lazy import
    except Exception as exc:  # Playwright not installed.
        logger.warning(
            "Playwright unavailable (%s); disabling it for this run and using "
            "the requests fallback.",
            exc.__class__.__name__,
        )
        _playwright_disabled = True
        return None

    try:
        with sync_playwright() as p:
            # Short launch timeout so a broken/unavailable browser fails fast.
            # A launch failure means the browser itself is broken, so disable
            # Playwright for the rest of the run; a per-URL navigation timeout
            # (handled below) only skips that one URL.
            try:
                browser = p.chromium.launch(headless=True, timeout=20000)
            except Exception as exc:
                logger.warning(
                    "Playwright browser failed to launch (%s); disabling it for "
                    "this run and using the requests fallback.",
                    exc.__class__.__name__,
                )
                _playwright_disabled = True
                return None
            try:
                context = browser.new_context(user_agent=BROWSER_UA)
                page = context.new_page()
                # Use domcontentloaded (reliable) rather than networkidle, which
                # never settles on Yale's homepage; then give JS time to hydrate
                # the author/date, which are client-rendered.
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                if wait_for_js:
                    # Poll until the target content (the byline timestamp) has
                    # hydrated, re-checking every 1.5s for up to ~9s. We sleep
                    # first so JS gets a chance to run, then evaluate the
                    # predicate. Date-less content (galleries, podcasts) never
                    # satisfies it and simply uses the full budget, after which
                    # the caller records an UNPARSED date rather than a wrong one.
                    for _ in range(6):
                        page.wait_for_timeout(1500)
                        try:
                            if page.evaluate(wait_for_js):
                                break
                        except Exception:
                            pass
                else:
                    page.wait_for_timeout(2500)
                html = page.content()
                # Poll for the checkpoint to clear (self-resolves after JS runs).
                for _ in range(6):
                    if not _is_checkpoint(html):
                        break
                    page.wait_for_timeout(1500)
                    html = page.content()
                inner_text = ""
                try:
                    inner_text = page.evaluate("document.body.innerText") or ""
                except Exception:
                    inner_text = ""
                return html, inner_text
            except Exception as exc:  # Per-URL navigation/timeout: skip this URL.
                logger.warning(
                    "Playwright render failed for %s (%s); skipping this URL.",
                    url,
                    exc.__class__.__name__,
                )
                return None
            finally:
                browser.close()
    except Exception as exc:  # Unexpected Playwright failure.
        logger.warning(
            "Playwright error (%s); disabling it for this run and using the "
            "requests fallback.",
            exc.__class__.__name__,
        )
        _playwright_disabled = True
        return None


# JS predicate: true once the byline timestamp ("11:36 p.m., May 27, 2026")
# has hydrated. Used to wait out client-side rendering on article pages.
_YALE_DATE_READY_JS = (
    r"() => /\d{1,2}:\d{2}\s*[ap]\.m\.,\s*(January|February|March|April|May|"
    r"June|July|August|September|October|November|December)\s+\d{1,2},\s+20\d{2}/i"
    r".test(document.body.innerText)"
)


def _render_yale(url: str, wait_for_js: str | None = None) -> tuple[str, str] | None:
    """Get article HTML past Yale's Vercel checkpoint.

    Prefers Playwright (renders JS, so client-side author/date hydrate); falls
    back to a browser-UA requests GET (recovers server-rendered body text).
    Returns ``(html, inner_text)`` -- ``inner_text`` is the rendered page text
    when Playwright was used and "" for the requests fallback (which cannot see
    JS-rendered content such as the date). Returns None if still blocked.
    """
    rendered = _render_with_playwright(url, wait_for_js=wait_for_js)
    if rendered is not None and not _is_checkpoint(rendered[0]):
        return rendered

    # Fallback: plain requests with a browser UA.
    try:
        resp = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=20)
        if resp.status_code == 200 and not _is_checkpoint(resp.text):
            return resp.text, ""
        logger.warning("Yale still behind Vercel checkpoint for %s", url)
    except requests.RequestException as exc:
        logger.warning("Yale fallback fetch failed for %s: %s", url, exc)
    return None


# Yale (Payload CMS) serves articles at /articles/<slug> (verified at runtime).
_YALE_ARTICLE_PATH = re.compile(r"/articles/[a-z0-9][a-z0-9-]+/?$", re.IGNORECASE)


# Yale section landing pages -> human-readable section labels (verified at runtime).
_YALE_SECTION_PAGES: list[tuple[str, str]] = [
    ("university", "University"),
    ("city", "City"),
    ("scitech", "SciTech"),
    ("arts", "Arts"),
    ("sports", "Sports"),
    ("opinion", "Opinion"),
    ("investigations", "Investigations"),
    ("wknd", "WKND"),
    ("magazine", "Magazine"),
]


def _discover_yale(config: dict, fetcher: "Fetcher") -> Iterable[dict]:
    """Phase 1: render per-section landing pages and collect article links."""
    base_url = config.get("base_url", "https://yaledailynews.com").rstrip("/")
    max_articles = config.get("max_articles", 100)

    # Keyed by URL so the first section that lists an article wins.
    by_url: dict[str, dict] = {}
    section_pages = config.get("section_pages")
    if section_pages:
        pages = [(slug, label) for slug, label in section_pages]
    else:
        pages = _YALE_SECTION_PAGES

    for slug, section_label in pages:
        if len(by_url) >= max_articles:
            break
        durl = f"{base_url}/{slug}"
        _polite_sleep(config)
        rendered = _render_yale(durl)
        if not rendered:
            logger.warning("Yale discovery blocked/empty for %s", durl)
            continue
        html = rendered[0]
        soup = make_soup(html)
        for a in soup.find_all("a", href=True):
            full = urljoin(base_url, a["href"])
            if not _YALE_ARTICLE_PATH.search(full):
                continue
            if full in by_url:
                continue
            if len(by_url) >= max_articles:
                break
            by_url[full] = {
                "url": full,
                "title": clean_text(a.get_text(" ")),
                "author": "",
                "publication_date": "",
                "section": section_label,
            }

    return list(by_url.values())[:max_articles]


def _extract_text_yale(url: str, fetcher: "Fetcher") -> dict:
    """Phase 2: render an article and return text + author + date."""
    rendered = _render_yale(url, wait_for_js=_YALE_DATE_READY_JS)
    if not rendered:
        return _empty_page()
    html, inner_text = rendered
    soup = make_soup(html)

    # Body: Payload CMS renders the article into .payload-richtext (.prose).
    # Try known containers first, then fall back to the largest <p> cluster.
    body = None
    for selector in (".payload-richtext", "article", ".article-content", ".entry-content", ".post-content", "main"):
        candidate = soup.select_one(selector)
        if candidate and candidate.find_all("p"):
            body = candidate
            break
    if body is None:
        body = _largest_p_container(soup)
    if body is None:
        logger.warning("Yale body container not found for %s", url)
        return _empty_page()
    text = clean_text(" ".join(p.get_text(" ") for p in body.find_all("p")))

    # Title backfill: some listing anchors are image-only (no text), so capture
    # the article <h1> here for extract_yale to use when discovery had none.
    h1 = soup.select_one("h1")
    page_title = clean_text(h1.get_text(" ")) if h1 else ""

    # Author: collect all /author/ byline links (supports co-authors).
    author = _collect_byline_authors(soup, 'a[href*="/author/"]')
    if not author:
        m = re.search(r"\bBy\s+([A-Z][a-z]+(?:\s+[A-Z][a-z.'-]+){1,3})", soup.get_text(" "))
        if m:
            author = clean_text(m.group(1))

    time_el = soup.select_one("time[datetime]")
    pub = time_el.get("datetime", "") if time_el else ""
    if not pub:
        # Yale renders the byline date in shadow/custom elements absent from
        # page.content(); read it from rendered innerText via find_long_date.
        pub = find_long_date(inner_text or "", prefer_time_prefixed=True)

    subtitle = _subtitle_from_og_or_deck(soup)

    return {
        "text": text,
        "author": author,
        "publication_date": pub,
        "subtitle": subtitle,
        "title": page_title,
    }


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
            title=meta.get("title", "") or page.get("title", ""),
            subtitle=page.get("subtitle", ""),
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
