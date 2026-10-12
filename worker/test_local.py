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
    Two-pass probe of the Hermes API:
      Pass 1 — raw prompt (does the model produce thinking natively?)
      Pass 2 — with our <think> system instruction (does it follow it?)
    """
    client = _get_client()
    model  = os.getenv("HERMES_MODEL") or "hermes-agent"
    print(f"[probe] model={model!r}  base_url={os.getenv('HERMES_BASE_URL')!r}\n")

    system_with_think = (
        "Before EVERY reply, reason step by step inside <think>…</think> tags. "
        "After </think>, write your visible answer."
    )

    for label, messages in [
        ("Pass 1 — no system prompt (native thinking?)", [
            {"role": "user", "content": "Think step by step: what is 12 × 17?"},
        ]),
        ("Pass 2 — with <think> instruction in system prompt", [
            {"role": "system", "content": system_with_think},
            {"role": "user",   "content": "What is 12 × 17?"},
        ]),
    ]:
        print(f"\033[1m── {label} ──\033[0m")
        stream = client.chat.completions.create(
            model=model,
            messages=messages,
            stream=True,
            max_tokens=400,
        )

        seen_reasoning = False
        seen_think_tag = False
        last_delta_dump = ""

        with stream:
            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
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
                        print("\033[33m[reasoning_content]\033[0m ", end="")
                        seen_reasoning = True
                    print(f"\033[33m{reasoning}\033[0m", end="", flush=True)

                if content:
                    if "<think>" in content or "</think>" in content:
                        seen_think_tag = True
                    print(content, end="", flush=True)

        print(f"\n\n\033[90mδ dump: {last_delta_dump or '(empty)'}\033[0m")

        if seen_reasoning:
            verdict = "\033[32m✓ reasoning_content — captured natively\033[0m"
        elif seen_think_tag:
            verdict = "\033[32m✓ <think> tags in content — filter captures them\033[0m"
        else:
            verdict = "\033[31m✗ no thinking tokens in this pass\033[0m"
        print(verdict + "\n")


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
