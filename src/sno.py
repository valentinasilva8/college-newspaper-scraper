"""Shared extractor for papers on SNO Sites (WordPress + SNO/FLEX theme).

Most US college papers on the target list run SNO. They share discovery (a
WordPress-core ``wp-sitemap-posts-post-N.xml`` or Yoast ``post-sitemap*.xml``
index) and page structure, so one configurable extractor covers them. A new
SNO paper is a ``sites.yaml`` entry with ``platform: sno``; no code.

Required site config: ``base_url``, ``institution``.
Optional: ``sitemap_index``, ``exclude_url_patterns`` (regex list),
``corpus_year_start`` (default 2000), ``year_start``/``year_end``/``per_year``
for sample mode.

Dates are read from the article page, preferring the visible local date:
``.sno-story-date`` -> classic ``.storydate`` -> the date after "•" in the
byline -> ``article:published_time`` -> JSON-LD ``datePublished`` -> the
``/YYYY/MM/DD/`` permalink. Never ``.time-wrapper``: on some SNO sites (Biola)
that is the masthead's current date. Sitemap ``<lastmod>`` only buckets
candidates and is never written as a publication date.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Iterator

import requests
from bs4 import BeautifulSoup

from .extractor import (
    DISCOVERY_PROGRESS_EVERY,
    PROJECT_ROOT,
    _chicago_byline_authors,
    _empty_page,
    _fetch_error_reason,
    _largest_p_container,
    _meta_content,
    _page_year,
    _skip_urls,
    _stratified_sample,
    _year_from_iso,
    clean_text,
    make_soup,
    normalize_date,
    resolve_section_path,
)
from .schema import Article

if TYPE_CHECKING:
    from .fetcher import Fetcher

logger = logging.getLogger(__name__)

CACHE_DIR = PROJECT_ROOT / "logs" / "cache"

_MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|"
    r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
_DATE_RE = re.compile(_MONTH + r"\.?\s+\d{1,2},\s+(?:19|20)\d{2}")
_URL_DATE_RE = re.compile(r"/((?:19|20)\d{2})/(\d{2})/(\d{2})/")


def _label(config: dict) -> str:
    return str(config.get("institution") or config.get("site_key") or "SNO site")


def _cache_path(config: dict):
    key = config.get("site_key")
    if not key:
        raise ValueError("SNO extractor needs site_key in its config")
    return CACHE_DIR / f"{key}_sitemap.json"


def _url_date(url: str) -> str:
    m = _URL_DATE_RE.search(url)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def _bucket_year(url: str, lastmod: str) -> int | None:
    """Candidate bucket: permalink year when present, else sitemap lastmod."""
    m = _URL_DATE_RE.search(url)
    if m:
        return int(m.group(1))
    return _year_from_iso(lastmod)


# ----------------------------------------------------------------------
# Discovery
# ----------------------------------------------------------------------

def _fetch_sitemap_urls(config: dict, fetcher: "Fetcher") -> list[list[str]]:
    """Every (url, lastmod) pair from the site's post sitemaps."""
    label = _label(config)
    base = str(config.get("base_url", "")).rstrip("/")
    index_url = config.get("sitemap_index") or f"{base}/sitemap.xml"
    try:
        index_resp = fetcher.get(index_url)
    except requests.RequestException as exc:
        logger.warning("%s sitemap index fetch failed: %s", label, exc)
        return []
    if index_resp is None:
        logger.warning("%s sitemap index blocked by robots.txt", label)
        return []

    index = BeautifulSoup(index_resp.text, "xml")
    subs = [
        loc.text.strip()
        for loc in index.find_all("loc")
        if loc.text
        and ("wp-sitemap-posts-post" in loc.text or "post-sitemap" in loc.text)
    ]
    total = len(subs)
    logger.info("%s sitemap: scanning %d post sub-sitemap(s)", label, total)

    excludes = [re.compile(p) for p in config.get("exclude_url_patterns") or []]
    pairs: dict[str, str] = {}
    for idx, sub_url in enumerate(subs, start=1):
        try:
            sub_resp = fetcher.get(sub_url)
        except requests.RequestException as exc:
            logger.warning("%s sub-sitemap fetch failed for %s: %s", label, sub_url, exc)
            continue
        if sub_resp is None:
            continue
        for url_el in BeautifulSoup(sub_resp.text, "xml").find_all("url"):
            loc_el = url_el.find("loc")
            if not loc_el or not loc_el.text:
                continue
            loc = loc_el.text.strip()
            if any(p.search(loc) for p in excludes):
                continue
            lm_el = url_el.find("lastmod")
            pairs[loc] = lm_el.text.strip() if lm_el and lm_el.text else ""
        if idx == 1 or idx % DISCOVERY_PROGRESS_EVERY == 0 or idx == total:
            logger.info(
                "%s sitemap progress: %d/%d sub-sitemap(s), %d article URL(s)",
                label,
                idx,
                total,
                len(pairs),
            )
    return [[url, lastmod] for url, lastmod in sorted(pairs.items())]


def _load_or_fetch_urls(config: dict, fetcher: "Fetcher") -> list[list[str]]:
    label = _label(config)
    path = _cache_path(config)
    if not config.get("refresh_discovery") and path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            urls = [list(pair) for pair in data["urls"]]
            logger.info(
                "%s sitemap: using cache (%d article URL(s), cached %s)",
                label,
                len(urls),
                data.get("cached_at", "unknown"),
            )
            return urls
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("%s sitemap cache unreadable (%s); rescanning.", label, exc)

    urls = _fetch_sitemap_urls(config, fetcher)
    if urls:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"cached_at": datetime.now(timezone.utc).isoformat(), "urls": urls},
                indent=1,
            ),
            encoding="utf-8",
        )
        logger.info("%s sitemap cache saved -> %s", label, path.name)
    return urls


def _discover_sno(config: dict, fetcher: "Fetcher") -> list[dict]:
    """Full mode: every URL, oldest bucket first. Sample: stratified per year."""
    label = _label(config)
    pairs = _load_or_fetch_urls(config, fetcher)
    metas = [
        {"url": url, "lastmod": lastmod, "year": _bucket_year(url, lastmod)}
        for url, lastmod in pairs
    ]
    metas.sort(key=lambda m: (m["year"] or 0, m["url"]))

    if str(config.get("mode", "sample")).lower() == "full":
        bucket = config.get("candidate_lastmod_year")
        if bucket is not None:
            metas = [m for m in metas if m["year"] == int(bucket)]
            logger.info(
                "%s full discovery: bucket year %s -> %d candidate URL(s)",
                label,
                bucket,
                len(metas),
            )
        else:
            logger.info("%s full discovery: %d candidate URL(s)", label, len(metas))
        return metas

    year_start = int(config.get("year_start", 2000))
    year_end = int(config.get("year_end", datetime.now(timezone.utc).year))
    per_year = int(config.get("per_year", 2))
    by_year: dict[int, list[dict]] = defaultdict(list)
    for m in metas:
        if m["year"] is not None and year_start <= m["year"] <= year_end:
            by_year[m["year"]].append(m)
    picks = [
        m for year in sorted(by_year) for m in _stratified_sample(by_year[year], per_year * 4)
    ]
    logger.info(
        "%s sample discovery: %d candidate URL(s) across %d-%d",
        label,
        len(picks),
        year_start,
        year_end,
    )
    return picks


# ----------------------------------------------------------------------
# Article page
# ----------------------------------------------------------------------

def _first_date(text: str) -> str:
    m = _DATE_RE.search(text or "")
    return m.group(0) if m else ""


def _jsonld_date_published(soup: BeautifulSoup) -> str:
    for tag in soup.find_all("script", type="application/ld+json"):
        m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', tag.string or "")
        if m:
            return m.group(1)
    return ""


def _jsonld_article_section(soup: BeautifulSoup) -> str:
    """First ``articleSection`` from JSON-LD (TCU has no article:section meta)."""
    for tag in soup.find_all("script", type="application/ld+json"):
        m = re.search(r'"articleSection"\s*:\s*\[?\s*"([^"]+)"', tag.string or "")
        if m:
            return clean_text(m.group(1))
    return ""


def _page_date(soup: BeautifulSoup, url: str) -> str:
    """Raw publication date from the page (see module docstring for order)."""
    for selector in (".sno-story-date", ".storydate"):
        el = soup.select_one(selector)
        if el:
            found = _first_date(el.get_text(" "))
            if found:
                return found
    byline = soup.select_one(".sno-story-byline")
    if byline and "•" in byline.get_text():
        found = _first_date(byline.get_text(" ").split("•", 1)[1])
        if found:
            return found
    return (
        _meta_content(soup, "article:published_time")
        or _jsonld_date_published(soup)
        or _url_date(url)
    )


_DATE_LIKE_RE = re.compile(r"^" + _MONTH + r"\.?\s+\d")


def _page_author(soup: BeautifulSoup) -> str:
    """Byline names; "" when the page has none.

    Legacy SNO templates (Biola 2007) put only the date in the byline slot, and
    the raw-text fallback would otherwise return "September 19".
    """
    candidates = [_chicago_byline_authors(soup)]
    classic = soup.select_one(".storybyline")
    if classic:
        candidates.append(clean_text(classic.get_text(" ")).split(",")[0])
    candidates.append(_meta_content(soup, "author"))
    for raw in candidates:
        author = clean_text(re.sub(r"^by\s+", "", raw or "", flags=re.IGNORECASE))
        author = clean_text(author.split("•")[0])
        if author and not _DATE_LIKE_RE.match(author):
            return author
    return ""


def _page_title(soup: BeautifulSoup, institution: str) -> str:
    title = _meta_content(soup, "og:title")
    if not title:
        headline = soup.find(id="sno-story-headline")
        title = clean_text(headline.get_text(" ")) if headline else ""
    for sep in (" - ", " | ", " – "):
        suffix = f"{sep}{institution}"
        if institution and title.endswith(suffix):
            return title[: -len(suffix)].strip()
    return title


def _page_subtitle(soup: BeautifulSoup) -> str:
    el = soup.select_one(
        "#sno-story-subtitle, .sno-story-subtitle, .sno-story-deck, #sno-story-deck"
    )
    return clean_text(el.get_text(" ")) if el else ""


def _extract_text_sno(url: str, fetcher: "Fetcher", label: str) -> dict:
    try:
        resp = fetcher.get(url)
    except requests.RequestException as exc:
        logger.warning("%s fetch failed for %s: %s", label, url, exc)
        out = _empty_page()
        out["error"] = _fetch_error_reason(exc)
        return out
    if resp is None:
        logger.warning("%s fetch skipped (robots.txt) for %s", label, url)
        out = _empty_page()
        out["error"] = "robots_blocked"
        return out

    soup = make_soup(resp.text)
    body = (
        soup.find(id="sno-story-body-content")
        or soup.find(id="classic_story")
        or soup.find(id="sno-sites-main-content")
        or _largest_p_container(soup)
    )
    text = clean_text(" ".join(p.get_text(" ") for p in body.find_all("p"))) if body else ""
    if not text:
        out = _empty_page()
        out["error"] = "empty_body"
        return out

    primary = _meta_content(soup, "article:section") or _jsonld_article_section(soup)
    section, subsection = resolve_section_path(soup, primary)
    return {
        "text": text,
        "title": _page_title(soup, label),
        "author": _page_author(soup),
        "publication_date": _page_date(soup, url),
        "subtitle": _page_subtitle(soup),
        "section": section,
        "subsection": subsection,
    }


# ----------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------

def extract_sno(config: dict, fetcher: "Fetcher") -> Iterator[Article]:
    """Yield Articles for one SNO paper.

    Full mode: every discovered URL; rows dated before ``corpus_year_start``
    are skipped after fetch; only ``max_fetch`` caps fetches. Sample mode:
    per-year backfill until ``per_year`` rows have body text.
    """
    label = _label(config)
    institution = str(config.get("institution") or label)
    full = str(config.get("mode", "sample")).lower() == "full"
    skip_urls = _skip_urls(config)
    failure_sink = config.get("failure_sink")
    year_floor = int(config.get("corpus_year_start", 2000))
    max_fetch = config.get("max_fetch") if full else config.get("max_articles", 100)
    max_fetch_i = int(max_fetch) if max_fetch is not None else None
    per_year = int(config.get("per_year", 2))
    per_year_ok: dict[int, int] = defaultdict(int)
    fetched = 0

    for meta in _discover_sno(config, fetcher):
        url = meta["url"]
        if url in skip_urls:
            continue
        if max_fetch_i is not None and fetched >= max_fetch_i:
            break
        year = meta.get("year")
        if not full and year is not None and per_year_ok[year] >= per_year:
            continue
        fetched += 1
        page = _extract_text_sno(url, fetcher, label)
        if not page.get("text"):
            reason = page.get("error") or "empty_body"
            logger.warning("%s %s: %s", label, reason, url)
            if callable(failure_sink):
                failure_sink(url, year, reason)
            continue
        pub = normalize_date(page.get("publication_date", ""), url)
        page_year = _page_year(pub)
        if full and page_year is not None and page_year < year_floor:
            logger.info("Skipping %s pre-%d article (%s): %s", label, year_floor, pub, url)
            if callable(failure_sink):
                failure_sink(url, page_year, f"pre_{year_floor}")
            continue
        if year is not None:
            per_year_ok[year] += 1
        yield Article(
            institution=institution,
            title=page.get("title", ""),
            subtitle=page.get("subtitle", ""),
            author=clean_text(page.get("author", "")),
            publication_date=pub,
            section=page.get("section", ""),
            subsection=page.get("subsection", ""),
            url=url,
            text=page["text"],
            scraped_at=datetime.now(timezone.utc).isoformat(),
        )


def is_sno_site(site_cfg: dict) -> bool:
    return str((site_cfg or {}).get("platform", "")).lower() == "sno"


def sno_site_keys(sites: dict) -> list[str]:
    return [key for key, cfg in (sites or {}).items() if is_sno_site(cfg)]
