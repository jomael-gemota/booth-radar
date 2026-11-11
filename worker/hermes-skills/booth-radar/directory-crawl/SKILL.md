---
name: directory-crawl
description: Collect every entry from an exhibitor, dealer, store or member directory, including letter tabs, page numbers and large directories run as background jobs. Load for trade-show exhibitor lists and similar directories.
version: 1.0.0
metadata:
  hermes:
    tags: [booth-radar, scraping, directories]
    requires_toolsets: [mcp-booth-radar]
---

# Directory crawl

Use this when the user wants a full list from a directory: trade-show exhibitors and
booths, dealers, stores, members, sponsors.

## Steps

1. Follow the `site-scraping` order first: `find_feeds`, then `capture_api` on the
   directory page. Many exhibitor platforms load the list from a JSON API, which
   `capture_api` turns into a feed.
2. Otherwise `fetch_page` the directory once to see how it is split:
   - **Letter tabs** (A–Z, #): collect every letter page URL from the links.
   - **Page numbers** (`?page=2`, `/page/2/`): read the last page number and build
     every page URL.
   - **Categories or halls**: collect each category URL.
   - **"Load more" or infinite scroll with no URLs**: use the `interactive-pages`
     skill (`scroll_until_end`, or `paginate` on a "Next" button).
3. Extract the listing pages:
   - Exhibitor brands and booth numbers: `extract_booths` with the page URLs.
   - Any other columns: `extract` with the user's fields.
4. If the listing only shows names and each entry has its own page with the details
   the user wants, collect the entry URLs into a "URL" column first, then fill in the
   details with `start_job(url_column="URL", fields=[...])` when there are more than
   100 entries, or `extract` when there are fewer.

## Large directories

- More than 100 pages: `start_job`. Say how many pages and roughly how long, and that
  rows keep appearing in the table while the user can keep chatting.
- One job per conversation. The user pauses or cancels it from the chat; call
  `cancel_job` only when they ask you to stop.

## Reporting

- Give the total entries collected, which columns, and how many pages failed.
- Booth numbers are copied as shown (e.g. "N1234", "Hall 3 / B12"); never guess a
  missing booth.
- Entries without a booth stay in the table with an empty booth column.
