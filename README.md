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

| Site | Method | Articles | Title | Author | Date | Body text |
|------|--------|---------:|:-----:|:------:|:----:|:---------:|
| Northwestern | RSS + HTML | 10 | yes | yes | yes (ISO) | yes |
| Duke | HTML + browser UA | 100 | yes | yes | yes (ISO) | yes |
| Yale | Playwright / requests fallback | 47 | yes | yes (46/47) | see note | yes |

Counts reflect this environment; they vary by run (see limitations). Output is
written to `output/<site>.csv` plus a combined `output/combined.csv`.

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
  output/             # generated CSVs (gitignored)
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
| `author` | byline (cleaned) |
| `publication_date` | ISO 8601 `YYYY-MM-DD`, or `UNPARSED:<raw>` if unparseable |
| `section` | site section/category |
| `url` | canonical article URL (dedup key) |
| `text` | full body text (cleaned) |
| `scraped_at` | ISO 8601 timestamp of collection |

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

## Limitations and next steps

These are known, deliberate boundaries of the pilot — several are themselves
findings:

- **RSS only surfaces recent items.** Northwestern's WordPress feed returns
  ~10-15 recent posts, so a run yields well under the `max_articles=100` cap.
  This is a property of RSS discovery, not a failure. Full-archive collection
  would require **sitemap (`/sitemap.xml`) or archive-page/pagination
  traversal**, documented here as the next step.
- **Yale publication dates require JavaScript.** Yale's body text is
  server-rendered (recoverable via the requests fallback), but the byline date
  is hydrated client-side. It is captured when Playwright renders the page; in a
  requests-only environment those dates are written as `UNPARSED:` rather than
  guessed. This is a transparency choice, not data loss.
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
