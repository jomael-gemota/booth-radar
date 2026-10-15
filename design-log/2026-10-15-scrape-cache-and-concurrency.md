# Scrape Cache, Warm Browser and Parallel Extraction

**Date:** 2026-10-15
**Status:** accepted
**Author:** collaborative

## Context

Users reported that the first scrape of a show site takes a while, and that any
follow-up question about the same site takes just as long again. The
[conversational agent](2026-10-13-conversational-agent.md) entry already noted that
multi-page requests are slow. The causes:

- The worker is stateless and only receives chat text from the server. Scraped page
  content is discarded at the end of each turn, so follow-ups re-scrape the site.
- `_fetch_playwright` launched a new Chromium for every URL and waited up to 15 s for
  network idle.
- `fetch_page` followed by `extract_booths` on the same URL scraped it twice.
- `extract_booths` processed URLs strictly one after another (scrape, then LLM).

## Decision

### In-memory cache (`worker/page_cache.py`)

A thread-safe TTL + LRU cache with two stores:

- **Pages:** fragment-less URL → `(text, links)`. `scrape()` and `snapshot()` both read
  from it, so `fetch_page` and `extract_booths` share one load per URL.
- **Extractions:** `(url, sha1(page text))` → rows. Re-extracting an unchanged page skips
  the LLM call.

`PAGE_CACHE_TTL` (default 3600 s) and `PAGE_CACHE_MAX` (default 300 entries per store).
The cache is shared across conversations (public trade-show pages) and is lost when the
worker restarts.

### Warm browser pool (`worker/scraper.py`)

Playwright's sync API is bound to the thread that created it, so all scraping runs on one
long-lived `ThreadPoolExecutor(SCRAPE_CONCURRENCY)`. Each pool thread keeps its own
Chromium in `threading.local`; every page gets a fresh browser context (no cookies or
state carried between pages). A crashed browser is relaunched once and the page retried.
Images, media and fonts are blocked, and the network-idle wait is configurable via
`SCRAPE_IDLE_TIMEOUT_MS` (default 8000 ms, was 15000 ms).

### Parallel extraction (`worker/agent.py`)

`extract_booths` submits scrape + extract for each URL to the pool (up to
`SCRAPE_CONCURRENCY`, default 4, at once). Rows are merged in the original URL order so
de-duplication and table order match the sequential behaviour. A status line is sent as
each page finishes.

### Agent tools

- `fetch_page` gains `offset` (read further into a cached page) and `refresh` (bypass cache).
- `extract_booths` gains `refresh`.
- The system prompt tells Hermes that recently loaded pages are cached and instant, so it
  should call `fetch_page` again to answer follow-ups, and use `refresh` only when the
  user asks for fresh data.

## Alternatives Considered

- **Persist page snapshots in MongoDB per conversation:** survives restarts and works days
  later, but adds storage, a server/worker protocol change and breaks the stateless-worker
  design. Deferred.
- **Keep extraction sequential:** gentlest on target sites and the Hermes server, but
  multi-page directories (A–Z) stay slow.
- **One shared browser behind a lock:** saves memory but serialises all scraping.

## Consequences

- Follow-up questions about a page loaded within the TTL return in milliseconds.
- Memory use grows with `SCRAPE_CONCURRENCY` (one Chromium per pool thread) and the cache
  size; both are configurable.
- Up to 4 concurrent requests hit the target site and the Hermes server during
  multi-page extraction.
- Cached pages can be up to an hour stale; users can ask for a refresh.
- Extra page details (hall, category, …) are still not stored in the results table;
  Hermes reads them from the cached page instead.
