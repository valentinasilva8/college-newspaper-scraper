# College Newspaper Scraper (Pilot)

A modular, config-driven pipeline for building a small research corpus of
college newspaper articles. The pilot targets three publications:

- **Duke** — The Chronicle
- **Yale** — Yale Daily News
- **Northwestern** — The Daily Northwestern

Each site was audited before any extraction logic was written: does it expose
RSS, what does its `robots.txt` allow, is the body static HTML or JS-rendered?
Those answers shaped three deliberately different access strategies. See
[`recon/RECON.md`](recon/RECON.md) for the full landscape audit.

## Results (latest run)

| Site | Method | Articles | Year span | Title | Subtitle | Author | Section | Date | Body text |
|------|--------|---------:|-----------|:-----:|:--------:|:------:|:-------:|:----:|:---------:|
| Northwestern | Sitemap + HTML (stratified) | 62 | 2000–2026 | yes | empty (by design) | yes* | yes | yes (ISO) | yes |
| Duke | HTML + browser UA (stratified) | 133 | 2015–2026 | yes | yes | yes | yes | yes (ISO) | yes |
| Yale | Playwright + sitemap | 114 | 2024–2026 | yes | yes | yes | yes | yes (ISO) | yes |

Northwestern coverage is even — **2 articles for every year 2000–2025** (plus the
10 most-recent for 2026) — with no empty body text and only 2 rows missing a
byline. Body extraction handles three eras of the SNO theme (see below).

**Why the row counts differ.** Counts reflect the *sampling configuration*, not
the size of each archive. Northwestern is sampled thin-and-wide on purpose — a
fixed `per_year` across 27 years for an even diachronic grid — so it has the most
years but the fewest rows, even though its sitemap exposes the largest archive of
the three (~70k article URLs). Duke and Yale instead weight toward recent
coverage, so their totals are higher despite shorter spans. Row count is a tunable
function of `per_year` × year range (see `config/sites.yaml`): raising
Northwestern's `per_year` would scale it up while preserving the even spread.

Output is written to `output/<site>.csv`. `combined.csv` is rebuilt only when `python run.py --site all --combined` is run explicitly.

Runs are **incremental by default**: existing rows in a per-site CSV are preserved and their URLs are not re-fetched unless the stored body text is empty. Northwestern sitemap URLs are cached under `logs/cache/` so re-runs skip the 74-file scan unless `--overwrite` is used. Discovery logs progress every 10 sitemap sub-files (Northwestern) or listing pages (Duke). Use `--overwrite` for a full rebuild.

## Data quality and verification

The most important fixes were caught by comparing each site's output with its
live article pages rather than assuming the output was correct. That comparison
surfaced problems a schema check alone would miss: on **Duke**, the byline
selector was capturing lead-image photo credits ("Photo by…") instead of writers,
and multi-author bylines were truncated to the first name — both fixed to select
the writer byline and join co-authors with `, `. The same audit showed `section`
and `subsection` were flattened or empty (Duke hard-coded to `News`, Northwestern
reading an unordered RSS tag bag), so the hierarchy is now read from each paper's
own nav and kicker. On **Yale**, the body text is server-rendered but the byline
date hydrates client-side via JavaScript, so dates require browser rendering to
capture accurately — and an early bug that grabbed the site masthead's *current*
date instead of the article's publication date was caught this way and fixed. On
**Northwestern**, comparing the output's year histogram to the live archive
revealed an 8-year gap (2015–2023) where a different CMS theme left the body
container empty; the selector chain was extended so coverage is now even across
all years. Across the corpus, all dates are normalized to ISO 8601 — unparseable
ones (and ambiguous cases where more than one date appears on a page) preserved as
`UNPARSED:<raw>` rather than silently dropped — and there are zero duplicate URLs.
The detailed case-by-case log is in [Accuracy fixes](#accuracy-fixes) below.

## Methodology: recon first

Before writing any extraction logic, each site was audited:

1. Is there an **RSS feed** or sitemap? (cheap, polite discovery)
2. What does **robots.txt** allow for our research User-Agent?
3. Is the article body **static HTML** or **JS-rendered**?
4. Which **fetch method** does that imply?

The audit produced one finding per site that defined its strategy:

- **Northwestern** exposes a Yoast sitemap (~70k article URLs back to 1994) and
  static HTML. Discovery uses the sitemap with **date-stratified sampling**
  (`per_year` articles per year in a configurable range) rather than RSS, which
  only surfaces ~10 recent items. The full year→URL map is cached under
  `logs/cache/` so re-runs skip the ~74-file sitemap scan. Discovery **oversamples**
  each year and the extractor keeps fetching until `per_year` articles yield real
  body text, so unparseable pages never leave a hole in the year coverage. Body
  text is read from whichever SNO container the era used (`#sno-story-body-content`
  on the current FLEX Pro theme, `#classic_story` / `#sno-sites-main-content` on
  the 2015–2023 classic theme), with a densest-`<p>`-cluster fallback. The
  robots.txt `Crawl-delay: 6` is honored via `delay_min: 6`.
- **Duke** has no RSS and its SNWorks site sits behind an AWS WAF that returns
  **HTTP 403 for our honest research User-Agent on every path** (robots.txt
  included). A browser User-Agent accesses publicly available articles with
  `Crawl-delay: 10` honored via config. Discovery uses **per-year page targeting**
  on `/section/news` (~28 listing pages per year back) instead of walking every
  page linearly; results are cached under `logs/cache/`.
- **Yale** is behind a **Vercel bot-detection checkpoint** requiring Playwright.
  The rebuilt site (Jan 2026) has no deep historical archive; discovery uses
  `articles-sitemap.xml` for breadth while section labels come from per-section
  landing pages and on-page kickers when present.

## Architecture

```
newspaper-scraper/
  config/
    sites.yaml        # per-site config: name, base_url, method, selectors, max_articles, rate_limit
  src/
    schema.py         # Article dataclass (the common corpus schema)
    fetcher.py        # shared HTTP layer: rate limiting, backoff, robots.txt, honest research UA
    extractor.py      # shared parsing helpers + per-site extractors (two-phase)
    writer.py         # CSV output: per-site + combined
    pipeline.py       # orchestrator: config -> extractors -> dedup -> writer
  output/             # generated CSVs (committed: the pilot corpus)
  logs/               # scrape logs (gitignored)
  recon/RECON.md      # landscape audit notes
  run.py              # entry point
```

Adding a new institution requires only **two** changes: an entry in
`config/sites.yaml` and one `extract_<site>` generator (plus its two phase
helpers) in `src/extractor.py`, registered in `SITE_EXTRACTORS`. The fetch,
dedup, and write layers never change.

### Two-phase extraction

Every site uses a **discovery pass** to collect article URLs and listing
metadata, then a **full-text pass** to fetch each article page for the body.
RSS feeds and listing pages only carry summaries, so the full-text pass is
required everywhere. All `_extract_text_<site>` helpers return the uniform
shape `{text, author, publication_date, subtitle, section, subsection}`;
`extract_<site>` merges full-text values over discovery values.

### The Article schema

| field | meaning |
|-------|---------|
| `institution` | publication / university |
| `title` | article headline (cleaned) |
| `subtitle` | editor-written deck when the CMS exposes one; **empty when it does not** (see below) |
| `author` | byline (cleaned); multiple writers joined with `, ` |
| `publication_date` | ISO 8601 `YYYY-MM-DD`, or `UNPARSED:<raw>` if unparseable |
| `section` | top-level category in the paper's own taxonomy |
| `subsection` | child category when the paper nests sections; **empty otherwise** |
| `url` | canonical article URL (dedup key) |
| `text` | full body text (cleaned) |
| `scraped_at` | ISO 8601 timestamp of collection |

**Subtitle population rules (uniform column, honest empties):**

| Site | `subtitle` populated? | Why |
|------|----------------------|-----|
| Yale | Yes | `og:description` is a genuine editor deck (matches the on-page `<h2>`) |
| Duke | Yes | `og:description` carries the article deck |
| Northwestern | **No — left empty** | SNO/FLEX theme sets `og:description` to an auto-generated body excerpt (~first 380 chars), not a distinct deck. Storing that excerpt as a subtitle would be inaccurate. |

The column exists on every CSV so the schema stays uniform; Northwestern rows
simply leave it blank. This is a deliberate accuracy choice, not a missing
feature.

### Section hierarchy: `section` + `subsection`

Each paper uses a different category depth, so a single `section` field would
either lose detail or mix levels. We capture **two levels** uniformly:

| Site | `section` | `subsection` | Source |
|------|-----------|--------------|--------|
| Duke | `News` | `University`, `Local/National`, … | per-article kicker `"News \| University"` above the headline (JSON-LD `articleSection` fallback) |
| Northwestern | `Campus`, `A&E`, … | `Academic`, `Events`, … (empty for top-level articles) | `<meta article:section>` (most-specific) resolved to its parent via the nav `/category/<parent>/<child>/` taxonomy |
| Yale | `University`, `City`, `Sports`, … | **always empty** | per-section discovery page; Yale's nav is single-level (no sub-menus) |

Rules:

- `section` is the broad bucket, `subsection` the specific child. Labels are
  taken **verbatim from each publication's own nav** — no cross-paper
  normalization (that would be a separate modeling step).
- When a paper's category is itself top-level (Yale entirely; Northwestern
  articles filed directly under "A&E" or "Crosswords"), `subsection` is left
  empty rather than duplicated or invented.

### Design choices baked into the infrastructure

- **Honest User-Agent by default** —
  `CollegeNewspaperResearchBot/1.0 (academic research; contact: valentinatsilva@proton.me)`,
  used for both robots.txt checks and requests. A browser UA is used **only**
  where a site's bot protection blocks the honest one (Duke), and that decision
  is documented per site.
- **Sequential by default** — `max_concurrency: 1`. One request at a time per
  domain: intentional politeness toward small newsroom servers, not a limit.
- **Polite, configurable delays** — randomized per request. Northwestern uses
  `delay_min: 6` to honor its `robots.txt` `Crawl-delay: 6`; Duke/Yale reuse the
  same randomized-delay logic even when fetching outside the shared Fetcher.
- **robots.txt respected** — fetched/cached per domain; 404 → no restrictions
  (logged), unreachable → fail open with a warning.
- **Exponential backoff** — retries on 429/5xx and connection errors.
- **Bounded collection** — `max_articles` per site (default 100); no full
  archives in the pilot.
- **Date auditing** — unparseable dates are preserved as `UNPARSED:<raw>` and
  logged with the article URL, never silently dropped.
- **URL deduplication** — performed once in the orchestrator via a per-run
  `seen_urls` set.

## Setup

```bash
cd newspaper-scraper
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Yale only: install the headless browser used to clear its JS checkpoint.
# Kept inside the gitignored playwright-browsers/ folder (see .env.example).
PLAYWRIGHT_BROWSERS_PATH=playwright-browsers python -m playwright install chromium
```

## Usage

```bash
python run.py --site northwestern   # one site (incremental merge)
python run.py --site duke
python run.py --site yale
python run.py --site all            # every site (no combined.csv unless --combined)
python run.py --site northwestern --overwrite   # ignore existing rows + refresh sitemap cache
python run.py --site all --combined             # also write output/combined.csv
```

Output CSVs land in `output/`; a timestamped log lands in `logs/`.

## Data quality

Each site required debugging before the output was trusted. The items below
document what went wrong, how it was caught, and what was fixed.

### Accuracy fixes

1. **Long-form date regex, generalized.** Yale has no `<time datetime>` element;
   the byline date appears as visible text after JS hydration. The logic was
   extracted into a shared `find_long_date(text, prefer_time_prefixed=True)`
   helper so any site can reuse it when structured date markup is absent.

2. **`networkidle` timeout disabled Playwright for the whole run.** Yale's
   homepage never reaches network-idle (persistent connections), so
   `page.goto(..., wait_until="networkidle")` timed out. A single timeout was
   then disabling Playwright for every subsequent article, silently falling back
   to requests (no dates). **Caught:** all Yale dates came back `UNPARSED:` in a
   full run despite no error messages. **Fix:** `domcontentloaded` + a hydration
   poll loop; a browser launch failure disables Playwright globally, but a
   per-URL navigation timeout skips only that URL.

3. **47 wrong dates, all stamped as today.** Two dates coexist on Yale article
   pages: the site masthead's current date and the article's byline timestamp.
   A naive regex grabbed the masthead. **Caught:** the CSV dates didn't match
   the live articles. **Fix:** anchor on the time-prefixed byline date (e.g.
   `"9:48 a.m., June 9, 2026"`) via `find_long_date(...,
   prefer_time_prefixed=True)`.

4. **Byline date in shadow DOM, invisible to serialized HTML.** After fixing the
   masthead issue, dates still failed intermittently: `document.body.innerText`
   contained the timestamp but `page.content()` did not — Yale renders the date
   inside a custom element that HTML serialization omits. **Fix:** capture both
   `html` and `innerText` from Playwright and run `find_long_date` on the
   rendered text.

5. **Playwright arm64 vs x86_64 Chromium mismatch.** The first browser install
   downloaded an x86_64 build; on Apple Silicon Playwright silently fell back to
   requests (no dates). **Caught:** running `playwright launch` directly and
   inspecting the binary with `file`. **Fix:** remove the stale build and
   reinstall with `PLAYWRIGHT_BROWSERS_PATH=playwright-browsers python -m
   playwright install chromium` on the target architecture.

6. **Empty `section` for Yale and Duke.** Yale discovery had been homepage-only
   with no section metadata; Duke never tagged articles with their listing
   section. **Fix:** Yale renders per-section landing pages and tags each article
   URL; Duke derives `section = "News"` from the crawled `/section/news` path.

7. **Multi-author truncation (Yale).** Byline `By Jolynda Wang & Aria Lynn-Skov`
   was captured as only `Jolynda Wang` because the code grabbed the first
   `/author/` link only. **Fix:** `_collect_byline_authors()` collects all
   matching byline anchors, dedupes, and joins with `, `.

8. **Northwestern subtitle left empty (intentional).** SNO/FLEX sets
   `og:description` to an auto-generated body excerpt, not an editor deck. Using
   it as a subtitle would mislabel body text. The `subtitle` column is present
   but left empty for Northwestern; Yale and Duke populate it from `og:description`
   where it genuinely carries the deck.

9. **Duke photo credit misidentified as author.** SNWorks `.article--byline`
   wraps both writer bylines and lead-image photo credits. **Fix:** skip bylines
   whose prefix contains "Photo by"; join writer bylines labeled "By".

10. **Section hierarchy flattened to one level.** Northwestern's RSS `<category>`
    tags are an unordered bag that mixes sections, subsections, nav buckets, and
    people's names — `tags[0]` was effectively arbitrary. Duke was hard-coded to
    `"News"`. **Caught:** comparing the CSV section values to the live nav, which
    nests categories (Duke `News > University`, Northwestern `Campus > Academic`).
    **Fix:** added a `subsection` column; Duke resolved from its
    `"Section | Subsection"` kicker, Northwestern by mapping `article:section` to
    its parent through the nav's `/category/<parent>/<child>/` taxonomy.

11. **Northwestern depth limited by RSS.** The WordPress RSS feed returns only
    10–15 recent items regardless of `max_articles`. **Fix:** Yoast sitemap
    discovery with date-stratified per-year sampling (configurable
    `year_start`/`year_end`/`per_year`); `/games/` URLs are excluded from
    discovery. The full year→URL map is cached so re-runs skip the ~74-file scan.

12. **Northwestern 2015–2023 returned empty bodies, leaving an 8-year hole.**
    The first stratified run produced clean rows for 2000–2014 and 2024–2026 but
    nothing for 2015–2023: every sampled article logged "body container not
    found". **Caught:** verifying the year histogram of the output CSV after the
    run. **Cause:** that era uses the **classic SNO template**, whose body lives
    in `#classic_story` / `#sno-sites-main-content`, not the FLEX Pro
    `#sno-story-body-content`; the byline is `span.storybyline` (`"Name , Role"`)
    rather than `.sno-story-byline`. **Fix:** added the classic container ids to
    the body selector chain and the classic byline to the author logic, and made
    discovery **oversample + backfill per year** so a few unparseable pages can't
    re-open the gap. Result: an even 2 articles/year across 2000–2025.

### Archive expansion (diachronic sampling)

To support studying campus culture over time, Northwestern and Duke use
**date-stratified sampling**: roughly `per_year` articles per calendar year in a
configured range, spread evenly across each year's candidates (not first-N).
Yale cannot be stratified historically — the rebuilt site exposes dateless slugs
in its sitemap and dates require Playwright — so Yale widens **recent** coverage
via `articles-sitemap.xml` instead.

| Site | Discovery | Year range (first run) | `per_year` |
|------|-----------|------------------------|------------|
| Northwestern | Yoast `post-sitemap*.xml` → filter `/YYYY/MM/DD/...` paths | 2000–2026 | 2 |
| Duke | Deep `/section/news` pagination → slug `-YYYYMMDD` | 2015–2026 | 3 |
| Yale | `articles-sitemap.xml` + section-page labels | recent only | n/a |

Tradeoffs: the one-time Northwestern sitemap scan is slow (~74 files at
`Crawl-delay: 6`) but is cached afterward; Duke discovery is slow at
`Crawl-delay: 10`; Yale has no recoverable deep archive on the current site.

### Verification results

- Spot-checked article (`white-house-proposal…`): `author = Jolynda Wang, Aria
  Lynn-Skov`, `section = University`, subtitle matches the on-page deck, date
  `2026-06-09`.
- Northwestern (62 rows): even **2/year for 2000–2025** plus 10 for 2026; 0 empty
  body text, 0 empty section, all dates ISO, only 2 rows missing a byline;
  subtitle empty on all rows by design.
- Duke (133 rows): clean 2015–2026 spread, 0 empty section, 0 duplicate/blank
  URLs, all dates ISO; subsection empty only where the kicker is single-level.
- Yale (114 rows): 2024–2026 (recent only), 0 empty section/text, 2 UNPARSED
  dates (photo galleries with no byline timestamp).

Combined corpus: **0 duplicate URLs, 0 blank URLs** across all sites.

Legitimately thin rows: crossword (Northwestern), photo galleries and podcast
pages (Yale). Filtering by section/subsection is a natural next step.

## Limitations and next steps

Known boundaries of the pilot:

- **Temporal depth.** Northwestern (even 2/year, 2000–2026) and Duke (2015–2026)
  use date-stratified sampling; Yale is recent-only on the rebuilt site. Raise
  `per_year` in `config/sites.yaml` for a denser sample; runs are incremental and
  top up sparse years rather than re-fetching existing rows.
- **Yale has no recoverable historical archive.** The Jan 2026 rebuild exposes
  ~1,000 dateless slugs in `articles-sitemap.xml`; publication dates require
  Playwright rendering per article.
- **Duke uses a browser User-Agent by necessity.** Duke's WAF blocked both
  robots.txt and content for the honest research UA. We identify as a browser to
  access publicly available articles and keep politeness delays; we do not attempt
  to defeat rate limits or access non-public content.
- **`Crawl-delay` is honored via config, not auto-applied.** The shared Fetcher
  reads robots.txt but does not auto-apply `Crawl-delay`; the delay is encoded
  in `config/sites.yaml` per site.
- **Text enrichment.** The corpus contains raw cleaned text. A natural
  enrichment pass would run Named Entity Recognition (e.g. spaCy) to tag people,
  organizations, and locations inline, and add keyword or topic fields (TF-IDF,
  BERTopic). These tags would make the corpus far more useful for research
  queries — filtering by named entity, building co-occurrence networks, tracking
  entity salience over time — and would index cleanly into a database or vector
  store for retrieval-augmented analysis.
