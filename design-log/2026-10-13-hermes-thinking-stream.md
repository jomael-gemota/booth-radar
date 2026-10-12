# Hermes Thinking Stream

**Date:** 2026-10-13
**Status:** accepted
**Author:** collaborative

## Context

The conversational agent (see [2026-10-13-conversational-agent.md](2026-10-13-conversational-agent.md))
already streams `token` events from Hermes to the browser in real-time. However, many
reasoning-capable LLMs (including the Hermes-3 family and DeepSeek-R1 compatible
endpoints) emit a separate thinking / chain-of-thought trace **before** their reply.

Two delivery formats exist in the wild:
- **Native `reasoning_content`** — the delta object carries a `reasoning_content` field
  (used by xAI Grok, some OpenAI-compatible providers).
- **`<think>…</think>` tags** — reasoning is inlined in the `content` stream between
  explicit tags (NousResearch Hermes-3 native format).

Neither was handled; thinking tokens were either silently discarded or bled into the
user-visible assistant text.

## Decision

### New SSE event: `thinking`

Add `thinking` alongside the existing `token | status | results | done | error` types.
The worker emits `{ type: "thinking", payload: { text: string } }` for each reasoning
chunk. The server relays it verbatim (no persistence — thinking traces are ephemeral).

### Worker changes (`extractor.py`)

Add a `_ThinkingFilter` class (mirrors `_ToolBlockFilter` in `agent.py`) that parses
`<think>…</think>` tags from the content stream without buffering more than one partial
tag. `chat_stream()` gains an `on_thinking` parameter:

- If the delta has a non-empty `reasoning_content` attribute → call `on_thinking`
  directly (native format).
- Otherwise feed the delta's `content` through `_ThinkingFilter`:
  - thinking content → `on_thinking`
  - normal content → `on_token`

### Worker changes (`agent.py`)

In `run_turn()`, pass `on_thinking=lambda t: emit("thinking", {"text": t})` to every
`chat_stream()` call. The `_ToolBlockFilter` wraps only `on_token`; thinking tokens
bypass it because they are never interleaved with tool blocks.

### Server changes (`relay.ts`)

Add `'thinking'` to `WorkerEventType`. Broadcast to SSE clients the same way as
`token`. Do **not** persist to MongoDB (thinking traces are not part of the
conversation history sent back to Hermes).

### Client changes

`useStream.ts` — add `onThinking?: (text: string) => void` handler and a
`'thinking'` EventSource listener.

`ChatBox.tsx` — introduce a `'thinking'` role and a `ThinkingBubble` component:
- Auto-expands while streaming (user sees reasoning in real-time).
- Collapses automatically when the turn ends (stays expandable on click).
- Appears immediately above the assistant reply bubble.

## Alternatives Considered

- **Show thinking inside the assistant bubble** — harder to read; mixes reasoning with
  the actual reply.
- **Persist thinking tokens** — unnecessary noise in the conversation history sent to
  Hermes on the next turn.
- **Only support one format** — `reasoning_content` only misses Hermes-3 native;
  `<think>` only misses other providers. Supporting both costs little.

## Consequences

- Adds a small per-token callback overhead in `chat_stream` (negligible).
- If a provider uses neither format, `on_thinking` is simply never called; no UI
  change occurs.
- The thinking bubble is not stored in MongoDB, so it disappears on page refresh
  (intentional — only the final reply is persisted).
