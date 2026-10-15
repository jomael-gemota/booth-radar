# Persist Turn Duration on Assistant Messages

**Date:** 2026-10-15
**Status:** accepted
**Author:** collaborative

## Context

Each assistant response bubble displays a "time to respond" badge (clock icon + elapsed
time). The value is calculated client-side: `turnStartRef` is set when the `/api/chat`
POST fires and cleared when the SSE stream emits `done`. Because the duration lives only
in React state it is gone as soon as the user refreshes the page.

## Decision

Track the duration **server-side** inside `relay.ts`:

1. Add `startedAt: number` to the `Turn` struct — stamped with `Date.now()` in
   `startTurn()`.
2. Add an optional `duration?: number` (milliseconds) field to the `ChatMessage`
   interface.
3. In `persist()`, when `type === 'done'`, compute
   `duration = Date.now() - turn.startedAt` and attach it to the assistant message
   before writing it to MongoDB.
4. Expose `duration` in `GET /api/conversations/:id` (already maps message fields —
   just include the new field).
5. In `ChatBox.tsx → loadConversation()`, pull `duration` from the API response and
   set it on the reconstructed `Message` objects.

The server-side clock measures from `startTurn()` (worker dispatch) to the receipt of
the `done` event from the worker, which is the pure worker-processing latency. This
differs slightly from the original client-side measure (which included the HTTP
round-trip), but the difference is negligible and the value is now permanent.

## Alternatives Considered

- **Client PATCH after endTurn**: send the elapsed time to a new API endpoint to be
  stored. Adds a round-trip and a new route just for one field.
- **localStorage map (conversationId → messageIndex → duration)**: no server change but
  can desync across devices and after sidebar title renames shift indices.

## Consequences

- All future turns will have a persistent duration badge.
- Existing assistant messages already in MongoDB have no `duration` — their bubbles will
  silently omit the badge (same behaviour as before).
- No migration needed; the field is optional everywhere.
