# Faster Screenshots of Stalled Pages

**Date:** 2026-11-24
**Status:** accepted
**Author:** collaborative

## Context

Follows [screenshots on slow pages](2026-11-24-screenshot-slow-pages.md). On the VM the
thorogoodusa.com screenshot worked but took 55 s. The log showed two requests that never
finished: `api.ipify.org` (a visitor IP lookup) and one image. The tool waited the full
20 s for the load event, then 5 s for `page.screenshot` before falling back to DevTools.

## Decision

- Visitor IP lookup services (`ipify.org`, `ipinfo.io`, `ipapi.co`) join the blocked
  tracker hosts, for scraping and screenshots alike.
- `SCREENSHOT_LOAD_MS` defaults to 12 s.
- A page that didn't finish loading goes straight to the DevTools capture.

## Alternatives Considered

- **Keep 20 s:** more images may arrive on a slow connection, but the user waits on
  every stalled page; 12 s is enough for the page's first screen in practice.

## Consequences

- Stalled pages return in roughly 15–25 s on the VM instead of ~55 s.
- On very slow connections some images may still be missing at 12 s.
