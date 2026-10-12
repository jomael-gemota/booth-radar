"""
agent.py  –  Conversational Booth Radar agent.

Runs one assistant turn: Hermes either replies in plain text or calls a tool
with a <tool>{...}</tool> block. Tool results are fed back as <tool_result>
messages until Hermes answers without a tool call.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Callable

from extractor import chat_stream, extract
from scraper import scrape, snapshot

log = logging.getLogger(__name__)

AGENT_MAX_STEPS      = int(os.getenv("AGENT_MAX_STEPS", "12"))
AGENT_PAGE_CHARS     = int(os.getenv("AGENT_PAGE_CHARS", "12000"))
MAX_URLS_PER_CALL    = int(os.getenv("MAX_URLS_PER_CALL", "40"))
HISTORY_CONTEXT_ROWS = 200

Emit = Callable[[str, dict[str, Any]], None]
Row = dict[str, str]

TOOL_OPEN = "<tool>"
_TOOL_RE = re.compile(r"<tool>\s*(.*?)\s*(?:</tool>|$)", re.DOTALL)

SYSTEM_PROMPT = f"""\
You are Booth Radar, a friendly assistant that helps the team collect exhibitor
brands and their booth locations from trade-show websites. Chat naturally with
the user, answer their questions, and follow their scraping instructions.

You can use tools. To call a tool, write a short sentence saying what you are
about to do, then output exactly one block and STOP writing:

<tool>{{"name": "<tool name>", "args": {{...}}}}</tool>

Available tools:
- fetch_page  {{"url": "https://..."}}
    Loads a page in a headless browser and returns its visible text and links.
    Use it to understand a site, find the exhibitor directory, and discover
    pagination or A–Z letter pages before extracting.
- extract_booths  {{"urls": ["https://...", ...]}}
    Scrapes each URL and extracts every exhibitor brand + booth. Rows are added
    to the results table the user can export as CSV (duplicates are skipped).
    Max {MAX_URLS_PER_CALL} URLs per call; call it again for more.
- clear_results  {{}}
    Empties the results table.

After each tool call you will receive a <tool_result> message. Continue until
the user's request is done, then reply in plain text WITHOUT a tool block.

Guidelines:
- If the user just chats or asks a question, answer directly without tools.
- When a directory is split across pages (letters, page numbers, categories),
  build the full list of URLs and extract all of them, unless the user asked
  for only some.
- Never invent brands or booths. Only report what the tools returned.
- When finished, summarise what was collected (how many rows, which pages,
  any pages that failed) and remind the user they can export the table as CSV.
- Keep replies concise.
"""


# ── Streaming filter ───────────────────────────────────────────────────────

class _ToolBlockFilter:
    """Forward streamed text but withhold everything from <tool> onwards."""

    def __init__(self, forward: Callable[[str], None]) -> None:
        self._forward = forward
        self._buffer = ""
        self._stopped = False

    def feed(self, chunk: str) -> None:
        if self._stopped:
            return
        self._buffer += chunk
        idx = self._buffer.find(TOOL_OPEN)
        if idx != -1:
            self._emit(self._buffer[:idx])
            self._buffer = ""
            self._stopped = True
            return
        # Hold back a suffix that could be the start of "<tool>"
        hold = 0
        for n in range(min(len(TOOL_OPEN) - 1, len(self._buffer)), 0, -1):
            if TOOL_OPEN.startswith(self._buffer[-n:]):
                hold = n
                break
        self._emit(self._buffer[: len(self._buffer) - hold])
        self._buffer = self._buffer[len(self._buffer) - hold:]

    def flush(self) -> None:
        if not self._stopped:
            self._emit(self._buffer)
        self._buffer = ""

    def _emit(self, text: str) -> None:
        if text:
            self._forward(text)


# ── Tools ──────────────────────────────────────────────────────────────────

def _row_key(row: Row) -> tuple[str, str]:
    return row["brand"].casefold(), row["booth"].casefold()


def _tool_fetch_page(args: dict[str, Any], emit: Emit) -> str:
    url = str(args.get("url", "")).strip()
    if not url:
        return "Error: 'url' is required."
    emit("status", {"text": f"🔍 Loading {url}"})
    text, links = snapshot(url)
    shown = text[:AGENT_PAGE_CHARS]
    note = f" (showing first {AGENT_PAGE_CHARS:,})" if len(text) > AGENT_PAGE_CHARS else ""
    link_lines = "\n".join(f"- {label or '(no text)'} -> {href}" for label, href in links)
    return (
        f"URL: {url}\nVisible text: {len(text):,} chars{note}\n\n"
        f"--- TEXT ---\n{shown}\n--- END TEXT ---\n\n"
        f"--- LINKS ({len(links)}) ---\n{link_lines}\n--- END LINKS ---"
    )


def _tool_extract_booths(args: dict[str, Any], results: list[Row], emit: Emit) -> str:
    urls = args.get("urls")
    if isinstance(urls, str):
        urls = [urls]
    if not isinstance(urls, list) or not urls:
        return "Error: 'urls' must be a non-empty list of URLs."
    urls = [str(u).strip() for u in urls if str(u).strip()]
    skipped = urls[MAX_URLS_PER_CALL:]
    urls = urls[:MAX_URLS_PER_CALL]

    existing = {_row_key(r) for r in results}
    lines: list[str] = []
    for i, url in enumerate(urls, 1):
        emit("status", {"text": f"📄 Extracting {i}/{len(urls)}: {url}"})
        try:
            rows = extract(scrape(url), url)
        except Exception as exc:
            log.warning("[agent] extract failed for %s: %s", url, exc)
            lines.append(f"- {url}: FAILED ({exc})")
            continue
        added = 0
        for row in rows:
            key = _row_key(row)
            if key not in existing:
                existing.add(key)
                results.append(row)
                added += 1
        lines.append(f"- {url}: {len(rows)} rows found, {added} new")
        emit("results", {"results": list(results)})

    if skipped:
        lines.append(f"Not processed (over the {MAX_URLS_PER_CALL}-URL limit): {len(skipped)} URLs")
    lines.append(f"Results table now has {len(results)} rows.")
    return "\n".join(lines)


def _run_tool(name: str, args: dict[str, Any], results: list[Row], emit: Emit) -> str:
    if name == "fetch_page":
        return _tool_fetch_page(args, emit)
    if name == "extract_booths":
        return _tool_extract_booths(args, results, emit)
    if name == "clear_results":
        results.clear()
        emit("status", {"text": "🧹 Cleared the results table"})
        emit("results", {"results": list(results)})
        return "Results table cleared (0 rows)."
    return f"Error: unknown tool {name!r}. Available: fetch_page, extract_booths, clear_results."


# ── Turn ───────────────────────────────────────────────────────────────────

def _context_prompt(results: list[Row]) -> str:
    if not results:
        return "The results table is currently empty."
    shown = results[:HISTORY_CONTEXT_ROWS]
    rows = "\n".join(f"{r['brand']} | {r['booth']}" for r in shown)
    more = f"\n… and {len(results) - len(shown)} more" if len(results) > len(shown) else ""
    return f"The results table currently has {len(results)} rows (brand | booth):\n{rows}{more}"


def run_turn(
    history: list[dict[str, str]],
    results: list[Row],
    emit: Emit,
) -> tuple[str, list[Row]]:
    """
    Run one assistant turn over *history* (user/assistant messages, last one
    from the user). Returns (reply_text, updated_results).
    """
    results = [dict(r) for r in results]
    convo: list[dict[str, str]] = [
        {"role": "system", "content": f"{SYSTEM_PROMPT}\n\n{_context_prompt(results)}"},
        *({"role": m["role"], "content": m["content"]} for m in history),
    ]
    reply_parts: list[str] = []

    for step in range(AGENT_MAX_STEPS):
        filt = _ToolBlockFilter(lambda t: emit("token", {"text": t}))
        text = chat_stream(convo, filt.feed)
        filt.flush()

        idx = text.find(TOOL_OPEN)
        visible = (text if idx == -1 else text[:idx]).strip()
        if visible:
            reply_parts.append(visible)
        if idx == -1:
            break

        match = _TOOL_RE.search(text, idx)
        raw_call = match.group(1) if match else ""
        convo.append({"role": "assistant", "content": text[: match.end() if match else len(text)]})

        try:
            call = json.loads(raw_call)
            name = str(call.get("name", ""))
            args = call.get("args") or {}
            if not isinstance(args, dict):
                raise ValueError("'args' must be an object")
        except (json.JSONDecodeError, ValueError, AttributeError) as exc:
            observation = f"Error: could not parse tool call ({exc}). Use valid JSON."
            name = "invalid"
        else:
            log.info("[agent] step %d tool=%s args=%s", step + 1, name, args)
            try:
                observation = _run_tool(name, args, results, emit)
            except Exception as exc:
                log.warning("[agent] tool %s failed: %s", name, exc)
                observation = f"Error: {name} failed: {exc}"
                emit("status", {"text": f"⚠️ {name} failed: {exc}"})

        convo.append({
            "role": "user",
            "content": f"<tool_result name=\"{name}\">\n{observation}\n</tool_result>",
        })
    else:
        note = f"(Stopped after {AGENT_MAX_STEPS} tool steps — ask me to continue if needed.)"
        emit("token", {"text": note})
        reply_parts.append(note)

    return "\n\n".join(reply_parts), results
