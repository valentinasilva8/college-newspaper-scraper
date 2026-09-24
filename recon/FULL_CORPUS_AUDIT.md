# Full Corpus Feasibility Audit

Audit date: 2026-09-23  
Target range: 2000-01-01 through 2026-09-23

This is a read-only feasibility audit. It used existing discovery caches,
sitemaps, listing pages, `robots.txt`, HEAD requests for three legacy Yale URLs,
and existing run logs. It did **not** fetch article bodies or change any scraper
code or corpus CSV.

Runtime estimates are lower bounds: they apply the configured average delay to
each article request and exclude retries, transient blocks, parsing failures,
and time spent retrying failed URLs. Estimates marked **guess** depend on
incomplete discovery.

## Summary

| Site | Discovery source | Complete? | URLs 2000+ | Earliest year | Method | Delay | Est. hours | Blockers |
|---|---|---:|---:|---:|---|---:|---:|---|
| Northwestern | Yoast sitemap index → `post-sitemap*.xml`; existing `northwestern_sitemap_by_year.json` | Near-complete for indexed, standard dated article URLs; current cache excludes `/games/` and nonmatching paths | 70,369 cached | 1994 in cache; 2000 for target range | Honest research UA, `requests`, robots checked | 6–8 s (7 s avg) | **136.8 h** article fetches; ~137 h total | Cache is stale since 2026-06-16; multiple historical templates; current discovery filter intentionally drops `/games/`; old pages may lack bylines or use legacy containers |
| Chicago Maroon | WordPress core sitemap → `wp-sitemap-posts-post-*.xml`; existing `chicago_articles_by_year.json` | Likely complete for indexed posts matching `/<id>/<section>/<slug>`; year classification is by **lastmod**, not publication date | 22,439 cached | 2001 by sitemap lastmod | Honest research UA, `requests`, robots checked | 6–8 s (7 s avg) | **43.6 h** article fetches; ~43.7 h total | Cache is stale since 2026-06-17; `<lastmod>` can differ from publication year; full mode must retain the true on-page date; subtitle handling is currently under separate review |
| Duke Chronicle | Paginated `/section/<name>` listings; no sitemap | **Partial/unproven.** Current cache covers only seven-page windows in `/section/news`; all-section crawl has not been deduplicated | 1,342 cached partial; **40k–65k unique guess** across sections | Sports reaches 1999; News reaches 2002 | Browser UA + `requests` because WAF blocks honest UA; robots checked with browser UA | 10–12 s (11 s avg) | **131–211 h guess**, including listing discovery | No sitemap; ~3k–4k top-level listing pages **guess**; overlapping parent/child sections; 10 s crawl delay; WAF; current cache is not a full inventory |
| Yale Daily News | Current `articles-sitemap.xml` plus external Yale Library archive / Wayback for history | **Partial.** Current sitemap is capped at exactly 1,000 URLs; historical sources are separate issue/archive systems | 1,000 current URLs, not classifiable by publication year; historical total unknown | Sampled 2011 legacy URL resolves; Yale Library archive reaches 1878 | Browser UA for sitemap; Playwright for article fields (JS/shadow-DOM date) | 1–3 s plus rendering; prior runs measured ~3.2 s/article | **~0.9 h** for current 1,000 only; historical run unknown | All sitemap lastmods are 2026 migration/update timestamps; old URLs absent from current sitemap; Playwright required; library archive is issue/PDF/IIIF based and needs a separate design; Wayback is incomplete |

## Per-year discovery counts

### Northwestern

Source: `logs/cache/northwestern_sitemap_by_year.json`, cached
2026-06-16. Counts are exact for the cache's accepted URL pattern and filters,
not a guarantee that every publication item is represented.

```text
2000: 1,439; 2001: 2,663; 2002: 2,726; 2003: 2,978; 2004: 3,276
2005: 3,298; 2006: 3,049; 2007: 2,738; 2008: 2,418; 2009: 3,035
2010: 2,771; 2011: 2,587; 2012: 2,555; 2013: 3,243; 2014: 2,903
2015: 2,825; 2016: 2,637; 2017: 2,724; 2018: 2,570; 2019: 2,529
2020: 2,361; 2021: 2,186; 2022: 2,328; 2023: 2,113; 2024: 2,027
2025: 2,562; 2026: 1,828
Total 2000–2026: 70,369 unique URLs
```

The cache also contains four 1994 URLs but none for 1995–1999. That discontinuity
may be a migration/archive gap and should not be interpreted as proof that the
paper published no articles in those years.

### Chicago Maroon

Source: `logs/cache/chicago_articles_by_year.json`, cached 2026-06-17. These are
**lastmod-year counts**, not verified publication-year counts. Migrated or edited
articles can therefore fall in the wrong year bucket even though the URL
inventory is useful.

```text
2000: 0; 2001: 191; 2002: 557; 2003: 799; 2004: 977
2005: 1,028; 2006: 1,705; 2007: 1,315; 2008: 1,380; 2009: 1,165
2010: 938; 2011: 950; 2012: 1,078; 2013: 1,062; 2014: 958
2015: 917; 2016: 1,189; 2017: 1,144; 2018: 1,027; 2019: 807
2020: 626; 2021: 403; 2022: 496; 2023: 289; 2024: 226
2025: 798; 2026: 414
Total 2000–2026: 22,439 unique URLs
```

### Duke Chronicle

Source: `logs/cache/duke_articles_by_year.json`, cached 2026-06-16. These counts
are **known partial counts**, produced by a seven-page window around an estimated
page for each year in `/section/news`. The repeated 140 values are the window
ceiling (7 pages × 20 archive cards), not complete annual totals.

```text
2000–2014: unknown (not inventoried by the cache)
2015: 78; 2016: 74; 2017: 62; 2018: 68
2019: 140; 2020: 140; 2021: 140; 2022: 140
2023: 140; 2024: 140; 2025: 140; 2026: 80
Cached partial total: 1,342 unique URLs
```

Discovery-only pagination probes found:

- `/section/news`: about 1,110 valid pages; page 1,110 reaches 2002, while page
  1,120 returns HTTP 500. At roughly 20 archive cards per page, News alone is
  about 22,200 listing placements.
- `/section/sports`: page 1,500 still resolves and reaches 1999, so the target
  range through 2000 is reachable there.
- `/section/opinion`: page 500 resolves and reaches 2008.
- `/section/recess`: page 500 returns HTTP 500, so it is shallower than 500 pages.

From these probes, the four major top-level listings likely total roughly
3,000–4,000 pages (**guess**), or 60k–80k listing placements. Because articles
can be cross-listed in parent and child sections, a deduplicated 2000+ inventory
is estimated at 40k–65k unique URLs (**guess**). A complete discovery pass is
required before an exact per-year count is possible.

Other section routes exposed in Duke's navigation are: Blue Zone, Campus Voices,
Centennial, Chronquiry, College Sports 101, Crosswords, Elections, Features,
Football, Guest Columns, Health/Science, Letters to the Editor, Local/National,
Men's Basketball, Opinion, Arts/Culture (`recess`), Recess Campus, Recess
Culture, Recess Local, Sports, Sports Columns, Sports Features, University, and
Women's Basketball. Several are children or special projects, so summing their
page counts would double-count articles.

### Yale Daily News

The current `articles-sitemap.xml` returned exactly 1,000 URLs. All URLs use
dateless `/articles/<slug>` paths and all 1,000 `<lastmod>` values are in 2026,
so the sitemap cannot produce publication-year counts without rendering article
pages. Under Phase 0's no-article-fetch rule:

```text
2000–2026: unknown per year
Current sitemap total: 1,000 URLs (partial and publication dates unclassifiable)
```

The exact 1,000 count appears to be a cap or bounded feed, not a complete archive:
three known legacy articles from 2011 and 2020 were absent from it.

## Yale historical reachability

HEAD-only checks (no article bodies) confirmed that sampled legacy paths still
resolve:

- `/blog/2020/03/16/for-our-readers-covering-the-covid-19-crisis/`
- `/blog/2020/09/25/black-students-for-disarmament-pens-letter-calling-on-yale-to-abolish-its-police-force/`
- `/blog/2011/04/07/yale-acts-on-sex-grievance-overhaul/`

Each redirects first to the slash-normalized legacy path and then to
`/articles/<slug>`, ending with HTTP 200. This proves that at least some migrated
pre-2024 articles remain reachable, but the current sitemap does not enumerate
them.

External holdings:

- [Yale Daily News Historical Archive](https://ydnhistorical.library.yale.edu/)
  provides searchable digitized print issues from 1878 through March 19, 2021
  (24,617 issues reported by the collection). It exposes issue PDFs, OCR text,
  persistent article views, and IIIF resources. It is an issue/archive source,
  not the same schema as current web articles.
- [Yale Library collection description](https://web.library.yale.edu/digital-collections/yale-daily-news-historical-archive)
  confirms more than 140 years of reporting and notes some missing issues.
- The Wayback Machine has captures of legacy web articles, including a 2011
  `/blog/YYYY/MM/DD/<slug>/` article, but coverage is capture-dependent and not
  guaranteed complete.

A historical Yale corpus is feasible only as a separate discovery/extraction
design (current redirects, Yale Library archive, and possibly Wayback). No
Wayback or library scraper should be added implicitly to the current extractor.

## Access and runtime basis

- **Northwestern:** honest research UA, shared `Fetcher`, robots checked,
  sequential requests, configured 6–8 seconds. `70,369 × 7 s = 136.8 h`.
- **Chicago:** honest research UA, shared `Fetcher`, robots checked, sequential
  requests, configured 6–8 seconds. `22,439 × 7 s = 43.6 h`.
- **Duke:** browser UA is necessary because the WAF blocks the honest research
  UA. Browser-UA `robots.txt` says `Crawl-delay: 10`; config uses 10–12 seconds.
  For the 40k–65k unique-URL guess, article fetching is 122–199 hours, plus
  roughly 9–12 hours for 3k–4k discovery pages: **131–211 hours total guess**.
- **Yale:** browser UA is required even for the current sitemap (the honest UA
  returned HTTP 429 during this audit); article fields require Playwright.
  Previous successful logs processed 100 rows in roughly 5.2–5.7 minutes,
  approximately 3.2 seconds/article including configured delay and render time.
  The current 1,000 URLs are therefore about 0.9 hours. Historical runtime is
  unknown because no complete historical URL inventory exists.

## Easiest-to-hardest ranking

### 1. Chicago Maroon

Easiest because a compact, cached WordPress sitemap supplies 22,439 unique URLs,
pages are static, the honest UA works, and the existing extractor already handles
the SNO template. The expected fetch is about 44 hours. The main methodological
risk is that sitemap `<lastmod>` is not publication date, so full-mode range
filtering cannot rely on the cache year alone; the stored on-page publication
date remains authoritative.

### 2. Northwestern

Discovery is strong: the cached Yoast inventory contains 70,369 unique target
URLs with exact years encoded in the URL. Static pages and an existing
multi-template extractor make implementation realistic. It ranks below Chicago
because the corpus is three times larger (~137 hours), historical templates have
already caused body-selector gaps, and the current discovery filter excludes
`/games/`, which must be consciously included or documented for a true
every-article corpus.

### 3. Duke Chronicle

Technically feasible but discovery must be redesigned before fetching. There is
no sitemap, the honest UA is blocked, the 10-second crawl delay is expensive, and
the current cache is only a News-section sample. A full crawl must traverse and
deduplicate several thousand overlapping section pages before the URL count is
known. The current best estimate is 40k–65k unique URLs and 131–211 hours
(**guess**).

### 4. Yale Daily News

Hardest because the current sitemap is a partial, dateless 1,000-URL feed whose
lastmods all reflect 2026, while article dates require Playwright. Historical
articles do survive through redirects and external archives, but neither the
current sitemap nor one homogeneous source enumerates all 2000+ articles. The
Yale Library archive is promising through March 2021, and Wayback fills some web
captures, but both require a separate data model and extraction strategy before
a defensible full-corpus count or runtime estimate is possible.

## Existing documentation/config claims that need correction

These were found during Phase 0 and are reported here rather than silently
changed:

1. `README.md` says Yale has “no recoverable historical archive.” That is too
   strong. The **current rebuilt site's sitemap** has no deep, classifiable
   archive, but sampled legacy URLs still redirect and Yale Library provides
   digitized issues through March 2021.
2. `recon/RECON.md` similarly frames Yale as having no historical depth. It
   should distinguish “not discoverable through the current sitemap” from
   “not recoverable.”
3. `config/sites.yaml` says Chicago's current WordPress archive “effectively
   starts ~2016.” Its cached sitemap inventory reaches 2001 by `<lastmod>`.
   Because `<lastmod>` is only a proxy, the correct statement is that exact
   publication-year coverage is unknown until article dates are read.

No existing file was corrected as part of Phase 0.
