"""Shared extractor for WordPress papers (SNO Sites and generic themes).

Every WordPress paper shares discovery: a WordPress-core
``wp-sitemap-posts-post-N.xml`` or Yoast/AIOSEO ``post-sitemap*.xml`` index.
Only the article page layout differs, so each site picks a page profile:

  - ``platform: sno``        -> the SNO/FLEX parser in ``src/sno.py``
  - ``platform: wordpress``  -> ``page_profile`` (default ``wp_generic``)

A new paper is a ``sites.yaml`` entry; no code. Required: ``base_url``,
``institution``. Optional: ``sitemap_index``, ``exclude_url_patterns`` (regex
list), ``corpus_year_start`` (default 2000), ``year_start``/``year_end``/
``per_year`` for sample mode, and ``selectors`` -- per-site CSS overrides
tried before the profile (``body``, ``date``, ``author``, ``section``,
``title``, ``subtitle``).

Sitemap ``<lastmod>`` only buckets candidates and is never written as a
publication date.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Iterator

import requests
from bs4 import BeautifulSoup

from . import sno
from .extractor import (
    DISCOVERY_PROGRESS_EVERY,
    PROJECT_ROOT,
    _collect_byline_authors,
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

WORDPRESS_PLATFORMS = frozenset({"sno", "wordpress"})


def _label(config: dict) -> str:
    return str(config.get("institution") or config.get("site_key") or "WordPress site")


def _cache_path(config: dict):
    key = config.get("site_key")
    if not key:
        raise ValueError("WordPress extractor needs site_key in its config")
    return CACHE_DIR / f"{key}_sitemap.json"


def _bucket_year(url: str, lastmod: str) -> int | None:
    """Candidate bucket: permalink year when present, else sitemap lastmod."""
    m = sno.URL_DATE_RE.search(url)
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


def _discover(config: dict, fetcher: "Fetcher") -> list[dict]:
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
# Generic WordPress page profile
# ----------------------------------------------------------------------

_GENERIC_BODY = (
    ".entry-content",
    ".post-content",
    ".article-content",
    ".single-post-content",
    "[itemprop='articleBody']",
)
_GENERIC_AUTHOR_LINKS = (
    "a[rel~='author'], .byline a, .entry-author a, .author-name a, .post-author a"
)
_GENERIC_AUTHOR_TEXT = (".author-name", ".byline", ".entry-author", ".post-author")
_GENERIC_SUBTITLE = ".entry-subtitle, .post-subtitle, .article-subtitle, .wp-block-post-excerpt__excerpt"


def _jsonld_author(soup: BeautifulSoup) -> str:
    for tag in soup.find_all("script", type="application/ld+json"):
        m = re.search(
            r'"author"\s*:\s*\[?\s*\{[^{}]*?"name"\s*:\s*"([^"]+)"', tag.string or ""
        )
        if m:
            return clean_text(m.group(1))
    return ""


def _generic_date(soup: BeautifulSoup, url: str) -> str:
    for selector in ("time.entry-date[datetime]", "time.published[datetime]", "article time[datetime]"):
        el = soup.select_one(selector)
        if el and el.get("datetime"):
            return el["datetime"]
    return (
        _meta_content(soup, "article:published_time")
        or sno.jsonld_date_published(soup)
        or sno.url_date(url)
    )


def _generic_author(soup: BeautifulSoup) -> str:
    author = _collect_byline_authors(soup, _GENERIC_AUTHOR_LINKS)
    if not author:
        for selector in _GENERIC_AUTHOR_TEXT:
            el = soup.select_one(selector)
            if el:
                author = clean_text(el.get_text(" "))
                break
    author = author or _meta_content(soup, "author") or _jsonld_author(soup)
    author = clean_text(re.sub(r"^by\s+", "", author, flags=re.IGNORECASE))
    return "" if sno.DATE_LIKE_RE.match(author) else author


def _generic_section(soup: BeautifulSoup) -> str:
    primary = _meta_content(soup, "article:section") or sno.jsonld_article_section(soup)
    if not primary:
        el = soup.select_one(".cat-links a, a[rel~='category']")
        primary = clean_text(el.get_text(" ")) if el else ""
    return primary


def parse_wp_generic(soup: BeautifulSoup, url: str, label: str) -> dict:
    body = None
    for selector in _GENERIC_BODY:
        body = soup.select_one(selector)
        if body and body.find("p"):
            break
        body = None
    body = body or _largest_p_container(soup)
    title = _meta_content(soup, "og:title")
    if not title:
        heading = soup.select_one("h1.entry-title, h1")
        title = clean_text(heading.get_text(" ")) if heading else ""
    subtitle = soup.select_one(_GENERIC_SUBTITLE)
    section, subsection = resolve_section_path(soup, _generic_section(soup))
    return {
        "text": sno.body_text(body),
        "title": sno.strip_site_suffix(title, label),
        "author": _generic_author(soup),
        "publication_date": _generic_date(soup, url),
        "subtitle": clean_text(subtitle.get_text(" ")) if subtitle else "",
        "section": section,
        "subsection": subsection,
    }


PAGE_PROFILES: dict[str, Callable[[BeautifulSoup, str, str], dict]] = {
    "sno": sno.parse_sno_page,
    "wp_generic": parse_wp_generic,
}


def page_profile(config: dict) -> str:
    platform = str(config.get("platform", "")).lower()
    profile = str(config.get("page_profile") or ("sno" if platform == "sno" else "wp_generic"))
    if profile not in PAGE_PROFILES:
        raise ValueError(f"Unknown page_profile {profile!r} (known: {', '.join(PAGE_PROFILES)})")
    return profile


# ----------------------------------------------------------------------
# Article page
# ----------------------------------------------------------------------

def _apply_selector_overrides(soup: BeautifulSoup, page: dict, selectors: dict) -> None:
    """Per-site CSS from sites.yaml wins whenever it finds something."""
    for field, selector in (selectors or {}).items():
        if field == "author":
            value = _collect_byline_authors(soup, selector)
        else:
            el = soup.select_one(selector)
            if el is None:
                continue
            if field == "body":
                value = sno.body_text(el)
            elif field == "date":
                value = el.get("datetime") or el.get("content") or clean_text(el.get_text(" "))
            else:
                value = clean_text(el.get_text(" "))
        if not value:
            continue
        if field == "body":
            page["text"] = value
        elif field == "date":
            page["publication_date"] = value
        elif field == "section":
            page["section"], page["subsection"] = resolve_section_path(soup, value)
        elif field in ("author", "title", "subtitle"):
            page[field] = value
        else:
            raise ValueError(f"Unknown selector override {field!r}")


def fetch_page(
    url: str,
    fetcher: "Fetcher",
    label: str,
    profile: str = "sno",
    selectors: dict | None = None,
) -> dict:
    """Fetch and parse one article; ``error`` is set when there is no body."""
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
    page = PAGE_PROFILES[profile](soup, url, label)
    _apply_selector_overrides(soup, page, selectors or {})
    if not page.get("text"):
        out = _empty_page()
        out["error"] = "empty_body"
        return out
    return page


# ----------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------

def extract_wordpress(config: dict, fetcher: "Fetcher") -> Iterator[Article]:
    """Yield Articles for one WordPress paper.

    Full mode: every discovered URL; rows dated before ``corpus_year_start``
    are skipped after fetch; only ``max_fetch`` caps fetches. Sample mode:
    per-year backfill until ``per_year`` rows have body text.
    """
    label = _label(config)
    institution = str(config.get("institution") or label)
    profile = page_profile(config)
    selectors = config.get("selectors") or {}
    full = str(config.get("mode", "sample")).lower() == "full"
    skip_urls = _skip_urls(config)
    failure_sink = config.get("failure_sink")
    year_floor = int(config.get("corpus_year_start", 2000))
    max_fetch = config.get("max_fetch") if full else config.get("max_articles", 100)
    max_fetch_i = int(max_fetch) if max_fetch is not None else None
    per_year = int(config.get("per_year", 2))
    per_year_ok: dict[int, int] = defaultdict(int)
    fetched = 0

    for meta in _discover(config, fetcher):
        url = meta["url"]
        if url in skip_urls:
            continue
        if max_fetch_i is not None and fetched >= max_fetch_i:
            break
        year = meta.get("year")
        if not full and year is not None and per_year_ok[year] >= per_year:
            continue
        fetched += 1
        page = fetch_page(url, fetcher, label, profile, selectors)
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


def is_wordpress_site(site_cfg: dict) -> bool:
    return str((site_cfg or {}).get("platform", "")).lower() in WORDPRESS_PLATFORMS


def wordpress_site_keys(sites: dict) -> list[str]:
    return [key for key, cfg in (sites or {}).items() if is_wordpress_site(cfg)]
