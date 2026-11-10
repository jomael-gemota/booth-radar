# Custom Fields and Site Feeds

**Date:** 2026-11-10
**Status:** accepted
**Author:** collaborative

## Context

Implements the first slice of phases 2–4 of
[general web scraping via MCP](2026-11-07-general-web-scraping-mcp.md): users want to
scrape any public site (example: thorogoodusa.com, a B2B boot brand) for whatever fields
they ask for, fast. Today extraction, the results table and CSV are fixed to
`{brand, booth}`.

Probe of thorogoodusa.com (2026-11-10): WordPress, public REST API with
`/wp-json/wp/v2/product` (115 records, custom fields under `acf`) and
`/wp-json/wp/v2/wpsl_stores` (33 dealer locations), plus Yoast sitemaps. The full catalog
is two HTTP requests (~5 s); rendering 115 pages and running 115 LLM extractions takes
minutes.

## Decision

### Results rows with any columns

- A result row is any `Record<string, string>`. The worker always sends complete rows
  (every column present, `""` when empty), so the **column order is the key order of the
  first row**. No separate `columns` field or Mongo migration; legacy `{brand, booth}`
  rows work unchanged.
- Server: `BoothEntry` becomes `ResultRow`; CSV header comes from the first row's keys
  (first letter capitalised, so legacy files keep `Brand,Booth`).
- Client: `ResultsTable` renders columns dynamically; labels say "rows" instead of
  "exhibitors".
- Worker: `results_table.ResultsTable` owns columns, rows and de-duplication (on
  `key_fields`, default all columns). New fields that overlap the current columns extend
  the table (old rows get `""`); a disjoint set of fields needs `replace=true`, so a
  table is never wiped silently.

### General extraction

- `extractor.extract(page_text, url, fields=None, instructions="")`: with `fields`, a
  generic prompt returns one object per item with exactly those keys; without `fields`,
  the existing booth prompt and normalisation run unchanged.
- The extraction cache key includes fields and instructions.
- Scraped page text gets a compact **JSON-LD** block appended (Product, Offer,
  ItemList, Event, LocalBusiness and similar nodes; Yoast's WebPage/Breadcrumb noise is
  skipped), so product pages expose price/SKU/brand to the extractor.

### Site feeds (`worker/site_feeds.py`)

- `find_feeds(url)`: detects **WordPress REST** content types (from `/wp/v2/types`,
  excluding `attachment`, menu and `wp_*` internals, with record counts from
  `X-WP-Total`) and **Shopify** `products.json`. Cached per origin.
- `read_feed(feed)`: fetches all records (WordPress `per_page=100`, Shopify
  `limit=250`, pages in parallel, capped by `MAX_FEED_RECORDS`, default 2000) and
  flattens them: rendered HTML → text, taxonomy ids → term names, `acf` custom fields
  inlined, SEO/link metadata dropped. Shopify products get price range, vendor, type,
  tags, SKUs and product URL.
- Records are then mapped to the user's fields by the extraction model in batches
  (~12k chars per batch, `EXTRACT_CONCURRENCY` in parallel, default 6), which handles
  derived values (e.g. boot height inside a title). Deterministic field mapping without
  an LLM is left for the recipes phase.
- `map_site(url, include?, limit?)`: reads `robots.txt` sitemaps (or `/sitemap.xml`,
  `/sitemap_index.xml`), follows sitemap indexes, filters by substring.
- Feed and sitemap requests respect `robots.txt` (`BoothRadar` user agent).

### Tools (inline protocol and MCP server)

| Tool | Change |
|------|--------|
| `find_feeds(url)` | new |
| `read_feed(url, feed, fields, instructions?, key_fields?, replace?)` | new |
| `map_site(url, include?, limit?)` | new |
| `extract(urls, fields, instructions?, key_fields?, refresh?, replace?)` | new, general |
| `extract_booths(urls, refresh?)` | kept; now `fields=[brand, booth]` via the booth prompt |
| `fetch_page`, `clear_results` | unchanged |

The system prompt describes Booth Radar as a general public-web data assistant and
gives the strategy: `find_feeds` first on a new site, `read_feed` when a feed holds the
records, otherwise `map_site` / `fetch_page` to find pages and `extract`. Field names are
short human-readable labels taken from the user's request (`Product Name`, `Price`).

## Alternatives Considered

- **Separate `columns` field on the conversation:** explicit, but needs server, client
  and protocol plumbing for information already present in the rows.
- **Deterministic feed→field mapping only (one LLM call per site):** fastest, but cannot
  derive values embedded in text; planned as a recipe optimisation.
- **Generic JSON API capture via the browser (`capture_api`):** broader coverage for
  sites without WordPress/Shopify; deferred to the navigation-tools phase.

## Consequences

- Any-field scraping works for page extraction and for WordPress/Shopify sites; feed
  sites return full catalogs in seconds plus a few parallel LLM batches.
- Sites without a known feed still use browser + LLM per page (now with JSON-LD hints).
- Large tables are rendered in full by the client; very wide/long tables may need
  pagination later.
- Extra outbound requests to `wp-json` / `products.json` on first contact with a site.
