"""
tool_bridge.py  –  Local HTTP endpoint that runs Booth Radar tools inside a live turn.

Hermes starts mcp_server.py as a separate process. Each MCP tool call is forwarded
here (POST /tools/<name>) so it runs in the worker with the turn's results table,
page cache, browser pool and status events.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

log = logging.getLogger(__name__)

BRIDGE_HOST     = "127.0.0.1"
BRIDGE_PORT     = int(os.getenv("TOOL_BRIDGE_PORT", "8765"))
BRIDGE_TOKEN    = os.getenv("TOOL_BRIDGE_TOKEN", "")
MAX_BODY_BYTES  = 1_000_000

Runner = Callable[[str, dict[str, Any]], str]

_turns: dict[str, Runner] = {}
_lock = threading.Lock()
_server: ThreadingHTTPServer | None = None


def register(turn_id: str, runner: Runner) -> None:
    with _lock:
        _turns[turn_id] = runner


def unregister(turn_id: str) -> None:
    with _lock:
        _turns.pop(turn_id, None)


def _resolve(turn_id: str) -> Runner | None:
    with _lock:
        if turn_id in _turns:
            return _turns[turn_id]
        # Hermes may drop the turn argument; that is only unambiguous with one turn running.
        if len(_turns) == 1:
            return next(iter(_turns.values()))
    return None


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if not self.path.startswith("/tools/"):
            self._reply(404, "Not found.")
            return
        sent = self.headers.get("Authorization", "")
        if not hmac.compare_digest(sent, f"Bearer {BRIDGE_TOKEN}"):
            self._reply(401, "Unauthorized.")
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self._reply(413, "Request too large.")
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            args = body.get("args") or {}
            if not isinstance(args, dict):
                raise ValueError("'args' must be an object")
        except (json.JSONDecodeError, ValueError, AttributeError) as exc:
            self._reply(400, f"Error: bad request ({exc}).")
            return

        name = self.path[len("/tools/"):]
        runner = _resolve(str(body.get("turn") or ""))
        if runner is None:
            self._reply(409, "Error: no Booth Radar turn matches this call. Pass the turn id from your instructions.")
            return
        log.info("[bridge] tool=%s args=%s", name, args)
        try:
            text = runner(name, args)
        except Exception as exc:
            log.warning("[bridge] tool %s failed: %s", name, exc)
            text = f"Error: {name} failed: {exc}"
        self._reply(200, text)

    def _reply(self, status: int, text: str) -> None:
        data = json.dumps({"text": text}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        log.debug("[bridge] " + format, *args)


def start() -> None:
    """Serve the bridge on a daemon thread. Safe to call more than once."""
    global _server
    if _server is not None:
        return
    if not BRIDGE_TOKEN:
        raise RuntimeError("TOOL_BRIDGE_TOKEN must be set when AGENT_TOOL_MODE=mcp")
    _server = ThreadingHTTPServer((BRIDGE_HOST, BRIDGE_PORT), _Handler)
    _server.daemon_threads = True
    threading.Thread(target=_server.serve_forever, name="tool-bridge", daemon=True).start()
    log.info("[bridge] listening on http://%s:%d", BRIDGE_HOST, BRIDGE_PORT)
