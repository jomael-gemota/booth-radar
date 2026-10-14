"""
artifacts.py  –  Files Hermes creates during a turn, offered as chat downloads.

Each conversation gets OUTPUT_ROOT/<conversationId>/. After a turn, new or
modified files in that folder, plus absolute paths mentioned in the reply, are
collected — but only files that resolve inside ARTIFACT_ROOTS.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
import re
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

OUTPUT_ROOT = Path(os.getenv("OUTPUT_ROOT") or Path.home() / "booth-radar-outputs").expanduser()
ARTIFACT_ROOTS = [
    Path(p.strip()).expanduser().resolve()
    for p in (os.getenv("ARTIFACT_ROOTS") or str(OUTPUT_ROOT)).split(",")
    if p.strip()
]
ARTIFACT_MAX_BYTES = int(os.getenv("ARTIFACT_MAX_BYTES", str(10 * 1024 * 1024)))
ARTIFACT_MAX_FILES = int(os.getenv("ARTIFACT_MAX_FILES", "5"))
ARTIFACT_EXTENSIONS = {".csv", ".xlsx", ".json", ".txt", ".md"}

_CONVERSATION_ID_RE = re.compile(r"^[a-f0-9]{24}$", re.IGNORECASE)
_PATH_RE = re.compile(
    r"(/[^\s`'\"<>|*?()\[\]]+\.(?:" + "|".join(e[1:] for e in ARTIFACT_EXTENSIONS) + r"))\b",
    re.IGNORECASE,
)

# Absolute local file paths (/a/b/name.ext, ~/a/name.ext). The lookbehind skips URLs.
_LOCAL_FILE_RE = re.compile(
    r"(?<![\w:/.~])~?/(?:[^\s/`'\"<>|()\[\]]+/)+([^\s/`'\"<>|()\[\]]+\.[A-Za-z0-9]{1,5})\b"
)

Snapshot = dict[str, tuple[float, int]]


def hide_paths(text: str) -> str:
    """Replace local file paths with their file name; users download files from the chat."""
    return _LOCAL_FILE_RE.sub(r"\1", text)


class PathScrubber:
    """Apply hide_paths to streamed text, holding back the trailing partial word."""

    def __init__(self, forward: Callable[[str], None]) -> None:
        self._forward = forward
        self._buffer = ""

    def feed(self, chunk: str) -> None:
        self._buffer += chunk
        cut = max(self._buffer.rfind(c) for c in " \t\r\n")
        if cut == -1:
            return
        ready, self._buffer = self._buffer[: cut + 1], self._buffer[cut + 1:]
        self._forward(hide_paths(ready))

    def flush(self) -> None:
        if self._buffer:
            self._forward(hide_paths(self._buffer))
        self._buffer = ""


def prepare_dir(conversation_id: str | None) -> Path | None:
    """Create the conversation's output folder; Hermes may run as another user."""
    if not conversation_id or not _CONVERSATION_ID_RE.match(conversation_id):
        return None
    out = OUTPUT_ROOT / conversation_id
    try:
        out.mkdir(parents=True, exist_ok=True)
        for d in (OUTPUT_ROOT, out):
            os.chmod(d, 0o777)
    except OSError as exc:
        log.warning("[artifacts] cannot prepare %s: %s", out, exc)
        return None
    return out


def snapshot(out: Path | None) -> Snapshot:
    if out is None or not out.is_dir():
        return {}
    snap: Snapshot = {}
    for p in out.rglob("*"):
        try:
            st = p.stat()
        except OSError:
            continue
        if p.is_file():
            snap[str(p)] = (st.st_mtime, st.st_size)
    return snap


def _allowed(path: Path) -> Path | None:
    """Return the resolved path if it is a safe, deliverable file, else None."""
    try:
        real = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not real.is_file() or real.suffix.lower() not in ARTIFACT_EXTENSIONS:
        return None
    root = next((r for r in ARTIFACT_ROOTS if real.is_relative_to(r)), None)
    if root is None:
        return None
    # Hidden files/folders (.ssh, .config, .env, …) are never delivered
    if any(part.startswith(".") for part in real.relative_to(root).parts):
        return None
    return real


def collect(out: Path | None, before: Snapshot, reply: str) -> list[dict[str, Any]]:
    """Return `file` frame payloads for files produced during the turn."""
    candidates: list[Path] = []
    after = snapshot(out)
    candidates += [Path(p) for p, sig in after.items() if before.get(p) != sig]
    candidates += [Path(m) for m in _PATH_RE.findall(reply)]

    files: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for cand in candidates:
        real = _allowed(cand)
        if real is None or real in seen:
            continue
        seen.add(real)
        size = real.stat().st_size
        if size > ARTIFACT_MAX_BYTES:
            log.warning("[artifacts] skipping %s (%d bytes > %d)", real, size, ARTIFACT_MAX_BYTES)
            continue
        if len(files) >= ARTIFACT_MAX_FILES:
            log.warning("[artifacts] file limit reached, skipping %s", real)
            continue
        try:
            data = real.read_bytes()
        except OSError as exc:
            log.warning("[artifacts] cannot read %s: %s", real, exc)
            continue
        mime = mimetypes.guess_type(real.name)[0] or "application/octet-stream"
        files.append({
            "name": real.name,
            "mime": mime,
            "size": len(data),
            "data": base64.b64encode(data).decode("ascii"),
        })
        log.info("[artifacts] delivering %s (%d bytes)", real, len(data))
    return files
