"""Hermes MCP 工具失敗要回 isError=true，訊息要講人話（商用驗收 AI-HMCP-003-3）。

事故背景（Hermes MCP 249 → Hermes 247，dashboard 停掉、Pod 不動）：

- ``hermes_inspect`` 把每段例外存成 ``status_error``／``config_error``… 再 ``return json.dumps(...)``，
  MCP 結果 ``isError=false``——依賴 isError 的 client（Claude Code）看不到錯誤旗標；其他 Hermes 工具
  則是例外直接拋出、``isError=true``。同一個故障兩種回報方式。
- 兩邊的文字都是 httpx 原文（``Server error '502 Bad Gateway' for url …``＋developer.mozilla.org 連結），
  沒說是 Hermes dashboard 不可達，也沒有建議動作。
- 後台「測試連線」（POST /api/config/test/dashboard）把 Cloudflare 502 整頁 HTML 前 200 字塞進 message。

本檔走真的 MCP 協定（in-memory client session → MCPServer → lowlevel server），檢查的是 client 實際拿到的
``CallToolResult``，不是工具函式的回傳值。MCP SDK 2.x 會把非 ``ToolError`` 的例外遮成
``Error executing tool <name>``，server.py 以 ``keep_error_text`` 換回 1.x 的完整文字（#78），所以這裡斷言的是
實際的錯誤訊息，不只是 isError。
"""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hermes_mcp_server import server as hermes_server
from management_client import revision_headers
from mcp.client import Client

GATEWAY = "http://hermes-agent-svc:8642"
DASHBOARD = "http://hermes-agent-svc:9119"

# Cloudflare 502 頁（節錄 247 實測的開頭，含 IE 條件註解）
CF_502_HTML = (
    "<!DOCTYPE html>\n<!--[if lt IE 7]> <html class=\"no-js ie6 oldie\" lang=\"en-US\"> <![endif]-->\n"
    "<!--[if IE 7]>    <html class=\"no-js ie7 oldie\" lang=\"en-US\"> <![endif]-->\n"
    "<head><title>hermes.example | 502: Bad gateway</title></head><body>Bad gateway Error code 502</body></html>"
)

# 使用者不該再看到的東西：httpx 原文的 MDN 連結、HTML
RAW_HTTPX_MARKERS = ("developer.mozilla.org", "<!DOCTYPE", "<html", "<!--[if")

_RealAsyncClient = httpx.AsyncClient


def _install_backend(monkeypatch, *, gateway=None, dashboard=None, login=None):
    """把 httpx.AsyncClient 接到 MockTransport；gateway／dashboard／login 是 (status, body) 或例外。

    預設：gateway 200、dashboard 200、login 200＋session cookie。
    """
    gateway = gateway or (200, {"version": "0.21.6"})
    dashboard = dashboard or (200, {"version": "0.21.6", "gateway_running": True, "active_sessions": 1,
                                    "model": "m", "toolsets": [], "max_live_sessions": 4})
    login = login or (200, {"ok": True})

    def respond(spec, request):
        if isinstance(spec, Exception):
            raise spec
        status, body = spec
        headers = {}
        if request.url.path == "/auth/password-login" and status == 200:
            headers["set-cookie"] = 'hermes_session_at="tok"; Path=/'
        if isinstance(body, str):
            return httpx.Response(status, text=body, headers={**headers, "content-type": "text/html"})
        return httpx.Response(status, json=body, headers=headers)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.port == 8642:
            return respond(gateway, request)
        if request.url.path == "/auth/password-login":
            return respond(login, request)
        return respond(dashboard, request)

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _RealAsyncClient(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("HERMES_GATEWAY_URL", GATEWAY)
    monkeypatch.setenv("HERMES_GATEWAY_API_KEY", "gw-key")
    monkeypatch.setenv("HERMES_DASHBOARD_URL", DASHBOARD)
    monkeypatch.setenv("HERMES_DASHBOARD_USERNAME", "admin")
    monkeypatch.setenv("HERMES_DASHBOARD_PASSWORD", "dash-pw")
    hermes_server._cookie_cache.clear()
    yield
    hermes_server._cookie_cache.clear()


async def _call(tool: str, args: dict):
    async with Client(hermes_server.mcp, mode="legacy") as client:
        return await client.call_tool(tool, args)


def _text(result) -> str:
    return "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")


def _assert_no_raw_httpx(text: str) -> None:
    for marker in RAW_HTTPX_MARKERS:
        assert marker not in text, f"仍夾帶原始錯誤／HTML（{marker}）：{text[:300]}"


# ---------------------------------------------------------------------------
# hermes_inspect：任何一段失敗都要 isError=true
# ---------------------------------------------------------------------------
async def test_inspect_dashboard_502_is_error_with_actionable_message(conn, monkeypatch):
    _install_backend(monkeypatch, dashboard=(502, CF_502_HTML), login=(502, CF_502_HTML))

    result = await _call("hermes_inspect", {"target": "status"})

    assert result.is_error is True
    body = json.loads(_text(result))
    msg = body["status_error"]
    assert "BACKEND_UNAVAILABLE status=502: Hermes Dashboard 不可達" in msg
    assert DASHBOARD not in msg
    assert "Connection > Dashboard REST API" in msg      # 建議動作指到後台的哪一格
    _assert_no_raw_httpx(_text(result))


async def test_inspect_partial_failure_keeps_good_sections_but_flags_error(conn, monkeypatch):
    _install_backend(monkeypatch, dashboard=(502, CF_502_HTML), login=(502, CF_502_HTML))

    result = await _call("hermes_inspect", {"target": "all"})

    assert result.is_error is True
    body = json.loads(_text(result))
    assert body["capabilities"] == {"version": "0.21.6"}   # gateway 正常的那段照樣回
    for key in ("config_error", "model_info_error", "status_error"):
        assert "Hermes Dashboard" in body[key]
    assert "capabilities_error" not in body


async def test_inspect_gateway_rejects_key_says_credentials_rejected(conn, monkeypatch):
    _install_backend(monkeypatch, gateway=(401, {"error": {"message": "Invalid API key"}}))

    result = await _call("hermes_inspect", {"target": "capabilities"})

    assert result.is_error is True
    msg = json.loads(_text(result))["capabilities_error"]
    assert msg.startswith("BACKEND_AUTH_REJECTED status=401: Hermes Gateway 憑證被拒"), msg
    assert GATEWAY not in msg and "Invalid API key" not in msg
    assert "Connection > Gateway API Server" in msg


async def test_inspect_dashboard_unreachable_connect_error(conn, monkeypatch):
    _install_backend(monkeypatch, login=httpx.ConnectError("[Errno 111] Connection refused"),
                     dashboard=httpx.ConnectError("[Errno 111] Connection refused"))

    result = await _call("hermes_inspect", {"target": "config"})

    assert result.is_error is True
    msg = json.loads(_text(result))["config_error"]
    assert "BACKEND_UNAVAILABLE: Hermes Dashboard 不可達" in msg
    assert "Connection > Dashboard REST API" in msg
    assert DASHBOARD not in msg
    assert "Connection refused" not in msg


async def test_inspect_all_ok_is_not_error(conn, monkeypatch):
    _install_backend(monkeypatch)

    result = await _call("hermes_inspect", {"target": "all"})

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body["status"]["gateway_running"] is True
    assert not [k for k in body if k.endswith("_error")]


# ---------------------------------------------------------------------------
# 其他工具：本來就 isError=true，訊息要和 hermes_inspect 同一套說法
# ---------------------------------------------------------------------------
async def test_session_tool_502_message_is_actionable(conn, monkeypatch):
    _install_backend(monkeypatch, dashboard=(502, CF_502_HTML))

    result = await _call("hermes_session", {"action": "list"})

    assert result.is_error is True
    text = _text(result)
    assert "BACKEND_UNAVAILABLE status=502: Hermes Dashboard 不可達" in text
    assert DASHBOARD not in text
    _assert_no_raw_httpx(text)


async def test_chat_tool_gateway_timeout_message(conn, monkeypatch):
    _install_backend(monkeypatch, gateway=httpx.ReadTimeout("timed out"))

    result = await _call("hermes_chat", {"message": "hi"})

    assert result.is_error is True
    text = _text(result)
    assert text.endswith("BACKEND_TIMEOUT: Hermes Gateway timed out; check the Hermes service and "
                         "Connection > Gateway API Server in the MCP admin page."), text
    assert GATEWAY not in text


async def test_dashboard_http_error_hides_upstream_detail(conn, monkeypatch):
    _install_backend(monkeypatch, dashboard=(422, {"detail": "unknown toolset 'nope'"}))

    result = await _call("hermes_tools", {"action": "enable", "toolset": "nope"})

    assert result.is_error is True
    text = _text(result)
    assert "BACKEND_HTTP_ERROR status=422" in text
    assert "unknown toolset 'nope'" not in text and DASHBOARD not in text
    _assert_no_raw_httpx(text)


async def test_tool_schemas_unchanged_by_error_wrapper():
    """包一層錯誤轉換不能改到 tools/list 的參數 schema（client 端的呼叫方式不變）。"""
    async with Client(hermes_server.mcp, mode="legacy") as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools["hermes_cron"].input_schema["properties"]) == {
        "action", "name", "schedule", "prompt", "enabled", "job_id"}
    assert tools["hermes_inspect"].input_schema["properties"]["target"]["default"] == "all"
    assert tools["hermes_session"].description.startswith("Manage Hermes sessions")


# ---------------------------------------------------------------------------
# 後台「測試連線」：只回狀態碼與 detail，不夾 HTML
# ---------------------------------------------------------------------------
def _admin_client():
    from hermes_mcp_admin.routers import config as hermes_config
    app = FastAPI()
    app.include_router(hermes_config.router)
    return TestClient(app)


def _seed(client: TestClient) -> None:
    assert client.put("/api/config/connection", json={
        "gateway_url": GATEWAY, "gateway_api_key": "gw-key",
        "dashboard_url": DASHBOARD, "dashboard_username": "admin", "dashboard_password": "dash-pw",
        "restart": False,
    }, headers=revision_headers(client, None, read_path='/api/config')).status_code == 200


@pytest.mark.parametrize("endpoint,label", [("dashboard", "Hermes Dashboard"), ("gateway", "Hermes Gateway")])
def test_admin_connection_test_502_has_no_html(store_env, monkeypatch, endpoint, label):
    client = _admin_client()
    _seed(client)
    _install_backend(monkeypatch, gateway=(502, CF_502_HTML), dashboard=(502, CF_502_HTML),
                     login=(502, CF_502_HTML))

    body = client.post(f"/api/config/test/{endpoint}").json()

    assert body["success"] is False
    assert "HTTP 502" in body["message"] and label in body["message"]
    assert "<" not in body["message"]
    assert body["details"].get("status_code") == 502


def test_admin_dashboard_test_wrong_password_says_rejected(store_env, monkeypatch):
    client = _admin_client()
    _seed(client)
    _install_backend(monkeypatch, login=(401, {"detail": "Invalid credentials"}))

    body = client.post("/api/config/test/dashboard").json()

    assert body["success"] is False
    assert "憑證被拒" in body["message"] and "Invalid credentials" in body["message"]


# ---------------------------------------------------------------------------
# #78：MCP SDK 2.x 的例外遮蔽——每一種「丟例外給 client 看」的錯誤都要保留原本的文字
# ---------------------------------------------------------------------------


async def test_guard_error_text_reaches_the_client(conn, monkeypatch):
    """輸入被安全規則拒絕（guards.GuardError，ValueError）：client 看到原因，不是 "Error executing tool"。"""
    _install_backend(monkeypatch)
    result = await _call("hermes_session", {"action": "get", "session_id": "../config/raw"})
    assert result.is_error is True
    text = _text(result)
    assert text.startswith(
        "Error executing tool hermes_session: INVALID_INPUT: invalid session_id: only letters, digits"), text


async def test_not_found_and_not_configured_texts_reach_the_client(monkeypatch):
    """HermesNotFoundError（LookupError）與 ConnectionNotConfiguredError（RuntimeError）的指示性文字。"""
    for key in ("HERMES_GATEWAY_URL", "HERMES_GATEWAY_API_KEY", "HERMES_DASHBOARD_URL",
                "HERMES_DASHBOARD_USERNAME", "HERMES_DASHBOARD_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    result = await _call("hermes_chat", {"message": "hi"})
    assert result.is_error is True
    text = _text(result)
    assert text.startswith("Error executing tool hermes_chat: ") and text != "Error executing tool hermes_chat: "
    assert "Gateway" in text or "gateway" in text, text

