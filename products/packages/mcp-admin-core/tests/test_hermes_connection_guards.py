"""hermes MCP server 連線守衛：「沒設定」要講成「沒設定」。

事故背景：`connection.dashboard_url` 為空字串時，`_get_dashboard_client()` 直接把 ""
餵給 httpx，MCP client 只看到 httpx 的
`Request URL is missing an 'http://' or 'https://' protocol.`
——這句把調查者帶去找不存在的 `HERMES_*_BACKEND_URL` 環境變數，整輪調查白做。

admin REST 面（routers/config.py 的 test_dashboard、routers/health.py、
routers/dashboard_proxy.py）早就會回「not configured」；本檔釘住 MCP 工具面
（hermes_mcp_server/server.py）也必須一樣講人話。
"""
from __future__ import annotations

import json

import pytest

from hermes_mcp_server import server as hermes_server
from hermes_mcp_server.server import ConnectionNotConfiguredError

# httpx 對「空字串 / 缺 scheme」的原始訊息——測試要證明使用者再也看不到它。
HTTPX_MISLEADING = "missing an 'http://' or 'https://' protocol"

_CONN_ENV = (
    "HERMES_GATEWAY_URL",
    "HERMES_GATEWAY_API_KEY",
    "HERMES_DASHBOARD_URL",
    "HERMES_DASHBOARD_USERNAME",
    "HERMES_DASHBOARD_PASSWORD",
)


@pytest.fixture
def blank_conn(monkeypatch):
    """把五個連線 env 都清成「租戶沒填」的狀態（＝事故現場）。"""
    for name in _CONN_ENV:
        monkeypatch.delenv(name, raising=False)
    hermes_server._cookie_cache.clear()
    yield
    hermes_server._cookie_cache.clear()


# ---------------------------------------------------------------------------
# Dashboard 側：空值 / 缺 scheme
# ---------------------------------------------------------------------------
async def test_dashboard_tool_says_not_configured_when_url_blank(blank_conn):
    with pytest.raises(ConnectionNotConfiguredError) as exc:
        await hermes_server.hermes_config(action="get")

    msg = str(exc.value)
    assert "dashboard_url is not configured" in msg
    assert "Connection > Dashboard REST API" in msg   # 指向可執行的動作
    assert HTTPX_MISLEADING not in msg                # 不再是那句誤導訊息


async def test_dashboard_tool_says_scheme_missing_when_url_has_no_scheme(blank_conn, monkeypatch):
    # 另一種會觸發同一句 httpx 訊息的形狀：有值但沒有 scheme
    monkeypatch.setenv("HERMES_DASHBOARD_URL", "hermes-agent-svc:9119")

    with pytest.raises(ConnectionNotConfiguredError) as exc:
        await hermes_server.hermes_skill(action="list")

    msg = str(exc.value)
    assert "dashboard_url" in msg
    assert "http:// or https://" in msg
    assert "hermes-agent-svc:9119" not in msg         # 公開錯誤不回顯設定網址
    assert "Connection > Dashboard REST API" in msg
    assert HTTPX_MISLEADING not in msg


async def test_dashboard_login_blank_credentials_hint(blank_conn, monkeypatch):
    """帳密沒設定而 dashboard 回 401 時，講「沒設定」而不是「認證失敗」。"""
    monkeypatch.setenv("HERMES_DASHBOARD_URL", "http://hermes-agent-svc:9119")
    monkeypatch.setattr(hermes_server.httpx, "AsyncClient", _fake_client_factory(401))

    with pytest.raises(ConnectionNotConfiguredError) as exc:
        await hermes_server.hermes_model(action="info")

    msg = str(exc.value)
    assert "dashboard_username" in msg and "dashboard_password" in msg
    assert "not configured" in msg


async def test_dashboard_login_401_with_credentials_set_is_untouched(blank_conn, monkeypatch):
    """帳密有設定卻被拒 → 維持原本的 HTTPStatusError（真的是認證失敗，不可誤報成沒設定）。"""
    monkeypatch.setenv("HERMES_DASHBOARD_URL", "http://hermes-agent-svc:9119")
    monkeypatch.setenv("HERMES_DASHBOARD_USERNAME", "admin")
    monkeypatch.setenv("HERMES_DASHBOARD_PASSWORD", "wrong-password")
    monkeypatch.setattr(hermes_server.httpx, "AsyncClient", _fake_client_factory(401))

    with pytest.raises(hermes_server.httpx.HTTPStatusError):
        await hermes_server.hermes_model(action="info")


# ---------------------------------------------------------------------------
# Gateway 側：兩邊一起改，否則下次換 gateway 那半誤導人
# ---------------------------------------------------------------------------
async def test_gateway_tool_says_not_configured_when_url_blank(blank_conn):
    with pytest.raises(ConnectionNotConfiguredError) as exc:
        await hermes_server.hermes_chat(message="hi")

    msg = str(exc.value)
    assert "gateway_url is not configured" in msg
    assert "Connection > Gateway API Server" in msg
    assert HTTPX_MISLEADING not in msg


async def test_gateway_401_with_blank_api_key_hint(blank_conn, monkeypatch):
    monkeypatch.setenv("HERMES_GATEWAY_URL", "http://hermes-agent-svc:8642")
    monkeypatch.setattr(hermes_server.httpx, "AsyncClient", _fake_client_factory(401))

    with pytest.raises(ConnectionNotConfiguredError) as exc:
        await hermes_server.hermes_chat(message="hi")

    assert "gateway_api_key is not configured" in str(exc.value)


# ---------------------------------------------------------------------------
# hermes_inspect：三個 *_error key 不再共用同一句誤導訊息
# ---------------------------------------------------------------------------
async def test_inspect_error_keys_are_actionable(blank_conn):
    # AI-HMCP-003-3：有 *_error 時回 CallToolResult(isError=True)，內容仍是同一份 JSON
    returned = await hermes_server.hermes_inspect(target="all")
    assert returned.is_error is True
    result = json.loads(returned.content[0].text)

    for key in ("capabilities_error", "config_error", "model_info_error", "status_error"):
        assert key in result, f"缺少 {key}"
        assert "not configured" in result[key], f"{key} 沒講「沒設定」：{result[key]}"
        assert HTTPX_MISLEADING not in result[key]

    assert "gateway_url" in result["capabilities_error"]
    assert "dashboard_url" in result["config_error"]


# ---------------------------------------------------------------------------
# 設定齊全時不得誤擋（守衛只在缺值時說話）
# ---------------------------------------------------------------------------
async def test_configured_urls_pass_through_and_are_normalised(blank_conn, monkeypatch):
    monkeypatch.setenv("HERMES_GATEWAY_URL", "  http://hermes-agent-svc:8642/  ")
    monkeypatch.setenv("HERMES_GATEWAY_API_KEY", "sk-test")

    seen: dict[str, object] = {}
    monkeypatch.setattr(hermes_server.httpx, "AsyncClient", _fake_client_factory(200, seen=seen))

    await hermes_server.hermes_chat(message="hi")

    assert seen["base_url"] == "http://hermes-agent-svc:8642"    # strip + 去尾斜線
    assert seen["headers"]["Authorization"] == "Bearer sk-test"


# ---------------------------------------------------------------------------
# 假 httpx client（只服務本檔需要的 post/get/stream）
# ---------------------------------------------------------------------------
def _fake_client_factory(status_code: int, seen: dict | None = None):
    import httpx

    class _FakeResponse:
        def __init__(self, url: str) -> None:
            self.status_code = status_code
            self.headers = httpx.Headers({} if status_code >= 400 else {
                "set-cookie": 'hermes_session_at="tok"; Path=/',
            })
            self._url = url

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError(
                    f"Client error '{self.status_code}' for url '{self._url}'",
                    request=httpx.Request("GET", self._url),
                    response=httpx.Response(self.status_code),
                )

        def json(self):
            return {"ok": True}

        async def aread(self):
            return b""

    class _FakeAsyncClient:
        def __init__(self, base_url: str = "", headers: dict | None = None, **kwargs) -> None:
            if seen is not None:
                seen["base_url"] = base_url
                seen["headers"] = headers or {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, path: str, **kwargs):
            return _FakeResponse(path)

        async def get(self, path: str, **kwargs):
            return _FakeResponse(path)

        def stream(self, method: str, path: str, **kwargs):
            # hermes_chat 走串流（AI-HMCP-001-6：逾時要能關連線中止那一輪）
            response = _FakeResponse(path)

            class _Stream:
                async def __aenter__(self):
                    return response

                async def __aexit__(self, *exc):
                    return False

            return _Stream()

    return _FakeAsyncClient
