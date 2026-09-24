"""CSV output: one file per site, plus a combined corpus file.

Sample mode rewrites the whole file at the end of a run (pilot behavior).
Full-corpus mode appends batches of 50 with flush/fsync and a byte-offset
checkpoint so a crash loses at most one batch.
"""

from __future__ import annotations

import csv
import io
import logging
import os
from pathlib import Path
from typing import Iterable, Sequence

from .checkpoint import Checkpoint, load_checkpoint, save_checkpoint
from .schema import Article

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output"
LOG_DIR = PROJECT_ROOT / "logs"
BATCH_SIZE = 50
FAILED_FIELDNAMES = ["url", "year", "reason"]


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
    """Write a per-site CSV to ``output/<site>.csv`` (sample-mode full rewrite)."""
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


# ----------------------------------------------------------------------
# Full-corpus append + checkpoint helpers
# ----------------------------------------------------------------------


def site_csv_path(site: str) -> Path:
    return OUTPUT_DIR / f"{site}.csv"


def failed_csv_path(site: str) -> Path:
    return LOG_DIR / f"failed_{site}.csv"


def _serialize_rows(articles: Sequence[Article], *, include_header: bool) -> bytes:
    """Serialize rows to UTF-8 CSV bytes (exact on-disk format)."""
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=Article.fieldnames())
    if include_header:
        writer.writeheader()
    for article in articles:
        writer.writerow(article.to_row())
    return buf.getvalue().encode("utf-8")


def validate_site_csv_header(path: Path) -> None:
    """Reject a CSV whose header does not match the frozen schema order."""
    if not path.is_file() or path.stat().st_size == 0:
        return
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
    expected = Article.fieldnames()
    if header != expected:
        raise ValueError(
            f"CSV header mismatch for {path.name}: got {header!r}, expected {expected!r}"
        )


def prepare_append_csv(site: str, *, overwrite: bool = False) -> Checkpoint:
    """Prepare ``output/<site>.csv`` for crash-safe append writes.

    - Creates the file with a header when missing.
    - Truncates any uncommitted tail back to the last checkpoint offset.
    - Rejects wrong column order.
    - Blocks empty-text rows that would create silent duplicate-URL risk:
      those URLs must be cleared or overwritten intentionally.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = site_csv_path(site)

    if overwrite and path.is_file():
        path.unlink()
        from .checkpoint import clear_checkpoint

        clear_checkpoint(site)

    checkpoint = load_checkpoint(site)

    if not path.is_file() or path.stat().st_size == 0:
        header_bytes = _serialize_rows([], include_header=True)
        with path.open("wb") as fh:
            fh.write(header_bytes)
            fh.flush()
            os.fsync(fh.fileno())
        checkpoint = Checkpoint(
            site=site,
            csv_path=str(path),
            committed_offset=len(header_bytes),
            rows_committed=0,
        )
        save_checkpoint(checkpoint)
        return checkpoint

    validate_site_csv_header(path)

    # Preflight: empty-text rows must not be left for append mode.
    empty_text_urls = [
        article.url
        for article in read_site_csv(site)
        if article.url and not article.text.strip()
    ]
    if empty_text_urls:
        raise ValueError(
            f"Site '{site}' has {len(empty_text_urls)} row(s) with empty text. "
            "Full-corpus append mode refuses to create duplicate URLs. "
            "Delete those rows or use --overwrite after review."
        )

    size = path.stat().st_size
    if checkpoint is None:
        # Existing sample CSV without a checkpoint: treat whole file as committed.
        rows = read_site_csv(site)
        checkpoint = Checkpoint(
            site=site,
            csv_path=str(path),
            committed_offset=size,
            rows_committed=len(rows),
        )
        save_checkpoint(checkpoint)
        return checkpoint

    offset = checkpoint.committed_offset
    if offset < 0 or offset > size:
        raise ValueError(
            f"Checkpoint offset {offset} is invalid for {path.name} size {size}"
        )
    if offset < size:
        # Truncate uncommitted tail from a crashed previous run.
        with path.open("r+b") as fh:
            fh.truncate(offset)
            fh.flush()
            os.fsync(fh.fileno())
        logger.warning(
            "Truncated uncommitted tail on %s (%d -> %d bytes)",
            path.name,
            size,
            offset,
        )
    return checkpoint


def append_article_batch(
    site: str,
    articles: Sequence[Article],
    checkpoint: Checkpoint,
) -> Checkpoint:
    """Append a fully-serialized batch, fsync, then advance the checkpoint."""
    if not articles:
        return checkpoint

    path = site_csv_path(site)
    payload = _serialize_rows(articles, include_header=False)
    with path.open("ab") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())

    checkpoint.committed_offset = path.stat().st_size
    checkpoint.rows_committed += len(articles)
    checkpoint.csv_path = str(path)
    save_checkpoint(checkpoint)
    logger.info(
        "Appended %d row(s) for '%s' (committed=%d)",
        len(articles),
        site,
        checkpoint.rows_committed,
    )
    return checkpoint


def log_failed_url(site: str, url: str, year: str | int | None, reason: str) -> None:
    """Append one failure row to ``logs/failed_<site>.csv``."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = failed_csv_path(site)
    write_header = not path.is_file() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FAILED_FIELDNAMES)
        if write_header:
            writer.writeheader()
        writer.writerow(
            {
                "url": url,
                "year": "" if year is None else str(year),
                "reason": reason,
            }
        )
        fh.flush()
