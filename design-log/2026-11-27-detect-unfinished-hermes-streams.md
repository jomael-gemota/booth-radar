# Detect Unfinished Hermes Streams

**Date:** 2026-11-27
**Status:** accepted
**Author:** collaborative

## Context

A style request on refrigiwear.com (2026-11-27, mcp mode) ended 41 s in with
"turn … completed — 0 result rows". The worker log showed:

- `fetch_page(browser=true)` reached the bridge at 15:45:29.9 and launched Chromium.
- The turn was reported complete at 15:45:45, while that tool was still running.
- At 15:45:51 the tool finished and the bridge's reply failed with `BrokenPipeError`:
  the HTTP client (`mcp_server.py`, a child process of Hermes) was gone.

Every configured timeout is 900 s, so no timeout fired. Reading Hermes Agent's source
(`agent/tool_executor.py`, `tools/mcp_tool_handlers.py`,
`gateway/platforms/api_server_openai_routes.py`):

- When Hermes times out or abandons a tool call, `mcp_server.py` keeps running and its
  `requests.post` still completes, so the bridge would not see a broken pipe. A broken
  pipe means the `mcp_server.py` process exited, which points to Hermes itself
  restarting or being killed (e.g. out of memory while Chromium starts).
- Hermes ends a normal stream with a final chunk (`finish_reason` `stop`) and
  `data: [DONE]`. A failed or stopped run ends with `finish_reason` `error` /
  `cancelled` and an `error` / `hermes.error` message.

`extractor.chat_stream` ignored all of this: a connection that closed without
`[DONE]`, or a final chunk reporting an error, was returned as a successful reply. The
user got an empty "completed" turn instead of an error, and the logs did not say why.

## Decision

1. **`chat_stream` checks how the stream ended** (when the turn wasn't cancelled):
   - no `[DONE]` and no final chunk → `RuntimeError("Hermes closed the connection
     before the reply finished …")`;
   - `finish_reason == "error"` → `RuntimeError("Hermes run failed: <message>")`;
   - `finish_reason == "cancelled"` → `RuntimeError("Hermes stopped the run …")`;
   - `finish_reason == "length"` (budget or truncation) → warning only; the partial
     reply is still useful.
   The worker already turns these into an `error` frame, so the user sees what happened
   and can retry. Each case is logged as a warning.
2. **Tool bridge:** a reply to a caller that has disconnected (`ConnectionError`) is
   logged as one warning with the tool's run time instead of a socketserver traceback.
3. **MCP turns** log every tool still running when Hermes ends the reply
   ("Hermes ended the reply while fetch_page was still running (15.2s)").

## Alternatives Considered

- **Restart the turn automatically when Hermes drops the stream:** Hermes may be down
  or restarting; repeating the request could repeat a crash (for example memory
  pressure from Chromium) and duplicate background jobs. The user can retry.
- **Treat a missing `[DONE]` as success when text arrived:** that is exactly the
  silent empty turn this fixes.

## Consequences

- Hermes crashes, restarts and failed runs show up as errors in the chat and in the
  worker log, with a reason.
- The Hermes-side cause of the 2026-11-27 turn still has to be confirmed from the VM's
  Hermes log and kernel log (`journalctl` for the Hermes unit, `dmesg` for the OOM killer).
- OpenAI-compatible servers that end streams without `[DONE]` still work as long as
  they send a `finish_reason`.
