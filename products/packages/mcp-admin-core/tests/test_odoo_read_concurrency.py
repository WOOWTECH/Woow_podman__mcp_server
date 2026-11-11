"""Odoo MCP 唯讀工具並發（商用驗收 DATA-ODOO-011-1）。

MCP 240 在 10 個 session 並發時延遲隨並發線性上升（p95 約 3.8 s）。根因：上游 odoo-mcp 的工具是同步
``def``，mcp 1.x 的 FastMCP 直接在事件迴圈上呼叫，每次 Odoo XML-RPC 都卡住整個行程。
修法見 ``odoo_mcp_admin/concurrency.py``（mcp 2.x 改成預設把同步工具全部丟工作緒，修法同時把寫入類釘回
事件迴圈，#78）。這裡兩層驗證：

1. 單元：「同步且唯讀」的工具改在工作緒跑；「同步且非唯讀」的寫入類釘在事件迴圈上（序列）；本來就是 async 的
   不動；idempotent；ServerProxy 在工作緒各自一份、事件迴圈上維持共用那一份。
2. 端到端：假的 Odoo XML-RPC（每個呼叫睡一下）＋真的 ``woow-odoo-mcp`` 子行程＋官方 MCP SDK
   開 10 個 session 並發呼叫 search_records；牆鐘時間必須明顯小於序列時間。

寫入序列化與 ``ODOO_MCP_READ_THREADS`` 上限的端到端量測、以及「不 import fastmcp」的正式映像條件，見
``test_odoo_concurrency_sdk2.py``。
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from socketserver import ThreadingMixIn
from xmlrpc.server import Fault, SimpleXMLRPCRequestHandler, SimpleXMLRPCServer

import pytest

pytest.importorskip("odoo_mcp")
pytest.importorskip("odoo_mcp_admin")


# ---- 單元 ----------------------------------------------------------------------

WRITE_LIKE = {"chatter_post", "execute_approved_write", "execute_method"}
#: WRITE_LIKE 中上游本來就寫成 async 的（不經 concurrency 包裝，SDK 直接在事件迴圈上 await）。
ASYNC_WRITE = {"execute_approved_write"}


@pytest.fixture
def fresh_upstream(monkeypatch):
    """每個測試用乾淨的上游 server 模組狀態（install 會改 tool.fn，測完還原）。"""
    from odoo_mcp.odoo_client import OdooClient
    from odoo_mcp.server import mcp

    saved = {t.name: (t.fn, t.is_async) for t in mcp._tool_manager.list_tools()}
    saved_cls = (OdooClient.__dict__.get("_common"), OdooClient.__dict__.get("_models"))
    yield mcp, OdooClient
    for tool in mcp._tool_manager.list_tools():
        tool.fn, tool.is_async = saved[tool.name]
    for name, value in zip(("_common", "_models"), saved_cls):
        if value is None:
            if name in OdooClient.__dict__:
                delattr(OdooClient, name)
        else:
            setattr(OdooClient, name, value)


def test_only_sync_read_only_tools_move_to_threads(fresh_upstream):
    from odoo_mcp_admin import concurrency

    mcp, client_cls = fresh_upstream
    tools = {t.name: t for t in mcp._tool_manager.list_tools()}
    original = {name: t.fn for name, t in tools.items()}
    patched = concurrency.install(mcp, client_cls)

    assert set(tools) - set(patched) == WRITE_LIKE, "寫入／核准類工具必須維持在事件迴圈上序列執行"
    assert "search_records" in patched and "read_record" in patched and "get_model_fields" in patched
    assert concurrency.tools_in_lane(mcp, "thread") == patched
    for name in patched:
        assert tools[name].is_async is True
        # mcp 2.x 的欄位是 snake_case；不要讀 readOnlyHint（只有 import 過 fastmcp 的行程才補得回來）
        assert tools[name].annotations.read_only_hint is True
    # mcp 2.x 會把同步工具丟工作緒並行：同步的寫入類要包成 async、釘回事件迴圈（與 1.x 相同的序列語意）
    sync_writes = WRITE_LIKE - ASYNC_WRITE
    assert concurrency.tools_in_lane(mcp, "event_loop") == sorted(sync_writes)
    for name in sync_writes:
        assert tools[name].is_async is True
        assert tools[name].fn.__wrapped__ is original[name]
    for name in ASYNC_WRITE:
        assert tools[name].fn is original[name], f"{name} 上游本來就是 async，不得被包裝"


async def test_write_tools_run_on_the_event_loop_thread(fresh_upstream):
    """非唯讀的同步工具不進工作緒（mcp 2.x 預設會丟工作緒）；唯讀的進工作緒。"""
    import threading

    from odoo_mcp_admin import concurrency

    mcp, client_cls = fresh_upstream
    seen = {}

    def probe(name):
        def run(**_kwargs):
            seen[name] = threading.current_thread()
            return {"success": True}
        return run

    tools = {t.name: t for t in mcp._tool_manager.list_tools()}
    for name in ("execute_method", "search_records"):
        tools[name].fn = probe(name)
    concurrency.install(mcp, client_cls)
    await tools["execute_method"].fn(model="res.partner", method="read")
    await tools["search_records"].fn(model="res.partner")
    assert seen["execute_method"] is threading.current_thread(), "寫入類必須在事件迴圈的執行緒上執行"
    assert seen["search_records"] is not threading.current_thread(), "唯讀工具必須在工作緒執行"


async def test_exceptions_keep_their_text_through_the_launcher_patches(fresh_upstream):
    """工具直接丟出的例外（不是上游自己包好的 success: false）照 mcp 1.x 回「Error executing tool X: 原因」。

    走 launcher 的修補順序（tool_errors → keep_error_text → concurrency），唯讀工具的例外從工作緒傳回、
    寫入類的例外在事件迴圈上丟出，兩條路都要保留文字（mcp 2.x 預設只回 "Error executing tool X"，#78）。
    """
    from mcp.client import Client

    from mcp_admin_core.tool_error_text import keep_error_text
    from odoo_mcp_admin import concurrency, tool_errors

    mcp, client_cls = fresh_upstream
    tools = {t.name: t for t in mcp._tool_manager.list_tools()}

    def boom(**_kwargs):
        raise RuntimeError("Odoo exploded (ProtocolError 502)")

    for name in ("search_records", "execute_method"):
        tools[name].fn = boom
    tool_errors.install(mcp)
    keep_error_text(mcp)
    concurrency.install(mcp, client_cls)
    assert getattr(tools["search_records"].fn, concurrency.LANE_ATTR) == "thread"
    assert getattr(tools["execute_method"].fn, concurrency.LANE_ATTR) == "event_loop"
    async with Client(mcp, mode="legacy") as client:
        for name, args in (("search_records", {"model": "res.partner"}),
                           ("execute_method", {"model": "res.partner", "method": "read"})):
            result = await client.call_tool(name, args)
            assert result.is_error is True
            assert result.content[0].text == f"Error executing tool {name}: Odoo exploded (ProtocolError 502)"


def test_install_is_idempotent(fresh_upstream):
    from odoo_mcp_admin import concurrency

    mcp, client_cls = fresh_upstream
    first = concurrency.install(mcp, client_cls)
    assert first and concurrency.install(mcp, client_cls) == []


def test_proxies_are_per_thread_and_shared_on_the_event_loop(fresh_upstream):
    from odoo_mcp_admin import concurrency

    _mcp, client_cls = fresh_upstream
    concurrency.install(_mcp, client_cls)

    class Fake(client_cls):  # 不連線的 client：只驗 descriptor
        def __init__(self):
            self.url, self.timeout, self.verify_ssl = "http://odoo.invalid", 3, True
            self._models = "SHARED-MODELS"
            self._common = "SHARED-COMMON"

    client = Fake()
    assert client._models == "SHARED-MODELS" and client._common == "SHARED-COMMON"

    seen = {}

    def worker(tag):
        concurrency._tls.worker = True
        try:
            a, b = client._models, client._models
            seen[tag] = (a, b, client._common)
        finally:
            concurrency._tls.worker = False

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    for tag in (0, 1):
        models_a, models_b, common = seen[tag]
        assert models_a is models_b, "同一執行緒重用同一份 proxy"
        assert models_a != "SHARED-MODELS" and common != "SHARED-COMMON"
        assert models_a._ServerProxy__host == "odoo.invalid"
    assert seen[0][0] is not seen[1][0], "不同執行緒不可共用同一個 Transport（持久連線不是執行緒安全的）"
    assert client._models == "SHARED-MODELS", "事件迴圈上的呼叫（寫入類）仍用共用的那一份"


# ---- 端到端 --------------------------------------------------------------------

FAKE_DELAY = 0.2


class _Threaded(ThreadingMixIn, SimpleXMLRPCServer):
    daemon_threads = True
    allow_reuse_address = True


class _Handler(SimpleXMLRPCRequestHandler):
    rpc_paths = ("/xmlrpc/2/common", "/xmlrpc/2/object")


class _FakeOdoo:
    def _dispatch(self, method, params):
        if method == "version":
            return {"server_version": "18.0"}
        if method == "authenticate":
            return 2
        if method == "execute_kw":
            time.sleep(FAKE_DELAY)
            if params[3] == "bad.model":
                raise Fault(2, "Traceback (most recent call last):\n  File \"/usr/lib/python3/dist-packages/odoo/http.py\", "
                               "line 1, in dispatch\nodoo.exceptions.AccessError: You are not allowed to access 'bad'.\n")
            odoo_method = params[4]
            if odoo_method == "fields_get":
                return {"name": {"type": "char", "string": "Name", "store": True}}
            if odoo_method == "search_read":
                return [{"id": 1, "name": "x"}]
            if odoo_method == "search_count":
                return 1
            return []
        raise Fault(1, method)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_ten_concurrent_sessions_do_not_queue_behind_each_other(tmp_path):
    pytest.importorskip("mcp.client.streamable_http")
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    odoo = _Threaded(("127.0.0.1", 0), requestHandler=_Handler, logRequests=False, allow_none=True)
    odoo.register_instance(_FakeOdoo())
    threading.Thread(target=odoo.serve_forever, daemon=True).start()
    mcp_port = _free_port()
    env = dict(
        os.environ,
        ODOO_URL=f"http://127.0.0.1:{odoo.server_address[1]}", ODOO_DB="d",
        ODOO_USERNAME="u", ODOO_PASSWORD="p",
        ODOO_MCP_RETRY_ATTEMPTS="0", ODOO_MCP_ENABLE_WRITES="0", ODOO_MCP_LOG_FILE=str(tmp_path / "mcp.log"),
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "odoo_mcp_admin.launcher",
         "--transport", "streamable-http", "--host", "127.0.0.1", "--port", str(mcp_port)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    async def call_once():
        async with streamable_http_client(f"http://127.0.0.1:{mcp_port}/mcp") as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                start = time.monotonic()
                result = await session.call_tool(
                    "search_records", {"model": "res.partner", "fields": ["name"], "limit": 5})
                return time.monotonic() - start, result

    async def failing_call():
        async with streamable_http_client(f"http://127.0.0.1:{mcp_port}/mcp") as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                return await session.call_tool(
                    "search_records", {"model": "bad.model", "fields": ["name"], "limit": 5})

    async def scenario():
        for _ in range(80):
            try:
                socket.create_connection(("127.0.0.1", mcp_port), timeout=0.5).close()
                break
            except OSError:
                await asyncio.sleep(0.25)
        single, warm = await call_once()  # 暖機＋單一請求的基準
        assert json.loads(warm.content[0].text)["success"] is True, warm.content[0].text
        single, _ = await call_once()
        start = time.monotonic()
        results = await asyncio.gather(*[call_once() for _ in range(10)])
        return single, time.monotonic() - start, results, await failing_call()

    try:
        single, wall, results, failed = asyncio.run(asyncio.wait_for(scenario(), timeout=120))
    finally:
        proc.terminate()
        proc.wait(timeout=15)
        odoo.shutdown()

    assert all(json.loads(r.content[0].text)["success"] for _t, r in results)
    assert not any(r.is_error for _t, r in results), "成功的呼叫不得帶 isError"

    # DATA-OMCP-004-4：工具失敗要讓 MCP 用戶端看得出來，且不外洩 Odoo 的 Traceback／內部路徑
    assert failed.is_error is True
    body = json.loads(failed.content[0].text)
    assert body["success"] is False and "not allowed" in body["error"]
    assert "Traceback" not in failed.content[0].text and "dist-packages" not in failed.content[0].text
    assert failed.structured_content == body, "文字內容與 structuredContent 是同一份 JSON"
    serial = single * 10
    # 修補前：10 個請求在事件迴圈上排成一條隊，牆鐘 ≈ 序列時間（實測 3.6 s vs 單次 0.34 s）；
    # 修補後明顯並行（實測 0.9 s）。門檻取序列時間的 60%，給 CI 機器很大的餘裕。
    assert wall < serial * 0.6, f"10 並發牆鐘 {wall:.2f}s，序列估計 {serial:.2f}s（單次 {single:.2f}s）"


# ---- tool_errors 單元 ----------------------------------------------------------


def test_scrub_error_text_keeps_only_the_last_exception_line():
    from odoo_mcp_admin.tool_errors import scrub_error_text

    fault = ("<Fault 2: 'Traceback (most recent call last):\\n  File \"/usr/lib/python3/dist-packages/odoo/http.py\", "
             "line 1, in x\\nodoo.exceptions.UserError: boom\\n'>")
    assert scrub_error_text(fault) == "Odoo server error: odoo.exceptions.UserError: boom"
    real_newlines = "Traceback (most recent call last):\n  File \"x\", line 1\nValueError: nope\n"
    assert scrub_error_text(real_newlines) == "Odoo server error: ValueError: nope"
    quoted = "<Fault 2: 'Traceback (most recent call last):\\nodoo.exceptions.AccessError: no access to \\'bad\\'\\n'>"
    assert scrub_error_text(quoted).endswith("no access to 'bad'")
    assert scrub_error_text("Unknown model res.nothing") == "Unknown model res.nothing"
    assert scrub_error_text(None) is None


def test_as_error_result_flags_only_success_false():
    from mcp.types import CallToolResult

    from odoo_mcp_admin.tool_errors import as_error_result

    ok = {"success": True, "count": 0}
    assert as_error_result(ok) is ok
    assert as_error_result("text") == "text"
    no_flag = {"result": 1}
    assert as_error_result(no_flag) is no_flag
    flagged = as_error_result({"success": False, "error": "x"})
    assert isinstance(flagged, CallToolResult) and flagged.is_error is True
    assert flagged.structured_content == {"success": False, "error": "x"}


def test_as_error_result_wraps_structured_content_when_the_schema_is_wrapped():
    from odoo_mcp_admin.tool_errors import as_error_result

    flagged = as_error_result({"success": False, "error": "x"}, wrap=True)
    assert flagged.is_error is True
    assert flagged.structured_content == {"result": {"success": False, "error": "x"}}
    assert json.loads(flagged.content[0].text) == {"success": False, "error": "x"}, "文字內容不包"


def test_error_results_match_every_published_output_schema(fresh_upstream):
    """#78：每支工具的錯誤結果 structuredContent 都要符合 tools/list 公布的 outputSchema。

    TS SDK client（n8n、Claude Code）連錯誤結果的 structuredContent 都會對 outputSchema 驗證，不符就整個呼叫丟例外；
    mcp 1.x 則在 server 端驗證失敗、把錯誤換成 pydantic 驗證錯誤。``Dict`` 回傳的 15 支工具 outputSchema 是
    ``{"result": …}``，錯誤結果也要照包。
    """
    import inspect

    import jsonschema

    from odoo_mcp_admin import tool_errors

    mcp, _client_cls = fresh_upstream
    failing = {"success": False, "tool": "t", "error": "Traceback (most recent call last):\nValueError: nope"}
    for tool in mcp._tool_manager.list_tools():
        if inspect.iscoroutinefunction(tool.fn):
            async def fake(**_kwargs):
                return dict(failing)
        else:
            def fake(**_kwargs):
                return dict(failing)
        tool.fn = fake
    tool_errors.install(mcp)
    wrapped = 0
    for tool in mcp._tool_manager.list_tools():
        result = asyncio.run(tool.fn()) if inspect.iscoroutinefunction(tool.fn) else tool.fn()
        assert result.is_error is True, tool.name
        assert tool.output_schema, tool.name
        jsonschema.validate(result.structured_content, tool.output_schema)
        wrapped += "result" in result.structured_content
    assert wrapped == 15


def test_tool_errors_install_wraps_every_tool_once(fresh_upstream):
    from odoo_mcp_admin import tool_errors

    mcp, _client_cls = fresh_upstream
    first = tool_errors.install(mcp)
    assert len(first) == len(mcp._tool_manager.list_tools()) == 41
    assert tool_errors.install(mcp) == [], "idempotent"
