"""
test_local.py  –  Test the worker locally without the server or WebSocket.

Usage:
    python test_local.py https://example.com/exhibitors   # one-shot scrape + extract
    python test_local.py --chat                            # chat with the agent in the terminal
"""

from __future__ import annotations

import json
import sys
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from agent import run_turn
from extractor import extract
from scraper import scrape


def scrape_once(url: str) -> None:
    print(f"[test] scraping {url!r} …")
    page_text = scrape(url)
    print(f"[test] got {len(page_text):,} chars")
    print("[test] extracting …")

    results = extract(page_text, url, on_token=lambda t: print(t, end="", flush=True))
    print()  # newline after streaming output
    print(f"\n[test] {len(results)} results:")
    print(json.dumps(results, indent=2, ensure_ascii=False))


def chat() -> None:
    history: list[dict[str, str]] = []
    results: list[dict[str, str]] = []

    def emit(type_: str, payload: dict[str, Any]) -> None:
        if type_ == "token":
            print(payload["text"], end="", flush=True)
        elif type_ == "status":
            print(f"\n  [{payload['text']}]", flush=True)

    print("[test] chat mode — Ctrl+C to quit")
    while True:
        try:
            message = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not message:
            continue
        history.append({"role": "user", "content": message})
        print("bot> ", end="", flush=True)
        reply, results = run_turn(history, results, emit)
        history.append({"role": "assistant", "content": reply})
        print(f"\n  [results table: {len(results)} rows]")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    if sys.argv[1] == "--chat":
        chat()
    else:
        scrape_once(sys.argv[1])


if __name__ == "__main__":
    main()
