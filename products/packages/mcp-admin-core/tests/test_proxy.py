"""MCP reverse-proxy token 驗證回歸測試。

只測「token 驗證」這段確定性行為：錯誤 token → 403；正確 token 通過驗證後
才嘗試連上游（測試環境無上游、子行程也沒在跑 → 503），藉此證明 token 正確時確實放行。
"""

from __future__ import annotations

# 指向幾乎不可能有人監聽的埠，讓「正確 token」案例必定走到 ConnectError
# （子行程未運行 → 503＋可讀訊息；運行中但連不上 → 502，見 #1350）
_DEAD_PORT = 59999


def test_proxy_rejects_when_no_token_configured(make_app):
    c = make_app()  # 預設 mcp_auth_token = "" → 任何 token 皆拒絕
    assert c.get("/private_anything/health").status_code == 403


def test_proxy_rejects_wrong_token(make_app):
    c = make_app({"mcp_auth_token": "secret"})
    assert c.get("/private_wrong/health").status_code == 403


def test_proxy_accepts_correct_token_then_upstream_down(make_app):
    c = make_app(
        {
            "mcp_auth_token": "secret",
            "mcp_server": {"command": "", "args": [], "port": _DEAD_PORT, "env": {}},
        }
    )
    # token 正確 → 通過驗證 → 嘗試連 127.0.0.1:59999（無上游、子行程未運行）→ 503
    r = c.get("/private_secret/health")
    assert r.status_code == 503
    assert "not running" in r.json()["detail"]
    assert "管理頁" in r.json()["detail"]


def test_proxy_upstream_down_while_process_running_is_502(make_app, monkeypatch):
    """子行程在跑但連不上（例如啟動窗口）→ 維持 502，不誤報「未運行」。"""
    import mcp_admin_core.proxy as proxy_mod

    class _RunningPM:
        is_running = True

    monkeypatch.setattr(proxy_mod, "get_process_manager", lambda: _RunningPM())
    c = make_app(
        {
            "mcp_auth_token": "secret",
            "mcp_server": {"command": "", "args": [], "port": _DEAD_PORT, "env": {}},
        }
    )
    r = c.get("/private_secret/health")
    assert r.status_code == 502


def test_proxy_forwards_loopback_host_with_port(make_app, monkeypatch):
    """轉發給子行程的 Host header 必須帶 port（``127.0.0.1:{mcp_port}``）。

    FastMCP 子行程的 TransportSecurityMiddleware（DNS-rebinding 保護）只收
    ``127.0.0.1:*`` / ``localhost:*``（pattern 需帶 port）；裸 ``localhost`` 會被回
    421 ``Invalid Host header``（pilot 端到端實測踩到，單元 mock 抓不到）。
    此測試攔截 proxy 的 httpx client、斷言轉發的 Host 帶 port。
    """
    import httpx

    import mcp_admin_core.proxy as proxy_mod

    captured: dict = {}

    class _StubClient:
        def build_request(self, *, method, url, headers, content):
            captured["headers"] = headers
            return object()

        async def send(self, request, *, stream):  # noqa: ARG002
            raise httpx.ConnectError("no upstream in test")

    monkeypatch.setattr(proxy_mod, "_get_client", lambda timeout=86400: _StubClient())

    c = make_app(
        {
            "mcp_auth_token": "secret",
            "mcp_server": {"command": "", "args": [], "port": 8123, "env": {}},
        }
    )
    r = c.get("/private_secret/sse")
    assert r.status_code == 503  # 通過 token 驗證、攔截 build_request 後 send→ConnectError（子行程未運行）→503
    assert captured["headers"]["host"] == "127.0.0.1:8123"


def test_proxy_drops_external_origin(make_app, monkeypatch):
    """轉發給子行程的請求必須 drop 外部 Origin header（Host 修正的對稱另一半）。

    FastMCP 子行程的 TransportSecurityMiddleware 同時驗 Origin：loopback binding 自動
    開 ``allowed_origins``（``http://127.0.0.1:*`` / ``http://localhost:*`` /
    ``http://[::1]:*``），其他 Origin（瀏覽器型 web MCP client 送的
    ``https://ws-xxx.woowtech.io``）→ 403 ``Invalid Origin header``（與 Host 的 421 分開）；
    缺 Origin 則短路放行（``transport_security.py:_validate_origin`` ``if not origin: return True``）。
    proxy 反代 = 受信任 loopback client，外部 Origin 對子行程無意義，必須 drop。
    """
    import httpx

    import mcp_admin_core.proxy as proxy_mod

    captured: dict = {}

    class _StubClient:
        def build_request(self, *, method, url, headers, content):
            captured["headers"] = headers
            return object()

        async def send(self, request, *, stream):  # noqa: ARG002
            raise httpx.ConnectError("no upstream in test")

    monkeypatch.setattr(proxy_mod, "_get_client", lambda timeout=86400: _StubClient())

    c = make_app(
        {
            "mcp_auth_token": "secret",
            "mcp_server": {"command": "", "args": [], "port": 8123, "env": {}},
        }
    )
    r = c.get("/private_secret/sse", headers={"Origin": "https://ws-xxx.woowtech.io"})
    assert r.status_code == 503
    # 外部 Origin 必須被 drop（不透傳給子行程，否則 FastMCP 回 403）
    assert "origin" not in {k.lower() for k in captured["headers"]}
    # Host 仍正規化帶 port（不回歸）
    assert captured["headers"]["host"] == "127.0.0.1:8123"


def _stub_slow_upstream(monkeypatch, delay: float):
    import asyncio

    import mcp_admin_core.proxy as proxy_mod

    class _StubClient:
        def build_request(self, *, method, url, headers, content):
            return object()

        async def send(self, request, *, stream):  # noqa: ARG002
            await asyncio.sleep(delay)
            raise AssertionError("should have been cancelled by the gateway deadline")

    monkeypatch.setattr(proxy_mod, "_get_client", lambda timeout=86400: _StubClient())
    return proxy_mod


def test_slow_tool_call_gets_jsonrpc_error_before_cloudflare_524(make_app, monkeypatch):
    """DATA-OMCP-007-3：同步 tools/call 超過閘道期限 → 回可讀的 JSON-RPC 錯誤，而不是等 CF 的 524。"""
    proxy_mod = _stub_slow_upstream(monkeypatch, delay=5)
    monkeypatch.setattr(proxy_mod, "TOOL_CALL_TIMEOUT", 0.2)
    c = make_app({"mcp_auth_token": "secret", "mcp_server": {"command": "", "args": [], "port": 8123, "env": {}}})
    r = c.post(
        "/private_secret/mcp",
        json={"jsonrpc": "2.0", "id": 7, "method": "tools/call",
              "params": {"name": "search_records", "arguments": {"model": "ir.attachment"}}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == 7
    assert body["error"]["code"] == proxy_mod.TOOL_TIMEOUT_ERROR_CODE
    assert "limit" in body["error"]["message"] and "datas" in body["error"]["message"]


def test_non_tool_calls_are_not_subject_to_the_tool_deadline(make_app, monkeypatch):
    """tools/list、initialize、長連線 GET /sse 不套用工具期限（只有 tools/call 才套）。"""
    import httpx

    import mcp_admin_core.proxy as proxy_mod

    class _StubClient:
        def build_request(self, *, method, url, headers, content):
            return object()

        async def send(self, request, *, stream):  # noqa: ARG002
            raise httpx.ConnectError("no upstream in test")

    monkeypatch.setattr(proxy_mod, "_get_client", lambda timeout=86400: _StubClient())
    monkeypatch.setattr(proxy_mod, "TOOL_CALL_TIMEOUT", 0.0001)
    c = make_app({"mcp_auth_token": "secret", "mcp_server": {"command": "", "args": [], "port": 8123, "env": {}}})
    r = c.post("/private_secret/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    # 走到上游（ConnectError、子行程沒在跑 → 閘道的佔位 MCP 回 setup_status），而不是被當成工具逾時
    assert r.status_code == 200
    assert [tool["name"] for tool in r.json()["result"]["tools"]] == ["setup_status"]
    assert c.get("/private_secret/sse").status_code == 503
