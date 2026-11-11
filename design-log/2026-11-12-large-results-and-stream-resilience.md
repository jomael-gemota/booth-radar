# Large results: fast mapping, light streaming, preview + file

**Date:** 2026-11-12
**Status:** accepted
**Author:** collaborative

## Context

The first real run after [letter-filtered feeds and lookups](2026-11-12-filtered-and-lookup-feeds.md)
on atlantamarket.com stopped at "Mapped 67/123" with **Connection lost**, showing 2,680 rows.

- **Mapping:** every 40-record batch went to the AI model (67 × 40 = 2,680). `read_feed` only uses
  the learned field mapping when the agent passes no `instructions`, and Hermes passed some. The
  4,918 records needed 123 model calls instead of 1, and the turn ran for minutes.
- **Streaming:** after every batch the worker sends the **whole table** as a `results` event. The
  server keeps every one of those events in the turn's replay buffer, writes each one to MongoDB,
  and pushes each one over SSE. By batch 67 that is tens of MB on one connection, and the
  browser re-renders the full table (thousands of `<tr>`) each time.
- **No recovery:** "Connection lost" is the browser's EventSource dropping without a server error.
  This is likely a proxy timeout on a long stream or a stalled tab. The worker and server keep
  going, but the client never reconnects, so the user sees a dead turn.
- **UX:** a table of thousands of rows in the chat, plus one progress line per batch
  ("Mapped 1/123" … "Mapped 123/123"), is hard to use.

## Decision

### 1. Map big feeds without the model whenever possible (`agent.py`, `recipes.py`)

- **Direct match:** when every requested field equals a record key (ignoring case, spaces and
  punctuation: "Exhibitor Name" = `exhibitorName`), map the keys directly with no model call.
- **With instructions:** still learn the field mapping from the first batch. Then check it on
  one batch from the middle of the feed. If the model kept every record in both batches (the
  instructions didn't filter anything) and the mapping reproduces both, apply it to the rest.
  Otherwise use the model for every batch, as today.
- Tool docs say `instructions` are for filtering records or explaining a value, and that they can
  make large feeds slower.

### 2. Stream less (`worker`, `server`)

- **Worker:** at most one `results` event every `RESULTS_EMIT_S` (2 s). Each event carries the
  latest snapshot, and the table is always flushed when a tool finishes and in `done`.
- **Server:**
  - Keep only the latest `results` frame in the turn's replay buffer, replacing older ones.
  - MongoDB writes follow the worker's cadence (at most one every 2 s), so the server needs no
    throttle of its own. `done` always writes.
  - Every SSE frame gets an `id:`, so a reconnecting browser asks for `?after=<last id>` and
    receives only what it missed. A turn that no longer exists answers `expired`.
- **Progress lines:**
  - Repeated progress statuses ("Mapped 12/123", "Looked up 450/1,731", "Extracted 8/40") carry
    `replace: true`.
  - The client and the saved thinking text replace the previous progress line instead of adding
    one, so the log shows one moving counter per step.

### 3. Reconnect instead of giving up (`client/useStream.ts`)

On a stream error without a server message, reconnect to `/api/stream/:turnId` up to 5 times
(1, 2, 4, 8, 16 s). The server replays the turn's events, which are now small. If the turn has
expired (finished while disconnected), reload the conversation so the reply and table appear.
"Connection lost" shows only after all retries fail, and the job/turn keeps running regardless.

### 4. Big tables: a CSV file, not a chat table (`client`)

- Tables of up to **20 rows** (`MAX_INLINE_ROWS`) show in the chat as today.
- Above 20 rows, the chat shows **no table**. It shows a results card instead, with the live row
  count and columns ("4,878 rows · Name, Exhibitor, Booth"):
  - while a turn is running, the card says the CSV is ready when the turn finishes;
  - afterwards it shows the **Download CSV** button (the existing export of the full table).
- A running background job doesn't block the download: it exports the rows collected so far.
- Not rendering big tables keeps live updates cheap however large the results get.
- CSV only (no Excel), as decided with the user.

## Alternatives Considered

- **Sending only new rows (deltas):** smaller frames, but merges change existing rows (filled
  cells), so deltas need row IDs end to end. Throttled snapshots plus a 50-row preview remove the
  load with far less change.
- **Never using the model when instructions are given:** that is today's behaviour, and it is
  what made the run slow.
- **Virtualised scrolling table for all rows, or a 50-row preview:** both still put a big
  table in the chat. The user prefers no table above 20 rows: the CSV is where large results are read.

## Consequences

- Filtering instructions that drop nothing in the two checked batches but would drop records
  later are applied by the mapping, not the model. The two-batch check (start and middle) makes
  this unlikely, and the summary lists how many records were mapped without the model.
- The UI updates up to 2 s late during big reads.
- Tests: unit tests for direct matching, the instruction check and progress-line replacement; a
  fake-model run of 4,918 records; a client reconnect test (kill the stream mid-turn); then
  `npm run check`.
