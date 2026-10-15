"""
page_cache.py  –  In-memory TTL + LRU caches for scraped pages and extractions.

  * pages        – url -> (visible_text, links)
  * extractions  – (url, sha1(page_text)) -> [{"brand": …, "booth": …}, …]

Shared by every conversation the worker handles; cleared on worker restart.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections import OrderedDict
from typing import Generic, Hashable, Optional, TypeVar
from urllib.parse import urldefrag

PAGE_CACHE_TTL = int(os.getenv("PAGE_CACHE_TTL", "3600"))
PAGE_CACHE_MAX = int(os.getenv("PAGE_CACHE_MAX", "300"))

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


class TTLCache(Generic[K, V]):
    """Thread-safe cache that expires entries after *ttl* seconds and keeps at most *max_size*."""

    def __init__(self, ttl: float, max_size: int) -> None:
        self._ttl = ttl
        self._max = max_size
        self._data: OrderedDict[K, tuple[float, V]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: K) -> Optional[V]:
        if self._ttl <= 0 or self._max <= 0:
            return None
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            stored_at, value = entry
            if time.monotonic() - stored_at > self._ttl:
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return value

    def set(self, key: K, value: V) -> None:
        if self._ttl <= 0 or self._max <= 0:
            return
        with self._lock:
            self._data[key] = (time.monotonic(), value)
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)


Links = list[tuple[str, str]]
Rows = list[dict[str, str]]

pages: TTLCache[str, tuple[str, Links]] = TTLCache(PAGE_CACHE_TTL, PAGE_CACHE_MAX)
extractions: TTLCache[tuple[str, str], Rows] = TTLCache(PAGE_CACHE_TTL, PAGE_CACHE_MAX)


def url_key(url: str) -> str:
    return urldefrag(url.strip())[0]


def text_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()
