"""Offline tests for scripts/access_probe.py UA selection and fallback."""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load():
    spec = importlib.util.spec_from_file_location(
        "access_probe", SCRIPTS / "access_probe.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cfg(site: str, base: str) -> dict:
    return {
        "defaults": {
            "user_agent": "CollegeNewspaperResearchBot/1.0 (academic research)"
        },
        "sites": {site: {"base_url": base, "rate_limit": {"delay_min": 0}}},
    }


def _ok(url: str) -> dict:
    return {"url": url, "status": 200, "bytes": 80, "seconds": 0.0, "error": ""}


def _blocked(url: str) -> dict:
    return {"url": url, "status": 403, "bytes": 0, "seconds": 0.0, "error": ""}


def test_probe_honest_first_when_ok(monkeypatch):
    probe = _load()
    monkeypatch.setattr(probe.time, "sleep", lambda *_a, **_k: None)
    uas: list[str] = []

    def fake_probe(url, ua):
        uas.append(ua)
        return _ok(url)

    monkeypatch.setattr(probe, "probe_one", fake_probe)
    outcome = probe.probe_site(
        "ucsd",
        _cfg("ucsd", "https://ucsdguardian.org"),
        ["https://ucsdguardian.org/story/"],
    )
    assert outcome["verdict"] == "OK"
    assert outcome["user_agent"] == "honest_research"
    assert outcome["used_browser_fallback"] is False
    assert probe.BROWSER_UA not in uas


def test_probe_retries_with_browser_ua_after_honest_block(monkeypatch):
    probe = _load()
    monkeypatch.setattr(probe.time, "sleep", lambda *_a, **_k: None)

    def fake_probe(url, ua):
        if "/robots.txt" in url:
            return {"url": url, "status": 404, "bytes": 0, "seconds": 0.0, "error": ""}
        if ua == probe.BROWSER_UA:
            return _ok(url)
        return _blocked(url)

    monkeypatch.setattr(probe, "probe_one", fake_probe)
    outcome = probe.probe_site(
        "ucsd",
        _cfg("ucsd", "https://ucsdguardian.org"),
        ["https://ucsdguardian.org/story/"],
    )
    assert outcome["verdict"] == "OK"
    assert outcome["user_agent"] == "browser"
    assert outcome["used_browser_fallback"] is True
    assert outcome["honest_verdict"] == "BLOCKED"


def test_duke_starts_with_browser_ua(monkeypatch):
    probe = _load()
    monkeypatch.setattr(probe.time, "sleep", lambda *_a, **_k: None)
    uas: list[str] = []

    def fake_probe(url, ua):
        uas.append(ua)
        return _ok(url)

    monkeypatch.setattr(probe, "probe_one", fake_probe)
    outcome = probe.probe_site(
        "duke",
        _cfg("duke", "https://www.dukechronicle.com"),
        ["https://www.dukechronicle.com/article/x"],
    )
    assert outcome["user_agent"] == "browser"
    assert outcome["used_browser_fallback"] is False
    assert all(ua == probe.BROWSER_UA for ua in uas)
