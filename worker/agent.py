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

import page_cache
import recipes
import site_feeds
import tool_bridge
from extractor import chat_stream, extract, quick_completion
from results_table import ResultsTable
from scraper import SCRAPE_CONCURRENCY, load_markup, load_page, scrape, used_browser

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
HISTORY_CONTEXT_ROWS = 200
HISTORY_ROW_CHARS    = 300

BOOTH_FIELDS = ("brand", "booth")
# Columns filled with the page URL when the extractor leaves them empty
_URL_FIELDS = {"url", "link", "page url", "product url", "source url"}

Emit = Callable[[str, dict[str, Any]], None]
Row = dict[str, str]


class TurnCancelled(Exception):
    """Raised inside run_turn() when the cancel event is set."""

TOOL_OPEN = "<tool>"
_TOOL_RE = re.compile(r"<tool>\s*(.*?)\s*(?:</tool>|$)", re.DOTALL)

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
        '"instructions": "", "key_fields": [], "replace": false}',
        'read_feed(url, feed, fields, instructions="", key_fields=[], replace=false)',
        "Reads every record of one feed (from find_feeds or capture_api) and fills the\n"
        "results table with the requested fields. Fastest way to collect a whole\n"
        "catalog or list. Feeds\n"
        "often lack details only shown on each item's page (specs, prices): collect\n"
        "the URLs here, then extract those pages for the missing fields.",
    ),
    (
        "capture_api",
        '{"url": "https://...", "scroll": false}',
        "capture_api(url, scroll=false)",
        "Opens a page in the browser and records the JSON data requests it makes\n"
        "(search results, product grids, store lists). Each one that returns records\n"
        "becomes a feed (api:1, api:2, …) you can read with read_feed. Use it on a\n"
        "listing or search page when find_feeds found nothing. Set scroll=true for\n"
        "infinite-scroll or \"load more\" pages.",
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
        '"instructions": "", "key_fields": [], "refresh": false, "replace": false}',
        'extract(urls, fields, instructions="", key_fields=[], refresh=false, replace=false)',
        "Scrapes each URL in parallel and extracts one row per matching item with\n"
        "the requested fields into the results table. Use `instructions` to say which\n"
        f"items count or how to read a value. Max {MAX_URLS_PER_CALL} URLs per call;\n"
        "call it again for more.",
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
        "Empties the results table.",
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


_GUIDELINES = """
────────────────────────────────────────────────────────────
How to scrape fast
────────────────────────────────────────────────────────────
1. New site: ALWAYS call find_feeds first. If a feed holds the items the user
   wants, read_feed collects all of them in seconds. Do not extract listing
   pages or read the sitemap to collect items or URLs a feed already gives.
2. No feed, and the items are listed on a search, category or "load more"
   page: call capture_api on that page and read_feed the api feed it finds.
3. Need details the feed lacks (or nothing above worked): get the item URLs
   from the feed rows, map_site, or a listing page, then extract them in
   batches. Pages with the same layout are read faster after the first one.
4. Only use fetch_page to understand a site's layout or answer a question.

Fields: take them from the user's request as short, human-readable column
names ("Product Name", "Style Number", "Height", "Price", "URL"). Include "URL"
when rows come from different pages. Keep the same names when adding more
details to the same rows later, so they merge into one table. Use key_fields to
say which columns identify a row (e.g. ["URL"] or ["Style Number"]).

Pages, feeds and sitemaps loaded in the last hour are cached and return
instantly. To answer follow-up questions, call the tool again instead of
guessing. Only set refresh to true when the user asks for fresh data.

────────────────────────────────────────────────────────────
Guidelines
────────────────────────────────────────────────────────────
- If the user just chats or asks a question, answer directly without tools.
- When a list is split across pages (letters, page numbers, categories),
  build the full list of URLs and extract all of them, unless the user asked
  for only some.
- Never invent data. Only report what the tools returned.
- When finished, summarise what was collected: which site, how many rows,
  which columns, and any pages that failed.
- Only mention the results table or its Export CSV button if you added rows
  in this turn.
- Never mention file paths, folders, the server, your tools, or which actions
  were or weren't available. Speak only about the results and the user's request.
- Keep replies concise — conversational does not mean verbose.
"""

SYSTEM_PROMPT = _PERSONA + _INLINE_TOOLS + _GUIDELINES


def _system_prompt(turn_id: str) -> str:
    if AGENT_TOOL_MODE == "mcp":
        return _PERSONA + _mcp_tools(turn_id) + _GUIDELINES
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

    def read(self, i: int, url: str, refresh: bool) -> list[Row] | None:
        key = self.keys[i]
        with self._lock:
            recipe = self._recipes.get(key)
        if recipe is None:
            return None
        try:
            _, markup = load_markup(url, refresh, browser=bool(recipe.get("browser")))
            rows = recipes.apply_page(recipe, markup, url, self.fields)
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

    def learn(self, i: int, url: str, rows: list[Row], browser: bool) -> None:
        key = self.keys[i]
        with self._lock:
            if key not in self._learn or not rows:
                return
            self._learn.discard(key)
        _, markup = load_markup(url, browser=browser)
        recipe = recipes.learn_page(markup, url, self.fields, rows)
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
) -> list[str]:
    """Run *work* for each label in parallel and merge rows into *table* in label order."""
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
                continue
            added, updated = table.add(outcome)
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


def _table_summary(table: ResultsTable) -> str:
    return f"Results table now has {len(table)} rows with columns {table.columns}."


def _tool_extract(args: dict[str, Any], table: ResultsTable, emit: Emit, booths: bool = False) -> str:
    urls = _str_list(args.get("urls"))
    if not urls:
        return "Error: 'urls' must be a non-empty list of URLs."
    fields = list(BOOTH_FIELDS) if booths else _str_list(args.get("fields"))
    if not fields:
        return "Error: 'fields' is required, e.g. [\"Product Name\", \"Price\"]."
    error = table.prepare(fields, _str_list(args.get("key_fields")) or None, _flag(args, "replace"))
    if error:
        return error
    skipped = urls[MAX_URLS_PER_CALL:]
    urls = urls[:MAX_URLS_PER_CALL]
    refresh = _flag(args, "refresh")
    instructions = str(args.get("instructions") or "")
    # Booth rows are normalised by the booth prompt, so a recipe could never reproduce them.
    layouts = _PageRecipes(urls, fields, refresh) if recipes.RECIPES_ENABLED and not booths else None

    def work(i: int) -> list[Row]:
        url = urls[i]
        if booths:
            return _model_rows(url, refresh, None, "")[0]
        rows = layouts.read(i, url, refresh) if layouts else None
        if rows is None:
            rows, browser = _model_rows(url, refresh, fields, instructions)
            if layouts:
                layouts.learn(i, url, rows, browser)
        return _fill_urls(rows, fields, url)

    emit("status", {"text": f"📄 Extracting {len(urls)} page{'s' if len(urls) != 1 else ''}…"})
    leaders = layouts.leaders() if layouts else []
    rest = [i for i in range(len(urls)) if i not in leaders]
    lines: list[str] = []
    for group in (leaders, rest):
        if group:
            lines += _collect(
                [urls[i] for i in group], lambda j, g=group: work(g[j]),
                max(SCRAPE_CONCURRENCY, EXTRACT_CONCURRENCY), table, emit, "Extracted",
            )
    if layouts and layouts.uses:
        recipes.record_uses(layouts.uses)
        lines.append(f"{sum(layouts.uses.values())} pages were read with a learned page layout (no AI call).")
    if skipped:
        lines.append(f"Not processed (over the {MAX_URLS_PER_CALL}-URL limit): {len(skipped)} URLs")
    lines.append(_table_summary(table))
    return "\n".join(lines)


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

    emit("status", {"text": f"⚡ Reading the {feed_id} feed from {site}"})
    records = site_feeds.read_feed(url, feed_id, refresh)
    if not records:
        return f"The {feed_id} feed returned no records."
    error = table.prepare(fields, _str_list(args.get("key_fields")) or None, _flag(args, "replace"))
    if error:
        return error

    user_instructions = str(args.get("instructions") or "").strip()
    instructions = (
        f"{user_instructions}\n"
        "Each line is one JSON record from the site's data feed; output one row per "
        "record that matches the request."
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
        lines = _collect(labels, work, EXTRACT_CONCURRENCY, table, emit, "Mapped")
    else:
        # The first batch is already cached when the recipe was just learned from it.
        lines = _collect(labels[:1], work, 1, table, emit, "Mapped") if learned_from_first else []
        remaining = records[batches[1][0]:] if learned_from_first else records
        rows = recipes.apply_feed(recipe, remaining, fields)
        added, updated = table.add(rows)
        _emit_results(table, emit)
        recipes.record_uses({recipe_key: 1})
        emit("status", {"text": f"⚡ Mapped {len(remaining)} records instantly with the learned field mapping"})
        merged = f", {updated} existing rows filled in" if updated else ""
        lines.append(f"- {len(remaining)} records mapped by field (no AI call): {len(rows)} rows, {added} new{merged}")
    return "\n".join([f"Feed {feed_id}: {len(records)} records read.", *lines, _table_summary(table)])


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


def _run_tool(name: str, args: dict[str, Any], table: ResultsTable, emit: Emit) -> str:
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
        return _tool_extract(args, table, emit)
    if name == "extract_booths":
        return _tool_extract(args, table, emit, booths=True)
    if name == "clear_results":
        table.clear()
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
    if name == "map_site":
        return "Let me read the site map to find all the pages we need 🗺️"
    if name in ("extract", "extract_booths"):
        count = len(_str_list(args.get("urls")))
        what = "the brands and booth numbers" if name == "extract_booths" else "the details you asked for"
        if count > 1:
            return f"Great, I'll collect {what} from {count} pages now — this may take a moment! 📄"
        return f"Great, I'll collect {what} from that page now 📄"
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
        speak_first(NATIVE_TOOL_PREAMBLE)
        safe_emit("thinking", {"text": t})

    def run_tool(name: str, args: dict[str, Any]) -> str:
        if cancel and cancel.is_set():
            return "Error: the user cancelled this turn. Stop and do not call more tools."
        args = {k: v for k, v in args.items() if k != "turn"}
        speak_first(_preamble_for(name, args))
        try:
            return _run_tool(name, args, table, safe_emit)
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
) -> tuple[str, list[Row]]:
    """
    Run one assistant turn over *history* (user/assistant messages, last one
    from the user). Returns (reply_text, updated_results).

    Raises TurnCancelled if *cancel* is set before or between steps.
    """
    turn_id = turn_id or uuid.uuid4().hex
    table = ResultsTable(results)
    system = f"{_system_prompt(turn_id)}{_output_prompt(output_dir)}\n\n{_context_prompt(table)}"
    convo: list[dict[str, str]] = [
        {"role": "system", "content": system},
        *({"role": m["role"], "content": m["content"]} for m in history),
    ]
    ack = _Acknowledgement(history)

    if AGENT_TOOL_MODE == "mcp":
        return _run_mcp_turn(turn_id, convo, table, emit, ack, cancel)

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
                observation = _run_tool(name, args, table, emit)
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
