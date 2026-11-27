---
name: product-catalog
description: Collect styles and products (default style fields, one row per color and size) with specs and prices from shop and brand sites, merge details from product pages into catalog rows, and compare a product across a few merchant sites. Load for any style, product, spec or price request.
version: 1.1.0
metadata:
  hermes:
    tags: [booth-radar, scraping, products, prices]
    requires_toolsets: [mcp-booth-radar]
---

# Product catalog

Use this for styles, product lists, specifications and prices.

## Default style fields

Whenever the user asks to scrape a style or product — one page, a few pages or a
whole catalog — ALWAYS collect these columns, even if the user didn't ask for them:

| Column | Contents |
|--------|----------|
| Product Title | Product name as shown |
| Style Number | Style / model / item number (not the variant SKU) |
| Style Name | Style or model name, when the site gives one apart from the title |
| Description | Main description text |
| Bullets | Feature bullets, joined with " \| " |
| Specs | Specification table, "Name: value" pairs joined with "; " |
| Color | Variant colour |
| Size | Variant size, with width when shown ("10 W") |
| Price | This variant's price as shown |
| Stock | Stock quantity or availability for this variant, when shown |
| Variant Reviews | Rating and review count for this variant, when shown |
| URL | The style's own product page, not the listing it was found on |

- Fields the user asks for that are not in the list are added, named as the user
  said them ("Heel Height", "Country of Origin"). If the user's name matches
  a default column ("SKU" for Style Number, "Name" for Product Title), use the user's
  name in that column's place.
- Only skip the defaults when the user clearly asks for nothing else ("just the
  prices").
- One row per colour × size combination, with the style columns repeated on each row.
  A style without variants is one row with Color and Size empty.
- Leave a value empty when the site doesn't show it (stock and per-variant reviews
  are often missing). Never guess. Say in the summary which columns the site doesn't
  provide.

### Two passes

Long text repeated on every variant row would overflow one extraction, so collect
the variant rows first and then fill the long style text once per page:

1. **Variant rows**:
   `extract(urls=[...], fields=["Product Title", "Style Number", "Style Name", "Color",
   "Size", "Price", "Stock", "Variant Reviews", "URL"], key_fields=["URL", "Color",
   "Size"], instructions="One row per color and size combination; list every variant
   the page offers. Price, Stock and Variant Reviews belong to that variant.")`
   Add the user's extra per-variant fields (e.g. "UPC", "Width") to this pass.
2. **Style text**:
   `extract(url_template="{URL}", fields=["Description", "Bullets", "Specs"],
   instructions="One row for the style. Bullets joined with ' | ', specs as
   'Name: value' joined with '; '.")`
   Each page is read once (from cache) and fills every variant row with its URL. Add
   the user's extra style-level fields (e.g. "Materials", "Country of Origin") here.

Sizes and colours often sit in dropdowns, swatches or JSON-LD offers. If pass 1 only
returns one row but the page shows a size or colour picker, read the variants from the
page's JSON-LD with `fetch_page`, or use `navigate` and the interaction tools.

## Whole catalog

1. `find_feeds` first. WordPress (WooCommerce) and Shopify shops usually publish the
   whole catalog: `read_feed` gives names, URLs, prices and often SKUs in seconds.
   On Shopify, read `shopify:variants` (one record per variant with its options,
   price, SKU and stock) with the pass 1 columns and
   `key_fields=["URL", "Color", "Size"]`, and no `instructions`.
2. No feed: `capture_api` on a category or search page, then `read_feed`.
3. Feeds usually lack bullets, specs, style numbers and reviews. Fill them in from
   each product page with the pass 2 call, plus "Style Number" and "Style Name" when
   the feed left them empty:
   - up to 100 products: `extract(url_template="{URL}", fields=[...])`;
   - more: `start_job(url_template="{URL}", fields=[...])`.
   Rows merge on URL, so the table keeps one row per variant.
4. No feed and no API: list the product URLs (`map_site` or listing pages) and run
   both passes; for more than 100 products run pass 1 with
   `start_job(urls=[...], fields=[...pass 1 columns...], key_fields=["URL", "Color",
   "Size"])` and pass 2 in a later turn, once the job is done.

## Uploaded style lists

A user message can end with an `<uploaded_file name="…" rows="…">` block: the rows of a
CSV, Excel or TXT file the user attached, one row per line, cells joined with " | ".

- The first line may be a header ("Style Number | Color") or already a style (lists
  of bare style numbers often have no header); decide from the values.
- Work through EVERY row, not a sample. Tell the user how many styles the file has.
- Rows with product page URLs: run both passes on those URLs (`start_job` with
  `urls` for more than 100 pages).
- Style numbers or names only: if the user didn't say which site, ask once. Then
  read the site's feed (`find_feeds`, `read_feed`) with `instructions` like "Only
  styles whose style number is one of: …", or find each style's page through the
  site's search (`navigate` + `fill`, or a search URL with `fetch_page`), and run both
  passes on the pages found.
- Keep the file's column names for the matching columns ("Item #" instead of "Style
  Number"), and carry over other columns the user wants kept (e.g. their own notes).
- In the summary, list the styles from the file that weren't found on the site.

## Columns

- Keep the names the user used ("Style Number", not "SKU", if they said style number).
- Prices: one "Price" column with the number as shown; add a "Currency" column when
  the site or the user's request involves more than one currency. Use the sale price
  when shown, and add a "Regular Price" column when the page shows both.

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
