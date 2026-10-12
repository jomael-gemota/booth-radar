"""
extractor.py  –  Call the Hermes / OpenAI-compatible LLM to extract
                 brand + booth listings from scraped page text.

Streams reasoning tokens back via a callback so the server can relay
them to the browser in real time.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Callable

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
  - Output the raw JSON array only — no markdown fences, no explanation.
"""


def extract(
    page_text: str,
    url: str,
    on_token: Callable[[str], None],
) -> list[dict[str, str]]:
    """
    Stream an LLM extraction over *page_text*.

    Calls *on_token(chunk)* for each streamed text chunk so the caller
    can relay it to the browser.  Returns the parsed list of
    {"brand": …, "booth": …} dicts when done.

    Raises on LLM or JSON parse errors.
    """
    client = OpenAI(
        base_url=os.getenv("HERMES_BASE_URL") or None,
        api_key=os.getenv("HERMES_API_KEY", ""),
        timeout=REQUEST_TIMEOUT,
    )

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

    model = os.getenv("HERMES_MODEL", "hermes-agent")
    log.info("[extractor] calling %s with model %s", os.getenv("HERMES_BASE_URL", "OpenAI"), model)

    full_text = ""

    with client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_content},
        ],
        stream=True,
        max_tokens=MAX_TOKENS,
    ) as stream:
        for chunk in stream:
            delta = chunk.choices[0].delta
            content = getattr(delta, "content", None) or ""
            if content:
                full_text += content
                on_token(content)

    log.info("[extractor] received %d chars total", len(full_text))

    # Parse JSON — strip possible markdown fences if the model added them
    cleaned = re.sub(r"^```(?:json)?\s*", "", full_text.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    results: list[dict[str, str]] = json.loads(cleaned)
    if not isinstance(results, list):
        raise ValueError(f"Expected a JSON array, got {type(results).__name__}")

    # Normalise keys
    normalised = []
    for item in results:
        normalised.append({
            "brand": str(item.get("brand", "")).strip(),
            "booth": str(item.get("booth", "N/A")).strip() or "N/A",
        })

    log.info("[extractor] parsed %d entries", len(normalised))
    return normalised
