"""
mcp_server.py  –  Booth Radar MCP server for Hermes Agent (stdio).

Hermes launches this process from ~/.hermes/config.yaml (see README). It holds no
state: every tool call is forwarded to the running worker's tool bridge, which
executes it inside the active chat turn.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer

load_dotenv(Path(__file__).with_name(".env"))

BRIDGE_URL     = f"http://127.0.0.1:{os.getenv('TOOL_BRIDGE_PORT', '8765')}"
BRIDGE_TOKEN   = os.getenv("TOOL_BRIDGE_TOKEN", "")
BRIDGE_TIMEOUT = float(os.getenv("TOOL_BRIDGE_TIMEOUT", "900"))

server = MCPServer(
    "booth-radar",
    instructions=(
        "Booth Radar's website data tools. Always pass the `turn` id given in your "
        "instructions. Use these tools for every website request: find_feeds first, "
        "read_feed when a feed has the items, capture_api on listing pages without a "
        "feed, otherwise map_site or fetch_page to find pages and extract. Use navigate "
        "and the interaction tools when data only appears after clicks or scrolling."
    ),
)


def _post(name: str, turn: str, args: dict[str, Any]) -> str:
    try:
        resp = requests.post(
            f"{BRIDGE_URL}/tools/{name}",
            json={"turn": turn, "args": args},
            headers={"Authorization": f"Bearer {BRIDGE_TOKEN}"},
            timeout=(5, BRIDGE_TIMEOUT),
        )
    except requests.ConnectionError:
        return "Error: the Booth Radar worker is not running, so this tool is unavailable."
    except requests.Timeout:
        return f"Error: {name} did not finish within {BRIDGE_TIMEOUT:.0f} s."
    try:
        return str(resp.json().get("text", ""))
    except ValueError:
        return f"Error: unexpected response from the worker (HTTP {resp.status_code})."


async def _call(name: str, turn: str, **args: Any) -> str:
    return await asyncio.to_thread(_post, name, turn, args)


@server.tool(structured_output=False)
async def find_feeds(url: str, turn: str = "", refresh: bool = False) -> str:
    """
    Check whether a site publishes its content as a data feed (WordPress REST API,
    Shopify products.json) and list each feed with its record count and fields.
    Takes seconds; call it first on any new site.
    """
    return await _call("find_feeds", turn, url=url, refresh=refresh)


@server.tool(structured_output=False)
async def read_feed(
    url: str,
    feed: str,
    fields: list[str],
    turn: str = "",
    instructions: str = "",
    key_fields: list[str] | None = None,
    replace: bool = False,
    refresh: bool = False,
    letters: str = "",
    id_column: str = "",
) -> str:
    """
    Read every record of one feed (id from find_feeds or capture_api) and fill the
    user's results table with the requested fields. Fastest way to collect a whole
    catalog or list. Use field names like the feed's own ("Exhibitor Name" for
    exhibitorName); give `instructions` only to filter records or explain a value, as
    they make big feeds slower. A letter-filtered feed is read for every letter A–Z and #
    (or only `letters`, e.g. "A-C"). A detail lookup feed needs `id_column`: the
    results column holding the item IDs; every row sharing an ID gets its details.
    """
    return await _call(
        "read_feed", turn, url=url, feed=feed, fields=fields, instructions=instructions,
        key_fields=key_fields or [], replace=replace, refresh=refresh, letters=letters,
        id_column=id_column,
    )


@server.tool(structured_output=False)
async def capture_api(url: str, turn: str = "", scroll: bool = False) -> str:
    """
    Open a listing or search page in the browser and record the JSON data requests it
    makes. Each one that returns records becomes a feed (api:1, api:2, …) readable with
    read_feed. Use it when find_feeds found nothing; set `scroll` for infinite-scroll or
    "load more" pages. On one item's page it finds detail lookup feeds, which fetch
    fields the list lacks (booth, phone…) for every row at once.
    """
    return await _call("capture_api", turn, url=url, scroll=scroll)


@server.tool(structured_output=False)
async def map_site(url: str, turn: str = "", include: str = "", limit: int = 200, refresh: bool = False) -> str:
    """
    List page URLs from the site's sitemaps, filtered to URLs containing `include`
    (e.g. "/product/"). Finds all item pages without clicking through categories.
    """
    return await _call("map_site", turn, url=url, include=include, limit=limit, refresh=refresh)


@server.tool(structured_output=False)
async def fetch_page(
    url: str, turn: str = "", offset: int = 0, refresh: bool = False, browser: bool = False,
) -> str:
    """
    Load a web page and return its visible text, links and JSON-LD structured data.
    Use it to understand a site and discover pagination or letter pages. Long pages
    are cut; pass `offset` to read further. Set `browser` if content looks missing
    (JavaScript-rendered page). Recently loaded pages are cached; set `refresh` only
    for fresh data.
    """
    return await _call("fetch_page", turn, url=url, offset=offset, refresh=refresh, browser=browser)


@server.tool(structured_output=False)
async def extract(
    fields: list[str],
    urls: list[str] | None = None,
    turn: str = "",
    instructions: str = "",
    key_fields: list[str] | None = None,
    refresh: bool = False,
    replace: bool = False,
    session: str = "",
    url_template: str = "",
) -> str:
    """
    Scrape each URL in parallel and extract one row per matching item with the
    requested fields (e.g. ["Product Name", "Price", "URL"]) into the user's results
    table. Rows matching existing ones on `key_fields` fill in their empty cells.
    Pass `session` instead of `urls` to extract from a browser session's current page,
    or `url_template` (e.g. "https://site/item/{Item ID}") to build item pages from a
    results column; each page fills the rows sharing its value.
    """
    return await _call(
        "extract", turn, urls=urls or [], fields=fields, instructions=instructions,
        key_fields=key_fields or [], refresh=refresh, replace=replace, session=session,
        url_template=url_template,
    )


@server.tool(structured_output=False)
async def start_job(
    fields: list[str],
    turn: str = "",
    urls: list[str] | None = None,
    url_column: str = "",
    sitemap: str = "",
    include: str = "",
    instructions: str = "",
    key_fields: list[str] | None = None,
    replace: bool = False,
    url_template: str = "",
) -> str:
    """
    Extract many pages in the background, outside this reply, filling the results
    table as it goes; returns at once. Give exactly one URL source: `urls`,
    `url_column` (a results column holding page URLs, to fill in details from each
    page), `sitemap` (a site URL) with `include`, or `url_template` (e.g.
    "https://site/item/{Item ID}", pages built from a results column; each fills the
    rows sharing its value). Use it instead of extract for large page counts.
    """
    return await _call(
        "start_job", turn, fields=fields, urls=urls or [], url_column=url_column, sitemap=sitemap,
        include=include, instructions=instructions, key_fields=key_fields or [], replace=replace,
        url_template=url_template,
    )


@server.tool(structured_output=False)
async def cancel_job(turn: str = "") -> str:
    """Stop the conversation's background job (only when the user asks); collected rows stay."""
    return await _call("cancel_job", turn)


@server.tool(structured_output=False)
async def navigate(url: str, turn: str = "", session: str = "") -> str:
    """
    Open a page in a browser session you can interact with (new session if none is
    given; ids s1, s2…). Returns the page's clickable elements with refs (e1, e2…) and
    its text. Use it when data only appears after clicking, filtering, searching or
    scrolling.
    """
    return await _call("navigate", turn, url=url, session=session)


@server.tool(structured_output=False)
async def snapshot(session: str, turn: str = "", offset: int = 0) -> str:
    """Re-read a session's current page (elements and text); `offset` reads further down."""
    return await _call("snapshot", turn, session=session, offset=offset)


@server.tool(structured_output=False)
async def click(session: str, ref: str, turn: str = "") -> str:
    """Click an element by its ref from the latest snapshot; returns the new snapshot."""
    return await _call("click", turn, session=session, ref=ref)


@server.tool(structured_output=False)
async def fill(session: str, ref: str, text: str, turn: str = "", submit: bool = False) -> str:
    """Type text into a field (`submit` presses Enter); returns the new snapshot."""
    return await _call("fill", turn, session=session, ref=ref, text=text, submit=submit)


@server.tool(structured_output=False)
async def select(session: str, ref: str, value: str, turn: str = "") -> str:
    """Choose a dropdown option by its label or value; returns the new snapshot."""
    return await _call("select", turn, session=session, ref=ref, value=value)


@server.tool(structured_output=False)
async def scroll_until_end(session: str, turn: str = "", max_rounds: int = 10) -> str:
    """Scroll until the page stops loading more (infinite scroll), then extract with the session."""
    return await _call("scroll_until_end", turn, session=session, max_rounds=max_rounds)


@server.tool(structured_output=False)
async def paginate(
    session: str,
    next_ref: str,
    fields: list[str],
    turn: str = "",
    max_pages: int = 10,
    instructions: str = "",
    key_fields: list[str] | None = None,
    replace: bool = False,
) -> str:
    """
    Extract the fields from the session's current page, click the "Next" element
    (next_ref) and repeat up to max_pages pages, filling the results table. For
    pagination without page URLs.
    """
    return await _call(
        "paginate", turn, session=session, next_ref=next_ref, fields=fields, max_pages=max_pages,
        instructions=instructions, key_fields=key_fields or [], replace=replace,
    )


@server.tool(structured_output=False)
async def extract_booths(urls: list[str], turn: str = "", refresh: bool = False) -> str:
    """
    Trade-show shortcut: scrape each URL and extract every exhibitor brand and booth
    into the user's results table. Pages are processed in parallel.
    """
    return await _call("extract_booths", turn, urls=urls, refresh=refresh)


@server.tool(structured_output=False)
async def clear_results(turn: str = "") -> str:
    """Empty the user's results table. Only when the user asks for a new table."""
    return await _call("clear_results", turn)


if __name__ == "__main__":
    server.run("stdio")
