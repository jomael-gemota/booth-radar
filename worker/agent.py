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
import threading
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from typing import Any, Callable

import page_cache
from extractor import chat_stream, extract
from scraper import SCRAPE_CONCURRENCY, load_page, scrape

log = logging.getLogger(__name__)

AGENT_MAX_STEPS      = int(os.getenv("AGENT_MAX_STEPS", "12"))
AGENT_PAGE_CHARS     = int(os.getenv("AGENT_PAGE_CHARS", "12000"))
MAX_URLS_PER_CALL    = int(os.getenv("MAX_URLS_PER_CALL", "40"))
HISTORY_CONTEXT_ROWS = 200

Emit = Callable[[str, dict[str, Any]], None]
Row = dict[str, str]


class TurnCancelled(Exception):
    """Raised inside run_turn() when the cancel event is set."""

TOOL_OPEN = "<tool>"
_TOOL_RE = re.compile(r"<tool>\s*(.*?)\s*(?:</tool>|$)", re.DOTALL)

SYSTEM_PROMPT = f"""\
You are Booth Radar, a friendly assistant that helps the team collect exhibitor
brands and their booth locations from trade-show websites. You are genuinely
conversational — you talk WITH the user, not just at them.

Personality:
- You are warm, upbeat, and genuinely happy to help. You enjoy hunting down
  booths and it shows: greet people cheerfully, celebrate finished scrapes
  ("Done! Found 312 brands across 10 pages 🎉"), and thank users for their requests.
- Sound like an enthusiastic teammate, not a robot. Use a natural, positive tone,
  exclamation points now and then, and at most one or two emoji per reply.
- Stay cheerful when things go wrong: explain failures honestly and kindly, and
  suggest a next step.
- Enthusiasm never replaces accuracy or brevity. Keep numbers exact and replies
  concise, and don't pad answers with filler.

────────────────────────────────────────────────────────────
RULE: Always talk first, act second.
────────────────────────────────────────────────────────────
Before EVERY tool call you MUST write a natural, conversational paragraph that:
  1. Acknowledges what the user asked for.
  2. Tells them exactly what you are about to do and why.
  3. Sets honest expectations (e.g. number of pages, time it might take).

Good examples:
  "Great, I've got the URL! I'll start by loading the directory page to see how
   it's laid out — whether it's paginated, alphabetical, or all on one page.
   Give me just a moment 🔍"

  "Looks like there are 10 letter tabs (A–Z minus a few). I'll scrape all of
   them so we get the complete list — this might take a minute or two!"

  "Before I extract, let me quickly fetch the overview page to make sure I'm
   grabbing the right exhibitor section and not a sponsor list."

Bad (do NOT do this): jumping straight into a tool call with no message, or
writing only "Fetching..." before the block — that's not conversational.

If the request is ambiguous or a URL is missing, ask ONE focused clarifying
question instead of guessing. Do not use any tool until you have what you need.

After EVERY tool result, if you are about to use another tool, explain what
you learned and what you are going to do next before calling the next tool.

────────────────────────────────────────────────────────────
Tools
────────────────────────────────────────────────────────────
To call a tool, write your conversational message first, then output exactly
one block and STOP:

<tool>{{"name": "<tool name>", "args": {{...}}}}</tool>

Available tools:
- fetch_page  {{"url": "https://...", "offset": 0, "refresh": false}}
    Loads a page in a headless browser and returns its visible text and links.
    Use it to understand a site, find the exhibitor directory, and discover
    pagination or A–Z letter pages before extracting. Long pages are cut at
    {AGENT_PAGE_CHARS:,} chars; pass "offset" to read further.
- extract_booths  {{"urls": ["https://...", ...], "refresh": false}}
    Scrapes each URL and extracts every exhibitor brand + booth. Rows are added
    to the results table the user can export as CSV (duplicates are skipped).
    Max {MAX_URLS_PER_CALL} URLs per call; call it again for more.

Pages loaded in the last hour are cached and return instantly. To answer
follow-up questions about a site (other details on a page, a page you already
looked at), call fetch_page again instead of guessing. Only set "refresh": true
when the user asks for fresh or updated data.
- clear_results  {{}}
    Empties the results table.

After each tool call you will receive a <tool_result> message. Continue until
the user's request is done, then reply in plain text WITHOUT a tool block.

────────────────────────────────────────────────────────────
Guidelines
────────────────────────────────────────────────────────────
- If the user just chats or asks a question, answer directly without tools.
- When a directory is split across pages (letters, page numbers, categories),
  build the full list of URLs and extract all of them, unless the user asked
  for only some.
- Never invent brands or booths. Only report what the tools returned.
- When finished, summarise what was collected: which site, how many rows,
  which pages were covered, and any pages that failed.
- Only mention the results table or its Export CSV button if you added rows
  with extract_booths in this turn.
- Never mention file paths, folders, the server, your tools, or which actions
  were or weren't available. Speak only about the results and the user's request.
- Keep replies concise — conversational does not mean verbose.
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


def _flag(args: dict[str, Any], name: str) -> bool:
    value = args.get(name)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def _tool_fetch_page(args: dict[str, Any], emit: Emit) -> str:
    url = str(args.get("url", "")).strip()
    if not url:
        return "Error: 'url' is required."
    try:
        offset = max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        return "Error: 'offset' must be a whole number."
    refresh = _flag(args, "refresh")

    emit("status", {"text": f"🔍 Loading {url}"})
    text, links, cached = load_page(url, refresh)
    shown = text[offset:offset + AGENT_PAGE_CHARS]
    end = offset + len(shown)
    note = ""
    if offset or end < len(text):
        more = f"; pass offset={end} for more" if end < len(text) else ""
        note = f" (showing chars {offset:,}–{end:,}{more})"
    source = " (from cache)" if cached else ""
    link_lines = "\n".join(f"- {label or '(no text)'} -> {href}" for label, href in links)
    return (
        f"URL: {url}{source}\nVisible text: {len(text):,} chars{note}\n\n"
        f"--- TEXT ---\n{shown}\n--- END TEXT ---\n\n"
        f"--- LINKS ({len(links)}) ---\n{link_lines}\n--- END LINKS ---"
    )


def _extract_url(url: str, refresh: bool) -> list[Row]:
    text = scrape(url, refresh)
    key = (page_cache.url_key(url), page_cache.text_hash(text))
    rows = None if refresh else page_cache.extractions.get(key)
    if rows is None:
        rows = extract(text, url)
        page_cache.extractions.set(key, rows)
    else:
        log.info("[agent] extraction cache hit for %s", url)
    return [dict(r) for r in rows]


def _tool_extract_booths(args: dict[str, Any], results: list[Row], emit: Emit) -> str:
    urls = args.get("urls")
    if isinstance(urls, str):
        urls = [urls]
    if not isinstance(urls, list) or not urls:
        return "Error: 'urls' must be a non-empty list of URLs."
    urls = [str(u).strip() for u in urls if str(u).strip()]
    if not urls:
        return "Error: 'urls' must be a non-empty list of URLs."
    skipped = urls[MAX_URLS_PER_CALL:]
    urls = urls[:MAX_URLS_PER_CALL]
    refresh = _flag(args, "refresh")

    existing = {_row_key(r) for r in results}
    lines: list[str] = []
    outcomes: dict[int, list[Row] | Exception] = {}
    next_to_merge = 0

    def merge_ready() -> None:
        # Merge in URL order so de-duplication matches sequential processing.
        nonlocal next_to_merge
        while next_to_merge in outcomes:
            url, outcome = urls[next_to_merge], outcomes.pop(next_to_merge)
            next_to_merge += 1
            if isinstance(outcome, Exception):
                lines.append(f"- {url}: FAILED ({outcome})")
                continue
            added = 0
            for row in outcome:
                key = _row_key(row)
                if key not in existing:
                    existing.add(key)
                    results.append(row)
                    added += 1
            lines.append(f"- {url}: {len(outcome)} rows found, {added} new")

    emit("status", {"text": f"📄 Extracting {len(urls)} page{'s' if len(urls) != 1 else ''}…"})
    with ThreadPoolExecutor(max_workers=min(SCRAPE_CONCURRENCY, len(urls))) as pool:
        futures: dict[Future[list[Row]], int] = {
            pool.submit(_extract_url, url, refresh): i for i, url in enumerate(urls)
        }
        for done, future in enumerate(as_completed(futures), 1):
            i = futures[future]
            try:
                outcomes[i] = future.result()
                emit("status", {"text": f"📄 Extracted {done}/{len(urls)}: {urls[i]}"})
            except Exception as exc:
                log.warning("[agent] extract failed for %s: %s", urls[i], exc)
                outcomes[i] = exc
                emit("status", {"text": f"⚠️ Failed {done}/{len(urls)}: {urls[i]}"})
            before = len(results)
            merge_ready()
            if len(results) != before:
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


# ── Preambles ──────────────────────────────────────────────────────────────

NATIVE_TOOL_PREAMBLE = "On it! Give me a moment while I look into this 🔍"


def _preamble_for(name: str, args: dict[str, Any]) -> str:
    """Fallback message shown when Hermes calls a tool without saying anything first."""
    if name == "fetch_page":
        url = str(args.get("url", "")).strip()
        if args.get("offset"):
            return f"Let me read a bit further down {url} 📖"
        return f"Let me open {url} and see how it's laid out 🔍"
    if name == "extract_booths":
        urls = args.get("urls")
        count = 1 if isinstance(urls, str) else len(urls) if isinstance(urls, list) else 0
        if count > 1:
            return f"Great, I'll collect the brands and booth numbers from {count} pages now — this may take a moment! 📄"
        return "Great, I'll collect the brands and booth numbers from that page now 📄"
    if name == "clear_results":
        return "Sure! I'll clear the results table so we can start fresh 🧹"
    return "Give me a moment while I work on that 🔍"


# ── Turn ───────────────────────────────────────────────────────────────────

def _context_prompt(results: list[Row]) -> str:
    if not results:
        return "The results table is currently empty."
    shown = results[:HISTORY_CONTEXT_ROWS]
    rows = "\n".join(f"{r['brand']} | {r['booth']}" for r in shown)
    more = f"\n… and {len(results) - len(shown)} more" if len(results) > len(shown) else ""
    return f"The results table currently has {len(results)} rows (brand | booth):\n{rows}{more}"


def _output_prompt(output_dir: str | None) -> str:
    if not output_dir:
        return ""
    return (
        "\n\nWhen you produce a file for the user (CSV, XLSX, JSON, TXT, MD), save it in\n"
        f"{output_dir}/ with a descriptive file name. Files saved there are attached\n"
        "to your reply as downloads. In your reply, refer to the file only by its name\n"
        "(never its path) and tell the user it is attached below for download."
    )


def run_turn(
    history: list[dict[str, str]],
    results: list[Row],
    emit: Emit,
    output_dir: str | None = None,
    cancel: threading.Event | None = None,
) -> tuple[str, list[Row]]:
    """
    Run one assistant turn over *history* (user/assistant messages, last one
    from the user). Returns (reply_text, updated_results).

    Raises TurnCancelled if *cancel* is set before or between steps.
    """
    results = [dict(r) for r in results]
    system = f"{SYSTEM_PROMPT}{_output_prompt(output_dir)}\n\n{_context_prompt(results)}"
    convo: list[dict[str, str]] = [
        {"role": "system", "content": system},
        *({"role": m["role"], "content": m["content"]} for m in history),
    ]
    reply_parts: list[str] = []

    for step in range(AGENT_MAX_STEPS):
        if cancel and cancel.is_set():
            raise TurnCancelled()

        spoke = False
        native_preamble = ""

        def on_visible(t: str) -> None:
            nonlocal spoke
            if t.strip():
                spoke = True
            emit("token", {"text": t})

        def on_tool_progress(t: str) -> None:
            # Hermes is running one of its own tools; make sure the user hears from us first.
            nonlocal spoke, native_preamble
            if not spoke:
                spoke = True
                native_preamble = NATIVE_TOOL_PREAMBLE
                emit("token", {"text": native_preamble + "\n\n"})
            emit("thinking", {"text": t})

        filt = _ToolBlockFilter(on_visible)
        text = chat_stream(
            convo,
            on_token=filt.feed,
            on_thinking=lambda t: emit("thinking", {"text": t}),
            cancel=cancel,
            on_tool_progress=on_tool_progress,
        )
        filt.flush()

        if cancel and cancel.is_set():
            raise TurnCancelled()

        idx = text.find(TOOL_OPEN)
        visible = (text if idx == -1 else text[:idx]).strip()
        if native_preamble:
            visible = f"{native_preamble}\n\n{visible}".strip()
        if idx == -1:
            if visible:
                reply_parts.append(visible)
            break

        match = _TOOL_RE.search(text, idx)
        raw_call = match.group(1) if match else ""

        try:
            call = json.loads(raw_call)
            name = str(call.get("name", ""))
            args = call.get("args") or {}
            if not isinstance(args, dict):
                raise ValueError("'args' must be an object")
        except (json.JSONDecodeError, ValueError, AttributeError) as exc:
            call_ok = False
            observation = f"Error: could not parse tool call ({exc}). Use valid JSON."
            name = "invalid"
        else:
            call_ok = True

        if call_ok and not visible:
            visible = _preamble_for(name, args)
            log.info("[agent] step %d: tool call had no preamble, using fallback", step + 1)
            emit("token", {"text": visible})
        if visible:
            reply_parts.append(visible)

        raw_assistant = text[idx: match.end() if match else len(text)]
        convo.append({"role": "assistant", "content": f"{visible}\n\n{raw_assistant}".strip()})

        if call_ok:
            log.info("[agent] step %d tool=%s args=%s", step + 1, name, args)
            try:
                observation = _run_tool(name, args, results, emit)
            except TurnCancelled:
                raise
            except Exception as exc:
                log.warning("[agent] tool %s failed: %s", name, exc)
                observation = f"Error: {name} failed: {exc}"
                emit("status", {"text": f"⚠️ {name} failed: {exc}"})

        if cancel and cancel.is_set():
            raise TurnCancelled()

        convo.append({
            "role": "user",
            "content": f"<tool_result name=\"{name}\">\n{observation}\n</tool_result>",
        })
    else:
        note = f"(Stopped after {AGENT_MAX_STEPS} tool steps — ask me to continue if needed.)"
        emit("token", {"text": note})
        reply_parts.append(note)

    return "\n\n".join(reply_parts), results
