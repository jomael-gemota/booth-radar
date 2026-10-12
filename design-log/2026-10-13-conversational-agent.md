# Conversational Agent Chat

**Date:** 2026-10-13
**Status:** accepted
**Author:** collaborative

## Context

The first version ([booth-radar-architecture](2026-10-13-booth-radar-architecture.md))
treated the chat box as a URL form: the client rejected anything that was not a URL and
the worker ran one fixed pipeline (scrape one page → extract JSON). In production this
did not match the intended UX — users must be able to talk to Hermes like they talk to
the Hermes agent directly, and give it instructions such as "scrape every letter of the
Atlanta Apparel exhibitor directory".

Production also surfaced two worker bugs:

- `openai==1.37.0` passes `proxies=` to `httpx.Client`, which `httpx>=0.28` removed,
  so every LLM call crashed with `TypeError: ... unexpected keyword argument 'proxies'`.
- The systemd unit runs as root, so Playwright looked for browsers in
  `/root/.cache/ms-playwright`, where none were installed. JS-rendered directories then
  fell back to `requests` and returned ~200 characters.

This entry supersedes the "LLM extraction", "Chat UX" and per-job export parts of the
architecture entry. The three-tier layout (browser → Railway server → Ubuntu worker)
is unchanged.

## Decision

### Conversations instead of jobs

A `conversations` collection replaces `jobs`:

```
{ _id, createdAt, updatedAt,
  messages: [{ role: "user" | "assistant", content, at }],
  results:  [{ brand, booth }] }
```

The server is the only writer. The worker is stateless and no longer needs MongoDB.

### API

```
POST /api/chat                      { conversationId?, message } → { conversationId, turnId }
GET  /api/conversations/:id         → { id, messages, results }
GET  /api/stream/:turnId            SSE for one assistant turn
GET  /api/conversations/:id/export  CSV of the conversation's results table
```

Each user message starts a **turn**. The server sends the worker
`{ type: "chat", turnId, messages, results }` (last 30 messages) and relays worker frames
to the browser. Frames are buffered per turn and replayed to late SSE subscribers, so
tokens emitted before the browser's `EventSource` connects are not lost. If the worker
disconnects mid-turn, open turns get an `error` event instead of hanging.

SSE event types: `token` (assistant text delta), `status` (tool progress line),
`results` (full updated table), `done` (`{ reply, results }`), `error`.

### Agent loop (worker)

Hermes runs a text-protocol tool loop (max `AGENT_MAX_STEPS`, default 12). To use a tool
it emits `<tool>{"name": …, "args": …}</tool>`; the worker runs it and feeds back a
`<tool_result>` message. A plain-text answer with no tool block ends the turn. Tool
blocks are hidden from the streamed output.

Tools:

- `fetch_page {url}` — render the page (Playwright, requests fallback) and return
  visible text plus links, so Hermes can inspect a site and discover directory pages
  or pagination (e.g. `?char=A` … `?char=Z`).
- `extract_booths {urls}` — scrape each URL and run the existing JSON extractor; rows
  are merged (de-duplicated) into the conversation's results table.
- `clear_results {}` — empty the table.

A text protocol was chosen over native function calling because the Hermes Agent API
server runs its own toolsets and is not guaranteed to honour client-supplied `tools`.
The text protocol works with any OpenAI-compatible chat endpoint.

### Fixes

- Bump `openai` to a version compatible with current `httpx`.
- When Playwright fails and the fallback returns almost nothing, raise a clear error
  that names the Playwright failure instead of extracting from an empty page.
- Escape HTML in rendered chat bubbles — assistant text now includes scraped content.

## Alternatives Considered

- **Keep URL-only input, add canned replies:** Does not meet the requirement that users
  can give free-form instructions.
- **Proxy the chat straight to the Hermes Agent and let its own browser tools scrape:**
  Simplest, but results would come back as prose, with no reliable structured table for
  CSV export, and behaviour would depend on which toolsets are enabled on the agent.
- **Native OpenAI function calling:** Cleaner protocol, but not portable across the
  backends this worker supports (see above).

## Consequences

- One Hermes turn can make many LLM calls (one per tool step + one per extracted page),
  so multi-page requests take longer. Progress is shown via `status` events.
- The old `jobs` collection and `/api/scrape`, `/api/jobs/*` endpoints are removed.
- The worker must have Playwright browsers installed for the user it runs as
  (`sudo .venv/bin/playwright install --with-deps chromium` for a root service).
- In-flight turns are kept in server memory; a server restart mid-turn loses that turn
  (the user message is already persisted, so the user can simply resend).
