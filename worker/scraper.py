"""
scraper.py  –  Fetch and clean text from an exhibitor website.

Strategy:
  1. Try Playwright (headless Chromium) to handle JS-rendered SPAs.
  2. If the resulting text is too short (< MIN_TEXT_LEN chars),
     fall back to requests + BeautifulSoup for static pages.

Pages are loaded on a shared pool of SCRAPE_CONCURRENCY threads, each keeping a
warm Chromium, and results are cached in page_cache.pages.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urljoin, urldefrag

import requests
from bs4 import BeautifulSoup

import page_cache

log = logging.getLogger(__name__)

MIN_TEXT_LEN = 200  # chars; below this, try the requests fallback
MAX_LINKS = 150
SCRAPE_CONCURRENCY     = max(1, int(os.getenv("SCRAPE_CONCURRENCY", "4")))
SCRAPE_IDLE_TIMEOUT_MS = int(os.getenv("SCRAPE_IDLE_TIMEOUT_MS", "8000"))

_BLOCKED_RESOURCES = {"image", "media", "font"}

# Playwright's sync API is bound to the thread that started it, so each pool
# thread owns its own Playwright instance and browser.
_local = threading.local()


def _mark_pool_thread() -> None:
    _local.in_pool = True


_pool = ThreadPoolExecutor(
    max_workers=SCRAPE_CONCURRENCY,
    thread_name_prefix="scrape",
    initializer=_mark_pool_thread,
)

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


def _links(soup: BeautifulSoup, base_url: str) -> list[tuple[str, str]]:
    """Return de-duplicated (label, absolute_url) pairs for http(s) links."""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=True):
        href = urldefrag(urljoin(base_url, a["href"]))[0]
        if not href.lower().startswith(("http://", "https://")) or href in seen:
            continue
        seen.add(href)
        label = " ".join(a.get_text(" ").split())[:80]
        out.append((label, href))
        if len(out) >= MAX_LINKS:
            break
    return out


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
    return resp.text


def _browser() -> Any:
    """Return this thread's Chromium, launching it on first use or after a crash."""
    browser = getattr(_local, "browser", None)
    if browser is not None and browser.is_connected():
        return browser
    # Import here so the worker can still start if Playwright is not installed;
    # callers should handle ImportError.
    from playwright.sync_api import sync_playwright  # type: ignore

    if getattr(_local, "playwright", None) is None:
        _local.playwright = sync_playwright().start()
    log.info("[scraper] launching Chromium on %s", threading.current_thread().name)
    _local.browser = _local.playwright.chromium.launch(headless=True)
    return _local.browser


def _block_heavy_assets(route: Any) -> None:
    if route.request.resource_type in _BLOCKED_RESOURCES:
        route.abort()
    else:
        route.continue_()


def _render(url: str) -> str:
    from playwright.sync_api import TimeoutError as PlaywrightTimeout  # type: ignore

    context = _browser().new_context()
    try:
        context.route("**/*", _block_heavy_assets)
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        try:
            page.wait_for_load_state("networkidle", timeout=SCRAPE_IDLE_TIMEOUT_MS)
        except PlaywrightTimeout:
            # Sites with constant polling never go idle; use what has rendered.
            log.info("[scraper] networkidle timed out for %s, using current DOM", url)
        return page.content()
    finally:
        try:
            context.close()
        except Exception:
            pass


def _fetch_playwright(url: str) -> str:
    """Use Playwright (sync API) for JS-rendered pages."""
    try:
        return _render(url)
    except ImportError:
        raise
    except Exception:
        browser = getattr(_local, "browser", None)
        if browser is not None and browser.is_connected():
            raise
        log.warning("[scraper] Chromium disconnected, relaunching for %s", url)
        _local.browser = None
        return _render(url)


def fetch_html(url: str) -> str:
    """
    Return the HTML of *url*, rendered by Playwright when possible.
    Raises on unrecoverable errors.
    """
    if not re.match(r"https?://", url, re.IGNORECASE):
        raise ValueError(f"Invalid URL: {url!r}")

    html = ""
    playwright_error: str | None = None

    # 1. Try Playwright
    try:
        log.info("[scraper] trying Playwright for %s", url)
        html = _fetch_playwright(url)
        log.info("[scraper] Playwright returned %d chars of HTML", len(html))
    except ImportError:
        playwright_error = "Playwright is not installed"
        log.warning("[scraper] Playwright not available, using requests fallback")
    except Exception as exc:
        playwright_error = str(exc).splitlines()[0]
        log.warning("[scraper] Playwright failed (%s), falling back to requests", playwright_error)

    # 2. Fallback: requests
    if len(_clean(BeautifulSoup(html, "html.parser"))) < MIN_TEXT_LEN:
        log.info("[scraper] falling back to requests for %s", url)
        fallback = _fetch_requests(url)
        fallback_text = _clean(BeautifulSoup(fallback, "html.parser"))
        log.info("[scraper] requests returned %d chars", len(fallback_text))
        if len(fallback_text) < MIN_TEXT_LEN and playwright_error:
            raise RuntimeError(
                f"The page returned almost no text without a browser "
                f"({len(fallback_text)} chars) and the headless browser failed: {playwright_error}"
            )
        html = fallback

    return html


def _load(url: str) -> tuple[str, list[tuple[str, str]]]:
    soup = BeautifulSoup(fetch_html(url), "html.parser")
    links = _links(soup, url)
    text = _clean(soup)
    if not text.strip():
        raise RuntimeError("Scraping returned empty content")
    return text, links


def load_page(url: str, refresh: bool = False) -> tuple[str, list[tuple[str, str]], bool]:
    """
    Return (visible_text, links, from_cache) for *url*, serving recently
    loaded pages from the cache unless *refresh* is set.
    """
    key = page_cache.url_key(url)
    if not refresh:
        cached = page_cache.pages.get(key)
        if cached is not None:
            log.info("[scraper] cache hit for %s", key)
            return cached[0], cached[1], True

    if getattr(_local, "in_pool", False):
        text, links = _load(key)
    else:
        text, links = _pool.submit(_load, key).result()
    page_cache.pages.set(key, (text, links))
    return text, links, False


def scrape(url: str, refresh: bool = False) -> str:
    """Scrape *url* and return clean visible text."""
    return load_page(url, refresh)[0]


def snapshot(url: str, refresh: bool = False) -> tuple[str, list[tuple[str, str]]]:
    """Scrape *url* and return (visible_text, links)."""
    text, links, _ = load_page(url, refresh)
    return text, links
