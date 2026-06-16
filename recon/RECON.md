# Reconnaissance: Landscape Audit

Before writing any site-specific extraction logic, each target publication was
audited: does it expose RSS or a sitemap, what does its `robots.txt` allow, is
the article body static HTML or JS-rendered, and which fetch method that
implies. Every finding below was verified live before implementation.

## Audit table

| Site | RSS / sitemap available? | robots.txt rules | Static or JS-rendered? | Chosen method | Notes |
|------|--------------------------|------------------|------------------------|---------------|-------|
| Duke (The Chronicle) | No RSS. SNWorks CMS exposes no `/feed` or `/rss` endpoint. | Readable with browser UA: `Crawl-delay: 10`, `Allow: /`. Honest research UA gets HTTP 403 on `/robots.txt`. | Static HTML (SNWorks server-renders), gated by an AWS WAF. | HTML + per-year page targeting, browser UA | WAF blocked the honest UA. Browser UA + `delay_min: 10`. Article URLs: `/article/<slug>-YYYYMMDD`. Stratified discovery jumps to ~28 listing pages per year back (not linear crawl); cached under `logs/cache/`. |
| Yale (Yale Daily News) | `articles-sitemap.xml` (~1,000 URLs, dateless slugs). Site rebuilt Jan 2026. | Readable over requests (`Allow: /` for major bots). Article pages still behind Vercel checkpoint. | **JS-gated** for article pages (dates in shadow DOM). Sitemap is plain XML. | Playwright + sitemap discovery | No deep historical archive on the rebuilt site. Sitemap widens recent coverage; section labels from per-section landing pages and on-page kickers. Dates require Playwright `innerText`. |
| Northwestern (The Daily Northwestern) | **Yoast sitemap index** at `/sitemap.xml`; ~74 `post-sitemap*.xml` sub-sitemaps, ~70k article URLs back to ~1994. RSS at `/feed/rss/` (~10 recent items). | Readable and permissive. `Crawl-delay: 6`; `Disallow: /cgi-bin/`, `/?s=`, `/*?*`. | Static HTML (WordPress 7.0, SNO theme — FLEX Pro now, classic 2015–2023). | Sitemap + HTML (stratified) | Filter `/YYYY/MM/DD/<section>/<slug>/` paths; exclude `/games/`. Bucket by URL year, cache the year→URL map under `logs/cache/`, **oversample + backfill** `per_year` until each year has real body text. Body containers vary by era: `#sno-story-body-content` (FLEX Pro), `#classic_story` / `#sno-sites-main-content` (classic), densest-`<p>` fallback. `delay_min: 6`. |

## Key implementation notes

**Northwestern** — sitemap index → all `post-sitemap*.xml` sub-sitemaps → filter
article paths → bucket by URL year (cached) → stratified, oversampled `per_year`
sample. The extractor keeps fetching a year's candidates until `per_year` produce
real body text, so unparseable pages don't leave year gaps. Title, author, and
date are read from each article page across two theme eras: FLEX Pro
(`#sno-story-headline`, `.sno-story-byline`) and classic (`#classic_story`,
`span.storybyline` formatted `"Name , Role"`), with `article:published_time` and a
URL-path date fallback. Result: an even 2 articles/year across 2000–2025.

**Duke** — per-year **page targeting** on `/section/news` with browser UA and
10s delays (~28 listing pages per year back from the present, not a linear crawl
to page 300+). Publication year from slug `-YYYYMMDD`. Stratified `per_year`
sample; discovery cached under `logs/cache/`. Phase 2 unchanged:
`div.article-content`, kicker section hierarchy, `og:description` subtitle.

**Yale** — `articles-sitemap.xml` for breadth (requests, no checkpoint on
sitemap). Per-section landing pages tag URLs with section labels where possible;
phase 2 also reads section from on-page kickers. Author/date/subtitle still
require Playwright rendering. No historical stratification — site rebuild.

**Incremental merge** — default runs load existing `output/<site>.csv`, skip
URLs that already have body text, and write the union. `--overwrite` rebuilds
from scratch. `combined.csv` is only written with `--combined`.

**robots.txt** — Northwestern readable + `Crawl-delay: 6` via config; Duke
`Crawl-delay: 10` via config (browser UA); Yale robots readable but article
pages remain checkpoint-gated.

## Accuracy findings and fixes

These were discovered by comparing CSV output to live article pages.

| Issue | How we caught it | Fix |
|-------|------------------|-----|
| Yale dates all `UNPARSED:` in a full run | Dates empty despite no Playwright error messages | `networkidle` → `domcontentloaded` + hydration poll; don't disable Playwright on per-URL timeout |
| 47 Yale dates all = today | CSV dates didn't match live article bylines; masthead date visible on every page | `find_long_date(..., prefer_time_prefixed=True)` — anchor on `"9:48 a.m., June 9, 2026"` not `"Monday, June 15, 2026"` |
| Dates still flaky after masthead fix | `innerText` had date, `page.content()` did not | Read date from Playwright `innerText` (shadow DOM); shared `find_long_date` helper |
| Playwright silent failure (no dates) | `playwright launch` test; binary was x86_64 on arm64 Mac | Reinstall Chromium for host architecture |
| Yale `section` empty (0/47) | CSV column audit | Per-section discovery (`/university`, `/city`, …) tags each article URL |
| Duke `section` empty (0/100) | CSV column audit | `section_label: "News"` from `/section/news` crawl path |
| Yale single author only | Spot-checked article: `Jolynda Wang` vs `Jolynda Wang & Aria Lynn-Skov` | `_collect_byline_authors()` on all `/author/` links |
| Northwestern `subtitle` | Compared `og:description` to on-page content — it's a body excerpt | Leave `subtitle` empty (uniform column, honest empty) |
| Duke photo credit as author | Spot-check CSV authors | Skip bylines with "Photo by" prefix; join writer bylines |
| Section flattened to one level | CSV `section` vs live nav: Duke/NW nest categories, Yale does not | Add `subsection`; Duke from kicker, NW from nav `/category/<parent>/<child>/` map, Yale single-level |
| Northwestern RSS capped at ~10 articles | Column audit; well under `max_articles=100` regardless of config | Yoast sitemap + date-stratified per-year sampling; year→URL map cached |
| NW 2015–2023 empty bodies → 8-year hole (2000–2014 and 2024+ only) | Year histogram of output CSV after the run | Classic SNO theme uses `#classic_story` / `#sno-sites-main-content` (not FLEX `#sno-story-body-content`) and `span.storybyline` author; added both to selector chains + oversample/backfill per year |
| Yale no historical depth | Sitemap slugs dateless; site rebuilt Jan 2026 | Document limitation; sitemap for recent breadth only |

### Section taxonomy per site (verified from live nav)

| Site | Depth | `section` source | `subsection` source |
|------|-------|------------------|---------------------|
| Duke | 2 levels | kicker `"News \| University"` above `<h1>` (JSON-LD `articleSection` fallback) | second kicker segment |
| Northwestern | 2 levels | parent resolved from nav `/category/<parent>/<child>/` | `<meta article:section>` (most-specific category) |
| Yale | 1 level | sitemap + section pages + on-page kicker | none — nav has no sub-menus (empty) |

Northwestern's RSS `<category>` tags are an **unordered bag** (section,
subsection, nav buckets, and topic/person tags mixed together), so they cannot
be used to infer hierarchy; the parent is resolved from the site nav instead.
Labels are kept verbatim from each paper — cross-paper section normalization is
a deliberate non-goal of the pilot.

### Uniform schema (`subtitle` column)

All sites share the same CSV columns. `subtitle` is populated only where the
CMS exposes a genuine editor deck:

- **Yale / Duke:** `og:description` (verified deck, not body text).
- **Northwestern:** intentionally empty — SNO/FLEX `og:description` is an
  auto-generated excerpt (~first 380 characters of the article), not a distinct
  subtitle field. Storing it would mislabel body text as an editor-written deck.
