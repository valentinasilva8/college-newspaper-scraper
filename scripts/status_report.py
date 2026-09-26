#!/usr/bin/env python3
"""Report scrape progress for every site: rows, checkpoint age, failures.

Standard library only, so it runs on a bare server or your laptop.

Usage:
    python scripts/status_report.py
    python scripts/status_report.py --json logs/status.json
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output"
CHECKPOINT_DIR = PROJECT_ROOT / "logs" / "checkpoints"
CACHE_DIR = PROJECT_ROOT / "logs" / "cache"
LOG_DIR = PROJECT_ROOT / "logs"


def count_rows(path: Path) -> int:
    if not path.is_file():
        return 0
    with path.open(newline="", encoding="utf-8") as fh:
        return max(sum(1 for _ in csv.reader(fh)) - 1, 0)


def _entry_url(entry: object) -> str | None:
    """Pull the URL out of the three cache entry shapes we write."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, (list, tuple)) and entry:
        return str(entry[0])
    if isinstance(entry, dict):
        url = entry.get("url")
        return str(url) if url else None
    return None


def discovered_total(site: str) -> int | None:
    """Unique candidate URLs from a discovery cache, if one exists.

    This is URLs *discovered so far*, not the true archive size: Duke's cache in
    particular is a partial section sample, so percentages against it read high.
    """
    for cache in sorted(CACHE_DIR.glob(f"{site}_*.json")):
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        by_year = data.get("by_year")
        entries = (
            [entry for bucket in by_year.values() for entry in bucket]
            if isinstance(by_year, dict)
            else data.get("urls")  # flat list written by src/wordpress.py
        )
        if isinstance(entries, list):
            urls = {url for entry in entries if (url := _entry_url(entry))}
            if urls:
                return len(urls)
    return None


def systemd_state(site: str) -> str:
    if not shutil.which("systemctl"):
        return "n/a"
    try:
        result = subprocess.run(
            ["systemctl", "is-active", f"newspaper-scraper@{site}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip() or "unknown"
    except (subprocess.SubprocessError, OSError):
        return "unknown"


def failure_counts(site: str) -> dict[str, int]:
    path = LOG_DIR / f"failed_{site}.csv"
    if not path.is_file():
        return {}
    counts: dict[str, int] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            reason = row.get("reason") or "unknown"
            counts[reason] = counts.get(reason, 0) + 1
    return counts


def build_report() -> dict:
    now = datetime.now(timezone.utc)
    sites: list[dict] = []

    site_keys = sorted(
        {p.stem for p in OUTPUT_DIR.glob("*.csv") if p.stem != "combined"}
        | {p.stem for p in CHECKPOINT_DIR.glob("*.json")}
    )

    for site in site_keys:
        csv_path = OUTPUT_DIR / f"{site}.csv"
        rows = count_rows(csv_path)
        checkpoint_path = CHECKPOINT_DIR / f"{site}.json"
        checkpoint: dict = {}
        age_minutes = None
        if checkpoint_path.is_file():
            try:
                checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                updated = checkpoint.get("updated_at")
                if updated:
                    then = datetime.fromisoformat(updated)
                    age_minutes = round((now - then).total_seconds() / 60, 1)
            except (OSError, ValueError):
                checkpoint = {}

        total = discovered_total(site)
        sites.append(
            {
                "site": site,
                "rows": rows,
                "discovered_total": total,
                "percent_complete": (
                    round(100 * rows / total, 1) if total else None
                ),
                "checkpoint_rows": checkpoint.get("rows_committed"),
                "checkpoint_age_minutes": age_minutes,
                "service": systemd_state(site),
                "failures": failure_counts(site),
                "csv_mb": (
                    round(csv_path.stat().st_size / 1_048_576, 1)
                    if csv_path.is_file()
                    else 0.0
                ),
            }
        )

    usage = shutil.disk_usage(PROJECT_ROOT)
    return {
        "generated_at": now.isoformat(),
        "disk_free_gb": round(usage.free / 1_073_741_824, 1),
        "disk_used_percent": round(100 * usage.used / usage.total, 1),
        "sites": sites,
    }


def print_report(report: dict) -> None:
    print(f"Status at {report['generated_at']}")
    print(
        f"Disk: {report['disk_free_gb']} GB free "
        f"({report['disk_used_percent']}% used)\n"
    )
    header = f"{'site':<14}{'rows':>9}{'found':>9}{'%':>7}{'ckpt age':>11}  service"
    print(header)
    print("-" * len(header))
    for site in report["sites"]:
        total = site["discovered_total"]
        pct = site["percent_complete"]
        age = site["checkpoint_age_minutes"]
        print(
            f"{site['site']:<14}"
            f"{site['rows']:>9}"
            f"{(total if total is not None else '-'):>9}"
            f"{(f'{pct}%' if pct is not None else '-'):>7}"
            f"{(f'{age}m' if age is not None else '-'):>11}"
            f"  {site['service']}"
        )
        if site["failures"]:
            detail = ", ".join(f"{k}={v}" for k, v in sorted(site["failures"].items()))
            print(f"{'':<14}failures: {detail}")
    print("\n'found' is URLs discovered so far, not the full archive size.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        help="Also write the report as JSON to this path.",
    )
    args = parser.parse_args(argv)

    report = build_report()
    print_report(report)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nJSON written to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
