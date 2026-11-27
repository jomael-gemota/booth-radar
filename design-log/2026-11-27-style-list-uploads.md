# Style List Uploads (CSV, Excel, TXT)

**Date:** 2026-11-27
**Status:** accepted
**Author:** collaborative

## Context

Users often already have the styles they want scraped in a spreadsheet or text file
(style numbers, names, product URLs). Today they have to paste the list into the chat,
which is capped at 8,000 characters (`MAX_MESSAGE_CHARS` in `routes/chat.ts`), loses
the spreadsheet's columns, and is awkward for more than a few dozen styles.

Once the agent has the list, the existing tools already cover the work: `extract`
takes up to 40 URLs per call, `start_job` takes up to 5,000 URLs in the background,
and the [default style fields](2026-11-27-default-style-fields.md) decide the columns.
What's missing is getting the file's contents to the agent.

## Decision

### 1. Attach a file in the chat input

A paperclip button next to the input opens a file picker for `.csv`, `.xlsx`, `.txt`
and `.tsv` (dropping a file on the input also works). The chosen file shows as a chip
above the textarea (name, row count, remove button) and is sent with the next message.
The user can type instructions alongside it ("get these from merrell.com"); with no
text, the message is "Scrape the styles in the attached file."

### 2. Parse in the browser

The browser turns the file into rows (`string[][]`) before sending, so the server and
worker never handle spreadsheet formats:

| Type | Parsing |
|------|---------|
| `.csv` | RFC 4180 parser (quoted fields, embedded commas/newlines); `;` delimiter detected when the first line has more `;` than `,` (European Excel exports) |
| `.tsv` | Tab-separated |
| `.txt` | One style per line; lines with tabs are split into columns |
| `.xlsx` | First worksheet, via [`read-excel-file`](https://www.npmjs.com/package/read-excel-file) |

Empty rows and trailing empty columns are dropped; a UTF-8 BOM is stripped. The first
row is kept as-is: the agent decides whether it is a header (lists of bare style
numbers often have none), so no header-guessing heuristic is needed.

Old binary `.xls` files are not supported; the picker says to save as `.xlsx` or CSV.

### 3. Send and store the rows with the user message

`POST /api/chat` accepts `upload: { name, rows }`. The server validates it (at most
1,000 rows, 50 columns, 60,000 characters of cell text) and stores it on the user
message (`ChatMessage.upload`). Conversation reads return only `{ name, rows: count }`
so the user bubble shows a file chip; the rows themselves are not sent back.

When building the history for the worker, the server appends the rows to that
message's content:

```
<uploaded_file name="styles.xlsx" rows="124">
Style Number | Color
M12345 | Black
…
</uploaded_file>
```

The block stays in the history window on later turns, so "now do the rest" still
works. The 8,000-character limit applies to the typed text only; the JSON body limit
on `/api/chat` is raised to 2 MB.

### 4. Agent guidance

`product-catalog/SKILL.md` gets an "Uploaded style lists" section, and the guidelines
in `agent.py` get one line pointing to it:

- Use every row of the file; say how many styles it has.
- Rows with product URLs: run the usual two passes on those URLs (`start_job` above
  100 pages).
- Style numbers or names only: ask for the site if the user didn't give one; then read
  the site's feed with `instructions` limiting it to the listed styles, or find each
  style through the site's search. Report the styles that weren't found.
- Keep the file's own columns (e.g. "Style Number") as the user's names for those
  columns.

## Alternatives Considered

- **Upload the file to the server and parse it in the worker (`openpyxl`)**: supports
  more formats, but needs a binary upload path through the server and the WebSocket,
  a new Python dependency, and still ends with the same rows in the prompt.
- **Load the rows into the results table** so `url_template` / `url_column` can read
  them: no prompt growth, but it overwrites or mixes with the conversation's results,
  input column names leak into the output, and one-row-per-variant output can't grow
  from one-row-per-style input through `fill`.
- **SheetJS (`xlsx`) for Excel**: reads `.xls` too, but the npm release is outdated
  with known advisories (newer builds are only on its own CDN), and it is much larger.

## Consequences

- Large lists cost prompt space on every turn of that conversation (60,000 characters
  ≈ 15k tokens at the cap). Lists over 1,000 rows must be split across conversations.
- The agent still writes the URLs into its `extract` / `start_job` calls, so very long
  URL lists take a while to generate.
- New client dependency: `read-excel-file`.
