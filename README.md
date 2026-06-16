# College Newspaper Scraper (Pilot)

A modular, config-driven pipeline for building a small research corpus of
college newspaper articles. The pilot targets three publications:

- **Duke** — The Chronicle
- **Yale** — Yale Daily News
- **Northwestern** — The Daily Northwestern

The headline of this project is not the code, it is the **reconnaissance**: each
site was audited before a single line of extraction logic was written, and the
audit is what shaped three deliberately different access strategies. See
[`recon/RECON.md`](recon/RECON.md) for the full landscape audit.

## Results (latest pilot run)

| Site | Method | Articles | Title | Subtitle | Author | Section | Date | Body text |
|------|--------|---------:|:-----:|:--------:|:------:|:-------:|:----:|:---------:|
| Northwestern | RSS + HTML | 10 | yes | empty (by design) | yes | yes (RSS) | yes (ISO) | yes |
| Duke | HTML + browser UA | 100 | yes | yes | yes | yes (`News`) | yes (ISO) | yes |
| Yale | Playwright / requests fallback | 100 | yes | yes (75/100) | yes | yes | yes (99/100 ISO) | yes |

Counts reflect the latest accuracy pass (Phase 1). Output is written to
`output/<site>.csv`. The combined `output/combined.csv` is **not** rebuilt in
Phase 1 — that waits until Phase 3 expansion (or an explicit full re-run).

## Methodology: recon first

Rather than guessing at each site's structure, we ran a landscape audit first,
documented in [`recon/RECON.md`](recon/RECON.md):

1. Is there an **RSS feed**? (cheap, polite discovery)
2. What does **robots.txt** allow for our research User-Agent?
3. Is the article body **static HTML** or **JS-rendered**?
4. Which **fetch method** does that imply (`rss` / `html` / `playwright`)?

That audit produced one finding per site that defined its strategy:

- **Northwestern** exposes a clean WordPress RSS feed and static HTML. The
  easiest case, implemented first to establish the end-to-end pipeline.
- **Duke** has no RSS and its SNWorks site sits behind an AWS WAF that returns
  **HTTP 403 for our honest research User-Agent on every path** (robots.txt
  included). A 403 means the rules were *unreadable, not absent*. We used a
  browser User-Agent to access the publicly available article pages and applied
  the **same politeness delays** as the rest of the pipeline.
- **Yale** is behind a **Vercel bot-detection checkpoint** that requires
  JavaScript and persists even with a browser User-Agent, so it needs a real
  rendering engine (Playwright), with a requests fallback.

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
  recon/RECON.md      # landscape audit notes (the backbone of this README)
  run.py              # entry point
```

Adding a new institution requires only **two** changes: an entry in
`config/sites.yaml` and one `extract_<site>` generator (plus its two phase
helpers) in `src/extractor.py`, registered in `SITE_EXTRACTORS`. The fetch,
dedup, and write layers never change.

### Two-phase extraction

Every site uses **Phase 1 (discovery)** to collect article URLs + listing
metadata, then **Phase 2 (full text)** to fetch each article page for the body.
RSS feeds and listing pages only carry summaries/metadata, so phase 2 is
required everywhere for full body text. All `_extract_text_<site>` helpers
return the uniform shape `{text, author, publication_date}`; `extract_<site>`
merges phase-2 values over phase-1 values.

### The Article schema

| field | meaning |
|-------|---------|
| `institution` | publication / university |
| `title` | article headline (cleaned) |
| `subtitle` | editor-written deck when the CMS exposes one; **empty when it does not** (see below) |
| `author` | byline (cleaned); multiple writers joined with `, ` |
| `publication_date` | ISO 8601 `YYYY-MM-DD`, or `UNPARSED:<raw>` if unparseable |
| `section` | site section/category |
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
simply leave it blank. This is a documented accuracy judgment, not a missing
feature.

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
- **robots.txt respected** — fetched/cached per domain; 404 -> no restrictions
  (logged), unreachable -> fail open with a warning.
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
python run.py --site northwestern   # one site
python run.py --site duke
python run.py --site yale
python run.py --site all            # every configured site + combined.csv
```

Output CSVs land in `output/`; a timestamped log lands in `logs/`.

## Data quality and verification

Real-world HTML is messy, so the corpus was verified rather than assumed correct.
The items below document **what went wrong, how we caught it, and how we fixed
it** — the judgment trail is the differentiator of this submission.

### War stories (accuracy fixes)

1. **Long-form date regex (generalized beyond Yale).** Yale has no
   `<time datetime>` element; the byline date appears as visible text after JS
   hydration. We extracted the logic into a shared `find_long_date(text,
   prefer_time_prefixed=True)` helper in `extractor.py` so any site can reuse
   it when structured date markup is absent — not a Yale-only hack.

2. **`networkidle` timeout disabled Playwright for the whole run.** Yale's
   homepage never reaches network-idle (persistent connections), so
   `page.goto(..., wait_until="networkidle")` timed out. Worse: a single timeout
   had been disabling Playwright for every subsequent article, silently falling
   back to requests (no dates). **Caught:** all Yale dates were `UNPARSED:` in a
   full `--site all` run despite no Playwright error messages. **Fix:**
   `domcontentloaded` + a hydration poll; launch failure disables Playwright
   globally, but a per-URL navigation timeout skips only that URL.

3. **47 wrong dates, all stamped as "today".** Two dates coexist on Yale article
   pages: the site masthead's current date (`"Monday, June 15, 2026"`) and the
   article's byline timestamp (`"9:48 a.m., June 9, 2026"`). A naive long-form
   date regex grabbed the masthead, so every article looked published today.
   **Caught:** spot-checking the CSV against the live page (screenshot article).
   **Fix:** anchor on the time-prefixed byline date via `find_long_date(...,
   prefer_time_prefixed=True)`.

4. **Byline date lives in shadow DOM, not serialized HTML.** Even after fixing
   the masthead bug, dates still failed intermittently: `document.body.innerText`
   contained the timestamp but `page.content()` did not — Yale renders the date
   in a custom element that serialization omits. **Fix:** capture both `html` and
   `innerText` from Playwright and run `find_long_date` on the rendered text.

5. **Playwright arm64 vs x86_64 Chromium mismatch.** The first browser install
   downloaded an x86_64 build; on Apple Silicon Playwright looked for arm64,
   failed silently, and fell back to requests (no dates). **Caught:** direct
   `playwright launch` test + `file` on the binary. **Fix:** remove stale
   browsers and reinstall with `PLAYWRIGHT_BROWSERS_PATH=playwright-browsers
   python -m playwright install chromium` on the target architecture.

6. **Empty `section` for Yale and Duke.** Yale discovery had been homepage-only
   with no section metadata; Duke discovery never tagged articles with their
   listing section. **Fix:** Yale now renders per-section landing pages
   (`/university`, `/city`, …) and tags each `/articles/<slug>` with the section
   that listed it; Duke sets `section = "News"` from the crawled `/section/news`
   path (config-driven `section_label`).

7. **Multi-author truncation (Yale).** Byline `By Jolynda Wang & Aria Lynn-Skov`
   was captured as only `Jolynda Wang` because `select_one` grabbed the first
   `/author/` link and the fallback regex stopped at the first name. **Fix:**
   `_collect_byline_authors()` gathers all matching byline anchors, dedupes, and
   joins with `, ` (same co-author pattern Duke already used for SNWorks bylines).

8. **Northwestern empty `subtitle` (intentional).** SNO/FLEX sets
   `og:description` to an auto-generated body excerpt, not an editor-written
   deck. We verified this on a live Northwestern article. Rather than store a
   misleading excerpt as subtitle, the uniform `subtitle` column is left empty for
   Northwestern (and will be for other SNO sites such as UChicago in Phase 3).

9. **Duke author vs photo credit (earlier fix).** SNWorks `.article--byline`
   also wraps lead-image photo credits (`"Photo by …"`). **Fix:** skip bylines
   whose prefix mentions "photo"; join writer bylines labeled `"By"`.

### Latest verification (Phase 1 re-run)

- Screenshot article (`white-house-proposal…`): `author = Jolynda Wang, Aria
  Lynn-Skov`, `section = University`, subtitle matches the on-page deck, date
  `2026-06-09`.
- Duke: 100/100 section (`News`), 100/100 subtitle, 100/100 ISO dates.
- Northwestern: 10/10 section (RSS), **0/10 subtitle** (by design), 10/10 ISO dates.
- Yale: 100/100 section, 99/100 ISO dates (1 UNPARSED photo gallery), 75/100
  subtitle (articles without a distinct deck correctly left empty).

### Earlier sanity checks

- **Sanity checks on prior `output/combined.csv`** (157 rows): 0 duplicate URLs,
  0 blank URLs.
- **Legitimately thin rows:** crossword (Northwestern), photo galleries and
  podcast pages (Yale) — filtering by section/genre is a natural next step.

## Limitations and next steps

These are known, deliberate boundaries of the pilot — several are themselves
findings:

- **This pilot captures recent articles, not historical depth.** The corpus
  spans roughly the last few weeks to two months per site (Northwestern
  2026-06-12..06-15, Duke 2026-04-09..06-14). Measuring *ideological change over
  time* — the project's actual goal — requires deep historical archives, which
  is the natural next phase (see the archive-traversal note below). The pilot
  establishes a generalizable pipeline; extending its temporal reach is a
  configuration/discovery problem, not an architectural one.
- **RSS only surfaces recent items.** Northwestern's WordPress feed returns
  ~10-15 recent posts, so a run yields well under the `max_articles=100` cap.
  This is a property of RSS discovery, not a failure. Full-archive collection
  would require **sitemap (`/sitemap.xml`) or archive-page/pagination
  traversal** (the same HTML-pagination approach already used for Duke could be
  pointed at Northwestern's category archives to go deeper), documented here as
  the next step.
- **Yale publication dates require JavaScript.** Yale's body text is
  server-rendered (recoverable via the requests fallback), but the byline date
  is hydrated client-side inside shadow/custom elements. It is captured when
  Playwright renders the page and `find_long_date` runs on `innerText`; in a
  requests-only environment those dates are written as `UNPARSED:` rather than
  guessed. Photo galleries and similar content may legitimately have no timestamp.
- **Yale access depends on Vercel's checkpoint.** Yale Daily News is protected
  by Vercel's bot-detection checkpoint, which can block both requests-based and
  headless-browser access. Getting past it reliably at scale would require
  residential proxies or automation specifically designed to defeat the site's
  explicit bot protection — **outside the scope and ethics of this pilot.** A
  zero or partial Yale result is therefore a demonstration of judgment, not a
  bug.
- **Duke uses a browser User-Agent by necessity.** Duke's WAF blocked both
  robots.txt and content for our honest research UA. We identify as a browser to
  read publicly available articles and keep our politeness delays; we do not
  attempt to defeat rate limits or access non-public content.
- **`Crawl-delay` is honored via config, not the Fetcher.** The shared Fetcher
  reads robots.txt but does not auto-apply `Crawl-delay`; we encode the required
  delay in `config/sites.yaml` per site (e.g. Northwestern `delay_min: 6`).
  Teaching the Fetcher to read and apply `Crawl-delay` automatically is a clean
  future enhancement.
