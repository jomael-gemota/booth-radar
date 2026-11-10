"""
test_local.py  –  Test the worker locally without the server or WebSocket.

Usage:
    python test_local.py https://example.com/exhibitors   # one-shot scrape + extract
    python test_local.py https://example.com/item --fields "Name,Price,SKU"
                                                           # one-shot extract of chosen fields
    python test_local.py --feeds https://example.com       # list data feeds, read the first
    python test_local.py --capture https://example.com/search [--scroll]
                                                           # record a page's JSON APIs, read the first
    python test_local.py --chat                            # chat with the agent in the terminal
    python test_local.py --probe-api                       # probe Hermes API for thinking format
    python test_local.py --probe-server                    # discover all server endpoints + params
    python test_local.py --probe-raw                       # dump raw SSE wire bytes (bypasses SDK)
    python test_local.py --probe-raw "message"             # same, as a real Booth Radar turn
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

from dotenv import load_dotenv

load_dotenv()

import site_feeds
import tool_bridge
from agent import AGENT_TOOL_MODE, run_turn
from extractor import extract, _get_client
from scraper import scrape


def scrape_once(url: str, fields: list[str] | None = None) -> None:
    print(f"[test] scraping {url!r} …")
    started = time.monotonic()
    page_text = scrape(url)
    print(f"[test] got {len(page_text):,} chars in {time.monotonic() - started:.1f}s")
    print(f"[test] extracting {fields or 'brand + booth'} …")

    started = time.monotonic()
    results = extract(page_text, url, on_token=lambda t: print(t, end="", flush=True), fields=fields)
    print()  # newline after streaming output
    print(f"\n[test] {len(results)} results in {time.monotonic() - started:.1f}s:")
    print(json.dumps(results, indent=2, ensure_ascii=False))


def feeds(url: str) -> None:
    started = time.monotonic()
    found = site_feeds.find_feeds(url)
    print(f"[test] {len(found)} feeds in {time.monotonic() - started:.1f}s")
    for feed in found:
        print(feed.describe())
    if found:
        started = time.monotonic()
        records = site_feeds.read_feed(url, found[0].id)
        print(f"\n[test] {found[0].id}: {len(records)} records in {time.monotonic() - started:.1f}s; first:")
        print(json.dumps(records[:1], indent=2, ensure_ascii=False))


def capture(url: str, scroll: bool) -> None:
    started = time.monotonic()
    found = site_feeds.capture_api(url, scroll)
    print(f"[test] {len(found)} JSON record endpoints in {time.monotonic() - started:.1f}s")
    for feed in found:
        print(feed.describe())
    if found:
        started = time.monotonic()
        records = site_feeds.read_feed(url, found[0].id)
        print(f"\n[test] {found[0].id}: {len(records)} records in {time.monotonic() - started:.1f}s; first:")
        print(json.dumps(records[:1], indent=2, ensure_ascii=False))


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

    if AGENT_TOOL_MODE == "mcp":
        tool_bridge.start()
    print(f"[test] chat mode ({AGENT_TOOL_MODE} tools) — Ctrl+C to quit")
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


def probe_server() -> None:
    """
    Discover every route + parameter the Hermes server exposes, then try
    various extra_body flags known to enable thinking on different servers.
    """
    import urllib.request
    import urllib.error

    base = (os.getenv("HERMES_BASE_URL") or "http://localhost:8642/v1").rstrip("/v1").rstrip("/")
    model = os.getenv("HERMES_MODEL") or "hermes-agent"
    print(f"[probe-server] base={base!r}  model={model!r}\n")

    # ── 1. Discover endpoints ─────────────────────────────────────────────
    discovery_paths = [
        "/",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/v1/models",
        "/v1/chat/completions",   # HEAD only
        "/api",
        "/api/docs",
        "/api/openapi.json",
    ]
    print("\033[1m── Endpoint discovery ──\033[0m")
    for path in discovery_paths:
        url = base + path
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=4) as r:
                ct = r.headers.get("Content-Type", "")
                body_preview = r.read(400).decode("utf-8", errors="replace")
                print(f"  \033[32m{r.status}\033[0m  {url}  [{ct}]")
                if "openapi" in path or "json" in ct:
                    print(f"       {body_preview[:300]}")
        except urllib.error.HTTPError as e:
            print(f"  \033[33m{e.code}\033[0m  {url}")
        except Exception as e:
            print(f"  \033[31mERR\033[0m  {url}  ({e})")

    # ── 2. Try common thinking-enablement parameters ──────────────────────
    print("\n\033[1m── Thinking parameter probes ──\033[0m")
    probe_cases: list[tuple[str, dict]] = [
        ("include_reasoning=true",         {"include_reasoning": True}),
        ("stream_thinking=true",           {"stream_thinking": True}),
        ("show_thinking=true",             {"show_thinking": True}),
        ("thinking={enabled}",             {"thinking": {"type": "enabled", "budget_tokens": 512}}),
        ("reasoning_effort=high",          {"reasoning_effort": "high"}),
        ("reasoning={effort:high}",        {"reasoning": {"effort": "high"}}),
        ("extended_thinking=true",         {"extended_thinking": True}),
        ("enable_thinking=true",           {"enable_thinking": True}),
    ]

    client = _get_client()
    for label, extra in probe_cases:
        print(f"\n  trying extra_body={label} …", end=" ", flush=True)
        try:
            stream = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "Say: hello"}],
                stream=True,
                max_tokens=60,
                extra_body=extra,
            )
            chunks = []
            found_reasoning = False
            with stream:
                for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    r = getattr(delta, "reasoning_content", None) or ""
                    c = getattr(delta, "content", None) or ""
                    if r:
                        found_reasoning = True
                        chunks.append(f"\033[33m[R]{r}\033[0m")
                    if c:
                        if "<think>" in c or "</think>" in c:
                            found_reasoning = True
                        chunks.append(c)
            result = "".join(chunks)
            if found_reasoning:
                print(f"\033[32m✓ THINKING FOUND!\033[0m → {result[:120]}")
            else:
                print(f"no thinking  → {result[:80]}")
        except Exception as e:
            print(f"\033[31mERROR: {e}\033[0m")


def probe_raw(message: str | None = None) -> None:
    """
    Bypass the OpenAI SDK entirely and print every raw SSE line from the server.
    The SDK silently drops non-standard event types (e.g. 'event: thinking').
    This shows us the true wire format. With *message*, sends it with the real
    Booth Radar system prompt so the stream matches a live turn.
    """
    import urllib.request
    from agent import SYSTEM_PROMPT, _context_prompt
    from results_table import ResultsTable

    if message:
        messages = [
            {"role": "system", "content": f"{SYSTEM_PROMPT}\n\n{_context_prompt(ResultsTable())}"},
            {"role": "user", "content": message},
        ]
    else:
        messages = [{"role": "user", "content": "Think step by step: what is 7 × 8?"}]

    base_url = os.getenv("HERMES_BASE_URL") or "http://localhost:8642/v1"
    api_key  = os.getenv("HERMES_API_KEY") or "not-needed"
    model    = os.getenv("HERMES_MODEL") or "hermes-agent"
    url      = base_url.rstrip("/") + "/chat/completions" if not base_url.endswith("/chat/completions") else base_url

    payload = json.dumps({
        "model":    model,
        "messages": messages,
        "stream":   True,
        "max_tokens": 2000 if message else 300,
    }).encode()

    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {api_key}",
            "Accept":        "text/event-stream",
        },
    )

    print(f"[probe-raw] POST {url}\n")
    print("\033[90m── Raw SSE wire (every line printed) ──\033[0m\n")

    event_types: set[str] = set()
    field_names: set[str] = set()

    try:
        with urllib.request.urlopen(req, timeout=300 if message else 30) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    continue

                # Colour-code by field type
                if line.startswith("event:"):
                    etype = line[6:].strip()
                    event_types.add(etype)
                    print(f"\033[35m{line}\033[0m")   # magenta = event type
                elif line.startswith("data:"):
                    data = line[5:].strip()
                    if data == "[DONE]":
                        print(f"\033[90m{line}\033[0m")
                    else:
                        try:
                            parsed = json.loads(data)
                            field_names.update(parsed.keys())
                            print(f"\033[36mdata:\033[0m {json.dumps(parsed)}")
                        except json.JSONDecodeError:
                            print(f"\033[33mdata (raw):\033[0m {data[:200]}")
                else:
                    print(f"\033[90m{line}\033[0m")   # grey = other

    except Exception as e:
        print(f"\033[31mERROR: {e}\033[0m")
        return

    print(f"\n\033[1m── Summary ──\033[0m")
    print(f"  SSE event types seen : {event_types or {'(none — all plain data:)'}}")
    print(f"  Top-level data fields: {field_names}")
    if event_types - {"message", ""}:
        print(f"\033[32m  ✓ Custom event types found! These are hidden by the OpenAI SDK.\033[0m")
    else:
        print(f"  No custom event types. Thinking is not exposed at the SSE level.")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    if sys.argv[1] == "--chat":
        chat()
    elif sys.argv[1] == "--probe-api":
        probe_api()
    elif sys.argv[1] == "--probe-server":
        probe_server()
    elif sys.argv[1] == "--probe-raw":
        probe_raw(sys.argv[2] if len(sys.argv) > 2 else None)
    elif sys.argv[1] == "--feeds" and len(sys.argv) > 2:
        feeds(sys.argv[2])
    elif sys.argv[1] == "--capture" and len(sys.argv) > 2:
        capture(sys.argv[2], "--scroll" in sys.argv[3:])
    elif len(sys.argv) > 3 and sys.argv[2] == "--fields":
        scrape_once(sys.argv[1], [f.strip() for f in sys.argv[3].split(",") if f.strip()])
    else:
        scrape_once(sys.argv[1])


if __name__ == "__main__":
    main()
