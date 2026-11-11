"""
site_feeds.py  –  Read a site's public data feeds and sitemaps instead of rendering pages.

  * find_feeds(url)        – WordPress REST content types and Shopify products.json
  * capture_api(url)       – JSON endpoints the page calls while loading (api:N feeds)
  * read_feed(url, feed)   – every record of one feed, flattened to plain fields
                             (every letter of a letter-filtered feed)
  * read_lookup(url, feed, ids) – a captured detail endpoint replayed for many IDs
  * map_site(url, include) – page URLs listed in the site's sitemaps

All requests honour robots.txt for the BoothRadar user agent and only reach public hosts.
"""

from __future__ import annotations

import copy
import gzip
import html
import json
import logging
import math
import os
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import requests

import net_guard
import page_cache
import scraper

log = logging.getLogger(__name__)

USER_AGENT       = net_guard.ROBOTS_UA
FEED_TIMEOUT     = 20
FEED_CONCURRENCY = 4
MAX_FEED_RECORDS = int(os.getenv("MAX_FEED_RECORDS", "10000"))
LOOKUP_BATCH     = max(1, int(os.getenv("LOOKUP_BATCH", "50")))
MAX_LOOKUP_IDS   = int(os.getenv("MAX_LOOKUP_IDS", "5000"))
LETTER_SHARE     = 0.8
ID_SEARCH_DEPTH  = 6
MAX_SITEMAPS     = 50
MAX_TERM_PAGES   = 10
SAMPLE_KEYS      = 25
MAX_API_PAGES    = 100
MAX_API_FEEDS    = 8
MIN_API_RECORDS  = 2
RECORDS_DEPTH    = 4
FLATTEN_DEPTH    = 3

_PAGE_KEYS   = {"page", "p", "pg", "pagenumber", "page_number", "pageindex", "pageno", "currentpage"}
_OFFSET_KEYS = {"offset", "start", "skip", "from", "startindex", "start_index"}
_TOTAL_KEYS  = {"total", "totalcount", "total_count", "totalresults", "total_results", "nbhits", "count", "totalitems"}
# Request headers not replayed (set by requests itself, or browser-only)
_SKIP_HEADERS = {"host", "content-length", "connection", "accept-encoding", "transfer-encoding"}

_WP_SKIP_TYPES = {"attachment", "nav_menu_item"}
_WP_DROP_KEYS = {
    "guid", "_links", "_embedded", "yoast_head", "yoast_head_json", "class_list",
    "template", "meta", "featured_media", "author", "comment_status", "ping_status",
    "format", "sticky", "status", "type", "date_gmt", "modified_gmt", "menu_order",
    "parent",
}
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_LETTERS = [chr(c) for c in range(ord("A"), ord("Z") + 1)]
# Values sites use for the "#" (non-letter) tab, tried in order
_OTHER_LETTERS = ("#", "0-9")
_ID_VALUE_RE = re.compile(r"^(\d{2,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", re.IGNORECASE)


@dataclass
class Feed:
    id: str
    label: str
    endpoint: str
    total: int | None = None
    sample_keys: list[str] = field(default_factory=list)
    # record field name -> endpoint listing that taxonomy's terms (WordPress)
    taxonomies: dict[str, str] = field(default_factory=dict)
    # Captured JSON endpoint (capture_api): the request to replay, never shown to the agent
    request: dict[str, Any] | None = None
    records_path: str = ""
    paging: "_Paging | None" = None
    # The one-letter parameter of an A–Z directory endpoint
    letter: "_Param | None" = None
    # The ID parameter of a detail endpoint, and how it takes several IDs
    # ("repeat", "comma" or "single"; "" until the first read_lookup)
    lookup: "_Param | None" = None
    lookup_mode: str = ""

    def describe(self) -> str:
        if self.lookup is not None:
            count = "one item per ID"
        elif self.letter is not None:
            count = "all letters: count known after reading"
        else:
            count = f"{self.total:,} records" if self.total is not None else "record count unknown"
        keys = f"; fields: {', '.join(self.sample_keys)}" if self.sample_keys else ""
        return f"- {self.id}: {self.label} ({count}){keys}"


@dataclass
class _Paging:
    """Where a captured request keeps its page number or offset."""
    location: str          # "query" or "body"
    path: tuple[str, ...]  # key path inside the query string or JSON body
    kind: str              # "page" or "offset"
    value: int
    as_string: bool


@dataclass
class _Param:
    """A letter filter or lookup ID inside a captured request."""
    location: str          # "query" or "body"
    path: tuple[str, ...]
    value: Any


# ── HTTP ───────────────────────────────────────────────────────────────────

origin = net_guard.origin


def _get(url: str, **params: Any) -> requests.Response:
    return net_guard.request(
        "GET",
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


# ── Captured JSON APIs ─────────────────────────────────────────────────────

def _records_at(data: Any, path: tuple[str, ...] = (), depth: int = 0) -> tuple[tuple[str, ...], list[dict[str, Any]]]:
    """The largest list of objects in *data* (up to RECORDS_DEPTH levels deep) and its key path."""
    if isinstance(data, list):
        objects = [d for d in data if isinstance(d, dict)]
        return (path, objects) if objects and len(objects) * 2 >= len(data) else ((), [])
    best: tuple[tuple[str, ...], list[dict[str, Any]]] = ((), [])
    if isinstance(data, dict) and depth < RECORDS_DEPTH:
        for key, value in data.items():
            found = _records_at(value, (*path, str(key)), depth + 1)
            if len(found[1]) > len(best[1]):
                best = found
    return best


def _records_from(data: Any, path: tuple[str, ...]) -> list[dict[str, Any]]:
    for key in path:
        data = data.get(key) if isinstance(data, dict) else None
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def _total_in(data: Any, path: tuple[str, ...] = ()) -> int | None:
    """A total-count value at the top level, one level down, or beside the records list."""
    if not isinstance(data, dict):
        return None
    nodes = [data, *(v for v in data.values() if isinstance(v, dict))]
    node = data
    for key in path[:-1]:
        node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, dict):
            nodes.append(node)
    for node in nodes:
        for key, value in node.items():
            if key.lower() in _TOTAL_KEYS and isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return value
    return None


def _joined(values: list[Any]) -> str:
    """Distinct non-empty values as one "; "-separated string."""
    parts: list[str] = []
    for value in values:
        if isinstance(value, list):
            text = ", ".join(json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v) for v in value)
        else:
            text = json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else str(value)
        for part in text.split("; "):
            if part and part not in parts:
                parts.append(part)
    return "; ".join(parts)


def _flatten_json(record: dict[str, Any], prefix: str = "", depth: int = 0) -> dict[str, Any]:
    """
    Nested objects become dotted keys ("price.amount"), lists of objects one dotted
    key per field with the distinct values joined ("showrooms.name" = "B1; C2");
    HTML values become text.
    """
    out: dict[str, Any] = {}
    for key, value in record.items():
        if key == "__typename":
            continue
        name = f"{prefix}{key}"
        if isinstance(value, dict) and depth < FLATTEN_DEPTH:
            out.update(_flatten_json(value, f"{name}.", depth + 1))
        elif isinstance(value, list) and depth < FLATTEN_DEPTH and any(isinstance(v, dict) for v in value):
            merged: dict[str, list[Any]] = {}
            for item in value:
                if isinstance(item, dict):
                    for k, v in _flatten_json(item, f"{name}.", depth + 1).items():
                        merged.setdefault(k, []).append(v)
            out.update({k: _joined(vs) for k, vs in merged.items()})
        else:
            out[name] = _plain(value)
    return {k: v for k, v in out.items() if v not in ("", None, [], {})}


def _json_body(request: dict[str, Any]) -> Any:
    try:
        return json.loads(request.get("body") or "")
    except ValueError:
        return None


def _paging_kind(key: str) -> str | None:
    name = key.lower()
    if name in _PAGE_KEYS:
        return "page"
    if name in _OFFSET_KEYS:
        return "offset"
    return None


def _find_paging(request: dict[str, Any]) -> _Paging | None:
    for key, value in parse_qsl(urlsplit(request["url"]).query, keep_blank_values=True):
        kind = _paging_kind(key)
        if kind and value.isdigit():
            return _Paging("query", (key,), kind, int(value), True)
    body = _json_body(request)
    if not isinstance(body, dict):
        return None
    nodes: list[tuple[tuple[str, ...], dict[str, Any]]] = [((), body)]
    nodes += [((k,), v) for k, v in body.items() if isinstance(v, dict)]
    for prefix, node in nodes:
        for key, value in node.items():
            kind = _paging_kind(key)
            if not kind or isinstance(value, bool):
                continue
            if isinstance(value, int):
                return _Paging("body", (*prefix, key), kind, value, False)
            if isinstance(value, str) and value.isdigit():
                return _Paging("body", (*prefix, key), kind, int(value), True)
    return None


def _set_param(request: dict[str, Any], location: str, path: tuple[str, ...], value: Any) -> dict[str, Any]:
    """*request* with the parameter at *path* set to *value*; a list repeats a query parameter."""
    out = dict(request)
    if location == "query":
        parts = urlsplit(request["url"])
        values = value if isinstance(value, list) else [value]
        query: list[tuple[str, str]] = []
        for k, v in parse_qsl(parts.query, keep_blank_values=True):
            if k != path[0]:
                query.append((k, v))
            elif values:
                query += [(k, str(x)) for x in values]
                values = []
        out["url"] = urlunsplit(parts._replace(query=urlencode(query)))
        return out
    body = copy.deepcopy(_json_body(request))
    node = body
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    out["body"] = json.dumps(body)
    return out


def _with_paging(request: dict[str, Any], paging: _Paging, value: int) -> dict[str, Any]:
    return _set_param(request, paging.location, paging.path, str(value) if paging.as_string else value)


def _params(request: dict[str, Any]) -> list[_Param]:
    """Every query parameter, and the JSON body's values at the top level and one level down."""
    found = [_Param("query", (k,), v) for k, v in parse_qsl(urlsplit(request["url"]).query, keep_blank_values=True)]
    body = _json_body(request)
    if isinstance(body, dict):
        nodes: list[tuple[tuple[str, ...], dict[str, Any]]] = [((), body)]
        nodes += [((k,), v) for k, v in body.items() if isinstance(v, dict)]
        for prefix, node in nodes:
            found += [_Param("body", (*prefix, k), v) for k, v in node.items() if not isinstance(v, dict)]
    return found


def _letter(value: Any) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if text == "#" or (len(text) == 1 and text.isascii() and text.isalpha()):
        return text.upper()
    return ""


def _starts_with(record: dict[str, Any], letter: str) -> bool:
    """Whether a name-like value of *record* starts with *letter* ("#": with a non-letter)."""
    for value in record.values():
        text = value.strip() if isinstance(value, str) else ""
        if not re.search(r"[A-Za-z]", text):
            continue
        if (not text[0].isalpha()) if letter == "#" else text[0].upper() == letter:
            return True
    return False


def _find_letter(request: dict[str, Any], records: list[dict[str, Any]]) -> _Param | None:
    """The parameter holding one letter that most records start with (an A–Z directory)."""
    for param in _params(request):
        letter = _letter(param.value)
        if letter and sum(_starts_with(r, letter) for r in records) >= LETTER_SHARE * len(records):
            return param
    return None


def _is_id_name(key: str) -> bool:
    return key.lower().endswith(("id", "ids"))


def _find_lookup(request: dict[str, Any], page_url: str, records: list[dict[str, Any]]) -> _Param | None:
    """
    The parameter holding the ID of the item a detail page shows: its value is part of
    the page URL (/exhibitor/97991 → exhibitorIds=97991), or, for a single-record
    response, its name ends in "id"/"ids" and its value looks like an ID.
    """
    parts = urlsplit(page_url)
    on_page = {unquote(s) for s in parts.path.split("/") if s} | {v for _, v in parse_qsl(parts.query)}
    on_page = {v for v in on_page if len(v) >= 2}
    candidates = [
        (p, [str(v) for v in (p.value if isinstance(p.value, list) else [p.value])])
        for p in _params(request)
        if p.value not in ("", None, []) and not isinstance(p.value, bool)
    ]
    for param, values in candidates:
        id_like = all(_ID_VALUE_RE.match(v) for v in values)
        if any(v in on_page for v in values) and (id_like or _is_id_name(param.path[-1])):
            return param
    if len(records) == 1:
        for param, values in candidates:
            if _is_id_name(param.path[-1]) and all(_ID_VALUE_RE.match(v) for v in values):
                return param
    return None


def _signature(request: dict[str, Any], skip: list[Any], path: tuple[str, ...]) -> str:
    """Identifies an endpoint across captures; the paging, letter and lookup values in *skip* are left out."""
    parts = urlsplit(request["url"])
    names = {p.path[0] for p in skip if p is not None and p.location == "query"}
    query = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in names)
    return f"{request['method']} {parts.netloc}{parts.path}?{urlencode(query)} {'.'.join(path)}"


def _feed_signature(feed: Feed) -> str:
    path = tuple(feed.records_path.split(".")) if feed.records_path else ()
    return _signature(feed.request or {}, [feed.paging, feed.letter, feed.lookup], path)


def _replay(request: dict[str, Any]) -> Any:
    url = request["url"]
    headers = {
        k: v for k, v in (request.get("headers") or {}).items()
        if not k.startswith(":") and k.lower() not in _SKIP_HEADERS
    }
    body = request.get("body")
    resp = net_guard.request(
        request["method"], url, headers=headers,
        data=body.encode("utf-8") if body else None, timeout=FEED_TIMEOUT,
    )
    resp.raise_for_status()
    data = _json(resp)
    if data is None:
        raise RuntimeError(f"{urlsplit(url).netloc}{urlsplit(url).path} did not return JSON")
    return data


def _api_label(
    request: dict[str, Any], path: tuple[str, ...], paging: _Paging | None, per_page: int,
    letter: _Param | None, lookup: _Param | None,
) -> str:
    parts = urlsplit(request["url"])
    endpoint = f"{request['method']} {parts.netloc}{parts.path}"
    if lookup is not None:
        return (
            f"{endpoint} — detail lookup: one item per {'.'.join(lookup.path)!r} value. Read it with "
            "read_feed and id_column=<the results column holding these IDs> to fetch every row's details"
        )
    where = f"records at {'.'.join(path)}" if path else "top-level records"
    if paging is None:
        pages = "single page (no page or offset parameter found)"
    else:
        pages = f"{per_page} per page, pages by {paging.kind} parameter {'.'.join(paging.path)!r}"
    if letter is not None:
        pages += (
            f"; filtered to letter {letter.value!r} by parameter {'.'.join(letter.path)!r} "
            f"({per_page} records for that letter). read_feed reads every letter A–Z and #"
        )
    return f"{endpoint} — {where}, {pages}"


def capture_api(url: str, scroll: bool = False) -> list[Feed]:
    """
    Load *url* in the browser, record the JSON endpoints it calls that return lists of
    records (or one item's details), and register them as api:N feeds of the site
    (readable with read_feed, or read_lookup for detail endpoints).
    """
    site = origin(url)
    known = list(page_cache.api_feeds.get(site) or [])
    by_signature = {_feed_signature(f): f for f in known}
    next_id = 1 + max((int(f.id.split(":")[1]) for f in known), default=0)

    found: dict[str, tuple[int, Feed]] = {}
    for capture in scraper.capture_json(url, scroll):
        path, records = _records_at(capture["data"])
        if not records:
            continue
        request = {k: capture[k] for k in ("method", "url", "headers", "body")}
        letter = _find_letter(request, records) if len(records) >= MIN_API_RECORDS else None
        lookup = None if letter else _find_lookup(request, url, records)
        if lookup is None and len(records) < MIN_API_RECORDS:
            continue
        paging = _find_paging(request)
        signature = _signature(request, [paging, letter, lookup], path)
        if signature in found:
            continue
        feed = by_signature.get(signature)
        if feed is None:
            feed = Feed(f"api:{next_id}", "", request["url"])
            next_id += 1
        feed.request, feed.paging, feed.records_path = request, paging, ".".join(path)
        feed.letter, feed.lookup = letter, lookup
        if lookup is None:
            feed.lookup_mode = ""
        feed.label = _api_label(request, path, paging, len(records), letter, lookup)
        if letter or lookup:
            feed.total = None
        else:
            feed.total = _total_in(capture["data"], path) or (len(records) if paging is None else None)
        keys = list(_flatten_json(records[0]).keys())
        feed.sample_keys = keys[:SAMPLE_KEYS]
        # Lists rank by size; detail lookups by how much each item carries.
        found[signature] = (len(keys) if lookup else len(records), feed)

    ranked = [f for _, f in sorted(found.values(), key=lambda item: -item[0])]
    feeds = [f for f in ranked if f.lookup is None][:MAX_API_FEEDS]
    feeds += [f for f in ranked if f.lookup is not None][:MAX_API_FEEDS // 2]
    ids = {f.id for f in feeds}
    page_cache.api_feeds.set(site, [f for f in known if f.id not in ids] + feeds)
    log.info("[feeds] %s: captured %s", url, [f.id for f in feeds] or "no JSON record endpoints")
    return feeds


def _records_path(feed: Feed) -> tuple[str, ...]:
    return tuple(feed.records_path.split(".")) if feed.records_path else ()


def _read_pages(feed: Feed, request: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """Every page of records *request* returns (by the feed's paging), unflattened."""
    paging, path = feed.paging, _records_path(feed)
    if paging is None:
        return _records_from(_replay(request), path)[:limit]

    # Start from the first page even if the browser captured a later one.
    value = 0 if paging.kind == "offset" or paging.value == 0 else 1
    records: list[dict[str, Any]] = []
    previous_first = ""
    for _ in range(MAX_API_PAGES):
        data = _replay(_with_paging(request, paging, value))
        page = _records_from(data, path)
        first = json.dumps(page[0], sort_keys=True, default=str) if page else ""
        if not page or first == previous_first:
            break
        previous_first = first
        records += page
        total = _total_in(data, path)
        if len(records) >= limit or (total and len(records) >= total):
            break
        value += 1 if paging.kind == "page" else len(page)
    return records[:limit]


def _letter_values(letters: str) -> list[str]:
    """"" means every letter and "#"; otherwise e.g. "A-C", "A,B,#" or "#"."""
    if not letters.strip():
        return [*_LETTERS, "#"]
    out: list[str] = []
    for part in re.split(r"[,\s]+", letters.upper().strip()):
        if re.fullmatch(r"[A-Z]-[A-Z]", part):
            out += [chr(c) for c in range(ord(part[0]), ord(part[2]) + 1)]
        elif part == "#" or re.fullmatch(r"[A-Z]", part):
            out.append(part)
    return list(dict.fromkeys(out))


def _read_letters(feed: Feed, limit: int, letters: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Every letter's records of a letter-filtered feed, without duplicates; plus notes on failed letters."""
    param = feed.letter
    assert param is not None and feed.request is not None
    lower = str(param.value).islower()

    def read(letter: str) -> tuple[list[dict[str, Any]], str]:
        error = "no records"
        for value in _OTHER_LETTERS if letter == "#" else (letter.lower() if lower else letter,):
            try:
                records = _read_pages(feed, _set_param(feed.request or {}, param.location, param.path, value), limit)
            except Exception as exc:
                error = str(exc)
                continue
            if records:
                return records, ""
        return [], error

    wanted = _letter_values(letters)
    with ThreadPoolExecutor(FEED_CONCURRENCY) as pool:
        results = list(pool.map(read, wanted))
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    notes: list[str] = []
    for letter, (got, error) in zip(wanted, results):
        if not got:
            notes.append(f"letter {letter}: {error}")
        for record in got:
            key = json.dumps(record, sort_keys=True, default=str)
            if key not in seen:
                seen.add(key)
                records.append(record)
    if len(records) > limit:
        notes.append(f"stopped at the {limit:,}-record limit")
    return records[:limit], notes


def _read_api(feed: Feed, limit: int, letters: str) -> tuple[list[dict[str, Any]], list[str]]:
    if feed.letter is not None:
        records, notes = _read_letters(feed, limit, letters)
    else:
        records = _read_pages(feed, feed.request or {}, limit)
        notes = [f"{feed.id} has no letter filter, so it was read as captured"] if letters.strip() else []
    return [_flatten_json(r) for r in records], notes


def _scalars(data: Any, depth: int = 0) -> set[str]:
    if isinstance(data, dict) and depth < ID_SEARCH_DEPTH:
        return {s for v in data.values() for s in _scalars(v, depth + 1)}
    if isinstance(data, list) and depth < ID_SEARCH_DEPTH:
        return {s for v in data for s in _scalars(v, depth + 1)}
    if isinstance(data, (str, int)) and not isinstance(data, bool):
        return {str(data).strip()}
    return set()


def _match_ids(ids: list[str], records: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """Pair each record with the requested ID it contains (request order when none does)."""
    if len(ids) == 1:
        return [(ids[0], r) for r in records]
    pairs: list[tuple[str, dict[str, Any]]] = []
    for i, record in enumerate(records):
        values = _scalars(record)
        if len(records) == len(ids) and ids[i] in values:
            pairs.append((ids[i], record))
        elif hits := [x for x in ids if x in values]:
            pairs.append((hits[0], record))
    if not pairs and len(records) == len(ids):
        return list(zip(ids, records))
    return pairs


def _typed(like: Any, value: str) -> Any:
    sample = like[0] if isinstance(like, list) and like else like
    return int(value) if isinstance(sample, int) and not isinstance(sample, bool) and value.isdigit() else value


def _lookup_request(feed: Feed, ids: list[str], mode: str) -> dict[str, Any]:
    param = feed.lookup
    assert param is not None and feed.request is not None
    value: Any
    if mode == "comma":
        value = ",".join(ids)
    elif param.location == "query" or isinstance(param.value, list) or len(ids) > 1:
        value = [_typed(param.value, i) for i in ids]
    else:
        value = _typed(param.value, ids[0])
    return _set_param(feed.request, param.location, param.path, value)


def _lookup_once(feed: Feed, ids: list[str], mode: str) -> list[tuple[str, dict[str, Any]]]:
    records = _records_from(_replay(_lookup_request(feed, ids, mode)), _records_path(feed))
    return _match_ids(ids, records)


def _probe_lookup_mode(feed: Feed, ids: list[str]) -> str:
    """How the endpoint takes several IDs: a repeated parameter, a comma list, or one per call."""
    sample = ids[:3]
    if len(sample) >= 2:
        for mode in ("repeat", "comma"):
            try:
                matched = _lookup_once(feed, sample, mode)
            except Exception as exc:
                log.info("[feeds] %s lookup with %s IDs failed: %s", feed.id, mode, exc)
                continue
            if len({i for i, _ in matched}) >= 2:
                return mode
    return "single"


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


def _feed(url: str, feed_id: str, refresh: bool = False) -> Feed:
    site = origin(url)
    api = feed_id.startswith("api:")
    candidates = (page_cache.api_feeds.get(site) or []) if api else find_feeds(url, refresh)
    feeds = {f.id: f for f in candidates}
    feed = feeds.get(feed_id)
    if feed is None:
        if api:
            raise ValueError(f"Unknown feed {feed_id!r} for {site}. Captured endpoints expire after an hour; call capture_api again.")
        known = ", ".join(feeds) or "none"
        raise ValueError(f"Unknown feed {feed_id!r} for {site}. Available: {known}")
    return feed


def read_feed(
    url: str, feed_id: str, refresh: bool = False, limit: int = MAX_FEED_RECORDS, letters: str = "",
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    Every record of one feed, and notes on parts that could not be read. A
    letter-filtered API feed is read for every letter, or only those in *letters*.
    """
    site = origin(url)
    key = f"{site}|{feed_id}|{limit}|{letters.strip().upper()}"
    cached = None if refresh else page_cache.feed_records.get(key)
    if cached is not None:
        return cached, []
    feed = _feed(url, feed_id, refresh)
    notes: list[str] = []
    if feed_id.startswith("api:"):
        records, notes = _read_api(feed, limit, letters)
    elif feed_id.startswith("shopify:"):
        records = _read_shopify(feed, limit)
    else:
        records = _read_wp(feed, limit)
    log.info("[feeds] read %d records from %s %s", len(records), site, feed_id)
    if not notes:
        page_cache.feed_records.set(key, records)
    return records, notes


def feed_kind(url: str, feed_id: str) -> str:
    """"lookup" (detail endpoint), "letters" (A–Z directory endpoint) or "" (a plain list, or unknown)."""
    if not feed_id.startswith("api:"):
        return ""
    try:
        feed = _feed(url, feed_id)
    except ValueError:
        return ""
    return "lookup" if feed.lookup is not None else "letters" if feed.letter is not None else ""


def read_lookup(
    url: str, feed_id: str, ids: list[str], progress: Callable[[int, int], None] | None = None,
) -> tuple[list[tuple[str, dict[str, Any]]], list[str]]:
    """
    Replay a captured detail endpoint for each ID (batched when it takes several):
    (ID, flattened record) pairs in ID order, and notes on IDs that failed or had no record.
    """
    feed = _feed(url, feed_id)
    if feed.lookup is None:
        raise ValueError(f"{feed_id} is not a detail lookup; read it with read_feed without id_column.")
    ids = list(dict.fromkeys(str(i).strip() for i in ids if str(i).strip()))
    notes = [f"only the first {MAX_LOOKUP_IDS:,} of {len(ids):,} IDs were looked up"] if len(ids) > MAX_LOOKUP_IDS else []
    ids = ids[:MAX_LOOKUP_IDS]
    if not ids:
        return [], notes
    if not feed.lookup_mode:
        feed.lookup_mode = _probe_lookup_mode(feed, ids)
        log.info("[feeds] %s takes IDs as %s", feed.id, feed.lookup_mode)
    size = 1 if feed.lookup_mode == "single" else LOOKUP_BATCH
    batches = [ids[i:i + size] for i in range(0, len(ids), size)]

    pairs: list[tuple[str, dict[str, Any]]] = []
    failed: list[str] = []
    done = 0
    with ThreadPoolExecutor(FEED_CONCURRENCY) as pool:
        futures = {pool.submit(_lookup_once, feed, batch, feed.lookup_mode): batch for batch in batches}
        for future in as_completed(futures):
            batch = futures[future]
            try:
                pairs += future.result()
            except Exception as exc:
                log.info("[feeds] %s lookup of %d IDs failed: %s", feed.id, len(batch), exc)
                failed += batch
                notes.append(f"IDs {batch[0]}…{batch[-1]}: {exc}"[:300])
            done += len(batch)
            if progress:
                progress(done, len(ids))
    order = {i: n for n, i in enumerate(ids)}
    pairs.sort(key=lambda pair: order[pair[0]])
    found = {i for i, _ in pairs}
    missing = len(ids) - len(found) - len(failed)
    if missing > 0:
        notes.append(f"{missing:,} IDs returned no record")
    log.info("[feeds] looked up %d/%d IDs with %s %s", len(found), len(ids), origin(url), feed_id)
    return [(i, _flatten_json(r)) for i, r in pairs], notes


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
    parser = net_guard.robots_for(site)
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
