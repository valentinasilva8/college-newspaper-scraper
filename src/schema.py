"""Common article schema shared across all site extractors.

Every per-site extractor yields ``Article`` instances so the downstream
writer and pipeline can stay completely site-agnostic.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any


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
    section: str
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
