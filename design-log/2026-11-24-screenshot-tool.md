# Screenshot Tool

**Date:** 2026-11-24
**Status:** accepted
**Author:** collaborative

## Context

Booth Radar can navigate pages and read their text, but has no way to capture what
they look like visually. Users sometimes want to see a page — for verification,
debugging, or when visual context matters (layouts, charts, images).

The existing `browser.py` runs a headless Chromium via Playwright; every session tab
has a `page` object. Adding `page.screenshot()` is trivial. The `file` worker-event
path already stores binary files in MongoDB and delivers them to the client as
`Attachment { id, name, size }`. The only gaps are:

1. No `screenshot` tool in the tool layer (MCP server + inline `_TOOL_DOCS`).
2. `Attachment` carries no `mime`, so the client cannot distinguish images from
   documents and always renders a download chip.
3. No inline-image display in the chat.

## Decision

### 1. New tool `screenshot(session?, url?, full_page?, turn)`

| Parameter | Type | Required | Meaning |
|-----------|------|----------|---------|
| `session` | str | one of | Existing session's current page |
| `url` | str | one of | Fresh URL to load and screenshot |
| `full_page` | bool | no | Scroll to capture the full page height (default false) |
| `turn` | str | MCP only | Turn id |

Exactly one of `session` or `url` must be given. Using `session` is cheaper (no
extra navigation). Using `url` opens a fresh isolated page that **allows images**
(for a visually complete screenshot); `session` pages use whatever the browser
already loaded.

### 2. `browser.py` additions

- `TurnSessions.screenshot(sid, full_page)` — calls `page.screenshot(type="png")`
  on the session's current page.
- `screenshot_url(url, full_page)` — opens a fresh context via `_route_screenshot`
  (blocks only trackers and media, not images/fonts) so the page renders visually;
  navigates, settles, screenshots, closes.
- `_route_screenshot` — like `_route` but `_BLOCKED_RESOURCES` becomes `{"media"}`.

### 3. `agent.py` additions

- Tool doc entry added to `_TOOL_DOCS`.
- `_tool_screenshot` handler: emits `status`, calls browser, emits a `file` event
  (`image/png`), returns a one-line text summary.
- `_preamble_for` entry for natural fallback text.

### 4. `mcp_server.py` addition

A `screenshot` tool that forwards to the bridge like every other tool.

### 5. `Attachment` gains `mime` (server + client)

The server's `acceptFile` already stores `mime` in `FileDoc`; it was just not
forwarded in the `Attachment` object broadcast to the client. Adding `mime?:
string` to the `Attachment` interface (relay, useStream) lets the client branch on
image vs. document.

### 6. Inline image rendering in the chat

`AttachmentChip` gains an image branch: when `file.mime?.startsWith('image/')` it
renders an `<img src="/api/files/:id">` with max-width styling plus a small
download link below. All other MIME types keep the existing chip. The `/api/files`
route already serves the correct `Content-Type`; `Content-Disposition: attachment`
does not affect `<img src>` subresource loads.

## Alternatives Considered

- **Store PNG on disk and use the artifacts system:** `artifacts.py` would need
  `.png` added to `ARTIFACT_EXTENSIONS` and the route filter, plus Hermes' output
  folder path in the return text. Using the existing `file` event is simpler and
  keeps images in MongoDB alongside other files.
- **Return base64 data URL in the text reply:** embed inline in the AI message.
  Blows up message storage and SSE frame size for no benefit over the file path.
- **Serve images inline (no Content-Disposition):** minor UX difference. Can be
  added later with `?inline=1`.

## Consequences

- Screenshots use one tab slot (`_tabs` semaphore) while loading; URL-based shots
  use their own isolated context (cleaned up immediately after).
- Full-page screenshots of very tall pages can exceed the 10 MB `MAX_FILE_BYTES`
  server limit; the server rejects oversized files and the agent surfaces the error.
- Session screenshots capture the page as the agent left it (images may be blocked
  if the session context does not allow them). This is intentional: it shows what
  the agent sees.
- `mime` is added as an optional field to `Attachment`; existing conversations and
  client code that don't set it continue to render the download chip.
