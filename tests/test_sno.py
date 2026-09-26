"""Offline tests for the WordPress extractor (src/wordpress.py) and its SNO profile."""

from __future__ import annotations

import pytest

from src import sno, wordpress
from src.extractor import make_soup, normalize_date


def _page(body: str) -> str:
    return f"<html><head></head><body>{body}</body></html>"


PARAS = "<p>One.</p><p>Two.</p><p>Three.</p>"


@pytest.mark.parametrize(
    ("markup", "expected"),
    [
        # Masthead .time-wrapper carries today's date and must never win (Biola).
        (
            '<div class="time-wrapper">September 24, 2026</div>'
            '<span class="sno-story-byline">Olivia Kam , News Editor • March 28, 2025</span>',
            "2025-03-28",
        ),
        ('<div class="sno-story-date">Published Mar 26, 2026</div>', "2026-03-26"),
        ('<span class="storydate">April 21, 2022</span>', "2022-04-21"),
        (
            '<meta property="article:published_time" content="2025-03-29T01:32:35+00:00">',
            "2025-03-29",
        ),
        (
            '<script type="application/ld+json">{"datePublished":"2026-04-21T20:47:36Z"}</script>',
            "2026-04-21",
        ),
    ],
)
def test_page_date_priority(markup, expected):
    url = "https://example.com/123/news/story/"
    raw = sno._page_date(make_soup(_page(markup)), url)
    assert normalize_date(raw, url) == expected


def test_page_date_falls_back_to_permalink():
    url = "https://tcu360.com/2019/03/04/story/"
    assert sno._page_date(make_soup(_page("")), url) == "2019-03-04"


def test_page_date_sno_story_date_beats_meta():
    """Visible local date wins over the UTC meta timestamp (late-night posts)."""
    html = _page(
        '<meta property="article:published_time" content="2025-03-29T01:32:35+00:00">'
        '<div class="sno-story-date">March 28, 2025</div>'
    )
    raw = sno._page_date(make_soup(html), "https://example.com/1/news/x/")
    assert normalize_date(raw, "u") == "2025-03-28"


class _HtmlFetcher:
    def __init__(self, pages: dict[str, str]):
        self.pages = pages

    def get(self, url):
        class _R:
            text = self.pages[url]

        return _R()


def test_extract_text_sno_fields():
    url = "https://example.com/1/news/x/"
    html = _page(
        '<meta property="og:title" content="Headline">'
        '<meta property="article:section" content="News">'
        '<span class="sno-story-byline">By <a href="/staff_name/jane-doe/">Jane Doe</a>'
        " , Staff Writer • May 2, 2024</span>"
        f'<div id="sno-story-body-content">{PARAS}</div>'
    )
    page = wordpress.fetch_page(url, _HtmlFetcher({url: html}), "Test", "sno")
    assert page["title"] == "Headline"
    assert page["author"] == "Jane Doe"
    assert page["section"] == "News"
    assert page["text"] == "One. Two. Three."
    assert normalize_date(page["publication_date"], url) == "2024-05-02"


def test_date_only_byline_is_not_an_author():
    """Legacy Biola pages put only the date in the byline slot."""
    soup = make_soup(_page('<span class="sno-story-byline">September 19, 2007</span>'))
    assert sno._page_author(soup) == ""
    soup = make_soup(_page('<span class="sno-story-byline">November 1, 2007</span>'
                           '<meta name="author" content="Pat Lee">'))
    assert sno._page_author(soup) == "Pat Lee"


def test_title_drops_site_name_suffix():
    soup = make_soup(_page('<meta property="og:title" content="Band plays - The Chimes">'))
    assert sno._page_title(soup, "The Chimes") == "Band plays"
    soup = make_soup(_page('<meta property="og:title" content="Q&A - A Chimes Story">'))
    assert sno._page_title(soup, "The Chimes") == "Q&A - A Chimes Story"


def test_extract_text_sno_jsonld_section_and_empty_body():
    url = "https://tcu360.com/2026/03/26/x/"
    html = _page(
        '<script type="application/ld+json">{"articleSection":["Community","Features"]}</script>'
        f'<div id="sno-story-body-content">{PARAS}</div>'
    )
    assert wordpress.fetch_page(url, _HtmlFetcher({url: html}), "T", "sno")["section"] == "Community"
    fullscreen = _page('<div class="storyfullscreen"><p></p></div>')
    empty = wordpress.fetch_page(url, _HtmlFetcher({url: fullscreen}), "T", "sno")
    assert empty["text"] == "" and empty["error"] == "empty_body"


def test_discover_full_orders_by_bucket_and_filters(monkeypatch):
    pairs = [
        ["https://tcu360.com/2020/01/01/b/", ""],
        ["https://example.com/5/news/a/", "2018-06-01T00:00:00+00:00"],
        ["https://tcu360.com/2019/05/01/c/", ""],
    ]
    monkeypatch.setattr(wordpress, "_load_or_fetch_urls", lambda cfg, f: pairs)
    metas = wordpress._discover({"mode": "full", "site_key": "t"}, None)
    assert [m["year"] for m in metas] == [2018, 2019, 2020]
    only = wordpress._discover({"mode": "full", "site_key": "t", "candidate_lastmod_year": 2019}, None)
    assert [m["url"] for m in only] == ["https://tcu360.com/2019/05/01/c/"]


def test_extract_sno_full_mode(monkeypatch):
    metas = [{"url": f"https://example.com/{i}/news/x/", "year": 2020} for i in range(6)]
    metas.insert(0, {"url": "https://example.com/old/", "year": 1999})
    pages = {m["url"]: {"text": "body", "publication_date": "May 2, 2020"} for m in metas}
    pages["https://example.com/old/"] = {"text": "body", "publication_date": "May 2, 1998"}
    pages["https://example.com/3/news/x/"] = {"text": "", "error": "http_403"}
    monkeypatch.setattr(wordpress, "_discover", lambda cfg, f: metas)
    monkeypatch.setattr(wordpress, "fetch_page", lambda url, f, label, *a: dict(pages[url]))
    failures = []
    config = {
        "mode": "full",
        "institution": "Test Paper",
        "max_articles": 2,  # sample cap; must not apply in full mode
        "skip_urls": {"https://example.com/0/news/x/"},
        "failure_sink": lambda u, y, r: failures.append((u, r)),
    }
    rows = list(wordpress.extract_wordpress(config, None))
    assert [r.url.split("/")[3] for r in rows] == ["1", "2", "4", "5"]
    assert all(r.institution == "Test Paper" and r.publication_date == "2020-05-02" for r in rows)
    assert ("https://example.com/old/", "pre_2000") in failures
    assert ("https://example.com/3/news/x/", "http_403") in failures

    capped = list(wordpress.extract_wordpress(dict(config, max_fetch=2, failure_sink=None), None))
    assert len(capped) == 1  # two fetches: the pre-2000 skip and one row


def test_pipeline_resolves_sno_sites():
    from src import pipeline

    config = {"sites": {"tcu": {"platform": "sno"}, "duke": {}}}
    keys = pipeline.site_keys_from_config(config)
    assert "tcu" in keys and "northwestern" in keys
    assert pipeline._resolve_extractor("tcu", {"platform": "sno"}) is wordpress.extract_wordpress
    with pytest.raises(KeyError):
        pipeline._resolve_extractor("nope", {})


def test_repo_config_sno_sites_are_complete():
    from src import pipeline

    sites = pipeline.load_config()["sites"]
    keys = wordpress.wordpress_site_keys(sites)
    assert {"smu", "tcu", "biola", "union", "stolaf"} <= set(keys)
    for key in keys:
        cfg = sites[key]
        assert cfg.get("base_url") and cfg.get("institution"), key
        assert cfg["rate_limit"]["delay_min"] >= 6, key  # published Crawl-delay


# ----------------------------------------------------------------------
# Generic WordPress profile and per-site overrides
# ----------------------------------------------------------------------

WP_GENERIC = _page(
    '<meta property="og:title" content="Council votes - The Point Weekly">'
    '<script type="application/ld+json">{"@graph":[{"@type":"NewsArticle",'
    '"author":{"@type":"Person","name":"Sam Rivera"},'
    '"datePublished":"2016-09-29T08:00:00-07:00"}]}</script>'
    '<article><header><span class="cat-links"><a href="/category/news/" rel="category tag">News</a></span>'
    '<time class="entry-date published" datetime="2016-09-29T08:00:00-07:00">Sept 29</time></header>'
    '<div class="entry-content"><p>One.</p><p>Two.</p></div>'
    '<aside><p>a</p><p>b</p><p>c</p><p>d</p></aside></article>'
)


def test_wp_generic_profile_fields():
    url = "https://lomabeat.com/9468/"
    page = wordpress.fetch_page(url, _HtmlFetcher({url: WP_GENERIC}), "The Point Weekly", "wp_generic")
    assert page["text"] == "One. Two."  # .entry-content beats the denser aside
    assert page["title"] == "Council votes"
    assert page["author"] == "Sam Rivera"  # JSON-LD fallback
    assert page["section"] == "News"
    assert page["subtitle"] == ""  # never og:description
    assert normalize_date(page["publication_date"], url) == "2016-09-29"


def test_wp_generic_prefers_byline_links_and_rejects_dates():
    soup = make_soup(_page('<span class="byline">By <a rel="author" href="/a/">Ana Diaz</a></span>'))
    assert wordpress._generic_author(soup) == "Ana Diaz"
    soup = make_soup(_page('<span class="byline">March 3, 2011</span>'))
    assert wordpress._generic_author(soup) == ""


def test_selector_overrides_win_when_they_match():
    url = "https://example.com/x/"
    html = WP_GENERIC.replace(
        "</article>", '<p class="kicker">Opinion</p><span class="writer">Lee Park</span></article>'
    )
    selectors = {"section": ".kicker", "author": ".writer", "subtitle": ".missing"}
    page = wordpress.fetch_page(url, _HtmlFetcher({url: html}), "T", "wp_generic", selectors)
    assert (page["section"], page["author"], page["subtitle"]) == ("Opinion", "Lee Park", "")


def test_page_profile_defaults_and_validation():
    assert wordpress.page_profile({"platform": "sno"}) == "sno"
    assert wordpress.page_profile({"platform": "wordpress"}) == "wp_generic"
    assert wordpress.page_profile({"platform": "wordpress", "page_profile": "sno"}) == "sno"
    with pytest.raises(ValueError):
        wordpress.page_profile({"platform": "wordpress", "page_profile": "newspack"})
    from src import pipeline

    assert pipeline._resolve_extractor("x", {"platform": "wordpress"}) is wordpress.extract_wordpress
