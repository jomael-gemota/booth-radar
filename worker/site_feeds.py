"""
site_feeds.py  –  Read a site's public data feeds and sitemaps instead of rendering pages.

  * find_feeds(url)        – WordPress REST content types and Shopify products.json
  * read_feed(url, feed)   – every record of one feed, flattened to plain fields
  * map_site(url, include) – page URLs listed in the site's sitemaps

All requests honour robots.txt for the BoothRadar user agent.
"""

from __future__ import annotations

import gzip
import html
import logging
import math
import os
import re
import threading
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any
from urllib import robotparser
from urllib.parse import urlsplit

import requests

import page_cache

log = logging.getLogger(__name__)

USER_AGENT       = "Mozilla/5.0 (compatible; BoothRadar/1.0)"
ROBOTS_AGENT     = "BoothRadar"
FEED_TIMEOUT     = 20
FEED_CONCURRENCY = 4
MAX_FEED_RECORDS = int(os.getenv("MAX_FEED_RECORDS", "2000"))
MAX_SITEMAPS     = 50
MAX_TERM_PAGES   = 10
SAMPLE_KEYS      = 25

_WP_SKIP_TYPES = {"attachment", "nav_menu_item"}
_WP_DROP_KEYS = {
    "guid", "_links", "_embedded", "yoast_head", "yoast_head_json", "class_list",
    "template", "meta", "featured_media", "author", "comment_status", "ping_status",
    "format", "sticky", "status", "type", "date_gmt", "modified_gmt", "menu_order",
    "parent",
}
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


@dataclass
class Feed:
    id: str
    label: str
    endpoint: str
    total: int | None = None
    sample_keys: list[str] = field(default_factory=list)
    # record field name -> endpoint listing that taxonomy's terms (WordPress)
    taxonomies: dict[str, str] = field(default_factory=dict)

    def describe(self) -> str:
        count = f"{self.total:,} records" if self.total is not None else "record count unknown"
        keys = f"; fields: {', '.join(self.sample_keys)}" if self.sample_keys else ""
        return f"- {self.id}: {self.label} ({count}){keys}"


# ── HTTP + robots.txt ──────────────────────────────────────────────────────

_robots: dict[str, robotparser.RobotFileParser | None] = {}
_robots_lock = threading.Lock()


def origin(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"Invalid URL: {url!r}")
    return f"{parts.scheme}://{parts.netloc}"


def _robots_for(site: str) -> robotparser.RobotFileParser | None:
    with _robots_lock:
        if site in _robots:
            return _robots[site]
    parser: robotparser.RobotFileParser | None = None
    try:
        resp = requests.get(f"{site}/robots.txt", headers={"User-Agent": USER_AGENT}, timeout=FEED_TIMEOUT)
        if resp.status_code == 200:
            parser = robotparser.RobotFileParser()
            parser.parse(resp.text.splitlines())
    except requests.RequestException as exc:
        log.info("[feeds] robots.txt unavailable for %s: %s", site, exc)
    with _robots_lock:
        _robots[site] = parser
    return parser


def _get(url: str, **params: Any) -> requests.Response:
    parser = _robots_for(origin(url))
    if parser is not None and not parser.can_fetch(ROBOTS_AGENT, url):
        raise PermissionError(f"robots.txt does not allow {url}")
    return requests.get(
        url,
        params=params or None,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json, application/xml, */*"},
        timeout=FEED_TIMEOUT,
    )


def _json(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


# ── Flattening ─────────────────────────────────────────────────────────────

def _text(value: str) -> str:
    return _SPACE_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", value))).strip()


def _plain(value: Any) -> Any:
    if isinstance(value, str):
        return _text(value) if "<" in value or "&" in value else value.strip()
    if isinstance(value, list):
        return [p for p in (_plain(v) for v in value) if p not in ("", None, [], {})]
    if isinstance(value, dict):
        if "rendered" in value:
            return _plain(value["rendered"])
        return {k: p for k, v in value.items() if (p := _plain(v)) not in ("", None, [], {})}
    return value


def _flatten_wp(record: dict[str, Any], terms: dict[str, dict[int, str]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in record.items():
        if key in _WP_DROP_KEYS:
            continue
        if key in terms and isinstance(value, list):
            value = [terms[key].get(v, v) for v in value]
        if key == "acf":
            for acf_key, acf_value in (value.items() if isinstance(value, dict) else []):
                out[acf_key if acf_key not in out else f"acf_{acf_key}"] = _plain(acf_value)
            continue
        out["url" if key == "link" else key] = _plain(value)
    return {k: v for k, v in out.items() if v not in ("", None, [], {})}


def _flatten_shopify(product: dict[str, Any], site: str) -> dict[str, Any]:
    variants = product.get("variants") or []
    prices = sorted(float(v["price"]) for v in variants if _is_number(v.get("price")))
    compare = sorted(float(v["compare_at_price"]) for v in variants if _is_number(v.get("compare_at_price")))
    out: dict[str, Any] = {
        "title": product.get("title"),
        "url": f"{site}/products/{product.get('handle')}",
        "vendor": product.get("vendor"),
        "product_type": product.get("product_type"),
        "tags": product.get("tags"),
        "price_min": prices[0] if prices else None,
        "price_max": prices[-1] if prices else None,
        "compare_at_price": compare[-1] if compare else None,
        "available": any(v.get("available") for v in variants) if variants else None,
        "skus": [v.get("sku") for v in variants if v.get("sku")],
        "variants": [v.get("title") for v in variants if v.get("title") not in (None, "Default Title")],
        "description": _text(product.get("body_html") or "")[:1500],
        "published_at": product.get("published_at"),
    }
    for option in product.get("options") or []:
        if option.get("name") and option.get("values") and option["name"] != "Title":
            out[f"option_{option['name']}"] = option["values"]
    return {k: v for k, v in out.items() if v not in ("", None, [], {})}


def _is_number(value: Any) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


# ── WordPress ──────────────────────────────────────────────────────────────

def _wp_taxonomy_endpoints(site: str) -> dict[str, tuple[str, str]]:
    """taxonomy slug -> (record field name, terms endpoint)"""
    try:
        data = _json(_get(f"{site}/wp-json/wp/v2/taxonomies"))
    except (requests.RequestException, PermissionError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for slug, info in data.items():
        if isinstance(info, dict) and info.get("rest_base"):
            ns = info.get("rest_namespace") or "wp/v2"
            out[slug] = (info["rest_base"], f"{site}/wp-json/{ns}/{info['rest_base']}")
    return out


def _wp_probe(feed: Feed) -> Feed:
    try:
        resp = _get(feed.endpoint, per_page=1)
    except (requests.RequestException, PermissionError):
        return feed
    records = _json(resp)
    if resp.status_code != 200 or not isinstance(records, list):
        return feed
    feed.total = int(resp.headers.get("X-WP-Total") or len(records))
    if records and isinstance(records[0], dict):
        feed.sample_keys = list(_flatten_wp(records[0], {}).keys())[:SAMPLE_KEYS]
    return feed


def _wp_feeds(site: str) -> list[Feed]:
    try:
        resp = _get(f"{site}/wp-json/wp/v2/types")
    except (requests.RequestException, PermissionError):
        return []
    types = _json(resp)
    if resp.status_code != 200 or not isinstance(types, dict):
        return []
    taxonomies = _wp_taxonomy_endpoints(site)
    feeds = []
    for slug, info in types.items():
        if not isinstance(info, dict):
            continue
        base = info.get("rest_base")
        if not base or slug in _WP_SKIP_TYPES or slug.startswith("wp_"):
            continue
        ns = info.get("rest_namespace") or "wp/v2"
        taxes = dict(taxonomies[t] for t in info.get("taxonomies", []) if t in taxonomies)
        feeds.append(Feed(f"wp:{base}", str(info.get("name") or slug), f"{site}/wp-json/{ns}/{base}", taxonomies=taxes))
    with ThreadPoolExecutor(FEED_CONCURRENCY) as pool:
        feeds = list(pool.map(_wp_probe, feeds))
    return [f for f in feeds if f.total]


def _wp_terms(endpoint: str) -> dict[int, str]:
    terms: dict[int, str] = {}
    for page in range(1, MAX_TERM_PAGES + 1):
        resp = _get(endpoint, per_page=100, page=page, _fields="id,name")
        data = _json(resp)
        if resp.status_code != 200 or not isinstance(data, list):
            break
        terms.update({t["id"]: _text(str(t.get("name", ""))) for t in data if isinstance(t, dict) and "id" in t})
        if page >= int(resp.headers.get("X-WP-TotalPages") or 1):
            break
    return terms


def _read_wp(feed: Feed, limit: int) -> list[dict[str, Any]]:
    def page(n: int) -> list[Any]:
        resp = _get(feed.endpoint, per_page=100, page=n)
        resp.raise_for_status()
        data = _json(resp)
        return data if isinstance(data, list) else []

    first = _get(feed.endpoint, per_page=100, page=1)
    first.raise_for_status()
    records = _json(first)
    if not isinstance(records, list):
        raise RuntimeError(f"{feed.id} did not return a list of records")
    pages = min(int(first.headers.get("X-WP-TotalPages") or 1), math.ceil(limit / 100))
    with ThreadPoolExecutor(FEED_CONCURRENCY) as pool:
        for more in pool.map(page, range(2, pages + 1)):
            records += more
        names = list(feed.taxonomies)
        term_maps = dict(zip(names, pool.map(_wp_terms, feed.taxonomies.values())))
    return [_flatten_wp(r, term_maps) for r in records[:limit] if isinstance(r, dict)]


# ── Shopify ────────────────────────────────────────────────────────────────

def _shopify_feeds(site: str) -> list[Feed]:
    try:
        resp = _get(f"{site}/products.json", limit=1)
    except (requests.RequestException, PermissionError):
        return []
    data = _json(resp)
    if resp.status_code != 200 or not isinstance(data, dict) or not isinstance(data.get("products"), list):
        return []
    sample = data["products"][:1]
    keys = list(_flatten_shopify(sample[0], site).keys())[:SAMPLE_KEYS] if sample else []
    return [Feed("shopify:products", "Products", f"{site}/products.json", sample_keys=keys)]


def _read_shopify(feed: Feed, limit: int) -> list[dict[str, Any]]:
    site = origin(feed.endpoint)
    products: list[dict[str, Any]] = []
    page = 1
    while len(products) < limit:
        resp = _get(feed.endpoint, limit=250, page=page)
        resp.raise_for_status()
        batch = (_json(resp) or {}).get("products") or []
        products += [p for p in batch if isinstance(p, dict)]
        if len(batch) < 250:
            break
        page += 1
    return [_flatten_shopify(p, site) for p in products[:limit]]


# ── Public API ─────────────────────────────────────────────────────────────

def find_feeds(url: str, refresh: bool = False) -> list[Feed]:
    site = origin(url)
    cached = None if refresh else page_cache.feeds.get(site)
    if cached is not None:
        return cached
    with ThreadPoolExecutor(2) as pool:
        wp, shopify = pool.submit(_wp_feeds, site), pool.submit(_shopify_feeds, site)
        feeds = shopify.result() + sorted(wp.result(), key=lambda f: -(f.total or 0))
    log.info("[feeds] %s: %s", site, [f.id for f in feeds] or "no feeds")
    page_cache.feeds.set(site, feeds)
    return feeds


def read_feed(url: str, feed_id: str, refresh: bool = False, limit: int = MAX_FEED_RECORDS) -> list[dict[str, Any]]:
    site = origin(url)
    key = f"{site}|{feed_id}|{limit}"
    cached = None if refresh else page_cache.feed_records.get(key)
    if cached is not None:
        return cached
    feeds = {f.id: f for f in find_feeds(url, refresh)}
    feed = feeds.get(feed_id)
    if feed is None:
        known = ", ".join(feeds) or "none"
        raise ValueError(f"Unknown feed {feed_id!r} for {site}. Available: {known}")
    records = _read_shopify(feed, limit) if feed_id.startswith("shopify:") else _read_wp(feed, limit)
    log.info("[feeds] read %d records from %s %s", len(records), site, feed_id)
    page_cache.feed_records.set(key, records)
    return records


def _sitemap_entries(url: str) -> tuple[list[str], list[str]]:
    """Return (child sitemap URLs, page URLs) listed in one sitemap."""
    resp = _get(url)
    if resp.status_code != 200:
        return [], []
    content = resp.content
    if url.endswith(".gz") or content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return [], []
    children, pages = [], []
    for node in root:
        loc = next((c.text.strip() for c in node if c.tag.endswith("loc") and c.text), None)
        if not loc:
            continue
        (children if node.tag.endswith("sitemap") else pages).append(loc)
    return children, pages


def _all_sitemap_urls(site: str) -> list[str]:
    parser = _robots_for(site)
    queue = list((parser.site_maps() if parser else None) or [])
    queue = queue or [f"{site}/sitemap_index.xml", f"{site}/sitemap.xml"]
    seen: set[str] = set()
    pages: list[str] = []
    with ThreadPoolExecutor(FEED_CONCURRENCY) as pool:
        while queue and len(seen) < MAX_SITEMAPS:
            batch = [u for u in dict.fromkeys(queue) if u not in seen][: MAX_SITEMAPS - len(seen)]
            seen.update(batch)
            queue = []

            def safe(u: str) -> tuple[list[str], list[str]]:
                try:
                    return _sitemap_entries(u)
                except (requests.RequestException, PermissionError, OSError) as exc:
                    log.info("[feeds] sitemap %s failed: %s", u, exc)
                    return [], []

            for children, found in pool.map(safe, batch):
                queue += children
                pages += found
    return list(dict.fromkeys(pages))


def map_site(url: str, include: str = "", limit: int = 200, refresh: bool = False) -> tuple[list[str], int]:
    """Return (up to *limit* page URLs containing *include*, total matching)."""
    site = origin(url)
    urls = None if refresh else page_cache.sitemaps.get(site)
    if urls is None:
        urls = _all_sitemap_urls(site)
        page_cache.sitemaps.set(site, urls)
    needle = include.strip().lower()
    matches = [u for u in urls if needle in u.lower()] if needle else urls
    return matches[:limit], len(matches)
