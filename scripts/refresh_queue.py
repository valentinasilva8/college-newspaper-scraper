#!/usr/bin/env python3
"""Build and refresh ``data/scrape_queue.csv`` from the ranking CSVs.

One row per newspaper domain. Categories are National Universities, Public
Universities, Liberal Arts, and Christian Colleges (a dual-label paper keeps
one primary category for queue grouping, with the rest in ``also_categories``).

Queue states:

  done            corpus file finished (Status tab or systemd inactive + high %)
  running         systemd unit active, or Status says running
  ready           in sites.yaml, 200-test passed, not yet started (auto-fill only)
  candidate       WP/SNO with a sitemap; needs yaml + probe + 200-test first
  needs_adapter   SNWorks / BLOX / WAF-unknown / no sitemap / unknown CMS
  blocked         probe or 200-test failed, or known park (Pitt, Hilltop, …)
  excluded        Instagram, login, PDF archive, wrong school, discontinued
  later_pass      more work later on the same CSV (Duke other sections)
  mac_only        must run from the Mac (Yale live site)

``ready`` is never invented here from a ranking URL alone. Mark a domain ready
by writing a row in ``data/queue_ready.txt`` (one site_key per line) after a
200-test passes, or by passing ``--mark-ready SITE_KEY``.

Usage:
    python scripts/refresh_queue.py
    python scripts/refresh_queue.py --mark-ready williams
    python scripts/refresh_queue.py --rankings-dir data/rankings
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

try:
    import yaml
except ImportError:  # pragma: no cover - server always has PyYAML via requirements
    yaml = None

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RANKINGS = PROJECT_ROOT / "data" / "rankings"
TRACKER_PATH = PROJECT_ROOT / "data" / "targets.csv"
STATUS_XLSX = PROJECT_ROOT / "data" / "targets_raw.xlsx"
SITES_YAML = PROJECT_ROOT / "config" / "sites.yaml"
QUEUE_PATH = PROJECT_ROOT / "data" / "scrape_queue.csv"
READY_PATH = PROJECT_ROOT / "data" / "queue_ready.txt"
OVERRIDES_PATH = PROJECT_ROOT / "data" / "target_overrides.csv"

RANKING_FILES = {
    "National Universities": "national_universities.csv",
    "Public Universities": "public_universities.csv",
    "Liberal Arts": "liberal_arts.csv",
    "Christian Colleges": "christian_colleges.csv",
}

# Prefer this order when a domain appears on more than one tab.
CATEGORY_PLACE_ORDER = (
    "Public Universities",
    "Christian Colleges",
    "Liberal Arts",
    "National Universities",
)

# Domains that must not auto-start even if they look like WordPress.
BLOCKED_DOMAINS = {
    "pittnews.com": "200-test HTTP 403 on /opinions/satire/",
    "thehilltoponline.com": "homepage hang / HTTP 508; Crawl-delay 30",
    "thelantern.com": "sitemap timeout; Crawl-delay 30",
    "cuindependent.com": "recon sampled SEO/spam pages",
    "panthernow.com": "robots Crawl-delay 60; occupies a slot for weeks",
}

LATER_PASS = {
    "dukechronicle.com": "News-section pass done; Opinion/Sports/etc. later",
}

MAC_ONLY = {
    "yaledailynews.com": "live site Playwright on Mac only; Vercel 429 on VM; library PDFs out of queue",
}

# Verified WordPress/SNO with a sitemap but not yet in data/recon/.
# Keep these as candidate so they enter the ready-buffer pipeline.
KNOWN_CANDIDATES = {
    "bachelor.wabash.edu": "The Bachelor; Yoast sitemap_index.xml (verified 2026-10-08)",
    "wheatonwire.com": "The Wheaton Wire (Wheaton MA); wp-sitemap.xml (verified 2026-10-08)",
}

URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)


def domain_of(url: str) -> str:
    if not url:
        return ""
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def extract_url(cells: list[str]) -> str:
    for cell in cells:
        if not cell:
            continue
        m = URL_RE.search(cell)
        if m:
            return m.group(0).rstrip(".,);]")
    return ""


def load_tracker() -> dict[str, dict]:
    if not TRACKER_PATH.is_file():
        return {}
    with TRACKER_PATH.open(encoding="utf-8", newline="") as fh:
        return {row["domain"]: row for row in csv.DictReader(fh) if row.get("domain")}


def load_sites_yaml() -> dict[str, dict]:
    if yaml is None or not SITES_YAML.is_file():
        return {}
    data = yaml.safe_load(SITES_YAML.read_text(encoding="utf-8")) or {}
    sites = data.get("sites") or {}
    by_domain: dict[str, dict] = {}
    for key, cfg in sites.items():
        base = (cfg or {}).get("base_url") or ""
        d = domain_of(base)
        if d:
            by_domain[d] = {"site_key": key, **(cfg or {})}
    return by_domain


def load_status_tab() -> dict[str, dict]:
    """``{domain: {status, site_key, university, paper, articles, notes}}``."""
    if not STATUS_XLSX.is_file():
        return {}
    try:
        import openpyxl
    except ImportError:
        return {}
    wb = openpyxl.load_workbook(STATUS_XLSX, data_only=True)
    if "Status" not in wb.sheetnames:
        return {}
    ws = wb["Status"]
    out: dict[str, dict] = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or not row[3]:
            continue
        domain = str(row[3]).strip().lower()
        if domain in ("domain",):
            continue
        out[domain] = {
            "university": row[0],
            "paper": row[1],
            "site_key": row[2],
            "domain": domain,
            "category": row[4],
            "status": (row[5] or "").strip().lower() if row[5] else "",
            "articles": row[6],
            "notes": row[9] if len(row) > 9 else "",
        }
    return out


def load_ready_keys() -> set[str]:
    if not READY_PATH.is_file():
        return set()
    keys: set[str] = set()
    for line in READY_PATH.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            keys.add(line)
    return keys


def save_ready_keys(keys: set[str]) -> None:
    READY_PATH.parent.mkdir(parents=True, exist_ok=True)
    READY_PATH.write_text(
        "# site_keys cleared for auto-fill (200-test passed)\n"
        + "\n".join(sorted(keys))
        + ("\n" if keys else ""),
        encoding="utf-8",
    )


def systemd_active_sites() -> set[str]:
    if not shutil.which("systemctl"):
        return set()
    try:
        result = subprocess.run(
            [
                "systemctl",
                "list-units",
                "--type=service",
                "--state=running",
                "newspaper-scraper@*",
                "--no-pager",
                "--no-legend",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.SubprocessError, OSError):
        return set()
    active: set[str] = set()
    for line in result.stdout.splitlines():
        # newspaper-scraper@michigan.service ...
        m = re.search(r"newspaper-scraper@([a-z0-9_-]+)\.service", line)
        if m:
            active.add(m.group(1))
    return active


def read_ranking_rows(rankings_dir: Path) -> list[dict]:
    """Flatten ranking CSVs into university rows with category + url."""
    rows: list[dict] = []
    for category, filename in RANKING_FILES.items():
        path = rankings_dir / filename
        if not path.is_file():
            print(f"warning: missing {path}", file=sys.stderr)
            continue
        with path.open(encoding="utf-8-sig", newline="") as fh:
            reader = csv.reader(fh)
            header = next(reader, None)
            if not header:
                continue
            for raw in reader:
                if not raw or not (raw[0] or "").strip():
                    continue
                university = raw[0].strip()
                ranking = raw[1].strip() if len(raw) > 1 else ""
                url = extract_url(raw[2:])
                # Instagram-only strings without a clean student-paper URL
                notes = ""
                if not url and any("instagram.com" in (c or "").lower() for c in raw):
                    notes = "instagram_only"
                    m = URL_RE.search(" ".join(raw))
                    if m and "instagram.com" in m.group(0):
                        url = m.group(0)
                rows.append(
                    {
                        "category": category,
                        "university": university,
                        "ranking": ranking,
                        "url": url,
                        "domain": domain_of(url) if url and "instagram.com" not in url else (
                            "instagram.com" if notes == "instagram_only" else ""
                        ),
                        "sheet_note": notes,
                    }
                )
    return rows


def pick_primary_category(categories: set[str]) -> str:
    for cat in CATEGORY_PLACE_ORDER:
        if cat in categories:
            return cat
    return sorted(categories)[0] if categories else ""


def classify(
    domain: str,
    tracker: dict[str, dict],
    sites: dict[str, dict],
    status: dict[str, dict],
    ready_keys: set[str],
    active: set[str],
    sheet_note: str,
) -> tuple[str, str, str]:
    """Return (queue_state, site_key, reason)."""
    t = tracker.get(domain) or {}
    st = status.get(domain) or {}
    site = sites.get(domain) or {}
    site_key = (st.get("site_key") or t.get("site_key") or site.get("site_key") or "").strip()

    exclusion = (t.get("exclusion_reason") or "").strip()
    if sheet_note == "instagram_only" or exclusion == "instagram_only":
        return "excluded", site_key, exclusion or "instagram_only"
    if exclusion:
        return "excluded", site_key, exclusion

    if domain in MAC_ONLY:
        return "mac_only", site_key or "yale", MAC_ONLY[domain]
    if domain in LATER_PASS:
        # News pass may already be done; still later_pass for remaining sections.
        return "later_pass", site_key or "duke", LATER_PASS[domain]
    if domain in BLOCKED_DOMAINS:
        return "blocked", site_key, BLOCKED_DOMAINS[domain]

    if site_key and site_key in active:
        return "running", site_key, "systemd active"
    status_val = (st.get("status") or "").lower()
    if status_val == "running":
        return "running", site_key, "Status tab running"
    if status_val == "done":
        return "done", site_key, "Status tab done"

    if site_key and site_key in ready_keys:
        return "ready", site_key, "200-test passed; waiting for a free slot"

    platform = (t.get("platform") or "").strip()
    access = (t.get("access_profile") or "").strip()
    sitemap_kind = (t.get("sitemap_kind") or "").strip()
    crawl_delay = t.get("crawl_delay") or ""

    if site_key and not status_val:
        # Configured but not on Status / not started.
        if access == "datacenter_blocked":
            return "mac_only", site_key, t.get("notes") or "datacenter blocked"
        return "candidate", site_key, "configured; needs 200-test before ready"

    if not domain:
        return "excluded", "", "missing newspaper URL"

    if platform == "snworks":
        return "needs_adapter", site_key, "SNWorks adapter unbuilt"
    if platform == "blox":
        return "needs_adapter", site_key, "BLOX adapter unbuilt"
    if platform in ("library_archive",) or exclusion == "pdf_archive":
        return "excluded", site_key, "PDF / library archive"
    if access == "waf_browser_ua" and not site_key:
        return "needs_adapter", site_key, "WAF / unknown CMS; browser UA may help later"
    if platform in ("possible_university_newsroom",):
        return "needs_adapter", site_key, "may be a university newsroom, not student paper"
    if crawl_delay in ("60.0", "60") or (crawl_delay and float(crawl_delay) >= 60):
        return "blocked", site_key, f"Crawl-delay {crawl_delay}"
    if crawl_delay in ("30.0", "30") or (crawl_delay and float(crawl_delay) >= 30):
        return "blocked", site_key, f"Crawl-delay {crawl_delay}"

    if domain in KNOWN_CANDIDATES:
        return "candidate", site_key, KNOWN_CANDIDATES[domain]
    if platform in ("sno", "wordpress") and sitemap_kind and sitemap_kind not in ("none", ""):
        return "candidate", site_key, f"{platform} + {sitemap_kind}; needs yaml/probe/200-test"
    if platform in ("sno", "wordpress"):
        return "needs_adapter", site_key, f"{platform} but no usable sitemap in recon"
    if platform in ("unknown", "") or not platform:
        return "needs_adapter", site_key, "unknown platform or missing recon"

    return "needs_adapter", site_key, f"platform={platform} access={access}"


def build_queue(rankings_dir: Path) -> list[dict]:
    tracker = load_tracker()
    sites = load_sites_yaml()
    status = load_status_tab()
    ready_keys = load_ready_keys()
    active = systemd_active_sites()
    ranking_rows = read_ranking_rows(rankings_dir)

    # Also inject Wabash / Wheaton Wire if ranking copies are stale but tracker has them.
    for domain, university, url, category in (
        ("bachelor.wabash.edu", "Wabash College", "https://bachelor.wabash.edu/", "Liberal Arts"),
        ("wheatonwire.com", "Wheaton College", "https://wheatonwire.com/", "Liberal Arts"),
    ):
        if not any(r["domain"] == domain for r in ranking_rows):
            ranking_rows.append(
                {
                    "category": category,
                    "university": university,
                    "ranking": "",
                    "url": url,
                    "domain": domain,
                    "sheet_note": "",
                }
            )

    by_domain: dict[str, dict] = {}
    no_domain_rows: list[dict] = []

    for r in ranking_rows:
        domain = r["domain"]
        if not domain:
            no_domain_rows.append(r)
            continue
        entry = by_domain.get(domain)
        if entry is None:
            by_domain[domain] = {
                "domain": domain,
                "url": r["url"],
                "universities": [r["university"]],
                "categories": {r["category"]},
                "rankings": {r["category"]: r["ranking"]},
                "sheet_note": r["sheet_note"],
            }
        else:
            if r["university"] not in entry["universities"]:
                entry["universities"].append(r["university"])
            entry["categories"].add(r["category"])
            entry["rankings"][r["category"]] = r["ranking"]
            if r["sheet_note"]:
                entry["sheet_note"] = r["sheet_note"]

    queue: list[dict] = []
    for domain, entry in by_domain.items():
        categories = entry["categories"]
        primary = pick_primary_category(categories)
        also = " | ".join(sorted(c for c in categories if c != primary))
        state, site_key, reason = classify(
            domain,
            tracker,
            sites,
            status,
            ready_keys,
            active,
            entry.get("sheet_note") or "",
        )
        t = tracker.get(domain) or {}
        st = status.get(domain) or {}
        queue.append(
            {
                "category": primary,
                "also_categories": also,
                "queue_state": state,
                "site_key": site_key,
                "domain": domain,
                "url": entry["url"] or t.get("sample_url") or "",
                "university": " | ".join(entry["universities"]),
                "paper": st.get("paper") or "",
                "platform": t.get("platform") or "",
                "access_profile": t.get("access_profile") or "",
                "est_urls": t.get("est_urls") or "",
                "est_days": t.get("est_days") or "",
                "articles": st.get("articles") or "",
                "reason": reason,
                "notes": (st.get("notes") or t.get("notes") or "")[:200],
            }
        )

    for r in no_domain_rows:
        queue.append(
            {
                "category": r["category"],
                "also_categories": "",
                "queue_state": "excluded",
                "site_key": "",
                "domain": "",
                "url": "",
                "university": r["university"],
                "paper": "",
                "platform": "",
                "access_profile": "",
                "est_urls": "",
                "est_days": "",
                "articles": "",
                "reason": "missing newspaper URL",
                "notes": "",
            }
        )

    # Stable order: category place order, then queue_state priority, then university.
    state_order = {
        "running": 0,
        "ready": 1,
        "candidate": 2,
        "later_pass": 3,
        "mac_only": 4,
        "needs_adapter": 5,
        "blocked": 6,
        "done": 7,
        "excluded": 8,
    }
    cat_order = {c: i for i, c in enumerate(CATEGORY_PLACE_ORDER)}

    def sort_key(row: dict) -> tuple:
        return (
            cat_order.get(row["category"], 99),
            state_order.get(row["queue_state"], 50),
            row["university"].lower(),
            row["domain"],
        )

    queue.sort(key=sort_key)
    return queue


QUEUE_FIELDS = [
    "category",
    "also_categories",
    "queue_state",
    "site_key",
    "domain",
    "url",
    "university",
    "paper",
    "platform",
    "access_profile",
    "est_urls",
    "est_days",
    "articles",
    "reason",
    "notes",
]


def write_queue(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=QUEUE_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in QUEUE_FIELDS})


def print_summary(rows: list[dict]) -> None:
    by_state: dict[str, int] = defaultdict(int)
    by_cat_state: dict[tuple[str, str], int] = defaultdict(int)
    for r in rows:
        if not r["domain"] and r["queue_state"] == "excluded":
            by_state["excluded_no_url"] += 1
        else:
            by_state[r["queue_state"]] += 1
        by_cat_state[(r["category"], r["queue_state"])] += 1
    print(f"Wrote {QUEUE_PATH} ({len(rows)} rows)")
    print("By state:")
    for state, n in sorted(by_state.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {state:20} {n}")
    print("Ready for auto-fill:")
    ready = [r for r in rows if r["queue_state"] == "ready"]
    if not ready:
        print("  (none — run a 200-test, then --mark-ready SITE_KEY)")
    for r in ready:
        print(f"  {r['site_key']:16} {r['domain']:30} {r['category']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rankings-dir",
        type=Path,
        default=DEFAULT_RANKINGS,
        help="Directory with the four ranking CSVs (default: data/rankings)",
    )
    parser.add_argument(
        "--mark-ready",
        action="append",
        default=[],
        metavar="SITE_KEY",
        help="Add a site_key to data/queue_ready.txt after a 200-test passes",
    )
    parser.add_argument(
        "--unready",
        action="append",
        default=[],
        metavar="SITE_KEY",
        help="Remove a site_key from the ready list",
    )
    args = parser.parse_args(argv)

    ready = load_ready_keys()
    for key in args.mark_ready:
        ready.add(key.strip())
    for key in args.unready:
        ready.discard(key.strip())
    if args.mark_ready or args.unready:
        save_ready_keys(ready)
        print(f"Updated {READY_PATH}: {sorted(ready)}")

    rows = build_queue(args.rankings_dir)
    write_queue(rows, QUEUE_PATH)
    print_summary(rows)

    summary_path = PROJECT_ROOT / "logs" / "scrape_queue_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        counts[r["queue_state"]] += 1
    summary_path.write_text(
        json.dumps(
            {
                "rows": len(rows),
                "by_state": dict(counts),
                "ready": [r["site_key"] for r in rows if r["queue_state"] == "ready"],
                "running": [r["site_key"] for r in rows if r["queue_state"] == "running"],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
