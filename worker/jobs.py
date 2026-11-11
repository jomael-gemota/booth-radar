"""
jobs.py  –  Background crawl jobs: extract many pages into a conversation's
results table outside chat turns.

The server stores each job (its URLs, cursor and status) and sends `job_run` when
one should run (after a restart or a resume). This module runs jobs in batches
and reports `job_progress` after each batch and `job_finished` at the end. While a
conversation has a job, its chat turns share the job's live ResultsTable.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from results_table import ResultsTable

log = logging.getLogger(__name__)

JOB_MIN_URLS         = int(os.getenv("JOB_MIN_URLS", "100"))
MAX_JOB_URLS         = int(os.getenv("MAX_JOB_URLS", "5000"))
JOB_BATCH_URLS       = max(1, int(os.getenv("JOB_BATCH_URLS", "40")))
JOB_CONCURRENCY      = max(1, int(os.getenv("JOB_CONCURRENCY", "1")))
JOB_PAGE_CONCURRENCY = max(1, int(os.getenv("JOB_PAGE_CONCURRENCY", "4")))
MAX_FAILED_REPORTED  = 200

LOCK_REASON = (
    "a background job is filling this results table. Ask the user to cancel it "
    "first (or call cancel_job if they asked to stop it)."
)

Send = Callable[[dict[str, Any]], None]
Failure = tuple[str, str]


@dataclass
class Job:
    job_id: str
    conversation_id: str
    fields: list[str]
    key_fields: list[str]
    instructions: str
    urls: list[str]
    url_field: str = ""
    # Pages built from a results column ("https://site/item/{ID}"): their rows fill
    # the rows sharing that column's value
    url_template: str = ""
    cursor: int = 0
    rows_added: int = 0
    failed_count: int = 0
    status: str = "queued"
    stop: str = ""  # "pause" or "cancel" once requested
    table: ResultsTable = field(default_factory=ResultsTable, repr=False)

    def spec(self) -> dict[str, Any]:
        return {
            "jobId": self.job_id,
            "conversationId": self.conversation_id,
            "fields": self.fields,
            "keyFields": self.key_fields,
            "instructions": self.instructions,
            "urlField": self.url_field,
            "urlTemplate": self.url_template,
            "urls": self.urls,
        }


# Runs one batch: (urls, job, table) -> failed (url, error) pairs
Extractor = Callable[[list[str], Job, ResultsTable], list[Failure]]

_lock = threading.Condition()
_jobs: dict[str, Job] = {}
_queue: deque[str] = deque()
_threads: list[threading.Thread] = []
_send: Send = lambda frame: None
_extract: Extractor | None = None


def set_sender(send: Send) -> None:
    global _send
    _send = send


def set_extractor(extract: Extractor) -> None:
    global _extract
    _extract = extract


def active(conversation_id: str) -> Job | None:
    """The conversation's job this worker is running or about to run."""
    with _lock:
        return next((j for j in _jobs.values() if j.conversation_id == conversation_id), None)


def table_for(conversation_id: str) -> ResultsTable | None:
    job = active(conversation_id)
    return job.table if job else None


def _register(job: Job) -> None:
    job.table.job_lock = LOCK_REASON
    with _lock:
        _jobs[job.job_id] = job
        _queue.append(job.job_id)
        while len(_threads) < JOB_CONCURRENCY:
            thread = threading.Thread(target=_loop, name=f"job-runner-{len(_threads) + 1}", daemon=True)
            _threads.append(thread)
            thread.start()
        _lock.notify()


def create(
    table: ResultsTable,
    fields: list[str],
    key_fields: list[str],
    instructions: str,
    urls: list[str],
    url_field: str = "",
    url_template: str = "",
) -> Job:
    """Start a new job filling *table* (already prepared for *fields*)."""
    job = Job(
        job_id=uuid.uuid4().hex,
        conversation_id=table.conversation_id,
        fields=fields,
        key_fields=key_fields,
        instructions=instructions,
        urls=urls,
        url_field=url_field,
        url_template=url_template,
        table=table,
    )
    _send({"type": "job_created", "jobId": job.job_id, "payload": job.spec()})
    _register(job)
    log.info("[jobs] created %s: %d pages for conversation %s", job.job_id, len(urls), job.conversation_id)
    return job


def run(spec: dict[str, Any], cursor: int, results: list[dict[str, str]]) -> None:
    """Run a job the server has stored (resumed, or still running when the worker restarted)."""
    job_id = str(spec.get("jobId") or "")
    with _lock:
        known = _jobs.get(job_id)
        if known:
            if known.stop == "pause":
                known.stop = ""
            return
    conversation_id = str(spec.get("conversationId") or "")
    table = table_for(conversation_id) or ResultsTable(results, conversation_id)
    fields = [str(f) for f in spec.get("fields") or []]
    key_fields = [str(f) for f in spec.get("keyFields") or []]
    job = Job(
        job_id=job_id,
        conversation_id=conversation_id,
        fields=fields,
        key_fields=key_fields,
        instructions=str(spec.get("instructions") or ""),
        urls=[str(u) for u in spec.get("urls") or []],
        url_field=str(spec.get("urlField") or ""),
        url_template=str(spec.get("urlTemplate") or ""),
        cursor=max(0, int(cursor or 0)),
        table=table,
    )
    error = table.prepare(fields, key_fields)
    if error:
        _send({"type": "job_finished", "jobId": job_id, "payload": {"status": "failed", "error": error}})
        return
    _register(job)
    log.info("[jobs] resuming %s at %d/%d", job_id, job.cursor, len(job.urls))


def _stop(job_id: str, how: str) -> bool:
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return False
        job.stop = how
        queued = job_id in _queue
        if queued:
            _queue.remove(job_id)
    if queued:
        _release(job)
        if how == "cancel":
            _send({"type": "job_finished", "jobId": job_id, "payload": {"status": "cancelled"}})
    return True


def pause(job_id: str) -> bool:
    """Stop after the batch in flight; the server resumes it with job_run."""
    return _stop(job_id, "pause")


def cancel(job_id: str) -> bool:
    """Stop after the batch in flight; rows collected so far stay."""
    return _stop(job_id, "cancel")


def finish_paused(job_id: str) -> None:
    """Cancel a paused job; it isn't loaded here, so only the server needs telling."""
    _send({"type": "job_finished", "jobId": job_id, "payload": {"status": "cancelled"}})


def _release(job: Job) -> None:
    with _lock:
        _jobs.pop(job.job_id, None)
        shared = any(j.table is job.table for j in _jobs.values())
    if not shared:
        job.table.job_lock = ""


def _loop() -> None:
    while True:
        with _lock:
            while not _queue:
                _lock.wait()
            job = _jobs[_queue.popleft()]
        try:
            _run(job)
        except Exception as exc:
            log.exception("[jobs] %s failed", job.job_id)
            _send({"type": "job_finished", "jobId": job.job_id, "payload": {"status": "failed", "error": str(exc)}})
        finally:
            _release(job)


def _run(job: Job) -> None:
    assert _extract is not None, "jobs.set_extractor() was not called"
    job.status = "running"
    while job.cursor < len(job.urls) and not job.stop:
        batch = job.urls[job.cursor:job.cursor + JOB_BATCH_URLS]
        before = len(job.table)
        failed = _extract(batch, job, job.table)
        job.cursor += len(batch)
        job.rows_added += len(job.table) - before
        job.failed_count += len(failed)
        _send({
            "type": "job_progress",
            "jobId": job.job_id,
            "payload": {
                "cursor": job.cursor,
                "rowsAdded": job.rows_added,
                "failed": [{"url": u, "error": e[:300]} for u, e in failed[:MAX_FAILED_REPORTED]],
                "results": job.table.snapshot(),
            },
        })
        log.info("[jobs] %s: %d/%d pages, %d rows", job.job_id, job.cursor, len(job.urls), len(job.table))
    if job.stop == "pause":
        log.info("[jobs] %s paused at %d/%d", job.job_id, job.cursor, len(job.urls))
        return
    status = "cancelled" if job.stop == "cancel" else "done"
    _send({"type": "job_finished", "jobId": job.job_id, "payload": {"status": status}})
    log.info("[jobs] %s %s", job.job_id, status)
