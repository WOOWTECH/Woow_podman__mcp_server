"""Hermes MCP 安全缺口（商用驗收 AI-HMCP-001-1／-001-3／-002-1／-002-2）。

QA 服務 249 實測（2026-10-16）：

- 001-1 路徑穿越：工具把 session_id／job_id／name 用 f-string 拼進 dashboard 網址。httpx 會在送出前把
  ``..`` 正規化掉，``hermes_session get session_id="../config/raw"`` 實際打到 ``GET /api/config/raw``，
  回傳的 yaml 含 provider api_key 明文。
- 001-3 deny-list 只做字串全等：``terminal``（父鍵整棵換成純量）、``Terminal.Backend``、前後空白、
  全形、零寬字元、``model``、``mcp_servers.x`` 都放行。
- 002-1 後台 gateway_url／dashboard_url 沒有 SSRF 檢查：儲存與 Test 一律接受 loopback、metadata、
  叢集內位址與 file://。
- 002-2 stdio 封鎖可繞過：後台 ``POST /api/mcp-servers/live`` 直通可新增 stdio command；
  ``hermes_mcp add`` 只擋小寫 ``stdio:``／``command:`` 前綴。

每個測試都確認「被拒時沒有任何請求送到 Hermes」，不是只看回傳文字。
"""
from __future__ import annotations

from management_client import revision_headers

import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_mcp_server import server as hermes_server
from mcp_admin_core.util import ssrf

# ---------------------------------------------------------------------------
# 共用：假 Hermes dashboard（記錄實際送出的 raw path）
# ---------------------------------------------------------------------------


class FakeDashboard:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, bytes]] = []
        self.config: dict = {"display": {"compact": False}, "max_live_sessions": 4}

    @staticmethod
    def _merge(base: dict, incoming: dict) -> None:
        # 比照 Hermes PUT /api/config 的深合併（#69 的 hermes_config set 寫完會 GET 讀回驗證）
        for k, v in incoming.items():
            if isinstance(v, dict) and isinstance(base.get(k), dict):
                FakeDashboard._merge(base[k], v)
            else:
                base[k] = v

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.raw_path.decode(), request.content))
        if request.url.path == "/api/config" and request.method == "GET":
            return httpx.Response(200, json=self.config)
        if request.url.path == "/api/config" and request.method == "PUT":
            self._merge(self.config, json.loads(request.content).get("config") or {})
        return httpx.Response(200, json={"ok": True})

    @property
    def paths(self) -> list[str]:
        return [path for _, path, _ in self.requests]


@pytest.fixture
def dash(monkeypatch):
    fake = FakeDashboard()

    async def fake_client(conn):
        return httpx.AsyncClient(base_url="http://dash.test", transport=httpx.MockTransport(fake.handler))

    monkeypatch.setattr(hermes_server, "_get_dashboard_client", fake_client)
    return fake


# 001-1 的實測值，加上其他會改變路徑的寫法
TRAVERSAL_IDS = [
    "..",
    ".",
    "../config/raw",
    "../status",
    "a/../../config/raw",
    "x/../../../api/config/raw",
    "a/b",
    "..%2fconfig%2fraw",
    "%2e%2e",
    "x?y=1",
    "x#frag",
    "a\\..\\config",
    "x\u200b",
    " lead",
    "",
]

VALID_IDS = ["20261016_193839_d56dfbc7", "4f7520a8-b5bc-4a89-b360-0046f2125c8c", "qa1011-hermes-g56-import-01",
             "ac584d0a4512"]


def _id_calls(value: str):
    """所有把 id／name 拼進 dashboard 網址的工具呼叫。"""
    return [
        lambda: hermes_server.hermes_session(action="get", session_id=value),
        lambda: hermes_server.hermes_session(action="delete", session_id=value),
        lambda: hermes_server.hermes_cron(action="update", job_id=value, prompt="p"),
        lambda: hermes_server.hermes_cron(action="delete", job_id=value),
        lambda: hermes_server.hermes_cron(action="trigger", job_id=value),
        lambda: hermes_server.hermes_cron(action="pause", job_id=value),
        lambda: hermes_server.hermes_cron(action="resume", job_id=value),
        lambda: hermes_server.hermes_webhook(action="delete", name=value),
        lambda: hermes_server.hermes_webhook(action="toggle", name=value),
        lambda: hermes_server.hermes_tools(action="enable", toolset=value),
        lambda: hermes_server.hermes_tools(action="disable", toolset=value),
        lambda: hermes_server.hermes_mcp(action="remove", name=value),
    ]


# ---------------------------------------------------------------------------
# AI-HMCP-001-1：id／name 路徑穿越
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [v for v in TRAVERSAL_IDS if v])
async def test_traversal_ids_never_reach_dashboard(dash, value):
    for call in _id_calls(value):
        with pytest.raises(ValueError):
            await call()
    assert dash.requests == [], dash.paths
    assert not any("/config" in p or "/status" in p for p in dash.paths)


async def test_session_get_config_raw_regression(dash):
    """001-1 原樣重現：修正前打到 /api/config/raw。"""
    with pytest.raises(ValueError, match="session_id"):
        await hermes_server.hermes_session(action="get", session_id="../config/raw")
    assert dash.requests == []


@pytest.mark.parametrize("value", VALID_IDS)
async def test_valid_ids_hit_exact_resource(dash, value):
    await hermes_server.hermes_session(action="get", session_id=value)
    await hermes_server.hermes_cron(action="trigger", job_id=value)
    await hermes_server.hermes_webhook(action="delete", name=value)
    await hermes_server.hermes_tools(action="enable", toolset=value)
    await hermes_server.hermes_mcp(action="remove", name=value)
    assert dash.paths == [
        f"/api/sessions/{value}",
        f"/api/cron/jobs/{value}/trigger",
        f"/api/webhooks/{value}",
        f"/api/tools/toolsets/{value}",
        f"/api/mcp/servers/{value}",
    ]


async def test_valid_name_with_space_is_percent_encoded(dash):
    await hermes_server.hermes_mcp(action="remove", name="my server")
    assert dash.paths == ["/api/mcp/servers/my%20server"]


# ---------------------------------------------------------------------------
# AI-HMCP-001-3：deny-list 正規化
# ---------------------------------------------------------------------------

# 001-3 實測放行的變體（38 個裡可重建的部分）＋本次加入清單的根鍵
DENIED_VARIANTS = [
    "terminal.backend",
    "Terminal.Backend",
    "TERMINAL.BACKEND",
    " terminal.backend ",
    "terminal.backend\t",
    "terminal..backend",
    "terminal.backend[0]",
    "terminal",
    "terminal.cwd",
    ".terminal",
    "terminal.",
    "ｔｅｒｍｉｎａｌ.backend",          # 全形（NFKC 後是 terminal.backend）
    "terminal\u200b.backend",           # 零寬空白
    "ter\u00admi\u00adnal",             # soft hyphen
    "secrets",
    "secrets.a",
    "Secrets.OPENROUTER_API_KEY",
    "api_server",
    "api_server.host",
    "API_SERVER.cors_origins",
    "dashboard",
    "dashboard.basic_auth",
    "dashboard.basic_auth.password",
    "model",
    "model.provider",
    "Model.Default",
    "mcp_servers",
    "mcp_servers.x",
    "mcp_servers.github.env",
    "security.tirith_path",
    "providers.openrouter",
    "custom_providers",
    "display.api_key",                  # 任一段是秘密鍵：不經 MCP 寫入憑證
    "agent.access_token",
    "_config_version",                  # 底線開頭的內部鍵
    "a b",
    "",
    "   ",
]


@pytest.mark.parametrize("key", DENIED_VARIANTS)
@pytest.mark.parametrize("dry_run", [True, False])
async def test_denied_config_keys_are_never_written(dash, key, dry_run):
    out = json.loads(await hermes_server.hermes_config(action="set", key=key, value='"local"', dry_run=dry_run))
    assert "error" in out, out
    assert out.get("dry_run") is not True
    assert not any(method == "PUT" for method, _, _ in dash.requests), dash.requests


@pytest.mark.parametrize("key", ["display.compact", "max_live_sessions", "streaming", "compression.enabled",
                                 "Display.Compact", "max_tokens"])
async def test_allowed_config_keys_still_work(dash, key):
    dry = json.loads(await hermes_server.hermes_config(action="set", key=key, value="true", dry_run=True))
    assert dry["dry_run"] is True
    assert dash.requests == []
    out = json.loads(await hermes_server.hermes_config(action="set", key=key, value="true"))
    assert out["ok"] is True
    # #69（AI-HMCP-001-4）：點號 key 拆成巢狀寫入，寫完 GET 讀回驗證
    assert [(m, p) for m, p, _ in dash.requests] == [("PUT", "/api/config"), ("GET", "/api/config")]
    nested: object = True
    for part in reversed(key.split(".")):
        nested = {part: nested}
    assert json.loads(dash.requests[0][2]) == {"config": nested}


def test_config_key_check_unit():
    from hermes_mcp_server import guards

    assert guards.config_key_denial("display.compact") is None
    for key in ("terminal", "Terminal.Backend", "ｍｏｄｅｌ", "mcp_servers.x", "x.y..z", "x.y[0]"):
        assert guards.config_key_denial(key), key
    # 父鍵不能被純量覆寫：denied 的任何祖先也不能寫（目前清單都是根鍵，祖先就是根本身）
    assert guards.config_key_denial("dashboard")


# ---------------------------------------------------------------------------
# AI-HMCP-002-2：hermes_mcp add 的 URL 驗證（scheme＋SSRF）
# ---------------------------------------------------------------------------

BLOCKED_MCP_URLS = [
    "stdio:/bin/true",
    "STDIO:/bin/true",
    " stdio:/bin/true",
    "\tStdIo:/bin/true",
    "command:/bin/true",
    "COMMAND:/bin/true",
    "file:///bin/true",
    "gopher://example.com/",
    "ftp://example.com/",
    "/bin/true",
    "example.com/mcp",
    "http://169.254.169.254/",
    "http://127.0.0.1:8000/mcp",
    "http://0.0.0.0:8000/mcp",
    "http://[::1]:8000/mcp",
    "http://10.43.0.1/",
    "http://localhost:8000/mcp",
    "http://user:pass@example.com/mcp",
    "http://2130706433/",                # 十進位 127.0.0.1
    "   ",                               # 空字串走既有的 "Both 'name' and 'url' required"
]


@pytest.mark.parametrize("url", BLOCKED_MCP_URLS)
async def test_mcp_add_rejects_non_http_and_internal_urls(dash, monkeypatch, url):
    monkeypatch.delenv("WEBHOOK_SECURITY_MODE", raising=False)
    with pytest.raises(ValueError):
        await hermes_server.hermes_mcp(action="add", name="qa", url=url)
    assert dash.requests == []


async def test_mcp_add_rejects_traversal_name(dash):
    with pytest.raises(ValueError):
        await hermes_server.hermes_mcp(action="add", name="../x", url="https://mcp.example.com/mcp")
    assert dash.requests == []


async def test_mcp_add_public_https_is_forwarded_stripped(dash, monkeypatch):
    async def public(url, mode=None):
        return ssrf.SsrfCheck(True, "", ["93.184.215.14"])

    monkeypatch.setattr(ssrf, "check_url", public)
    out = json.loads(await hermes_server.hermes_mcp(action="add", name="github", url="  https://mcp.example.com/mcp "))
    assert out["ok"] is True
    method, path, body = dash.requests[0]
    assert (method, path) == ("POST", "/api/mcp/servers")
    assert json.loads(body) == {"name": "github", "transport": "streamable-http", "url": "https://mcp.example.com/mcp"}


# ---------------------------------------------------------------------------
# AI-HMCP-002-1：後台 connection 的 SSRF 檢查（儲存＋Test）
# ---------------------------------------------------------------------------

INTERNAL_URLS = [
    "http://127.0.0.1:8000",
    "http://0.0.0.0:8000",
    "http://169.254.169.254",
    "http://10.43.0.1",
    "http://192.168.1.10:9119",
    "http://[::1]:8080",
    "http://2130706433",
    "http://0x7f000001",
    "http://localhost:8000",
    "http://user:pass@127.0.0.1",
    "http://metadata.google.internal",
    "file:///etc/passwd",
    "gopher://127.0.0.1:6379/_",
]


def _config_client():
    from hermes_mcp_admin.routers import config as hermes_config

    app = FastAPI()
    app.include_router(hermes_config.router)
    return TestClient(app)


@pytest.fixture
def no_outbound(monkeypatch):
    """被 SSRF 擋下時不能有任何連線嘗試。"""
    calls: list[str] = []
    real = httpx.AsyncClient

    def guard(*args, **kwargs):
        def refuse(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(599)

        kwargs["transport"] = httpx.MockTransport(refuse)
        return real(*args, **kwargs)

    from hermes_mcp_admin.routers import config as hermes_config

    monkeypatch.setattr(hermes_config.httpx, "AsyncClient", guard)
    return calls


@pytest.mark.parametrize("field", ["gateway_url", "dashboard_url"])
@pytest.mark.parametrize("url", INTERNAL_URLS)
def test_connection_save_rejects_internal_targets(store_env, monkeypatch, field, url):
    monkeypatch.delenv("WEBHOOK_SECURITY_MODE", raising=False)
    c = _config_client()
    r = c.put("/api/config/connection", json={field: url, "restart": False}, headers=revision_headers(c, None, read_path='/api/config'))
    assert r.status_code == 400, r.text
    assert "SSRF" in r.json()["detail"]
    assert c.get("/api/config").json()[field] == ""


def test_connection_save_allows_public_and_clearing(store_env, monkeypatch):
    async def public(url, mode=None):
        return ssrf.SsrfCheck(True, "", ["104.16.0.1"])

    monkeypatch.setattr(ssrf, "check_url", public)
    c = _config_client()
    r = c.put("/api/config/connection", json={"gateway_url": "https://svc.example.com/gateway",
                                                "dashboard_url": "https://svc.example.com", "restart": False}, headers=revision_headers(c, None, read_path='/api/config'))
    assert r.status_code == 200, r.text
    r = c.put("/api/config/connection", json={"gateway_url": "", "restart": False}, headers=revision_headers(c, None, read_path='/api/config'))
    assert r.status_code == 200, r.text
    body = c.get("/api/config").json()
    assert body["gateway_url"] == "" and body["dashboard_url"] == "https://svc.example.com"


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000", "http://169.254.169.254", "http://10.43.0.1"])
@pytest.mark.parametrize("which", ["gateway", "dashboard"])
def test_connection_test_refuses_stored_internal_target(store_env, no_outbound, monkeypatch, url, which):
    """升級前就存進 config.json 的內網網址：Test 也不能連。"""
    import asyncio

    monkeypatch.delenv("WEBHOOK_SECURITY_MODE", raising=False)
    asyncio.run(store_env.patch("connection", {f"{which}_url": url, "dashboard_username": "u", "dashboard_password": "p"}))
    r = _config_client().post(f"/api/config/test/{which}")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is False
    assert "SSRF" in body["message"]
    assert no_outbound == []


# ---------------------------------------------------------------------------
# AI-HMCP-002-2：後台 /api/mcp-servers/live 直通
# ---------------------------------------------------------------------------


@pytest.fixture
def proxy(store_env, monkeypatch):
    from hermes_mcp_admin.routers import dashboard_proxy

    fake = FakeDashboard()
    real = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake.handler)
        return real(*args, **kwargs)

    async def cookie(conn):
        return "c"

    monkeypatch.setattr(dashboard_proxy.httpx, "AsyncClient", client)
    monkeypatch.setattr(dashboard_proxy, "_dashboard_cookie", cookie)

    import asyncio

    asyncio.run(store_env.patch("connection", {"dashboard_url": "http://dash.test"}))
    app = FastAPI()
    app.include_router(dashboard_proxy.router)
    return TestClient(app), fake


STDIO_BODIES = [
    {"name": "qa", "transport": "stdio", "command": "/bin/true", "args": []},
    {"name": "qa", "transport": "STDIO", "command": "/bin/true"},
    {"name": "qa", "transport": " stdio ", "url": "https://mcp.example.com"},
    {"name": "qa", "command": "/bin/true"},
    {"name": "qa", "type": "stdio", "command": ["/bin/sh", "-c", "id"]},
    {"name": "qa", "args": ["-c", "id"], "url": "https://mcp.example.com"},
    {"name": "qa", "transport": "streamable-http", "url": "file:///bin/true"},
    {"name": "qa", "transport": "streamable-http", "url": "STDIO:/bin/true"},
    {"name": "qa", "transport": "streamable-http", "url": "http://127.0.0.1:8000/mcp"},
    {"name": "qa", "transport": "streamable-http"},
    {"name": "../x", "transport": "streamable-http", "url": "https://mcp.example.com"},
    ["not", "an", "object"],
]


@pytest.mark.parametrize("body", STDIO_BODIES)
def test_live_add_rejects_stdio_and_bad_urls(proxy, monkeypatch, body):
    monkeypatch.delenv("WEBHOOK_SECURITY_MODE", raising=False)
    client, fake = proxy
    r = client.post("/api/mcp-servers/live", json=body)
    assert r.status_code == 400, r.text
    assert fake.requests == []


def test_live_add_http_server_is_forwarded(proxy, monkeypatch):
    async def public(url, mode=None):
        return ssrf.SsrfCheck(True, "", ["104.16.0.1"])

    monkeypatch.setattr(ssrf, "check_url", public)
    client, fake = proxy
    body = {"name": "github", "transport": "streamable-http", "url": "https://mcp.example.com/mcp"}
    r = client.post("/api/mcp-servers/live", json=body)
    assert r.status_code == 200, r.text
    assert [(m, p) for m, p, _ in fake.requests] == [("POST", "/api/mcp/servers")]
    assert json.loads(fake.requests[0][2]) == body


# httpx（TestClient）會在送出前把字面 ``..`` 正規化掉，所以用 %2E%2E：Starlette 解碼後路徑參數就是 ".."，
# 與 curl --path-as-is 送字面 ``..`` 到後台的效果相同。修正前 handler 會把它拼成 /api/sessions/.. → DELETE /api。
@pytest.mark.parametrize("method,path", [
    ("delete", "/api/sessions/%2E%2E"),
    ("delete", "/api/webhooks/%2E%2E"),
    ("put", "/api/webhooks/%2E%2E/enabled"),
    ("put", "/api/cron/jobs/%2E%2E"),
    ("delete", "/api/cron/jobs/%2E%2E"),
    ("post", "/api/cron/jobs/%2E%2E/trigger"),
    ("post", "/api/cron/jobs/%2E%2E/pause"),
    ("post", "/api/cron/jobs/%2E%2E/resume"),
    ("put", "/api/toolsets/%2E%2E"),
    ("delete", "/api/mcp-servers/live/%2E%2E"),
    ("put", "/api/mcp-servers/live/%2E%2E/enabled"),
    ("post", "/api/mcp-servers/live/%2E%2E/test"),
])
def test_proxy_dotdot_path_params_are_400(proxy, method, path):
    client, fake = proxy
    kwargs = {} if method == "delete" else {"json": {}}
    r = getattr(client, method)(path, **kwargs)
    assert r.status_code == 400, r.text
    assert fake.requests == []


def test_proxy_bulk_delete_skips_bad_ids(proxy):
    client, fake = proxy
    r = client.post("/api/sessions/bulk-delete", json={"session_ids": ["..", "../config/raw", "20261016_193839_d56dfbc7"]})
    assert r.status_code == 200
    assert fake.paths == ["/api/sessions/20261016_193839_d56dfbc7"]
    assert r.json()["deleted"] == 1


def test_proxy_skill_content_name_is_query_encoded(proxy):
    client, fake = proxy
    client.get("/api/skills/content", params={"name": "a&name=b"})
    assert fake.paths == ["/api/skills/content?name=a%26name%3Db"]
