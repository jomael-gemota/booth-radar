# Default Fields for Style and Product Requests

**Date:** 2026-11-27
**Status:** accepted
**Author:** collaborative

## Context

When a user asks Booth Radar to scrape a style (a product page or a brand's styles),
the agent only collects the columns the user names. The team always needs the same
core details, so users have to list them every time and often forget some.

Columns are chosen by the agent when it calls `extract`, `read_feed` or `start_job`
(see [custom fields and site feeds](2026-11-10-custom-fields-and-site-feeds.md)); the
strategy lives in the `product-catalog` skill
([Hermes skills](2026-11-12-hermes-skills.md)). Two things work against the request
today:

- `product-catalog` says "one row per product unless the user asks for one row per
  variant", so sizes, colours and their prices are collapsed.
- `site_feeds._flatten_shopify` reduces variants to `price_min` / `price_max` and a
  list of variant titles, so the per-variant price, SKU and availability never reach
  the extraction model.

## Decision

### 1. Default style fields

For any request to scrape a style or product (single page, list of pages or whole
catalog — agreed to apply to whole catalogs too), the agent always collects these
columns, plus any extra fields the user asks for (using the user's names):

| Column | Contents |
|--------|----------|
| Product Title | Product name as shown |
| Style Number | Style / model / item number (not the variant SKU) |
| Style Name | Style or model name when the site gives one apart from the title |
| Description | Main description text |
| Bullets | Feature bullets, joined with " \| " |
| Specs | Specification table, `Name: value` pairs joined with "; " |
| Color | Variant colour |
| Size | Variant size (including width when shown, e.g. "10 W") |
| Price | Variant price as shown (sale price when shown, plus "Regular Price" if both appear) |
| Stock | Stock quantity or availability ("In stock", "12 left") when shown |
| Variant Reviews | Rating and review count for this variant when the site shows them |
| URL | The style's own page URL (the product page, not the listing it was found on) |

Values that aren't on the page stay empty and are mentioned in the summary; they are
never guessed. If the user explicitly asks for only certain fields ("just the
prices"), only those are collected.

### 2. One row per variant

Rows are one per colour × size combination, with the style-level columns repeated on
each row, and `key_fields=["URL", "Color", "Size"]` so later passes merge into the same
rows. A style with no variants is one row with Color and Size empty.

Repeating description, bullets and specs on every variant row in one extraction would
overflow the model's output (`MAX_TOKENS`, 8192) on styles with dozens of variants, so
collection runs in two passes:

1. **Variant rows:** Product Title, Style Number, Style Name, Color, Size, Price,
   Stock, Variant Reviews, URL (plus per-variant extras), keyed on URL + Color + Size.
2. **Style text:** Description, Bullets, Specs (plus style-level extras) with
   `url_template="{URL}"`: each distinct page is read once (cached) and `ResultsTable.fill`
   copies its values into every row with that URL.

So the long text columns come after URL in the table.

### `url_template="{URL}"`

`_template_parts` accepted only `http…{Column}…` templates. A bare `{Column}` template
now uses the column's page URLs as they are (no quoting), for both `extract` and
`start_job`.

### 3. Where the rule lives

- `product-catalog/SKILL.md`: new "Default style fields" section with the table above,
  the row layout, the `instructions` text to pass, and how to fill details that feeds
  lack (extract the product pages with the same columns, merging on the keys).
- `site-scraping/SKILL.md` and the mcp-mode strategy summary in `agent.py`: one line
  saying style/product requests always use the default style fields and load
  `product-catalog`, so the rule holds even when Hermes skips `skill_view`.
- Tool docs for `extract` and `start_job` (`agent.py`, `mcp_server.py`) mention
  `url_template="{URL}"`.

### 4. Variant data from Shopify feeds

`find_feeds` lists a second Shopify feed, `shopify:variants`, read from the same
`products.json`: one record per variant with `product_title`, `url`, `variant_url`,
`sku`, `price`, `regular_price`, `stock` ("In stock" / "Out of stock"),
`inventory_quantity` when public, and each option under the shop's own name (`Color`,
`Size`, `Width`). Keys are named like the default columns, so `read_feed` maps them
with the field mapping (no model call per batch) and returns one row per variant. A
`variant_details` list on the products feed was rejected: the model would have to
expand 40 products × N variants per batch, which overflows its output.

## Alternatives Considered

- **Hard-code the default fields in the worker** (`extract` adds them when the request
  looks like a product): enforced every time, but the worker can't tell a style
  request from an exhibitor or dealer request, and it would add product columns to
  unrelated tables.
- **One row per style with all variations in one cell** ("Black / 10: $129; …"):
  shorter tables, but prices and stock can't be sorted or filtered in the CSV, and
  per-variant reviews have nowhere to go.

## Consequences

- Style requests return more columns and many more rows (one per variant).
- Bullets, specs and reviews are rarely in feeds, so whole-catalog requests need each
  product page extracted (a background job above 100 pages); expect them to take longer
  than a feed-only read.
- Per-variant reviews and stock quantities are often only loaded by JavaScript or not
  shown at all; those columns will often be empty.
