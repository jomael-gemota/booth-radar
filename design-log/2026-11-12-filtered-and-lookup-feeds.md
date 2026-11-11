# Letter-filtered feeds, detail lookups and no unprompted clears

**Date:** 2026-11-12
**Status:** accepted
**Author:** collaborative

## Context

On "extract all the brands with their booth numbers from atlantamarket.com/exhibitor/Directory,
A to #", the agent:

1. Called `capture_api` on the directory and got `api:2` (`GET /imc-api/v2/exhibitors/az?az=A`),
   labelled "single page". It read 310 records, but only for **letter A**, with no booth field.
2. Called `clear_results` on its own (the 200+ rows vanished), extracted the directory page
   (19 rows), then clicked exhibitors one by one until a click timed out.

The old native-tools Hermes run worked because it found the site's hidden detail API and fetched
1,606 exhibitor profiles in slices (2026-10-16 time-boxed fast scraping). The new pipeline can't
do that yet. [API capture and recipes](2026-11-11-api-capture-and-recipes.md) only keeps
*list* responses and only knows page/offset paging.

What the site actually offers (verified with plain HTTP replays, no browser):

| Request | Result |
|---|---|
| `az?az=A` … `az=Z`, `az=%23` (“#”) | 27 calls → 4,918 records (1,731 exhibitors + 3,187 brand lines), each with `exhibitorId`, `name`, `type`, `exhibitorName` |
| `OpenDetails?exhibitorIds=97991` | 1 record: company details incl. `activeLeases[].showrooms[].showroomDisplayName` = "B1 18-B1" |
| `OpenDetails?exhibitorIds=1&exhibitorIds=2…` (repeated) | up to 100 records per call, in request order (50 ≈ 7 s) |
| `OpenDetails?exhibitorIds=1,2` (comma) | 200 with 0 records |

So the whole answer is about 27 + 35 HTTP calls (a minute or two), instead of 1,700 rendered pages.

## Decision

### 1. Letter-filtered API feeds (`site_feeds`)

- `capture_api` looks for a query/body parameter whose value is one letter (A–Z, any case) or `#`,
  where at least 80% of the captured records have a text value starting with that letter (for `#`,
  starting with a non-letter). This is the feed's **filter** (`_Filter(location, path, value)`, like `_Paging`).
- Label: `… — filtered to letter 'A' by parameter 'az'; read_feed reads every letter A–Z and #`.
  Record count is shown as "letter A: 310 records" so it is never mistaken for the total.
- `read_feed` on a filtered feed replays every letter A–Z plus `#` (sent URL-encoded; if `#` fails,
  `0-9` is tried), each with its own paging. The calls run `FEED_CONCURRENCY` at a time, and duplicate
  records are removed. A failing letter is reported back, not silently dropped. The optional
  `letters` argument (e.g. `"A-C"` or `"#"`) limits the read.
- `MAX_FEED_RECORDS` default rises from 2,000 to 10,000. A directory this size (4,918) was being cut off.

### 2. Detail lookup feeds

- `capture_api` also keeps responses with **one or more** records when the request has an ID
  parameter. That's a parameter whose value appears in the page URL (`/exhibitor/97991` →
  `exhibitorIds=97991`) or whose name ends in `id`/`ids` with an ID-like value (digits, UUID, slug). It
  becomes a **lookup feed**: `api:N: lookup — one record per exhibitorIds value (fields: …)`.
- `read_feed(feed="api:N", id_column="Exhibitor ID", fields=[...])` reads the distinct values in
  that table column and replays the lookup for them. On the first call it probes batching with two
  IDs: a repeated parameter first, then comma-separated. If either returns both records, it sends
  `LOOKUP_BATCH` (50) IDs per call. Otherwise it sends one ID per call. Calls run
  `FEED_CONCURRENCY` at a time, with up to `MAX_LOOKUP_IDS` (5,000) IDs, and status updates show progress.
- Each returned record is matched to its ID by finding the ID value inside the record, with
  request order as the fallback. The ID is then written into the record under the `id_column`
  name, so the field mapping (AI or learned recipe) outputs it unchanged.
- Rows are written with a new `ResultsTable.fill(column, rows)`. It fills the empty cells of
  **every** row whose `column` equals the row's value, so all 3 brand lines of one exhibitor get its
  booth. `add()` stays as it is (it fills only the first key match).
- Lists in API records are flattened as well: lists of objects become dotted keys with the
  distinct values joined by "; ". For example, `companyDetails.activeLeases.showrooms.showroomDisplayName`
  = "B1 18-B1". The learned feed recipe can map such keys, so 1,731 records don't each go through
  the model. Values over `FEED_VALUE_CHARS` are still trimmed (the 50-state country lists).

### 3. Prompt and skill rules

- Guidelines: **never call `clear_results` or pass `replace=true` unless the user asked for a new
  table**. To fix rows, extract again into the same columns (rows merge by key).
- `directory-crawl` skill:
  - A letter-filtered feed covers every letter only through `read_feed`. Check the user's letters
    (A to #) are all in the result.
  - When the list feed lacks a requested field (booth, phone…), open one item page with
    `capture_api` to find a lookup feed, then `read_feed` it with `id_column`.
  - Use per-page `extract` / `start_job` only when no lookup feed exists.
- `start_job` / `extract` accept `url_template` (e.g. `https://site/exhibitor/{Exhibitor ID}`)
  as a fourth URL source. URLs are built from the distinct values of the named column, and each
  page's rows are written with `fill()` on that column. This covers sites with item pages but no
  lookup API.

### 4. Click fallback (`browser.click`)

When `locator.click` times out (the element is covered or animating), a link (the element or
its closest `<a href>`) is opened by navigating to its address. Other elements are retried once with
`force=True`. A forced click on a link can "succeed" without doing anything, which is why the
address comes first. The tool result says which fallback was used.

## Alternatives Considered

- **Hard-coding atlantamarket.com:** fast to write, but it breaks the general-scraper goal and every
  other market site (AmericasMart, Las Vegas Market) runs the same imc-api with different channels.
- **Rendering all 1,731 exhibitor pages with a background job:** works without an API, but at about
  1.5 s per page with 4 at a time it takes around 11 minutes and hundreds of model calls. Kept
  only as the fallback (`url_template`).
- **Asking the model to change the `az` parameter itself:** the agent never sees the raw request
  (sc_apikey stays hidden by design), and the failed run shows it doesn't notice the filter anyway.

## Consequences

- Filter detection is heuristic. A parameter like `lang=a` won't pass because the 80% first-letter
  check fails. A feed filtered by another single value (category, state) is still read as captured.
- Lookup batching is probed on every read. That costs one extra call, but it is cached with the feed.
- Learned feed recipes saved before this change keep working. Flattening replaces a list of
  objects with its dotted keys, and such lists weren't mappable before.
- While testing, `capture_api` on the directory sometimes recorded no JSON at all. The site's
  data calls can start after the network first goes quiet. When nothing JSON has arrived, the
  capture now waits one more idle round, and its log line shows the request count and page title.
- Measured on atlantamarket.com, with the field mapping learned after the first batch:
  - all letters: 4,918 records in about 13 s;
  - booths for all 1,731 exhibitors: about 60 s;
  - result: 4,878 rows, every one with a booth, from 2 model calls.
- Lookups run inline in the chat turn, at about 1–2 minutes for 1,731 IDs in batches. Lookups that
  only work one ID per call and have more than `JOB_MIN_URLS` IDs are reported with an estimate. Moving
  them into background jobs is follow-up work.
- Tests: unit tests for filter/lookup detection, flattening and `fill()`, plus a live test on
  atlantamarket.com (all letters and booths for every exhibitor), then `npm run check`.
