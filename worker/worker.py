"""
worker.py  –  Booth Radar Python worker.

Connects to the Express server over WebSocket and runs chat turns:
  1. Receives { type: "chat", turnId, conversationId, messages, results, job }
  2. Runs the Booth Radar agent (Hermes + scraping tools)
  3. Sends token / status / results / file / done / error frames back to the server

Background jobs (jobs.py) run outside turns: the server sends job_run / job_pause /
job_cancel, and the worker answers with job_created / job_progress / job_finished.

The server persists the conversation and relays frames to the browser via SSE.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from typing import Any, Callable

from dotenv import load_dotenv

load_dotenv()

import websockets
from websockets.exceptions import ConnectionClosed

import artifacts
import jobs
import tool_bridge
from agent import AGENT_TOOL_MODE, TurnCancelled, run_turn
from extractor import EXTRACT_MODEL

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
log = logging.getLogger("worker")

# ── Config ─────────────────────────────────────────────────────────────────
SERVER_WS_URL = os.getenv("SERVER_WS_URL", "ws://localhost:3001/ws")
RECONNECT_DELAY = 5  # seconds between reconnect attempts
# Min seconds between live results-table updates sent during a turn
RESULTS_EMIT_S = float(os.getenv("RESULTS_EMIT_S", "2"))

# Strong references so in-flight turn tasks are not garbage-collected
_running: set[asyncio.Task[None]] = set()

# Cancel events keyed by turnId — set when the server sends a cancel message
_cancel_events: dict[str, threading.Event] = {}


# ── Turn handler ───────────────────────────────────────────────────────────

class _LatestResults:
    """
    Sends the turn's results table at most every RESULTS_EMIT_S: each update is a
    full snapshot, so only the latest one pending needs sending.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, send: Callable[[str, dict[str, Any]], None]) -> None:
        self._loop = loop
        self._send = send
        self._lock = threading.Lock()
        self._pending: dict[str, Any] | None = None
        self._last = 0.0
        self._scheduled = False

    def put(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._pending = payload
            wait = self._last + RESULTS_EMIT_S - time.monotonic()
            if wait <= 0:
                self._flush_locked()
            elif not self._scheduled:
                self._scheduled = True
                self._loop.call_soon_threadsafe(self._loop.call_later, wait, self._flush)

    def drop(self) -> None:
        with self._lock:
            self._pending = None

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush(self) -> None:
        with self._lock:
            self._scheduled = False
            self._flush_locked()

    def _flush_locked(self) -> None:
        if self._pending is not None:
            self._send("results", self._pending)
            self._pending = None
            self._last = time.monotonic()


async def handle_chat(
    outbox: asyncio.Queue[dict[str, Any]],
    turn_id: str,
    conversation_id: str | None,
    messages: list[dict[str, str]],
    results: list[dict[str, str]],
    job: dict[str, Any] | None,
) -> None:
    loop = asyncio.get_running_loop()

    def send(type_: str, payload: dict[str, Any]) -> None:
        # Called from the executor thread; hand the frame to the event loop.
        loop.call_soon_threadsafe(
            outbox.put_nowait, {"turnId": turn_id, "type": type_, "payload": payload}
        )

    scrubber = artifacts.PathScrubber(lambda t: send("token", {"text": t}))
    results_out = _LatestResults(loop, send)

    def emit(type_: str, payload: dict[str, Any]) -> None:
        if type_ == "token":
            scrubber.feed(payload["text"])
            return
        if type_ == "results":
            results_out.put(payload)
            return
        if type_ == "done":
            results_out.drop()  # done carries the final table
        elif type_ == "error":
            results_out.flush()
        scrubber.flush()
        send(type_, payload)

    # Register a cancel event so the WebSocket loop can signal this thread
    cancel_event = threading.Event()
    _cancel_events[turn_id] = cancel_event

    out_dir = artifacts.prepare_dir(conversation_id)
    before = artifacts.snapshot(out_dir)

    try:
        reply, new_results = await loop.run_in_executor(
            None, run_turn, messages, results, emit,
            str(out_dir) if out_dir else None, cancel_event, turn_id, conversation_id, job,
        )
    except TurnCancelled:
        # Server already broadcast 'cancelled' to SSE clients — nothing more to send
        log.info("[worker] turn %s cancelled", turn_id)
        return
    except Exception as exc:
        log.exception("[worker] turn %s failed", turn_id)
        emit("error", {"message": f"Hermes request failed: {exc}"})
        return
    finally:
        _cancel_events.pop(turn_id, None)

    try:
        files = await loop.run_in_executor(None, artifacts.collect, out_dir, before, reply)
    except Exception:
        log.exception("[worker] collecting files for turn %s failed", turn_id)
        files = []
    for f in files:
        emit("file", f)

    emit("done", {"reply": artifacts.hide_paths(reply), "results": new_results})
    log.info(
        "[worker] turn %s completed — %d result rows, %d files",
        turn_id, len(new_results), len(files),
    )


async def _sender(ws, outbox: asyncio.Queue[dict[str, Any]]) -> None:
    while True:
        msg = await outbox.get()
        await ws.send(json.dumps(msg))


def _job_sender(loop: asyncio.AbstractEventLoop, outbox: asyncio.Queue[dict[str, Any]]) -> jobs.Send:
    # Job threads send frames on the current connection; frames sent while
    # disconnected are dropped, and the server resends job_run on reconnect.
    def send(frame: dict[str, Any]) -> None:
        loop.call_soon_threadsafe(outbox.put_nowait, frame)
    return send


def _handle_job_message(msg_type: str, msg: dict[str, Any]) -> None:
    if msg_type == "job_run":
        jobs.run(msg.get("job") or {}, int(msg.get("cursor") or 0), msg.get("results") or [])
    elif msg_type == "job_pause":
        jobs.pause(str(msg.get("jobId") or ""))
    elif msg_type == "job_cancel":
        jobs.cancel(str(msg.get("jobId") or ""))


# ── WebSocket loop ─────────────────────────────────────────────────────────

async def run() -> None:
    while True:
        try:
            log.info("[worker] connecting to %s", SERVER_WS_URL)
            async with websockets.connect(SERVER_WS_URL, ping_interval=20, ping_timeout=30) as ws:
                log.info("[worker] connected")
                outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
                sender = asyncio.create_task(_sender(ws, outbox))
                jobs.set_sender(_job_sender(asyncio.get_running_loop(), outbox))

                try:
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            log.warning("[worker] received non-JSON frame: %r", raw)
                            continue

                        msg_type = msg.get("type")

                        if msg_type == "cancel":
                            turn_id = msg.get("turnId")
                            event = _cancel_events.get(turn_id) if turn_id else None
                            if event:
                                log.info("[worker] cancelling turn %s", turn_id)
                                event.set()
                            else:
                                log.debug("[worker] cancel for unknown/finished turn %s", turn_id)
                            continue

                        if msg_type in ("job_run", "job_pause", "job_cancel"):
                            await asyncio.get_running_loop().run_in_executor(None, _handle_job_message, msg_type, msg)
                            continue

                        if msg_type != "chat":
                            log.warning("[worker] unknown message type: %s", msg_type)
                            continue

                        turn_id  = msg.get("turnId")
                        messages = msg.get("messages")
                        if not turn_id or not isinstance(messages, list) or not messages:
                            log.warning("[worker] malformed chat message: %s", msg)
                            continue
                        results = msg.get("results") or []
                        conversation_id = msg.get("conversationId")
                        log.info("[worker] received chat turn %s (%d messages)", turn_id, len(messages))
                        task = asyncio.create_task(
                            handle_chat(outbox, turn_id, conversation_id, messages, results, msg.get("job"))
                        )
                        _running.add(task)
                        task.add_done_callback(_running.discard)
                finally:
                    jobs.set_sender(lambda frame: None)
                    sender.cancel()

        except ConnectionClosed as exc:
            log.warning("[worker] connection closed (%s) — reconnecting in %ds", exc, RECONNECT_DELAY)
        except OSError as exc:
            log.warning("[worker] connection error (%s) — reconnecting in %ds", exc, RECONNECT_DELAY)
        except Exception:
            log.exception("[worker] unexpected error — reconnecting in %ds", RECONNECT_DELAY)

        await asyncio.sleep(RECONNECT_DELAY)


if __name__ == "__main__":
    log.info("[worker] tool mode: %s", AGENT_TOOL_MODE)
    if AGENT_TOOL_MODE == "mcp":
        tool_bridge.start()
        if not EXTRACT_MODEL:
            log.warning("[worker] EXTRACT_MODEL is unset: page extraction will run full Hermes agent turns")
    asyncio.run(run())
