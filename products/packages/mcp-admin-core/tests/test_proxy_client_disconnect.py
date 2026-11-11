"""用戶端在 MCP 請求內容送完之前斷線（上傳中斷）：閘道記一行 warning、不轉發，不再拋成 ASGI 例外（第三輪 G5）。

正式站 145（n8n）三次、194（Odoo）兩次：Starlette 讀 body 時拋 ``ClientDisconnect``，一路傳到 uvicorn，記成
``ERROR: Exception in ASGI application`` 加 190 行 traceback，看起來像伺服器錯誤。現在 proxy 自己接住：一行 warning
（方法與 token 之後的路徑，不含 token、不帶 traceback），回 499（用戶端已經不在，只給 access log 看）。
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from mcp_admin_core.request_limits import McpRequestLimits

TOKEN = "disconnect-test-token-0123456789"


@pytest.fixture
def gateway(make_app, monkeypatch):
    from mcp_admin_core import proxy

    forwarded: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        forwarded.append(request)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

    monkeypatch.setattr(proxy, "_get_client",
                        lambda timeout=86400: httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    client = make_app({"mcp_auth_token": TOKEN})
    return client.app, forwarded


async def _upload_then_disconnect(app, path: str, *, declared: int = 4096) -> list[dict]:
    """送一小塊（宣告還有更多），接著用戶端斷線；回傳 app 送出的 ASGI 事件。"""
    chunks = [b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"x","arguments":{"blob":"']

    async def receive():
        await asyncio.sleep(0)
        if chunks:
            return {"type": "http.request", "body": chunks.pop(0), "more_body": True}
        return {"type": "http.disconnect"}

    sent: list[dict] = []

    async def send(event):
        sent.append(event)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "https",
             "path": path, "raw_path": path.encode(), "query_string": b"",
             "headers": [(b"content-type", b"application/json"), (b"content-length", str(declared).encode())],
             "client": ("127.0.0.1", 1), "server": ("testserver", 443)}
    await asyncio.wait_for(app(scope, receive, send), timeout=10)
    return sent


@pytest.mark.parametrize("product", ["odoo", "n8n"])
def test_upload_interrupted_by_the_client_is_one_warning_not_an_error(gateway, caplog, product):
    app, forwarded = gateway
    if product == "n8n":  # n8n 用自己的 McpRequestLimits.read_body（正式站 145）；其他產品用共用的 request_body（194）
        app.state.mcp_request_limits = McpRequestLimits(4 * 1024 * 1024, 10_000)
    caplog.set_level(logging.DEBUG)

    sent = asyncio.run(_upload_then_disconnect(app, f"/private_{TOKEN}/mcp"))

    assert not forwarded
    starts = [e for e in sent if e["type"] == "http.response.start"]
    assert [e["status"] for e in starts] == [499]
    warnings = [r for r in caplog.records if r.name == "mcp_admin_core.proxy" and r.levelno == logging.WARNING]
    assert len(warnings) == 1
    record = warnings[0]
    assert record.exc_info is None and record.exc_text is None  # 不帶 traceback
    message = record.getMessage()
    assert "POST /mcp" in message and "中斷連線" in message
    assert TOKEN not in message and "private_" not in message
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_complete_requests_are_unaffected(gateway):
    """回歸保護：完整送達的請求照常轉發。"""
    app, forwarded = gateway
    from fastapi.testclient import TestClient

    response = TestClient(app).post(f"/private_{TOKEN}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert response.status_code == 200 and len(forwarded) == 1
