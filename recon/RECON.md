# Reconnaissance: Landscape Audit

Before writing any site-specific extraction logic, we audit each target
publication: does it expose an RSS feed, what does its `robots.txt` allow,
is the article content static HTML or JavaScript-rendered, and therefore
which fetch method we will use.

This document is filled in BEFORE implementing extractors. It is the
backbone of the project README.

## Audit table

| Site | RSS available? | robots.txt rules | Static or JS-rendered? | Chosen method | Notes |
|------|----------------|------------------|------------------------|---------------|-------|
| Duke (The Chronicle) | | | | | |
| Yale (Yale Daily News) | | | | | |
| Northwestern (The Daily Northwestern) | | | | | |

## Column guidance

- **RSS available?** — Is there a discoverable feed (e.g. `/feed`, `/rss`,
  `<link rel="alternate" type="application/rss+xml">`)? Note the URL and
  whether items include full text or only summaries.
- **robots.txt rules** — Relevant `Disallow`/`Allow` paths and any
  `Crawl-delay`. Confirm our paths are permitted for our research User-Agent.
- **Static or JS-rendered?** — Does the article body appear in the raw HTML
  (`requests` is enough) or is it injected client-side (needs Playwright)?
- **Chosen method** — `rss`, `html`, or `playwright`.
- **Notes** — Pagination, archive/sitemap availability, author/date markup
  quirks, rate-limit observations, anything that affects implementation.
