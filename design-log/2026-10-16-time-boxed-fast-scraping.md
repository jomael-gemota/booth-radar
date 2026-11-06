# Time-Boxed Fast Scraping

**Date:** 2026-10-16
**Status:** proposed
**Author:** collaborative

## Context

After [scrape cache and concurrency](2026-10-15-scrape-cache-and-concurrency.md) shipped,
scrapes still take 5–26 minutes. Persisted turns in MongoDB show why:

| Turn | Duration | Hermes native tool steps | Rows in results table |
|------|----------|--------------------------|-----------------------|
| Atlanta Market directory | 1,544 s | 46 | 0 |
| Inspired Home Show "clean + contain" | 299 s | 16 | 0 |

In both turns Hermes never called Booth Radar's `fetch_page` / `extract_booths`. It used
its own Hermes Agent toolsets (`browser`, `terminal`, `code_execution`, skill
`public-web-data-collection`), working one step at a time: each step is a full LLM
round-trip plus a browser session, and it fetched 1,606 exhibitor profiles in slices.
So the cache, warm Chromium pool and parallel extraction were never used, and the
results table stayed empty (output arrived only as an attached CSV).

A second slowdown sits inside our own pipeline: `extractor.extract()` sends every page to
`hermes-agent` too, so each page extraction starts a full agent run (which may itself
call tools) instead of a single fast completion.

Hermes Agent cannot disable its toolsets per request (request-local overrides were
removed upstream, NousResearch/hermes-agent#59854); only `platform_toolsets.api_server`
or `agent.disabled_toolsets` in `~/.hermes/config.yaml` on the VM can.

## Decision (proposed)

1. **Stop Hermes from using its own scraping tools for Booth Radar.** Either
   - (a) VM config: restrict `platform_toolsets.api_server` so the API surface has no
     `browser`, `terminal`, `code_execution`, `web`, `delegation` tools, or
   - (b) worker config: point the agent loop at a plain OpenAI-compatible model
     (`AGENT_BASE_URL` / `AGENT_MODEL`, e.g. `gpt-4o-mini`) that has no native tools.
2. **Fast extraction model.** `extract()` uses its own `EXTRACT_BASE_URL` /
   `EXTRACT_MODEL` (default: a plain chat model, not `hermes-agent`) so a page is one
   completion, not an agent run.
3. **Turn time budget** (`TURN_BUDGET_S`, default 150 s). The agent loop tracks elapsed
   time; `extract_booths` stops starting new pages once the budget is near, returns what
   it has (rows already stream into the results table) and lists unprocessed URLs. When
   the budget is spent, Hermes is told to wrap up and the turn ends with a summary and an
   offer to continue with the remaining pages.
4. **More parallelism.** Split scraping and LLM concurrency (`SCRAPE_CONCURRENCY` 6,
   `EXTRACT_CONCURRENCY` 8); long pages are split into chunks extracted in parallel
   instead of truncated at `MAX_PAGE_CHARS`.

## Alternatives Considered

- **Keep Hermes native tools, prompt it to be quick:** unreliable (prompt rules were
  already ignored, see [guaranteed preamble](2026-10-16-guaranteed-preamble.md)) and it
  never fills the results table.
- **Hard-cancel native Hermes runs at 3 minutes:** returns nothing — partial data lives
  in Hermes' checkpoint files, not in Booth Radar.

## Consequences

- Most turns finish within ~2–3 minutes with rows visible in the table as they arrive.
- Very large directories (e.g. Atlanta Market's 4,598 listings) are not completed in one
  turn; the user gets partial results at the budget and says "continue" for the rest.
- Sites whose booth numbers are only reachable through hidden APIs (Atlanta profiles)
  may yield `N/A` booths, which Hermes' native tools could recover given 20+ minutes.
- Option (a) needs a change on the Ubuntu VM; option (b) adds OpenAI API cost.
