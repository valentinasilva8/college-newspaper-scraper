"""Offline tests for the tracker merge (build_targets.py) and recon helpers."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


recon = _load("recon_site")


def _targets():
    # build_targets needs the local-only workbook tooling (requirements-dev.txt).
    pytest.importorskip("pandas")
    pytest.importorskip("openpyxl")
    return _load("build_targets")


def _row(**kw) -> dict:
    row = {
        "site_key": "",
        "domain": "example.com",
        "platform": "unknown",
        "status": "pending_recon",
        "tabs": "Christian Colleges",
        "notes": "",
    }
    row.update(kw)
    return row


# ----------------------------------------------------------------------
# build_targets merge
# ----------------------------------------------------------------------

def test_enrich_recon_facts_and_cloudflare_estimate():
    bt = _targets()
    facts = {"cdn": "cloudflare", "crawl_delay": 6.0, "sitemap_kind": "wp_core",
             "est_urls": 7200, "platform": "sno", "access_profile": "cloudflare"}
    row = bt.enrich(_row(), None, facts, None)
    assert row["platform"] == "sno"
    assert row["access_profile"] == "cloudflare"
    assert row["status"] == "recon_done"
    assert row["est_days"] == "1.0"  # 7200 URLs x 12 s


def test_enrich_override_wins_and_excludes():
    bt = _targets()
    row = bt.enrich(
        _row(notes="label:x"),
        None,
        {"access_profile": "open"},
        {"exclusion_reason": "wrong_school", "notes": "not this school"},
    )
    assert row["access_profile"] == "excluded"
    assert row["status"] == "excluded"
    assert row["notes"] == "label:x; not this school"


def test_enrich_configured_site_uses_config_delay_and_key():
    bt = _targets()
    configured = {"site_key": "biola", "platform": "sno", "delay": 12.0}
    row = bt.enrich(
        _row(), configured, {"est_urls": 14400}, {"category_labels": "Liberal Arts", "wave": "1"}
    )
    assert (row["site_key"], row["status"], row["wave"]) == ("biola", "configured", "1")
    assert row["category_labels"] == "Liberal Arts"
    assert row["est_days"] == "2.0"


def test_planned_delay_floor_for_open_sites():
    bt = _targets()
    assert bt.planned_delay(None, {"cdn": "none", "crawl_delay": 2}) == bt.MIN_PLANNED_DELAY
    assert bt.planned_delay(None, {"cdn": "none", "crawl_delay": 10}) == 10.0
    assert bt.planned_delay(None, None) is None


def test_duplicate_names_flags_repeated_university():
    bt = _targets()
    tabs = {
        "Christian Colleges": [
            {"university_name": "Point Loma", "source_row": 12, "domain": "anchor.hope.edu"},
            {"university_name": "Point Loma", "source_row": 15, "domain": "lomabeat.com"},
            {"university_name": "Biola", "source_row": 10, "domain": "chimesnewspaper.com"},
        ]
    }
    lines = bt.duplicate_names(tabs)
    assert len(lines) == 1 and "anchor.hope.edu" in lines[0] and "lomabeat.com" in lines[0]


def test_overrides_reject_unknown_profile(tmp_path):
    bt = _targets()
    path = tmp_path / "o.csv"
    path.write_text("key,access_profile\nexample.com,proxy\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown access_profile"):
        bt.load_overrides(path)


# ----------------------------------------------------------------------
# recon helpers
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Server": "cloudflare", "CF-RAY": "abc"}, "cloudflare"),
        ({"server": "awselb/2.0"}, "aws_elb"),
        ({"x-vercel-id": "iad1"}, "vercel"),
        ({"server": "LiteSpeed"}, "none"),
    ],
)
def test_detect_cdn(headers, expected):
    assert recon.detect_cdn(headers) == expected


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ('<div class="sno-story-body"></div><link href="/wp-content/x.css">', "sno"),
        ('<link href="/wp-content/themes/newspack/style.css">', "wordpress"),
        ("<footer>Powered by SNworks</footer>", "snworks"),
        ('<img src="https://bloximages.chicago2.vip.townnews.com/x.jpg">', "blox"),
        ("<html></html>", "unknown"),
    ],
)
def test_detect_platform(html, expected):
    assert recon.detect_platform(html) == expected


def test_classify_wp_core_sorts_numerically():
    xml = (
        "<sitemapindex>"
        + "".join(
            f"<sitemap><loc>https://x.com/wp-sitemap-posts-post-{n}.xml</loc></sitemap>"
            for n in (10, 2, 1)
        )
        + "<sitemap><loc>https://x.com/wp-sitemap-posts-page-1.xml</loc></sitemap>"
        + "</sitemapindex>"
    )
    kind, subs = recon.classify_sitemap(xml)
    assert kind == "wp_core"
    assert [recon._sub_number(u) for u in subs] == [1, 2, 10]


def test_classify_yoast_and_single():
    xml = (
        "<sitemapindex><sitemap><loc>https://x.com/post-sitemap.xml</loc></sitemap>"
        "<sitemap><loc>https://x.com/post-sitemap2.xml</loc></sitemap>"
        "<sitemap><loc>https://x.com/page-sitemap.xml</loc></sitemap></sitemapindex>"
    )
    kind, subs = recon.classify_sitemap(xml)
    assert kind == "yoast" and subs[0].endswith("post-sitemap.xml") and len(subs) == 2
    assert recon.classify_sitemap("<urlset><url><loc>https://x.com/a</loc></url></urlset>")[0] == "single"


def test_estimate_urls_and_samples():
    assert recon.estimate_urls(4, 2000, 150) == 6150
    assert recon.estimate_urls(1, 320, 0) == 320
    assert recon.pick_samples(["a", "b", "c"], ["y", "z"]) == ["a", "b", "z"]
    assert recon.pick_samples(["a"], []) == ["a"]


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ({"robots_allows_home": False}, "excluded"),
        ({"home_status": None}, ""),
        ({"home_status": 403, "cdn": "aws_elb"}, "waf_browser_ua"),
        ({"home_status": 200, "cdn": "cloudflare", "articles": []}, "cloudflare"),
        ({"home_status": 200, "cdn": "none", "articles": [{"status": 403}]}, "waf_browser_ua"),
        ({"home_status": 200, "cdn": "none", "articles": [{"status": 200}]}, "open"),
    ],
)
def test_suggest_access_profile(record, expected):
    assert recon.suggest_access_profile(record) == expected
