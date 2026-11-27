# Product Results as a Download Only

**Date:** 2026-11-28
**Status:** accepted
**Author:** collaborative

## Context

The chat shows the results table inline up to 20 rows (`MAX_INLINE_ROWS` in
`ChatBox.tsx`) and a "Download CSV" card above that
([large results](2026-11-12-large-results-and-stream-resilience.md)). Since the
[default style fields](2026-11-27-default-style-fields.md), style and product tables
carry Description, Bullets and Specs on every row, so even a few rows make a tall,
hard-to-read table in the chat.

## Decision

Style and product results are never shown as a table in the chat; they always get the
download card, whatever the row count.

The chat only sees the results, not the request, so a table counts as a product table
when its columns include any default style column that only product tables have:
Product Title, Style Number, Style Name, Description, Bullets or Specs (case-insensitive).
Tables whose names the user changed (e.g. "Name" for Product Title) still match on the
long-text columns, which are almost always present.

The card says the product details are in the CSV file instead of "Too many rows".
The agent guideline about the table is updated so the reply points to the Download CSV
button for product tables of any size.

## Alternatives Considered

- **Flag set by the agent or worker** ("this turn is a product request"): matches the
  request exactly, but needs a new field through worker, server and stored
  conversations, and the flag would have to be reconstructed for old conversations.
- **Hide any table with long cells**: also tidies non-product tables with long text,
  but changes behaviour beyond what was asked; can be added later if needed.

## Consequences

- A short product lookup (one style, a few prices) also becomes a download; the
  reply still summarises what was found.
- A non-product table that uses one of these column names (e.g. "Description" in a
  dealer list) is also download-only.
