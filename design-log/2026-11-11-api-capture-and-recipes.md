# API Capture and Learned Recipes

**Date:** 2026-11-11
**Status:** accepted
**Author:** collaborative

## Context

Completes phase 4 (extraction ladder) of
[general web scraping via MCP](2026-11-07-general-web-scraping-mcp.md). Phase 1 was
verified on the VM on 2026-11-11: Hermes calls Booth Radar's MCP tools natively, and a
Thorogood request filled 101 rows from the WordPress feed plus product pages.

[Custom fields and site feeds](2026-11-10-custom-fields-and-site-feeds.md) covers
WordPress and Shopify sites and JSON-LD hints. What is still slow or lossy:

1. **Sites without a known feed** whose listings load through JavaScript
   (search pages, "load more" grids, React/Vue storefronts) are rendered page by page,
   with one LLM call per page.
2. **Every page and every feed batch costs an LLM call**, even when hundreds of pages
   share one layout or the feed already has the requested values as plain keys.
3. **Long pages are cut** at `MAX_PAGE_CHARS` (40,000), so rows at the bottom of long
   directory pages are lost.
4. In the VM test, Hermes extracted ten listing pages and read the sitemap twice
   before calling `read_feed`, which already held the whole catalog.

## Decision

### 1. `capture_api(url, scroll?)` — find a site's own JSON endpoints

- Renders the page in Chromium and records `xhr` / `fetch` responses with a JSON body
  (method, URL, request headers and POST body, status, size).
- For each response, finds the largest list of objects (up to four levels deep, e.g.
  `data.products.items`), plus any total-count key (`total`, `totalCount`, `nbHits`,
  `count`) and pagination parameters in the query string or JSON body (`page`, `p`,
  `pageNumber`, `offset`, `start`, `skip`, `from`, `limit`, `per_page`, `size`).
- With `scroll=true`, scrolls to the bottom a few times first, so infinite-scroll grids
  fire their "next page" requests.
- Endpoints that return records are **registered as feeds** of the site (ids `api:1`,
  `api:2`, …) in the same cache as `find_feeds`. The tool returns their descriptions:
  record path, record count, total, sample keys, pagination.
- No new read tool: `read_feed(url, feed="api:1", fields…)` replays the request with
  `requests` (same method, body and headers, minus hop-by-hop headers), follows
  pagination by stepping the page number (+1) or offset (+records per page) until a page
  is empty, repeats the previous one, reaches the total or hits `MAX_FEED_RECORDS`, and
  then maps records to the user's fields exactly like WordPress/Shopify feeds.
- Cursor-based or signed endpoints that can't be stepped return only the captured
  page, and the tool says so.
- Replays honour `robots.txt` like other feed requests. Captured headers (which can
  include cookies or public API keys from the page) are kept only in worker memory for
  `PAGE_CACHE_TTL` and are never logged or returned to the agent.

### 2. Learned recipes — one LLM call per layout, not per page

A recipe is learned from the LLM's own output, verified, then reused without the LLM.

**Page recipes** (`extract` with 4 or more URLs on the same site):

1. The first page is extracted by the LLM as today.
2. One extra LLM call proposes CSS selectors for that page: an optional item container
   and, per field, a selector relative to it, optionally with an attribute
   (`a.title@href`).
3. The recipe is applied to the same page with BeautifulSoup (`soup.select`, already
   installed). It is **accepted only if it reproduces at least 90% of the LLM's
   non-empty cells** (whitespace- and case-normalised).
4. The remaining pages are fetched in parallel and read with the recipe. A page falls
   back to the LLM when the recipe finds no rows or leaves more than half the cells
   empty; three fallbacks in a row drop the recipe for the rest of the call.

**Feed recipes** (`read_feed` with more than one batch and no extra `instructions`):
the first batch is mapped by the LLM as today. If it returned one row per record, each
field is matched in code against the record keys, picking the key whose values equal
the LLM's values (normalised) for at least 90% of the non-empty cells. If every field
has such a key, the remaining batches are mapped in code with no further LLM calls.
Fields that need derivation (e.g. height inside a title) have no matching key, so the
feed keeps the LLM path. User `instructions` may filter records, which a key mapping
can't do, so they also keep the LLM path.

**Storage and scope:**

- Recipes are keyed by domain, field list and URL shape (path depth plus first path
  segment), so product pages and listing pages of one site get separate recipes.
- Saved to `RECIPES_PATH` (default `worker/data/recipes.json`, git-ignored), so they
  survive restarts. Each records when it was learned and how many pages it served.
- A recipe that fails verification on a later run is replaced. `refresh=true` on
  `extract` / `read_feed` ignores saved recipes.
- `save_recipe` / `get_recipe` from the original tool table are **not added**; learning
  is automatic and the agent doesn't need to write selectors itself.
- `RECIPES_ENABLED` (default `true`) turns the whole mechanism off.

### 3. Long pages are chunked, not cut

Page text longer than `MAX_PAGE_CHARS` is split on line boundaries into chunks of that
size with a small overlap, extracted in parallel (within `EXTRACT_CONCURRENCY`), and the
rows merged through the results table's de-duplication.

### 4. Strategy prompt

The guidelines state the order more firmly: on a new site call `find_feeds` first; if a
feed holds the records, use `read_feed` and only `extract` pages for fields the feed
lacks; never extract listing pages to collect URLs that a feed or sitemap already gives;
if there is no feed and the listing loads by JavaScript, try `capture_api` before
rendering pages one by one.

### Tools after this change

| Tool | Change |
|------|--------|
| `capture_api(url, scroll?)` | new |
| `read_feed(...)` | also reads `api:N` feeds; uses feed recipes |
| `extract(...)` | uses page recipes; chunks long pages |
| `find_feeds`, `map_site`, `fetch_page`, `extract_booths`, `clear_results` | unchanged |

## Alternatives Considered

- **`extruct` for microdata/RDFa:** JSON-LD already covers nearly all product and event
  pages; microdata alone rarely carries data that isn't also in the visible text. Not
  worth a new dependency now.
- **`selectolax` instead of BeautifulSoup:** faster parsing, but network time dominates
  and BeautifulSoup is already installed.
- **Agent-written recipes (`save_recipe` / `get_recipe`):** costs Hermes round-trips and
  depends on the model writing correct selectors; automatic learn-and-verify is cheaper
  and safer.
- **`trafilatura` main-content extraction:** built for articles; it drops repeated list
  items, which are what directory and catalog pages consist of.
- **Recipes stored in MongoDB:** the worker has no database connection today; a local
  file is enough for one worker.
- **Replaying APIs through the browser instead of `requests`:** handles more anti-bot
  setups but is far slower; kept as a later option if many sites reject plain replays.

## Consequences

- Sites whose listings come from a JSON API can be read in seconds without a feed
  plugin, and multi-page extraction drops to about two LLM calls per layout.
- A wrong recipe could fill wrong values; the 90% verification, the per-page fallback
  and `refresh=true` limit the damage, but spot checks are still advisable on new sites.
- API replay will fail on sites with signed requests, strict bot protection or
  cursor-only pagination; those fall back to page rendering.
- `capture_api` uses one Chromium page per call; nothing changes for the async
  Playwright work in phase 3.
- New files on the VM: `worker/data/recipes.json`. New env vars: `RECIPES_ENABLED`,
  `RECIPES_PATH`.
