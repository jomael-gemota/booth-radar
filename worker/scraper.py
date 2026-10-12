"""
scraper.py  –  Fetch and clean text from an exhibitor website.

Strategy:
  1. Try Playwright (headless Chromium) to handle JS-rendered SPAs.
  2. If the resulting text is too short (< MIN_TEXT_LEN chars),
     fall back to requests + BeautifulSoup for static pages.
"""

from __future__ import annotations

import logging
import re

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

MIN_TEXT_LEN = 200  # chars; below this, try the requests fallback

# Tags whose text we always strip (navigation chrome, boilerplate)
_NOISE_TAGS = {"script", "style", "noscript", "header", "footer", "nav", "iframe"}


def _clean(soup: BeautifulSoup) -> str:
    """Extract visible text from a BeautifulSoup tree."""
    for tag in soup(list(_NOISE_TAGS)):
        tag.decompose()
    text = soup.get_text(separator="\n")
    # Collapse blank lines
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines)


def _fetch_requests(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0 Safari/537.36 BoothRadar/1.0"
        )
    }
    resp = requests.get(url, headers=headers, timeout=30, allow_redirects=True)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    return _clean(soup)


def _fetch_playwright(url: str) -> str:
    """Use Playwright (sync API) for JS-rendered pages."""
    # Import here so the worker can still start if Playwright is not installed;
    # callers should handle ImportError.
    from playwright.sync_api import sync_playwright  # type: ignore

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="networkidle", timeout=45_000)
        html = page.content()
        browser.close()

    soup = BeautifulSoup(html, "html.parser")
    return _clean(soup)


def scrape(url: str) -> str:
    """
    Scrape *url* and return clean visible text.
    Raises on unrecoverable errors.
    """
    # Validate URL shape
    if not re.match(r"https?://", url, re.IGNORECASE):
        raise ValueError(f"Invalid URL: {url!r}")

    text = ""

    # 1. Try Playwright
    try:
        log.info("[scraper] trying Playwright for %s", url)
        text = _fetch_playwright(url)
        log.info("[scraper] Playwright returned %d chars", len(text))
    except ImportError:
        log.warning("[scraper] Playwright not available, using requests fallback")
    except Exception as exc:
        log.warning("[scraper] Playwright failed (%s), falling back to requests", exc)

    # 2. Fallback: requests + BS4
    if len(text) < MIN_TEXT_LEN:
        log.info("[scraper] falling back to requests for %s", url)
        text = _fetch_requests(url)
        log.info("[scraper] requests returned %d chars", len(text))

    if not text.strip():
        raise RuntimeError("Scraping returned empty content")

    return text
