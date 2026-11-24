# Screenshots on Pages That Never Finish Loading

**Date:** 2026-11-24
**Status:** accepted
**Author:** collaborative

## Context

Follows [screenshot session images](2026-11-24-screenshot-session-images.md). On the VM,
`test_local.py --screenshot https://thorogoodusa.com/` failed both ways: `Page.goto` and
`Page.reload` timed out after 45 s waiting for `domcontentloaded`. The same session had
just loaded the page with images blocked, and locally the page loads in ~3 s, so the
images and fonts (or their hosts) are what stall on the VM.

- Every allowed request runs a host check (`net_guard`, DNS) in the default executor, which
  has only a few threads on a small VM. Many image hosts can queue the page's scripts
  behind them and hold back `domcontentloaded`.
- A screenshot treated "the page didn't finish" as a failure.
- `page.screenshot` waits for web fonts, so it also hangs when fonts are stuck.

## Decision

- Route host checks run in their own pool (`ROUTE_CHECK_THREADS`, 16).
- Screenshot loads wait only for the response (`wait_until="commit"`), then up to
  `SCREENSHOT_LOAD_MS` (20 s) for the `load` event, and capture whatever rendered.
- If the page didn't finish, `page.screenshot` gets 5 s; when it times out the frame is
  captured with the DevTools `Page.captureScreenshot` command, which doesn't wait for
  fonts. The tool result names the hosts still loading so the agent can tell the user
  parts may be blank, and the worker log lists the pending URLs.

## Alternatives Considered

- **Longer timeouts:** a page that never finishes would still fail, only later.
- **Skip host checks for subresources:** faster, but drops the private-address guard
  for images and fonts.

## Consequences

- A screenshot returns within about 20–40 s even on a page that never finishes loading;
  it may show blank areas where images or fonts didn't arrive.
- The pending-request log line shows what stalls on the VM, if it happens again.
