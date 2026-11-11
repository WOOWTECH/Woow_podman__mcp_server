"""search_records／read_record 的回應大小上限（商用驗收 DATA-OMCP-007-3 延伸）。

修前：``search_records(model="ir.attachment", fields=["datas"])`` 把附件的 base64 整包塞進回應——
3 筆就 17.7 MiB，14 筆讀 125 秒，被 Cloudflare 以 524 切斷；就算送得到，也遠超過任何 LLM 的上下文。
上游 ``read_attachment`` 有 1 MiB 上限（ODOO_MCP_MAX_ATTACHMENT_BYTES），但這兩支泛用讀取工具沒有。

修法見 ``odoo_mcp_admin/result_limits.py``：結果超過 ``ODOO_MCP_MAX_RESULT_BYTES``（預設 2 MiB）時改回
``success: false``（再由 tool_errors 標 isError），訊息列出最大的欄位並指引改用 fields／limit／read_attachment。
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
from socketserver import ThreadingMixIn
from xmlrpc.server import Fault, SimpleXMLRPCRequestHandler, SimpleXMLRPCServer

import pytest

pytest.importorskip("odoo_mcp")
pytest.importorskip("odoo_mcp_admin")

MIB = 1024 * 1024


# ---- 單元 ----------------------------------------------------------------------


def test_small_success_result_passes_through_untouched(monkeypatch):
    from odoo_mcp_admin import result_limits

    monkeypatch.delenv("ODOO_MCP_MAX_RESULT_BYTES", raising=False)
    ok = {"success": True, "count": 1, "result": [{"id": 1, "name": "x"}]}
    assert result_limits.check_result_size(ok) is ok


def test_oversized_result_becomes_a_readable_failure(monkeypatch):
    from odoo_mcp_admin import result_limits

    monkeypatch.setenv("ODOO_MCP_MAX_RESULT_BYTES", str(MIB))
    big = {
        "success": True,
        "count": 2,
        "result": [{"id": i, "name": f"asset{i}.js", "datas": "A" * MIB} for i in (1, 2)],
        "fields_used": ["name", "datas"],
    }
    out = result_limits.check_result_size(big)
    assert out["success"] is False
    assert "datas" in out["error"] and "read_attachment" in out["error"] and "limit" in out["error"]
    assert out["largest_fields"][0] == "datas"
    assert out["result_bytes"] > 2 * MIB and out["max_result_bytes"] == MIB
    assert len(json.dumps(out)) < 4096, "錯誤回應本身要小"


def test_read_record_shape_is_measured_too(monkeypatch):
    from odoo_mcp_admin import result_limits

    monkeypatch.setenv("ODOO_MCP_MAX_RESULT_BYTES", str(MIB))
    big = {"success": True, "result": {"id": 1, "image_1920": "B" * (2 * MIB)}}
    out = result_limits.check_result_size(big)
    assert out["success"] is False and out["largest_fields"][0] == "image_1920"


def test_failures_and_disabled_cap_pass_through(monkeypatch):
    from odoo_mcp_admin import result_limits

    failure = {"success": False, "error": "boom"}
    assert result_limits.check_result_size(failure) is failure
    monkeypatch.setenv("ODOO_MCP_MAX_RESULT_BYTES", "0")
    big = {"success": True, "result": [{"id": 1, "datas": "A" * (3 * MIB)}]}
    assert result_limits.check_result_size(big) is big
    monkeypatch.setenv("ODOO_MCP_MAX_RESULT_BYTES", "not-a-number")
    assert result_limits.max_result_bytes() == result_limits.DEFAULT_MAX_RESULT_BYTES


@pytest.fixture
def fresh_upstream():
    from odoo_mcp.server import mcp

    saved = {t.name: (t.fn, t.is_async) for t in mcp._tool_manager.list_tools()}
    yield mcp
    for tool in mcp._tool_manager.list_tools():
        tool.fn, tool.is_async = saved[tool.name]


def test_install_wraps_only_the_generic_read_tools_once(fresh_upstream):
    from odoo_mcp_admin import result_limits

    assert result_limits.install(fresh_upstream) == ["read_record", "search_records"]
    assert result_limits.install(fresh_upstream) == [], "idempotent"


# ---- 端到端：真的 woow-odoo-mcp 子行程＋假 Odoo ------------------------------------

ASSET_BYTES = int(1.5 * MIB)


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
        if method != "execute_kw":
            raise Fault(1, method)
        odoo_method, kwargs = params[4], (params[6] if len(params) > 6 else {})
        if odoo_method == "fields_get":
            return {"name": {"type": "char", "string": "Name", "store": True},
                    "datas": {"type": "binary", "string": "File Content", "store": False}}
        fields = kwargs.get("fields") or ["name", "datas"]
        if odoo_method in ("search_read", "read"):
            ids = params[5][0] if odoo_method == "read" else [1, 2, 3]
            rows = []
            for i in ids:
                row = {"id": i}
                if "name" in fields:
                    row["name"] = f"asset{i}.js"
                if "datas" in fields:
                    row["datas"] = "A" * ASSET_BYTES
                rows.append(row)
            return rows
        if odoo_method == "search_count":
            return 3
        return []


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_real_launcher_refuses_oversized_binary_reads(tmp_path):
    pytest.importorskip("mcp.client.streamable_http")
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    odoo = _Threaded(("127.0.0.1", 0), requestHandler=_Handler, logRequests=False, allow_none=True)
    odoo.register_instance(_FakeOdoo())
    threading.Thread(target=odoo.serve_forever, daemon=True).start()
    mcp_port = _free_port()
    env = {k: v for k, v in os.environ.items() if k != "ODOO_MCP_MAX_RESULT_BYTES"}
    env.update(
        ODOO_URL=f"http://127.0.0.1:{odoo.server_address[1]}", ODOO_DB="d",
        ODOO_USERNAME="u", ODOO_PASSWORD="p",
        ODOO_MCP_RETRY_ATTEMPTS="0", ODOO_MCP_ENABLE_WRITES="0", ODOO_MCP_LOG_FILE=str(tmp_path / "mcp.log"),
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "odoo_mcp_admin.launcher",
         "--transport", "streamable-http", "--host", "127.0.0.1", "--port", str(mcp_port)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    async def scenario():
        for _ in range(80):
            try:
                socket.create_connection(("127.0.0.1", mcp_port), timeout=0.5).close()
                break
            except OSError:
                await asyncio.sleep(0.25)
        async with streamable_http_client(f"http://127.0.0.1:{mcp_port}/mcp", max_sse_event_size=None) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                big_search = await session.call_tool(
                    "search_records", {"model": "ir.attachment", "fields": ["name", "datas"], "limit": 3})
                big_read = await session.call_tool(
                    "read_record", {"model": "ir.attachment", "record_id": 1, "fields": ["*"]})
                small = await session.call_tool(
                    "search_records", {"model": "ir.attachment", "fields": ["name"], "limit": 3})
                return big_search, big_read, small

    try:
        big_search, big_read, small = asyncio.run(asyncio.wait_for(scenario(), timeout=120))
    finally:
        proc.terminate()
        proc.wait(timeout=15)
        odoo.shutdown()

    # 3 筆 × 1.5 MiB = 4.5 MiB > 2 MiB 預設上限
    assert big_search.is_error is True
    text = big_search.content[0].text
    assert len(text) < 8192, f"錯誤回應本身要小（實際 {len(text)} bytes）"
    body = json.loads(text)
    assert body["success"] is False and body["largest_fields"][0] == "datas"
    assert "read_attachment" in body["error"]
    # read_record 單筆 1.5 MiB 未超過 2 MiB → 照常回傳（上限不是禁止二進位欄位）
    assert big_read.is_error is False
    assert len(json.loads(big_read.content[0].text)["result"]["datas"]) == ASSET_BYTES
    assert small.is_error is False and json.loads(small.content[0].text)["count"] == 3
