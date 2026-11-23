# Screenshots: Session Images, Size Limit and Hermes Restart

**Date:** 2026-11-24
**Status:** accepted
**Author:** collaborative

## Context

Revises [screenshot tool](2026-11-24-screenshot-tool.md). The first deploy replied
"screenshot capture wasn't available". Testing locally showed:

- The tool works, and `mcp_server.py` advertises it, but Hermes starts the MCP server
  itself and keeps it running. Restarting only the worker leaves Hermes with the old
  tool list.
- Session screenshots were nearly blank: session contexts block images and fonts, and
  on some sites (thorogoodusa.com) the main content stays hidden without them.
- A full-page shot over the server's 10 MB `MAX_FILE_BYTES` was dropped silently by the
  server while the agent told the user it was attached.
- The skills never mentioned screenshots.

## Decision

- **Session screenshots** switch the session's context to the screenshot route (images
  and fonts allowed) for the rest of the turn. A page not clicked, typed into or scrolled
  since `navigate` is reloaded, which gives the same result as a `url` shot. After
  interactions a reload would undo them, so only `<img>` elements are re-requested and
  the tool result says fonts and lazy images may be missing.
- **Size:** above `MAX_SCREENSHOT_BYTES` (9.5 MB) a full-page shot is retaken as the
  visible part; if that is still too large the tool returns an error.
- **Guidance:** `site-scraping` skill step 8 and the mcp strategy summary say to call
  `screenshot` when the user asks to see a page.
- **Deploy:** README says to restart Hermes after a pull that changes tools;
  `test_local.py --screenshot URL` checks both kinds on the VM.

## Alternatives Considered

- **Allow images in every session:** simplest, but every interactive scrape would
  download images it doesn't need.
- **Always reload session pages:** complete images, but loses filters, tabs and typed
  searches, which is usually what the user wants to see.

## Consequences

- A session that has been screenshotted loads images for its later pages in the turn
  (slightly slower).
- Screenshots after interactions can still lack web fonts and lazy-loaded images.
