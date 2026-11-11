---
name: site-scraping
description: Booth Radar's fastest route to collect data from any public website (feeds, captured APIs, sitemaps, page extraction, background jobs). Load before working on a new site or data request.
version: 1.0.0
metadata:
  hermes:
    tags: [booth-radar, scraping]
    requires_toolsets: [mcp-booth-radar]
---

# Site scraping strategy

Use this for any request to collect data from a website. Try the routes in this
order and stop at the first that gives the items the user wants.

1. **Data feed.** On a new site, ALWAYS call `find_feeds` first. If a feed holds the
   items, `read_feed` collects all of them in seconds. Do not extract listing pages or
   read the sitemap to collect items or URLs a feed already gives.
2. **Captured API.** No feed, and the items are listed on a search, category or
   "load more" page: call `capture_api` on that page (`scroll=true` for infinite
   scroll) and `read_feed` the `api:N` feed it finds. A feed labelled "filtered to
   letter 'A'" holds one letter per call; `read_feed` reads every letter A–Z and #
   for you, so never extract the other letters' pages.
3. **Detail lookup.** The list lacks fields the user wants (booth, phone, address):
   include the item ID in the list's columns (e.g. "Exhibitor ID"), then `capture_api`
   ONE item's page. A feed labelled "detail lookup" fetches every item's details in a
   few calls: `read_feed(feed=..., id_column="Exhibitor ID", fields=["Booth"])`. All rows
   sharing an ID get its details.
4. **Item pages.** No lookup feed, or nothing above worked: get the item URLs from the
   feed rows, `map_site`, or a listing page, then `extract` them in batches of up to 40.
   When rows hold an ID but no URL, build the pages with `url_template`
   ("https://site/item/{Item ID}"). Pages with the same layout are read faster after
   the first one.
5. **Many pages.** More than 100 pages to extract: use `start_job` instead of many
   `extract` calls. Use `url_column` for URLs already in the results table (e.g. the
   "URL" column from a feed), `url_template` for pages built from an ID column, and
   `sitemap` + `include` for sitemap pages. Tell the user it runs in the background,
   then end your reply.
6. **Interactive pages.** Data that only appears after clicking a tab, choosing a
   filter, searching or scrolling: load the `interactive-pages` skill.
7. **fetch_page** only to understand a site's layout or answer a question.

## Fields

- Take column names from the user's request, short and human-readable: "Product
  Name", "Style Number", "Height", "Price", "URL".
- Include "URL" when rows come from different pages.
- Keep the same names when adding more details to the same rows later, so they merge
  into one table. Use `key_fields` to say which columns identify a row (e.g. `["URL"]`
  or `["Style Number"]`).
- Use `instructions` to say which items count or how to read a value ("only adult
  sizes", "price without currency symbol").
- Never call `clear_results` or pass `replace=true` unless the user asks for a new
  table. Rows you already collected are the user's results: to add or fix columns,
  read or extract again with the same column names and the rows merge.

## Caching

Pages, feeds and sitemaps loaded in the last hour are cached and return instantly. To
answer follow-up questions, call the tool again instead of guessing. Set
`refresh=true` only when the user asks for fresh data.

## Pitfalls

- A site that refuses a page (`robots.txt`, not a public website) won't change on
  retry; tell the user and suggest another page or source.
- When a list is split across pages (letters, page numbers, categories), build the
  full list of URLs and extract all of them unless the user asked for only some.
- Before finishing, check the result covers what the user asked for (every letter,
  every requested column). If not, say what is missing instead of starting over.
- Report failed pages in the summary instead of hiding them.
