"""
test_local.py  –  Test the worker locally without the server or WebSocket.

Usage:
    python test_local.py https://example.com/exhibitors   # one-shot scrape + extract
    python test_local.py --chat                            # chat with the agent in the terminal
    python test_local.py --probe-api                       # dump raw Hermes delta fields to
                                                           # diagnose thinking token format
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from agent import run_turn
from extractor import extract, _get_client
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
        elif type_ == "thinking":
            print(f"\033[35m{payload['text']}\033[0m", end="", flush=True)  # magenta
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


def probe_api() -> None:
    """
    Send a single message to Hermes and print every raw field on every delta chunk.
    This tells us definitively which format the Hermes instance uses for thinking:
      • reasoning_content  (xAI / DeepSeek-R1 style)
      • <think>…</think>  (Hermes-3 native, inside content)
      • neither            (thinking not exposed)
    """
    client = _get_client()
    model  = os.getenv("HERMES_MODEL") or "hermes-agent"
    print(f"[probe] model={model!r}  base_url={os.getenv('HERMES_BASE_URL')!r}")
    print("[probe] prompt: 'Think step by step: what is 2 + 2?'\n")

    stream = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "Think step by step: what is 2 + 2?"}],
        stream=True,
        max_tokens=300,
    )

    seen_reasoning = False
    seen_think_tag = False
    last_delta_dump: str = ""

    with stream:
        for i, chunk in enumerate(stream):
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            # Capture a dump of the last non-empty delta for the summary
            try:
                raw = delta.model_dump()
                if any(v for v in raw.values() if v):
                    last_delta_dump = json.dumps(raw, default=str)
            except Exception:
                pass

            reasoning = getattr(delta, "reasoning_content", None) or ""
            content   = getattr(delta, "content",           None) or ""

            if reasoning:
                if not seen_reasoning:
                    print("\n\033[33m── reasoning_content detected ──\033[0m")
                    seen_reasoning = True
                print(f"\033[33m{reasoning}\033[0m", end="", flush=True)

            if content:
                if "<think>" in content or "</think>" in content:
                    if not seen_think_tag:
                        print("\n\033[33m── <think> tag detected in content ──\033[0m")
                        seen_think_tag = True
                print(content, end="", flush=True)

    print("\n\n\033[90m── last non-empty delta (model_dump) ──\033[0m")
    print(last_delta_dump or "(empty)")
    print()

    if seen_reasoning:
        print("\033[32m✓ Hermes uses reasoning_content  → our code will capture it.\033[0m")
    elif seen_think_tag:
        print("\033[32m✓ Hermes uses <think> tags in content  → our filter will capture it.\033[0m")
    else:
        print("\033[31m✗ No thinking tokens detected in this response.\033[0m")
        print("  Hermes may not expose reasoning via this API, or the model")
        print("  doesn't produce thinking for this prompt. Try a harder prompt.")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    if sys.argv[1] == "--chat":
        chat()
    elif sys.argv[1] == "--probe-api":
        probe_api()
    else:
        scrape_once(sys.argv[1])


if __name__ == "__main__":
    main()
