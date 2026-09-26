#!/usr/bin/env python3
"""Polite one-pass recon for tracker domains -> ``data/recon/<domain>.json``.

Per domain, with the honest research User-Agent and at least the site's
robots.txt crawl delay (6 s floor) between requests, about 8 requests total:

  1. ``robots.txt``: allowed?, Crawl-delay, listed sitemaps, ``/wp-json/`` rule
  2. homepage: status, CDN (``server`` / ``cf-ray`` ...), platform fingerprint
  3. sitemap index: WordPress-core / Yoast / AIOSEO / other, sub-sitemap count
  4. first and last post sub-sitemap: estimated post URL count
  5. three sample articles through the matching ``src/wordpress.py`` page
     profile (``sno`` or ``wp_generic``): field audit

A refused homepage (401/403/429) stops the pass for that domain, so a
protected site sees two requests. The recon is a suggestion only: humans
record decisions in ``data/target_overrides.csv``, and access from the
server's IP is still checked with ``scripts/access_probe.py``.

After a batch, rebuild the tracker so the facts land in ``data/targets.csv``,
``docs/TARGET_LIST_REPORT.md`` and ``docs/RECON_SUMMARY.md``:

    .venv/bin/python scripts/build_targets.py

Usage:
    python scripts/recon_site.py --domain lomabeat.com
    python scripts/recon_site.py --tab "Christian Colleges"
    python scripts/recon_site.py --all --workers 2
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.extractor import _page_year, normalize_date  # noqa: E402
from src.fetcher import USER_AGENT, Fetcher, SiteBlockedError  # noqa: E402
from src.wordpress import fetch_page  # noqa: E402

TRACKER_PATH = PROJECT_ROOT / "data" / "targets.csv"
RECON_DIR = PROJECT_ROOT / "data" / "recon"

MIN_DELAY = 6.0
REFUSED_STATUSES = frozenset({401, 403, 407, 429})
AUDIT_FIELDS = ("text", "title", "author", "publication_date", "section")
WORDPRESS_SITEMAPS = frozenset({"wp_core", "yoast", "aioseo"})

logger = logging.getLogger("recon")


# ----------------------------------------------------------------------
# Pure helpers (unit-tested offline)
# ----------------------------------------------------------------------

def detect_cdn(headers: dict) -> str:
    h = {k.lower(): str(v).lower() for k, v in (headers or {}).items()}
    server = h.get("server", "")
    if "cf-ray" in h or "cloudflare" in server:
        return "cloudflare"
    if "x-vercel-id" in h or "vercel" in server:
        return "vercel"
    if "x-amz-cf-id" in h or "cloudfront" in h.get("via", ""):
        return "cloudfront"
    if "awselb" in server:
        return "aws_elb"
    if "x-sucuri-id" in h or "sucuri" in server:
        return "sucuri"
    if "x-fastly-request-id" in h or "fastly" in h.get("x-served-by", "") or "varnish" in h.get("via", ""):
        return "fastly"
    if "akamai" in server or "x-akamai-transformed" in h:
        return "akamai"
    return "none"


def detect_platform(html: str) -> str:
    lower = (html or "").lower()
    if "snosites" in lower or "sno-story" in lower or "sno-header" in lower:
        return "sno"
    if "snworks" in lower:
        return "snworks"
    if "bloximages" in lower or "tncms" in lower:
        return "blox"
    if "wp-content" in lower or "wp-json" in lower:
        return "wordpress"
    return "unknown"


def _sub_number(url: str) -> int:
    m = re.search(r"(\d+)\.xml", url)
    return int(m.group(1)) if m else 1


def classify_sitemap(xml: str) -> tuple[str, list[str]]:
    """``(kind, post_sub_sitemaps)`` for a sitemap index or urlset."""
    soup = BeautifulSoup(xml or "", "xml")
    if soup.find("urlset") is not None:
        return "single", []
    locs = [loc.text.strip() for loc in soup.find_all("loc") if loc.text]
    wp_core = [u for u in locs if "wp-sitemap-posts-post-" in u]
    if wp_core:
        return "wp_core", sorted(wp_core, key=_sub_number)
    posts = [u for u in locs if re.search(r"/post-sitemap\d*\.xml", u)]
    if posts:
        kind = "aioseo" if "aioseo" in (xml or "").lower() else "yoast"
        return kind, sorted(posts, key=_sub_number)
    if soup.find("sitemapindex") is not None:
        return "other", locs
    return "none", []


def url_locs(xml: str) -> list[str]:
    soup = BeautifulSoup(xml or "", "xml")
    return [
        loc.text.strip()
        for url_el in soup.find_all("url")
        if (loc := url_el.find("loc")) is not None and loc.text
    ]


def estimate_urls(n_subs: int, first_count: int, last_count: int) -> int:
    """Every sub-sitemap but the last is full (``first_count`` URLs)."""
    if n_subs <= 0:
        return 0
    if n_subs == 1:
        return first_count
    return (n_subs - 1) * first_count + last_count


def pick_samples(first_urls: list[str], last_urls: list[str]) -> list[str]:
    """Oldest, a middle one, and the newest post URL, without repeats."""
    pool = first_urls or last_urls
    if not pool:
        return []
    picks = [pool[0], pool[len(pool) // 2], (last_urls or first_urls)[-1]]
    return list(dict.fromkeys(picks))


def suggest_access_profile(record: dict) -> str:
    if record.get("robots_allows_home") is False:
        return "excluded"
    status = record.get("home_status")
    if status is None:
        return ""  # unreachable (network/TLS error): unknown, not open
    if status == 401:
        return "excluded"
    if status in REFUSED_STATUSES:
        return "waf_browser_ua"
    if any(a.get("status") in REFUSED_STATUSES for a in record.get("articles", [])):
        return "waf_browser_ua"
    if record.get("cdn") == "cloudflare":
        return "cloudflare"
    return "open"


# ----------------------------------------------------------------------
# Network pass
# ----------------------------------------------------------------------

def _get(fetcher: Fetcher, url: str, record: dict) -> requests.Response | None:
    """GET through the polite fetcher; never raises for HTTP/network errors."""
    record["requests"] += 1
    try:
        return fetcher.get(url)
    except requests.HTTPError as exc:
        resp = exc.response
        record["notes"].append(f"http_{resp.status_code if resp is not None else '?'}: {url}")
        return resp
    except requests.RequestException as exc:
        record["notes"].append(f"{type(exc).__name__}: {url}")
        return None


def _read_robots(base: str, record: dict) -> RobotFileParser | None:
    record["requests"] += 1
    try:
        resp = requests.get(f"{base}/robots.txt", headers={"User-Agent": USER_AGENT}, timeout=20)
    except requests.RequestException as exc:
        record["robots_status"] = type(exc).__name__
        return None
    record["robots_status"] = resp.status_code
    if resp.status_code >= 400:
        return None
    parser = RobotFileParser()
    parser.parse(resp.text.splitlines())
    return parser


def recon_domain(domain: str, start_url: str) -> dict:
    parsed = urlparse(start_url if "://" in start_url else f"https://{start_url}")
    base = f"{parsed.scheme or 'https'}://{parsed.netloc or domain}"
    record: dict = {
        "domain": domain,
        "base_url": base,
        "reconned_at": datetime.now(timezone.utc).isoformat(),
        "user_agent": "honest_research",
        "requests": 0,
        "notes": [],
        "articles": [],
    }

    robots = _read_robots(base, record)
    crawl = None
    sitemaps: list[str] = []
    if robots is not None:
        crawl = robots.crawl_delay(USER_AGENT) or robots.crawl_delay("*")
        sitemaps = list(robots.site_maps() or [])
        record["robots_allows_home"] = robots.can_fetch(USER_AGENT, f"{base}/")
        record["wp_json_allowed"] = robots.can_fetch(USER_AGENT, f"{base}/wp-json/wp/v2/posts")
    record["crawl_delay"] = float(crawl) if crawl else None
    if record.get("robots_allows_home") is False:
        record["access_profile"] = suggest_access_profile(record)
        return record

    delay = max(float(crawl or 0), MIN_DELAY)
    fetcher = Fetcher(
        delay_min=delay,
        delay_max=delay + 2,
        max_retries=1,
        timeout=20,
        block_threshold=2,
        block_cooldowns=(),
    )
    try:
        _recon_pages(fetcher, base, sitemaps, record)
    except SiteBlockedError as exc:
        record["notes"].append(f"stopped: {exc}")
    finally:
        fetcher.close()
    record["access_profile"] = suggest_access_profile(record)
    return record


def _recon_pages(fetcher: Fetcher, base: str, sitemaps: list[str], record: dict) -> None:
    home = _get(fetcher, f"{base}/", record)
    record["home_status"] = home.status_code if home is not None else None
    if home is None:
        return
    record["server"] = home.headers.get("server", "")
    record["cdn"] = detect_cdn(dict(home.headers))
    if home.status_code in REFUSED_STATUSES:
        return
    final = urlparse(home.url)
    base = f"{final.scheme}://{final.netloc}"
    record["base_url"] = base
    record["platform"] = detect_platform(home.text)

    candidates = [u for u in sitemaps if urlparse(u).netloc == final.netloc]
    candidates += [f"{base}/sitemap.xml", f"{base}/sitemap_index.xml", f"{base}/wp-sitemap.xml"]
    kind, subs, single_urls = "none", [], []
    for url in list(dict.fromkeys(candidates))[:3]:
        resp = _get(fetcher, url, record)
        if resp is None or resp.status_code != 200 or "<" not in resp.text[:200]:
            continue
        kind, subs = classify_sitemap(resp.text)
        if kind == "single":
            single_urls = url_locs(resp.text)
        if kind != "none":
            record["sitemap_url"] = url
            break
    record["sitemap_kind"] = kind
    record["post_sitemaps"] = len(subs)

    first_urls, last_urls = single_urls, []
    if kind in WORDPRESS_SITEMAPS and subs:
        first = _get(fetcher, subs[0], record)
        first_urls = url_locs(first.text) if first is not None and first.ok else []
        if len(subs) > 1:
            last = _get(fetcher, subs[-1], record)
            last_urls = url_locs(last.text) if last is not None and last.ok else []
        if first_urls:
            record["est_urls"] = estimate_urls(len(subs), len(first_urls), len(last_urls))
    elif kind == "single":
        record["est_urls"] = len(single_urls)

    if record["platform"] not in {"sno", "wordpress"} and kind not in WORDPRESS_SITEMAPS:
        record["notes"].append("no page adapter yet; field audit skipped")
        return
    years = []
    record["page_profile"] = "sno" if record["platform"] == "sno" else "wp_generic"
    for url in pick_samples(first_urls, last_urls):
        record["requests"] += 1
        page = fetch_page(url, fetcher, record["domain"], record["page_profile"])
        pub = normalize_date(page.get("publication_date", ""), url) if page.get("text") else ""
        year = _page_year(pub)
        if year:
            years.append(year)
        status = page.get("error", "")
        m = re.match(r"http_(\d+)", status)
        record["articles"].append(
            {
                "url": url,
                "status": int(m.group(1)) if m else (200 if page.get("text") else None),
                "error": status,
                "publication_date": pub,
                "text_chars": len(page.get("text", "")),
                **{f"has_{f}": bool(page.get(f)) for f in AUDIT_FIELDS},
            }
        )
    if years:
        record["earliest_year"] = min(years)
    n = len(record["articles"])
    if n:
        record["field_fill"] = {
            f: f"{sum(a[f'has_{f}'] for a in record['articles'])}/{n}" for f in AUDIT_FIELDS
        }


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def load_targets(tab: str | None, domains: list[str] | None, include_configured: bool) -> list[dict]:
    with TRACKER_PATH.open(newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if r["domain"]]
    if domains:
        wanted = set(domains)
        return [r for r in rows if r["domain"] in wanted]
    rows = [r for r in rows if r["status"] != "excluded"]
    if not include_configured:
        # Configured sites are already mapped, and some are mid-run: no extra traffic.
        rows = [r for r in rows if not r["site_key"]]
    if tab:
        rows = [r for r in rows if tab in r["category_labels"].split(" | ")]
    return rows


def _run_one(row: dict, refresh: bool) -> str:
    domain = row["domain"]
    out = RECON_DIR / f"{domain}.json"
    if out.is_file() and not refresh:
        return f"{domain}: cached (use --refresh to redo)"
    record = recon_domain(domain, row.get("sample_url") or f"https://{domain}/")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    fill = record.get("field_fill", {})
    return (
        f"{domain}: home={record.get('home_status')} cdn={record.get('cdn')} "
        f"platform={record.get('platform')} sitemap={record.get('sitemap_kind')} "
        f"est_urls={record.get('est_urls')} access={record.get('access_profile')} "
        f"fill={fill.get('publication_date', '-')} date / {fill.get('author', '-')} author"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--domain", action="append", dest="domains", help="Tracker domain (repeatable)")
    scope.add_argument("--tab", help="Every non-excluded domain in this category")
    scope.add_argument("--all", action="store_true", help="Every non-excluded domain")
    parser.add_argument("--workers", type=int, default=1, help="Domains in parallel (max 3)")
    parser.add_argument("--refresh", action="store_true", help="Redo domains that already have recon")
    parser.add_argument(
        "--include-configured", action="store_true", help="Also recon sites already in sites.yaml"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")

    rows = load_targets(args.tab, args.domains, args.include_configured)
    if args.domains:
        missing = set(args.domains) - {r["domain"] for r in rows}
        if missing:
            parser.error(f"not in data/targets.csv: {', '.join(sorted(missing))}")
    workers = max(1, min(args.workers, 3))
    print(f"Recon: {len(rows)} domain(s), {workers} at a time", flush=True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for line in pool.map(lambda r: _run_one(r, args.refresh), rows):
            print(line, flush=True)
    print("\nNext: .venv/bin/python scripts/build_targets.py", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
