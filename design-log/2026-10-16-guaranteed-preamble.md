# Guaranteed Preamble Before Agent Actions

**Date:** 2026-10-16
**Status:** accepted
**Author:** collaborative

## Context

Users still see Booth Radar act immediately on a request without first saying what it
is about to do. The system prompt already requires a conversational message before every
tool call (commit `d6ad34e`, "RULE: Always talk first, act second"), but Hermes does not
follow it reliably. Two ways this happens:

- Hermes emits our `<tool>{...}</tool>` block with no visible text before it, or puts its
  preamble inside `<think>` tags (shown only in the collapsed reasoning bubble).
- Hermes Agent runs one of its own built-in tools (`hermes.tool.progress` events) before
  streaming any reply text.

Prompt wording alone cannot guarantee the behaviour, so it is now enforced in code.

## Decision

In `worker/agent.py → run_turn()`:

1. **Our tool calls:** after each Hermes step, if a valid tool call has no visible text
   before it, emit a short fallback message built from the call (`_preamble_for`), e.g.
   "Let me open https://… and see how it's laid out 🔍" or "Great, I'll collect the brands
   and booth numbers from 10 pages now — this may take a moment! 📄". It is streamed before
   the tool runs, saved in the reply, and added to the assistant message in the
   conversation sent back to Hermes so the model sees itself as having spoken.
2. **Hermes native tools:** `chat_stream()` gains an `on_tool_progress` callback (falls
   back to `on_thinking`). If a native tool starts before any visible text in the step,
   the agent first emits "On it! Give me a moment while I look into this 🔍", then the
   progress line goes to the reasoning bubble as before.

The prompt rule stays; Hermes's own wording is always preferred when it provides it.

## Alternatives Considered

- **Stronger prompt wording:** already tried; unreliable.
- **Reject the tool call and ask Hermes to retry with a preamble:** natural wording, but
  costs a full extra LLM round-trip per offending step, which makes slow turns slower.
- **Separate "acknowledge" LLM call before every turn:** doubles latency on every message,
  including plain chat that needs no tools.

## Consequences

- The user always sees a message before any scraping starts.
- Fallback messages are templated and less varied than Hermes's own wording.
- Worker logs `tool call had no preamble, using fallback` so we can see how often Hermes
  skips the rule.
- Requires redeploying the worker on the Ubuntu machine.
