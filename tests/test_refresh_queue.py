"""Offline checks for the scrape queue builder."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
RANKINGS = Path(__file__).resolve().parent.parent / "data" / "rankings"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def rq():
    pytest.importorskip("yaml")
    return _load("refresh_queue")


def test_domain_of_strips_www(rq):
    assert rq.domain_of("https://www.example.com/path") == "example.com"
    assert rq.domain_of("") == ""


def test_pick_primary_prefers_public(rq):
    cats = {"National Universities", "Public Universities"}
    assert rq.pick_primary_category(cats) == "Public Universities"


def test_classify_yale_mac_only(rq):
    state, key, reason = rq.classify(
        "yaledailynews.com",
        tracker={"yaledailynews.com": {"site_key": "yale", "access_profile": "datacenter_blocked"}},
        sites={"yaledailynews.com": {"site_key": "yale"}},
        status={},
        ready_keys=set(),
        active=set(),
        sheet_note="",
    )
    assert state == "mac_only"
    assert key == "yale"
    assert "Mac" in reason or "library" in reason


def test_classify_duke_later_pass(rq):
    state, key, _ = rq.classify(
        "dukechronicle.com",
        tracker={"dukechronicle.com": {"site_key": "duke"}},
        sites={"dukechronicle.com": {"site_key": "duke"}},
        status={"dukechronicle.com": {"status": "done", "site_key": "duke"}},
        ready_keys=set(),
        active=set(),
        sheet_note="",
    )
    assert state == "later_pass"
    assert key == "duke"


def test_classify_pitt_blocked(rq):
    state, key, reason = rq.classify(
        "pittnews.com",
        tracker={"pittnews.com": {"site_key": "pitt", "platform": "sno"}},
        sites={"pittnews.com": {"site_key": "pitt"}},
        status={"pittnews.com": {"status": "queued", "site_key": "pitt"}},
        ready_keys=set(),
        active=set(),
        sheet_note="",
    )
    assert state == "blocked"
    assert "403" in reason


def test_classify_ready_requires_mark(rq):
    state, key, _ = rq.classify(
        "example.com",
        tracker={"example.com": {"site_key": "example", "platform": "sno", "sitemap_kind": "yoast"}},
        sites={"example.com": {"site_key": "example"}},
        status={},
        ready_keys={"example"},
        active=set(),
        sheet_note="",
    )
    assert state == "ready"
    assert key == "example"


def test_wabash_and_wheaton_wire_are_candidates(rq):
    for domain in ("bachelor.wabash.edu", "wheatonwire.com"):
        state, _, reason = rq.classify(
            domain,
            tracker={},
            sites={},
            status={},
            ready_keys=set(),
            active=set(),
            sheet_note="",
        )
        assert state == "candidate", (domain, state, reason)


def test_build_queue_includes_ranking_domains(rq):
    if not RANKINGS.is_dir():
        pytest.skip("data/rankings not present")
    rows = rq.build_queue(RANKINGS)
    assert rows
    assert set(rq.QUEUE_FIELDS) <= set(rows[0])
    domains = {r["domain"] for r in rows if r["domain"]}
    assert "yaledailynews.com" in domains
    assert "bachelor.wabash.edu" in domains
    assert "wheatonwire.com" in domains
