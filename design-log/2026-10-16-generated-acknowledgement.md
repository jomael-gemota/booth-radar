# Generated Acknowledgement Before Agent Actions

**Date:** 2026-10-16
**Status:** accepted
**Author:** collaborative

## Context

[2026-10-16-guaranteed-preamble.md](2026-10-16-guaranteed-preamble.md) made sure the user
always sees a message before Booth Radar acts, but the message is a fixed template ("On
it! Give me a moment while I look into this 🔍"). Users find it static and want a reply
that responds to what they actually asked.

Hermes itself cannot provide this line. The raw stream (`test_local.py --probe-raw`)
shows Hermes Agent starts its built-in tools (`hermes.tool.progress`) before streaming any
content, so by the time its own words arrive the action has already happened. Hermes also
adds ~35k prompt tokens of its own instructions to every request, so a separate Hermes
call just for an opening line would be slow.

## Decision

When a turn starts, `worker/agent.py` starts generating an opening line on a background
thread with `gpt-4o-mini` on OpenAI (`extractor.quick_completion`, `ACK_MODEL`), using a
short Booth Radar persona prompt (`ACK_PROMPT`) and the last six conversation messages.

- If Hermes starts a built-in tool, or calls one of our tools, before any visible text,
  the generated line is shown instead of the template. The agent waits up to
  `ACK_WAIT_SECONDS` (default 4 s) for it; measured latency is 1–3 s and generation
  starts when the message arrives, so it is normally ready already.
- If Hermes replies with text first, the generated line is discarded.
- It is used at most once per turn; later steps that need a preamble use the templates.
- If `OPENAI_API_KEY` is missing or the call fails, the templates from the earlier entry
  are used.

The templates in `_preamble_for` and `NATIVE_TOOL_PREAMBLE` remain as fallbacks.

## Alternatives Considered

- **Ask Hermes for the opening line in a separate call:** matches Hermes's voice, but each
  Hermes request carries its own large prompt and may itself run tools; too slow.
- **Generate the line only when the first tool starts:** wording could use the tool
  label, but the user would wait 1–3 s with nothing shown, or see the reasoning bubble
  appear above the message.
- **Generate a line only for messages that look like requests:** saves calls, but a
  keyword heuristic would misfire; one `gpt-4o-mini` call per message is cheap.

## Consequences

- One extra `gpt-4o-mini` call per user message, including plain chat where the line is
  discarded.
- The opening line comes from a different model than the rest of the reply, so its tone
  can differ slightly from Hermes's.
- The worker's `OPENAI_API_KEY` must be set on the Ubuntu machine; the worker must be
  restarted to pick up the change.
