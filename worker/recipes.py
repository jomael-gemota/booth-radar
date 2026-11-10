"""
recipes.py  –  Learned per-site extraction recipes, so only the first page of a
layout needs the extraction model.

  * Page recipes: CSS selectors for an item container and each field, proposed by
    the extraction model from a page it already extracted, and kept only if they
    reproduce that page's rows.
  * Feed recipes: which feed record key holds each field, matched in code against
    the extraction model's rows for the first batch of records.

Recipes are saved to RECIPES_PATH so they survive worker restarts.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from extractor import MAX_PAGE_CHARS, _as_text, complete_json

log = logging.getLogger(__name__)

RECIPES_ENABLED  = os.getenv("RECIPES_ENABLED", "true").strip().lower() not in ("0", "false", "no")
RECIPES_PATH     = Path(os.getenv("RECIPES_PATH") or Path(__file__).with_name("data") / "recipes.json")
MIN_RECIPE_PAGES = 4     # extract calls with fewer pages of one layout don't learn a recipe
MATCH_RATIO      = 0.9   # share of the model's non-empty cells a recipe must reproduce
MAX_EMPTY_RATIO  = 0.5   # a page whose recipe rows are emptier than this goes to the model
MAX_FALLBACKS    = 3     # consecutive fallbacks before a page recipe is dropped
SAMPLE_ROWS      = 8
ATTR_CHARS       = 120

_KEEP_ATTRS = {
    "id", "class", "href", "src", "itemprop", "itemtype", "title", "alt", "name",
    "content", "datetime", "aria-label", "role",
}
_SELECTOR_ATTR = re.compile(r"^(.*?)@([\w:-]+)$")

Row = dict[str, str]

_lock = threading.Lock()
_recipes: dict[str, dict[str, Any]] | None = None


# ── Storage ────────────────────────────────────────────────────────────────

def _all() -> dict[str, dict[str, Any]]:
    global _recipes
    if _recipes is None:
        try:
            _recipes = json.loads(RECIPES_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError:
            _recipes = {}
        except (OSError, ValueError) as exc:
            log.warning("[recipes] could not read %s (%s); starting empty", RECIPES_PATH, exc)
            _recipes = {}
    return _recipes


def _save() -> None:
    try:
        RECIPES_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = RECIPES_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_all(), ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, RECIPES_PATH)
    except OSError as exc:
        log.warning("[recipes] could not save %s: %s", RECIPES_PATH, exc)


def get(key: str) -> dict[str, Any] | None:
    with _lock:
        recipe = _all().get(key)
        return dict(recipe) if recipe else None


def put(key: str, recipe: dict[str, Any]) -> None:
    with _lock:
        _all()[key] = {**recipe, "learned_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "uses": 0}
        _save()
    log.info("[recipes] saved %s", key)


def forget(key: str) -> None:
    with _lock:
        if _all().pop(key, None) is not None:
            _save()
            log.info("[recipes] dropped %s", key)


def record_uses(uses: dict[str, int]) -> None:
    with _lock:
        recipes = _all()
        changed = False
        for key, count in uses.items():
            if key in recipes and count:
                recipes[key]["uses"] = int(recipes[key].get("uses", 0)) + count
                changed = True
        if changed:
            _save()


def _norm(value: Any) -> str:
    return " ".join(_as_text(value).split()).casefold()


def _match_ratio(expected: list[Row], got: list[Row], fields: list[str]) -> float:
    """Share of the non-empty (field, value) cells in *expected* that also appear in *got*."""
    wanted = [(f, _norm(r.get(f))) for r in expected for f in fields if _norm(r.get(f))]
    if not wanted:
        return 0.0
    available = Counter((f, _norm(r.get(f))) for r in got for f in fields)
    matched = 0
    for cell in wanted:
        if available[cell]:
            available[cell] -= 1
            matched += 1
    return matched / len(wanted)


# ── Page recipes ───────────────────────────────────────────────────────────

def page_key(url: str, fields: list[str]) -> str:
    """Pages of one site with the same URL shape (depth + first segment) share a recipe."""
    parts = urlsplit(url)
    host = parts.netloc.lower().removeprefix("www.")
    segments = [s for s in parts.path.split("/") if s]
    shape = f"{len(segments)}:{segments[0] if len(segments) >= 2 else '*'}"
    return f"page|{host}|{shape}|{json.dumps(fields, ensure_ascii=False)}"


_PAGE_PROMPT = """\
You write CSS selectors for a web scraper.

You receive a page's HTML and the rows that were already extracted from it. Write
selectors that reproduce exactly these rows on this page and on other pages with
the same layout.

Output ONLY a JSON object:
  {"item": "<selector matching one element per row, or null if the page is one row>",
   "fields": {"<field>": "<selector inside the item>" or "<selector>@<attribute>" or null}}

Rules:
  - Field selectors are relative to the item element (or the whole page when item is null).
  - Use "@href", "@src", "@content" etc. after a selector to read an attribute instead of
    the text, e.g. "a.product-title@href". Use only "@<attribute>" to read the item's own
    attribute.
  - Prefer stable class names, ids, itemprop and semantic tags over positions.
  - Use null for a field when no element on the page holds its value.
  - Output the raw JSON object only — no markdown fences, no explanation.
"""


def _compact_html(markup: str) -> str:
    soup = BeautifulSoup(markup, "html.parser")
    for tag in soup.find_all(True):
        tag.attrs = {
            k: (" ".join(v) if isinstance(v, list) else str(v))[:ATTR_CHARS]
            for k, v in tag.attrs.items() if k in _KEEP_ATTRS
        }
    return re.sub(r">\s+<", "><", str(soup))[:MAX_PAGE_CHARS]


def _value(element: Any, attr: str | None, base_url: str) -> str:
    if attr is None:
        return " ".join(element.get_text(" ").split())
    value = element.get(attr)
    value = " ".join(value) if isinstance(value, list) else (value or "")
    value = value.strip()
    if value and attr in ("href", "src"):
        value = urljoin(base_url, value)
    return value


def apply_page(recipe: dict[str, Any], markup: str, url: str, fields: list[str]) -> list[Row]:
    """Rows read from *markup* with *recipe*. Raises ValueError on an invalid selector."""
    soup = BeautifulSoup(markup, "html.parser")
    selectors = recipe.get("fields") or {}
    try:
        containers = soup.select(recipe["item"]) if recipe.get("item") else [soup]
        rows: list[Row] = []
        for container in containers:
            row: Row = {}
            for name in fields:
                selector = selectors.get(name)
                if not selector:
                    row[name] = ""
                    continue
                match = _SELECTOR_ATTR.match(selector)
                css, attr = (match.group(1).strip(), match.group(2)) if match else (selector, None)
                element = container.select_one(css) if css else container
                row[name] = _value(element, attr, url) if element is not None else ""
            if any(row.values()):
                rows.append(row)
    except Exception as exc:
        raise ValueError(f"invalid recipe selector: {exc}") from exc
    return rows


def looks_complete(recipe: dict[str, Any], rows: list[Row]) -> bool:
    """False when the recipe found nothing or left most of the cells it should fill empty."""
    filled = [name for name, selector in (recipe.get("fields") or {}).items() if selector]
    if not rows or not filled:
        return False
    empty = sum(1 for r in rows for name in filled if not r.get(name))
    return empty <= MAX_EMPTY_RATIO * len(rows) * len(filled)


def learn_page(markup: str, url: str, fields: list[str], rows: list[Row]) -> dict[str, Any] | None:
    """Ask the extraction model for a page recipe and keep it only if it reproduces *rows*."""
    sample = [r for r in rows if any(r.values())][:SAMPLE_ROWS]
    if not sample:
        return None
    user = (
        f"Page: {url}\nFields: {json.dumps(fields, ensure_ascii=False)}\n"
        f"Rows already extracted from this page ({len(sample)} of {len(rows)} shown):\n"
        f"{json.dumps(sample, ensure_ascii=False)}\n\n"
        f"--- HTML ---\n{_compact_html(markup)}\n--- END HTML ---"
    )
    try:
        proposal = complete_json(_PAGE_PROMPT, user)
        selectors = proposal.get("fields") or {}
        recipe = {
            "kind": "page",
            "item": str(proposal["item"]) if proposal.get("item") else None,
            "fields": {name: (str(selectors[name]) if selectors.get(name) else None) for name in fields},
        }
        got = apply_page(recipe, markup, url, fields)
    except Exception as exc:
        log.info("[recipes] no page recipe for %s: %s", url, exc)
        return None
    ratio = _match_ratio(rows, got, fields)
    log.info("[recipes] page recipe for %s reproduces %.0f%% (%d rows vs %d)", url, ratio * 100, len(got), len(rows))
    if ratio < MATCH_RATIO or len(got) > 1.5 * len(rows) + 1:
        return None
    return recipe


# ── Feed recipes ───────────────────────────────────────────────────────────

def feed_key(site: str, feed_id: str, fields: list[str]) -> str:
    return f"feed|{site}|{feed_id}|{json.dumps(fields, ensure_ascii=False)}"


def learn_feed(records: list[dict[str, Any]], rows: list[Row], fields: list[str]) -> dict[str, Any] | None:
    """
    Map each field to the record key holding the model's value for it, given *rows*
    extracted one per record from *records* in order. None if any field needs derivation.
    """
    if not records or len(rows) != len(records):
        return None
    keys = sorted({k for record in records for k in record})
    mapping: dict[str, str | None] = {}
    for name in fields:
        expected = [_norm(r.get(name)) for r in rows]
        wanted = [i for i, value in enumerate(expected) if value]
        if not wanted:
            mapping[name] = None
            continue
        hits = {k: sum(1 for i in wanted if _norm(records[i].get(k)) == expected[i]) for k in keys}
        best = max(hits, key=lambda k: hits[k], default=None)
        if best is None or hits[best] < MATCH_RATIO * len(wanted):
            log.info("[recipes] feed field %r has no matching record key", name)
            return None
        mapping[name] = best
    if not any(mapping.values()):
        return None
    return {"kind": "feed", "keys": mapping}


def apply_feed(recipe: dict[str, Any], records: list[dict[str, Any]], fields: list[str]) -> list[Row]:
    mapping = recipe.get("keys") or {}
    rows = [{name: _as_text(record.get(mapping[name])) if mapping.get(name) else "" for name in fields} for record in records]
    return [r for r in rows if any(r.values())]
