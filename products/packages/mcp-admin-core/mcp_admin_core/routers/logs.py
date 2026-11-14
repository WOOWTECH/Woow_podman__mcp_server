"""Log streaming router.

Provides real-time log streaming from the MCP server subprocess via
Server-Sent Events (SSE) and an in-memory searchable log buffer.

The process manager drains subprocess stdout into the Python logger;
this module keeps those lines, plus the admin application's own log
records (health checks, dashboard proxy calls, tool operations, httpx
requests, ...), in memory for search and SSE streaming on the ``/logs``
page.

每個帳號（profile）各自一個環形緩衝區（2026-11-13 多帳號實測 P3-e：原本整台共用 5,000 筆，忙的帳號幾秒到一分鐘就把
其他帳號的紀錄擠光）：

- 紀錄歸屬寫入當下的帳號（``profiles.current_profile_id()``；MCP 請求依 URL token、管理 API 依 ``X-MCP-Profile``、
  子行程的輸出與生命週期依啟動它的帳號），只進那個帳號的緩衝區，一個帳號的流量淘汰不到別的帳號的紀錄。
- 每個帳號最多 :data:`PROFILE_LOG_LINES` 筆、訊息合計最多 :data:`PROFILE_LOG_MESSAGE_BYTES`，先到先淘汰最舊的；
  最多同時保留 :data:`MAX_LOG_PROFILES` 個帳號的緩衝區（帳號上限 6 個，多留給剛刪除的帳號），所以總記憶體有上限
  （:data:`MAX_LOG_MEMORY_BYTES`）。
- 每筆 log record 只存一次：子行程的每行輸出原本被 process logger 與父 logger 的 handler 各存一份。
- uvicorn access log 不存：它在請求的 profile context 結束後才記，歸屬不到帳號，本來就沒有任何 Logs 頁看得到；
  只進容器 stdout（遮罩照舊）。

Endpoints:
    GET /api/logs/stream   - SSE endpoint streaming the selected profile's logs
    GET /api/logs/search   - Search the selected profile's log buffer
"""

from __future__ import annotations

import asyncio
import collections
import heapq
import itertools
import json
import logging
import re
import sys
import threading
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Iterator

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .. import profiles
from ..process import CHILD_OUTPUT_ATTR

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/logs", tags=["logs"])

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: 每個帳號最多保留的筆數。與修正前整台共用的 5,000 筆相同：只有預設帳號的服務（正式站現況）保留量不變。
PROFILE_LOG_LINES = 5000
#: 每個帳號的訊息字串合計最多佔多少記憶體（``sys.getsizeof``）。一般一行 60～150 字，5,000 筆不到 1 MiB，通常是筆數先到；
#: 一直寫很長的行（大段 traceback、子行程印的長 JSON）時由這個上限先淘汰最舊的。單一行比上限還大時仍保留那一行。
PROFILE_LOG_MESSAGE_BYTES = 2 * 1024 * 1024
#: 同時保留緩衝區的帳號數：預設＋最多 5 個額外帳號（``profiles.MAX_EXTRA_PROFILES``）＋2 個剛刪除的帳號。
#: 超過時先淘汰已經不存在的帳號；預設帳號的緩衝區不淘汰。
MAX_LOG_PROFILES = 8
#: 一筆紀錄不含訊息字串的記憶體估計上限（dict、時間字串、序號、deque 位置；CPython 3.12 實測約 310 bytes）。
LOG_ENTRY_OVERHEAD_BYTES = 512
#: Logs 頁緩衝區的總記憶體上限（估計，約 35.5 MiB）：每個緩衝區最多「筆數 × 每筆固定開銷＋訊息上限」，最多這麼多個緩衝區。
#: CPython 3.12 實測：兩個上限都寫滿的 8 個緩衝區約 27 MiB；一般長度（約 70 字）6 個帳號都寫滿約 12 MiB，只有預設帳號約 2 MiB。
MAX_LOG_MEMORY_BYTES = MAX_LOG_PROFILES * (PROFILE_LOG_LINES * LOG_ENTRY_OVERHEAD_BYTES + PROFILE_LOG_MESSAGE_BYTES)
STREAM_TAIL_LINES = 100  # initial lines to send on SSE connect
POLL_INTERVAL = 0.5  # seconds between buffer polls for SSE
HEARTBEAT_INTERVAL = 15  # seconds between SSE heartbeat comments
#: 子行程自己的輸出在 Logs 頁的來源欄位，與 ``_drain_logs`` 加的前綴。
CHILD_OUTPUT_SOURCE = "mcp-server"
CHILD_OUTPUT_PREFIX = "[mcp-server] "
#: 不進 Logs 頁的 logger（見模組說明）。``create_logs_router`` 照舊在它上面掛 handler，把等級維持在 INFO，容器 stdout 不變。
ACCESS_LOGGER = "uvicorn.access"


# ---------------------------------------------------------------------------
# In-memory log buffers (one ring buffer per profile)
# ---------------------------------------------------------------------------
#
# We use a *threading* lock (not asyncio.Lock) because the buffers must be
# writable from synchronous logging.Handler.emit() calls which may happen
# on any thread.  The lock protects only a few dict/deque operations, so it
# is held for microseconds and will never block the event loop in practice.
# ---------------------------------------------------------------------------


class _ProfileLogBuffers:
    """帳號 id → 環形緩衝區；每個緩衝區的筆數與訊息位元組、緩衝區的個數都有上限。

    本身不是執行緒安全的：一律在 ``_buffer_lock`` 內使用。每筆紀錄帶全域遞增的 ``seq``（``clear()`` 也不歸零），
    SSE 以它找出上次輪詢之後的新紀錄。迭代（``for entry in _log_buffer``）依寫入順序列出所有帳號的紀錄（快照），
    給測試與診斷用；Logs 頁只讀目前帳號的。
    """

    def __init__(self, lines: int, message_bytes: int, max_profiles: int) -> None:
        self.lines = lines
        self.message_bytes_limit = message_bytes
        self.max_profiles = max_profiles
        self.seq = 0
        self._buffers: dict[str, collections.deque[dict]] = {}
        self._message_bytes: dict[str, int] = {}

    def append(self, profile_id: str, entry: dict) -> None:
        buffer = self._buffers.get(profile_id)
        if buffer is None:
            self._make_room()
            buffer = self._buffers[profile_id] = collections.deque()
            self._message_bytes[profile_id] = 0
        self.seq += 1
        entry["seq"] = self.seq
        buffer.append(entry)
        used = self._message_bytes[profile_id] + sys.getsizeof(entry["message"])
        # 最新的一筆一定留著：單一行比訊息上限還大時，頁面上至少看得到它。
        while len(buffer) > self.lines or (used > self.message_bytes_limit and len(buffer) > 1):
            used -= sys.getsizeof(buffer.popleft()["message"])
        self._message_bytes[profile_id] = used

    def _make_room(self) -> None:
        """緩衝區個數到上限時淘汰一個：已經不存在的帳號先（其中最久沒寫的先）；不知道哪些帳號存在時淘汰最久沒寫的。

        預設帳號不淘汰。帳號上限 6 個、這裡最多 :data:`MAX_LOG_PROFILES` 個，知道哪些帳號存在時一定淘汰得到已刪除的帳號，
        還存在但很久沒寫紀錄的安靜帳號不會因為管理員反覆建立、刪除帳號而被清掉。
        """
        if len(self._buffers) < self.max_profiles:
            return
        try:
            live = profiles.existing_profile_ids()
        except Exception:  # 判斷不了就照最久沒寫的淘汰；不能因此讓寫 log 失敗
            live = None
        while len(self._buffers) >= self.max_profiles:
            candidates = [profile_id for profile_id in self._buffers if profile_id != profiles.DEFAULT_PROFILE]
            if not candidates:
                return
            gone = [profile_id for profile_id in candidates if live is not None and profile_id not in live]
            victim = min(gone or candidates, key=self._last_seq)
            del self._buffers[victim], self._message_bytes[victim]

    def _last_seq(self, profile_id: str) -> int:
        buffer = self._buffers[profile_id]
        return buffer[-1]["seq"] if buffer else 0

    def entries(self, profile_id: str) -> list[dict]:
        """*profile_id* 緩衝區的快照（舊到新）。"""
        return list(self._buffers.get(profile_id, ()))

    def tail(self, profile_id: str, count: int) -> list[dict]:
        """*profile_id* 最新的 *count* 筆（舊到新）。"""
        newest = list(itertools.islice(reversed(self._buffers.get(profile_id, ())), count))
        newest.reverse()
        return newest

    def since(self, profile_id: str, seq: int) -> list[dict]:
        """*profile_id* 序號大於 *seq* 的紀錄（舊到新）。只看緩衝區尾端：成本與新紀錄數成正比，與其他帳號的流量無關。"""
        buffer = self._buffers.get(profile_id)
        if not buffer or buffer[-1]["seq"] <= seq:
            return []
        newer = list(itertools.takewhile(lambda entry: entry["seq"] > seq, reversed(buffer)))
        newer.reverse()
        return newer

    def message_bytes(self, profile_id: str) -> int:
        return self._message_bytes.get(profile_id, 0)

    def profile_ids(self) -> list[str]:
        return list(self._buffers)

    def __len__(self) -> int:
        return sum(len(buffer) for buffer in self._buffers.values())

    def __iter__(self) -> Iterator[dict]:
        return iter(list(heapq.merge(*self._buffers.values(), key=lambda entry: entry["seq"])))

    def clear(self) -> None:
        self._buffers.clear()
        self._message_bytes.clear()


_log_buffer = _ProfileLogBuffers(PROFILE_LOG_LINES, PROFILE_LOG_MESSAGE_BYTES, MAX_LOG_PROFILES)
_buffer_lock = threading.Lock()


_LEVEL_RE = re.compile(r"\b(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)\b", re.IGNORECASE)


def _extract_level(line: str) -> str:
    """Try to extract a log level from a log line, default to 'info'."""
    m = _LEVEL_RE.search(line)
    if m:
        level = m.group(1).upper()
        if level == "CRITICAL":
            return "error"
        if level == "WARN":
            return "warning"
        return level.lower()
    return "info"


def _level_from_record(record: logging.LogRecord) -> str:
    """Map a :class:`logging.LogRecord` level to our ring-buffer level string."""
    if record.levelno >= logging.ERROR:
        return "error"
    if record.levelno >= logging.WARNING:
        return "warning"
    if record.levelno >= logging.INFO:
        return "info"
    return "debug"


def _append_log_line_sync(
    line: str,
    source: str = CHILD_OUTPUT_SOURCE,
    level: str | None = None,
    profile_id: str | None = None,
) -> None:
    """Synchronously append a log line to a profile's buffer (the current profile by default).

    This is safe to call from any thread (including from within a
    :class:`logging.Handler`).  The *level* parameter allows the caller
    to supply a pre-computed level; when ``None`` the level is extracted
    from the message text via :func:`_extract_level`.
    """
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": level if level is not None else _extract_level(line),
        "source": source,
        "message": line.rstrip(),
    }
    owner = profiles.current_profile_id() if profile_id is None else profile_id
    with _buffer_lock:
        _log_buffer.append(owner, entry)


async def append_log_line(line: str, source: str = CHILD_OUTPUT_SOURCE) -> None:
    """Append a log line to the current profile's buffer (async wrapper).

    Delegates to the synchronous :func:`_append_log_line_sync` which is
    safe to call from any context.
    """
    _append_log_line_sync(line, source=source)


# ---------------------------------------------------------------------------
# Logging handlers that feed the buffers
# ---------------------------------------------------------------------------
#
# 同一筆 record 會依序經過 mcp_admin_core.process 上的 _BufferLogHandler 與父 logger mcp_admin_core 上的
# _AdminLogHandler（app 自己的 logger 也可能是另一個有裝 handler 的 logger 的子 logger）。第一個存進緩衝區的 handler
# 在 record 上做記號，之後的 handler 看到記號就不再存——每筆 record 只存一次。記號只認模組內的物件，``extra=`` 帶同名
# 屬性偽造不了（偽造也只會讓那一行不進 Logs 頁）。
# ---------------------------------------------------------------------------

_STORED = object()
_STORED_ATTR = "_mcp_logs_page_stored"


def _already_stored(record: logging.LogRecord) -> bool:
    return record.__dict__.get(_STORED_ATTR) is _STORED


def _mark_stored(record: logging.LogRecord) -> None:
    record.__dict__[_STORED_ATTR] = _STORED


class _BufferLogHandler(logging.Handler):
    """``mcp_admin_core.process`` 的紀錄進目前帳號的緩衝區。

    子行程自己的輸出（``_drain_logs`` 帶 :data:`~mcp_admin_core.process.CHILD_OUTPUT_ATTR` 標記的 record）存成來源
    ``mcp-server``、去掉「[mcp-server] 」前綴、等級從內容判斷；其他紀錄（生命週期、身分拒絕）存成來源 logger 名、等級照
    record。與容器 stdout 的排除相同，只看結構標記、不看訊息文字（遮罩可能把整行換成「[redacted: …]」，前綴就不見了）。
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if _already_stored(record):
                return
            message = self.format(record)
            if getattr(record, CHILD_OUTPUT_ATTR, False) is True:
                if message.startswith(CHILD_OUTPUT_PREFIX):
                    message = message[len(CHILD_OUTPUT_PREFIX):]
                _append_log_line_sync(message, source=CHILD_OUTPUT_SOURCE)
            else:
                _append_log_line_sync(message, source=record.name, level=_level_from_record(record))
            _mark_stored(record)
        except Exception:
            # logging.Handler contract: never raise from emit()
            self.handleError(record)


def install_log_capture() -> None:
    """Install the buffer handler on the process logger so subprocess
    output captured by :class:`McpProcessManager` is also buffered.

    The process logger's level is explicitly set to DEBUG so that
    INFO-level ``[mcp-server]`` messages are not silently dropped
    when the root logger's effective level is WARNING (the default).
    """
    proc_logger = logging.getLogger("mcp_admin_core.process")
    if any(isinstance(h, _BufferLogHandler) for h in proc_logger.handlers):
        return
    # Ensure the logger accepts INFO messages even if root is WARNING
    if proc_logger.level == logging.NOTSET or proc_logger.level > logging.DEBUG:
        proc_logger.setLevel(logging.DEBUG)
    handler = _BufferLogHandler()
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(message)s"))
    proc_logger.addHandler(handler)


# ---------------------------------------------------------------------------
# Admin / application log handler
# ---------------------------------------------------------------------------
#
# Captures log records from the interesting application loggers so that API
# calls, health checks, proxy activity and httpx requests are visible on the
# /logs page of the profile they belong to.
# ---------------------------------------------------------------------------

class _AdminLogHandler(logging.Handler):
    """Captures application log records into the current profile's buffer.

    Unlike :class:`_BufferLogHandler` (which is specialised for the MCP
    process logger), this handler captures *all* records from the
    loggers supplied to :func:`_install_admin_log_capture` and stores
    them with the originating logger name as the ``source`` field.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            # Uvicorn emits access records after request middleware resets its profile context: they cannot be
            # attributed to a profile and no Logs page shows them, so they stay on container stdout only.
            if record.name == ACCESS_LOGGER or _already_stored(record):
                return
            msg = self.format(record)
            _append_log_line_sync(msg, source=record.name, level=_level_from_record(record))
            _mark_stored(record)
        except Exception:
            # logging.Handler contract: never raise from emit()
            self.handleError(record)


def _install_admin_log_capture(logger_names: tuple[str, ...]) -> None:
    """在指定的 logger 名單上安裝 _AdminLogHandler（idempotent）。"""
    fmt = logging.Formatter("%(message)s")
    for logger_name in logger_names:
        target = logging.getLogger(logger_name)
        if any(isinstance(h, _AdminLogHandler) for h in target.handlers):
            continue
        if target.level == logging.NOTSET or target.level > logging.INFO:
            target.setLevel(logging.INFO)
        handler = _AdminLogHandler()
        handler.setLevel(logging.INFO)
        handler.setFormatter(fmt)
        target.addHandler(handler)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class LogEntry(BaseModel):
    """A single log entry.

    Accepts both ``source`` and the legacy ``pod`` field name for
    backward compatibility with entries already in the ring buffer.
    Internal bookkeeping keys (``seq``) are ignored, so API output keeps
    exactly these four fields.
    """

    timestamp: str
    level: str = "info"
    source: str = ""
    message: str

    def __init__(self, **data: Any) -> None:
        # Migrate legacy "pod" field to "source"
        if "pod" in data and "source" not in data:
            data["source"] = data.pop("pod")
        elif "pod" in data:
            data.pop("pod")
        super().__init__(**data)


class LogSearchResponse(BaseModel):
    """Response from log search."""

    query: str
    total_buffered: int
    matched: int
    entries: list[LogEntry]


# ---------------------------------------------------------------------------
# SSE streaming
# ---------------------------------------------------------------------------


def _sse_data(entry: dict) -> str:
    return f"data: {json.dumps(LogEntry(**entry).model_dump())}\n\n"


async def _sse_log_generator(
    tail_lines: int = STREAM_TAIL_LINES,
    profile_id: str = profiles.DEFAULT_PROFILE,
) -> AsyncGenerator[str, None]:
    """Generate SSE events from *profile_id*'s log buffer.

    First sends a ``connected`` event so the client knows the stream is
    alive, then replays the most recent *tail_lines* of that profile,
    and finally polls for the profile's new entries yielding them as they
    appear (other profiles' traffic neither shows up nor hides them).

    Periodic SSE comment heartbeats (``:``) are sent to prevent proxies
    and load-balancers from killing idle connections.
    """
    # 0. Send a named "connected" event so the frontend can confirm the stream is alive.
    #    This is NOT a default "message" event -- the client must listen for event: connected.
    yield f"event: connected\ndata: {json.dumps({'status': 'connected'})}\n\n"

    # 1. Send tail of existing buffer
    with _buffer_lock:
        snapshot = _log_buffer.tail(profile_id, tail_lines)
        last_seq = _log_buffer.seq

    for entry in snapshot:
        yield _sse_data(entry)

    # 2. Poll for new lines, with periodic heartbeat comments
    polls_since_heartbeat = 0
    heartbeat_every_n_polls = max(1, int(HEARTBEAT_INTERVAL / POLL_INTERVAL))

    try:
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            polls_since_heartbeat += 1

            with _buffer_lock:
                new_entries = _log_buffer.since(profile_id, last_seq)
                last_seq = _log_buffer.seq

            for entry in new_entries:
                yield _sse_data(entry)
                polls_since_heartbeat = 0  # reset after real data

            # Send heartbeat comment to keep connection alive
            if polls_since_heartbeat >= heartbeat_every_n_polls:
                yield ": heartbeat\n\n"
                polls_since_heartbeat = 0

    except asyncio.CancelledError:
        logger.info("SSE log stream cancelled by client")
        return
    except Exception as exc:
        error_msg = json.dumps({"error": str(exc)})
        yield f"data: {error_msg}\n\n"
        logger.error("Log stream error: %s", exc)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/stream")
async def stream_logs(
    tail: int = Query(default=STREAM_TAIL_LINES, ge=1, le=1000, description="Number of initial tail lines"),
) -> StreamingResponse:
    """Stream the selected profile's logs via Server-Sent Events.

    Connect with an EventSource client:
        const es = new EventSource('/api/logs/stream?tail=100');
        es.onmessage = (e) => console.log(JSON.parse(e.data));
    """
    return StreamingResponse(
        _sse_log_generator(tail_lines=tail, profile_id=profiles.current_profile_id()),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/search", response_model=LogSearchResponse)
async def search_logs(
    q: str = Query(..., min_length=1, max_length=200, description="Search query (plain text or regex)"),
    limit: int = Query(default=100, ge=1, le=1000, description="Max results to return"),
    regex: bool = Query(default=False, description="Treat query as regex pattern"),
) -> LogSearchResponse:
    """Search the selected profile's log buffer.

    By default performs case-insensitive substring matching.
    Set ``regex=true`` to use the query as a regular expression pattern.
    """
    profile_id = profiles.current_profile_id()
    with _buffer_lock:
        buffer_snapshot = _log_buffer.entries(profile_id)

    total_buffered = len(buffer_snapshot)

    # Build matcher
    if regex:
        try:
            pattern = re.compile(q, re.IGNORECASE)
        except re.error as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid regex pattern: {exc}",
            ) from exc
        matcher = lambda msg: pattern.search(msg) is not None
    else:
        q_lower = q.lower()
        matcher = lambda msg: q_lower in msg.lower()

    # Filter and limit results
    matched_entries: list[LogEntry] = []
    for entry in reversed(buffer_snapshot):  # most recent first
        if matcher(entry["message"]):
            matched_entries.append(LogEntry(**entry))
            if len(matched_entries) >= limit:
                break

    return LogSearchResponse(
        query=q,
        total_buffered=total_buffered,
        matched=len(matched_entries),
        entries=matched_entries,
    )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_logs_router(app_logger_name: str) -> APIRouter:
    """回傳 /api/logs router，並在此 app 的 logger 命名空間上安裝 log 捕捉。

    app_logger_name 通常為 "{app}_mcp_admin"（如 "odoo_mcp_admin"），
    決定哪些 admin API 活動（health/config/tools 子 logger）被捕進 /logs。
    uvicorn.access 照舊掛 handler（把等級維持在 INFO，容器 stdout 的 access log 與修正前相同），但 access 紀錄不進 Logs 頁。
    """
    install_log_capture()
    _install_admin_log_capture(
        (app_logger_name, "mcp_admin_core", ACCESS_LOGGER, "httpx")
    )
    return router
