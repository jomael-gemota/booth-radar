"""
worker.py  –  Booth Radar Python worker.

Connects to the Express server over WebSocket and processes scraping jobs:
  1. Receives { type: "scrape", jobId, url }
  2. Scrapes the URL with Playwright / requests+BS4
  3. Calls Hermes to extract brand+booth listings (streaming tokens)
  4. Sends token / done / error frames back to the server

The server relays those frames to the browser via SSE.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import pymongo
from dotenv import load_dotenv

load_dotenv()

import websockets
from websockets.exceptions import ConnectionClosedError

from scraper import scrape
from extractor import extract

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
log = logging.getLogger("worker")

# ── Config ─────────────────────────────────────────────────────────────────
SERVER_WS_URL = os.getenv("SERVER_WS_URL", "ws://localhost:3001/ws")
MONGODB_URI   = os.getenv("MONGODB_URI",   "mongodb://localhost:27017")
MONGODB_DB    = os.getenv("MONGODB_DB",    "booth-radar")
RECONNECT_DELAY = 5  # seconds between reconnect attempts


# ── MongoDB ────────────────────────────────────────────────────────────────
def get_jobs_collection():
    client = pymongo.MongoClient(MONGODB_URI)
    return client[MONGODB_DB]["jobs"]


# ── Job handler ────────────────────────────────────────────────────────────

async def handle_scrape(ws, job_id: str, url: str) -> None:
    """Scrape *url*, call LLM, stream tokens back, save results."""
    jobs = get_jobs_collection()

    async def send(msg: dict) -> None:
        await ws.send(json.dumps(msg))

    # Mark job as running
    jobs.update_one({"_id": pymongo.collection.ObjectId(job_id)}, {"$set": {"status": "running"}})

    # ── Step 1: Scraping ──────────────────────────────────────────────────
    await send({"jobId": job_id, "type": "token", "payload": {"text": f"🔍 Scraping **{url}**…\n"}})

    try:
        page_text = await asyncio.get_event_loop().run_in_executor(None, scrape, url)
    except Exception as exc:
        log.exception("[worker] scrape failed for job %s", job_id)
        await send({"jobId": job_id, "type": "error", "payload": {"message": f"Scraping failed: {exc}"}})
        jobs.update_one(
            {"_id": pymongo.collection.ObjectId(job_id)},
            {"$set": {"status": "error", "error": str(exc)}},
        )
        return

    char_count = len(page_text)
    await send({"jobId": job_id, "type": "token", "payload": {"text": f"📄 Scraped **{char_count:,}** characters. Analysing with AI…\n\n"}})

    # ── Step 2: LLM extraction ────────────────────────────────────────────
    collected_tokens: list[str] = []

    def on_token(text: str) -> None:
        collected_tokens.append(text)
        # Schedule the async send from the sync callback
        asyncio.get_event_loop().call_soon_threadsafe(
            lambda t=text: asyncio.ensure_future(
                send({"jobId": job_id, "type": "token", "payload": {"text": t}})
            )
        )

    try:
        results = await asyncio.get_event_loop().run_in_executor(
            None, extract, page_text, url, on_token
        )
    except Exception as exc:
        log.exception("[worker] extraction failed for job %s", job_id)
        await send({"jobId": job_id, "type": "error", "payload": {"message": f"AI extraction failed: {exc}"}})
        jobs.update_one(
            {"_id": pymongo.collection.ObjectId(job_id)},
            {"$set": {"status": "error", "error": str(exc)}},
        )
        return

    # ── Step 3: Persist and notify ────────────────────────────────────────
    jobs.update_one(
        {"_id": pymongo.collection.ObjectId(job_id)},
        {"$set": {"status": "done", "results": results}},
    )

    await send({"jobId": job_id, "type": "done", "payload": {"results": results}})
    log.info("[worker] job %s completed — %d entries", job_id, len(results))


# ── WebSocket loop ─────────────────────────────────────────────────────────

async def run() -> None:
    while True:
        try:
            log.info("[worker] connecting to %s", SERVER_WS_URL)
            async with websockets.connect(SERVER_WS_URL, ping_interval=20, ping_timeout=30) as ws:
                log.info("[worker] connected")

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        log.warning("[worker] received non-JSON frame: %r", raw)
                        continue

                    msg_type = msg.get("type")

                    if msg_type == "scrape":
                        job_id = msg.get("jobId")
                        url    = msg.get("url")
                        if not job_id or not url:
                            log.warning("[worker] malformed scrape message: %s", msg)
                            continue
                        log.info("[worker] received scrape job %s  url=%s", job_id, url)
                        asyncio.ensure_future(handle_scrape(ws, job_id, url))
                    else:
                        log.warning("[worker] unknown message type: %s", msg_type)

        except ConnectionClosedError as exc:
            log.warning("[worker] connection closed (%s) — reconnecting in %ds", exc, RECONNECT_DELAY)
        except OSError as exc:
            log.warning("[worker] connection error (%s) — reconnecting in %ds", exc, RECONNECT_DELAY)
        except Exception as exc:
            log.exception("[worker] unexpected error — reconnecting in %ds", RECONNECT_DELAY)

        await asyncio.sleep(RECONNECT_DELAY)


if __name__ == "__main__":
    asyncio.run(run())
