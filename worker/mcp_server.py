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
        "Booth Radar's website tools. Always pass the `turn` id given in your "
        "instructions. Use these tools for every website request."
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
async def fetch_page(url: str, turn: str = "", offset: int = 0, refresh: bool = False) -> str:
    """
    Load a web page in a headless browser and return its visible text and links.
    Use it to understand a site, find the exhibitor directory, and discover pagination
    or A–Z letter pages before extracting. Long pages are cut; pass `offset` to read
    further. Recently loaded pages are cached; set `refresh` only for fresh data.
    """
    return await _call("fetch_page", turn, url=url, offset=offset, refresh=refresh)


@server.tool(structured_output=False)
async def extract_booths(urls: list[str], turn: str = "", refresh: bool = False) -> str:
    """
    Scrape each URL and extract every exhibitor brand and booth into the user's
    results table (duplicates are skipped). Pages are processed in parallel.
    """
    return await _call("extract_booths", turn, urls=urls, refresh=refresh)


@server.tool(structured_output=False)
async def clear_results(turn: str = "") -> str:
    """Empty the user's results table."""
    return await _call("clear_results", turn)


if __name__ == "__main__":
    server.run("stdio")
