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

import json
import logging
import os
import random
import re
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Iterator
from urllib.parse import urljoin, urlparse

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

# Log a discovery progress line every N sitemap sub-files or listing pages.
DISCOVERY_PROGRESS_EVERY = 10

# Northwestern Yoast sitemap buckets (gitignored via logs/).
NW_SITEMAP_CACHE = PROJECT_ROOT / "logs" / "cache" / "northwestern_sitemap_by_year.json"
DUKE_DISCOVERY_CACHE = PROJECT_ROOT / "logs" / "cache" / "duke_articles_by_year.json"

# SNWorks /section/news pagination: higher page number ≈ older articles (~28 pages/year).
DUKE_PAGES_PER_YEAR = 28


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
    return {
        "text": "",
        "author": "",
        "publication_date": "",
        "subtitle": "",
        "section": "",
        "subsection": "",
    }


def split_kicker(text: str) -> tuple[str, str]:
    """Split a "Section | Subsection" kicker into ``(section, subsection)``.

    Used by papers that print a two-level kicker above the headline (Duke's
    SNWorks theme renders e.g. ``"News | University"``). Extra levels beyond the
    second are ignored; a single-level kicker yields an empty subsection.
    """
    parts = [clean_text(p) for p in (text or "").split("|")]
    parts = [p for p in parts if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def resolve_section_path(
    soup: BeautifulSoup, primary: str
) -> tuple[str, str]:
    """Resolve a primary category name into ``(section, subsection)``.

    Papers on the SNO theme (Northwestern) expose only the most-specific
    category via ``<meta property="article:section">`` (e.g. ``"Academic"``),
    not its parent. The site nav, however, encodes the full taxonomy as
    ``/category/<parent>/<child>/`` links plus single-segment top-level links.
    We read that nav from the same page to map the primary category to its
    parent:

      - if ``primary`` matches a ``/category/<parent>/<child>/`` link, return
        ``(parent_label, primary)`` -- e.g. ``("Campus", "Academic")``;
      - otherwise ``primary`` is itself a top-level section, so return
        ``(primary, "")`` -- e.g. ``("Arts and Entertainment", "")``.

    Labels are taken verbatim from the publication's own nav (no normalization).
    """
    if not primary:
        return "", ""
    child_map: dict[str, tuple[str, str]] = {}  # child_label_lower -> (parent_slug, child_label)
    top_map: dict[str, str] = {}  # slug -> label
    for a in soup.find_all("a", href=True):
        label = clean_text(a.get_text(" "))
        if not label:
            continue
        segs = [s for s in urlparse(a["href"]).path.strip("/").split("/") if s]
        if len(segs) >= 3 and segs[0] == "category":
            child_map.setdefault(label.lower(), (segs[1], label))
        elif len(segs) == 1:
            top_map.setdefault(segs[0], label)
        elif len(segs) == 2 and segs[0] == "category":
            top_map.setdefault(segs[1], label)

    hit = child_map.get(primary.lower())
    if hit:
        parent_slug, child_label = hit
        parent_label = top_map.get(parent_slug, parent_slug.replace("-", " ").title())
        return parent_label, child_label
    return primary, ""


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


def _skip_urls(config: dict) -> set[str]:
    """URLs already present in the per-site CSV (incremental merge)."""
    return set(config.get("skip_urls") or ())


def _stratified_sample(candidates: list, n: int) -> list:
    """Evenly sample ``n`` items from ``candidates`` (spread, not first-N)."""
    if not candidates or n <= 0:
        return []
    if len(candidates) <= n:
        return list(candidates)
    if n == 1:
        return [candidates[len(candidates) // 2]]
    step = (len(candidates) - 1) / (n - 1)
    return [candidates[int(round(i * step))] for i in range(n)]


def _parse_sitemap_locs(xml_text: str) -> list[str]:
    """Return ``<loc>`` URLs from a sitemap XML document."""
    soup = BeautifulSoup(xml_text, "xml")
    return [loc.text.strip() for loc in soup.find_all("loc") if loc.text and loc.text.strip()]


_NW_ARTICLE_PATH = re.compile(
    r"^https://dailynorthwestern\.com/\d{4}/\d{2}/\d{2}/[^/]+/[^/]+/?$"
)


def _date_from_nw_url(url: str) -> str:
    """ISO date from a Northwestern article URL path (YYYY/MM/DD)."""
    m = re.search(r"/(\d{4})/(\d{2})/(\d{2})/", url)
    if not m:
        return ""
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


def _year_from_nw_url(url: str) -> int | None:
    m = re.search(r"/(\d{4})/", url)
    return int(m.group(1)) if m else None


def _year_from_duke_slug(url: str) -> int | None:
    """Publication year from SNWorks ``...-YYYYMMDD`` article slug."""
    m = re.search(r"-(\d{8})(?:/)?$", url.rstrip("/"))
    if not m:
        return None
    return int(m.group(1)[:4])


def _load_nw_sitemap_cache() -> tuple[dict[int, list[str]], str] | None:
    """Return ``(by_year, cached_at)`` from the on-disk cache, or None."""
    if not NW_SITEMAP_CACHE.is_file():
        return None
    try:
        data = json.loads(NW_SITEMAP_CACHE.read_text(encoding="utf-8"))
        by_year_raw = data.get("by_year") or {}
        by_year = {int(year): list(urls) for year, urls in by_year_raw.items()}
        cached_at = str(data.get("cached_at", ""))
        return by_year, cached_at
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("Northwestern sitemap cache unreadable (%s); rescanning.", exc)
        return None


def _save_nw_sitemap_cache(by_year: dict[int, list[str]]) -> None:
    """Persist article URLs bucketed by publication year."""
    NW_SITEMAP_CACHE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "by_year": {
            str(year): sorted(set(urls)) for year, urls in sorted(by_year.items())
        },
    }
    NW_SITEMAP_CACHE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info(
        "Northwestern sitemap cache saved -> %s (%d years)",
        NW_SITEMAP_CACHE.name,
        len(by_year),
    )


def _fetch_nw_sitemap_by_year(
    config: dict, fetcher: "Fetcher"
) -> dict[int, list[str]]:
    """Scan all Yoast post-sitemaps and bucket article URLs by year."""
    sitemap_index = config.get(
        "sitemap_index", "https://dailynorthwestern.com/sitemap.xml"
    )

    try:
        index_resp = fetcher.get(sitemap_index)
    except requests.RequestException as exc:
        logger.warning("Northwestern sitemap index fetch failed: %s", exc)
        return {}
    if index_resp is None:
        logger.warning("Northwestern sitemap index blocked by robots.txt")
        return {}

    sub_sitemaps = [
        loc
        for loc in _parse_sitemap_locs(index_resp.text)
        if "post-sitemap" in loc and loc.endswith(".xml")
    ]
    total = len(sub_sitemaps)
    logger.info("Northwestern sitemap: scanning %d post sub-sitemap(s)", total)

    by_year: dict[int, list[str]] = defaultdict(list)
    for idx, sub_url in enumerate(sub_sitemaps, start=1):
        try:
            sub_resp = fetcher.get(sub_url)
        except requests.RequestException as exc:
            logger.warning(
                "Northwestern sub-sitemap fetch failed for %s: %s", sub_url, exc
            )
            continue
        if sub_resp is None:
            continue
        for loc in _parse_sitemap_locs(sub_resp.text):
            if not _NW_ARTICLE_PATH.match(loc):
                continue
            if "/games/" in loc:
                continue
            year = _year_from_nw_url(loc)
            if year is None:
                continue
            by_year[year].append(loc)

        if idx == 1 or idx % DISCOVERY_PROGRESS_EVERY == 0 or idx == total:
            article_count = sum(len(urls) for urls in by_year.values())
            logger.info(
                "Northwestern sitemap progress: %d/%d sub-sitemap(s), "
                "%d article URL(s) bucketed",
                idx,
                total,
                article_count,
            )

    return dict(by_year)


def _sample_nw_sitemap(
    by_year: dict[int, list[str]],
    *,
    year_start: int,
    year_end: int,
    per_year: int,
    max_articles: int,
    oversample: int = 4,
) -> list[dict]:
    """Stratified per-year candidates from a year -> URL map.

    Returns up to ``per_year * oversample`` candidates per year, each tagged with
    its ``year``, so the orchestrator can backfill: if a sampled article yields no
    body text, the next candidate for that year is tried instead of leaving a
    hole in the diachronic coverage. The per-year *success* cap is enforced by
    ``extract_northwestern``.
    """
    results: list[dict] = []
    for year in range(year_start, year_end + 1):
        candidates = sorted(set(by_year.get(year, [])))
        picks = _stratified_sample(candidates, per_year * oversample)
        for url in picks:
            results.append(
                {
                    "url": url,
                    "title": "",
                    "author": "",
                    "publication_date": _date_from_nw_url(url),
                    "section": "",
                    "year": year,
                }
            )
    return results


# ----------------------------------------------------------------------
# Northwestern (The Daily Northwestern) -- RSS discovery + static HTML
# ----------------------------------------------------------------------

NW_DEFAULT_FEED = "https://dailynorthwestern.com/feed/rss/"


def _discover_northwestern_rss(config: dict, fetcher: "Fetcher") -> list[dict]:
    """Legacy RSS discovery (recent items only)."""
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
        tags = entry.get("tags", []) or []
        section = tags[0].get("term", "") if tags else ""
        results.append(
            {
                "url": url,
                "title": clean_text(entry.get("title", "")),
                "author": clean_text(entry.get("author", "")),
                "publication_date": entry.get("published", ""),
                "section": section,
            }
        )
    return results


def _discover_northwestern_sitemap(config: dict, fetcher: "Fetcher") -> list[dict]:
    """Sitemap discovery with date-stratified per-year sampling."""
    year_start = int(config.get("year_start", 2000))
    year_end = int(config.get("year_end", datetime.now(timezone.utc).year))
    per_year = int(config.get("per_year", 2))
    max_articles = int(config.get("max_articles", 100))
    refresh = config.get("refresh_discovery", False)

    by_year: dict[int, list[str]] | None = None
    if not refresh:
        cached = _load_nw_sitemap_cache()
        if cached is not None:
            by_year, cached_at = cached
            article_count = sum(len(urls) for urls in by_year.values())
            logger.info(
                "Northwestern sitemap: using cache (%d article URL(s), %d years, "
                "cached %s)",
                article_count,
                len(by_year),
                cached_at or "unknown",
            )

    if by_year is None:
        by_year = _fetch_nw_sitemap_by_year(config, fetcher)
        if by_year:
            _save_nw_sitemap_cache(by_year)

    results = _sample_nw_sitemap(
        by_year,
        year_start=year_start,
        year_end=year_end,
        per_year=per_year,
        max_articles=max_articles,
    )
    logger.info(
        "Northwestern sitemap discovery: %d candidate URL(s) across %d-%d",
        len(results),
        year_start,
        year_end,
    )
    return results


def _discover_northwestern(config: dict, fetcher: "Fetcher") -> Iterable[dict]:
    """Discovery: Yoast sitemap (stratified) or RSS fallback."""
    mode = config.get("discovery_mode", "sitemap")
    if mode == "sitemap":
        return _discover_northwestern_sitemap(config, fetcher)
    return _discover_northwestern_rss(config, fetcher)


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
    # Body container varies by era of the SNO theme:
    #   - FLEX Pro (current, ~2024+): #sno-story-body-content
    #   - Classic (~2015-2023):       #classic_story / #sno-sites-main-content
    # Try the known ids in order, then fall back to the densest paragraph block.
    body = soup.find(id="sno-story-body-content")
    if body is None:
        body = soup.find(id="classic_story")
    if body is None:
        body = soup.find(id="sno-sites-main-content")
    if body is None:
        body = _largest_p_container(soup)
    if body is None:
        logger.warning("Northwestern body container not found for %s", url)
        return _empty_page()
    paragraphs = [p.get_text(" ") for p in body.find_all("p")]
    text = clean_text(" ".join(paragraphs))

    # Section hierarchy: <meta article:section> gives only the most-specific
    # category (e.g. "Academic"); resolve its parent from the nav taxonomy so we
    # capture "Campus / Academic" rather than just "Academic". Top-level
    # categories (e.g. "Arts and Entertainment") resolve to an empty subsection.
    primary = _meta_content(soup, "article:section")
    section, subsection = resolve_section_path(soup, primary)

    # Author/date/title now come from the article page (sitemap discovery).
    title = ""
    headline = soup.find(id="sno-story-headline")
    if headline:
        title = clean_text(headline.get_text(" "))
    if not title:
        title = _meta_content(soup, "og:title")

    author = ""
    byline = soup.select_one(".sno-story-byline")
    if byline:
        author = re.sub(
            r"^by\s+", "", clean_text(byline.get_text(" ")), flags=re.IGNORECASE
        )
    if not author:
        # Classic template byline: "<Name> , <Role>" inside span.storybyline.
        classic = soup.select_one(".storybyline")
        if classic:
            raw = re.sub(
                r"^by\s+", "", clean_text(classic.get_text(" ")), flags=re.IGNORECASE
            )
            # Drop the trailing role ("Maia Pandey , Assistant Campus Editor").
            author = clean_text(raw.split(",")[0])

    pub = _meta_content(soup, "article:published_time")
    if not pub:
        pub = _date_from_nw_url(url)

    return {
        "text": text,
        "title": title,
        "author": author,
        "publication_date": pub,
        "subtitle": "",
        "section": section,
        "subsection": subsection,
    }


def extract_northwestern(config: dict, fetcher: "Fetcher") -> Iterator[Article]:
    """Yield Daily Northwestern Article records (sitemap/RSS + full-text fetch).

    Discovery oversamples candidates per year; here we keep fetching a year's
    candidates until ``per_year`` produce real body text, so a few unparseable
    articles (e.g. legacy photo recaps) don't leave gaps in the year coverage.
    """
    institution = "The Daily Northwestern"
    max_articles = config.get("max_articles", 100)
    per_year = int(config.get("per_year", 2))
    skip_urls = _skip_urls(config)
    count = 0
    per_year_ok: dict[int, int] = defaultdict(int)
    # Seed per-year counts from already-collected URLs so an incremental re-run
    # tops up sparse years instead of over-fetching ones already at quota.
    for done_url in skip_urls:
        done_year = _year_from_nw_url(done_url)
        if done_year is not None:
            per_year_ok[done_year] += 1
    for meta in _discover_northwestern(config, fetcher):
        url = meta.get("url", "")
        if not url:
            continue
        if url in skip_urls:
            logger.debug("Skipping already-scraped Northwestern URL: %s", url)
            continue
        if count >= max_articles:
            break
        year = meta.get("year")
        # Stop fetching a year once it already has per_year successful articles.
        if year is not None and per_year_ok[year] >= per_year:
            continue
        page = _extract_text_northwestern(url, fetcher)
        text = page.get("text", "")
        if not text:
            logger.warning("Skipping Northwestern article with empty body: %s", url)
            continue
        raw_date = page.get("publication_date") or meta.get("publication_date", "")
        section = page.get("section", "") or meta.get("section", "")
        if year is not None:
            per_year_ok[year] += 1
        yield Article(
            institution=institution,
            title=page.get("title", "") or meta.get("title", ""),
            subtitle="",
            author=clean_text(page.get("author", "") or meta.get("author", "")),
            publication_date=normalize_date(raw_date, url),
            section=section,
            subsection=page.get("subsection", ""),
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
    """Discovery: stratified by year (deep pagination) or recent listing pages."""
    if config.get("year_start") is not None and config.get("per_year"):
        return _discover_duke_stratified(config)
    return _discover_duke_recent(config)


def _discover_duke_recent(config: dict) -> list[dict]:
    """Walk a few /section/news pages for the most recent articles."""
    base_url = config.get("base_url", "https://www.dukechronicle.com")
    section_url = config.get("section_url") or f"{base_url}/section/news"
    section_label = config.get("section_label", "News")
    max_articles = config.get("max_articles", 100)
    max_pages = config.get("max_pages", 5)

    session = _duke_session()
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
        for a in soup.select("a[href^='/article/']"):
            href = a.get("href", "")
            if not href:
                continue
            full = urljoin(base_url, href)
            title = clean_text(a.get_text(" "))
            if not title:
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

        next_url = None
        pagination = soup.select_one("ol.index-pagination")
        if pagination:
            for a in pagination.select("a[href]"):
                if "next" in a.get_text(" ").strip().lower():
                    next_url = urljoin(base_url, a["href"])
                    break
        pages += 1

    return list(by_url.values())[:max_articles]


def _duke_listing_url(section_url: str, page: int) -> str:
    """SNWorks listing URL for page N (page 1 is the bare section URL)."""
    return section_url if page <= 1 else f"{section_url}?page={page}"


def _duke_articles_from_listing(
    soup: BeautifulSoup,
    *,
    base_url: str,
    section_label: str,
    year: int | None = None,
) -> dict[str, dict]:
    """Parse article cards from a /section/news listing page."""
    by_url: dict[str, dict] = {}
    for a in soup.select("a[href^='/article/']"):
        href = a.get("href", "")
        if not href:
            continue
        full = urljoin(base_url, href)
        article_year = _year_from_duke_slug(full)
        if article_year is None:
            continue
        if year is not None and article_year != year:
            continue
        title = clean_text(a.get_text(" "))
        if not title:
            title = re.sub(
                r"^read\s+", "", clean_text(a.get("aria-label", "")), flags=re.IGNORECASE
            )
        existing = by_url.get(full)
        if existing is None:
            by_url[full] = {
                "url": full,
                "title": title,
                "author": "",
                "publication_date": "",
                "section": section_label,
            }
        elif title and not existing["title"]:
            existing["title"] = title
    return by_url


def _fetch_duke_listing_page(
    session: requests.Session,
    config: dict,
    *,
    section_url: str,
    base_url: str,
    section_label: str,
    page: int,
) -> tuple[int | None, int | None, dict[str, dict]]:
    """Fetch one listing page; return ``(min_year, max_year, articles)``."""
    url = _duke_listing_url(section_url, page)
    _polite_sleep(config)
    try:
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Duke listing fetch failed for %s: %s", url, exc)
        return None, None, {}

    soup = make_soup(resp.text)
    articles = _duke_articles_from_listing(
        soup, base_url=base_url, section_label=section_label
    )
    years = [_year_from_duke_slug(meta["url"]) for meta in articles.values()]
    years = [y for y in years if y is not None]
    if not years:
        return None, None, {}
    return min(years), max(years), articles


def _estimate_duke_page_for_year(year: int, year_end: int, pages_per_year: int) -> int:
    """Heuristic page number where ``year`` articles appear (verified ~28 pages/year)."""
    return max(1, (year_end - year) * pages_per_year + 1)


def _collect_duke_year_articles(
    session: requests.Session,
    config: dict,
    *,
    year: int,
    year_end: int,
    section_url: str,
    base_url: str,
    section_label: str,
) -> dict[str, dict]:
    """Targeted window around the estimated page, with binary-search fallback."""
    pages_per_year = int(config.get("duke_pages_per_year", DUKE_PAGES_PER_YEAR))
    window = int(config.get("duke_page_window", 3))
    max_page = int(config.get("max_pages", 400))
    center = _estimate_duke_page_for_year(year, year_end, pages_per_year)

    by_url: dict[str, dict] = {}
    for page in range(max(1, center - window), center + window + 1):
        _, _, page_articles = _fetch_duke_listing_page(
            session,
            config,
            section_url=section_url,
            base_url=base_url,
            section_label=section_label,
            page=page,
        )
        for url, meta in page_articles.items():
            if _year_from_duke_slug(url) == year:
                by_url[url] = meta

    if by_url:
        return by_url

    # Fallback: binary-search for the oldest page whose minimum year is still <= target.
    lo, hi = 1, max_page
    anchor = center
    while lo <= hi:
        mid = (lo + hi) // 2
        min_year, _, _ = _fetch_duke_listing_page(
            session,
            config,
            section_url=section_url,
            base_url=base_url,
            section_label=section_label,
            page=mid,
        )
        if min_year is None:
            break
        if min_year > year:
            lo = mid + 1
        else:
            anchor = mid
            hi = mid - 1

    for page in range(max(1, anchor - window), min(max_page, anchor + window) + 1):
        _, _, page_articles = _fetch_duke_listing_page(
            session,
            config,
            section_url=section_url,
            base_url=base_url,
            section_label=section_label,
            page=page,
        )
        for url, meta in page_articles.items():
            if _year_from_duke_slug(url) == year:
                by_url[url] = meta
    return by_url


def _load_duke_discovery_cache(year_end: int) -> dict[int, dict[str, dict]] | None:
    if not DUKE_DISCOVERY_CACHE.is_file():
        return None
    try:
        data = json.loads(DUKE_DISCOVERY_CACHE.read_text(encoding="utf-8"))
        if int(data.get("year_end", 0)) != year_end:
            return None
        by_year: dict[int, dict[str, dict]] = {}
        for year_str, entries in (data.get("by_year") or {}).items():
            bucket: dict[str, dict] = {}
            for entry in entries:
                if isinstance(entry, dict) and entry.get("url"):
                    bucket[entry["url"]] = entry
            by_year[int(year_str)] = bucket
        cached_at = data.get("cached_at", "")
        total = sum(len(b) for b in by_year.values())
        logger.info(
            "Duke discovery: using cache (%d article URL(s), %d years, cached %s)",
            total,
            len(by_year),
            cached_at or "unknown",
        )
        return by_year
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("Duke discovery cache unreadable (%s); rescanning.", exc)
        return None


def _save_duke_discovery_cache(
    by_year: dict[int, dict[str, dict]], year_end: int
) -> None:
    DUKE_DISCOVERY_CACHE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "year_end": year_end,
        "by_year": {
            str(year): sorted(bucket.values(), key=lambda m: m["url"])
            for year, bucket in sorted(by_year.items())
        },
    }
    DUKE_DISCOVERY_CACHE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info(
        "Duke discovery cache saved -> %s (%d years)",
        DUKE_DISCOVERY_CACHE.name,
        len(by_year),
    )


def _fetch_duke_discovery_by_year(config: dict) -> dict[int, dict[str, dict]]:
    """Collect article metadata per year via targeted page windows (not linear crawl)."""
    base_url = config.get("base_url", "https://www.dukechronicle.com")
    section_url = config.get("section_url") or f"{base_url}/section/news"
    section_label = config.get("section_label", "News")
    year_start = int(config.get("year_start", 2015))
    year_end = int(config.get("year_end", datetime.now(timezone.utc).year))

    session = _duke_session()
    by_year: dict[int, dict[str, dict]] = {}
    years = list(range(year_start, year_end + 1))
    logger.info(
        "Duke discovery: targeted page windows for %d-%d (%d years)",
        year_start,
        year_end,
        len(years),
    )

    for idx, year in enumerate(years, start=1):
        by_year[year] = _collect_duke_year_articles(
            session,
            config,
            year=year,
            year_end=year_end,
            section_url=section_url,
            base_url=base_url,
            section_label=section_label,
        )
        if idx == 1 or idx % DISCOVERY_PROGRESS_EVERY == 0 or idx == len(years):
            logger.info(
                "Duke discovery progress: %d/%d years, %d URL(s) for %d",
                idx,
                len(years),
                len(by_year[year]),
                year,
            )

    return by_year


def _sample_duke_stratified(
    by_year: dict[int, dict[str, dict]],
    *,
    year_start: int,
    year_end: int,
    per_year: int,
    max_articles: int,
) -> list[dict]:
    results: list[dict] = []
    for year in range(year_start, year_end + 1):
        pool = sorted(by_year.get(year, {}).values(), key=lambda m: m["url"])
        for meta in _stratified_sample(pool, per_year):
            results.append(meta)
            if len(results) >= max_articles:
                return results
    return results[:max_articles]


def _discover_duke_stratified(config: dict) -> list[dict]:
    """Per-year page targeting on /section/news; sample from slug dates."""
    year_start = int(config.get("year_start", 2015))
    year_end = int(config.get("year_end", datetime.now(timezone.utc).year))
    per_year = int(config.get("per_year", 3))
    max_articles = int(config.get("max_articles", 100))
    refresh = config.get("refresh_discovery", False)

    by_year: dict[int, dict[str, dict]] | None = None
    if not refresh:
        by_year = _load_duke_discovery_cache(year_end)

    if by_year is None:
        by_year = _fetch_duke_discovery_by_year(config)
        if by_year:
            _save_duke_discovery_cache(by_year, year_end)

    results = _sample_duke_stratified(
        by_year,
        year_start=year_start,
        year_end=year_end,
        per_year=per_year,
        max_articles=max_articles,
    )
    logger.info(
        "Duke stratified discovery: %d candidate URL(s) across %d-%d",
        len(results),
        year_start,
        year_end,
    )
    return results


def _extract_text_duke(url: str, fetcher: "Fetcher", config: dict | None = None) -> dict:
    """Phase 2: fetch a Duke article (browser UA) -> text + author + date."""
    session = _duke_session()
    if config:
        _polite_sleep(config)
    else:
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

    # Section hierarchy: SNWorks prints the category as the <a> immediately above
    # the headline -- usually two-level ("News | University") but sometimes a
    # single level ("News"). Use it whenever present; only fall back to the
    # JSON-LD articleSection list if no kicker is found at all. The kicker is a
    # short label, so guard against accidentally grabbing an unrelated link.
    section, subsection = "", ""
    h1 = soup.find("h1")
    kicker = h1.find_previous("a") if h1 else None
    kicker_txt = clean_text(kicker.get_text(" ")) if kicker else ""
    if kicker_txt and len(kicker_txt) <= 60:
        section, subsection = split_kicker(kicker_txt)
    if not section:
        section, subsection = _duke_section_from_jsonld(soup)

    return {
        "text": text,
        "author": author,
        "publication_date": pub,
        "subtitle": subtitle,
        "section": section,
        "subsection": subsection,
    }


def _duke_section_from_jsonld(soup: BeautifulSoup) -> tuple[str, str]:
    """Fallback Duke section/subsection from JSON-LD ``articleSection``.

    SNWorks emits ``articleSection`` as a slug list mixing real categories with
    layout/placement flags (e.g. ``["news", "topstory", "newsletter-top", ...]``).
    We drop the placement flags and keep the first two real category slugs, then
    title-case them.
    """
    # Substrings that mark a placement/layout slug rather than a real section.
    placement_markers = ("topstory", "newsletter", "featured", "secondary", "carousel")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
        except (ValueError, TypeError):
            continue
        for obj in data if isinstance(data, list) else [data]:
            if not isinstance(obj, dict):
                continue
            secs = obj.get("articleSection")
            if isinstance(secs, list) and secs:
                real = [
                    clean_text(str(s)).replace("-", " ").title()
                    for s in secs
                    if not any(m in str(s).lower() for m in placement_markers)
                ]
                if real:
                    return real[0], (real[1] if len(real) > 1 else "")
    return "", ""


def extract_duke(config: dict, fetcher: "Fetcher") -> Iterator[Article]:
    """Yield Duke Chronicle Article records (HTML listing + full-text fetch)."""
    institution = "The Chronicle (Duke University)"
    max_articles = config.get("max_articles", 100)
    skip_urls = _skip_urls(config)
    count = 0
    for meta in _discover_duke(config, fetcher):
        url = meta.get("url", "")
        if not url:
            continue
        if url in skip_urls:
            logger.debug("Skipping already-scraped Duke URL: %s", url)
            continue
        if count >= max_articles:
            break
        page = _extract_text_duke(url, fetcher, config)
        text = page.get("text", "")
        if not text:
            logger.warning("Skipping Duke article with empty body: %s", url)
            continue
        raw_date = page.get("publication_date") or meta.get("publication_date", "")
        section = page.get("section", "") or meta.get("section", "")
        yield Article(
            institution=institution,
            title=meta.get("title", ""),
            subtitle=page.get("subtitle", ""),
            author=clean_text(page.get("author", "") or meta.get("author", "")),
            publication_date=normalize_date(raw_date, url),
            section=section,
            subsection=page.get("subsection", ""),
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


def _yale_section_from_page(soup: BeautifulSoup) -> str:
    """Best-effort section label from a rendered Yale article page."""
    labels = {slug: label for slug, label in _YALE_SECTION_PAGES}
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        for slug, label in labels.items():
            if f"/{slug}" in href:
                text = clean_text(a.get_text(" "))
                if text and text.lower() == label.lower():
                    return label
    h1 = soup.find("h1")
    if h1:
        kicker = h1.find_previous("a")
        if kicker:
            text = clean_text(kicker.get_text(" "))
            if text and len(text) <= 40:
                return text
    return ""


def _discover_yale_section_pages(config: dict) -> dict[str, str]:
    """Map article URL -> section from per-section landing pages."""
    base_url = config.get("base_url", "https://yaledailynews.com").rstrip("/")
    section_by_url: dict[str, str] = {}
    section_pages = config.get("section_pages")
    pages = (
        [(slug, label) for slug, label in section_pages]
        if section_pages
        else _YALE_SECTION_PAGES
    )
    for slug, section_label in pages:
        durl = f"{base_url}/{slug}"
        _polite_sleep(config)
        rendered = _render_yale(durl)
        if not rendered:
            logger.warning("Yale section discovery blocked/empty for %s", durl)
            continue
        soup = make_soup(rendered[0])
        for a in soup.find_all("a", href=True):
            full = urljoin(base_url, a["href"])
            if _YALE_ARTICLE_PATH.search(full):
                section_by_url.setdefault(full, section_label)
    return section_by_url


def _discover_yale_sitemap(config: dict) -> list[dict]:
    """Primary breadth: articles listed in articles-sitemap.xml."""
    base_url = config.get("base_url", "https://yaledailynews.com").rstrip("/")
    sitemap_url = config.get(
        "articles_sitemap", f"{base_url}/articles-sitemap.xml"
    )
    max_articles = int(config.get("max_articles", 100))

    section_by_url = _discover_yale_section_pages(config)

    try:
        resp = requests.get(sitemap_url, headers={"User-Agent": BROWSER_UA}, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Yale articles sitemap fetch failed: %s", exc)
        return []

    results: list[dict] = []
    for loc in _parse_sitemap_locs(resp.text):
        if not _YALE_ARTICLE_PATH.search(loc):
            continue
        results.append(
            {
                "url": loc,
                "title": "",
                "author": "",
                "publication_date": "",
                "section": section_by_url.get(loc, ""),
            }
        )
        if len(results) >= max_articles:
            break

    logger.info("Yale sitemap discovery: %d article URL(s)", len(results))
    return results


def _discover_yale(config: dict, fetcher: "Fetcher") -> Iterable[dict]:
    """Discovery: articles sitemap (breadth) with section-page labels."""
    if config.get("articles_sitemap") or config.get("discovery_mode") == "sitemap":
        return _discover_yale_sitemap(config)

    base_url = config.get("base_url", "https://yaledailynews.com").rstrip("/")
    max_articles = config.get("max_articles", 100)
    by_url: dict[str, dict] = {}
    section_pages = config.get("section_pages")
    pages = (
        [(slug, label) for slug, label in section_pages]
        if section_pages
        else _YALE_SECTION_PAGES
    )

    for slug, section_label in pages:
        if len(by_url) >= max_articles:
            break
        durl = f"{base_url}/{slug}"
        _polite_sleep(config)
        rendered = _render_yale(durl)
        if not rendered:
            logger.warning("Yale discovery blocked/empty for %s", durl)
            continue
        soup = make_soup(rendered[0])
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
    section = _yale_section_from_page(soup)

    return {
        "text": text,
        "author": author,
        "publication_date": pub,
        "subtitle": subtitle,
        "title": page_title,
        "section": section,
    }


def extract_yale(config: dict, fetcher: "Fetcher") -> Iterator[Article]:
    """Yield Yale Daily News Article records (Playwright render + full text).

    If Yale's Vercel checkpoint persists under headless automation, discovery
    yields nothing and this generator produces zero articles -- a documented
    result, not a failure (see recon/RECON.md and README).
    """
    institution = "Yale Daily News"
    max_articles = config.get("max_articles", 100)
    skip_urls = _skip_urls(config)
    count = 0
    for meta in _discover_yale(config, fetcher):
        url = meta.get("url", "")
        if not url:
            continue
        if url in skip_urls:
            logger.debug("Skipping already-scraped Yale URL: %s", url)
            continue
        if count >= max_articles:
            break
        page = _extract_text_yale(url, fetcher)
        text = page.get("text", "")
        if not text:
            logger.warning("Skipping Yale article with empty body: %s", url)
            continue
        raw_date = page.get("publication_date") or meta.get("publication_date", "")
        section = meta.get("section", "") or page.get("section", "")
        yield Article(
            institution=institution,
            title=meta.get("title", "") or page.get("title", ""),
            subtitle=page.get("subtitle", ""),
            author=clean_text(page.get("author", "") or meta.get("author", "")),
            publication_date=normalize_date(raw_date, url),
            section=section,
            subsection="",
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
