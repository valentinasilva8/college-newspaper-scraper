"""Crash-safe CSV checkpoints for full-corpus runs.

Design:
  - Append batches of articles to ``output/<site>.csv``.
  - Persist the last committed byte offset in ``logs/checkpoints/<site>.json``.
  - On restart, truncate any uncommitted tail back to that offset, then resume.
  - A crash loses at most one unflushed batch.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CHECKPOINT_DIR = PROJECT_ROOT / "logs" / "checkpoints"
LOCK_DIR = PROJECT_ROOT / "logs" / "locks"


@dataclass
class Checkpoint:
    """Persisted progress for a full-corpus site run."""

    site: str
    csv_path: str
    committed_offset: int
    rows_committed: int
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "site": self.site,
            "csv_path": self.csv_path,
            "committed_offset": self.committed_offset,
            "rows_committed": self.rows_committed,
            "updated_at": self.updated_at
            or datetime.now(timezone.utc).isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Checkpoint":
        return cls(
            site=str(data.get("site", "")),
            csv_path=str(data.get("csv_path", "")),
            committed_offset=int(data.get("committed_offset", 0)),
            rows_committed=int(data.get("rows_committed", 0)),
            updated_at=str(data.get("updated_at", "")),
        )


def checkpoint_path(site: str) -> Path:
    return CHECKPOINT_DIR / f"{site}.json"


def load_checkpoint(site: str) -> Checkpoint | None:
    path = checkpoint_path(site)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Checkpoint.from_dict(data)
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("Checkpoint unreadable for '%s' (%s); ignoring.", site, exc)
        return None


def save_checkpoint(checkpoint: Checkpoint) -> None:
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    path = checkpoint_path(checkpoint.site)
    tmp = path.with_suffix(".json.tmp")
    payload = checkpoint.to_dict()
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def clear_checkpoint(site: str) -> None:
    path = checkpoint_path(site)
    if path.is_file():
        path.unlink()


class DomainLock:
    """Exclusive lock so only one process scrapes a given domain at a time."""

    def __init__(self, domain: str) -> None:
        safe = domain.replace("/", "_").replace(":", "_")
        LOCK_DIR.mkdir(parents=True, exist_ok=True)
        self.path = LOCK_DIR / f"{safe}.lock"
        self._fh = None

    def acquire(self) -> None:
        import fcntl

        self._fh = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._fh.close()
            self._fh = None
            raise RuntimeError(
                f"Domain lock held by another process: {self.path.name}"
            ) from exc
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(f"pid={os.getpid()}\n")
        self._fh.flush()

    def release(self) -> None:
        import fcntl

        if self._fh is None:
            return
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "DomainLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
