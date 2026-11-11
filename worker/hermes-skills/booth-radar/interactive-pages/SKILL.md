---
name: interactive-pages
description: Get data that only appears after clicking tabs, choosing filters, submitting search forms, pressing "Next" or scrolling, using Booth Radar's browser sessions. Load when page URLs alone don't show the data.
version: 1.0.0
metadata:
  hermes:
    tags: [booth-radar, scraping, browser]
    requires_toolsets: [mcp-booth-radar]
---

# Interactive pages

Use this when the data only appears after an action on the page. If plain page URLs
exist for the same data (e.g. `?page=2`, `?category=boots`), use `extract` with those
URLs instead: it is faster and runs in parallel.

## Sessions and refs

- `navigate(url)` opens a browser session (`s1`, `s2`…, at most 3 per reply) and returns
  a snapshot: the page's elements with refs (`e1`, `e2`…) and its text.
- Every action returns a new snapshot. Use refs from the **latest** snapshot only; old
  refs stop working after the page changes.
- `snapshot(session, offset)` re-reads the page; pass `offset` to read further down.
- Pass `session` to `navigate` to open another URL in the same session (keeps cookies
  and consent choices).

## Actions

- **Tabs, buttons, links:** `click(session, ref)`.
- **Search boxes:** `fill(session, ref, text, submit=true)`, or fill then click the
  search button.
- **Dropdown filters:** `select(session, ref, value)` with the option's label or value.
- **Infinite scroll / "load more" on scroll:** `scroll_until_end(session)`, then
  `extract(session=..., fields=[...])`.
- **"Next" button without page URLs:** `paginate(session, next_ref, fields,
  max_pages)`. It extracts every page into the table and stops by itself on the last
  page.
- After filtering or searching, read the results with `extract(session=...,
  fields=[...])`.

## Pitfalls

- "element … is not on the page any more": take a new `snapshot` and use its refs.
- A cookie or consent banner hiding the page: click its accept or close button first.
- Don't click through many pages one by one with `click`; use `paginate`.
- Sessions close when your reply ends; open a new one in the next reply.
