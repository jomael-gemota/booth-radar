"""
browser.py  –  One headless Chromium on Playwright's async API, shared by every thread.

  * render(url)          – page HTML, loaded in a shared context per site
  * capture(url, scroll) – JSON responses the page receives while it loads
  * TurnSessions         – one turn's tabs for navigate / click / fill / select / scroll

The browser lives on its own asyncio loop thread; callers block in run(). Images,
media, fonts and common trackers are never downloaded, and every request must pass
net_guard (public hosts only; robots.txt for page navigations).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import threading
import time
from typing import Any, Awaitable, Callable, TypeVar
from urllib.parse import urlsplit

import net_guard

log = logging.getLogger(__name__)

T = TypeVar("T")

SCRAPE_CONCURRENCY     = max(1, int(os.getenv("SCRAPE_CONCURRENCY", "6")))  # tabs rendering at once
SCRAPE_IDLE_TIMEOUT_MS = int(os.getenv("SCRAPE_IDLE_TIMEOUT_MS", "3000"))
NAV_TIMEOUT_MS    = 45_000
ACTION_TIMEOUT_MS = 10_000
ACTION_IDLE_MS    = 3000
CONTEXT_IDLE_S    = 600
CALL_TIMEOUT_S    = 180
MAX_SESSIONS      = 3

# capture(): JSON responses recorded while a page loads
MAX_CAPTURED_RESPONSES = 40
MAX_CAPTURE_BYTES      = 5_000_000
CAPTURE_IDLE_MS        = 8000
CAPTURE_SCROLL_ROUNDS  = 5
SCROLL_WAIT_MS         = 2500

_BLOCKED_RESOURCES = {"image", "media", "font"}
_TRACKER_HOSTS = (
    "google-analytics.com", "googletagmanager.com", "doubleclick.net", "googlesyndication.com",
    "googleadservices.com", "facebook.net", "hotjar.com", "hotjar.io", "clarity.ms",
    "segment.com", "segment.io", "analytics.tiktok.com", "snap.licdn.com", "px.ads.linkedin.com",
    "bat.bing.com", "ads-twitter.com", "criteo.com", "criteo.net", "taboola.com", "outbrain.com",
    "adnxs.com", "amazon-adsystem.com", "nr-data.net", "fullstory.com", "mouseflow.com",
    "quantserve.com", "scorecardresearch.com", "mixpanel.com", "heap.io", "heapanalytics.com",
)

# Tags visible interactive elements with data-br-ref="eN" and describes them.
_SNAPSHOT_JS = r"""
() => {
  const SELECTOR = 'a[href],button,input,select,textarea,summary,[role=button],[role=link],' +
    '[role=tab],[role=checkbox],[role=radio],[role=menuitem],[role=option],[role=switch],[onclick]';
  document.querySelectorAll('[data-br-ref]').forEach(e => e.removeAttribute('data-br-ref'));
  const clean = s => (s || '').replace(/\s+/g, ' ').trim().slice(0, 80);
  const out = [];
  let n = 0;
  for (const el of document.querySelectorAll(SELECTOR)) {
    if (out.length >= 400) break;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (type === 'hidden') continue;
    const box = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    if ((box.width === 0 && box.height === 0) || style.visibility === 'hidden' || style.display === 'none') continue;
    let role = el.getAttribute('role') || {a: 'link', button: 'button', select: 'combobox', textarea: 'textbox', summary: 'button'}[tag] || '';
    if (tag === 'input') {
      role = type === 'checkbox' ? 'checkbox' : type === 'radio' ? 'radio'
        : ['submit', 'button', 'reset', 'image'].includes(type) ? 'button' : 'textbox';
    }
    role = role || 'button';
    const img = el.querySelector && el.querySelector('img[alt]');
    const label = clean(el.getAttribute('aria-label') || (el.labels && el.labels[0] && el.labels[0].innerText)
      || el.getAttribute('placeholder') || el.innerText || (role === 'button' ? el.value : '')
      || el.getAttribute('title') || el.getAttribute('alt') || (img && img.alt) || el.getAttribute('name'));
    if (!label && role === 'link') continue;
    const ref = 'e' + (++n);
    el.setAttribute('data-br-ref', ref);
    const item = {ref, role, label};
    if (tag === 'a') item.href = el.getAttribute('href');
    if (role === 'textbox') item.value = type === 'password' ? '' : clean(el.value);
    if (role === 'checkbox' || role === 'radio') item.checked = !!el.checked;
    if (tag === 'select') {
      const chosen = el.options[el.selectedIndex];
      item.value = chosen ? clean(chosen.text) : '';
      item.options = Array.from(el.options).slice(0, 20).map(o => clean(o.text));
    }
    out.push(item);
  }
  return out;
}
"""


class StaleRef(Exception):
    """The element ref no longer exists on the page."""


# ── Event loop ─────────────────────────────────────────────────────────────

_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()


def _get_loop() -> asyncio.AbstractEventLoop:
    global _loop
    with _loop_lock:
        if _loop is None:
            loop = asyncio.new_event_loop()
            threading.Thread(target=loop.run_forever, name="browser", daemon=True).start()
            _loop = loop
        return _loop


def run(coro: Awaitable[T], timeout: float = CALL_TIMEOUT_S) -> T:
    """Run *coro* on the browser loop and wait for its result."""
    future = asyncio.run_coroutine_threadsafe(coro, _get_loop())  # type: ignore[arg-type]
    try:
        return future.result(timeout)
    except concurrent.futures.TimeoutError:
        future.cancel()
        raise TimeoutError(f"the browser did not finish within {timeout:.0f} s") from None


# ── Browser and contexts (touched only on the loop thread) ─────────────────

class _State:
    playwright: Any = None
    browser: Any = None
    launch_lock: asyncio.Lock | None = None
    tabs: asyncio.Semaphore | None = None
    sites: dict[str, tuple[Any, float]] = {}


_s = _State()


async def _browser() -> Any:
    if _s.browser is not None and _s.browser.is_connected():
        return _s.browser
    _s.launch_lock = _s.launch_lock or asyncio.Lock()
    async with _s.launch_lock:
        if _s.browser is not None and _s.browser.is_connected():
            return _s.browser
        from playwright.async_api import async_playwright  # type: ignore

        if _s.playwright is None:
            _s.playwright = await async_playwright().start()
        log.info("[browser] launching Chromium")
        _s.browser = await _s.playwright.chromium.launch(headless=True)
        _s.sites.clear()
        return _s.browser


def _tabs() -> asyncio.Semaphore:
    _s.tabs = _s.tabs or asyncio.Semaphore(SCRAPE_CONCURRENCY)
    return _s.tabs


def _is_tracker(host: str) -> bool:
    return any(host == t or host.endswith("." + t) for t in _TRACKER_HOSTS)


def _allowed(url: str, top_navigation: bool) -> bool:
    if not url.startswith(("http://", "https://")):
        return True  # data:, blob:, about:
    if not net_guard.is_public(url):
        return False
    return not (top_navigation and net_guard.RESPECT_ROBOTS) or net_guard.robots_allows(url)


async def _route(route: Any) -> None:
    request = route.request
    url = request.url
    if request.resource_type in _BLOCKED_RESOURCES or _is_tracker(urlsplit(url).hostname or ""):
        await route.abort()
        return
    try:
        top = request.is_navigation_request() and request.frame.parent_frame is None
    except Exception:
        top = False
    allowed = await asyncio.get_running_loop().run_in_executor(None, _allowed, url, top)
    if allowed:
        await route.continue_()
    else:
        log.info("[browser] blocked %s", url)
        await route.abort("blockedbyclient")


async def _new_context() -> Any:
    context = await (await _browser()).new_context()
    await context.route("**/*", _route)
    return context


async def _close(target: Any) -> None:
    try:
        await target.close()
    except Exception:
        pass


async def _site_context(site: str) -> Any:
    """Shared context for one site (cookies and consent kept); idle ones are closed."""
    await _browser()
    now = time.monotonic()
    for key, (context, used) in list(_s.sites.items()):
        if key != site and now - used > CONTEXT_IDLE_S:
            del _s.sites[key]
            await _close(context)
    entry = _s.sites.get(site)
    context = entry[0] if entry else await _new_context()
    _s.sites[site] = (context, now)
    return context


async def _retry(make: Callable[[], Awaitable[T]]) -> T:
    """Run make() once more if Chromium crashed during the first attempt."""
    try:
        return await make()
    except Exception:
        if _s.browser is not None and _s.browser.is_connected():
            raise
        log.warning("[browser] Chromium disconnected, relaunching")
        _s.browser = None
        return await make()


async def _settle(page: Any, selector: str | None, timeout_ms: int) -> None:
    """Wait for *selector* when known, else for network idle, at most *timeout_ms*."""
    from playwright.async_api import TimeoutError as PlaywrightTimeout  # type: ignore

    try:
        if selector:
            await page.wait_for_selector(selector, state="attached", timeout=timeout_ms)
        else:
            await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PlaywrightTimeout:
        log.info("[browser] %s not settled after %d ms, using current DOM", page.url, timeout_ms)
    except Exception as exc:
        log.info("[browser] wait failed on %s: %s", page.url, exc)


# ── Renders ────────────────────────────────────────────────────────────────

async def _render(url: str, wait_for: str | None) -> str:
    async with _tabs():
        page = await (await _site_context(net_guard.origin(url))).new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            await _settle(page, wait_for, SCRAPE_IDLE_TIMEOUT_MS)
            return await page.content()
        finally:
            await _close(page)


def render(url: str, wait_for: str | None = None) -> str:
    """HTML of *url* after JavaScript ran; waits for *wait_for* (CSS) when given."""
    return run(_retry(lambda: _render(url, wait_for)))


def _is_json(response: Any) -> bool:
    return response.status == 200 and "json" in (response.headers.get("content-type") or "")


async def _capture(url: str, scroll: bool) -> list[dict[str, Any]]:
    responses: list[Any] = []

    def on_response(response: Any) -> None:
        if response.request.resource_type in ("xhr", "fetch") and len(responses) < MAX_CAPTURED_RESPONSES * 3:
            responses.append(response)

    async with _tabs():
        page = await (await _site_context(net_guard.origin(url))).new_page()
        page.on("response", on_response)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            await _settle(page, None, CAPTURE_IDLE_MS)
            if not any(_is_json(r) for r in responses):
                # Single-page apps can start their data calls after the network first goes quiet.
                await asyncio.sleep(CAPTURE_IDLE_MS / 2000)
                await _settle(page, None, CAPTURE_IDLE_MS)
            if scroll:
                for _ in range(CAPTURE_SCROLL_ROUNDS):
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await _settle(page, None, SCROLL_WAIT_MS)

            captured: list[dict[str, Any]] = []
            for response in responses:
                if len(captured) >= MAX_CAPTURED_RESPONSES:
                    break
                if not _is_json(response):
                    continue
                try:
                    body = await response.body()
                    if len(body) > MAX_CAPTURE_BYTES:
                        continue
                    data = json.loads(body)
                    headers = await response.request.all_headers()
                except Exception:
                    continue
                captured.append({
                    "method": response.request.method,
                    "url": response.request.url,
                    "headers": headers,
                    "body": response.request.post_data,
                    "data": data,
                })
            log.info(
                "[browser] captured %d JSON responses on %s (%d data requests, title %r)",
                len(captured), url, len(responses), await page.title(),
            )
            return captured
        finally:
            await _close(page)


def capture(url: str, scroll: bool = False) -> list[dict[str, Any]]:
    """JSON XHR/fetch responses received while *url* loads: method, url, headers, body, data."""
    return run(_retry(lambda: _capture(url, scroll)))


# ── Sessions ───────────────────────────────────────────────────────────────

class _Session:
    def __init__(self, sid: str, context: Any, page: Any) -> None:
        self.id = sid
        self.context = context
        self.page = page
        self.elements: list[dict[str, Any]] = []


async def _snapshot(session: _Session) -> dict[str, Any]:
    page = session.page
    try:
        elements = await page.evaluate(_SNAPSHOT_JS)
    except Exception:
        # The page was still navigating; read it once it has loaded.
        await _settle(page, None, ACTION_IDLE_MS)
        elements = await page.evaluate(_SNAPSHOT_JS)
    session.elements = elements
    return {
        "session": session.id,
        "url": page.url,
        "title": await page.title(),
        "elements": elements,
        "html": await page.content(),
    }


async def _after_action(session: _Session, pages_before: set[Any]) -> dict[str, Any]:
    """Follow a newly opened tab, wait for the page to settle, then snapshot it."""
    await asyncio.sleep(0.5)
    opened = [p for p in session.context.pages if p not in pages_before]
    if opened:
        session.page = opened[-1]
    try:
        await session.page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
    except Exception:
        pass
    await _settle(session.page, None, ACTION_IDLE_MS)
    return await _snapshot(session)


def _locator(session: _Session, ref: str) -> Any:
    return session.page.locator(f'[data-br-ref="{ref}"]').first


async def _click(session: _Session, locator: Any) -> str:
    """
    Click *locator*. When the click times out (element covered or still animating),
    open a link's address directly, or force the click on other elements.
    Returns the fallback used, or "".
    """
    from playwright.async_api import TimeoutError as PlaywrightTimeout  # type: ignore

    try:
        await locator.click(timeout=ACTION_TIMEOUT_MS)
        return ""
    except PlaywrightTimeout:
        log.info("[browser] click timed out on %s, using a fallback", session.page.url)
    href = await locator.evaluate("e => (e.closest('a[href]') || {}).href || ''")
    if href.startswith(("http://", "https://")):
        net_guard.check_url(href, net_guard.RESPECT_ROBOTS)
        await session.page.goto(href, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        return f"the element could not be clicked, so its link address {href} was opened"
    await locator.click(timeout=ACTION_TIMEOUT_MS, force=True)
    return "the element was covered or still moving, so the click was forced"


async def _require(session: _Session, ref: str) -> Any:
    locator = _locator(session, ref)
    if not await session.page.locator(f'[data-br-ref="{ref}"]').count():
        raise StaleRef(f"element {ref} is not on the page any more; take a new snapshot and use its refs")
    return locator


class TurnSessions:
    """The browser tabs one chat turn has opened. Closed with close() when the turn ends."""

    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.Lock()
        self._count = 0

    def _get(self, sid: str) -> _Session:
        session = self._sessions.get(sid)
        if session is None:
            known = ", ".join(self._sessions) or "none"
            raise KeyError(f"unknown session {sid!r} (open sessions: {known}); call navigate first")
        return session

    def navigate(self, url: str, sid: str = "") -> dict[str, Any]:
        net_guard.check_url(url, net_guard.RESPECT_ROBOTS)
        with self._lock:
            session = self._sessions.get(sid) if sid else None
            if session is None and len(self._sessions) >= MAX_SESSIONS:
                raise RuntimeError(f"at most {MAX_SESSIONS} sessions per turn; pass session=<id> to reuse one")

            async def go() -> dict[str, Any]:
                nonlocal session
                if session is None:
                    self._count += 1
                    context = await _new_context()
                    session = _Session(sid or f"s{self._count}", context, await context.new_page())
                    self._sessions[session.id] = session
                await session.page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                await _settle(session.page, None, ACTION_IDLE_MS)
                return await _snapshot(session)

            return run(go())

    def snapshot(self, sid: str) -> dict[str, Any]:
        with self._lock:
            return run(_snapshot(self._get(sid)))

    def element(self, sid: str, ref: str) -> dict[str, Any] | None:
        """The element *ref* as listed in the session's last snapshot."""
        return next((e for e in self._get(sid).elements if e.get("ref") == ref), None)

    def click(self, sid: str, ref: str) -> dict[str, Any]:
        with self._lock:
            session = self._get(sid)

            async def go() -> dict[str, Any]:
                locator = await _require(session, ref)
                before = set(session.context.pages)
                fallback = await _click(session, locator)
                snap = await _after_action(session, before)
                if fallback:
                    snap["fallback"] = fallback
                return snap

            return run(go())

    def click_matching(self, sid: str, role: str, label: str) -> dict[str, Any] | None:
        """Re-find an element by role and label on the current page and click it; None if gone."""
        with self._lock:
            session = self._get(sid)

            async def go() -> dict[str, Any] | None:
                await _snapshot(session)
                match = next((e for e in session.elements if e["role"] == role and e["label"] == label), None)
                if match is None:
                    return None
                before = set(session.context.pages)
                try:
                    await _click(session, _locator(session, match["ref"]))
                except Exception as exc:
                    log.info("[browser] could not click %s %r: %s", role, label, exc)
                    return None
                return await _after_action(session, before)

            return run(go())

    def fill(self, sid: str, ref: str, text: str, submit: bool = False) -> dict[str, Any]:
        with self._lock:
            session = self._get(sid)

            async def go() -> dict[str, Any]:
                locator = await _require(session, ref)
                before = set(session.context.pages)
                await locator.fill(text, timeout=ACTION_TIMEOUT_MS)
                if submit:
                    await locator.press("Enter", timeout=ACTION_TIMEOUT_MS)
                return await _after_action(session, before)

            return run(go())

    def select(self, sid: str, ref: str, value: str) -> dict[str, Any]:
        with self._lock:
            session = self._get(sid)

            async def go() -> dict[str, Any]:
                locator = await _require(session, ref)
                before = set(session.context.pages)
                try:
                    await locator.select_option(label=value, timeout=ACTION_TIMEOUT_MS)
                except Exception:
                    await locator.select_option(value=value, timeout=ACTION_TIMEOUT_MS)
                return await _after_action(session, before)

            return run(go())

    def scroll_until_end(self, sid: str, max_rounds: int) -> tuple[int, int, int, dict[str, Any]]:
        """Scroll until the page stops growing: (rounds, start height, end height, snapshot)."""
        with self._lock:
            session = self._get(sid)

            async def go() -> tuple[int, int, int, dict[str, Any]]:
                page = session.page
                measure = "document.documentElement.scrollHeight"
                start = height = int(await page.evaluate(measure))
                rounds = still = 0
                while rounds < max_rounds and still < 2:
                    rounds += 1
                    await page.evaluate(f"window.scrollTo(0, {measure})")
                    try:
                        await page.wait_for_function(
                            f"h => {measure} > h", arg=height, timeout=SCROLL_WAIT_MS
                        )
                        await _settle(page, None, ACTION_IDLE_MS)
                    except Exception:
                        pass
                    grown = int(await page.evaluate(measure))
                    still = still + 1 if grown <= height else 0
                    height = max(height, grown)
                return rounds, start, height, await _snapshot(session)

            return run(go(), timeout=CALL_TIMEOUT_S + max_rounds * SCROLL_WAIT_MS / 1000)

    def close(self) -> None:
        with self._lock:
            sessions, self._sessions = list(self._sessions.values()), {}
        if not sessions:
            return

        async def go() -> None:
            for session in sessions:
                await _close(session.context)

        try:
            run(go(), timeout=30)
        except Exception as exc:
            log.info("[browser] closing sessions failed: %s", exc)
