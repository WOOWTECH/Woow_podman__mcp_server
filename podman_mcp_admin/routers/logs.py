"""Admin + MCP subprocess logs: ring buffer, search and SSE stream.

``mcp_admin_core.process`` logs the child's stdout at INFO on its own module
logger. Nothing configures that logger, so it inherits root's WARNING and the
records are dropped before any handler runs — which is why the LogViewer page
could connect happily and then show nothing at all. ``install_log_capture()``
therefore sets the level as well as attaching the handler, and it attaches to
the *whole* application (root plus uvicorn's non-propagating loggers), not just
the child-process logger: an operator debugging a 500 from /api/config needs to
see the admin half of the service too.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import deque
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from sse_starlette.sse import EventSourceResponse

router = APIRouter(prefix="/api/logs", tags=["logs"])

CORE_PROCESS_LOGGER = "mcp_admin_core.process"
_MCP_PREFIX = "[mcp-server] "
_BUFFER_SIZE = 5000
_REPLAY_ON_CONNECT = 200
_HEARTBEAT_SECONDS = 20

# uvicorn installs handlers on "uvicorn" and "uvicorn.access" with
# propagate=False, so a handler on the root logger alone never sees a single
# access line. Attach to each of these explicitly and de-duplicate per record.
CAPTURED_LOGGERS = (
    "",  # root — catches everything that still propagates
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "mcp_admin_core",
    "podman_mcp_admin",
)
# Loggers whose INFO records are worth the buffer space. Root is deliberately
# left at its configured level so third-party libraries do not flood the page.
_VERBOSE_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access",
                    "mcp_admin_core", "podman_mcp_admin")

_BUFFER: deque[str] = deque(maxlen=_BUFFER_SIZE)
_SUBSCRIBERS: set[asyncio.Queue] = set()

_KNOWN_LEVELS = ("debug", "info", "warning", "error", "critical")
_LEVEL_ALIASES = {"warn": "warning", "fatal": "critical", "err": "error"}

# The child prints its own level; the record level only says how the core
# wrapped it, which is always INFO.
_LEVEL_IN_TEXT = re.compile(r"\b(CRITICAL|FATAL|ERROR|WARNING|WARN|INFO|DEBUG)\b", re.IGNORECASE)
# Python and rich both start a traceback with this line (rich wraps it in a box).
_TRACEBACK_START = re.compile(r"(?:^|[│╭─\s])Traceback \(most recent call last\)")
# The final line of a traceback: "ValueError: x", "pydantic_settings…SettingsError: x".
_EXCEPTION_LINE = re.compile(r"^\s*[│╰\s]*[A-Za-z_][\w.]*(?:Error|Exception|Warning|Exit|Interrupt)\b\s*:")
# Continuation lines of a traceback block carry no level of their own.
_TRACEBACK_BODY = re.compile(r"^(\s|[│╭╰─├└┌┐┘╞╟]|File \"|\.{3})")


class _LevelTracker:
    """Classifies a child line, remembering the traceback it is part of.

    A Python/rich traceback spans many lines and only the *first* one hints at
    severity; the continuation lines used to fall back to the core's INFO
    wrapper, so a genuine crash rendered blue and the red-error filter missed
    the single most important event class on the page.
    """

    def __init__(self) -> None:
        self._in_traceback = False

    def classify(self, text: str, record_level: str) -> str:
        stripped = text.strip()
        if not stripped:
            return "error" if self._in_traceback else record_level

        if _TRACEBACK_START.search(text):
            self._in_traceback = True
            return "error"

        if _EXCEPTION_LINE.match(text):
            # The exception line closes the block but is itself the payload.
            self._in_traceback = False
            return "error"

        if self._in_traceback:
            if _TRACEBACK_BODY.match(text):
                return "error"
            # A line that looks like a fresh log record ends the block.
            self._in_traceback = False

        match = _LEVEL_IN_TEXT.search(text[:80])
        if match:
            found = match.group(1).lower()
            return _LEVEL_ALIASES.get(found, found)
        return record_level


_tracker = _LevelTracker()


def _classify(text: str, record_level: str) -> str:
    """Prefer a level the child printed itself over the core's INFO wrapper."""
    return _tracker.classify(text, record_level)


def _entry(message: str, level: str, source: str) -> str:
    """One SSE payload, shaped for the fields LogViewer.jsx renders."""
    return json.dumps(
        {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "level": level,
            "message": message,
            "source": source,
        }
    )


def clear_buffer() -> None:
    _BUFFER.clear()


def recent(limit: int = _REPLAY_ON_CONNECT) -> list[str]:
    limit = max(1, min(int(limit or _REPLAY_ON_CONNECT), _BUFFER_SIZE))
    return list(_BUFFER)[-limit:]


def publish(message: str, level: str = "info", source: str = "mcp-server") -> None:
    """Add a line and fan it out to every open stream."""
    payload = _entry(message, level, source)
    _BUFFER.append(payload)
    for queue in list(_SUBSCRIBERS):
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            pass


def _source_for(record: logging.LogRecord, text: str) -> str:
    """Which column the LogViewer should file this record under."""
    if record.name.startswith(CORE_PROCESS_LOGGER):
        return "mcp-server" if _MCP_PREFIX in text else "supervisor"
    root = record.name.split(".", 1)[0]
    if root in ("podman_mcp_admin", "mcp_admin_core", "root", ""):
        return "admin"
    return root or "admin"


class _BufferLogHandler(logging.Handler):
    """Mirror the application's logging into the buffer."""

    def emit(self, record: logging.LogRecord) -> None:
        # The same record reaches this handler once per logger it is attached
        # to (a child logger *and* root); publish it exactly once.
        if getattr(record, "_mcp_admin_captured", False):
            return
        record._mcp_admin_captured = True  # noqa: SLF001 — marker on our own record

        try:
            text = record.getMessage()
        except Exception:  # noqa: BLE001 — logging must never raise
            return

        record_level = record.levelname.lower()
        source = _source_for(record, text)
        if source == "mcp-server":
            # Child output: strip the tag so the message column stays readable.
            body = text.split(_MCP_PREFIX, 1)[-1]
            publish(body, _classify(body, record_level), "mcp-server")
        else:
            # Lifecycle lines ("Starting MCP server", "exited with code 1") and
            # the admin service's own logging are exactly what an operator opens
            # this page for.
            publish(text, _LEVEL_ALIASES.get(record_level, record_level), source)


_installed = False
_handler = _BufferLogHandler()


def install_log_capture() -> None:
    global _installed
    if _installed:
        return
    _handler.setLevel(logging.INFO)
    for name in CAPTURED_LOGGERS:
        logger = logging.getLogger(name)
        # Without this the INFO records never reach the handler at all.
        if name in _VERBOSE_LOGGERS and (
            logger.level == logging.NOTSET or logger.level > logging.INFO
        ):
            logger.setLevel(logging.INFO)
        logger.addHandler(_handler)
    _installed = True


install_log_capture()


def _normalise_levels(raw: str) -> set[str]:
    wanted = set()
    for item in raw.split(","):
        name = item.strip().lower()
        if not name:
            continue
        name = _LEVEL_ALIASES.get(name, name)
        if name not in _KNOWN_LEVELS:
            # Fail loudly: a typo used to return the whole unfiltered buffer,
            # so "level=eror" looked like a successful error-only query.
            raise HTTPException(
                status_code=422,
                detail=f"Unknown level '{item.strip()}'. Known levels: "
                       + ", ".join(_KNOWN_LEVELS),
            )
        wanted.add(name)
    return wanted


def _parse_since(raw: str) -> datetime:
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"Invalid 'since' timestamp: {exc}"
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _entry_time(payload: dict[str, Any]) -> datetime | None:
    raw = payload.get("timestamp")
    if not isinstance(raw, str):
        return None
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@router.get("/search")
async def search_logs(
    q: Annotated[str, Query(description="Text to look for in the message")] = "",
    regex: Annotated[bool, Query(description="Treat q as a regular expression")] = False,
    level: Annotated[str, Query(description="Comma-separated levels, e.g. error,warning")] = "",
    source: Annotated[str, Query(description="Comma-separated sources, e.g. mcp-server")] = "",
    since: Annotated[str, Query(description="ISO-8601 timestamp; only newer lines")] = "",
    limit: Annotated[int, Query(ge=1, le=_BUFFER_SIZE)] = 200,
) -> dict[str, Any]:
    """Filter the buffered lines.

    Every parameter is implemented and validated. Previously only ``q`` and
    ``limit`` existed while FastAPI silently accepted (and ignored) the rest, so
    ``?level=error`` returned the complete INFO-heavy buffer and looked like a
    successful query. ``q`` matches the *message* — not the serialised envelope,
    which made anchors useless and leaked matches from the timestamp/level/source
    fields — and is a literal, case-insensitive substring unless ``regex=true``.
    """
    wanted_levels = _normalise_levels(level) if level else set()
    wanted_sources = {s.strip().lower() for s in source.split(",") if s.strip()}
    cutoff = _parse_since(since) if since else None

    matcher = None
    if q and regex:
        try:
            matcher = re.compile(q, re.IGNORECASE)
        except re.error as exc:
            # 422, not a 200 carrying {"error": …} with no "count" key.
            raise HTTPException(
                status_code=422, detail=f"Invalid regular expression: {exc}"
            ) from exc
    needle = q.lower() if q and not regex else ""

    limit = max(1, min(int(limit), _BUFFER_SIZE))  # limit=0 used to mean "all"

    matched: list[str] = []
    for line in list(_BUFFER):
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if wanted_levels and str(payload.get("level", "")).lower() not in wanted_levels:
            continue
        if wanted_sources and str(payload.get("source", "")).lower() not in wanted_sources:
            continue
        if cutoff is not None:
            stamp = _entry_time(payload)
            if stamp is None or stamp < cutoff:
                continue
        if q:
            message = str(payload.get("message", ""))
            if matcher is not None:
                if not matcher.search(message):
                    continue
            elif needle not in message.lower():
                continue
        matched.append(line)

    return {"count": len(matched), "lines": matched[-limit:]}


@router.get("/stream")
async def stream_logs() -> EventSourceResponse:
    """Live tail of the MCP subprocess output.

    Replays the recent buffer first so the page is never blank, then keeps the
    connection warm — an idle SSE stream through a proxy or tunnel gets cut.
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
    _SUBSCRIBERS.add(queue)

    async def publisher():
        try:
            for line in recent():
                yield {"data": line}
            while True:
                try:
                    line = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
                    continue
                yield {"data": line}
        finally:
            _SUBSCRIBERS.discard(queue)

    return EventSourceResponse(publisher())
