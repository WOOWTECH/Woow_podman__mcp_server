"""opendesign config router 契約測試：GET / PUT / POST test（反假 GREEN 誠實分流）。

連線兩欄皆非 secret：``od_api_base``（daemon URL）＋ ``od_chat_timeout``（選配、
字串化正數；空字串＝清除，子行程回退上游預設 300）。G2-B 已決不設 secret 欄。

POST /api/config/test 走 G2-A 定案的 k8s 式誠實分流（讀存檔值、不吃 body）：
- 子行程 running → 本地 MCP ``tools/call health`` 打通 supergateway→daemon 全鏈；
- 子行程未 running → 降級直打 daemon ``/api/health``，success 但誠實標「未端到端驗證」。

形比照 apps/vk 的 test_vk_config_router.py（bare FastAPI + store_env）＋ apps/k8s 的
test_config_router.py（分流語意）。
"""
from __future__ import annotations

from connection_forms import assert_unknown_field_rejected, gui_connection_fields, product_admin
from management_client import revision_headers

import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from opendesign_mcp_admin import health_probes
from opendesign_mcp_admin.health_probes import OD_HEALTH_PATH


def _client():
    from opendesign_mcp_admin.routers import config as od_config

    app = FastAPI()
    app.include_router(od_config.router)
    return TestClient(app)


def _patch_pm(monkeypatch, running: bool):
    from opendesign_mcp_admin.routers import config as od_config

    class _PM:
        is_running = running

        async def restart(self):  # pragma: no cover - 僅供 PUT restart 分支
            return True

    monkeypatch.setattr(od_config, "get_process_manager", lambda: _PM())


# ---------------------------------------------------------------------------
# GET /api/config
# ---------------------------------------------------------------------------


def test_get_config_returns_saved_values_unmasked(store_env):
    c = _client()
    c.put(
        "/api/config/connection",
        json={"od_api_base": "http://od:7457", "od_chat_timeout": "300", "restart": False},
     headers=revision_headers(c, None, read_path='/api/config'))
    response = c.get("/api/config")
    body = response.json()
    configuration = body.pop("configuration")
    assert response.headers["etag"] == '"' + configuration["revision"] + '"'
    # 非 secret 欄不遮罩；basic auth 未設（#1354）
    assert body == {
        "od_api_base": "http://od:7457",
        "od_chat_timeout": "300",
        "od_basic_user": "",
        "od_basic_password_masked": "(not set)",
    }


def test_get_config_defaults_blank_when_unset(store_env):
    c = _client()
    response = c.get("/api/config")
    body = response.json()
    configuration = body.pop("configuration")
    assert response.headers["etag"] == '"' + configuration["revision"] + '"'
    assert body == {
        "od_api_base": "",
        "od_chat_timeout": "",
        "od_basic_user": "",
        "od_basic_password_masked": "(not set)",
    }


# ---------------------------------------------------------------------------
# PUT /api/config/connection —— od_api_base
# ---------------------------------------------------------------------------


def test_put_strips_whitespace(store_env):
    c = _client()
    r = c.put("/api/config/connection", json={"od_api_base": "  http://od:7457  ", "restart": False}, headers=revision_headers(c, None, read_path='/api/config'))
    assert r.status_code == 200 and r.json()["success"] is True
    assert c.get("/api/config").json()["od_api_base"] == "http://od:7457"


def test_put_blank_url_clears_saved_value(store_env):
    # od_api_base 非 secret → 不受 drop_blank_secrets 保護，可被有意清空（比照 vk）
    c = _client()
    c.put("/api/config/connection", json={"od_api_base": "http://od:7457", "restart": False}, headers=revision_headers(c, None, read_path='/api/config'))
    c.put("/api/config/connection", json={"od_api_base": "", "restart": False}, headers=revision_headers(c, None, read_path='/api/config'))
    assert c.get("/api/config").json()["od_api_base"] == ""


# ---------------------------------------------------------------------------
# PUT /api/config/connection —— od_chat_timeout（驗證 / 清除 / 保留）
# ---------------------------------------------------------------------------


def test_put_chat_timeout_valid_saved(store_env):
    c = _client()
    r = c.put(
        "/api/config/connection",
        json={"od_api_base": "http://od:7457", "od_chat_timeout": "600", "restart": False},
     headers=revision_headers(c, None, read_path='/api/config'))
    assert r.status_code == 200
    assert c.get("/api/config").json()["od_chat_timeout"] == "600"


@pytest.mark.parametrize("bad", ["abc", "0", "-5", "nan", "inf"])
def test_put_chat_timeout_invalid_rejected(store_env, bad):
    # 非正有限數一律 400；空字串／純空白 strip 後＝空 → 走清除分支（另測），非 400
    c = _client()
    r = c.put(
        "/api/config/connection",
        json={"od_api_base": "http://od:7457", "od_chat_timeout": bad, "restart": False},
     headers=revision_headers(c, None, read_path='/api/config'))
    assert r.status_code in (400, 422)


def test_put_blank_chat_timeout_removes_key(store_env, tmp_config_path):
    # 空字串＝清除：須真「移除」該鍵，而非存 ""。存 "" 會令 _build_subprocess_env
    # 注入 OD_CHAT_TIMEOUT=""，vendored server 的 float("") 會 crash。
    c = _client()
    c.put(
        "/api/config/connection",
        json={"od_api_base": "http://od:7457", "od_chat_timeout": "600", "restart": False},
     headers=revision_headers(c, None, read_path='/api/config'))
    assert "od_chat_timeout" in json.loads(tmp_config_path.read_text())["connection"]

    c.put(
        "/api/config/connection",
        json={"od_api_base": "http://od:7457", "od_chat_timeout": "", "restart": False},
     headers=revision_headers(c, None, read_path='/api/config'))
    conn = json.loads(tmp_config_path.read_text())["connection"]
    assert "od_chat_timeout" not in conn  # 真移除、非存 ""
    assert conn["od_api_base"] == "http://od:7457"
    assert c.get("/api/config").json()["od_chat_timeout"] == ""  # GET 顯示空


def test_put_without_chat_timeout_preserves_existing(store_env, tmp_config_path):
    # 未帶 od_chat_timeout（None）→ 不動既有值（只更新 url）
    c = _client()
    c.put(
        "/api/config/connection",
        json={"od_api_base": "http://od:7457", "od_chat_timeout": "600", "restart": False},
     headers=revision_headers(c, None, read_path='/api/config'))
    c.put("/api/config/connection", json={"od_api_base": "http://od2:7457", "restart": False}, headers=revision_headers(c, None, read_path='/api/config'))
    conn = json.loads(tmp_config_path.read_text())["connection"]
    assert conn["od_chat_timeout"] == "600"
    assert conn["od_api_base"] == "http://od2:7457"


# ---------------------------------------------------------------------------
# PUT restart 語意（partial：restart 失敗不讓存檔失敗）
# ---------------------------------------------------------------------------


def test_put_restart_requested_but_pm_not_running_starts_it(store_env, monkeypatch):
    """#1350：子行程沒在跑 → 存檔後改為 start（舊版只回「已存檔、未重啟」）。"""
    from opendesign_mcp_admin.routers import config as od_config

    calls: list[str] = []

    class _PM:
        is_running = False

        async def start(self):
            calls.append("start")
            return True

    monkeypatch.setattr(od_config, "get_process_manager", lambda: _PM())
    c = _client()
    body = c.put(
        "/api/config/connection", json={"od_api_base": "http://od:7457", "restart": True}
    , headers=revision_headers(c, None, read_path='/api/config')).json()
    assert calls == ["start"]
    assert body["success"] is True and body["restarted"] is True
    assert "已啟動" in body["message"]


def test_put_restart_success(store_env, monkeypatch):
    from opendesign_mcp_admin.routers import config as od_config

    class _PM:
        is_running = True

        async def restart(self):
            return True

    monkeypatch.setattr(od_config, "get_process_manager", lambda: _PM())
    c = _client()
    body = c.put(
        "/api/config/connection", json={"od_api_base": "http://od:7457", "restart": True}
    , headers=revision_headers(c, None, read_path='/api/config')).json()
    assert body["success"] is True and body["restarted"] is True


def test_put_restart_failure_still_saves(store_env, monkeypatch):
    from opendesign_mcp_admin.routers import config as od_config

    class _PM:
        is_running = True

        async def restart(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(od_config, "get_process_manager", lambda: _PM())
    c = _client()
    body = c.put(
        "/api/config/connection", json={"od_api_base": "http://od:7457", "restart": True}
    , headers=revision_headers(c, None, read_path='/api/config')).json()
    assert body["success"] is True and body["restarted"] is False
    assert "重啟失敗" in body["message"]
    assert c.get("/api/config").json()["od_api_base"] == "http://od:7457"  # 已存檔


# ---------------------------------------------------------------------------
# POST /api/config/test —— 路徑一（子行程 running → MCP tools/call health）
# ---------------------------------------------------------------------------


def test_test_running_success(store_env, monkeypatch):
    from opendesign_mcp_admin import mcp_client

    _patch_pm(monkeypatch, True)

    async def _fake_call(port, tool, arguments, timeout=15.0, transport=None):
        assert tool == "health"
        return mcp_client.McpCallResult(ok=True, message='{"status":"ok"}')

    monkeypatch.setattr(mcp_client, "call_tool_via_mcp", _fake_call)
    c = _client()
    c.put("/api/config/connection", json={"od_api_base": "http://od:7457", "restart": False}, headers=revision_headers(c, None, read_path='/api/config'))
    body = c.post("/api/config/test").json()

    assert body["success"] is True
    assert "health" in body["message"]
    assert body["details"]["auth"] == "mcp_tools_call"


def test_test_running_tool_error(store_env, monkeypatch):
    from opendesign_mcp_admin import mcp_client

    _patch_pm(monkeypatch, True)

    async def _fake_call(port, tool, arguments, timeout=15.0, transport=None):
        return mcp_client.McpCallResult(ok=False, kind="tool_error", message="daemon 500")

    monkeypatch.setattr(mcp_client, "call_tool_via_mcp", _fake_call)
    c = _client()
    c.put("/api/config/connection", json={"od_api_base": "http://od:7457", "restart": False}, headers=revision_headers(c, None, read_path='/api/config'))
    body = c.post("/api/config/test").json()

    assert body["success"] is False
    assert "health" in body["message"]
    assert body["details"]["auth"] == "mcp_tools_call"


def test_test_running_timeout(store_env, monkeypatch):
    from opendesign_mcp_admin import mcp_client

    _patch_pm(monkeypatch, True)

    async def _fake_call(port, tool, arguments, timeout=15.0, transport=None):
        raise mcp_client.McpTimeout("timed out")

    monkeypatch.setattr(mcp_client, "call_tool_via_mcp", _fake_call)
    c = _client()
    c.put("/api/config/connection", json={"od_api_base": "http://od:7457", "restart": False}, headers=revision_headers(c, None, read_path='/api/config'))
    resp = c.post("/api/config/test")
    assert resp.status_code == 200  # 不冒成 500
    body = resp.json()
    assert body["success"] is False
    assert body["details"]["auth"] == "mcp_tools_call"


def test_test_running_unavailable(store_env, monkeypatch):
    from opendesign_mcp_admin import mcp_client

    _patch_pm(monkeypatch, True)

    async def _fake_call(port, tool, arguments, timeout=15.0, transport=None):
        raise mcp_client.McpUnavailable("connection refused")

    monkeypatch.setattr(mcp_client, "call_tool_via_mcp", _fake_call)
    c = _client()
    c.put("/api/config/connection", json={"od_api_base": "http://od:7457", "restart": False}, headers=revision_headers(c, None, read_path='/api/config'))
    resp = c.post("/api/config/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False
    assert "尚未就緒" in body["message"]
    assert body["details"]["auth"] == "mcp_tools_call"


def test_test_running_malformed(store_env, monkeypatch):
    from opendesign_mcp_admin import mcp_client

    _patch_pm(monkeypatch, True)

    async def _fake_call(port, tool, arguments, timeout=15.0, transport=None):
        raise mcp_client.McpClientError("回應非合法 JSON")

    monkeypatch.setattr(mcp_client, "call_tool_via_mcp", _fake_call)
    c = _client()
    c.put("/api/config/connection", json={"od_api_base": "http://od:7457", "restart": False}, headers=revision_headers(c, None, read_path='/api/config'))
    resp = c.post("/api/config/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False
    assert "無法解析" in body["message"]
    assert body["details"]["auth"] == "mcp_tools_call"


# ---------------------------------------------------------------------------
# POST /api/config/test —— 路徑二（子行程未 running → 降級 daemon 可達性）
#
# 沿用 vk 的 _FakeAsyncClient 模式。reachability 走 health_probes.probe_target（其內
# 部用 OD_HEALTH_PATH 單一常數）；patch health_probes.httpx.AsyncClient 即攔截其 GET。
# ---------------------------------------------------------------------------


class _FakeAsyncClient:
    calls: list[str] = []
    response: httpx.Response | None = None
    raise_exc: Exception | None = None

    def __init__(self, *args, **kwargs): ...
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kwargs):
        _FakeAsyncClient.calls.append(str(url))
        if _FakeAsyncClient.raise_exc is not None:
            raise _FakeAsyncClient.raise_exc
        return _FakeAsyncClient.response


@pytest.fixture
def fake_http(monkeypatch):
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.response = None
    _FakeAsyncClient.raise_exc = None
    monkeypatch.setattr(health_probes.httpx, "AsyncClient", _FakeAsyncClient)
    return _FakeAsyncClient


def _resp(status=200, json_body=None):
    req = httpx.Request("GET", "http://od:7457/api/health")
    return httpx.Response(status, request=req, json=json_body if json_body is not None else {"status": "ok"})


def test_test_not_running_degrades(store_env, monkeypatch, fake_http):
    _patch_pm(monkeypatch, False)
    fake_http.response = _resp(200)
    c = _client()
    c.put("/api/config/connection", json={"od_api_base": "http://od:7457/", "restart": False}, headers=revision_headers(c, None, read_path='/api/config'))
    body = c.post("/api/config/test").json()

    assert body["success"] is True  # daemon 可達＝連線目標有效（無獨立憑證需驗）
    assert "MCP 子行程未執行" in body["message"]
    assert "未端到端驗證" in body["message"]
    assert body["details"]["auth"] == "reachability_only"
    # 反假 GREEN：打的是 daemon {OD_HEALTH_PATH}（存檔值、尾斜線 rstrip），不是 supergateway :8000
    assert fake_http.calls == [f"http://od:7457{OD_HEALTH_PATH}"]
    assert all(":8000" not in u for u in fake_http.calls)


def test_test_not_running_unreachable(store_env, monkeypatch, fake_http):
    _patch_pm(monkeypatch, False)
    fake_http.raise_exc = httpx.ConnectError("refused")
    c = _client()
    c.put("/api/config/connection", json={"od_api_base": "http://od:7457", "restart": False}, headers=revision_headers(c, None, read_path='/api/config'))
    body = c.post("/api/config/test").json()

    assert body["success"] is False  # 不可達不誤綠
    assert body["details"]["auth"] == "reachability_only"


def test_test_not_running_unconfigured(store_env, monkeypatch, fake_http):
    _patch_pm(monkeypatch, False)
    c = _client()
    body = c.post("/api/config/test").json()

    assert body["success"] is False  # 未設定不誤綠
    assert fake_http.calls == []  # 未設定 → 不打 probe


def test_test_ignores_request_body(store_env, monkeypatch, fake_http):
    _patch_pm(monkeypatch, False)
    fake_http.response = _resp(200)
    c = _client()
    c.put("/api/config/connection", json={"od_api_base": "http://saved:7457", "restart": False}, headers=revision_headers(c, None, read_path='/api/config'))
    c.post("/api/config/test", json={"od_api_base": "http://evil:9"})  # body 必被忽略
    assert fake_http.calls == [f"http://saved:7457{OD_HEALTH_PATH}"]


# ---------------------------------------------------------------------------
# 沒宣告的欄位回 422、不默默忽略（以前回 200 什麼都沒存；MCP:Hermes 2026-11-05 同型）
# ---------------------------------------------------------------------------
SECRET = "od-canary-8b64c2-secret"


@pytest.fixture
def od_admin(tmp_config_path, monkeypatch):
    client, apply = product_admin("opendesign", tmp_config_path, monkeypatch)
    yield client, apply
    client.close()


def test_gui_payload_is_accepted(od_admin, tmp_config_path):
    """Connection 頁送四欄（Chat Timeout 是文字欄、送字串）、不送 restart。"""
    c, apply = od_admin
    gui = {"od_api_base": "http://open-design-svc:7457", "od_chat_timeout": "300",
           "od_basic_user": "admin", "od_basic_password": SECRET}
    assert set(gui) == gui_connection_fields("opendesign")
    r = c.put("/api/config/connection", json=gui, headers=revision_headers(c, None, read_path="/api/config"))
    assert r.status_code == 200, r.text
    assert json.loads(tmp_config_path.read_text())["connection"] == gui
    apply.assert_awaited_once()


def test_misspelled_password_field_is_rejected_without_echo_or_effect(od_admin, tmp_config_path):
    c, apply = od_admin
    headers = revision_headers(c, None, read_path="/api/config")
    before = tmp_config_path.read_bytes()
    r = c.put("/api/config/connection", headers=headers,
              json={"od_api_base": "http://open-design-svc:7457", "od_basic_user": "admin",
                    "od_basic_pass": SECRET, "restart": False})
    assert_unknown_field_rejected(r, "od_basic_pass", SECRET)
    assert tmp_config_path.read_bytes() == before
    apply.assert_not_awaited()
