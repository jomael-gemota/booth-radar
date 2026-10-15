# Cancel In-Flight Turn

**Date:** 2026-10-16
**Status:** accepted
**Author:** collaborative

## Context

Long scraping jobs (many pages, slow sites) can take minutes. Users have no way to
abort them once started. The request is to let users click the send button — which
becomes a Stop button during processing — to cancel the current turn.

## Decision

A cancellation signal flows through the full stack via a cooperative flag:

```
Browser → POST /api/turns/:turnId/cancel
        → relay broadcasts 'cancelled' SSE event (clients stop immediately)
        → relay sends {type:"cancel",turnId} to worker via WebSocket
        → worker sets threading.Event for that turn
        → agent/extractor check the flag at each step boundary and raise TurnCancelled
```

### Client (`ChatBox.tsx` / `useStream.ts`)

- The send button becomes a **Stop button** (square icon, red tint) while `busy`.
- Clicking it calls `POST /api/turns/:turnId/cancel`.
- `useStream` adds a `cancelled` SSE event handler → calls `onCancelled()`.
- `onCancelled` calls `endTurn()` (clears busy / turnId) and adds a `⏹ Stopped`
  status bubble. Any partial streaming text is kept as-is.
- A `cancelling` boolean prevents double-clicks.

### Server relay (`relay.ts`)

- New `WorkerEventType`: `'cancelled'` — treated as terminal (closes SSE clients,
  marks turn finished, removes from activeTurns). Nothing is persisted to MongoDB.
- `getTurnConversationId(turnId)` — maps turnId → conversationId string for the
  ownership check in the cancel route.
- `cancelTurn(turnId)` — broadcasts `cancelled` to clients and forwards
  `{type:"cancel"}` to the worker.

### Server route (`chat.ts`)

- `POST /api/turns/:turnId/cancel` — verifies the turn exists and belongs to the
  requesting user, then calls `cancelTurn()`.

### Worker (`worker.py`)

- `_cancel_events: dict[str, threading.Event]` — one event per live turn.
- `handle_chat` creates and registers the event; passes it to `run_turn`; catches
  `TurnCancelled` (returns silently — server already closed the SSE channel).
- The WebSocket loop handles `{type:"cancel"}` by `.set()`-ing the event.

### Agent (`agent.py`)

- `TurnCancelled` exception class.
- `run_turn()` accepts `cancel: threading.Event | None`. Checks it at the start of
  each agent step and after each tool call.
- Passes `cancel` through to `chat_stream()`.

### Extractor (`extractor.py`)

- `chat_stream()` accepts `cancel: threading.Event | None`. Checks it once per SSE
  line received from Hermes — breaks out of the streaming loop early.

## Alternatives Considered

- **asyncio Task cancellation**: `CancelledError` is injected into the awaiting
  coroutine but the executor thread keeps running. Still need the threading.Event
  for the thread, so this adds complexity without benefit.
- **SIGINT / process signal**: Heavy-handed; would kill the whole worker, not just
  one turn.
- **HTTP long-poll cancel**: The browser could just drop the SSE connection. But
  the worker would continue processing unaware; we need an explicit signal.

## Consequences

- If cancel fires between LLM steps, it stops immediately (< 1 SSE chunk latency).
- If cancel fires mid-scrape, the current page finishes loading (Playwright is not
  interrupted) but no further pages are scraped.
- The user message is already in MongoDB; the conversation will show the question
  with no assistant reply — the user can simply re-ask.
- No partial assistant message is persisted (server discards the turn on `cancelled`).
