"""MCP SDK 2.x 下 odoo 子行程的並行語意（#78），在「不 import fastmcp」的子行程裡量測。

mcp 2.x 把同步工具一律丟進 anyio 工作緒並行跑，``ToolAnnotations`` 的欄位也改成 snake_case。
``odoo_mcp_admin/concurrency.py`` 因此要做兩件事：唯讀工具受 ``ODOO_MCP_READ_THREADS`` 限制、寫入類維持
在事件迴圈上序列執行（評估報告 §0.2-C：未修時 20 個並發 execute_method 在假 Odoo 端最大並行 RPC 由 1 變 20，
``ODOO_MCP_READ_THREADS=2`` 也失效成 20）。

為什麼要子行程、而且擋掉 fastmcp：fastmcp 4 被 import 時會替 MCP SDK 的型別補回 camelCase 屬性
（``readOnlyHint``…）。CI 的 workspace venv 同時裝了 fastmcp，同一個 pytest 行程只要有任何測試 import 過它，
讀 camelCase 的程式就「看起來正常」；正式的 odoo 映像只裝 odoo app 的相依（沒有 fastmcp），同一段程式會靜默失效。
這裡的子行程先裝一個拒絕 ``fastmcp`` import 的 finder，重現正式映像的條件，再走真正的 ``launcher.main()``。
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import textwrap
import threading
import time
import xmlrpc.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("odoo_mcp")
pytest.importorskip("odoo_mcp_admin")

#: 子行程開頭：拒絕 fastmcp（正式 odoo 映像沒有裝它），之後才 import 任何東西。
_NO_FASTMCP = textwrap.dedent("""
    import sys

    class _NoFastMCP:
        def find_spec(self, name, path=None, target=None):
            if name == "fastmcp" or name.startswith("fastmcp."):
                raise ImportError("fastmcp is not installed in the odoo image: " + name)
            return None

    sys.meta_path.insert(0, _NoFastMCP())
""")

_ENV = {"ODOO_DB": "d", "ODOO_USERNAME": "u", "ODOO_PASSWORD": "p", "ODOO_MCP_RETRY_ATTEMPTS": "0"}


def _python(code: str, env: dict[str, str], **kwargs) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", _NO_FASTMCP + textwrap.dedent(code)],
                            env={**os.environ, **_ENV, **env}, **kwargs)


def test_launcher_lanes_without_fastmcp(tmp_path):
    """正式映像條件下，launcher 把 38 支唯讀工具放工作緒、2 支同步寫入工具釘在事件迴圈上。"""
    code = """
        import json, sys
        import odoo_mcp.__main__ as upstream
        upstream.main = lambda: 0          # 只套修補、不啟動 server
        from odoo_mcp_admin import concurrency, launcher
        launcher.main()
        from odoo_mcp.server import mcp
        tools = mcp._tool_manager.list_tools()
        print(json.dumps({
            "fastmcp_loaded": sorted(m for m in sys.modules if m == "fastmcp" or m.startswith("fastmcp.")),
            "read_only": sorted(t.name for t in tools if t.annotations is not None and t.annotations.read_only_hint is True),
            "camel_case_visible": any(hasattr(t.annotations, "readOnlyHint") for t in tools if t.annotations is not None),
            "thread": concurrency.tools_in_lane(mcp, "thread"),
            "event_loop": concurrency.tools_in_lane(mcp, "event_loop"),
            "not_async": sorted(t.name for t in tools if not t.is_async),
        }))
    """
    proc = _python(code, {"ODOO_URL": "http://127.0.0.1:9", "ODOO_MCP_LOG_FILE": str(tmp_path / "mcp.log")},
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = proc.communicate(timeout=120)
    assert proc.returncode == 0, err[-2000:]
    lanes = json.loads(out.strip().splitlines()[-1])

    assert lanes["fastmcp_loaded"] == []
    assert lanes["camel_case_visible"] is False, "前提：沒有 fastmcp 時 SDK 型別上沒有 readOnlyHint"
    assert len(lanes["read_only"]) == 38 and {"search_records", "read_record", "get_model_fields"} <= set(lanes["read_only"])
    assert lanes["thread"] == lanes["read_only"], "ODOO_MCP_READ_THREADS 要涵蓋每一支唯讀工具"
    assert lanes["event_loop"] == ["chatter_post", "execute_method"], "同步的寫入類要釘在事件迴圈上序列執行"
    assert lanes["not_async"] == [], "每支同步工具都要被分到其中一條路，不能落回 SDK 預設的工作緒池"


# ---- 端到端：真的 launcher 子行程＋會計數並行 RPC 的假 Odoo --------------------------------------

RPC_DELAY = 0.15


class _CountingOdoo(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _CountingHandler)
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def reset_peak(self) -> None:
        with self.lock:
            self.peak = 0


class _CountingHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802 — http.server API
        server: _CountingOdoo = self.server  # type: ignore[assignment]
        params, method = xmlrpc.client.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        if method == "version":
            value = {"server_version": "18.0", "server_version_info": [18, 0, 0, "final", 0]}
        elif method == "authenticate":
            value = 2
        else:
            with server.lock:
                server.active += 1
                server.peak = max(server.peak, server.active)
            time.sleep(RPC_DELAY)
            with server.lock:
                server.active -= 1
            odoo_method = params[4] if len(params) > 4 else ""
            if odoo_method == "fields_get":
                value = {"name": {"type": "char", "string": "Name", "store": True}}
            elif odoo_method == "search_count":
                value = 1
            else:
                value = [{"id": 1, "name": "x"}]
        data = xmlrpc.client.dumps((value,), methodresponse=True, allow_none=True).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def odoo_child(tmp_path):
    odoo = _CountingOdoo()
    threading.Thread(target=odoo.serve_forever, daemon=True).start()
    port = _free_port()
    code = f"""
        import sys
        sys.argv = ["woow-odoo-mcp", "--transport", "streamable-http", "--host", "127.0.0.1", "--port", "{port}"]
        from odoo_mcp_admin.launcher import main
        main()
    """
    env = {
        "ODOO_URL": f"http://127.0.0.1:{odoo.server_address[1]}",
        "ODOO_MCP_ENABLE_WRITES": "1", "ODOO_MCP_ALLOW_UNKNOWN_METHODS": "1",
        "ODOO_MCP_READ_THREADS": "2", "ODOO_MCP_LOG_FILE": str(tmp_path / "mcp.log"),
    }
    proc = _python(code, env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        yield odoo, f"http://127.0.0.1:{port}/mcp"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        odoo.shutdown()


async def _session(client, url: str) -> dict[str, str]:
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    for _ in range(200):
        try:
            response = await client.post(url, headers=headers, json={
                "jsonrpc": "2.0", "id": 0, "method": "initialize",
                "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}})
            break
        except Exception:  # noqa: BLE001 — 子行程還在開機
            await asyncio.sleep(0.1)
    else:
        raise RuntimeError("woow-odoo-mcp did not start")
    assert response.status_code == 200, response.text
    headers["Mcp-Session-Id"] = response.headers["mcp-session-id"]
    headers["MCP-Protocol-Version"] = "2025-11-25"
    await client.post(url, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    return headers


def _tool_result(response) -> dict:
    data = [line[5:].strip() for line in response.text.splitlines() if line.startswith("data:")]
    body = json.loads(data[-1]) if data else response.json()
    return body["result"]


async def _concurrent_calls(client, url, headers, name: str, arguments: dict, n: int) -> list[dict]:
    async def one(i: int) -> dict:
        response = await client.post(url, headers=headers, json={
            "jsonrpc": "2.0", "id": 100 + i, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
        return _tool_result(response)

    return await asyncio.gather(*(one(i) for i in range(n)))


def test_writes_stay_serialized_and_reads_respect_read_threads(odoo_child):
    httpx = pytest.importorskip("httpx")
    odoo, url = odoo_child
    write = {"model": "res.partner", "method": "search_read", "args": [[]], "kwargs": {"limit": 1}}
    read = {"model": "res.partner", "fields": ["name"], "limit": 1}

    async def scenario():
        async with httpx.AsyncClient(timeout=60) as client:
            headers = await _session(client, url)
            warm = await _concurrent_calls(client, url, headers, "search_records", read, 1)  # 第一次連線／登入
            assert warm[0].get("isError") is not True, warm
            odoo.reset_peak()
            writes = await _concurrent_calls(client, url, headers, "execute_method", write, 20)
            write_peak = odoo.peak
            odoo.reset_peak()
            reads = await _concurrent_calls(client, url, headers, "search_records", read, 20)
            return writes, write_peak, reads, odoo.peak

    writes, write_peak, reads, read_peak = asyncio.run(asyncio.wait_for(scenario(), timeout=120))

    assert all(r.get("isError") is not True for r in writes + reads), [r for r in writes + reads if r.get("isError")][:1]
    assert write_peak == 1, f"寫入類必須序列執行（mcp 1.x 語意），實際最大並行 RPC = {write_peak}"
    assert read_peak == 2, f"唯讀工具要並行、但不超過 ODOO_MCP_READ_THREADS=2，實際最大並行 RPC = {read_peak}"
