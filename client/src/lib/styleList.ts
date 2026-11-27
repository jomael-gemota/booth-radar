import { readSheet } from 'read-excel-file/browser'

/** A style list file read into rows of cell text, as sent with a chat message. */
export interface StyleList {
  name: string
  rows: string[][]
}

export const STYLE_LIST_ACCEPT = '.csv,.tsv,.txt,.xlsx'

// Keep in step with the limits in server/src/routes/chat.ts
const MAX_FILE_BYTES = 5 * 1024 * 1024
const MAX_ROWS = 1000
const MAX_COLUMNS = 50
const MAX_CHARS = 60_000

/** Read a CSV, TSV, TXT or XLSX file into rows; throws an Error with a user-facing message. */
export async function readStyleList(file: File): Promise<StyleList> {
  const ext = file.name.split('.').pop()?.toLowerCase() ?? ''
  if (ext === 'xls') {
    throw new Error('Old Excel (.xls) files aren\'t supported. Save the file as .xlsx or CSV and attach it again.')
  }
  if (!['csv', 'tsv', 'txt', 'xlsx'].includes(ext)) {
    throw new Error('Attach a CSV, Excel (.xlsx) or TXT file.')
  }
  if (file.size > MAX_FILE_BYTES) {
    throw new Error('The file is larger than 5 MB.')
  }

  let rows: string[][]
  if (ext === 'xlsx') {
    rows = await readXlsx(file)
  } else {
    const text = (await file.text()).replace(/^\uFEFF/, '')
    rows = ext === 'txt' ? parseLines(text) : parseDelimited(text, ext === 'tsv' ? '\t' : detectDelimiter(text))
  }

  rows = tidy(rows)
  if (!rows.length) throw new Error('The file has no styles in it.')
  if (rows.length > MAX_ROWS) {
    throw new Error(`The file has ${rows.length.toLocaleString()} rows; the limit is ${MAX_ROWS.toLocaleString()}. Split it into smaller files.`)
  }
  if (rows[0].length > MAX_COLUMNS) {
    throw new Error(`The file has ${rows[0].length} columns; the limit is ${MAX_COLUMNS}.`)
  }
  const chars = rows.reduce((n, r) => n + r.reduce((m, c) => m + c.length, 0), 0)
  if (chars > MAX_CHARS) {
    throw new Error('The file has too much text for one request. Remove columns that aren\'t needed or split it.')
  }
  return { name: file.name, rows }
}

async function readXlsx(file: File): Promise<string[][]> {
  let data
  try {
    data = await readSheet(file)
  } catch {
    throw new Error('Couldn\'t read the Excel file. Check that it opens in Excel, or save it as CSV.')
  }
  return data.map((row) => row.map((cell) => {
    if (cell == null) return ''
    if (cell instanceof Date) return cell.toISOString().slice(0, 10)
    return String(cell)
  }))
}

/** TXT: one style per line; tab-separated lines become columns. */
function parseLines(text: string): string[][] {
  return text.split(/\r?\n/).map((line) => line.split('\t'))
}

/** European Excel exports CSV with ";" when "," is the decimal separator. */
function detectDelimiter(text: string): string {
  const first = text.slice(0, text.search(/\r?\n|$/))
  const count = (ch: string) => first.split(ch).length - 1
  if (count('\t') > count(',') && count('\t') > count(';')) return '\t'
  return count(';') > count(',') ? ';' : ','
}

/** RFC 4180: quoted fields may hold the delimiter, newlines and doubled quotes. */
function parseDelimited(text: string, delimiter: string): string[][] {
  const rows: string[][] = []
  let row: string[] = []
  let field = ''
  let quoted = false
  for (let i = 0; i < text.length; i++) {
    const ch = text[i]
    if (quoted) {
      if (ch === '"' && text[i + 1] === '"') { field += '"'; i++ }
      else if (ch === '"') quoted = false
      else field += ch
    } else if (ch === '"' && field === '') {
      quoted = true
    } else if (ch === delimiter) {
      row.push(field)
      field = ''
    } else if (ch === '\n' || ch === '\r') {
      if (ch === '\r' && text[i + 1] === '\n') i++
      row.push(field)
      rows.push(row)
      row = []
      field = ''
    } else {
      field += ch
    }
  }
  row.push(field)
  rows.push(row)
  return rows
}

/** Trim cells, drop empty rows and trailing empty columns. */
function tidy(rows: string[][]): string[][] {
  const cleaned = rows
    .map((r) => r.map((c) => c.replace(/\s+/g, ' ').trim()))
    .filter((r) => r.some(Boolean))
  const width = cleaned.reduce((max, r) => {
    let last = r.length
    while (last > max && !r[last - 1]) last--
    return Math.max(max, last)
  }, 0)
  return cleaned.map((r) => {
    const cells = r.slice(0, width)
    while (cells.length < width) cells.push('')
    return cells
  })
}
