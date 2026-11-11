---
name: product-catalog
description: Collect product lists with specs and prices from shop and brand sites, merge details from product pages into catalog rows, and compare a product across a few merchant sites. Load for product, spec or price requests.
version: 1.0.0
metadata:
  hermes:
    tags: [booth-radar, scraping, products, prices]
    requires_toolsets: [mcp-booth-radar]
---

# Product catalog

Use this for product lists, specifications and prices.

## Whole catalog

1. `find_feeds` first. WordPress (WooCommerce) and Shopify shops usually publish the
   whole catalog: `read_feed` gives names, URLs, prices and often SKUs in seconds.
2. No feed: `capture_api` on a category or search page, then `read_feed`.
3. Feeds often lack specs (heights, materials, style numbers). Collect the rows with a
   "URL" column first, then fill in the specs from each product page with the same
   column names:
   - up to 100 products: `extract(urls=[...], fields=["URL", "Height", ...],
     key_fields=["URL"])`;
   - more: `start_job(url_column="URL", fields=["Height", ...])`.
   Rows merge by URL, so the table keeps one row per product.

## Columns

- Keep the names the user used ("Style Number", not "SKU", if they said style number).
- Prices: one "Price" column with the number as shown; add a "Currency" column when
  the site or the user's request involves more than one currency. Use
  `instructions` for sale vs regular price when it matters ("use the sale price when
  shown").
- Variants (sizes, colours): one row per product unless the user asks for one row per
  variant; say which in `instructions`.

## Comparing merchants

To compare one product across a few shops: find the product page on each shop (site
search with `navigate` + `fill`, or the shop's feed), then `extract` those pages with
the same fields plus "Shop" and "URL". Report the cheapest and any big outliers, and
say that prices are as shown on each site at the time of reading.

## Pitfalls

- Product pages with prices loaded by JavaScript: `extract` renders them in the
  browser automatically when the plain page has no rows.
- Don't mix currencies in one column without a "Currency" column.
- Never fill in a missing price or spec; leave it empty and mention it.
