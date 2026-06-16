"""CSV output: one file per site, plus a combined corpus file.

All writers emit a header row even when the article list is empty, so the
pilot's "header-only" output is an explicit, intended result rather than an
accident -- and downstream tools always see a well-formed CSV.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Iterable, Sequence

from .schema import Article

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"


def _write_csv(path: Path, articles: Sequence[Article]) -> int:
    """Write ``articles`` to ``path``. Always writes the header row.

    Returns the number of article rows written (0 means header-only).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=Article.fieldnames())
        writer.writeheader()  # header is written unconditionally
        count = 0
        for article in articles:
            writer.writerow(article.to_row())
            count += 1
    return count


def write_site_csv(site: str, articles: Iterable[Article]) -> Path:
    """Write a per-site CSV to ``output/<site>.csv``."""
    rows = list(articles)
    path = OUTPUT_DIR / f"{site}.csv"
    count = _write_csv(path, rows)
    logger.info("Wrote %d article(s) for '%s' -> %s", count, site, path.name)
    return path


def read_site_csv(site: str) -> list[Article]:
    """Load existing per-site rows from ``output/<site>.csv``.

    Returns an empty list when the file is missing. Missing columns in older
    CSVs are filled with ``""`` so incremental merges stay compatible.
    """
    path = OUTPUT_DIR / f"{site}.csv"
    if not path.is_file():
        return []

    fieldnames = Article.fieldnames()
    rows: list[Article] = []
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            data = {name: (raw.get(name) or "") for name in fieldnames}
            if not data.get("url"):
                continue
            rows.append(Article(**data))
    logger.info("Loaded %d existing row(s) for '%s' from %s", len(rows), site, path.name)
    return rows


def write_combined_csv(articles: Iterable[Article]) -> Path:
    """Write the combined corpus to ``output/combined.csv``.

    Explicitly handles the empty case: an empty list produces a valid
    header-only CSV instead of crashing.
    """
    rows = list(articles)
    path = OUTPUT_DIR / "combined.csv"
    if not rows:
        logger.info("No articles to combine; writing header-only %s", path.name)
    count = _write_csv(path, rows)
    logger.info("Wrote combined corpus: %d article(s) -> %s", count, path.name)
    return path
