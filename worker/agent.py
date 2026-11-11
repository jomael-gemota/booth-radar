"""
agent.py  –  Conversational Booth Radar agent.

Runs one assistant turn. In inline mode Hermes either replies in plain text or
calls a tool with a <tool>{...}</tool> block; tool results are fed back as
<tool_result> messages until Hermes answers without a tool call. In mcp mode
Hermes calls the tools natively through mcp_server.py and tool_bridge.py, and
the whole turn is a single Hermes request.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from typing import Any, Callable
from urllib.parse import quote, unquote

import jobs
import page_cache
import recipes
import site_feeds
import skills
import tool_bridge
from extractor import chat_stream, extract, quick_completion
from results_table import ResultsTable
from browser import StaleRef, TurnSessions
from scraper import SCRAPE_CONCURRENCY, load_markup, load_page, parse_html, scrape, used_browser

log = logging.getLogger(__name__)

# "inline": Hermes calls tools with <tool> blocks parsed here.
# "mcp": Hermes calls the booth-radar MCP server natively (needs the VM setup in README).
AGENT_TOOL_MODE      = os.getenv("AGENT_TOOL_MODE", "inline").strip().lower()
AGENT_STREAM_TIMEOUT = int(os.getenv("AGENT_STREAM_TIMEOUT", "900"))
AGENT_MAX_STEPS      = int(os.getenv("AGENT_MAX_STEPS", "12"))
ACK_WAIT_SECONDS     = float(os.getenv("ACK_WAIT_SECONDS", "4"))
ACK_HISTORY_MESSAGES = 6
AGENT_PAGE_CHARS     = int(os.getenv("AGENT_PAGE_CHARS", "12000"))
MAX_URLS_PER_CALL    = int(os.getenv("MAX_URLS_PER_CALL", "40"))
# Parallel extraction-model calls (pages and feed batches)
EXTRACT_CONCURRENCY  = max(1, int(os.getenv("EXTRACT_CONCURRENCY", "6")))
FEED_BATCH_CHARS     = 12000
FEED_BATCH_RECORDS   = 40
FEED_VALUE_CHARS     = 600
MAX_MAP_URLS         = 1000
MAX_SNAPSHOT_ELEMENTS  = 150
SNAPSHOT_ELEMENT_CHARS = 6000
MAX_SCROLL_ROUNDS      = 30
MAX_PAGINATE_PAGES     = 50
HISTORY_CONTEXT_ROWS = 200
HISTORY_ROW_CHARS    = 300

BOOTH_FIELDS = ("brand", "booth")
# Columns filled with the page URL when the extractor leaves them empty
_URL_FIELDS = {"url", "link", "page url", "product url", "source url"}

Emit = Callable[[str, dict[str, Any]], None]
Row = dict[str, str]
CallTool = Callable[[str, dict[str, Any], Emit], str]
# Writes rows into the results table: (rows added, rows updated)
Merge = Callable[[list[Row]], tuple[int, int]]

_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")


class TurnCancelled(Exception):
    """Raised inside run_turn() when the cancel event is set."""

TOOL_OPEN = "<tool>"
_TOOL_RE = re.compile(r"<tool>\s*(.*?)\s*(?:</tool>|$)", re.DOTALL)
# Hermes progress lines not shown: Booth Radar's own tools (they emit clearer status
# lines), Hermes' internal tool lookups and skill loading.
_HIDDEN_PROGRESS_RE = re.compile(
    r"\b(?:mcp_+booth[_-]radar_+\w+|tool_search|tool_describe|skills_list|skill_view)\b", re.IGNORECASE,
)

_PERSONA = """\
You are Booth Radar, a friendly assistant that helps the team collect data from
public websites: trade-show exhibitors and booths, product catalogs, specs,
dealer and store lists — whatever details the user asks for. You are genuinely
conversational — you talk WITH the user, not just at them.

Personality:
- You are warm, upbeat, and genuinely happy to help. You enjoy hunting down
  data and it shows: greet people cheerfully, celebrate finished scrapes
  ("Done! Collected 115 products with style numbers and heights 🎉"), and thank
  users for their requests.
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

  "Let me first check whether the site publishes its product catalog as a data
   feed — that would be way faster than opening every page one by one ⚡"

Bad (do NOT do this): jumping straight into a tool call with no message, or
writing only "Fetching..." before the call — that's not conversational.

If the request is ambiguous or a URL is missing, ask ONE focused clarifying
question instead of guessing. Do not use any tool until you have what you need.

After EVERY tool result, if you are about to use another tool, explain what
you learned and what you are going to do next before calling the next tool.
"""

# (name, inline JSON args, MCP signature, description)
_TOOL_DOCS: list[tuple[str, str, str, str]] = [
    (
        "find_feeds",
        '{"url": "https://..."}',
        "find_feeds(url)",
        "Checks whether the site publishes its content as a data feed (WordPress\n"
        "REST API, Shopify products.json) and lists each feed with its record count\n"
        "and fields. Takes seconds. Call it first on any new site.",
    ),
    (
        "read_feed",
        '{"url": "https://...", "feed": "wp:product", "fields": ["Product Name", "URL"], '
        '"instructions": "", "key_fields": [], "replace": false, "letters": "", "id_column": ""}',
        'read_feed(url, feed, fields, instructions="", key_fields=[], replace=false, letters="", id_column="")',
        "Reads every record of one feed (from find_feeds or capture_api) and fills the\n"
        "results table with the requested fields. Fastest way to collect a whole\n"
        "catalog or list. A letter-filtered feed is read for every letter A–Z and #\n"
        "(or only `letters`, e.g. \"A-C\"). A detail lookup feed needs `id_column`: the\n"
        "results column holding the item IDs (from a list feed); every row sharing an\n"
        "ID gets that item's details. Feeds often lack details only shown on each\n"
        "item's page: look for a lookup feed first, else extract those pages.",
    ),
    (
        "capture_api",
        '{"url": "https://...", "scroll": false}',
        "capture_api(url, scroll=false)",
        "Opens a page in the browser and records the JSON data requests it makes\n"
        "(search results, product grids, store lists). Each one that returns records\n"
        "becomes a feed (api:1, api:2, …) you can read with read_feed. Use it on a\n"
        "listing or search page when find_feeds found nothing. Set scroll=true for\n"
        "infinite-scroll or \"load more\" pages. On one item's page it finds detail\n"
        "lookup feeds, which fetch fields the list lacks (booth, phone…) for every row.",
    ),
    (
        "map_site",
        '{"url": "https://...", "include": "", "limit": 200}',
        'map_site(url, include="", limit=200)',
        "Lists page URLs from the site's sitemaps, filtered to URLs containing\n"
        "`include` (e.g. \"/product/\"). Use it to find all item pages without clicking\n"
        "through category pages.",
    ),
    (
        "fetch_page",
        '{"url": "https://...", "offset": 0, "refresh": false, "browser": false}',
        "fetch_page(url, offset=0, refresh=false, browser=false)",
        "Loads a page and returns its visible text, links and any JSON-LD structured\n"
        "data. Use it to understand a site and discover pagination or letter pages.\n"
        f"Long pages are cut at {AGENT_PAGE_CHARS:,} chars; pass offset to read further.\n"
        "Set browser=true if content looks missing (JavaScript-rendered page).",
    ),
    (
        "extract",
        '{"urls": ["https://...", ...], "fields": ["Product Name", "Price"], '
        '"instructions": "", "key_fields": [], "refresh": false, "replace": false, "session": "", '
        '"url_template": ""}',
        'extract(urls, fields, instructions="", key_fields=[], refresh=false, replace=false, session="", '
        'url_template="")',
        "Scrapes each URL in parallel and extracts one row per matching item with\n"
        "the requested fields into the results table. Use `instructions` to say which\n"
        f"items count or how to read a value. Max {MAX_URLS_PER_CALL} URLs per call;\n"
        "call it again for more. Pass `session` instead of urls to extract from a\n"
        "browser session's current page (after clicking, filtering or scrolling), or\n"
        "`url_template` (e.g. \"https://site/item/{Item ID}\") to build item pages from a\n"
        "results column; each page fills the rows sharing its value.",
    ),
    (
        "navigate",
        '{"url": "https://...", "session": ""}',
        'navigate(url, session="")',
        "Opens a page in a browser session you can interact with (a new session if\n"
        "none is given; ids s1, s2…). Returns a snapshot: the page's clickable\n"
        "elements with refs (e1, e2…) and its text. Use it when data only appears\n"
        "after clicking, filtering, searching or scrolling.",
    ),
    (
        "snapshot",
        '{"session": "s1", "offset": 0}',
        "snapshot(session, offset=0)",
        "Re-reads the session's current page (elements and text); pass offset to\n"
        "read further down long pages.",
    ),
    (
        "click",
        '{"session": "s1", "ref": "e12"}',
        "click(session, ref)",
        "Clicks an element by its ref from the latest snapshot (link, button, tab,\n"
        "checkbox) and returns the new snapshot.",
    ),
    (
        "fill",
        '{"session": "s1", "ref": "e3", "text": "...", "submit": false}',
        "fill(session, ref, text, submit=false)",
        "Types text into a field; submit=true presses Enter. Returns the new snapshot.",
    ),
    (
        "select",
        '{"session": "s1", "ref": "e4", "value": "..."}',
        "select(session, ref, value)",
        "Chooses a dropdown option by its label or value. Returns the new snapshot.",
    ),
    (
        "scroll_until_end",
        '{"session": "s1", "max_rounds": 10}',
        "scroll_until_end(session, max_rounds=10)",
        "Scrolls down until the page stops loading more (infinite scroll). Then\n"
        "extract with the session.",
    ),
    (
        "paginate",
        '{"session": "s1", "next_ref": "e40", "fields": ["Product Name", "Price"], '
        '"max_pages": 10, "instructions": "", "key_fields": [], "replace": false}',
        'paginate(session, next_ref, fields, max_pages=10, instructions="", key_fields=[], replace=false)',
        "Extracts the fields from the session's current page, clicks the \"Next\"\n"
        "element (next_ref), and repeats up to max_pages pages, filling the results\n"
        "table. Use it for pagination that has no page URLs to extract directly.",
    ),
    (
        "start_job",
        '{"fields": ["Product Name", "Price", "URL"], "urls": [], "url_column": "", '
        '"sitemap": "", "include": "", "url_template": "", "instructions": "", "key_fields": [], '
        '"replace": false}',
        'start_job(fields, urls=[], url_column="", sitemap="", include="", url_template="", '
        'instructions="", key_fields=[], replace=false)',
        "Extracts many pages in the background, outside this reply, filling the results\n"
        "table as it goes; returns at once. Give exactly one URL source: `urls`;\n"
        "`url_column` (a results column holding page URLs, e.g. \"URL\" from a feed, to\n"
        "fill in details from each page); `sitemap` (a site URL) with `include`\n"
        "(like map_site); or `url_template` (e.g. \"https://site/item/{Item ID}\", pages\n"
        "built from a results column; each fills the rows sharing its value).\n"
        f"Up to {jobs.MAX_JOB_URLS:,} pages. Use it instead of extract when more\n"
        f"than {jobs.JOB_MIN_URLS} pages need extracting. One job per conversation.",
    ),
    (
        "cancel_job",
        "{}",
        "cancel_job()",
        "Stops the conversation's background job (only when the user asks). Rows\n"
        "collected so far stay.",
    ),
    (
        "extract_booths",
        '{"urls": ["https://...", ...], "refresh": false}',
        "extract_booths(urls, refresh=false)",
        "Trade-show shortcut: extracts every exhibitor brand + booth from each URL.\n"
        f"Max {MAX_URLS_PER_CALL} URLs per call.",
    ),
    (
        "clear_results",
        "{}",
        "clear_results()",
        "Empties the results table. Only when the user asks for a new table.",
    ),
]


def _tool_list(mcp: bool) -> str:
    lines = []
    for name, inline_args, signature, description in _TOOL_DOCS:
        head = signature if mcp else f"{name}  {inline_args}"
        body = "\n".join(f"    {line}" for line in description.splitlines())
        lines.append(f"- {head}\n{body}")
    return "\n".join(lines)


_INLINE_TOOLS = f"""
────────────────────────────────────────────────────────────
Tools
────────────────────────────────────────────────────────────
To call a tool, write your conversational message first, then output exactly
one block and STOP:

<tool>{{"name": "<tool name>", "args": {{...}}}}</tool>

Available tools:
{_tool_list(mcp=False)}

After each tool call you will receive a <tool_result> message. Continue until
the user's request is done, then reply in plain text WITHOUT a tool block.
"""


def _mcp_tools(turn_id: str) -> str:
    return f"""
────────────────────────────────────────────────────────────
Tools
────────────────────────────────────────────────────────────
Use the booth-radar tools for every website request — never another browser,
terminal or code tool. Pass turn="{turn_id}" on EVERY booth-radar call.

{_tool_list(mcp=True)}
"""


def _skill_list() -> str:
    return "\n".join(f"- {s.name}: {s.description}" for s in skills.load())


# mcp mode: Hermes loads the full strategy from the booth-radar skills.
_MCP_STRATEGY = f"""
────────────────────────────────────────────────────────────
How to scrape fast
────────────────────────────────────────────────────────────
1. New site: find_feeds first; read_feed when a feed has the items.
2. No feed: capture_api on a listing or search page, then read_feed its api feed
   (a letter-filtered feed is read for every letter at once).
3. Fields missing from the list (booth, phone…): capture_api on ONE item's page and
   read_feed its "detail lookup" feed with id_column (the rows' ID column).
4. Otherwise get the page URLs (feed rows, map_site, listing pages, url_template) and
   extract them; for more than {jobs.JOB_MIN_URLS} pages use start_job and end your reply.
5. Data behind clicks, filters, search forms or scrolling: navigate and the
   interaction tools.
6. fetch_page only to understand a layout or answer a question.

Before working on a website, load the matching skill with skill_view —
always site-scraping, plus any other that fits:
{_skill_list()}
"""

# inline mode: Hermes can't call skill_view, so the skills are part of the prompt.
_INLINE_STRATEGY = "".join(
    f"""
────────────────────────────────────────────────────────────
Skill: {s.name} — {s.description}
────────────────────────────────────────────────────────────
{s.body}
"""
    for s in skills.load()
)

_GUIDELINES = """
────────────────────────────────────────────────────────────
Guidelines
────────────────────────────────────────────────────────────
- If the user just chats or asks a question, answer directly without tools.
- Never invent data. Only report what the tools returned.
- Never call clear_results or pass replace=true unless the user asked for a new
  table. Rows already collected stay; add missing columns or items to them.
- When finished, summarise what was collected: which site, how many rows,
  which columns, and any pages that failed.
- Only mention the results table or its Export CSV button if you added rows
  in this turn.
- Never mention file paths, folders, the server, your tools, or which actions
  were or weren't available. Speak only about the results and the user's request.
- Keep replies concise — conversational does not mean verbose.
"""

SYSTEM_PROMPT = _PERSONA + _INLINE_TOOLS + _INLINE_STRATEGY + _GUIDELINES


def _system_prompt(turn_id: str) -> str:
    if AGENT_TOOL_MODE == "mcp":
        return _PERSONA + _mcp_tools(turn_id) + _MCP_STRATEGY + _GUIDELINES
    return SYSTEM_PROMPT


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

def _str_list(value: Any) -> list[str]:
    """Accept a list or a comma-separated string; drop blanks."""
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


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
    text, links, cached = load_page(url, refresh, browser=_flag(args, "browser"))
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


def _cached_extract(
    cache_url: str, text: str, label: str, fields: list[str] | None, instructions: str, refresh: bool,
) -> list[Row]:
    spec = json.dumps([fields, instructions]) if fields else ""
    key = (cache_url, page_cache.text_hash(text + spec))
    rows = None if refresh else page_cache.extractions.get(key)
    if rows is None:
        rows = extract(text, label, fields=fields, instructions=instructions)
        page_cache.extractions.set(key, rows)
    else:
        log.info("[agent] extraction cache hit for %s", label)
    return [dict(r) for r in rows]


def _model_rows(
    url: str, refresh: bool, fields: list[str] | None, instructions: str,
) -> tuple[list[Row], bool]:
    """Rows the extraction model finds on *url*, and whether the page was force-rendered."""
    key = page_cache.url_key(url)
    rows = _cached_extract(key, scrape(url, refresh), url, fields, instructions, refresh)
    if rows or used_browser(url):
        return rows, False
    # The plain HTTP copy may lack JavaScript-loaded content; try the rendered page.
    log.info("[agent] no rows from plain fetch of %s, rendering in the browser", url)
    return _cached_extract(key, scrape(url, refresh, browser=True), url, fields, instructions, refresh), True


def _fill_urls(rows: list[Row], fields: list[str], url: str) -> list[Row]:
    for name in fields:
        if name.casefold() in _URL_FIELDS:
            for row in rows:
                row[name] = row.get(name) or url
    return rows


class _PageRecipes:
    """Saved and newly learned page recipes for one extract call."""

    def __init__(self, urls: list[str], fields: list[str], refresh: bool) -> None:
        self.fields = fields
        self.keys = [recipes.page_key(u, fields) for u in urls]
        self._lock = threading.Lock()
        self._recipes: dict[str, dict[str, Any]] = {}
        self._fallbacks: Counter[str] = Counter()
        self.uses: Counter[str] = Counter()
        self._learn: set[str] = set()
        for key, count in Counter(self.keys).items():
            saved = None if refresh else recipes.get(key)
            if saved:
                self._recipes[key] = saved
            elif count >= recipes.MIN_RECIPE_PAGES:
                self._learn.add(key)

    def leaders(self) -> list[int]:
        """The first URL of each layout that still needs a recipe; extracted before the rest."""
        return [self.keys.index(k) for k in sorted(self._learn, key=self.keys.index)]

    def read(self, i: int, url: str, load: Callable[[dict[str, Any]], str]) -> list[Row] | None:
        """Rows read with the layout's recipe from load(recipe)'s HTML; None if the model is needed."""
        key = self.keys[i]
        with self._lock:
            recipe = self._recipes.get(key)
        if recipe is None:
            return None
        try:
            rows = recipes.apply_page(recipe, load(recipe), url, self.fields)
        except ValueError as exc:
            log.info("[agent] recipe failed on %s: %s", url, exc)
            rows = []
        with self._lock:
            if recipes.looks_complete(recipe, rows):
                self._fallbacks[key] = 0
                self.uses[key] += 1
                return rows
            self._fallbacks[key] += 1
            if self._fallbacks[key] >= recipes.MAX_FALLBACKS and self._recipes.pop(key, None):
                recipes.forget(key)
        return None

    def learn(self, i: int, url: str, rows: list[Row], browser: bool, markup: Callable[[], str]) -> None:
        key = self.keys[i]
        with self._lock:
            if key not in self._learn or not rows:
                return
            self._learn.discard(key)
        recipe = recipes.learn_page(markup(), url, self.fields, rows)
        if recipe:
            recipe["browser"] = browser
            recipes.put(key, recipe)
            with self._lock:
                self._recipes[key] = recipe


def _emit_results(table: ResultsTable, emit: Emit) -> None:
    emit("results", {"results": table.snapshot()})


def _collect(
    labels: list[str],
    work: Callable[[int], list[Row]],
    workers: int,
    table: ResultsTable,
    emit: Emit,
    verb: str,
    failed: list[tuple[str, str]] | None = None,
    merge: Merge | None = None,
) -> list[str]:
    """
    Run *work* for each label in parallel and merge rows into *table* in label order
    (with *merge*, default table.add). Labels that fail are also added to *failed* as (label, error).
    """
    merge = merge or table.add
    lines: list[str] = []
    outcomes: dict[int, list[Row] | Exception] = {}
    next_to_merge = 0

    def merge_ready() -> bool:
        # Merge in label order so de-duplication matches sequential processing.
        nonlocal next_to_merge
        changed = False
        while next_to_merge in outcomes:
            label, outcome = labels[next_to_merge], outcomes.pop(next_to_merge)
            next_to_merge += 1
            if isinstance(outcome, Exception):
                lines.append(f"- {label}: FAILED ({outcome})")
                if failed is not None:
                    failed.append((label, str(outcome)))
                continue
            added, updated = merge(outcome)
            changed = changed or bool(added or updated)
            merged = f", {updated} existing rows filled in" if updated else ""
            lines.append(f"- {label}: {len(outcome)} rows found, {added} new{merged}")
        return changed

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(labels)))) as pool:
        futures: dict[Future[list[Row]], int] = {pool.submit(work, i): i for i in range(len(labels))}
        for done, future in enumerate(as_completed(futures), 1):
            i = futures[future]
            try:
                outcomes[i] = future.result()
                emit("status", {"text": f"📄 {verb} {done}/{len(labels)}: {labels[i]}"})
            except Exception as exc:
                log.warning("[agent] %s failed for %s: %s", verb.lower(), labels[i], exc)
                outcomes[i] = exc
                emit("status", {"text": f"⚠️ Failed {done}/{len(labels)}: {labels[i]}"})
            if merge_ready():
                _emit_results(table, emit)
    return lines


def _column(table: ResultsTable, name: str) -> str:
    """The results column called *name* (any case), or ""."""
    return next((c for c in table.columns if c.casefold() == name.casefold()), "")


def _template_parts(template: str) -> tuple[str, str, str] | None:
    """(before, column name, after) of a URL template with one {Column} placeholder."""
    names = _PLACEHOLDER_RE.findall(template)
    if len(names) != 1 or not template.startswith(("http://", "https://")):
        return None
    before, after = template.split("{" + names[0] + "}")
    return before, names[0].strip(), after


def _template_urls(table: ResultsTable, template: str) -> tuple[str, dict[str, str]] | str:
    """
    (column, {page URL: column value}) for each distinct value of the template's column,
    or an error message.
    """
    parts = _template_parts(template)
    if parts is None:
        return "Error: 'url_template' must be a page URL with one {Column} placeholder, e.g. https://site/item/{ID}."
    before, name, after = parts
    column = _column(table, name)
    if not column:
        return f"Error: the results table has no column {name!r}; columns are {table.columns}."
    values = list(dict.fromkeys(row[column] for row in table.snapshot() if row[column]))
    if not values:
        return f"Error: the {column!r} column is empty, so there are no pages to build."
    return column, {f"{before}{quote(v, safe='')}{after}": v for v in values}


def _template_value(template: str, url: str) -> str:
    """The column value a templated page URL was built from."""
    parts = _template_parts(template)
    if parts is None or not url.startswith(parts[0]) or not url.endswith(parts[2]):
        return ""
    return unquote(url[len(parts[0]):len(url) - len(parts[2])])


def _table_summary(table: ResultsTable) -> str:
    return f"Results table now has {len(table)} rows with columns {table.columns}."


def _tool_extract(
    args: dict[str, Any], table: ResultsTable, emit: Emit, sessions: TurnSessions | None = None, booths: bool = False,
) -> str:
    urls = _str_list(args.get("urls"))
    session = str(args.get("session") or "").strip()
    template = "" if booths else str(args.get("url_template") or "").strip()
    if not urls and not session and not template:
        return "Error: 'urls' must be a non-empty list of URLs (or pass url_template or a browser session)."
    fields = list(BOOTH_FIELDS) if booths else _str_list(args.get("fields"))
    if not fields:
        return "Error: 'fields' is required, e.g. [\"Product Name\", \"Price\"]."
    fill: tuple[str, dict[str, str]] | None = None
    if template and not urls:
        built = _template_urls(table, template)
        if isinstance(built, str):
            return built
        fill = built
        urls = list(built[1])
        fields = [built[0], *(f for f in fields if f.casefold() != built[0].casefold())]
        error = table.prepare(fields, list(table.key_fields))
    else:
        error = table.prepare(fields, _str_list(args.get("key_fields")) or None, _flag(args, "replace"))
    if error:
        return error
    if session and not urls:
        if sessions is None:
            return "Error: browser sessions are not available here."
        return _extract_session(session, fields, str(args.get("instructions") or ""), table, emit, sessions)
    skipped = urls[MAX_URLS_PER_CALL:]
    urls = urls[:MAX_URLS_PER_CALL]
    emit("status", {"text": f"📄 Extracting {len(urls)} page{'s' if len(urls) != 1 else ''}…"})
    lines = _extract_urls(
        urls, None if booths else fields, str(args.get("instructions") or ""), _flag(args, "refresh"),
        table, emit, max(SCRAPE_CONCURRENCY, EXTRACT_CONCURRENCY), fill=fill,
    )
    if skipped:
        how = "start_job with the same url_template" if fill else "start_job instead of more extract calls"
        lines.append(
            f"Not processed (over the {MAX_URLS_PER_CALL}-URL limit): {len(skipped)} URLs. "
            f"For more than {jobs.JOB_MIN_URLS} pages use {how}."
        )
    lines.append(_table_summary(table))
    return "\n".join(lines)


def _extract_urls(
    urls: list[str],
    fields: list[str] | None,
    instructions: str,
    refresh: bool,
    table: ResultsTable,
    emit: Emit,
    workers: int,
    url_field: str = "",
    failed: list[tuple[str, str]] | None = None,
    fill: tuple[str, dict[str, str]] | None = None,
) -> list[str]:
    """
    Extract *fields* (None = booth rows) from each URL into *table*; one line per URL.
    With *fill* = (column, {url: value}), each page's rows get that column value and
    fill the rows sharing it instead of adding new ones.
    """
    # Booth rows are normalised by the booth prompt, so a recipe could never reproduce them.
    layouts = _PageRecipes(urls, fields, refresh) if recipes.RECIPES_ENABLED and fields else None
    columns = fields or list(BOOTH_FIELDS)

    def work(i: int) -> list[Row]:
        url = urls[i]
        if fields is None:
            return _model_rows(url, refresh, None, "")[0]
        def load(recipe: dict[str, Any]) -> str:
            return load_markup(url, refresh, browser=bool(recipe.get("browser")), wait_for=recipe.get("item"))[1]

        rows = layouts.read(i, url, load) if layouts else None
        if rows is None:
            rows, browser = _model_rows(url, refresh, fields, instructions)
            if layouts:
                layouts.learn(i, url, rows, browser, lambda: load_markup(url, browser=browser)[1])
        rows = _fill_urls(rows, columns, url)
        if url_field:
            for row in rows:
                row[url_field] = url
        if fill:
            for row in rows:
                row[fill[0]] = fill[1].get(url, "")
        return rows

    merge: Merge | None = (lambda rows: table.fill(fill[0], rows)) if fill else None
    leaders = layouts.leaders() if layouts else []
    rest = [i for i in range(len(urls)) if i not in leaders]
    lines: list[str] = []
    for group in (leaders, rest):
        if group:
            lines += _collect(
                [urls[i] for i in group], lambda j, g=group: work(g[j]),
                workers, table, emit, "Extracted", failed, merge,
            )
    if layouts and layouts.uses:
        recipes.record_uses(layouts.uses)
        lines.append(f"{sum(layouts.uses.values())} pages were read with a learned page layout (no AI call).")
    return lines


def _job_batch(urls: list[str], job: jobs.Job, table: ResultsTable) -> list[tuple[str, str]]:
    failed: list[tuple[str, str]] = []
    fill = None
    if job.url_template:
        parts = _template_parts(job.url_template)
        column = _column(table, parts[1]) if parts else ""
        if column:
            fill = (column, {u: _template_value(job.url_template, u) for u in urls})
    _extract_urls(
        urls, job.fields, job.instructions, False, table, lambda _t, _p: None,
        jobs.JOB_PAGE_CONCURRENCY, job.url_field, failed, fill,
    )
    return failed


jobs.set_extractor(_job_batch)


def _tool_start_job(args: dict[str, Any], table: ResultsTable, emit: Emit) -> str:
    if not table.conversation_id:
        return "Error: background jobs need a saved conversation."
    if table.job_lock:
        return "Error: this conversation already has a background job. Wait for it, or cancel it first."
    fields = _str_list(args.get("fields"))
    if not fields:
        return "Error: 'fields' is required, e.g. [\"Product Name\", \"Price\"]."
    url_column = str(args.get("url_column") or "").strip()
    sitemap = str(args.get("sitemap") or "").strip()
    template = str(args.get("url_template") or "").strip()
    sources = [bool(_str_list(args.get("urls"))), bool(url_column), bool(sitemap), bool(template)]
    if sum(sources) != 1:
        return "Error: give exactly one URL source: 'urls', 'url_column', 'sitemap' or 'url_template'."

    key_fields = _str_list(args.get("key_fields"))
    url_field = ""
    if url_column:
        url_field = _column(table, url_column)
        if not url_field:
            return f"Error: the results table has no column {url_column!r}; columns are {table.columns}."
        urls = [row[url_field] for row in table.snapshot()]
        fields = fields if url_field in fields else [url_field, *fields]
        key_fields = key_fields or [url_field]
    elif template:
        built = _template_urls(table, template)
        if isinstance(built, str):
            return built
        urls = list(built[1])
        fields = [built[0], *(f for f in fields if f.casefold() != built[0].casefold())]
        key_fields = list(table.key_fields)
    elif sitemap:
        emit("status", {"text": f"🗺️ Reading the sitemap of {site_feeds.origin(sitemap)}"})
        urls, _ = site_feeds.map_site(sitemap, str(args.get("include") or ""), jobs.MAX_JOB_URLS, False)
    else:
        urls = _str_list(args.get("urls"))
    urls = list(dict.fromkeys(u for u in urls if u.startswith(("http://", "https://"))))
    skipped = max(0, len(urls) - jobs.MAX_JOB_URLS)
    urls = urls[:jobs.MAX_JOB_URLS]
    if not urls:
        return "Error: no page URLs found for the job."

    error = table.prepare(fields, key_fields or None, _flag(args, "replace"))
    if error:
        return error
    jobs.create(table, fields, table.key_fields, str(args.get("instructions") or ""), urls, url_field, template)
    emit("status", {"text": f"🚀 Started a background job for {len(urls):,} pages"})
    _emit_results(table, emit)
    minutes = max(1, round(len(urls) * 1.5 / jobs.JOB_PAGE_CONCURRENCY / 60))
    capped = f" ({skipped:,} more URLs were over the {jobs.MAX_JOB_URLS:,}-page limit)" if skipped else ""
    return (
        f"Background job started for {len(urls):,} pages{capped}, roughly {minutes} minute{'s' if minutes != 1 else ''}. "
        "Rows keep appearing in the results table, and the user can pause or cancel the job "
        "from the chat. Tell the user, then end your reply: don't wait for the job or "
        "extract these pages yourself."
    )


def _tool_cancel_job(table: ResultsTable, emit: Emit, job: dict[str, Any] | None) -> str:
    live = jobs.active(table.conversation_id) if table.conversation_id else None
    if live and jobs.cancel(live.job_id):
        emit("status", {"text": "🛑 Cancelling the background job"})
        return "The background job stops after the pages it is reading now. Rows collected so far stay."
    if job and job.get("status") == "paused":
        jobs.finish_paused(str(job.get("jobId")))
        table.job_lock = ""
        emit("status", {"text": "🛑 Cancelled the paused background job"})
        return "The paused background job was cancelled. Rows collected so far stay."
    return "There is no background job running in this conversation."


def _tool_find_feeds(args: dict[str, Any], emit: Emit) -> str:
    url = str(args.get("url", "")).strip()
    if not url:
        return "Error: 'url' is required."
    site = site_feeds.origin(url)
    emit("status", {"text": f"⚡ Checking {site} for data feeds"})
    feeds = site_feeds.find_feeds(url, _flag(args, "refresh"))
    if not feeds:
        return (
            f"No WordPress or Shopify data feed found on {site}. If the items are on a "
            "listing or search page, try capture_api on it; otherwise use map_site or "
            "fetch_page to find the pages, then extract."
        )
    listed = "\n".join(f.describe() for f in feeds)
    return f"Data feeds on {site} (use read_feed with the feed id):\n{listed}"


def _trim(record: dict[str, Any]) -> dict[str, Any]:
    return {k: (v[:FEED_VALUE_CHARS] if isinstance(v, str) else v) for k, v in record.items()}


def _record_batches(records: list[dict[str, Any]]) -> list[tuple[int, str]]:
    """Pack feed records into JSON-lines batches sized for one extraction call: (first record index, text)."""
    batches: list[tuple[int, list[str]]] = []
    current: list[str] = []
    start = size = 0
    for i, record in enumerate(records):
        line = json.dumps(_trim(record), ensure_ascii=False, separators=(",", ":"))
        if current and (size + len(line) > FEED_BATCH_CHARS or len(current) >= FEED_BATCH_RECORDS):
            batches.append((start, current))
            current, start, size = [], i, 0
        current.append(line)
        size += len(line) + 1
    if current:
        batches.append((start, current))
    return [(s, "\n".join(lines)) for s, lines in batches]


def _tool_read_feed(args: dict[str, Any], table: ResultsTable, emit: Emit) -> str:
    url = str(args.get("url", "")).strip()
    feed_id = str(args.get("feed", "")).strip()
    fields = _str_list(args.get("fields"))
    if not url or not feed_id:
        return "Error: 'url' and 'feed' are required (feed ids come from find_feeds)."
    if not fields:
        return "Error: 'fields' is required, e.g. [\"Product Name\", \"URL\"]."
    refresh = _flag(args, "refresh")
    site = site_feeds.origin(url)
    id_column = str(args.get("id_column") or "").strip()
    kind = site_feeds.feed_kind(url, feed_id)
    if kind == "lookup" and not id_column:
        return (
            f"Error: {feed_id} is a detail lookup (one item per ID). Pass id_column=<the results "
            "column holding the IDs, e.g. from a list feed> to fetch the details of every row."
        )
    if id_column and kind != "lookup":
        return (
            f"Error: id_column only works with a detail lookup feed, and {feed_id} is not one. "
            "Call capture_api on one item's page to find its lookup feed."
        )

    user_instructions = str(args.get("instructions") or "").strip()
    rule = "output one row per record that matches the request."
    merge: Merge = table.add
    if id_column:
        column = _column(table, id_column)
        if not column:
            return f"Error: the results table has no column {id_column!r}; columns are {table.columns}."
        ids = list(dict.fromkeys(row[column] for row in table.snapshot() if row[column]))
        if not ids:
            return f"Error: the {column!r} column is empty, so there are no IDs to look up."
        fields = [column, *(f for f in fields if f.casefold() != column.casefold())]
        error = table.prepare(fields, list(table.key_fields))
        if error:
            return error
        emit("status", {"text": f"🔎 Looking up details for {len(ids):,} IDs on {site}"})
        pairs, notes = site_feeds.read_lookup(
            url, feed_id, ids, lambda done, total: emit("status", {"text": f"🔎 Looked up {done:,}/{total:,} IDs"}),
        )
        records = [{column: i, **record} for i, record in pairs]
        if not records:
            return "\n".join([f"The {feed_id} lookup returned no records for {len(ids):,} IDs.", *notes])
        rule = f"output exactly one row per record, copying its {column!r} value unchanged."
        merge = lambda rows: table.fill(column, rows)  # noqa: E731
        heading = f"Feed {feed_id}: looked up {len(ids):,} IDs from {column!r}, {len(records):,} records."
    else:
        letters = str(args.get("letters") or "").strip()
        emit("status", {"text": f"⚡ Reading the {feed_id} feed from {site}"})
        records, notes = site_feeds.read_feed(url, feed_id, refresh, letters=letters)
        if not records:
            return "\n".join([f"The {feed_id} feed returned no records.", *notes])
        error = table.prepare(fields, _str_list(args.get("key_fields")) or None, _flag(args, "replace"))
        if error:
            return error
        covered = f" for letters {letters}" if letters else " for every letter A–Z and #" if kind == "letters" else ""
        heading = f"Feed {feed_id}: {len(records):,} records read{covered}."
    if notes:
        heading += "\nNot read: " + "; ".join(notes)

    instructions = (
        f"{user_instructions}\n"
        f"Each line is one JSON record from the site's data feed; {rule}"
    ).strip()
    batches = _record_batches(records)
    emit("status", {"text": f"⚡ Got {len(records)} records, filling in your columns…"})
    labels = [f"{feed_id} batch {i + 1}/{len(batches)}" for i in range(len(batches))]

    def work(i: int) -> list[Row]:
        return _cached_extract(f"feed:{site}|{feed_id}", batches[i][1], f"{site} {labels[i]}", fields, instructions, refresh)

    # Instructions can filter records, which a field-to-key mapping can't.
    use_recipe = recipes.RECIPES_ENABLED and not user_instructions and len(batches) > 1
    recipe_key = recipes.feed_key(site, feed_id, fields)
    recipe = recipes.get(recipe_key) if use_recipe and not refresh else None
    learned_from_first = False
    if use_recipe and recipe is None:
        try:
            first_rows = work(0)
        except Exception as exc:
            log.info("[agent] first %s batch failed, no feed recipe: %s", feed_id, exc)
        else:
            first_records = [_trim(r) for r in records[:batches[1][0]]]
            recipe = recipes.learn_feed(first_records, first_rows, fields)
            if recipe:
                recipes.put(recipe_key, recipe)
                learned_from_first = True

    if recipe is None:
        lines = _collect(labels, work, EXTRACT_CONCURRENCY, table, emit, "Mapped", merge=merge)
    else:
        # The first batch is already cached when the recipe was just learned from it.
        lines = _collect(labels[:1], work, 1, table, emit, "Mapped", merge=merge) if learned_from_first else []
        remaining = records[batches[1][0]:] if learned_from_first else records
        rows = recipes.apply_feed(recipe, remaining, fields)
        added, updated = merge(rows)
        _emit_results(table, emit)
        recipes.record_uses({recipe_key: 1})
        emit("status", {"text": f"⚡ Mapped {len(remaining):,} records instantly with the learned field mapping"})
        merged = f", {updated} existing rows filled in" if updated else ""
        lines.append(f"- {len(remaining):,} records mapped by field (no AI call): {len(rows):,} rows, {added} new{merged}")
    return "\n".join([heading, *lines, _table_summary(table)])


def _tool_capture_api(args: dict[str, Any], emit: Emit) -> str:
    url = str(args.get("url", "")).strip()
    if not url:
        return "Error: 'url' is required."
    emit("status", {"text": f"📡 Watching the data requests of {url}"})
    feeds = site_feeds.capture_api(url, _flag(args, "scroll"))
    if not feeds:
        return (
            f"{url} made no JSON requests that return lists of records. Its data is in the "
            "page itself: use extract on it (or map_site / fetch_page to find item pages)."
        )
    listed = "\n".join(f.describe() for f in feeds)
    return (
        f"JSON data endpoints used by {url} (read one with read_feed, feed=<id>; pick the one "
        f"whose fields match the items the user wants):\n{listed}"
    )


# ── Browser sessions ───────────────────────────────────────────────────────

def _format_snapshot(snap: dict[str, Any], offset: int = 0) -> str:
    """Interactive elements (non-links first) and the page text, within AGENT_PAGE_CHARS."""
    elements = sorted(snap["elements"], key=lambda e: e["role"] == "link")
    lines: list[str] = []
    size = 0
    for e in elements[:MAX_SNAPSHOT_ELEMENTS]:
        line = f'  {e["ref"]} {e["role"]} "{e["label"]}"'
        if e.get("href"):
            line += f' → {e["href"][:100]}'
        if "value" in e and e["role"] in ("textbox", "combobox"):
            line += f' = "{e["value"]}"'
        if e.get("options"):
            line += f' (options: {", ".join(e["options"])})'
        if "checked" in e:
            line += " [checked]" if e["checked"] else " [not checked]"
        if size + len(line) > SNAPSHOT_ELEMENT_CHARS:
            break
        lines.append(line)
        size += len(line) + 1
    text = parse_html(snap["html"], snap["url"])[0]
    budget = max(3000, AGENT_PAGE_CHARS - size)
    shown = text[offset:offset + budget]
    end = offset + len(shown)
    more = f"; pass offset={end} for more" if end < len(text) else ""
    return (
        f'Session {snap["session"]} · {snap["url"]} · "{snap["title"]}"\n'
        f"Elements ({len(lines)} of {len(elements)}):\n" + "\n".join(lines) + "\n"
        f"--- TEXT ({len(text):,} chars, showing {offset:,}–{end:,}{more}) ---\n{shown}\n--- END TEXT ---"
    )


def _session_args(args: dict[str, Any]) -> tuple[str, str]:
    return str(args.get("session") or "").strip(), str(args.get("ref") or "").strip()


def _tool_session(name: str, args: dict[str, Any], emit: Emit, sessions: TurnSessions) -> str:
    sid, ref = _session_args(args)
    if name == "navigate":
        url = str(args.get("url", "")).strip()
        if not url:
            return "Error: 'url' is required."
        emit("status", {"text": f"🧭 Opening {url}"})
        return _format_snapshot(sessions.navigate(url, sid))
    if not sid:
        return "Error: 'session' is required (the id navigate returned, e.g. \"s1\")."
    if name == "snapshot":
        try:
            offset = max(0, int(args.get("offset") or 0))
        except (TypeError, ValueError):
            return "Error: 'offset' must be a whole number."
        return _format_snapshot(sessions.snapshot(sid), offset)
    if name == "scroll_until_end":
        try:
            rounds = min(MAX_SCROLL_ROUNDS, max(1, int(args.get("max_rounds") or 10)))
        except (TypeError, ValueError):
            return "Error: 'max_rounds' must be a whole number."
        emit("status", {"text": "🖱️ Scrolling to load more…"})
        done, start, end, snap = sessions.scroll_until_end(sid, rounds)
        grown = "the page did not grow" if end <= start else f"page height grew from {start:,} to {end:,} px"
        return f"Scrolled {done} times; {grown}.\n\n{_format_snapshot(snap)}"

    if not ref:
        return "Error: 'ref' is required (an element ref from the latest snapshot, e.g. \"e12\")."
    element = sessions.element(sid, ref) or {}
    what = f'{element.get("role", "element")} "{element.get("label", ref)}"'
    if name == "click":
        emit("status", {"text": f"🖱️ Clicking {what}"})
        snap = sessions.click(sid, ref)
        note = f"Note: {snap['fallback']}.\n\n" if snap.get("fallback") else ""
        return note + _format_snapshot(snap)
    if name == "fill":
        emit("status", {"text": f"⌨️ Typing into {what}"})
        return _format_snapshot(sessions.fill(sid, ref, str(args.get("text") or ""), _flag(args, "submit")))
    if name == "select":
        value = str(args.get("value") or "")
        emit("status", {"text": f"🔽 Choosing {value!r} in {what}"})
        return _format_snapshot(sessions.select(sid, ref, value))
    return f"Error: unknown session tool {name!r}."


def _session_page(sessions: TurnSessions, sid: str) -> tuple[str, str, str]:
    """(url, text, markup) of the session's current page."""
    snap = sessions.snapshot(sid)
    text, _, markup = parse_html(snap["html"], snap["url"])
    return snap["url"], text, markup


def _extract_session(
    sid: str, fields: list[str], instructions: str, table: ResultsTable, emit: Emit, sessions: TurnSessions,
) -> str:
    url, text, _ = _session_page(sessions, sid)
    emit("status", {"text": f"📄 Extracting from the current page of session {sid}"})
    rows = _fill_urls(_cached_extract(f"{url}#session", text, url, fields, instructions, False), fields, url)
    added, updated = table.add(rows)
    _emit_results(table, emit)
    merged = f", {updated} existing rows filled in" if updated else ""
    return f"- {url} (session {sid}): {len(rows)} rows found, {added} new{merged}\n{_table_summary(table)}"


def _tool_paginate(args: dict[str, Any], table: ResultsTable, emit: Emit, sessions: TurnSessions) -> str:
    sid, _ = _session_args(args)
    next_ref = str(args.get("next_ref") or "").strip()
    fields = _str_list(args.get("fields"))
    if not sid or not next_ref:
        return "Error: 'session' and 'next_ref' (the ref of the Next button or link) are required."
    if not fields:
        return "Error: 'fields' is required, e.g. [\"Product Name\", \"Price\"]."
    try:
        max_pages = min(MAX_PAGINATE_PAGES, max(1, int(args.get("max_pages") or 10)))
    except (TypeError, ValueError):
        return "Error: 'max_pages' must be a whole number."
    element = sessions.element(sid, next_ref)
    if element is None:
        return f"Error: {next_ref} is not in the latest snapshot of {sid}; take a new snapshot."
    error = table.prepare(fields, _str_list(args.get("key_fields")) or None, _flag(args, "replace"))
    if error:
        return error
    instructions = str(args.get("instructions") or "")

    url, text, markup = _session_page(sessions, sid)
    layouts = _PageRecipes([url] * max_pages, fields, False) if recipes.RECIPES_ENABLED else None
    lines: list[str] = []
    seen_texts: set[str] = set()
    stop = f"reached max_pages={max_pages}"
    for page_no in range(max_pages):
        seen_texts.add(page_cache.text_hash(text))
        label = f"{url} (page {page_no + 1})"
        rows = layouts.read(page_no, url, lambda _recipe: markup) if layouts else None
        if rows is None:
            rows = _cached_extract(f"{url}#page{page_no + 1}", text, label, fields, instructions, False)
            if layouts:
                layouts.learn(page_no, url, rows, False, lambda: markup)
        added, updated = table.add(_fill_urls(rows, fields, url))
        _emit_results(table, emit)
        merged = f", {updated} existing rows filled in" if updated else ""
        lines.append(f"- {label}: {len(rows)} rows found, {added} new{merged}")
        emit("status", {"text": f"📄 Page {page_no + 1}: {len(rows)} rows"})
        if page_no + 1 >= max_pages:
            break
        snap = sessions.click_matching(sid, element["role"], element["label"])
        if snap is None:
            stop = f'no more "{element["label"]}" {element["role"]} to click'
            break
        url = snap["url"]
        text, _, markup = parse_html(snap["html"], url)
        if page_cache.text_hash(text) in seen_texts:
            stop = "the page stopped changing"
            break
    if layouts and layouts.uses:
        recipes.record_uses(layouts.uses)
    return "\n".join([*lines, f"Stopped: {stop}.", _table_summary(table)])


def _tool_map_site(args: dict[str, Any], emit: Emit) -> str:
    url = str(args.get("url", "")).strip()
    if not url:
        return "Error: 'url' is required."
    include = str(args.get("include") or "")
    try:
        limit = min(MAX_MAP_URLS, max(1, int(args.get("limit") or 200)))
    except (TypeError, ValueError):
        return "Error: 'limit' must be a whole number."
    emit("status", {"text": f"🗺️ Reading the sitemap of {site_feeds.origin(url)}"})
    urls, total = site_feeds.map_site(url, include, limit, _flag(args, "refresh"))
    if not total:
        where = f" containing {include!r}" if include else ""
        return f"No sitemap URLs{where} found. Use fetch_page to find listing pages instead."
    shown = f" (showing {len(urls)}; raise limit or narrow include for more)" if len(urls) < total else ""
    return f"{total} sitemap URLs{f' containing {include!r}' if include else ''}{shown}:\n" + "\n".join(urls)


_SESSION_TOOLS = {"navigate", "snapshot", "click", "fill", "select", "scroll_until_end"}


def _run_tool(
    name: str,
    args: dict[str, Any],
    table: ResultsTable,
    emit: Emit,
    sessions: TurnSessions,
    job: dict[str, Any] | None = None,
) -> str:
    if name in _SESSION_TOOLS or name == "paginate":
        try:
            if name == "paginate":
                return _tool_paginate(args, table, emit, sessions)
            return _tool_session(name, args, emit, sessions)
        except (StaleRef, KeyError, RuntimeError, PermissionError) as exc:
            return f"Error: {exc.args[0] if exc.args else exc}"
    if name == "fetch_page":
        return _tool_fetch_page(args, emit)
    if name == "find_feeds":
        return _tool_find_feeds(args, emit)
    if name == "read_feed":
        return _tool_read_feed(args, table, emit)
    if name == "capture_api":
        return _tool_capture_api(args, emit)
    if name == "map_site":
        return _tool_map_site(args, emit)
    if name == "extract":
        return _tool_extract(args, table, emit, sessions)
    if name == "extract_booths":
        return _tool_extract(args, table, emit, booths=True)
    if name == "start_job":
        return _tool_start_job(args, table, emit)
    if name == "cancel_job":
        return _tool_cancel_job(table, emit, job)
    if name == "clear_results":
        error = table.clear()
        if error:
            return error
        emit("status", {"text": "🧹 Cleared the results table"})
        _emit_results(table, emit)
        return "Results table cleared (0 rows)."
    return f"Error: unknown tool {name!r}. Available: {', '.join(d[0] for d in _TOOL_DOCS)}."


# ── Preambles ──────────────────────────────────────────────────────────────

NATIVE_TOOL_PREAMBLE = "On it! Give me a moment while I look into this 🔍"


def _preamble_for(name: str, args: dict[str, Any]) -> str:
    """Fallback message shown when Hermes calls a tool without saying anything first."""
    if name == "fetch_page":
        url = str(args.get("url", "")).strip()
        if args.get("offset"):
            return f"Let me read a bit further down {url} 📖"
        return f"Let me open {url} and see how it's laid out 🔍"
    if name == "find_feeds":
        return "Let me check whether this site publishes its data as a feed — that's the fastest route ⚡"
    if name == "read_feed":
        return "The site has a data feed, so I'll pull every record from it and fill in your columns ⚡"
    if name == "capture_api":
        return "Let me open that page and watch where it loads its data from — reading that directly is much faster ⚡"
    if name == "navigate":
        return f"Let me open {str(args.get('url', '')).strip() or 'the page'} so I can click through it 🧭"
    if name in ("click", "fill", "select", "snapshot"):
        return "Let me work through the page to get to the data you need 🖱️"
    if name == "scroll_until_end":
        return "This page loads more as you scroll, so I'll scroll to the end first 🖱️"
    if name == "paginate":
        return "I'll go through the pages one by one and collect the details as I go 📄"
    if name == "map_site":
        return "Let me read the site map to find all the pages we need 🗺️"
    if name in ("extract", "extract_booths"):
        count = len(_str_list(args.get("urls")))
        what = "the brands and booth numbers" if name == "extract_booths" else "the details you asked for"
        if count > 1:
            return f"Great, I'll collect {what} from {count} pages now — this may take a moment! 📄"
        return f"Great, I'll collect {what} from that page now 📄"
    if name == "start_job":
        return "That's a lot of pages, so I'll collect them in the background — rows will keep appearing in your table 🚀"
    if name == "cancel_job":
        return "Sure! I'll stop the background job — everything collected so far stays in the table 🛑"
    if name == "clear_results":
        return "Sure! I'll clear the results table so we can start fresh 🧹"
    return "Give me a moment while I work on that 🔍"


ACK_PROMPT = """\
You are Booth Radar, a warm, upbeat assistant that collects data from public
websites (trade-show exhibitors, product catalogs, dealer lists and more). The
user just sent the last message below and you are about to start working on it
(for example opening a page or collecting the details they asked for).

Write what you say to the user right before you start: one or two short sentences
that respond to what they actually said and tell them specifically what you are
about to do first. Sound like an enthusiastic teammate; at most one emoji.

Do not claim any results, do not ask questions, and do not mention tools, code,
servers or files. Output only the message."""


class _Acknowledgement:
    """
    Generates a request-specific opening line in the background so it is ready
    if Hermes starts acting before it says anything. Used at most once per turn.
    """

    def __init__(self, history: list[dict[str, str]]) -> None:
        self._text: str | None = None
        self._ready = threading.Event()
        self._used = False
        if not os.getenv("OPENAI_API_KEY"):
            self._ready.set()
            return
        threading.Thread(target=self._generate, args=(history,), daemon=True).start()

    def _generate(self, history: list[dict[str, str]]) -> None:
        recent = "\n".join(
            f"{m['role']}: {m['content'][:1500]}" for m in history[-ACK_HISTORY_MESSAGES:]
        )
        try:
            self._text = quick_completion([
                {"role": "system", "content": ACK_PROMPT},
                {"role": "user", "content": f"Conversation so far:\n{recent}"},
            ])
        except Exception as exc:
            log.warning("[agent] acknowledgement failed: %s", exc)
        finally:
            self._ready.set()

    def take(self, fallback: str) -> str:
        if self._used:
            return fallback
        self._used = True
        self._ready.wait(ACK_WAIT_SECONDS)
        return self._text or fallback


# ── Turn ───────────────────────────────────────────────────────────────────

ACTIVE_JOB_STATUSES = {"queued", "running", "paused"}
JOB_CONTEXT_FAILED  = 50


def _job_prompt(table: ResultsTable, job: dict[str, Any] | None) -> str:
    """The conversation's latest background job, with live progress when it runs here."""
    if not job:
        return ""
    status = str(job.get("status") or "")
    total = len(job.get("urls") or []) or int(job.get("total") or 0)
    cursor = int(job.get("cursor") or 0)
    failed = int(job.get("failedCount") or 0)
    live = jobs.active(table.conversation_id) if table.conversation_id else None
    if live and live.job_id == job.get("jobId"):
        status, cursor, failed = live.status, live.cursor, live.failed_count
    columns = ", ".join(job.get("fields") or [])
    failures = f", {failed:,} failed" if failed else ""
    if status in ACTIVE_JOB_STATUSES:
        state = "is paused" if status == "paused" else "is running"
        return (
            f"\n\nA background job {state} in this conversation, collecting {columns}: "
            f"{cursor:,} of {total:,} pages done{failures}, {len(table):,} rows in the table so far. "
            "The user can pause, resume or cancel it from the chat; call cancel_job only if "
            "they ask you to stop it."
        )
    urls = [f"- {f.get('url')}" for f in (job.get("failed") or [])[:JOB_CONTEXT_FAILED]]
    failed_list = ("\nPages it could not read:\n" + "\n".join(urls)) if urls else ""
    return (
        f"\n\nThe last background job in this conversation ({status}) collected {columns} "
        f"from {cursor:,} of {total:,} pages{failures}.{failed_list}"
    )


def _context_prompt(table: ResultsTable) -> str:
    if not table.rows:
        return "The results table is currently empty."
    shown = table.rows[:HISTORY_CONTEXT_ROWS]
    rows = "\n".join(
        " | ".join(r[c] for c in table.columns)[:HISTORY_ROW_CHARS] for r in shown
    )
    more = f"\n… and {len(table) - len(shown)} more" if len(table) > len(shown) else ""
    header = " | ".join(table.columns)
    return f"The results table currently has {len(table)} rows ({header}):\n{rows}{more}"


def _output_prompt(output_dir: str | None) -> str:
    if not output_dir:
        return ""
    return (
        "\n\nWhen you produce a file for the user (CSV, XLSX, JSON, TXT, MD), save it in\n"
        f"{output_dir}/ with a descriptive file name. Files saved there are attached\n"
        "to your reply as downloads. In your reply, refer to the file only by its name\n"
        "(never its path) and tell the user it is attached below for download."
    )


def _run_mcp_turn(
    turn_id: str,
    convo: list[dict[str, str]],
    table: ResultsTable,
    emit: Emit,
    ack: _Acknowledgement,
    cancel: threading.Event | None,
    call_tool: CallTool,
) -> tuple[str, list[Row]]:
    """One Hermes request; Hermes runs the tools itself via the MCP server and bridge."""
    emit_lock = threading.Lock()
    speak_lock = threading.Lock()
    spoke = False
    preamble = ""

    def safe_emit(type_: str, payload: dict[str, Any]) -> None:
        # Tools run on bridge threads while tokens stream on this one.
        with emit_lock:
            emit(type_, payload)

    def speak_first(fallback: str) -> None:
        nonlocal spoke, preamble
        with speak_lock:
            if spoke:
                return
            spoke = True
        preamble = ack.take(fallback)
        safe_emit("token", {"text": preamble + "\n\n"})

    def on_visible(t: str) -> None:
        nonlocal spoke
        if t.strip():
            spoke = True
        safe_emit("token", {"text": t})

    def on_tool_progress(t: str) -> None:
        if _HIDDEN_PROGRESS_RE.search(t):
            return
        speak_first(NATIVE_TOOL_PREAMBLE)
        safe_emit("thinking", {"text": t})

    def run_tool(name: str, args: dict[str, Any]) -> str:
        if cancel and cancel.is_set():
            return "Error: the user cancelled this turn. Stop and do not call more tools."
        args = {k: v for k, v in args.items() if k != "turn"}
        speak_first(_preamble_for(name, args))
        try:
            return call_tool(name, args, safe_emit)
        except Exception as exc:
            safe_emit("status", {"text": f"⚠️ {name} failed: {exc}"})
            raise

    tool_bridge.register(turn_id, run_tool)
    try:
        text = chat_stream(
            convo,
            on_token=on_visible,
            on_thinking=lambda t: safe_emit("thinking", {"text": t}),
            cancel=cancel,
            on_tool_progress=on_tool_progress,
            timeout=AGENT_STREAM_TIMEOUT,
        )
    finally:
        tool_bridge.unregister(turn_id)

    if cancel and cancel.is_set():
        raise TurnCancelled()
    reply = text.strip()
    if preamble:
        reply = f"{preamble}\n\n{reply}".strip()
    return reply, table.snapshot()


def run_turn(
    history: list[dict[str, str]],
    results: list[Row],
    emit: Emit,
    output_dir: str | None = None,
    cancel: threading.Event | None = None,
    turn_id: str | None = None,
    conversation_id: str | None = None,
    job: dict[str, Any] | None = None,
) -> tuple[str, list[Row]]:
    """
    Run one assistant turn over *history* (user/assistant messages, last one
    from the user). Returns (reply_text, updated_results).

    While the conversation has a background job, the turn shares the job's live
    results table instead of *results*. *job* is the server's record of the
    conversation's latest job.

    Raises TurnCancelled if *cancel* is set before or between steps.
    """
    turn_id = turn_id or uuid.uuid4().hex
    conversation_id = conversation_id or ""
    table = jobs.table_for(conversation_id) if conversation_id else None
    if table is None:
        table = ResultsTable(results, conversation_id)
        if job and job.get("status") in ACTIVE_JOB_STATUSES:
            table.job_lock = jobs.LOCK_REASON
    system = (
        f"{_system_prompt(turn_id)}{_output_prompt(output_dir)}\n\n"
        f"{_context_prompt(table)}{_job_prompt(table, job)}"
    )
    convo: list[dict[str, str]] = [
        {"role": "system", "content": system},
        *({"role": m["role"], "content": m["content"]} for m in history),
    ]
    ack = _Acknowledgement(history)
    sessions = TurnSessions()

    def call_tool(name: str, args: dict[str, Any], tool_emit: Emit) -> str:
        return _run_tool(name, args, table, tool_emit, sessions, job)

    try:
        if AGENT_TOOL_MODE == "mcp":
            return _run_mcp_turn(turn_id, convo, table, emit, ack, cancel, call_tool)
        return _run_inline_turn(convo, table, emit, ack, cancel, call_tool)
    finally:
        sessions.close()


def _run_inline_turn(
    convo: list[dict[str, str]],
    table: ResultsTable,
    emit: Emit,
    ack: _Acknowledgement,
    cancel: threading.Event | None,
    call_tool: CallTool,
) -> tuple[str, list[Row]]:
    """Hermes calls tools with <tool> blocks parsed here; one Hermes request per step."""
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
            if _HIDDEN_PROGRESS_RE.search(t):
                return
            if not spoke:
                spoke = True
                native_preamble = ack.take(NATIVE_TOOL_PREAMBLE)
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
            visible = ack.take(_preamble_for(name, args))
            log.info("[agent] step %d: tool call had no preamble, using fallback", step + 1)
            emit("token", {"text": visible})
        if visible:
            reply_parts.append(visible)

        raw_assistant = text[idx: match.end() if match else len(text)]
        convo.append({"role": "assistant", "content": f"{visible}\n\n{raw_assistant}".strip()})

        if call_ok:
            log.info("[agent] step %d tool=%s args=%s", step + 1, name, args)
            try:
                observation = call_tool(name, args, emit)
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

    return "\n\n".join(reply_parts), table.snapshot()
