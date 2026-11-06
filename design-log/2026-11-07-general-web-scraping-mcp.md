# General Web Scraping via a Booth Radar MCP Server

**Date:** 2026-11-07
**Status:** accepted
**Author:** collaborative

## Context

Booth Radar should grow from "brands + booth numbers from exhibitor directories" into a
general public-web scraper: arbitrary sites, product/price comparison (including Google
Shopping sellers and prices), and fast multi-page navigation (clicking, pagination,
crawling). Constraint from the user: **self-hosted / free tooling only** — no paid
scraping, SERP or unlocker APIs.

Relevant history:

- [Architecture](2026-10-13-booth-radar-architecture.md): Railway server relays chat over
  WebSocket to a Python worker on the Ubuntu VM, which runs Playwright and calls Hermes.
- [Scrape cache and concurrency](2026-10-15-scrape-cache-and-concurrency.md): warm
  Chromium pool, TTL cache, parallel extraction.
- [Time-boxed fast scraping](2026-10-16-time-boxed-fast-scraping.md) (still `proposed`):
  Hermes ignores Booth Radar's `<tool>` text protocol and uses its own `browser`,
  `terminal` and `code_execution` toolsets (46 steps / 25 min, empty results table), and
  `extractor.extract()` starts a full Hermes agent run per page.

Current limits that block the upgrade:

1. **Two competing tool layers.** Booth Radar's tools (`fetch_page`, `extract_booths`,
   `clear_results`) are offered as a prompt-level `<tool>{...}</tool>` protocol inside
   `agent.py`, while Hermes has native function-calling tools it prefers.
2. **Fixed row shape.** `BoothEntry { brand, booth }` is hard-coded in `server/src/relay.ts`,
   the CSV builder in `server/src/routes/chat.ts`, and the results table in
   `client/src/components/ChatBox.tsx`.
3. **LLM per page.** Every page goes through an LLM even when the site exposes JSON APIs,
   schema.org data, or a repeating DOM pattern.
4. **No interaction tools.** The agent can read pages but cannot click, fill, scroll or
   paginate.
5. **Work bounded by one chat turn.** Large directories cannot finish inside a turn.

Hermes Agent supports MCP servers natively (`mcp_servers` in `~/.hermes/config.yaml`;
each server becomes an `mcp-<name>` toolset with `tools.include` / `tools.exclude`), and
skills in `~/.hermes/skills/<name>/SKILL.md`.

## Decision (proposed)

### 1. Booth Radar MCP server — one tool layer

Move the scraping capabilities out of the `<tool>` prompt protocol into a stdio MCP
server, `worker/mcp_server.py` (official `mcp` Python SDK), registered on the VM:

```yaml
# ~/.hermes/config.yaml
mcp_servers:
  booth-radar:
    command: /path/to/booth-radar/worker/.venv/bin/python
    args: ["-m", "mcp_server"]
    timeout: 300
platform_toolsets:
  api_server: [mcp-booth-radar, skills, clarify]
```

Restricting `api_server` to the Booth Radar toolset resolves option (a) of the
time-boxed entry: Hermes can no longer fall back to `browser_exec` / `terminal`. Hermes
then calls our tools through native function calling, and `agent.py` drops `_TOOL_RE`,
`_ToolBlockFilter` and the tool-format rules in the system prompt.

Because the worker process and the MCP server process are separate, rows and status
events must reach the chat. The MCP server is a thin, stateless proxy: each tool call is
forwarded over HTTP to a **tool bridge** inside the worker (`127.0.0.1:TOOL_BRIDGE_PORT`,
shared-secret `TOOL_BRIDGE_TOKEN`), which runs the tool inside the active turn, so the
page cache, browser pool, results list and `status` / `results` events stay in the
worker. The turn id is passed as a `turn` tool argument given in the system prompt; if
it is missing and exactly one turn is active, that turn is used.

The worker switches modes with `AGENT_TOOL_MODE` (`inline` = current `<tool>` protocol,
default until the VM is configured; `mcp` = native Hermes tool calls via the bridge).

**Open question:** if Hermes' API server cannot stream MCP tool progress back to the
worker reliably, the fallback is to keep the tools in-process and switch `agent.py` from
the `<tool>` text protocol to OpenAI-style `tools=[...]` function calling against a
plain model (option (b) of the time-boxed entry).

### 2. Tool set

| Tool | Purpose |
|------|---------|
| `navigate(url, session?)` | Open a page in a persistent per-turn browser session; returns title, URL and a compact accessibility snapshot with element refs (`e12`). |
| `snapshot(session, offset?)` | Re-read the current page as a ref-annotated accessibility tree (much smaller than full text). |
| `click(session, ref)` / `fill(session, ref, text)` / `select(session, ref, value)` | Interact by ref, Playwright-MCP style. |
| `scroll_until_end(session, max_rounds)` | Infinite-scroll loader. |
| `paginate(session, next_ref | url_pattern, max_pages)` | Follow "Next" or `?page=N` patterns, returning page URLs. |
| `map_site(url, include?, exclude?)` | Discover URLs from `sitemap.xml`, `robots.txt` sitemaps and in-page links. |
| `capture_api(session, action?)` | Record JSON XHR/fetch responses while loading or interacting; returns endpoints and sample payloads so later pages can be fetched directly. |
| `extract(urls, schema, recipe?)` | Bulk extraction into rows matching `schema` (see §3/§4); streams rows to the results table. |
| `shopping_search(query, country?, max_results?)` | Product offers (§6). |
| `save_recipe(domain, recipe)` / `get_recipe(domain)` | Persist per-site selectors and pagination rules. |
| `set_columns(columns)` / `clear_results()` | Manage the conversation's results schema. |

`fetch_page` and `extract_booths` remain as thin aliases during migration (booth
extraction becomes `extract(urls, schema={brand, booth})`).

### 3. Extraction ladder (fastest first)

For each URL, `extract` tries in order and stops at the first that yields rows:

1. **Recipe hit** — a saved per-domain recipe (CSS selectors + field mapping + pagination)
   applied with `selectolax`. No LLM.
2. **Captured JSON API** — call the endpoint discovered by `capture_api` directly with
   `httpx` (HTTP/2, connection reuse), map fields per recipe. No browser, no LLM.
3. **Structured data** — `extruct` for JSON-LD / microdata / RDFa (`Product`, `Offer`,
   `Event`, `Organization`). No LLM.
4. **LLM extraction** — page text (or main content via `trafilatura`) chunked and sent to
   the extraction model with the target JSON schema. On the first page of a new domain,
   the LLM is also asked to propose a recipe; if the recipe reproduces the LLM's rows on
   that page, it is saved and used for the remaining pages.

Extraction model: `EXTRACT_BASE_URL` / `EXTRACT_MODEL`, a single chat completion — not
the `hermes-agent` endpoint. To stay free, default to a local OpenAI-compatible server on
the VM (e.g. Ollama) if the VM can run one; otherwise the existing Hermes model via a
plain completion endpoint. Recipes keep LLM calls to roughly one or two per site.

### 4. Generic results schema

- Conversation `results` becomes `{ columns: Column[], rows: Record<string, string>[] }`
  with `Column { key, label }`. Existing `{brand, booth}` data is read as
  `columns = [brand, booth]` (no migration needed; a missing `columns` implies the legacy
  shape).
- `relay.ts`, the CSV builder and `ChatBox.tsx` render columns dynamically.
- Rows carry a hidden `_source` URL for de-duplication and traceability; de-dup key is the
  schema's declared key fields (default: all fields).
- Greeting and sign-in copy become "scrape and compare data from public websites".

### 5. Faster navigation and fetching

- **HTTP first:** `httpx` fetch; render with Playwright only if the HTML lacks the
  target content or is a JS shell.
- **Async Playwright:** one Chromium, many pages, replacing one browser per pool thread;
  per-domain browser contexts reused within a turn (cookies/consent kept), still isolated
  between turns.
- **Smarter waits:** `wait_until="domcontentloaded"` plus wait-for-selector from the
  recipe, instead of network-idle; network-idle remains the fallback with the existing
  `SCRAPE_IDLE_TIMEOUT_MS`.
- **Block more:** add third-party analytics/ads/trackers to the existing image/media/font
  blocking.
- **Politeness:** respect `robots.txt`, per-domain concurrency cap
  (`DOMAIN_CONCURRENCY`, default 2) and minimum delay, identifiable user agent.

### 6. Google Shopping (self-hosted)

Paid SERP APIs are excluded, so `shopping_search` is a provider interface with a
self-hosted default:

- **Default provider `google_browser`:** low-volume Playwright search on Google Shopping,
  parsing listing title, price, seller, shipping, rating and product URL; follows the
  product page for the full seller list when requested. Results cached for
  `SHOPPING_CACHE_TTL` (default 6 h); hard cap `SHOPPING_MAX_QUERIES_PER_HOUR`
  (default 20).
- **Fallback provider `merchant_sites`:** search the merchants directly (site search
  pages + schema.org `Offer` data), using the extraction ladder.
- Provider is swappable via `SHOPPING_PROVIDER` so a paid API can be added later without
  changing the tool.

Risks are accepted explicitly (see Consequences); the tool surfaces CAPTCHA/blocked
states to the user instead of retrying aggressively. No CAPTCHA solving or fingerprint
evasion.

### 7. Background crawl jobs

Crawls over `TURN_BUDGET_S` (default 150 s, from the time-boxed entry) continue as a
background job:

- A `jobs` collection in MongoDB holds the frontier (pending URLs), done count, recipe and
  schema. The worker processes it outside the chat turn and keeps streaming rows into the
  conversation's results.
- The chat turn ends with a summary ("1,200 of 4,598 done, continuing in the background").
- UI shows a progress chip with pause/cancel; the cancel path reuses
  [cancel turn](2026-10-16-cancel-turn.md) plumbing.

### 8. Hermes skills (on the VM)

Short skills in `~/.hermes/skills/` replace long system-prompt rules:

- `directory-crawl` — strategy order: `map_site` → `capture_api` → structured data →
  rendered page; budget handling; when to offer "continue".
- `site-recipe` — how to derive and verify a recipe, then `save_recipe`.
- `price-comparison` — query formulation, currency normalisation, seller de-duplication,
  flagging cheapest/outliers, and which columns to set.

Skills are kept in the repo under `worker/hermes-skills/` and symlinked or copied to the
VM so they are versioned.

### 9. Rollout phases

1. MCP server with existing tools (`fetch_page`, `extract_booths`) + Hermes toolset
   lockdown + plain extraction model. Verifies the architecture; fixes the 25-minute turns.
2. Generic results schema (server, client, CSV).
3. Navigation tools (`navigate`, `snapshot`, `click`, `fill`, `scroll_until_end`,
   `paginate`, `map_site`) on async Playwright.
4. Extraction ladder: `capture_api`, `extruct`, recipes.
5. `shopping_search` with both free providers.
6. Background crawl jobs.
7. Hermes skills.

Each phase ships independently; `npm run check` and the worker's `test_local.py` must
pass at each step.

## Alternatives Considered

- **Keep the `<tool>` text protocol and prompt harder:** already failed (Hermes ignores
  it; see the time-boxed entry).
- **Use Hermes' native `browser` toolset for everything:** flexible, but one LLM
  round-trip per action, no shared cache, no results-table streaming; this is the cause
  of the 25-minute turns.
- **Off-the-shelf MCP servers only (Microsoft Playwright MCP, Crawl4AI, self-hosted
  Firecrawl):** all free, but each owns its own browser and output format, so Booth
  Radar's cache, results table, time budget and recipes would be bypassed. Their designs
  are borrowed instead (ref-based snapshots from Playwright MCP, schema extraction from
  Crawl4AI/Firecrawl). Crawl4AI remains an option to embed as a library in step 4 if
  building the ladder in-house proves slow.
- **Paid SERP / unlocker APIs (SerpApi, Bright Data, Apify, Oxylabs):** far more reliable
  for Google Shopping; excluded by the free-only constraint, but the provider interface
  leaves room to add one.
- **Scrapy as the crawl engine:** mature and fast for static sites, but a second
  framework beside Playwright and harder to drive interactively from an agent.

## Consequences

- One tool layer: Hermes uses Booth Radar's tools through native function calling, and
  rows always reach the results table.
- Most multi-page sites become one or two LLM calls plus fast HTTP/DOM work, instead of
  one LLM call per page.
- Requires VM changes (`~/.hermes/config.yaml`, skills folder) alongside code deploys;
  these must be documented in the README worker setup.
- **Google Shopping is the fragile part.** Automated querying is against Google's terms,
  is rate-limited and CAPTCHA-gated, and its markup changes often. Expect breakage and
  low throughput; keep volume low and treat it as best-effort. A paid provider is the fix
  if it becomes business-critical.
- Higher load on target sites from parallel crawling, mitigated by `robots.txt`,
  per-domain caps and delays.
- The legacy `{brand, booth}` results shape must keep working for existing conversations.
- A local extraction model (if used) needs RAM/GPU headroom on the VM next to Chromium.
- Supersedes [time-boxed fast scraping](2026-10-16-time-boxed-fast-scraping.md): its
  turn budget and plain extraction model are folded into §1, §3 and §7.
