# Project Context

Start-here briefing for a new chat or collaborator. Rules live in
[`AGENTS.md`](../AGENTS.md); this file is the current state and the reasoning
behind it. Update the "Current state" section when runs start or stop.

## Goal

A research corpus of US college student newspapers: every article from
2000-01-01 to today, all sections, for a balanced sample of about 12 papers per
category (National Universities, Liberal Arts, Public Universities, Christian
Colleges). Supervised by Prof. Kim (Columbia DSI Scholars).

Each paper becomes one CSV with the frozen 10-column schema
(`src/schema.py`): `institution, title, subtitle, author, publication_date,
section, subsection, url, text, scraped_at`. The on-page publication date is
authoritative; sitemap `<lastmod>` only buckets candidates.

## Where things run

| Machine | Runs | Why |
| --- | --- | --- |
| AWS EC2 (Ubuntu, `/opt/newspaper-scraper`) | One `newspaper-scraper@<site>` systemd service per paper, code from `main`; nightly S3 backup to `columbia-newspaper-corpus` | Always on |
| Mac (this checkout) | Chicago full run (tmux `chicago-full`), Yale | Yale blocks datacenter IPs; Chicago was already mid-run |

Server runbook: [`deploy/README.md`](../deploy/README.md). First-time AWS
walkthrough: [`docs/AWS_SETUP.md`](AWS_SETUP.md). Cost and architecture:
[`docs/CLOUD_PLAN.md`](CLOUD_PLAN.md).

## Current state (2026-09-25)

- **Chicago** (Mac): full run, about 8.5k of 22.5k URLs when last checked.
- **Northwestern** (server): full run. Cloudflare returned 403 to the server
  after about 9 hours at 6-8 s; now 10-14 s with block backoff. Health after the
  latest restart not yet confirmed.
- **SNO papers** `smu tcu biola union stolaf`: configured, not yet deployed or
  tested on the server.
- **Target list**: `data/targets.csv`, rebuilt from the live Google Sheet export
  (`data/targets_raw.xlsx`) plus `data/target_overrides.csv`.
- **Recon**: `scripts/recon_site.py` has run over the non-excluded list;
  results in `data/recon/` and [`docs/RECON_SUMMARY.md`](RECON_SUMMARY.md).

## How the code is organized

- `run.py` -> `src/pipeline.py`: sample mode (small stratified test) and
  `--mode full` (crash-safe: batches of 50, fsync, byte-offset checkpoints in
  `logs/checkpoints/`, failed URLs retried on the next run).
- `src/fetcher.py`: honest research UA, robots.txt, per-site delay,
  `max_concurrency: 1`, and block backoff (after 5 straight 403/429s: cooldowns
  of 15/30/60/120 min, then `SiteBlockedError`, exit code 75, which systemd
  does not restart).
- `src/wordpress.py`: one adapter for every WordPress paper. Sitemap discovery
  (WordPress-core / Yoast / AIOSEO) plus a page profile: `sno` (`src/sno.py`)
  or `wp_generic`, with per-site `selectors` overrides in `config/sites.yaml`.
  A new WordPress paper is a config entry, no code.
- `src/extractor.py`: hand-written extractors (Duke/SNWorks, Yale/Playwright,
  Chicago, Northwestern). Chicago and Northwestern move onto the shared adapter
  only after their runs finish and a 50-URL comparison matches.

## Target list workflow

1. Export the live Google Sheet to `data/targets_raw.xlsx`.
2. Put decisions in `data/target_overrides.csv` (category fixes,
   `access_profile`, `wave`, `exclusion_reason`), never in the sheet copy or
   the generated CSV.
3. `python scripts/recon_site.py --domain <d>` (or `--tab`, `--all`).
4. `python scripts/build_targets.py` rebuilds `data/targets.csv`,
   `docs/TARGET_LIST_REPORT.md` and `docs/RECON_SUMMARY.md`.

Known sheet errors (handled in overrides): "Union Univeristy" links
Concordiensis (Union College NY, so Liberal Arts); "Oklahoma City Univeristy"
links OU Daily (University of Oklahoma); the live sheet's Christian row 12
said "Point Loma" for Hope's The Anchor (corrected in `targets_raw.xlsx` on
import; fix it in the Google Sheet too). Liberal Arts rows 53-103 have no URLs.

## Planning model

Group papers by **adapter family** and **access profile**, and size them before
scheduling:

- Families: WordPress (SNO + generic), SNWorks, BLOX, custom, archive/OCR.
- Access profiles: `open`, `cloudflare` (slow pace; consider asking the paper),
  `waf_browser_ua` (needs PI approval), `datacenter_blocked` (Mac only),
  `excluded`.
- `est_days = est_urls x delay / 86400` (about 12 s per article at 10-14 s).
- Add 3-4 papers to the server at a time; check 403 counts after 24 h.

## Open items

- Confirm Northwestern is healthy; install the updated systemd unit.
- Deploy the SNO papers to the server; access probe + 200-article tests.
- Honest UA reaches the SNWorks homepages tested so far (Princeton, JHU, Penn,
  Cornell, Dartmouth); Brown and Rice return 403 like Duke. Bring this to
  Prof. Kim before building `src/snworks.py`.
- Northwestern allow-list email (eic@ and web@dailynorthwestern.com).
- Pick the first wave (12 per category; Christian has only 9 usable papers).
