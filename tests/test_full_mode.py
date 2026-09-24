"""Unit tests for crash-safe full-corpus writer, checkpoints, and domain lock."""

from __future__ import annotations

import csv
import os
import threading
from pathlib import Path

import pytest

from src.checkpoint import Checkpoint, DomainLock, clear_checkpoint, load_checkpoint, save_checkpoint
from src.schema import Article
from src import writer


def _article(n: int, *, text: str = "body text") -> Article:
    return Article(
        institution="Test Paper",
        title=f"Title {n}",
        subtitle="",
        author="Author",
        publication_date="2020-01-01",
        section="News",
        subsection="",
        url=f"https://example.com/article/{n}",
        text=text,
        scraped_at="2026-01-01T00:00:00+00:00",
    )


@pytest.fixture()
def isolated_dirs(tmp_path, monkeypatch):
    """Redirect writer/checkpoint paths into a temporary directory."""
    out = tmp_path / "output"
    logs = tmp_path / "logs"
    ckpt = logs / "checkpoints"
    locks = logs / "locks"
    out.mkdir()
    ckpt.mkdir(parents=True)
    locks.mkdir(parents=True)
    monkeypatch.setattr(writer, "OUTPUT_DIR", out)
    monkeypatch.setattr(writer, "LOG_DIR", logs)
    monkeypatch.setattr(writer, "PROJECT_ROOT", tmp_path)
    import src.checkpoint as checkpoint

    monkeypatch.setattr(checkpoint, "CHECKPOINT_DIR", ckpt)
    monkeypatch.setattr(checkpoint, "LOCK_DIR", locks)
    monkeypatch.setattr(checkpoint, "PROJECT_ROOT", tmp_path)
    return tmp_path, out, logs


def test_schema_field_order():
    assert Article.fieldnames() == [
        "institution",
        "title",
        "subtitle",
        "author",
        "publication_date",
        "section",
        "subsection",
        "url",
        "text",
        "scraped_at",
    ]


def test_header_written_once(isolated_dirs):
    _, out, _ = isolated_dirs
    site = "chicago"
    ckpt = writer.prepare_append_csv(site)
    assert ckpt.rows_committed == 0
    path = out / "chicago.csv"
    with path.open(encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    assert rows == [Article.fieldnames()]

    ckpt = writer.append_article_batch(site, [_article(1)], ckpt)
    ckpt = writer.append_article_batch(site, [_article(2)], ckpt)
    with path.open(encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == Article.fieldnames()
    assert len(rows) == 3
    assert ckpt.rows_committed == 2


def test_wrong_header_rejected(isolated_dirs):
    _, out, _ = isolated_dirs
    path = out / "chicago.csv"
    path.write_text("url,title\nhttps://x,y\n", encoding="utf-8")
    with pytest.raises(ValueError, match="header mismatch"):
        writer.prepare_append_csv("chicago")


def test_empty_text_preflight_blocks(isolated_dirs):
    _, out, _ = isolated_dirs
    writer.write_site_csv("chicago", [_article(1, text="")])
    with pytest.raises(ValueError, match="empty text"):
        writer.prepare_append_csv("chicago")


def test_batch_checkpoint_of_fifty(isolated_dirs):
    site = "chicago"
    ckpt = writer.prepare_append_csv(site)
    batch = [_article(i) for i in range(writer.BATCH_SIZE)]
    ckpt = writer.append_article_batch(site, batch, ckpt)
    assert ckpt.rows_committed == writer.BATCH_SIZE
    loaded = load_checkpoint(site)
    assert loaded is not None
    assert loaded.rows_committed == writer.BATCH_SIZE
    assert loaded.committed_offset == writer.site_csv_path(site).stat().st_size


def test_interrupted_append_rollback(isolated_dirs):
    """Simulate crash after append bytes but before checkpoint advance."""
    site = "chicago"
    ckpt = writer.prepare_append_csv(site)
    ckpt = writer.append_article_batch(site, [_article(1), _article(2)], ckpt)
    committed = ckpt.committed_offset

    # Corrupt: write extra bytes without saving checkpoint (crash mid-batch).
    path = writer.site_csv_path(site)
    orphan = writer._serialize_rows([_article(99)], include_header=False)
    with path.open("ab") as fh:
        fh.write(orphan)
        fh.flush()
        os.fsync(fh.fileno())
    assert path.stat().st_size > committed

    # Restart: truncate uncommitted tail, then resume without refetching 1–2.
    ckpt2 = writer.prepare_append_csv(site)
    assert ckpt2.committed_offset == committed
    assert path.stat().st_size == committed
    existing = writer.read_site_csv(site)
    assert {a.url for a in existing} == {
        "https://example.com/article/1",
        "https://example.com/article/2",
    }
    assert 99 not in {int(a.url.rsplit("/", 1)[-1]) for a in existing}


def test_resume_skip_urls_nonempty(isolated_dirs):
    site = "chicago"
    ckpt = writer.prepare_append_csv(site)
    writer.append_article_batch(site, [_article(1), _article(2)], ckpt)
    existing = writer.read_site_csv(site)
    skip = {a.url for a in existing if a.text.strip()}
    assert skip == {
        "https://example.com/article/1",
        "https://example.com/article/2",
    }


def test_failure_logging(isolated_dirs):
    _, _, logs = isolated_dirs
    writer.log_failed_url("chicago", "https://example.com/a", 2019, "empty_body")
    writer.log_failed_url("chicago", "https://example.com/b", 1999, "pre_2000")
    path = logs / "failed_chicago.csv"
    with path.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["reason"] == "empty_body"
    assert rows[1]["year"] == "1999"
    assert list(rows[0].keys()) == writer.FAILED_FIELDNAMES


def test_sample_mode_rewrite_compatible(isolated_dirs):
    path = writer.write_site_csv("chicago", [_article(1), _article(2)])
    rows = writer.read_site_csv("chicago")
    assert len(rows) == 2
    # Rewrite replaces contents.
    writer.write_site_csv("chicago", [_article(3)])
    rows = writer.read_site_csv("chicago")
    assert len(rows) == 1
    assert rows[0].url.endswith("/3")
    assert path.is_file()


def test_domain_lock_exclusive(isolated_dirs):
    lock1 = DomainLock("chicagomaroon.com")
    lock1.acquire()
    lock2 = DomainLock("chicagomaroon.com")
    with pytest.raises(RuntimeError, match="Domain lock held"):
        lock2.acquire()
    lock1.release()
    lock2.acquire()
    lock2.release()


def test_domain_lock_context_manager(isolated_dirs):
    with DomainLock("example.com"):
        with pytest.raises(RuntimeError):
            DomainLock("example.com").acquire()


def test_checkpoint_atomic_save(isolated_dirs):
    save_checkpoint(
        Checkpoint(
            site="chicago",
            csv_path="output/chicago.csv",
            committed_offset=10,
            rows_committed=1,
        )
    )
    loaded = load_checkpoint("chicago")
    assert loaded is not None
    assert loaded.rows_committed == 1
    clear_checkpoint("chicago")
    assert load_checkpoint("chicago") is None


def test_chicago_full_discovery_emits_all_urls():
    from src.extractor import _all_chicago, _sample_chicago

    by_year = {
        1998: [["https://chicagomaroon.com/1/news/a/", "1998-01-01"]],
        2016: [
            ["https://chicagomaroon.com/2/news/b/", "2016-01-01"],
            ["https://chicagomaroon.com/3/news/c/", "2016-06-01"],
        ],
        2020: [["https://chicagomaroon.com/4/news/d/", "2020-01-01"]],
    }
    all_urls = _all_chicago(by_year)
    assert len(all_urls) == 4
    sample = _sample_chicago(by_year, year_start=2016, year_end=2020, per_year=1)
    # Sample never includes 1998 lastmod bucket when year_start=2016.
    assert all(m["year"] >= 2016 for m in sample)
    assert len(sample) <= 3


def test_chicago_full_mode_ignores_sample_max_articles():
    """Regression: full mode must not inherit sample max_articles=30."""
    from src.extractor import extract_chicago

    class FakeFetcher:
        def get(self, url):  # pragma: no cover - not reached if discovery empty
            raise AssertionError("should not fetch in this unit test")

    config = {
        "mode": "full",
        "max_articles": 30,  # sample default — must NOT cap full mode
        # no max_fetch → uncapped
        "refresh_discovery": False,
        "skip_urls": set(),
    }
    import src.extractor as ex

    metas = [
        {
            "url": f"https://chicagomaroon.com/{i}/news/a/",
            "title": "",
            "author": "",
            "publication_date": "",
            "section": "news",
            "lastmod": "2024-01-01",
            "year": 2024,
        }
        for i in range(40)
    ]

    def fake_discover(cfg, fetcher):
        return metas

    def fake_page(url, fetcher):
        return {
            "text": "hello body",
            "title": "T",
            "author": "A",
            "publication_date": "January 1, 2024",
            "subtitle": "",
            "section": "News",
            "subsection": "",
        }

    orig_d, orig_p = ex._discover_chicago, ex._extract_text_chicago
    try:
        ex._discover_chicago = fake_discover
        ex._extract_text_chicago = fake_page
        articles = list(extract_chicago(config, FakeFetcher()))
    finally:
        ex._discover_chicago = orig_d
        ex._extract_text_chicago = orig_p
    assert len(articles) == 40
