# Booth Radar Architecture

**Date:** 2026-10-13
**Status:** accepted
**Author:** collaborative

## Context

Booth Radar is an internal tool that lets a user paste an exhibitor website URL into a
chat interface and receive the list of exhibiting brands with their booth locations as
a downloadable CSV. The project reuses the same three-tier pattern proven in doc-tidy
(see [doc-tidy README](https://github.com/jomael-gemota/doc-tidy#readme)):

- The heavy processing (scraping + LLM extraction) lives on the Ubuntu machine that
  already runs the Hermes Agent, so no extra cloud compute is needed.
- The browser-facing server runs cheaply on Railway and has no LLM dependency.
- MongoDB Atlas is the shared state store, just as in doc-tidy.

## Decision

### Three-tier layout

```
Browser (Vite + React)
  │  POST /api/scrape       ← submit a URL, get back a jobId
  │  GET  /api/stream/:id   ← SSE: live tokens from the worker
  │  GET  /api/jobs/:id/export ← download CSV when done
  ▼
Express Server  (Railway)
  │  WebSocket /ws          ← persistent worker connection
  ▼
Python Worker  (Ubuntu)
  │  Playwright / requests  ← scrape the exhibitor page
  │  Hermes Agent API       ← OpenAI-compatible, local
  ▼
MongoDB Atlas  (jobs + results collections)
```

### Scraper strategy

Playwright (headless Chromium) is the primary scraper because many modern exhibitor
portals are JavaScript-rendered SPAs. For pages that are fully server-rendered,
`requests` + BeautifulSoup serves as a lightweight fallback. The worker tries
Playwright first; if the resulting text is below a minimum length threshold it
retries with requests + BeautifulSoup.

### LLM extraction

The worker sends the scraped page text to Hermes via the OpenAI-compatible streaming
chat completions API. The system prompt instructs the model to output a JSON array of
`{"brand": "…", "booth": "…"}` objects. All intermediate reasoning/thinking tokens
are streamed back to the browser as chat messages so the user sees live progress.
The final assistant message containing the JSON array is parsed and stored in MongoDB.

### Export

CSV is generated server-side from the stored `results` array on the job document and
served at `GET /api/jobs/:id/export`. The client triggers a browser download by
creating a temporary `<a>` tag pointing at that endpoint.

### Chat UX

Each SSE event from the server is appended as a new chat message bubble in the React
UI. There are three event types:
- `token` — appended to the current assistant bubble (streaming effect)
- `done` — marks completion, reveals the Export CSV button and results table
- `error` — shown as a red error bubble

## Alternatives Considered

- **Backend-only scraping without LLM:** A fixed CSS-selector scraper would be faster
  but would break on every exhibitor site with different markup. Using the LLM makes
  the extractor site-agnostic.
- **Direct LLM call from the browser (no worker):** Would expose the Hermes API key in
  the browser and require the Ubuntu machine to be publicly reachable; the WebSocket
  relay keeps the worker behind a single Railway server.
- **gRPC instead of WebSocket:** Adds complexity with no benefit at this scale.

## Consequences

- The Ubuntu worker machine must be online for the tool to function. `GET /api/health`
  exposes `{ workerConnected: true/false }` so the UI can warn the user when it is not.
- Playwright requires system dependencies (`chromium`, `playwright install chromium`).
- Large exhibitor pages (>40 000 chars) are truncated before being sent to the LLM to
  stay within context limits; a `MAX_PAGE_CHARS` env var controls this threshold.
- The Railway free tier sleeps after inactivity; a keep-alive ping from the worker's
  WebSocket reconnect loop mitigates cold-start delays.
