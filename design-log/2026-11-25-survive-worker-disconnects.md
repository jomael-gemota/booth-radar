# Turns survive worker disconnects

**Date:** 2026-11-25
**Status:** accepted
**Author:** collaborative

## Context

In production, a price request ("style 0045 from refrigiwear.com, all sizes and colours") failed
with **"The worker disconnected while handling your message. Please try again."**

The server shows that error as soon as the worker's WebSocket closes. The socket can close for
several reasons:

- the `booth-radar-worker` systemd service restarted (it is restarted after every pull) or crashed;
- a network blip between the Ubuntu machine and Railway;
- a frame larger than 1 MiB from the server (the `websockets` client's default `max_size`). The
  `chat` and `job_run` frames carry the conversation's whole results table, so a follow-up message
  in a conversation with a large table closes the socket with code 1009 on every attempt.

Two design choices make each of these fatal to the turn:

- **Server (`relay.ts`):** `detach` broadcasts `error` to every unfinished turn immediately, even
  though the worker reconnects within about 5 s.
- **Worker (`worker.py`):** a turn's frames go to the outbox of the connection it started on. If
  that connection drops, the turn keeps running but nothing it sends ever reaches the server.

The [stream resilience entry](2026-11-12-large-results-and-stream-resilience.md) made the
browser-to-server stream reconnect. This entry does the same for the server-to-worker link.

## Decision

### 1. Worker: one outbox across connections, plus a `hello` frame

- Turn frames go to a module-level queue that outlives connections. Each connection's sender
  drains it, and a frame leaves the queue only after `ws.send` succeeds. Frames produced while the
  worker is disconnected are delivered after it reconnects.
- Job frames keep today's behaviour: they are dropped while the worker is disconnected, because the
  server resends `job_run` on reconnect.
- The first frame on every connection is `{ type: "hello", turns: [...] }`. It lists the turns the
  worker still owns: turns that are running, plus turns with frames still queued.
- `max_size=None`: the server is trusted, and its `ws` library already accepts up to 100 MiB.

### 2. Server: a grace period, then resume or restart

- On disconnect, unfinished turns are marked **orphaned** (not failed). Each one gets a status line,
  "Lost the connection to the worker, waiting for it to reconnect…". A timer of `WORKER_GRACE_MS`
  (default 120 s) fails any turn that is still orphaned when it fires.
- On reconnect, the server waits for `hello` (up to 10 s; a worker without `hello` counts as
  owning no turns):
  - **The worker still owns the turn** (network blip): the turn continues, and the frames queued by
    the worker arrive.
  - **The worker lost the turn** (restart or crash): the server sends the same `chat` frame again
    under the same turn id, with a fresh job context. Up to `MAX_TURN_RESTARTS` (default 2)
    restarts are allowed. A status line says the request is starting over.
- Only after the grace period runs out, or after the restarts run out, does the user see an error,
  and that error says what happened.

## Alternatives Considered

- **Retry from the browser:** the browser has no idea why the stream ended, and it would duplicate
  the user message. The server already has everything it needs to resend the turn.
- **Persist turn state and resume mid-run after a worker restart:** agent runs are not resumable
  (a Hermes conversation in progress, open browser sessions). Restarting the turn is simpler, and
  the page cache makes the second run faster.
- **Raise `max_size` only:** this fixes the 1009 close but not restarts, crashes or blips.

## Consequences

- A restarted turn shows the first attempt's activity, then a "starting over" line. Any partial
  reply text from the first attempt stays visible until the page reloads. Only the final reply is
  saved.
- If a turn's first run had already started a background job, the restart sees that job in its
  context instead of blindly starting a second one.
- A turn that crashes the worker on every run fails after `MAX_TURN_RESTARTS` restarts, not after
  looping forever.
- If the Railway server itself restarts, in-memory turns are still lost (unchanged; the browser
  reloads the conversation on `expired`).
