#!/usr/bin/env python3
"""Entry point for the college newspaper scraping pilot.

Usage:
    python run.py --site duke
    python run.py --site all
"""

from __future__ import annotations

import argparse
import sys

from src import pipeline
from src.extractor import SITE_EXTRACTORS


def build_parser() -> argparse.ArgumentParser:
    choices = sorted(SITE_EXTRACTORS.keys()) + ["all"]
    parser = argparse.ArgumentParser(
        description="Scrape a college newspaper corpus (pilot).",
    )
    parser.add_argument(
        "--site",
        required=True,
        choices=choices,
        help="Site key to scrape, or 'all' for every configured site.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Ignore existing per-site CSV rows and rebuild from scratch.",
    )
    parser.add_argument(
        "--combined",
        action="store_true",
        help="Also write output/combined.csv (off by default).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    pipeline.setup_logging()
    pipeline.run(
        args.site,
        incremental=not args.overwrite,
        write_combined=args.combined,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
