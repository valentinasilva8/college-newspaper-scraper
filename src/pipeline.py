"""Orchestrator: load config, run site extractors, dedup, write output.

The pipeline is deliberately site-agnostic. It resolves extractors from the
``SITE_EXTRACTORS`` registry, applies cross-site URL deduplication, and hands
records to the writer. All site-specific behavior lives in ``extractor.py``.

Sample mode rewrites ``output/<site>.csv`` at the end of a run.
Full mode appends batches of 50 with crash-safe checkpoints.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from .checkpoint import DomainLock
from .extractor import SITE_EXTRACTORS
from .fetcher import Fetcher
from .schema import Article
from .writer import (
    BATCH_SIZE,
    append_article_batch,
    log_failed_url,
    prepare_append_csv,
    read_site_csv,
    write_combined_csv,
    write_site_csv,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "sites.yaml"
LOG_DIR = PROJECT_ROOT / "logs"
PROGRESS_EVERY = 100


def setup_logging(level: int = logging.INFO) -> None:
    """Configure logging to both stdout and a timestamped file in logs/."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_file = LOG_DIR / f"scrape_{stamp}.log"

    handlers: list[logging.Handler] = [
        logging.StreamHandler(),
        logging.FileHandler(log_file, encoding="utf-8"),
    ]
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    logger.info("Logging to %s", log_file)


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    """Load and lightly normalize the YAML site configuration."""
    with path.open(encoding="utf-8") as fh:
        config = yaml.safe_load(fh) or {}
    return config


def _merge_site_config(defaults: dict, site_cfg: dict) -> dict:
    """Overlay a site's config on top of global defaults."""
    merged = dict(defaults or {})
    merged.update(site_cfg or {})
    # rate_limit is a nested dict; merge it explicitly so a site can override
    # just one knob without dropping the others.
    rl = dict((defaults or {}).get("rate_limit", {}))
    rl.update((site_cfg or {}).get("rate_limit", {}))
    if rl:
        merged["rate_limit"] = rl
    return merged


def _build_fetcher(site_config: dict) -> Fetcher:
    rl = site_config.get("rate_limit", {}) or {}
    return Fetcher(
        delay_min=rl.get("delay_min", 1.0),
        delay_max=rl.get("delay_max", 3.0),
        max_concurrency=rl.get("max_concurrency", 1),
    )


def _domain_from_config(site_config: dict) -> str:
    base = site_config.get("base_url") or site_config.get("sitemap_index") or ""
    host = urlparse(str(base)).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host or "unknown"


def run_site(
    site_key: str,
    config: dict[str, Any],
    seen_urls: set[str],
    *,
    incremental: bool = True,
) -> list[Article]:
    """Run a single site's extractor; return the merged per-site corpus.

    When ``incremental`` is True (default), existing rows in
    ``output/<site>.csv`` are preserved and their URLs are passed to the
    extractor via ``skip_urls`` so phase-2 fetches are not repeated.
    """
    if site_key not in SITE_EXTRACTORS:
        raise KeyError(f"No extractor registered for site '{site_key}'")

    defaults = config.get("defaults", {})
    sites = config.get("sites", {})
    site_cfg = _merge_site_config(defaults, sites.get(site_key, {}))
    # Sample mode is the default path; full mode uses run_site_full.
    site_cfg.setdefault("mode", "sample")
    if str(site_cfg.get("mode", "sample")).lower() == "full":
        return run_site_full(
            site_key,
            config,
            seen_urls,
            overwrite=not incremental,
            refresh_discovery=site_cfg.get("refresh_discovery", False),
            max_fetch=site_cfg.get("max_fetch"),
        )

    existing: list[Article] = read_site_csv(site_key) if incremental else []
    # Only skip URLs whose existing row already has body text (re-fetch empties).
    skip_urls = {article.url for article in existing if article.text.strip()}
    site_cfg["skip_urls"] = skip_urls
    site_cfg["refresh_discovery"] = not incremental

    for article in existing:
        seen_urls.add(article.url)

    extractor = SITE_EXTRACTORS[site_key]
    fetcher = _build_fetcher(site_cfg)

    logger.info(
        "Running extractor for '%s' (%d existing row(s), incremental=%s)",
        site_key,
        len(existing),
        incremental,
    )
    new_articles: list[Article] = []
    try:
        for article in extractor(site_cfg, fetcher):
            if article.url in seen_urls:
                logger.debug("Duplicate URL skipped: %s", article.url)
                continue
            seen_urls.add(article.url)
            new_articles.append(article)
    finally:
        fetcher.close()

    updated_urls = {article.url for article in new_articles}
    merged = [
        article
        for article in existing
        if article.url not in updated_urls and article.text.strip()
    ]
    merged.extend(new_articles)
    write_site_csv(site_key, merged)
    logger.info(
        "Site '%s': %d new row(s), %d total row(s) written.",
        site_key,
        len(new_articles),
        len(merged),
    )
    return merged


def run_site_full(
    site_key: str,
    config: dict[str, Any],
    seen_urls: set[str],
    *,
    overwrite: bool = False,
    refresh_discovery: bool = False,
    max_fetch: int | None = None,
) -> list[Article]:
    """Crash-safe full-corpus run: append batches of 50 with checkpoints."""
    if site_key not in SITE_EXTRACTORS:
        raise KeyError(f"No extractor registered for site '{site_key}'")

    defaults = config.get("defaults", {})
    sites = config.get("sites", {})
    site_cfg = _merge_site_config(defaults, sites.get(site_key, {}))
    site_cfg["mode"] = "full"
    site_cfg["refresh_discovery"] = bool(refresh_discovery)
    if max_fetch is not None:
        site_cfg["max_fetch"] = int(max_fetch)

    domain = _domain_from_config(site_cfg)
    lock = DomainLock(domain)
    lock.acquire()
    try:
        checkpoint = prepare_append_csv(site_key, overwrite=overwrite)
        existing = read_site_csv(site_key)
        # Resume: only skip URLs that already have non-empty text.
        skip_urls = {article.url for article in existing if article.text.strip()}
        site_cfg["skip_urls"] = skip_urls
        for article in existing:
            seen_urls.add(article.url)

        def _failure_sink(url: str, year: Any, reason: str) -> None:
            log_failed_url(site_key, url, year, reason)

        site_cfg["failure_sink"] = _failure_sink

        extractor = SITE_EXTRACTORS[site_key]
        fetcher = _build_fetcher(site_cfg)
        batch: list[Article] = []
        new_count = 0
        started = time.monotonic()
        logger.info(
            "Full-mode run for '%s' (domain=%s, existing=%d, overwrite=%s, "
            "refresh_discovery=%s, max_fetch=%s)",
            site_key,
            domain,
            len(skip_urls),
            overwrite,
            refresh_discovery,
            site_cfg.get("max_fetch"),
        )
        try:
            for article in extractor(site_cfg, fetcher):
                if article.url in seen_urls:
                    logger.debug("Duplicate URL skipped: %s", article.url)
                    continue
                if not article.text.strip():
                    log_failed_url(site_key, article.url, None, "empty_body")
                    continue
                seen_urls.add(article.url)
                batch.append(article)
                new_count += 1
                if len(batch) >= BATCH_SIZE:
                    checkpoint = append_article_batch(site_key, batch, checkpoint)
                    batch = []
                if new_count % PROGRESS_EVERY == 0:
                    elapsed = max(time.monotonic() - started, 1e-6)
                    rate = new_count / elapsed
                    logger.info(
                        "Progress '%s': %d new article(s), %.2f art/s, "
                        "ETA unknown (open-ended full corpus)",
                        site_key,
                        new_count,
                        rate,
                    )
            if batch:
                checkpoint = append_article_batch(site_key, batch, checkpoint)
        finally:
            fetcher.close()

        merged = read_site_csv(site_key)
        elapsed = max(time.monotonic() - started, 1e-6)
        logger.info(
            "Site '%s' full mode: %d new row(s), %d total, %.2f art/s, "
            "committed_offset=%d",
            site_key,
            new_count,
            len(merged),
            new_count / elapsed if new_count else 0.0,
            checkpoint.committed_offset,
        )
        return merged
    finally:
        lock.release()


def run(
    site: str,
    config: dict[str, Any] | None = None,
    *,
    incremental: bool = True,
    write_combined: bool = False,
    mode: str | None = None,
    refresh_discovery: bool = False,
    max_fetch: int | None = None,
    lastmod_year: int | None = None,
) -> list[Article]:
    """Run one site or all sites; write per-site CSV output.

    ``site`` is a site key (e.g. "duke") or the literal "all".
    ``write_combined`` is off by default so ``combined.csv`` is only rebuilt
    when explicitly requested.
    ``mode`` overrides each site's configured ``mode`` when provided.
    """
    config = config if config is not None else load_config()
    # Apply CLI overrides onto a shallow copy so callers can reuse the original.
    config = dict(config)
    sites = dict(config.get("sites") or {})
    config["sites"] = sites

    if site == "all":
        site_keys = list(SITE_EXTRACTORS.keys())
    else:
        site_keys = [site]

    for key in site_keys:
        site_cfg = dict(sites.get(key) or {})
        if mode is not None:
            site_cfg["mode"] = mode
        if refresh_discovery:
            site_cfg["refresh_discovery"] = True
        if max_fetch is not None:
            site_cfg["max_fetch"] = max_fetch
        if lastmod_year is not None:
            site_cfg["candidate_lastmod_year"] = lastmod_year
        sites[key] = site_cfg

    seen_urls: set[str] = set()
    all_articles: list[Article] = []
    for key in site_keys:
        site_mode = str((sites.get(key) or {}).get("mode", "sample")).lower()
        if site_mode == "full":
            all_articles.extend(
                run_site_full(
                    key,
                    config,
                    seen_urls,
                    overwrite=not incremental,
                    refresh_discovery=bool(
                        (sites.get(key) or {}).get("refresh_discovery", False)
                    ),
                    max_fetch=(sites.get(key) or {}).get("max_fetch"),
                )
            )
        else:
            all_articles.extend(
                run_site(key, config, seen_urls, incremental=incremental)
            )

    if write_combined:
        write_combined_csv(all_articles)
    logger.info(
        "Done. %d site(s), %d unique article(s) total.",
        len(site_keys),
        len(all_articles),
    )
    return all_articles
