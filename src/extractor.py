"""Shared parsing helpers and per-site extraction functions.

Adding a new institution should require only:
  1. a new entry in ``config/sites.yaml``, and
  2. one ``extract_<site>`` generator here (plus its two phase helpers),
     registered in ``SITE_EXTRACTORS``.

Every extractor follows a two-phase pattern (see ADJ 3):
  Phase 1 (discovery): parse the RSS feed -> url/title/author/date/section.
  Phase 2 (full text):  fetch each article page -> full body text.

All per-site logic below is currently a STUB. No real scraping is
performed until reconnaissance (recon/RECON.md) is complete.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import TYPE_CHECKING, Iterable, Iterator

from bs4 import BeautifulSoup
from dateutil import parser as date_parser

from .schema import Article

if TYPE_CHECKING:
    from .fetcher import Fetcher

logger = logging.getLogger(__name__)


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


# ----------------------------------------------------------------------
# Per-site extractors (STUBS -- reconnaissance pending)
# ----------------------------------------------------------------------
#
# Contract note (watch this when implementing for real):
#   _discover_<site> returns an ITERABLE of dicts (article metadata), e.g.
#       {"url", "title", "author", "publication_date", "section"}.
#   The stub returns an empty list (``[]``) which is a valid iterable, so the
#   ``for ... in discover(...)`` loops below work today. When implementing,
#   keep returning an iterable of dicts (a list or a generator of dicts) --
#   do NOT switch to yielding bare strings or Article objects from discovery,
#   or the orchestration loop's assumptions break.

# -- Duke (The Chronicle) ----------------------------------------------

def _discover_duke(config: dict, fetcher: "Fetcher") -> Iterable[dict]:
    """Phase 1: parse Duke RSS feed -> iterable of article-metadata dicts.

    TODO: reconnaissance pending. Implement RSS discovery with feedparser,
    returning dicts of {url, title, author, publication_date, section}.
    """
    return []


def _extract_text_duke(url: str, fetcher: "Fetcher") -> str:
    """Phase 2: fetch a Duke article page -> full body text.

    TODO: reconnaissance pending. Fetch the HTML, select the body, and
    return clean_text(...) of the article body.
    """
    return ""


def extract_duke(config: dict, fetcher: "Fetcher") -> Iterator[Article]:
    """Yield Duke Article records (RSS discovery + full-text fetch).

    TODO: reconnaissance pending. Orchestrate _discover_duke ->
    _extract_text_duke, build Article records, and stop after
    config["max_articles"].
    """
    return
    yield  # pragma: no cover -- makes this a generator while stubbed


# -- Yale (Yale Daily News) --------------------------------------------

def _discover_yale(config: dict, fetcher: "Fetcher") -> Iterable[dict]:
    """Phase 1: parse Yale RSS feed -> iterable of article-metadata dicts.

    TODO: reconnaissance pending.
    """
    return []


def _extract_text_yale(url: str, fetcher: "Fetcher") -> str:
    """Phase 2: fetch a Yale article page -> full body text.

    TODO: reconnaissance pending.
    """
    return ""


def extract_yale(config: dict, fetcher: "Fetcher") -> Iterator[Article]:
    """Yield Yale Article records (RSS discovery + full-text fetch).

    TODO: reconnaissance pending. Respect config["max_articles"].
    """
    return
    yield  # pragma: no cover -- makes this a generator while stubbed


# -- Northwestern (The Daily Northwestern) -----------------------------

def _discover_northwestern(config: dict, fetcher: "Fetcher") -> Iterable[dict]:
    """Phase 1: parse Northwestern RSS feed -> iterable of metadata dicts.

    TODO: reconnaissance pending.
    """
    return []


def _extract_text_northwestern(url: str, fetcher: "Fetcher") -> str:
    """Phase 2: fetch a Northwestern article page -> full body text.

    TODO: reconnaissance pending.
    """
    return ""


def extract_northwestern(config: dict, fetcher: "Fetcher") -> Iterator[Article]:
    """Yield Northwestern Article records (RSS discovery + full-text fetch).

    TODO: reconnaissance pending. Respect config["max_articles"].
    """
    return
    yield  # pragma: no cover -- makes this a generator while stubbed


# ----------------------------------------------------------------------
# Registry: site key -> extraction generator
# ----------------------------------------------------------------------

SITE_EXTRACTORS = {
    "duke": extract_duke,
    "yale": extract_yale,
    "northwestern": extract_northwestern,
}
