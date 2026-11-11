"""雙 transport wrapper（SSE + StreamableHTTP）行為測試。

驗證核心新行為：裸 POST /mcp 直接 200（不 307 轉址）、回 mcp-session-id；
既有 SSE 路由仍註冊。用假的低階 Server，不連 odoo、不起真 server。
"""
from __future__ import annotations

import mcp.types as types
import pytest
from mcp.server.lowlevel import Server
from starlette.testclient import TestClient

from mcp_admin_core.mcp_sse_wrapper import DualTransportApp

_INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
               "clientInfo": {"name": "test", "version": "0"}},
}
_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _fake_server() -> Server:
    async def _list_tools(ctx, params):
        return types.ListToolsResult(tools=[types.Tool(name="search_records", description="d", input_schema={"type": "object"})])

    return Server("fake-odoo", on_list_tools=_list_tools)


@pytest.fixture()
def client():
    # TestClient context 會進入 app lifespan（啟動 StreamableHTTPSessionManager.run()）
    with TestClient(DualTransportApp(_fake_server())) as c:
        yield c


def test_bare_mcp_initialize_returns_200_with_session(client):
    r = client.post("/mcp", json=_INIT, headers=_HEADERS, follow_redirects=False)
    assert r.status_code == 200
    assert r.headers.get("mcp-session-id")
    # 核心契約：裸 /mcp 直接處理，不得 307 轉址（否則 Location 會洩漏 loopback upstream）。
    assert "location" not in r.headers
    assert not r.is_redirect


def test_mcp_trailing_slash_also_200(client):
    r = client.post("/mcp/", json=_INIT, headers=_HEADERS, follow_redirects=False)
    assert r.status_code == 200
    # 與裸 /mcp 對齊：確認真的走進 StreamableHTTP（回 session），而非只是裸 200。
    assert r.headers.get("mcp-session-id")


def test_mcp_prefix_boundary_not_hijacked(client):
    # __call__ 用 rstrip("/")=="/mcp" 精確比對；/mcpx、/mcp/extra 不得被劫進 StreamableHTTP
    # （若有人改成 startswith("/mcp") 這裡會抓到）。兩者皆落回 Starlette route → 404。
    assert client.post("/mcpx", json=_INIT, headers=_HEADERS, follow_redirects=False).status_code == 404
    assert client.post("/mcp/extra", json=_INIT, headers=_HEADERS, follow_redirects=False).status_code == 404


def test_mcp_get_without_session_returns_400(client):
    # GET /mcp 無 session-id：StreamableHTTP 應立即回 400（不會開串流、不會 hang）。
    r = client.get("/mcp", headers=_HEADERS, follow_redirects=False)
    assert r.status_code == 400


def test_mcp_delete_terminates_session(client):
    # 動詞覆蓋：__call__ 攔所有 method。走完整生命週期 initialize → DELETE 該 session。
    init = client.post("/mcp", json=_INIT, headers=_HEADERS, follow_redirects=False)
    sid = init.headers["mcp-session-id"]
    r = client.delete("/mcp", headers={**_HEADERS, "mcp-session-id": sid}, follow_redirects=False)
    assert r.status_code == 200


def test_sse_routes_registered():
    app = DualTransportApp(_fake_server())
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/sse" in paths
    assert "/messages" in paths
    # 注意：測試中切勿直接 GET /sse 或帶有效 session 的 GET /mcp——那會開無界 SSE 串流，
    # TestClient 會緩衝整段而卡死。此處只 introspect 路由註冊，不實際請求。


def test_unknown_path_returns_404(client):
    r = client.get("/nope", follow_redirects=False)
    assert r.status_code == 404


def test_stateless_mode_initialize_without_session_id():
    """stateless=True：/mcp 照常 200，但不發 Mcp-Session-Id（不留 per-session 狀態，#1369）。"""
    with TestClient(DualTransportApp(_fake_server(), stateless=True)) as c:
        r = c.post("/mcp", json=_INIT, headers=_HEADERS)
        assert r.status_code == 200
        assert "mcp-session-id" not in {k.lower() for k in r.headers}
        # 不帶 session 也能直接 tools/list（每請求獨立 transport）
        r2 = c.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers=_HEADERS,
        )
        assert r2.status_code == 200
        assert "search_records" in r2.text
