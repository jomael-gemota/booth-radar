"""
extractor.py  –  Call the Hermes / OpenAI-compatible LLM.

  * chat_stream()  – generic streaming chat completion (used by the agent loop)
  * extract()      – extract brand + booth listings from scraped page text
"""

from __future__ import annotations

import json
import logging
import os
from typing import Callable, Optional

from openai import OpenAI

log = logging.getLogger(__name__)

MAX_PAGE_CHARS    = int(os.getenv("MAX_PAGE_CHARS", "40000"))
REQUEST_TIMEOUT   = int(os.getenv("REQUEST_TIMEOUT", "120"))
MAX_TOKENS        = int(os.getenv("MAX_TOKENS", "8192"))

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
) -> str:
    """Run a streaming chat completion and return the full response text."""
    model = os.getenv("HERMES_MODEL") or "hermes-agent"
    full_text = ""

    stream = _get_client().chat.completions.create(
        model=model,
        messages=messages,  # type: ignore[arg-type]
        stream=True,
        max_tokens=MAX_TOKENS,
    )
    with stream:
        for chunk in stream:
            if not chunk.choices:
                continue
            content = getattr(chunk.choices[0].delta, "content", None) or ""
            if content:
                full_text += content
                if on_token:
                    on_token(content)

    return full_text


def extract(
    page_text: str,
    url: str,
    on_token: Optional[Callable[[str], None]] = None,
) -> list[dict[str, str]]:
    """
    Run an LLM extraction over *page_text* and return the parsed list of
    {"brand": …, "booth": …} dicts.

    Raises on LLM or JSON parse errors.
    """
    truncated = page_text[:MAX_PAGE_CHARS]
    if len(page_text) > MAX_PAGE_CHARS:
        log.warning(
            "[extractor] page text truncated from %d to %d chars",
            len(page_text),
            MAX_PAGE_CHARS,
        )

    user_content = (
        f"Exhibitor website: {url}\n\n"
        f"--- SCRAPED TEXT START ---\n{truncated}\n--- SCRAPED TEXT END ---"
    )

    log.info("[extractor] extracting from %s", url)
    full_text = chat_stream(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_content},
        ],
        on_token,
    )
    log.info("[extractor] received %d chars total", len(full_text))

    # The model may wrap the array in fences or prose; take the outermost [...]
    start, end = full_text.find("["), full_text.rfind("]")
    if start == -1 or end < start:
        raise ValueError("The model did not return a JSON array")
    results = json.loads(full_text[start:end + 1])
    if not isinstance(results, list):
        raise ValueError(f"Expected a JSON array, got {type(results).__name__}")

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
