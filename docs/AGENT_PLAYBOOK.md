# Agent Playbook — College Newspaper Corpus

Numbered beginner checklist for scaling from the four-site pilot to the full
target list. Source of truth for methodology: `recon/FULL_CORPUS_AUDIT.md`.
Frozen article schema: ten columns in `src/schema.py` (do not reorder).

---

## 0. Hard rules (read first)

1. Honest research User-Agent is the default.
2. Browser UA is allowed only for approved Duke and Yale access paths.
3. No proxies, CAPTCHA solving, paywall/login bypass, or robots ignoring.
4. On-page publication date is authoritative; never assign year from sitemap
   `<lastmod>`.
5. Only the **main checkout** writes `output/`. Worktrees write fixtures/tests only.
6. Never start a multi-hour full corpus run without explicit approval.
7. `config/sites.yaml` is **lead-owned**. Builders propose blocks in
   `docs/requests/<site>.md` — they do not edit `sites.yaml` directly.

---

## 1. Required architecture order

Do these in order. Do not skip ahead to builder agents before 1–3 are merged.

1. **Crash-safe full mode on the current architecture** (batch append,
   checkpoints, domain lock, sample mode preserved). ← current foundation
2. **Split `extractor.py`** into `src/sites/` and `src/common/`.
3. **Add `src/platforms/` adapters** (WordPress/SNO, SNWorks, Playwright).
4. **Tracker workflow** using `data/targets.csv` (recon updates status fields).
5. **`scripts/validate.py`** — schema, empties, UNPARSED dates, duplicates.
6. **`scripts/status.py`** — per-site progress, ETA, checkpoint age.

**Builder agents should wait until steps 1–3 are merged**, not merely 1–2,
because builders need stable platform APIs. **Recon can run in parallel from
the start.**

---

## 2. Full-mode crash design

- Batch size **50** articles.
- Serialize the whole batch in memory first.
- Append bytes, then `flush` + `fsync`.
- Persist last committed CSV **byte offset** atomically under
  `logs/checkpoints/<site>.json`.
- On restart: truncate only an uncommitted tail back to the recorded offset;
  resume by skipping URLs that already have non-empty text.
- Reject wrong CSV headers.
- Preflight-block existing empty-text rows (do not silently create duplicate URLs).
- `logs/failed_<site>.csv` records `url,year,reason`.
- Progress log every 100 articles with rate.
- Per-domain lock under `logs/locks/` so two processes cannot scrape one host.
- Sample mode still rewrites `output/<site>.csv` at end of run.
- `combined.csv` remains **opt-in** (`--combined`).

CLI sketch:

```bash
python run.py --site chicago --mode full --refresh-discovery
python run.py --site chicago --mode full --max-fetch 200 --lastmod-year 2024 --refresh-discovery
```

---

## 3. Roles and approval gates

| Role | Owns | Must not |
| --- | --- | --- |
| Lead | Architecture, `sites.yaml`, crash safety, merge order, launch gates | Silent full launches; schema changes |
| Recon | Discovery notes, robots, platform guess, tracker status updates | Article-body scrapes; editing extractors |
| Builder | Site/platform modules in a worktree; `docs/requests/<site>.md` | Editing `sites.yaml`; writing `output/` |
| QA | Validation scripts, fixture checks, five live-page spot checks | Changing extractors without a bug ticket |

**Gates**

1. Diff + offline unit tests before any discovery refresh / article fetch for a
   new full-mode path.
2. Bounded test (e.g. max 200) before a multi-hour full run.
3. Explicit approval before launching a full run in `tmux`.
4. PR or `/apply-worktree` before merging builder work into main.

---

## 4. Cursor workflow (worktrees)

Verified against current Cursor docs patterns:

1. Open Agents Window, or use `/worktree`, or CLI `agent --worktree`.
2. Do the work in the isolated worktree.
3. Review the diff; run tests.
4. Land via PR **or** `/apply-worktree`.
5. Clean up with `/delete-worktree`.

**Concurrency recommendation (Pro):** start with **3** concurrent coding agents
(lead + recon + one isolated builder/QA). Official docs publish no fixed cap;
scale only after watching usage and merge load.

**Cloud Agents:** reproducible coding/tests only. **Never** run corpus scrapes
in Cloud Agents.

---

## 5. Parallel scraper operations (Mac)

- Run **2–3 domains** at once on the laptop; **exactly one process per domain**.
- Install: `brew install tmux`.
- Keep the Mac plugged in, lid open, and enable
  **System Settings → Battery → Options → Prevent automatic sleeping on power
  adapter when the display is off**.
- Wrap long runs in `caffeinate -ims` inside a named detached `tmux` session.

Useful commands:

```bash
# start (example — Chicago full run, only after approval)
tmux new -s chicago-full -d \
  'cd /path/to/newspaper-scraper && caffeinate -ims .venv/bin/python run.py --site chicago --mode full --refresh-discovery'

tmux ls
tmux attach -t chicago-full
# detach: Ctrl-b then d
tmux kill-session -t chicago-full

# resume after crash/restart: same command; checkpoints skip committed URLs
```

**Wi-Fi / reboot recovery:** re-attach or restart the same `tmux` command. Full
mode truncates any uncommitted tail and resumes. Re-check domain locks under
`logs/locks/` if a process was killed hard.

---

## 6. Weekly operating routine and throughput

From `recon/FULL_CORPUS_AUDIT.md` only (four built sites):

| Site | Approx domain-hours |
| --- | ---: |
| Chicago | ~43.6 h |
| Northwestern | ~137 h |
| Duke | ~131–211 h (guess) |
| Yale (current 1k only) | ~0.9 h |
| **Four-site total** | **~313–393 h** |

- Three always-on parallel runs: roughly **4–6 days** wall time for that baseline.
- Laptop overnight-only: roughly **2–3 weeks** for the four-site baseline.
- After platform reuse, **1–2 universities/week** is plausible on a laptop.
- 239 university rows / 155 known unique domains ⇒ roughly **78–155 weeks** at
  that laptop rate.
- Main lever toward a ~1-year program: an always-on host plus **3–5** parallel
  domains.

Weekly ritual:

1. Update tracker statuses from recon notes.
2. Finish or resume one full-mode domain.
3. Merge at most one builder PR that touches shared code.
4. Spot-check five live pages per newly completed site.
5. Backup `output/`, checkpoints, and failure logs (checksums) to an external
   drive; cloud object store TBD after PI meeting.

---

## 7. PI / operating decisions (recorded 2026-09-24)

| Decision | Status |
| --- | --- |
| Host | Always-on Mac (this laptop) for now |
| Yale Library archive as corpus data | **Decide later** (see supervisor question below) |
| Browser UA for Duke/Yale | Allowed whenever needed |
| Crosswords / galleries / podcasts | Exclude when body text is empty (do not store empty rows) |
| Priority after Chicago | Finish already-started sites (Chicago → Northwestern → Duke → Yale current site) |
| Git | Commit foundation code; keep `output/*.csv` local only (gitignored) |
| Chicago run length | Until manually stopped; resume via checkpoint |

### Supervisor question (Yale) — copy/paste

> For the Yale Daily News, the current website only lists about 1,000 recent articles, and we cannot see a complete list of everything published since 2000 on that site alone. Yale’s library has a separate historical archive of older issues (often as scanned pages/PDFs, not the same HTML articles). **Should our research corpus include that library archive as a data source, or should we only collect articles from the live yaledailynews.com website?**

---

## 8. Special-case procedures (built sites)

### Chicago Maroon

1. Refresh discovery cache before a full run (`--refresh-discovery`).
2. Full mode emits **every** sitemap article URL (no lastmod year filter).
3. Fetch each URL; store on-page `.sno-story-date` only (no lastmod year assign).
4. Drop rows with parseable year **&lt; 2000** only after that date is parsed.
5. Bounded tests may use `--lastmod-year YYYY --max-fetch 200` to select
   candidates; report on-page year distribution vs lastmod year separately.
6. Delay 6–8 s; honest UA.

### Northwestern

1. Yoast `post-sitemap*.xml`; years are in the URL path.
2. Decide explicitly whether `/games/` stays excluded for “every article.”
3. Multi-template body containers (FLEX + classic); empty bodies go to failure log.
4. Delay 6–8 s; honest UA; expect ~137 h.

### Duke Chronicle

1. No sitemap — discovery must walk section listings with dedupe.
2. Browser UA required (WAF blocks honest UA); robots checked with browser UA.
3. Delay 10–12 s.
4. Before full fetch: complete all-section inventory, confirm ~500-page
   termination behavior, write exact per-year cache. No article fetch during
   discovery-only inventory.

### Yale Daily News

1. Current `articles-sitemap.xml` is capped (~1,000) with migration lastmods.
2. Browser UA for sitemap; Playwright for article fields.
3. Historical corpus needs a **separate** design (Library archive / Wayback);
   do not pretend the current sitemap is complete history.
4. PI decision pending on Library archive as data.

---

## 9. Git and data hygiene

- Keep full outputs in local `output/`.
- Later (separate approved change): `gitignore output/*.csv`, keep
  `output/.gitkeep`, then `git rm --cached output/*.csv` (preserves local files).
- Store small deterministic fixtures under `tests/fixtures/`, not corpus CSVs.
- **Never** push full corpus CSVs to GitHub.
- Backup outputs + checkpoints + failure logs with checksums to an external
  drive and a cloud object store selected after the PI meeting.

---

## 10. Definition of done (per site)

- Tracker row status updated.
- `output/<site>.csv` uses frozen schema/order.
- Checkpoint committed; failed URLs logged with reasons.
- Five live-page spot checks documented.
- Sample mode still works for that site when `mode: sample`.
