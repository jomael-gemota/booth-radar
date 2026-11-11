# Browser Sessions and Navigation Tools

**Date:** 2026-11-12
**Status:** accepted
**Author:** collaborative

## Context

Phase 3 of [general web scraping via MCP](2026-11-07-general-web-scraping-mcp.md)
(§2 tool table, §5 faster navigation). Phases 1, 2 and 4 are in place
([custom fields and site feeds](2026-11-10-custom-fields-and-site-feeds.md),
[API capture and recipes](2026-11-11-api-capture-and-recipes.md)); `map_site` from
phase 3 already exists.

What still blocks users:

1. **No interaction.** Data behind a tab, filter, search form, "Load more" button,
   infinite scroll or a JavaScript "Next" button can't be reached; `fetch_page` and
   `extract` only load a URL.
2. **Heavy browser use.** `scraper.py` runs Playwright's sync API on
   `SCRAPE_CONCURRENCY` threads, each with its own Chromium (~4 browsers), and every
   render starts a fresh context, so cookie and consent banners reappear on every page.
3. **Slow waits.** Every render waits for network idle (up to
   `SCRAPE_IDLE_TIMEOUT_MS`, 8 s); analytics and ad requests keep pages busy.
4. **Politeness gaps.** No per-site request cap (an `extract` of 40 URLs can hit one
   site 6 at a time) and `robots.txt` is only checked for feeds and sitemaps.
5. **Local network reachable.** `fetch_page` would load `http://127.0.0.1:8642` (Hermes)
   or other private addresses if asked; navigation tools make this easier to trigger.

## Decision

### 1. Browser service (`worker/browser.py`) on async Playwright

- One asyncio event loop on a dedicated thread runs **one Chromium**. Any worker
  thread (agent loop, tool bridge, extraction pool) calls it through
  `asyncio.run_coroutine_threadsafe`, so callers stay synchronous and nothing else in
  the worker changes shape. The browser is relaunched if it crashes.
- **Renders** (`fetch_page`, `extract`, `capture_api`) open a tab in a shared context
  per site (cookies and consent kept), close the tab afterwards, and close idle site
  contexts after 10 minutes. `SCRAPE_CONCURRENCY` becomes the number of tabs rendering
  at once (default **6**).
- **Sessions** (navigation tools, §3) get their own context per turn, so one user's
  clicks and logins never leak into another turn or into the shared render contexts.
  A turn may hold up to 3 sessions; they close when the turn ends or is cancelled.
- Playwright stays pinned at 1.45.1 (its async API is the same package), so the VM
  needs no reinstall.

### 2. Faster, lighter page loads

- **Tracker blocking:** besides images, media and fonts, requests to common analytics,
  ad and session-recording hosts (Google Analytics/Tag Manager, DoubleClick, Facebook,
  Hotjar, Clarity, Segment, TikTok, LinkedIn insight and similar; a short built-in list)
  are aborted.
- **Smarter waits:** after `domcontentloaded`, wait for a known content selector when
  there is one (a page recipe's item selector), otherwise for network idle capped at
  **3 s** for renders (`SCRAPE_IDLE_TIMEOUT_MS`, default lowered from 8000 to 3000).
  Blocking trackers makes idle arrive sooner, so the lower cap loses little content.

### 3. Navigation tools

Inspired by Microsoft's Playwright MCP: the page is described as a numbered list of
interactive elements, and actions refer to those numbers.

| Tool | Purpose |
|------|---------|
| `navigate(url, session?)` | Open a URL in a session (new one if omitted; ids `s1`, `s2`…). Returns the snapshot. |
| `snapshot(session, offset?)` | Re-read the current page. |
| `click(session, ref)` | Click an element; waits for navigation or the page to settle; follows a new tab if one opens. Returns the new snapshot. |
| `fill(session, ref, text, submit?)` | Type into a field; `submit=true` presses Enter. |
| `select(session, ref, value)` | Choose an option in a dropdown (by label or value). |
| `scroll_until_end(session, max_rounds?)` | Scroll until the page stops growing (default 10 rounds) for infinite scroll or "Load more" lists; reports how much was loaded. |
| `paginate(session, next_ref, fields, max_pages?, …)` | Click "Next" up to `max_pages` (default 10), extracting `fields` from every page into the results table (same extraction and recipe path as `extract`). Stops when the button disappears or the page stops changing. |
| `extract(..., session?)` | Existing tool; with `session` instead of `urls`, extracts from the session's current page (after clicks, filters or scrolling). |

**Snapshot format** (kept within `AGENT_PAGE_CHARS`):

```
Session s1 · https://example.com/boots?sort=price · "Work Boots | Example"
Elements:
  e1 link "Men's Boots" → /mens/
  e2 button "Load more"
  e3 textbox "Search products" = ""
  e4 combobox "Sort by" = "Featured" (options: Featured, Price low–high, Newest)
--- TEXT (12,431 chars, showing 0–6,000; pass offset for more) ---
...
```

- Elements are found by a script in the page: visible links, buttons, inputs,
  selects, textareas and elements with button/link/tab/checkbox roles or click
  handlers, labelled by their accessible text (aria-label, label, placeholder, text).
  Each gets a `data-br-ref` attribute, so a ref stays valid until the page changes;
  stale refs return a "take a new snapshot" error. Up to 150 elements are listed,
  most useful first (forms and buttons before repeated item links).
- Snapshots are not cached; renders stay cached as today.

### 4. Politeness and safety

- **Per-site cap:** at most `DOMAIN_CONCURRENCY` (default **4**) page requests to one
  host at a time, for both plain HTTP and browser loads, across all turns. Feeds keep
  their own cap of 4.
- **`robots.txt` for pages:** page loads and navigations (not the page's own
  sub-requests) are checked against `robots.txt` for the `BoothRadar` agent, sharing
  the parser cache in `site_feeds.py`. `RESPECT_ROBOTS` (default `true`) can turn this
  off if a site the team relies on disallows its directory pages.
- **No private addresses:** `fetch_page`, `extract`, `capture_api`, feeds and
  navigation refuse URLs whose host resolves to loopback, private, link-local or
  reserved addresses, and the browser aborts any request to them, including redirects.

### 5. Prompt and MCP server

- `_TOOL_DOCS` and `mcp_server.py` gain the navigation tools; the strategy section
  adds: "if the data only appears after clicking, filtering, searching or scrolling,
  use navigate and the interaction tools, then extract with the session or paginate".
- Sessions are passed per turn through the existing tool runner, so inline and MCP
  modes both work.

### Rollout inside this phase

1. Browser service, tracker blocking, waits, per-site cap, robots for pages and the
   private-address guard (no new tools; existing tools switch to the service).
2. Navigation tools, `extract(session)` and `paginate`.

Each step passes `npm run check`, `py_compile`, and live tests on public practice
sites (quotes.toscrape.com: JavaScript page, "Next" pagination, infinite scroll and a
search form with dropdowns).

## Alternatives Considered

- **Microsoft's Playwright MCP server as a second MCP server in Hermes:** ready-made,
  but it owns its own browser, so results wouldn't reach the results table or reuse
  the cache and recipes, and each click costs a Hermes round-trip with a large
  accessibility tree. Its ref-based design is borrowed instead.
- **Playwright's built-in `aria_snapshot()`:** needs Playwright 1.49+, so a pin bump
  and browser reinstall on the VM, and its output is larger than the element list
  above.
- **Keep the sync API with one browser per thread:** no migration work, but sessions
  can't move between bridge threads (sync Playwright objects are bound to the thread
  that created them), so multi-step navigation across tool calls isn't possible.
- **Per-turn contexts for renders too:** stricter isolation, but cookie and consent
  state would be rebuilt on every turn. Renders only read public pages, so a shared
  per-site context is enough; sessions keep strict isolation.
- **`DOMAIN_CONCURRENCY` 2** (as in the original plan): gentler, but halves `extract`
  speed on one site. 4 is still modest; it can be lowered in `.env`.

## Consequences

- Data behind clicks, filters, forms, infinite scroll and JavaScript pagination
  becomes reachable; each step is one tool call with a compact snapshot.
- One Chromium instead of ~4 lowers memory on the VM; tabs are cheap.
- The async migration touches the core of `scraper.py`; a browser-service bug would
  affect every render. Mitigated by the crash relaunch and the step-1/step-2 split.
- `robots.txt` checks on pages may block sites that worked before; the message says
  so, and `RESPECT_ROBOTS=false` restores the old behaviour.
- Interaction depends on the model choosing sensible refs; long click sequences cost
  several Hermes round-trips, so `paginate` and `scroll_until_end` do the repetitive
  parts in one call.
- New env vars: `DOMAIN_CONCURRENCY`, `RESPECT_ROBOTS`; changed defaults:
  `SCRAPE_CONCURRENCY` 6 (tabs), `SCRAPE_IDLE_TIMEOUT_MS` 3000.
