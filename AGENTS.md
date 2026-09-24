# AGENTS.md — College Newspaper Scraper

Role boundaries and hard constraints for every coding agent working in this
repository. Keep this file aligned with `.cursor/rules/newspaper-scraper.mdc`
(Cursor docs do not define precedence between Project Rules and `AGENTS.md`).

Detailed checklist: [`docs/AGENT_PLAYBOOK.md`](docs/AGENT_PLAYBOOK.md).
Copy/paste prompts: [`docs/AGENT_PROMPTS.md`](docs/AGENT_PROMPTS.md).

## Roles

| Role | May do | Must not |
| --- | --- | --- |
| Lead | Shared architecture, crash-safe full mode, `config/sites.yaml`, merge order, launch gates | Silent multi-hour scrapes; schema changes |
| Recon | Discovery, robots, platform notes, tracker status fields | Fetching article bodies at scale; editing extractors |
| Builder | Site/platform code in a worktree; propose YAML in `docs/requests/<site>.md` | Edit `config/sites.yaml`; write `output/` |
| QA | Validators, fixtures, spot checks | Drive corpus scrapes from worktrees |

## Frozen schema

CSV columns and order are fixed in `src/schema.py`:

`institution, title, subtitle, author, publication_date, section, subsection, url, text, scraped_at`

Do not rename, reorder, or drop columns. Subtitle behavior stays as implemented
unless the lead opens an explicit change.

## Access and politeness

- Default User-Agent: honest research bot from `config/sites.yaml`.
- Browser UA: **Duke and Yale only**, and only on already-approved paths.
- Honor `robots.txt` and configured crawl delays.
- One process per domain (domain lock). Max 2–3 domains in parallel on the laptop.
- No proxies, CAPTCHA farms, paywall/login bypass, or IP rotation.

## Dates

- Article-page publication date is authoritative.
- Never assign corpus year from sitemap `<lastmod>`.
- Full mode may use lastmod only to **select test candidates**.

## Outputs and git

- Only the **main checkout** writes `output/`.
- Worktrees write tests/fixtures only.
- Do not commit full corpus CSVs. Fixtures go under `tests/fixtures/`.
- Do not `git commit`, `merge`, or `push` unless the user explicitly asks.

## Approval gates

1. Offline unit/fixture tests before network use for new full-mode code.
2. Show shared-code diff before discovery refresh / article requests.
3. Bounded test before any multi-hour full run.
4. Explicit user approval before launching a full run (`tmux` + `caffeinate`).
5. Builders land via PR or `/apply-worktree`; lead merges `sites.yaml`.

## Architecture order

1. Crash-safe full mode (current tree)
2. Split `extractor.py` → `src/sites/` + `src/common/`
3. Platform adapters → `src/platforms/`
4. Tracker workflow
5. `scripts/validate.py`
6. `scripts/status.py`

Delay builder swarms until **1–3** are merged. Recon may start immediately.

## Definition of done (site)

- Tracker updated; frozen schema intact; checkpoint + failure log present;
  five live-page checks noted; sample mode still works.
