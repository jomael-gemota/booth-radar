"""
net_guard.py  –  Safety and politeness rules for every outbound request.

  * is_public(url)       – False for loopback, private, link-local and reserved hosts
  * check_url(url)       – raises PermissionError for non-public hosts or robots.txt disallows
  * request(method, url) – requests call that re-checks every redirect hop
  * domain_slot(url)     – caps concurrent page loads per host (DOMAIN_CONCURRENCY)
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator
from urllib import robotparser
from urllib.parse import urljoin, urlsplit

import requests

log = logging.getLogger(__name__)

ROBOTS_AGENT       = "BoothRadar"
ROBOTS_UA          = "Mozilla/5.0 (compatible; BoothRadar/1.0)"
ROBOTS_TIMEOUT     = 20
RESPECT_ROBOTS     = os.getenv("RESPECT_ROBOTS", "true").strip().lower() not in ("0", "false", "no")
DOMAIN_CONCURRENCY = max(1, int(os.getenv("DOMAIN_CONCURRENCY", "4")))
MAX_REDIRECTS      = 5
HOST_CACHE_S       = 600

_hosts: dict[str, tuple[float, bool]] = {}
_hosts_lock = threading.Lock()
_robots: dict[str, robotparser.RobotFileParser | None] = {}
_robots_lock = threading.Lock()
_slots: dict[str, threading.BoundedSemaphore] = {}
_slots_lock = threading.Lock()


def origin(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"Invalid URL: {url!r}")
    return f"{parts.scheme}://{parts.netloc}"


# ── Public hosts only ──────────────────────────────────────────────────────

def _resolves_public(host: str) -> bool:
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror:
            return True  # unresolvable: the request itself will fail
        addresses = [ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos]
    return bool(addresses) and all(a.is_global and not a.is_multicast for a in addresses)


def host_is_public(host: str) -> bool:
    host = host.strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return False
    now = time.monotonic()
    with _hosts_lock:
        cached = _hosts.get(host)
        if cached and now - cached[0] < HOST_CACHE_S:
            return cached[1]
    public = _resolves_public(host)
    with _hosts_lock:
        _hosts[host] = (now, public)
    return public


def is_public(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme in ("http", "https") and bool(parts.hostname) and host_is_public(parts.hostname or "")


# ── robots.txt ─────────────────────────────────────────────────────────────

def robots_for(site: str) -> robotparser.RobotFileParser | None:
    """Parsed robots.txt for an origin (cached), or None when the site has none."""
    with _robots_lock:
        if site in _robots:
            return _robots[site]
    parser: robotparser.RobotFileParser | None = None
    try:
        resp = request("GET", f"{site}/robots.txt", robots=False, headers={"User-Agent": ROBOTS_UA}, timeout=ROBOTS_TIMEOUT)
        if resp.status_code == 200:
            parser = robotparser.RobotFileParser()
            parser.parse(resp.text.splitlines())
    except (requests.RequestException, PermissionError) as exc:
        log.info("[guard] robots.txt unavailable for %s: %s", site, exc)
    with _robots_lock:
        _robots[site] = parser
    return parser


def robots_allows(url: str) -> bool:
    parser = robots_for(origin(url))
    return parser is None or parser.can_fetch(ROBOTS_AGENT, url)


def check_url(url: str, robots: bool = True) -> None:
    """Raise PermissionError unless *url* is on a public host (and allowed by robots.txt)."""
    if not is_public(url):
        raise PermissionError(f"{urlsplit(url).netloc or url} is not a public website")
    if robots and not robots_allows(url):
        raise PermissionError(f"robots.txt of {origin(url)} does not allow {url}")


# ── Requests ───────────────────────────────────────────────────────────────

def request(method: str, url: str, robots: bool = True, **kwargs: Any) -> requests.Response:
    """requests.request that follows redirects itself, checking every hop with check_url."""
    data = kwargs.pop("data", None)
    for _ in range(MAX_REDIRECTS + 1):
        check_url(url, robots)
        resp = requests.request(method, url, data=data, allow_redirects=False, **kwargs)
        location = resp.headers.get("Location")
        if not resp.is_redirect or not location:
            return resp
        url = urljoin(url, location)
        if resp.status_code == 303 or (resp.status_code in (301, 302) and method != "HEAD"):
            method, data = "GET", None
        kwargs.pop("params", None)
    raise requests.TooManyRedirects(f"more than {MAX_REDIRECTS} redirects")


@contextmanager
def domain_slot(url: str) -> Iterator[None]:
    """Hold one of DOMAIN_CONCURRENCY page-load slots for *url*'s host."""
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    with _slots_lock:
        slot = _slots.setdefault(host, threading.BoundedSemaphore(DOMAIN_CONCURRENCY))
    with slot:
        yield
