#!/usr/bin/env python3
"""Check whether this machine's IP can reach each publisher before a full run.

This is the gate to run FIRST on any new server. A datacenter IP may be blocked
where a home IP was not (Duke's WAF and Yale's bot checkpoint are the known
risks). If a site is blocked here, the fallback is to run that site from an
approved machine -- never proxies or IP rotation.

The probe is deliberately tiny: robots.txt, the homepage, and two already-known
article URLs per site, with the site's configured crawl delay between requests.

Usage:
    python scripts/access_probe.py
    python scripts/access_probe.py --site duke --site yale
    python scripts/access_probe.py --json logs/access_probe.json

Exit code is 1 when any site looks blocked, so it can gate a deploy script.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "sites.yaml"
PROBE_URLS_PATH = PROJECT_ROOT / "deploy" / "probe_urls.json"

# Kept in sync with src/extractor.py BROWSER_UA. Duplicated here so the probe
# runs on a bare server without importing the full extractor dependency chain.
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Only these sites are approved for the browser UA (see AGENTS.md).
BROWSER_UA_SITES = {"duke", "yale"}

TIMEOUT = 30.0


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def site_settings(config: dict, site: str) -> dict:
    defaults = dict(config.get("defaults") or {})
    site_cfg = dict((config.get("sites") or {}).get(site) or {})
    rate = dict((defaults.get("rate_limit") or {}))
    rate.update(site_cfg.get("rate_limit") or {})
    merged = {**defaults, **site_cfg}
    merged["rate_limit"] = rate
    return merged


def user_agent_for(site: str, config: dict) -> str:
    if site in BROWSER_UA_SITES:
        return BROWSER_UA
    return (config.get("defaults") or {}).get("user_agent") or (
        "CollegeNewspaperResearchBot/1.0 (academic research)"
    )


def probe_one(url: str, ua: str) -> dict:
    """Single GET. Never raises; the status is the result."""
    started = time.monotonic()
    try:
        resp = requests.get(url, headers={"User-Agent": ua}, timeout=TIMEOUT)
        return {
            "url": url,
            "status": resp.status_code,
            "bytes": len(resp.content),
            "seconds": round(time.monotonic() - started, 2),
            "error": "",
        }
    except requests.RequestException as exc:
        return {
            "url": url,
            "status": None,
            "bytes": 0,
            "seconds": round(time.monotonic() - started, 2),
            "error": type(exc).__name__,
        }


def robots_allows(robots_text: str, ua: str, url: str) -> bool:
    parser = RobotFileParser()
    parser.parse(robots_text.splitlines())
    return parser.can_fetch(ua, url)


def verdict_for(results: list[dict]) -> str:
    """OK when article pages return 200; BLOCKED on auth/bot status codes."""
    article_results = [r for r in results if r["kind"] == "article"]
    if not article_results:
        article_results = results
    statuses = [r["status"] for r in article_results]
    if all(s == 200 for s in statuses):
        return "OK"
    if any(s in (401, 403, 407, 429) for s in statuses if s is not None):
        return "BLOCKED"
    if any(s is None for s in statuses):
        return "NETWORK_ERROR"
    return "DEGRADED"


def probe_site(site: str, config: dict, probe_urls: list[str]) -> dict:
    settings = site_settings(config, site)
    ua = user_agent_for(site, config)
    delay = float((settings.get("rate_limit") or {}).get("delay_min", 3.0))
    base_url = settings.get("base_url") or ""
    if not base_url and probe_urls:
        parsed = urlparse(probe_urls[0])
        base_url = f"{parsed.scheme}://{parsed.netloc}"

    results: list[dict] = []

    robots_url = base_url.rstrip("/") + "/robots.txt"
    robots = probe_one(robots_url, ua)
    robots["kind"] = "robots"
    results.append(robots)
    robots_text = ""
    if robots["status"] == 200:
        try:
            robots_text = requests.get(
                robots_url, headers={"User-Agent": ua}, timeout=TIMEOUT
            ).text
        except requests.RequestException:
            robots_text = ""

    time.sleep(delay)
    home = probe_one(base_url, ua)
    home["kind"] = "homepage"
    results.append(home)

    for url in probe_urls:
        if robots_text and not robots_allows(robots_text, ua, url):
            results.append(
                {
                    "url": url,
                    "kind": "article",
                    "status": None,
                    "bytes": 0,
                    "seconds": 0.0,
                    "error": "robots_disallow",
                }
            )
            continue
        time.sleep(delay)
        result = probe_one(url, ua)
        result["kind"] = "article"
        results.append(result)

    return {
        "site": site,
        "user_agent": "browser" if site in BROWSER_UA_SITES else "honest_research",
        "delay_used": delay,
        "verdict": verdict_for(results),
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--site",
        action="append",
        dest="sites",
        help="Site key to probe (repeatable). Default: every site in probe_urls.json.",
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=PROJECT_ROOT / "logs" / "access_probe.json",
        help="Where to write the machine-readable report.",
    )
    args = parser.parse_args(argv)

    config = load_config()
    probe_data = json.loads(PROBE_URLS_PATH.read_text(encoding="utf-8"))
    all_sites = probe_data.get("sites") or {}
    sites = args.sites or list(all_sites)

    report = {
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "sites": [],
    }
    for site in sites:
        print(f"\n=== {site} ===")
        outcome = probe_site(site, config, all_sites.get(site, []))
        report["sites"].append(outcome)
        for item in outcome["results"]:
            status = item["status"] if item["status"] is not None else item["error"]
            print(f"  {item['kind']:<9} {str(status):<16} {item['bytes']:>8}B  {item['url']}")
        print(f"  verdict: {outcome['verdict']}  (UA: {outcome['user_agent']})")

    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nReport written to {args.json}")

    blocked = [s["site"] for s in report["sites"] if s["verdict"] != "OK"]
    if blocked:
        print(
            "\nNOT CLEARED: " + ", ".join(blocked) +
            "\nRun these sites from an approved machine instead. "
            "Do not use proxies or IP rotation."
        )
        return 1
    print("\nAll probed sites reachable from this IP.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
