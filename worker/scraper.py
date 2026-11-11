"""
scraper.py  –  Fetch and clean text from a website.

Strategy:
  1. Plain HTTP when the page is server-rendered (SCRAPE_HTTP_FIRST).
  2. Otherwise the shared headless Chromium (browser.py) for JS-rendered pages.
  3. If the rendered text is too short (< MIN_TEXT_LEN chars),
     fall back to requests + BeautifulSoup.

Every load is checked by net_guard (public hosts, robots.txt) and holds one of the
host's DOMAIN_CONCURRENCY slots. Results are cached in page_cache.pages.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any
from urllib.parse import urljoin, urldefrag

from bs4 import BeautifulSoup

import browser as browser_service
import net_guard
import page_cache
from browser import SCRAPE_CONCURRENCY  # noqa: F401  (re-exported for agent.py)

log = logging.getLogger(__name__)

MIN_TEXT_LEN = 200  # chars; below this, try the requests fallback
MAX_LINKS = 150

# Kept out of the cached markup used by recipes (no data, lots of bytes)
_MARKUP_STRIP_TAGS = ["script", "style", "noscript", "svg", "template", "iframe", "link", "meta"]

# JSON-LD node types worth passing to the extractor (skips WebPage/Breadcrumb noise)
_LD_TYPES = {
    "Product", "ProductGroup", "Offer", "AggregateOffer", "ItemList", "ListItem",
    "Event", "ExhibitionEvent", "BusinessEvent", "LocalBusiness", "Store", "Place",
    "PostalAddress", "Brand", "JobPosting",
}
MAX_LD_CHARS = 8000

# Server-rendered pages are read over plain HTTP (much faster than Chromium); pages
# with little text or an empty JS app shell are rendered in the browser.
SCRAPE_HTTP_FIRST = os.getenv("SCRAPE_HTTP_FIRST", "true").strip().lower() not in ("0", "false", "no")
HTTP_MIN_TEXT_LEN = 1500
_SPA_SHELL = re.compile(r'<div[^>]+id="(?:root|app|__next|__nuxt)"[^>]*>\s*</div>', re.IGNORECASE)

# url keys whose plain HTTP fetch was not enough and were rendered by the browser
_rendered: set[str] = set()

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
    resp = net_guard.request("GET", url, robots=net_guard.RESPECT_ROBOTS, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.text


def _fetch_static(url: str) -> str | None:
    """Plain HTTP HTML when it already holds the page content; None if it needs a browser."""
    try:
        html = _fetch_requests(url)
    except PermissionError:
        raise
    except Exception as exc:
        log.info("[scraper] plain fetch failed for %s (%s), rendering instead", url, exc)
        return None
    if _SPA_SHELL.search(html):
        return None
    if len(_clean(BeautifulSoup(html, "html.parser"))) < HTTP_MIN_TEXT_LEN:
        return None
    return html


def fetch_html(url: str, browser: bool = False, wait_for: str | None = None) -> tuple[str, bool]:
    """
    Return (html, rendered_by_browser) for *url*. Unless *browser* is set, a plain
    HTTP fetch is used when the page is server-rendered; otherwise Chromium, waiting
    for the *wait_for* CSS selector when given. Raises on unrecoverable errors, and
    PermissionError for private hosts or pages robots.txt disallows.
    """
    if not re.match(r"https?://", url, re.IGNORECASE):
        raise ValueError(f"Invalid URL: {url!r}")
    net_guard.check_url(url, net_guard.RESPECT_ROBOTS)

    with net_guard.domain_slot(url):
        if SCRAPE_HTTP_FIRST and not browser:
            static = _fetch_static(url)
            if static is not None:
                log.info("[scraper] using plain HTTP for %s", url)
                return static, False

        html = ""
        browser_error: str | None = None

        # 1. Try the browser
        try:
            log.info("[scraper] rendering %s", url)
            html = browser_service.render(url, wait_for)
            log.info("[scraper] browser returned %d chars of HTML", len(html))
        except ImportError:
            browser_error = "Playwright is not installed"
            log.warning("[scraper] Playwright not available, using requests fallback")
        except Exception as exc:
            browser_error = str(exc).splitlines()[0]
            log.warning("[scraper] browser failed (%s), falling back to requests", browser_error)

        # 2. Fallback: requests
        if len(_clean(BeautifulSoup(html, "html.parser"))) < MIN_TEXT_LEN:
            log.info("[scraper] falling back to requests for %s", url)
            fallback = _fetch_requests(url)
            fallback_text = _clean(BeautifulSoup(fallback, "html.parser"))
            log.info("[scraper] requests returned %d chars", len(fallback_text))
            if len(fallback_text) < MIN_TEXT_LEN and browser_error:
                raise RuntimeError(
                    f"The page returned almost no text without a browser "
                    f"({len(fallback_text)} chars) and the headless browser failed: {browser_error}"
                )
            return fallback, False

    return html, True


def _ld_nodes(data: Any) -> list[dict[str, Any]]:
    """Flatten JSON-LD (lists and @graph) into nodes whose @type is useful data."""
    if isinstance(data, list):
        return [n for item in data for n in _ld_nodes(item)]
    if not isinstance(data, dict):
        return []
    if "@graph" in data:
        return _ld_nodes(data["@graph"])
    types = data.get("@type")
    types = types if isinstance(types, list) else [types]
    return [data] if _LD_TYPES & {str(t) for t in types} else []


def _structured_data(soup: BeautifulSoup) -> str:
    nodes: list[dict[str, Any]] = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            nodes += _ld_nodes(json.loads(tag.string or ""))
        except (json.JSONDecodeError, TypeError):
            continue
    if not nodes:
        return ""
    return json.dumps(nodes, ensure_ascii=False, separators=(",", ":"))[:MAX_LD_CHARS]


def parse_html(html: str, url: str) -> page_cache.Page:
    """(visible text + JSON-LD block, links, body HTML without scripts/styles) of a page."""
    soup = BeautifulSoup(html, "html.parser")
    links = _links(soup, url)
    structured = _structured_data(soup)
    for tag in soup(_MARKUP_STRIP_TAGS):
        tag.decompose()
    markup = str(soup.body or soup)
    text = _clean(soup)
    if structured:
        text += f"\n\n--- STRUCTURED DATA (JSON-LD) ---\n{structured}"
    return text, links, markup


def _load(url: str, browser: bool, wait_for: str | None) -> tuple[page_cache.Page, bool]:
    html, rendered = fetch_html(url, browser, wait_for)
    entry = parse_html(html, url)
    if not entry[0].strip():
        raise RuntimeError("Scraping returned empty content")
    return entry, rendered


def used_browser(url: str) -> bool:
    """True if the cached copy of *url* was already rendered by the browser."""
    return page_cache.url_key(url) in _rendered


def _page(url: str, refresh: bool, browser: bool, wait_for: str | None = None) -> tuple[page_cache.Page, bool]:
    key = page_cache.url_key(url)
    browser = browser and key not in _rendered
    cache_key = f"{key} [browser]" if browser else key
    if not refresh:
        cached = page_cache.pages.get(cache_key)
        if cached is not None:
            log.info("[scraper] cache hit for %s", cache_key)
            return cached, True

    entry, rendered = _load(key, browser, wait_for)
    if rendered and not browser:
        _rendered.add(key)
    page_cache.pages.set(cache_key, entry)
    return entry, False


def load_page(
    url: str, refresh: bool = False, browser: bool = False,
) -> tuple[str, list[tuple[str, str]], bool]:
    """
    Return (visible_text, links, from_cache) for *url*, serving recently
    loaded pages from the cache unless *refresh* is set. *browser* forces a
    browser render instead of a plain HTTP fetch.
    """
    (text, links, _), cached = _page(url, refresh, browser)
    return text, links, cached


def load_markup(
    url: str, refresh: bool = False, browser: bool = False, wait_for: str | None = None,
) -> tuple[str, str]:
    """Return (visible_text, body HTML without scripts/styles) for *url*, cached like load_page."""
    (text, _, markup), _ = _page(url, refresh, browser, wait_for)
    return text, markup


def scrape(url: str, refresh: bool = False, browser: bool = False) -> str:
    """Scrape *url* and return clean visible text."""
    return load_page(url, refresh, browser)[0]


def capture_json(url: str, scroll: bool = False) -> list[dict[str, Any]]:
    """
    Render *url* in Chromium (optionally scrolling to the bottom a few times) and
    return the JSON XHR/fetch responses it received: method, url, request headers,
    POST body and parsed JSON data.
    """
    if not re.match(r"https?://", url, re.IGNORECASE):
        raise ValueError(f"Invalid URL: {url!r}")
    net_guard.check_url(url, net_guard.RESPECT_ROBOTS)
    with net_guard.domain_slot(url):
        return browser_service.capture(page_cache.url_key(url), scroll)
