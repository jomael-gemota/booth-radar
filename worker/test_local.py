"""
test_local.py  –  Test the scraper + extractor pipeline locally without
                  the server or WebSocket.

Usage:
    python test_local.py https://example.com/exhibitors
"""

from __future__ import annotations

import json
import sys

from dotenv import load_dotenv

load_dotenv()

from scraper import scrape
from extractor import extract


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python test_local.py <url>")
        sys.exit(1)

    url = sys.argv[1]
    print(f"[test] scraping {url!r} …")
    page_text = scrape(url)
    print(f"[test] got {len(page_text):,} chars")
    print("[test] extracting …")

    results = extract(page_text, url, on_token=lambda t: print(t, end="", flush=True))
    print()  # newline after streaming output
    print(f"\n[test] {len(results)} results:")
    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
