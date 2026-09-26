"""SNO Sites (WordPress + SNO/FLEX theme) article page profile.

Most US college papers on the target list run SNO. Discovery, fetching and
orchestration are shared with every WordPress paper in ``src/wordpress.py``;
this module only reads an SNO article page (``platform: sno``).

Dates are read from the article page, preferring the visible local date:
``.sno-story-date`` -> classic ``.storydate`` -> the date after "•" in the
byline -> ``article:published_time`` -> JSON-LD ``datePublished`` -> the
``/YYYY/MM/DD/`` permalink. Never ``.time-wrapper``: on some SNO sites (Biola)
that is the masthead's current date.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from .extractor import (
    _chicago_byline_authors,
    _largest_p_container,
    _meta_content,
    clean_text,
    resolve_section_path,
)

_MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|"
    r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
_DATE_RE = re.compile(_MONTH + r"\.?\s+\d{1,2},\s+(?:19|20)\d{2}")
URL_DATE_RE = re.compile(r"/((?:19|20)\d{2})/(\d{2})/(\d{2})/")
DATE_LIKE_RE = re.compile(r"^" + _MONTH + r"\.?\s+\d")


# ----------------------------------------------------------------------
# Helpers shared with the generic WordPress profile
# ----------------------------------------------------------------------

def url_date(url: str) -> str:
    m = URL_DATE_RE.search(url)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def jsonld_date_published(soup: BeautifulSoup) -> str:
    for tag in soup.find_all("script", type="application/ld+json"):
        m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', tag.string or "")
        if m:
            return m.group(1)
    return ""


def jsonld_article_section(soup: BeautifulSoup) -> str:
    """First ``articleSection`` from JSON-LD (TCU has no article:section meta)."""
    for tag in soup.find_all("script", type="application/ld+json"):
        m = re.search(r'"articleSection"\s*:\s*\[?\s*"([^"]+)"', tag.string or "")
        if m:
            return clean_text(m.group(1))
    return ""


def body_text(body) -> str:
    return clean_text(" ".join(p.get_text(" ") for p in body.find_all("p"))) if body else ""


def strip_site_suffix(title: str, institution: str) -> str:
    for sep in (" - ", " | ", " – "):
        suffix = f"{sep}{institution}"
        if institution and title.endswith(suffix):
            return title[: -len(suffix)].strip()
    return title


# ----------------------------------------------------------------------
# SNO page
# ----------------------------------------------------------------------

def _first_date(text: str) -> str:
    m = _DATE_RE.search(text or "")
    return m.group(0) if m else ""


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
        or jsonld_date_published(soup)
        or url_date(url)
    )


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
        if author and not DATE_LIKE_RE.match(author):
            return author
    return ""


def _page_title(soup: BeautifulSoup, institution: str) -> str:
    title = _meta_content(soup, "og:title")
    if not title:
        headline = soup.find(id="sno-story-headline")
        title = clean_text(headline.get_text(" ")) if headline else ""
    return strip_site_suffix(title, institution)


def _page_subtitle(soup: BeautifulSoup) -> str:
    el = soup.select_one(
        "#sno-story-subtitle, .sno-story-subtitle, .sno-story-deck, #sno-story-deck"
    )
    return clean_text(el.get_text(" ")) if el else ""


def parse_sno_page(soup: BeautifulSoup, url: str, label: str) -> dict:
    body = (
        soup.find(id="sno-story-body-content")
        or soup.find(id="classic_story")
        or soup.find(id="sno-sites-main-content")
        or _largest_p_container(soup)
    )
    primary = _meta_content(soup, "article:section") or jsonld_article_section(soup)
    section, subsection = resolve_section_path(soup, primary)
    return {
        "text": body_text(body),
        "title": _page_title(soup, label),
        "author": _page_author(soup),
        "publication_date": _page_date(soup, url),
        "subtitle": _page_subtitle(soup),
        "section": section,
        "subsection": subsection,
    }
