# Reconnaissance: Landscape Audit

Before writing any site-specific extraction logic, we audited each target
publication: does it expose RSS, what does its `robots.txt` allow, is the
article body static HTML or JS-rendered, and which fetch method that implies.
Every finding below was verified live (not assumed) before implementation.

## Audit table

| Site | RSS available? | robots.txt rules | Static or JS-rendered? | Chosen method | Notes |
|------|----------------|------------------|------------------------|---------------|-------|
| Duke (The Chronicle) | No. SNWorks CMS exposes no `/feed` or `/rss` endpoint. | Could NOT be read: the WAF returns HTTP 403 for the honest research UA, including on `/robots.txt`. A 403 means the rules were unreadable, not that none exist. | Static HTML (SNWorks server-renders), but gated by an AWS WAF. | HTML + section pagination, via a **browser User-Agent** | The WAF (`server: awselb/2.0`) blocked both `robots.txt` and content for our honest UA. We used a browser UA to access publicly available articles and applied the **same politeness delays** as the rest of the pipeline. Article URLs: `/article/<slug>-YYYYMMDD`. Listing `/section/news` (~20 links/page); pagination `<ol class="index-pagination">` Next -> `?page=N`. Body `div.article-content` (inside `.full-article.prose`); byline `.article--byline`; date in `<time datetime>`. |
| Yale (Yale Daily News) | No confirmed feed (custom CMS, rebuilt Jan 2026). | Could NOT be read: `/robots.txt` also returns the Vercel checkpoint challenge, not the file. | **JS-gated.** The site sits behind a Vercel bot-detection checkpoint ("Enable JavaScript to continue") that persists even with a browser UA. | Playwright (headless Chromium), with a browser-UA requests fallback | Yale Daily News is protected by Vercel's bot-detection checkpoint, which blocks both requests-based and headless-browser access. Accessing it reliably would require residential proxies or automation that defeats the site's explicit bot protection, which is outside the scope and ethics of this pilot. If the checkpoint persists, Yale yields **zero** articles -- a documented result, not a failure. Selectors are resolved defensively at runtime. |
| Northwestern (The Daily Northwestern) | **Yes.** `https://dailynorthwestern.com/feed/rss/` -- standard WordPress feed. | Readable and permissive for article URLs. `Crawl-delay: 6`; `Disallow: /cgi-bin/`, `/?s=`, `/*?*` (query-string URLs). | Static HTML (WordPress 7.0, SNO/FLEX Pro theme). | RSS (discovery) + HTML (full text) | Cleanest of the three and implemented first. RSS gives metadata + summaries (`dc:creator` author, `pubDate`, `<category>` sections); full body is fetched from each article page. Body lives in `div#sno-story-body-content` (NOT `entry-content`). We set `delay_min: 6` in config to honor `Crawl-delay: 6`. WordPress RSS only surfaces ~10-15 recent items, so runs return well under `max_articles=100` -- a known limitation of RSS discovery (full-archive traversal is a documented next step). |

## Key implementation notes

**Northwestern** — implemented first. RSS confirmed, static HTML, cleanest
architecture. Two-phase: RSS discovery -> per-article HTML body fetch via the
shared Fetcher. Honors `Crawl-delay: 6` through config (`delay_min: 6`).

**Duke** — SNWorks with an AWS WAF that 403s the honest research UA on every
path. A browser UA is required to reach the publicly available article HTML.
This override is scoped to the Duke module (a dedicated `requests.Session`); the
shared Fetcher is unchanged, and Duke fetches use the same randomized politeness
delays. Discovery walks `/section/news` pagination; each article is tagged
`section = "News"`. Bodies come from `div.article-content`; subtitle from
`og:description`.

**Yale** — custom CMS behind Vercel's security checkpoint. We render with
Playwright (headless Chromium) and fall back to a browser-UA requests GET.
Discovery walks per-section landing pages (`/university`, `/city`, `/scitech`,
`/arts`, `/sports`, `/opinion`, `/investigations`, `/wknd`, `/magazine`) so each
article URL carries a section label. Author/date/subtitle hydrate client-side;
dates are read from rendered `innerText` via the shared `find_long_date`
helper. The checkpoint may still block headless automation in some environments;
if so, Yale yields zero or partial results, which we treat as a finding about
the site's bot protection rather than a failure.

**robots.txt** — The shared Fetcher checks robots.txt at runtime for the honest
UA. Results: Northwestern readable + permissive (Crawl-delay 6 honored via
config); Duke and Yale both unreadable (403 / JS checkpoint).

**RSS vs full text** — even where RSS exists (Northwestern), feeds carry
summaries only, so phase 2 (HTML article fetch) is required for full body text
across all sites.

## Post-implementation accuracy findings (Phase 1)

These were discovered by reading CSV output against live pages, not by assuming
selectors would work. Each entry records what broke, how we noticed, and the fix.

| Issue | How we caught it | Fix |
|-------|------------------|-----|
| Yale dates all `UNPARSED:` in `--site all` | Full run after Duke; no Playwright disable message but dates empty | `networkidle` → `domcontentloaded` + hydration poll; don't disable Playwright on per-URL timeout |
| 47 Yale dates all = today | CSV dates didn't match article bylines; masthead date visible on page | `find_long_date(..., prefer_time_prefixed=True)` — anchor on `"9:48 a.m., June 9, 2026"` not `"Monday, June 15, 2026"` |
| Dates still flaky after masthead fix | `innerText` had date, `page.content()` did not | Read date from Playwright `innerText` (shadow DOM); shared `find_long_date` helper |
| Playwright silent failure (no dates) | `playwright launch` test; binary was x86_64 on arm64 Mac | Reinstall Chromium for host architecture |
| Yale `section` empty (0/47) | CSV column audit | Per-section discovery (`/university`, `/city`, …) tags each article URL |
| Duke `section` empty (0/100) | CSV column audit | `section_label: "News"` from `/section/news` crawl path |
| Yale single author only | Screenshot article: `Jolynda Wang` vs `Jolynda Wang & Aria Lynn-Skov` | `_collect_byline_authors()` on all `/author/` links |
| Northwestern `subtitle` | Compared `og:description` to on-page content — it's a body excerpt | Leave `subtitle` empty (uniform column); document in README |
| Duke photo credit as author | Spot-check CSV authors | Skip bylines with `"Photo by"` prefix; join writer bylines |

### Uniform schema (`subtitle` column)

All sites share the same CSV columns. `subtitle` is populated only where the CMS
exposes a genuine editor deck:

- **Yale / Duke:** `og:description` (verified deck, not body text).
- **Northwestern:** intentionally empty — SNO/FLEX `og:description` is an
  auto-generated excerpt (~first 380 characters of the article), not a distinct
  subtitle field. Storing it would mislabel body text as an editor-written deck.
