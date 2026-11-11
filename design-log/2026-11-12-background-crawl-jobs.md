# Background Crawl Jobs

**Date:** 2026-11-12
**Status:** accepted
**Author:** collaborative

## Context

Phase 6 of [general web scraping via MCP](2026-11-07-general-web-scraping-mcp.md) (§7).
Phases 1–4 are in place, including the navigation tools from
[browser sessions and navigation](2026-11-12-browser-sessions-and-navigation.md).

Today every page is read inside a chat turn:

- `extract` takes at most `MAX_URLS_PER_CALL` (40) URLs, so a 4,598-page directory needs
  ~115 extract calls in one turn. Inline mode stops after `AGENT_MAX_STEPS` (12) and mcp
  mode after `AGENT_STREAM_TIMEOUT` (900 s).
- The user stares at one long turn and can't ask anything else meanwhile (the server
  answers 409 while a turn runs).
- A worker restart loses all progress except rows already saved.

The `TURN_BUDGET_S` from the general plan was never built; this entry replaces it with
an explicit tool the agent calls when the page count is large.

## Decision

### 1. Tools

| Tool | Purpose |
|------|---------|
| `start_job(fields, urls?, url_column?, sitemap?, include?, instructions?, key_fields?, replace?)` | Queue a background extraction of many pages into the results table and return at once. Exactly one URL source: `urls` (a list), `url_column` (the URLs already in a results column, e.g. feed rows that need details from each page), or `sitemap` + `include` (the site's sitemap URLs, like `map_site`). URLs are de-duplicated and capped at `MAX_JOB_URLS` (default **5000**). |
| `cancel_job()` | Stop the conversation's job; rows collected so far stay. |

- Guideline: for more than `JOB_MIN_URLS` pages (default **100**), use `start_job`
  instead of repeated `extract` calls. Below that, `extract` stays in the turn as today.
- `start_job` runs `table.prepare(...)` immediately, so a column mismatch is reported in
  the turn, and returns e.g. "Job started: 4,598 pages, about 25 minutes. Rows will keep
  appearing in the table." The agent tells the user and ends the turn.
- The system prompt's context section shows the conversation's job ("A background job is
  running: 1,200 of 4,598 pages, 3,950 rows"), so "how is it going?" is answered without
  a tool.
- Session-based crawling (`paginate`, clicking) stays in the turn; sessions are per turn.

### 2. Where state lives

The server owns MongoDB, so it owns the job record. A new `jobs` collection:

```ts
{ _id, jobId, conversationId, userId,
  status: 'queued' | 'running' | 'paused' | 'done' | 'cancelled' | 'failed',
  fields, keyFields, instructions, urls: string[], cursor: number,
  failed: { url, error }[] /* first 200 */, failedCount, rowsAdded,
  createdAt, updatedAt, finishedAt? }
```

`urls` is stored once; the worker processes it in order, so progress is a single
`cursor` (pages done). The worker keeps no state that matters across restarts.

### 3. Messages

Worker → server (new frames, keyed by `jobId` instead of `turnId`):

- `job_created` (sent during the turn by `start_job`): the full job spec. The server
  inserts the document with status `queued`.
- `job_progress` after every batch: `cursor`, newly failed URLs, rows added, and the
  conversation's full results snapshot.
- `job_finished`: final status (`done` / `cancelled` / `failed`) and an error message if
  any.

Server → worker:

- `job_run`: job spec, `cursor`, and the conversation's current results. Sent when a job
  is resumed, and for every `queued`/`running` job when the worker (re)connects. A new
  job starts on the worker as soon as `start_job` creates it.
- `job_pause`, `job_cancel`.

### 4. Running a job (worker)

- A job runner thread runs one job at a time (`JOB_CONCURRENCY`, default **1**), first
  come first served; others wait as `queued`.
- Each batch takes `JOB_BATCH_URLS` (default **40**) URLs from `cursor` and runs them
  through the same code as `extract`: page cache, learned recipes, HTTP first, browser
  fallback, `DOMAIN_CONCURRENCY` and `robots.txt`. Job batches use at most
  `JOB_PAGE_CONCURRENCY` tabs (default **4** of `SCRAPE_CONCURRENCY`'s 6), leaving room
  for chat turns.
- Pause and cancel take effect after the batch in flight (at most one batch of work).
  Cancel keeps the rows already collected.
- After a worker restart the server re-sends `job_run` with the saved `cursor`; at most
  one batch is read again, and its rows merge by key.

### 5. One results table per conversation

A chat turn and a job may write the same conversation's table at the same time, and
both persist full snapshots, so a stale copy could overwrite newer rows. To prevent it:

- While a conversation has a job, the worker keeps one live `ResultsTable` for it. A chat
  turn in that conversation uses this table instead of the copy the server sent, so
  every snapshot from the turn or the job contains all rows. `ResultsTable` gets a lock.
- While a job is active, `clear_results`, `replace=true` and `extract` with columns that
  don't overlap the job's columns return an error asking the user to cancel the job
  first. Adding columns that overlap still works.
- The server applies a conversation's result writes in arrival order (one promise chain
  per conversation), so an older snapshot never lands after a newer one.
- Chat stays open during a job: the user can ask questions or start other small scrapes.

### 6. Finishing

When a job ends, the server appends an assistant message to the conversation, built
from a template (no model call), e.g.:

> 🎉 Background job finished: read 4,598 pages and the table now has 4,571 rows. 27 pages
> couldn't be read.

The failed URLs (first 200) stay on the job record. The context prompt lists them after a
job ends, so the agent can retry them with `extract` or a new job when the user asks.

### 7. UI

- A job chip above the composer: "Collecting details · 1,200 / 4,598 pages · 3,950 rows"
  with **Pause/Resume** and **Cancel**, plus a thin progress bar.
- Endpoints: `GET /api/conversations/:id/job` (the latest job),
  `POST /api/jobs/:jobId/pause | resume | cancel` (owner-checked).
- While a job is queued, running or paused and the conversation is open, the client polls
  the job every 3 s. When `cursor` changes it refetches the conversation's results; when
  the job finishes it refetches the messages, which shows the finish message.

### 8. Configuration

`JOB_MIN_URLS` (100), `MAX_JOB_URLS` (5000), `JOB_BATCH_URLS` (40), `JOB_CONCURRENCY` (1),
`JOB_PAGE_CONCURRENCY` (4) in `worker/.env`.

## Alternatives Considered

- **Time-based handover (`TURN_BUDGET_S`):** the turn runs extract calls until 150 s,
  then moves the rest into a job. Needs the agent to plan URL lists it hasn't finished
  calling for, and makes job start unpredictable. An explicit tool is simpler and the
  agent can tell the user up front.
- **Worker writes MongoDB directly:** puts database credentials on the VM and splits
  ownership of conversations between two processes.
- **A job queue (Redis, Celery, RQ):** new infrastructure for one job at a time.
- **SSE per conversation for live job updates:** faster updates, but more server state
  for no real gain at one batch every 20–60 s; polling a small job record is enough.
- **Block chat while a job runs:** avoids the shared-table work but leaves the user
  waiting for up to an hour.

## Consequences

- Large directories and catalogs finish without holding a chat turn open, survive
  worker and server restarts, and can be paused or cancelled.
- New `jobs` collection, new WebSocket frames, two new tools, three endpoints and a UI
  chip. Each job batch also writes the conversation's full results (up to a few MB for
  5,000 rows, once per batch).
- Chat turns share the browser with a running job; turns may be slightly slower while
  one runs (`JOB_PAGE_CONCURRENCY` limits this).
- A job's URLs are fixed when it starts. Crawls that discover new pages as they go
  (following links, clicking "Next") are not covered.
