# College Newspaper Scraper (Pilot)

A modular, config-driven pipeline for building a small research corpus of
college newspaper articles. The pilot targets three publications:

- **Duke** — The Chronicle
- **Yale** — Yale Daily News
- **Northwestern** — The Daily Northwestern

> Status: **scaffold + reconnaissance phase.** The shared infrastructure
> (fetching, schema, parsing helpers, CSV output, orchestration) is complete.
> Per-site extraction logic is intentionally left as stubs until the
> reconnaissance audit in [`recon/RECON.md`](recon/RECON.md) is filled in.
> We investigate before we build.

## Methodology: recon first

Rather than guessing at each site's structure, we run a landscape audit
first — documented in [`recon/RECON.md`](recon/RECON.md):

1. Is there an **RSS feed**? (cheap, polite discovery)
2. What does **robots.txt** allow for our research User-Agent?
3. Is the article body **static HTML** or **JS-rendered**?
4. Which **fetch method** does that imply (`rss` / `html` / `playwright`)?

That audit drives every implementation decision and is the backbone of this
document.

## Architecture

```
newspaper-scraper/
  config/
    sites.yaml        # per-site config: name, base_url, method, rss_url, selectors, max_articles, rate_limit
  src/
    schema.py         # Article dataclass (the common corpus schema)
    fetcher.py        # shared HTTP layer: rate limiting, backoff, robots.txt, research UA
    extractor.py      # shared parsing helpers + per-site extractors (two-phase)
    writer.py         # CSV output: per-site + combined
    pipeline.py       # orchestrator: config -> extractors -> dedup -> writer
  output/             # generated CSVs (gitignored)
  logs/               # scrape logs (gitignored)
  recon/RECON.md      # landscape audit notes
  run.py              # entry point
```

Adding a new institution requires only **two** changes: a new entry in
`config/sites.yaml` and one `extract_<site>` function (plus its two phase
helpers) in `src/extractor.py`, registered in `SITE_EXTRACTORS`. The
fetch / dedup / write layers never change.

### The Article schema

| field | meaning |
|-------|---------|
| `institution` | publication / university |
| `title` | article headline |
| `author` | byline (cleaned) |
| `publication_date` | ISO 8601 `YYYY-MM-DD`, or `UNPARSED:<raw>` if unparseable |
| `section` | site section/category |
| `url` | canonical article URL (dedup key) |
| `text` | full body text (cleaned) |
| `scraped_at` | ISO 8601 timestamp of collection |

### Design choices baked into the infrastructure

- **Sequential by default** — `max_concurrency: 1`. One request at a time per
  domain. This is intentional politeness toward small newsroom servers, not a
  technical limit; the `Semaphore` is in place to scale later.
- **Polite delays** — randomized 1–3s between requests.
- **Exponential backoff** — retries on 429/5xx and connection errors.
- **robots.txt respected** — fetched and cached per domain. A missing
  robots.txt (404) is logged and treated as no restrictions; an unreachable
  robots.txt fails open with a warning.
- **Honest User-Agent** —
  `CollegeNewspaperResearchBot/1.0 (academic research; contact: valentinatsilva@proton.me)`,
  used for both robots checks and requests. No browser impersonation.
- **Two-phase extraction** — RSS for discovery (URLs/metadata), then HTML for
  full body text, since feeds usually carry only summaries.
- **Bounded collection** — `max_articles` per site (default 100).
- **Date auditing** — unparseable dates are preserved as `UNPARSED:<raw>`
  and logged, never silently dropped.
- **URL deduplication** — performed once in the orchestrator via a per-run
  `seen_urls` set.

## Setup

```bash
cd newspaper-scraper
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Playwright is installed as a library but its browser binaries are **not**
downloaded by default (not needed unless a site turns out to be JS-rendered).
See `.env.example` for the project-local install command if it becomes
necessary.

## Usage

```bash
python run.py --site duke          # one site
python run.py --site all           # every configured site
```

Output CSVs are written to `output/` (`<site>.csv` plus `combined.csv`), and
a timestamped log is written to `logs/`. During the scaffold/recon phase the
extractors are stubs, so a run produces valid **header-only** CSVs and exits
cleanly.

## Next steps

- Fill in `recon/RECON.md` for all three sites.
- Implement the per-site extractors based on the audit.
- **Full-archive collection** is out of scope for this pilot: RSS feeds only
  surface recent items. Collecting complete historical archives requires
  sitemap parsing (`/sitemap.xml`) or archive-page / pagination traversal,
  which is documented here as a deliberate follow-up rather than attempted now.
