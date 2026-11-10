"""
extractor.py  –  Call the Hermes / OpenAI-compatible LLM.

  * chat_stream()  – generic streaming chat completion (used by the agent loop)
  * extract()      – extract requested fields (or brand + booth) from page text
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Any, Callable, Optional

from openai import BadRequestError, OpenAI

log = logging.getLogger(__name__)

MAX_PAGE_CHARS    = int(os.getenv("MAX_PAGE_CHARS", "40000"))
REQUEST_TIMEOUT   = int(os.getenv("REQUEST_TIMEOUT", "120"))
MAX_TOKENS        = int(os.getenv("MAX_TOKENS", "8192"))
ACK_MODEL         = os.getenv("ACK_MODEL", "gpt-4o-mini")
# Page extraction runs on a plain chat model (e.g. local Ollama) when set, so each
# page is one completion instead of a full Hermes agent run.
EXTRACT_BASE_URL  = os.getenv("EXTRACT_BASE_URL", "")
EXTRACT_MODEL     = os.getenv("EXTRACT_MODEL", "")

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


# ── Thinking filter ────────────────────────────────────────────────────────

class _ThinkingFilter:
    """
    Splits a streamed content string into normal text and <think>…</think>
    reasoning blocks, emitting each chunk to the appropriate callback without
    buffering more than one partial tag at a time.
    """

    _OPEN  = "<think>"
    _CLOSE = "</think>"

    def __init__(
        self,
        on_content: Callable[[str], None],
        on_thinking: Callable[[str], None],
    ) -> None:
        self._on_content  = on_content
        self._on_thinking = on_thinking
        self._buf         = ""
        self._in_think    = False

    def feed(self, chunk: str) -> None:
        self._buf += chunk
        self._process()

    def _process(self) -> None:
        tag = self._CLOSE if self._in_think else self._OPEN
        while tag in self._buf:
            idx    = self._buf.index(tag)
            before = self._buf[:idx]
            self._buf = self._buf[idx + len(tag):]
            if before:
                (self._on_thinking if self._in_think else self._on_content)(before)
            self._in_think = not self._in_think
            tag = self._CLOSE if self._in_think else self._OPEN

        # Hold back a suffix that could be the start of the next tag boundary
        hold = 0
        for n in range(min(len(tag) - 1, len(self._buf)), 0, -1):
            if tag.startswith(self._buf[-n:]):
                hold = n
                break
        safe = self._buf[: len(self._buf) - hold]
        if safe:
            (self._on_thinking if self._in_think else self._on_content)(safe)
            self._buf = self._buf[len(self._buf) - hold:]

    def flush(self) -> None:
        if self._buf:
            (self._on_thinking if self._in_think else self._on_content)(self._buf)
            self._buf = ""

SYSTEM_PROMPT = """\
You are Booth Radar, an expert web-scraping assistant.

Your task:
  Given raw text scraped from an exhibitor/trade-show website, extract every exhibitor
  brand and its booth number or location.

Output format (IMPORTANT — output ONLY this, no prose before or after):
  A valid JSON array where each element is:
    {"brand": "<brand name>", "booth": "<booth number or location>"}

Rules:
  - If a booth number is not listed, use "N/A" for the booth field.
  - Normalise brand names (proper title case, remove stray symbols).
  - De-duplicate entries (same brand, same booth = one row).
  - Do not invent brands not present in the source text.
  - If the page lists no exhibitors, output [].
  - Output the raw JSON array only — no markdown fences, no explanation.
"""

_client: Optional[OpenAI] = None
_ack_client: Optional[OpenAI] = None
_extract_client: Optional[OpenAI] = None
_extract_params: dict[str, Any] = {"max_tokens": MAX_TOKENS, "temperature": 0}


def quick_completion(messages: list[dict[str, str]], max_tokens: int = 120) -> str:
    """Short non-streaming reply from ACK_MODEL on OpenAI (needs OPENAI_API_KEY)."""
    global _ack_client
    if _ack_client is None:
        _ack_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], timeout=15)
    resp = _ack_client.chat.completions.create(
        model=ACK_MODEL,
        messages=messages,  # type: ignore[arg-type]
        max_tokens=max_tokens,
        temperature=0.8,
    )
    return (resp.choices[0].message.content or "").strip()


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            base_url=os.getenv("HERMES_BASE_URL") or None,
            api_key=os.getenv("HERMES_API_KEY") or "not-needed",
            timeout=REQUEST_TIMEOUT,
        )
    return _client


def chat_stream(
    messages: list[dict[str, str]],
    on_token: Optional[Callable[[str], None]] = None,
    on_thinking: Optional[Callable[[str], None]] = None,
    cancel: Optional["threading.Event"] = None,
    on_tool_progress: Optional[Callable[[str], None]] = None,
    timeout: Optional[int] = None,
) -> str:
    """
    Run a streaming chat completion using raw HTTP so that custom SSE event
    types emitted by the Hermes server (e.g. ``event: hermes.tool.progress``)
    are captured.  The OpenAI Python SDK silently drops unknown event types,
    which is why these never reached the caller before.

    Thinking / reasoning tokens are routed to *on_thinking* (if provided).
    Three sources are handled in priority order:

    1. ``event: hermes.tool.progress`` — Hermes native tool-execution events.
       Running events are forwarded to *on_thinking* as a one-liner.
    2. ``event: hermes.reason*`` — Hermes native reasoning events (if present).
    3. ``reasoning_content`` field on the delta (xAI / DeepSeek-R1 style).
    4. ``<think>…</think>`` tags inside ``content`` (Hermes-3 native text format).
    """
    import urllib.request
    import urllib.error

    base_url = (os.getenv("HERMES_BASE_URL") or "http://localhost:8642/v1").rstrip("/")
    api_key  = os.getenv("HERMES_API_KEY") or "not-needed"
    model    = os.getenv("HERMES_MODEL") or "hermes-agent"
    url      = base_url + "/chat/completions"

    payload = json.dumps({
        "model":      model,
        "messages":   messages,
        "stream":     True,
        "max_tokens": MAX_TOKENS,
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

    full_text       = ""
    thinking_chunks = 0
    content_chunks  = 0

    def _handle_content(chunk: str) -> None:
        nonlocal full_text, content_chunks
        full_text      += chunk
        content_chunks += 1
        if on_token:
            on_token(chunk)

    think_filt: Optional[_ThinkingFilter] = (
        _ThinkingFilter(on_content=_handle_content, on_thinking=on_thinking)  # type: ignore[arg-type]
        if on_thinking
        else None
    )

    current_event: Optional[str] = None

    try:
        with urllib.request.urlopen(req, timeout=timeout or REQUEST_TIMEOUT) as resp:
            for raw_line in resp:
                if cancel and cancel.is_set():
                    break
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")

                if not line:
                    current_event = None   # blank line = end of SSE event
                    continue

                if line.startswith("event:"):
                    current_event = line[6:].strip()
                    continue

                if not line.startswith("data:"):
                    continue

                data = line[5:].strip()
                if data == "[DONE]":
                    break

                try:
                    parsed = json.loads(data)
                except json.JSONDecodeError:
                    log.warning("[extractor] non-JSON SSE data: %r", data[:120])
                    current_event = None
                    continue

                evt           = current_event
                current_event = None

                # ── hermes.tool.progress ──────────────────────────────────
                if evt == "hermes.tool.progress":
                    progress_cb = on_tool_progress or on_thinking
                    if parsed.get("status") == "running" and progress_cb:
                        emoji = parsed.get("emoji", "⚙️")
                        label = parsed.get("label") or parsed.get("tool", "tool")
                        thinking_chunks += 1
                        progress_cb(f"{emoji} {label}\n")
                    continue

                # ── hermes.reason* (reasoning / think events) ────────────
                if evt and evt.startswith("hermes.reason"):
                    text = (
                        parsed.get("text")
                        or parsed.get("content")
                        or parsed.get("delta")
                        or json.dumps(parsed)
                    )
                    if on_thinking and text:
                        thinking_chunks += 1
                        on_thinking(str(text))
                    continue

                # ── other hermes.* events — log for future discovery ──────
                if evt and evt.startswith("hermes."):
                    log.debug("[extractor] unhandled hermes event %r: %s", evt, json.dumps(parsed)[:200])
                    continue

                # ── standard OpenAI streaming chunk ───────────────────────
                choices = parsed.get("choices")
                if not choices:
                    continue
                delta = choices[0].get("delta", {})

                # Format 1: native reasoning_content (xAI / DeepSeek-R1)
                reasoning = delta.get("reasoning_content") or ""
                if reasoning:
                    thinking_chunks += 1
                    if thinking_chunks == 1:
                        log.debug("[extractor] thinking via reasoning_content")
                    if on_thinking:
                        on_thinking(reasoning)
                    continue

                # Format 2: content stream (may embed <think> tags)
                content = delta.get("content") or ""
                if not content:
                    continue

                if think_filt:
                    think_filt.feed(content)
                else:
                    _handle_content(content)

    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read(300).decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"Hermes API HTTP {exc.code} {exc.reason}: {body}") from exc
    except Exception as exc:
        raise RuntimeError(f"Hermes stream error: {exc}") from exc

    if think_filt:
        think_filt.flush()

    log.debug(
        "[extractor] stream done — thinking_chunks=%d content_chunks=%d full_text_len=%d",
        thinking_chunks, content_chunks, len(full_text),
    )
    if thinking_chunks == 0 and on_thinking:
        log.info("[extractor] no thinking events from Hermes for this request")

    return full_text


def _get_extract_client() -> OpenAI:
    global _extract_client
    if _extract_client is None:
        _extract_client = OpenAI(
            base_url=EXTRACT_BASE_URL or None,
            api_key=os.getenv("EXTRACT_API_KEY") or "not-needed",
            timeout=REQUEST_TIMEOUT,
        )
    return _extract_client


def _open_extraction_stream(messages: list[dict[str, str]]) -> Any:
    """
    Start the EXTRACT_MODEL stream. Newer OpenAI models reject `max_tokens` and
    `temperature`, while most other servers still expect `max_tokens`; whatever the
    model refuses is adjusted once and remembered for later pages.
    """
    global _extract_params
    for _ in range(3):
        params = dict(_extract_params)
        try:
            return _get_extract_client().chat.completions.create(
                model=EXTRACT_MODEL,
                messages=messages,  # type: ignore[arg-type]
                stream=True,
                **params,
            )
        except BadRequestError as exc:
            rejected = getattr(exc, "param", None)
            if rejected not in params:
                raise
            if rejected == "max_tokens":
                params["max_completion_tokens"] = params.pop("max_tokens")
            else:
                params.pop(rejected)
            log.info("[extractor] %s rejected %r, retrying with %s", EXTRACT_MODEL, rejected, sorted(params))
            _extract_params = params
    raise RuntimeError(f"{EXTRACT_MODEL} rejected the extraction request parameters")


def _extraction_completion(
    messages: list[dict[str, str]],
    on_token: Optional[Callable[[str], None]],
) -> str:
    """One plain completion on EXTRACT_MODEL, or a Hermes request when it is unset."""
    if not EXTRACT_MODEL:
        return chat_stream(messages, on_token)
    stream = _open_extraction_stream(messages)
    parts: list[str] = []
    with stream:
        for chunk in stream:
            if not chunk.choices:
                continue
            content = chunk.choices[0].delta.content or ""
            if content:
                parts.append(content)
                if on_token:
                    on_token(content)
    return "".join(parts)


GENERIC_PROMPT = """\
You are Booth Radar, a precise web data extractor.

You receive content from a public website (visible page text, optional JSON-LD
structured data, or JSON records from the site's data feed) plus the fields the
user wants. Extract one row per matching item.

Output format (IMPORTANT — output ONLY this, no prose before or after):
  A valid JSON array of objects with exactly these keys: {fields}

Rules:
  - Use "" when a value is not present in the source. Never guess or invent values.
  - Copy values as written in the source (trim whitespace, decode HTML entities).
  - A value may be derived from text that clearly states it (e.g. a height in a title).
  - Include every matching item, with no duplicates.
  - If nothing matches, output [].
  - Output the raw JSON array only — no markdown fences, no explanation.
"""


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(_as_text(v) for v in value if v not in (None, ""))
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()


def _parse_array(full_text: str) -> list[Any]:
    full_text = _THINK_RE.sub("", full_text)
    # The model may wrap the array in fences or prose; take the outermost [...]
    start, end = full_text.find("["), full_text.rfind("]")
    if start == -1 or end < start:
        raise ValueError("The model did not return a JSON array")
    results = json.loads(full_text[start:end + 1])
    if not isinstance(results, list):
        raise ValueError(f"Expected a JSON array, got {type(results).__name__}")
    return results


def extract(
    page_text: str,
    url: str,
    on_token: Optional[Callable[[str], None]] = None,
    fields: Optional[list[str]] = None,
    instructions: str = "",
) -> list[dict[str, str]]:
    """
    Run an LLM extraction over *page_text*. With *fields*, returns rows with exactly
    those keys; without, returns exhibitor {"brand": …, "booth": …} rows.

    Raises on LLM or JSON parse errors.
    """
    truncated = page_text[:MAX_PAGE_CHARS]
    if len(page_text) > MAX_PAGE_CHARS:
        log.warning(
            "[extractor] page text truncated from %d to %d chars",
            len(page_text),
            MAX_PAGE_CHARS,
        )

    if fields:
        system = GENERIC_PROMPT.replace("{fields}", json.dumps(fields, ensure_ascii=False))
        request = f"What to extract: {instructions.strip()}\n\n" if instructions.strip() else ""
        user_content = (
            f"Source: {url}\n{request}"
            f"--- CONTENT START ---\n{truncated}\n--- CONTENT END ---"
        )
    else:
        system = SYSTEM_PROMPT
        user_content = (
            f"Exhibitor website: {url}\n\n"
            f"--- SCRAPED TEXT START ---\n{truncated}\n--- SCRAPED TEXT END ---"
        )

    log.info("[extractor] extracting from %s", url)
    full_text = _extraction_completion(
        [
            {"role": "system", "content": system},
            {"role": "user",   "content": user_content},
        ],
        on_token,
    )
    log.info("[extractor] received %d chars total", len(full_text))
    results = _parse_array(full_text)

    if fields:
        rows = [
            {f: _as_text(item.get(f)) for f in fields}
            for item in results
            if isinstance(item, dict)
        ]
        rows = [r for r in rows if any(r.values())]
        log.info("[extractor] parsed %d rows", len(rows))
        return rows

    normalised = []
    for item in results:
        if not isinstance(item, dict):
            continue
        brand = str(item.get("brand", "")).strip()
        if not brand:
            continue
        normalised.append({
            "brand": brand,
            "booth": str(item.get("booth", "N/A")).strip() or "N/A",
        })

    log.info("[extractor] parsed %d entries", len(normalised))
    return normalised
