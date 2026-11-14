"""Logs 頁每個帳號（profile）各自的緩衝區、子程式輸出只存一次、記憶體有上限（2026-11-13 多帳號實測 P3-e）。

e0a80d6 的證據（隔離沙盒、正式站同一映像與 chart 0.1.10）：
- mp-qa Q3-S4（``receipts/q3-s4-log-buffer.json``）：A 跑 60 秒負載後，B 的 Logs 頁從 2,135 筆變 0 筆。
- mp-odoo-res R2b（``receipts/r2b-logs-page-eviction-P5.json``）：只讓其他 5 個帳號跑 30 秒，安靜帳號 P5 的 Logs 頁變 0 筆；
  頁面上每行子程式輸出有兩份（來源 ``mcp-server`` 與 ``mcp_admin_core.process``）。
- mp-odoo-iso S12b：D 跑約 2,400 次呼叫之後，A、B、C、F 的 Logs 頁 ``total_buffered`` 都是 0。
- mp-n8n（info）：``_BufferLogHandler`` 存一次去掉前綴的行，父 logger 上的 ``_AdminLogHandler`` 又存一次帶前綴的行。

全部用合成值，不起子行程。
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import sys
import tracemalloc
from types import SimpleNamespace

import pytest

from mcp_admin_core import log_redaction, profiles
from mcp_admin_core.process import CHILD_OUTPUT_ATTR, McpProcessManager
from mcp_admin_core.routers import logs

PROC = "mcp_admin_core.process"
_CAPTURED = (PROC, "mcp_admin_core", "httpx", "uvicorn.access")


@pytest.fixture
def capture():
    """正式 app 匯入時 ``create_logs_router()`` 裝好的 handler（process 與 admin 兩種），緩衝區清空；結束後還原 logger。"""
    saved = {name: (list(logging.getLogger(name).handlers), logging.getLogger(name).level) for name in _CAPTURED}
    log_redaction.install()
    logs.install_log_capture()
    logs._install_admin_log_capture(("mcp_admin_core", "uvicorn.access", "httpx"))
    # 與正式 app 相同的等級（別的測試匯入的模組可能把 httpx 等 logger 調高，例如 litellm launcher 壓到 WARNING）
    for name in _CAPTURED:
        logging.getLogger(name).setLevel(logging.DEBUG if name == PROC else logging.INFO)
    with logs._buffer_lock:
        logs._log_buffer.clear()
    yield
    for name, (handlers, level) in saved.items():
        logging.getLogger(name).handlers[:] = handlers
        logging.getLogger(name).setLevel(level)
    with logs._buffer_lock:
        logs._log_buffer.clear()


@pytest.fixture
def service(make_app):
    """預設帳號加兩個額外帳號（quiet、busy）的服務，Logs router 與正式 app 相同。回傳 (client, 管理 JWT 標頭, [quiet, busy])。"""
    client = make_app({"mcp_server": {"command": "", "args": [], "port": 59999, "env": {}}})
    client.app.include_router(logs.router)
    login = client.post("/api/auth/login", json={"password": "admin"})
    auth = {"Authorization": "Bearer " + login.json()["token"]}
    ids = []
    for label in ("quiet", "busy"):
        created = client.post("/api/profiles", headers=auth, json={"label": label})
        assert created.status_code == 201, created.text
        ids.append(created.json()["id"])
    return client, auth, ids


@contextlib.contextmanager
def as_profile(profile_id: str):
    """與 ``ProfileRegistry.activate`` 相同的 contextvar，給不需要真的帳號設定檔的測試用。"""
    token = profiles._current_profile.set(profile_id)
    try:
        yield
    finally:
        profiles._current_profile.reset(token)


def page(client, auth, profile_id, q=".", limit=1000):
    """管理頁 Logs 的搜尋（``/api/logs/search``，以 X-MCP-Profile 選帳號）。"""
    response = client.get("/api/logs/search", params={"q": q, "regex": "true", "limit": limit},
                          headers={**auth, "X-MCP-Profile": profile_id})
    assert response.status_code == 200, response.text
    return response.json()


def all_buffered() -> list[dict]:
    with logs._buffer_lock:
        return list(logs._log_buffer)


def proxy_traffic(requests: int, child_port: int) -> None:
    """一個轉發請求在 Logs 會碰到的紀錄：httpx 一行（帶 profile）、子程式輸出一行、uvicorn access 一行（請求結束後才記）。"""
    for i in range(requests):
        logging.getLogger("httpx").info('HTTP Request: POST http://127.0.0.1:%d/mcp "HTTP/1.1 200 OK"', child_port)
        logging.getLogger(PROC).info("[mcp-server] %s", f'INFO:     127.0.0.1:{40000 + i % 20000} - "POST /mcp HTTP/1.1" 200 OK',
                                     extra={CHILD_OUTPUT_ATTR: True})
        logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d', "10.0.0.1:5555", "POST", "/private_***/mcp", "1.1", 200)


class _Stdout:
    def __init__(self, lines):
        self._lines = [line.encode() + b"\n" for line in lines]

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for line in self._lines:
            yield line


class _Proc:
    def __init__(self, lines):
        self.stdout = _Stdout(lines)
        self.returncode = 0

    async def wait(self):
        return 0


async def drain(lines: list[str]) -> None:
    """跑真的 ``McpProcessManager._drain_logs``（子行程已被換掉的路徑：讀完輸出就返回，不碰重啟邏輯）。"""
    fake = SimpleNamespace(_last_output=collections.deque(maxlen=20), _process=object())
    await McpProcessManager._drain_logs(fake, _Proc(lines))


# ---- 一個帳號的流量不能擠掉其他帳號的紀錄 ------------------------------------------------------------------------------


def test_busy_profile_cannot_empty_a_quiet_profiles_logs_page(service, capture):
    """mp-qa Q3-S4／mp-odoo-res R2b：忙的帳號跑一陣子之後，安靜帳號（與預設帳號）的 Logs 頁原本會變 0 筆。"""
    client, auth, (quiet, busy) = service
    registry = profiles.get_profile_registry()
    with logs._buffer_lock:
        logs._log_buffer.clear()  # 建帳號時「沒有指令、不啟動」那幾行不算
    logging.getLogger(PROC).info("default-profile-lifecycle-line")
    with registry.activate(quiet):
        logging.getLogger(PROC).info("Starting MCP server: synthetic-child --port 18000")
        logging.getLogger(PROC).warning("MCP server exited unexpectedly with code %s", -9)
    quiet_before, default_before = page(client, auth, quiet), page(client, auth, "default")
    assert quiet_before["total_buffered"] > 0 and default_before["total_buffered"] > 0

    with registry.activate(busy):
        proxy_traffic(6000, 18001)  # 舊的共用緩衝區只有 5,000 筆，每個請求又寫兩到三筆

    assert page(client, auth, quiet) == quiet_before
    assert page(client, auth, "default") == default_before
    assert [entry["message"] for entry in page(client, auth, quiet)["entries"]] == [
        "MCP server exited unexpectedly with code -9", "Starting MCP server: synthetic-child --port 18000"]
    assert [entry["message"] for entry in page(client, auth, "default")["entries"]] == ["default-profile-lifecycle-line"]
    busy_page = page(client, auth, busy)
    assert busy_page["total_buffered"] > 0
    assert all("synthetic-child" not in entry["message"] for entry in busy_page["entries"])


def test_busy_profile_keeps_its_own_newest_lines_up_to_its_cap(capture):
    busy = "b" * 24
    with as_profile(busy):
        for i in range(logs.PROFILE_LOG_LINES + 50):
            logging.getLogger("httpx").info("synthetic busy line %d", i)
    with logs._buffer_lock:
        kept = [entry["message"] for entry in logs._log_buffer.entries(busy)]
    assert len(kept) == logs.PROFILE_LOG_LINES
    assert kept[0] == "synthetic busy line 50"
    assert kept[-1] == f"synthetic busy line {logs.PROFILE_LOG_LINES + 49}"


def test_single_profile_service_keeps_as_many_lines_as_before(capture):
    """正式站目前每台只有預設帳號：保留筆數與修正前（整台 5,000 筆）相同，沒有變少。"""
    assert logs.PROFILE_LOG_LINES == 5000
    for i in range(logs.PROFILE_LOG_LINES):
        logging.getLogger("httpx").info("synthetic default line %d", i)
    with logs._buffer_lock:
        assert len(logs._log_buffer.entries("default")) == 5000


# ---- 子程式輸出只存一次 --------------------------------------------------------------------------------------------


async def test_each_child_output_line_is_stored_once(capture):
    """mp-n8n：每行子程式輸出原本存兩份（mcp-server 去前綴、mcp_admin_core.process 帶前綴）。"""
    lines = ["[2026-11-13T03:05:36.081Z] [n8n-mcp] [INFO] Session removed {", "  reason: 'transport_closed'", "}",
             "ERROR: synthetic backend refused the request"]
    await drain(lines)
    stored = [(entry["source"], entry["level"], entry["message"]) for entry in all_buffered()]
    assert stored == [("mcp-server", "info", lines[0]), ("mcp-server", "info", lines[1]), ("mcp-server", "info", lines[2]),
                      ("mcp-server", "error", lines[3])]


async def test_child_output_with_a_credential_is_stored_once_and_masked(capture):
    secret = "synthetic-logs-buffer-token-0123456789abcdef"
    await drain([f"GET /private_{secret}/mcp failed", "GET /x?token=synthetic-backend-detail%zz"])
    stored = [(entry["source"], entry["message"]) for entry in all_buffered()]
    assert stored == [("mcp-server", "GET /private_***/mcp failed"), ("mcp-server", log_redaction.AMBIGUOUS)]
    assert all(secret not in entry["message"] for entry in all_buffered())


@pytest.mark.parametrize("level, message", [
    (logging.INFO, "MCP server started (pid=7)"),
    (logging.WARNING, "MCP server exited unexpectedly with code -9"),
    (logging.ERROR, "Refusing to start the MCP child on 127.0.0.1:9 (child_port_in_use): another socket is bound"),
])
def test_lifecycle_line_is_stored_once_with_its_own_level(capture, level, message):
    logging.getLogger(PROC).log(level, "%s", message)
    stored = [(entry["source"], entry["level"], entry["message"]) for entry in all_buffered()]
    assert stored == [(PROC, logging.getLevelName(level).lower(), message)]


def test_admin_log_line_is_stored_once_even_if_two_capturing_loggers_see_it(capture):
    """同一筆 record 經過兩個裝了 admin handler 的 logger（例如 app 自己的 logger 是另一個的子 logger），也只存一次。"""
    target = logging.getLogger("mcp_admin_core.owner_defaults")
    saved = (list(target.handlers), target.level)
    logs._install_admin_log_capture(("mcp_admin_core.owner_defaults",))
    try:
        target.warning("synthetic owner defaults line")
        assert [entry["message"] for entry in all_buffered()] == ["synthetic owner defaults line"]
    finally:
        target.handlers[:] = saved[0]
        target.setLevel(saved[1])


def test_access_log_lines_are_not_buffered(capture):
    """uvicorn access 在請求的 profile context 結束後才記，不屬於任何帳號，沒有 Logs 頁看得到：不佔緩衝區。"""
    logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d', "10.0.0.1:5555", "GET", "/healthz", "1.1", 200)
    assert all_buffered() == []


# ---- 記憶體總量有上限 ----------------------------------------------------------------------------------------------


def test_a_profiles_buffer_is_also_bounded_by_message_bytes(capture):
    profile_id = "c" * 24
    big = "x" * (64 * 1024)
    with as_profile(profile_id):
        for i in range(100):
            logging.getLogger("httpx").info("%d %s", i, big)
    with logs._buffer_lock:
        kept = logs._log_buffer.entries(profile_id)
        used = logs._log_buffer.message_bytes(profile_id)
    assert used <= logs.PROFILE_LOG_MESSAGE_BYTES
    assert used == sum(sys.getsizeof(entry["message"]) for entry in kept)
    assert 1 < len(kept) < 100 and kept[-1]["message"].startswith("99 ")


def test_an_oversized_line_is_still_shown_alone(capture):
    """遮罩已把單一字串截在 128 KiB；直接寫入比訊息上限還大的一行時，頁面上仍看得到這一行（其餘淘汰）。"""
    profile_id = "c" * 24
    logs._append_log_line_sync("first", source="httpx", level="info", profile_id=profile_id)
    logs._append_log_line_sync("z" * (logs.PROFILE_LOG_MESSAGE_BYTES + 10), source="httpx", level="info", profile_id=profile_id)
    with logs._buffer_lock:
        kept = logs._log_buffer.entries(profile_id)
    assert len(kept) == 1 and kept[0]["message"].startswith("zzz")


def test_buffers_are_kept_for_a_bounded_number_of_profiles_and_deleted_ones_go_first(service, capture):
    """管理員反覆建立、刪除帳號時，已刪除帳號的緩衝區先淘汰；還存在、但很久沒寫紀錄的帳號與預設帳號保留。"""
    client, auth, (quiet, _busy) = service
    registry = profiles.get_profile_registry()
    with logs._buffer_lock:
        logs._log_buffer.clear()
    logging.getLogger(PROC).info("default-profile-line")
    with registry.activate(quiet):
        logging.getLogger(PROC).info("quiet-profile-line")  # 最早寫、之後一直安靜
    for n in range(3 * logs.MAX_LOG_PROFILES):  # 不在 profile_index 的帳號（已刪除）一個接一個寫紀錄
        with as_profile(f"{n + 1:024x}"):
            logging.getLogger(PROC).info("deleted-profile-line %d", n)
    with logs._buffer_lock:
        kept_ids = logs._log_buffer.profile_ids()
    assert len(kept_ids) == logs.MAX_LOG_PROFILES
    assert {"default", quiet} <= set(kept_ids)
    assert [entry["message"] for entry in page(client, auth, quiet)["entries"]] == ["quiet-profile-line"]
    assert page(client, auth, "default", q="default-profile-line")["matched"] == 1


def _index_unreadable():
    raise RuntimeError("synthetic: profile index snapshot unavailable")


@pytest.mark.parametrize("existing", [lambda: None, _index_unreadable], ids=["unknown", "raises"])
def test_without_a_profile_index_the_least_recently_written_buffer_goes_first(capture, monkeypatch, existing):
    """registry 還沒建立、設定還沒載入（None），或判斷本身出錯：照最久沒寫的淘汰，寫 log 照常成功。"""
    monkeypatch.setattr(profiles, "existing_profile_ids", existing)
    logging.getLogger(PROC).info("default-profile-line")
    ids = [f"{n + 1:024x}" for n in range(logs.MAX_LOG_PROFILES + 2)]
    for n, profile_id in enumerate(ids):
        with as_profile(profile_id):
            logging.getLogger(PROC).info("profile-line %d", n)
    with logs._buffer_lock:
        kept_ids = logs._log_buffer.profile_ids()
    assert set(kept_ids) == {"default", *ids[-(logs.MAX_LOG_PROFILES - 1):]}


def test_all_live_profiles_fit_in_the_buffer_limit():
    assert logs.MAX_LOG_PROFILES >= 1 + profiles.MAX_EXTRA_PROFILES


def test_total_buffer_memory_stays_under_the_documented_ceiling(capture):
    """所有緩衝區都寫滿（筆數與訊息位元組兩個上限同時碰到），實際配置量仍在 ``MAX_LOG_MEMORY_BYTES`` 以內。"""
    ids = ["default", *(f"{n + 1:024x}" for n in range(logs.MAX_LOG_PROFILES + 1))]
    # 每筆都是新的字串物件，長度讓 5,000 筆剛好碰到訊息位元組上限（最壞情況：兩個上限都用滿）
    width = (logs.PROFILE_LOG_MESSAGE_BYTES // logs.PROFILE_LOG_LINES) - (sys.getsizeof("ab") - 2)
    tracemalloc.start()
    try:
        baseline = tracemalloc.get_traced_memory()[0]
        for profile_id in ids:
            for i in range(logs.PROFILE_LOG_LINES + 10):
                logs._append_log_line_sync(f"{i:06d} ".ljust(width, "y"), source="httpx", level="info", profile_id=profile_id)
        used = tracemalloc.get_traced_memory()[0] - baseline
    finally:
        tracemalloc.stop()
    with logs._buffer_lock:
        kept_ids = logs._log_buffer.profile_ids()
        entries = len(logs._log_buffer)
        message_bytes = sum(logs._log_buffer.message_bytes(profile_id) for profile_id in kept_ids)
    assert len(kept_ids) == logs.MAX_LOG_PROFILES
    assert entries <= logs.MAX_LOG_PROFILES * logs.PROFILE_LOG_LINES
    assert message_bytes <= logs.MAX_LOG_PROFILES * logs.PROFILE_LOG_MESSAGE_BYTES
    assert used <= logs.MAX_LOG_MEMORY_BYTES, (used, logs.MAX_LOG_MEMORY_BYTES)


# ---- Logs 頁的 API 與 SSE 照常可用 -----------------------------------------------------------------------------------


def test_search_response_shape_is_unchanged(service, capture):
    client, auth, (quiet, _busy) = service
    registry = profiles.get_profile_registry()
    with logs._buffer_lock:
        logs._log_buffer.clear()
    with registry.activate(quiet):
        logging.getLogger(PROC).info("shape-needle")
    body = page(client, auth, quiet, q="shape-needle")
    assert set(body) == {"query", "total_buffered", "matched", "entries"}
    assert (body["total_buffered"], body["matched"]) == (1, 1)
    assert set(body["entries"][0]) == {"timestamp", "level", "source", "message"}
    assert page(client, auth, "default", q="shape-needle")["matched"] == 0


async def test_stream_sends_own_new_lines_even_when_another_profile_floods_between_polls(capture, monkeypatch):
    monkeypatch.setattr(logs, "POLL_INTERVAL", 0.01)
    quiet, busy = "d" * 24, "e" * 24
    with as_profile(quiet):
        logging.getLogger(PROC).info("quiet-before-connect")
    stream = logs._sse_log_generator(tail_lines=5, profile_id=quiet)
    try:
        assert (await stream.__anext__()).startswith("event: connected\n")
        tail = await stream.__anext__()
        assert json.loads(tail.removeprefix("data: "))["message"] == "quiet-before-connect"
        assert set(json.loads(tail.removeprefix("data: "))) == {"timestamp", "level", "source", "message"}

        with as_profile(quiet):
            logging.getLogger(PROC).info("quiet-after-connect")
        with as_profile(busy):  # 下一次輪詢之前，另一個帳號寫了比緩衝區還多的紀錄
            for i in range(6000):
                logging.getLogger("httpx").info("busy-flood %d", i)
        with as_profile(quiet):
            logging.getLogger(PROC).info("quiet-after-flood")

        received = [await asyncio.wait_for(stream.__anext__(), 5) for _ in range(2)]
        assert [json.loads(event.removeprefix("data: "))["message"] for event in received] == [
            "quiet-after-connect", "quiet-after-flood"]
    finally:
        await stream.aclose()


async def test_stream_endpoint_serves_only_the_selected_profile(service, capture):
    """EventSource（前端以 fetch 帶 X-MCP-Profile）經過整個 app：200、text/event-stream、connected 事件、只有該帳號的紀錄。"""
    client, auth, (quiet, busy) = service
    registry = profiles.get_profile_registry()
    with logs._buffer_lock:
        logs._log_buffer.clear()
    with registry.activate(busy):
        logging.getLogger(PROC).info("busy-stream-line")
    with registry.activate(quiet):
        logging.getLogger(PROC).info("quiet-stream-line")
    with registry.activate(busy):
        logging.getLogger(PROC).info("busy-stream-line-2")

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": "/api/logs/stream", "raw_path": b"/api/logs/stream", "query_string": b"tail=10", "root_path": "",
        "headers": [(b"host", b"testserver"), (b"accept", b"text/event-stream"),
                    (b"authorization", auth["Authorization"].encode()), (b"x-mcp-profile", quiet.encode())],
        "client": ("127.0.0.1", 5555), "server": ("testserver", 80),
    }
    disconnect = asyncio.Event()
    requested = False
    sent: list[dict] = []

    async def receive():
        nonlocal requested
        if not requested:
            requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if b"quiet-stream-line" in b"".join(m.get("body", b"") for m in sent):
            disconnect.set()

    await asyncio.wait_for(client.app(scope, receive, send), 10)
    start = sent[0]
    assert start["type"] == "http.response.start" and start["status"] == 200
    headers = {key.decode().lower(): value.decode() for key, value in start["headers"]}
    assert headers["content-type"].startswith("text/event-stream")
    body = b"".join(message.get("body", b"") for message in sent[1:]).decode()
    assert body.startswith("event: connected\n")
    data = [json.loads(line.removeprefix("data: ")) for line in body.splitlines() if line.startswith("data: ")]
    assert [item["message"] for item in data if "status" not in item] == ["quiet-stream-line"]
