"""Common article schema shared across all site extractors.

Every per-site extractor yields ``Article`` instances so the downstream
writer and pipeline can stay completely site-agnostic.
"""

from __future__ import annotations

import csv
import sys
from dataclasses import asdict, dataclass, fields
from typing import Any


def raise_csv_field_limit() -> None:
    """Lift csv's default 128 KiB field cap so long article bodies resume.

    One CSV per site. Do not split a paper across files: checkpoints store a
    byte offset into that file, and full mode skips URLs already in it.
    Compilation pages (e.g. Michigan Daily senior goodbyes) can exceed 128 KiB;
    writing them always worked, but the next read crashed until this ran.
    """
    try:
        csv.field_size_limit(sys.maxsize)
    except OverflowError:
        csv.field_size_limit(2**31 - 1)


raise_csv_field_limit()


@dataclass
class Article:
    """A single newspaper article in the common corpus schema.

    Field order here is also the CSV column order (see ``fieldnames``).
    """

    institution: str
    title: str
    subtitle: str  # editor-written deck when the CMS exposes one; empty otherwise
    author: str
    publication_date: str  # normalized to ISO 8601 (YYYY-MM-DD) or "UNPARSED:<raw>"
    section: str  # top-level category (publication's own taxonomy)
    subsection: str  # child category when the paper nests sections; empty otherwise
    url: str
    text: str
    scraped_at: str  # ISO 8601 timestamp of when the record was collected

    @classmethod
    def fieldnames(cls) -> list[str]:
        """Return the ordered list of CSV column names."""
        return [f.name for f in fields(cls)]

    def to_row(self) -> dict[str, Any]:
        """Return a dict suitable for ``csv.DictWriter.writerow``."""
        return asdict(self)
