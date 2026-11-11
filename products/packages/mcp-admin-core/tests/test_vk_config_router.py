"""vk config router 契約測試：GET / PUT / POST test（反假 GREEN 核心）。"""
from __future__ import annotations

from connection_forms import assert_unknown_field_rejected, gui_connection_fields, product_admin
from management_client import revision_headers

import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# 端點單一常數：Gate 2 定案 /api/health，再變只改 health_probes 一行；Test 與 probe 同源
from vk_mcp_admin.health_probes import VK_HEALTH_PATH


def _client():
    from vk_mcp_admin.routers import config as vk_config
    app = FastAPI()
    app.include_router(vk_config.router)
    return TestClient(app)


def test_get_config_returns_saved_url_unmasked(store_env):
    c = _client()
    c.put("/api/config/connection", json={"vibe_backend_url": "http://vk:3000", "restart": False}, headers=revision_headers(c, None, read_path='/api/config'))
    response = c.get("/api/config")
    body = response.json()
    configuration = body.pop("configuration")
    assert response.headers["etag"] == '"' + configuration["revision"] + '"'
    assert body == {"vibe_backend_url": "http://vk:3000"}   # url 非 secret：不遮罩、無 _masked 欄


def test_put_strips_whitespace(store_env):
    c = _client()
    r = c.put("/api/config/connection",
              json={"vibe_backend_url": "  http://vk:3000  ", "restart": False}, headers=revision_headers(c, None, read_path='/api/config'))
    assert r.status_code == 200 and r.json()["success"] is True
    assert c.get("/api/config").json()["vibe_backend_url"] == "http://vk:3000"


def test_put_blank_url_clears_saved_value(store_env):
    # vibe_backend_url 非 secret → 不受 drop_blank_secrets 保護，可被有意清空
    c = _client()
    c.put("/api/config/connection", json={"vibe_backend_url": "http://vk:3000", "restart": False}, headers=revision_headers(c, None, read_path='/api/config'))
    c.put("/api/config/connection", json={"vibe_backend_url": "", "restart": False}, headers=revision_headers(c, None, read_path='/api/config'))
    assert c.get("/api/config").json()["vibe_backend_url"] == ""


def test_put_restart_requested_but_pm_not_running_starts_it(store_env, monkeypatch):
    """#1350：子行程沒在跑 → 存檔後改為 start（舊版只回「已存檔、未重啟」）。"""
    from vk_mcp_admin.routers import config as vk_config

    calls: list[str] = []

    class _PM:
        is_running = False

        async def start(self):
            calls.append("start")
            return True
    monkeypatch.setattr(vk_config, "get_process_manager", lambda: _PM())
    c = _client()
    body = c.put("/api/config/connection",
                 json={"vibe_backend_url": "http://vk:3000", "restart": True}, headers=revision_headers(c, None, read_path='/api/config')).json()
    assert calls == ["start"]
    assert body["success"] is True and body["restarted"] is True
    assert "已啟動" in body["message"]


def test_put_pm_not_running_start_fails_still_saves(store_env, monkeypatch):
    from vk_mcp_admin.routers import config as vk_config

    class _PM:
        is_running = False

        async def start(self):
            return False
    monkeypatch.setattr(vk_config, "get_process_manager", lambda: _PM())
    c = _client()
    body = c.put("/api/config/connection",
                 json={"vibe_backend_url": "http://vk:3000", "restart": True}, headers=revision_headers(c, None, read_path='/api/config')).json()
    assert body["success"] is True and body["restarted"] is False
    assert "啟動失敗" in body["message"]
    assert c.get("/api/config").json()["vibe_backend_url"] == "http://vk:3000"  # 已存檔


def test_put_restart_failure_still_saves(store_env, monkeypatch):
    from vk_mcp_admin.routers import config as vk_config

    class _PM:
        is_running = True
        async def restart(self):
            raise RuntimeError("boom")
    monkeypatch.setattr(vk_config, "get_process_manager", lambda: _PM())
    c = _client()
    body = c.put("/api/config/connection",
                 json={"vibe_backend_url": "http://vk:3000", "restart": True}, headers=revision_headers(c, None, read_path='/api/config')).json()
    assert body["success"] is True and body["restarted"] is False
    assert "重啟失敗" in body["message"]
    assert c.get("/api/config").json()["vibe_backend_url"] == "http://vk:3000"  # 已存檔


# ---------------------------------------------------------------------------
# Task 5：POST /api/config/test（反假 GREEN 核心）
#
# 沿用 Task 3 的 _FakeAsyncClient 模式，本檔自帶一份（測試檔間少量重複可接受）。
# patch 機制說明：health_probes 與 routers.config 皆 module-level `import httpx`，兩檔
# 引用的是同一個 httpx module 物件（實測 config.httpx is hp.httpx）；因此 monkeypatch
# httpx module 上的 AsyncClient 對兩端同時生效（patch config.httpx 或 hp.httpx 等效）。
# 真正該防的是有人把某端改成 `from httpx import AsyncClient`——屆時該端綁的是自己
# 命名空間的名字、不再指向 module attribute，需改 patch 各自命名空間。
# ---------------------------------------------------------------------------


class _FakeAsyncClient:
    """假 httpx.AsyncClient：記錄 GET URL，回預設回應或丟預設例外。"""
    calls: list[str] = []
    response: httpx.Response | None = None
    raise_exc: Exception | None = None

    def __init__(self, *args, **kwargs): ...
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): return False
    async def get(self, url, **kwargs):
        _FakeAsyncClient.calls.append(str(url))
        if _FakeAsyncClient.raise_exc is not None:
            raise _FakeAsyncClient.raise_exc
        return _FakeAsyncClient.response


@pytest.fixture(autouse=True)
def _reset_fake(monkeypatch):
    # autouse：每個 test 前重置 class 級可變狀態並重新 patch，消除跨 test 殘留 footgun
    # （與 test_vk_health_probes.py 的 _reset_fake 一致）。patch 對象＝httpx module 上的
    # AsyncClient；因 routers.config 走 `import httpx`，此 patch 對 POST /test 端點自建
    # 的 AsyncClient 生效。
    from vk_mcp_admin.routers import config as vk_config
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.response = None
    _FakeAsyncClient.raise_exc = None
    monkeypatch.setattr(vk_config.httpx, "AsyncClient", _FakeAsyncClient)
    yield


def _resp(status=200, json_body=None, url="http://vk-host:3000/api/health"):
    req = httpx.Request("GET", url)
    return httpx.Response(status, request=req, json=json_body if json_body is not None else {})


def test_test_reads_saved_value_and_hits_vk_health(store_env):
    c = _client()
    c.put("/api/config/connection", json={"vibe_backend_url": "http://vk-host:3000/", "restart": False}, headers=revision_headers(c, None, read_path='/api/config'))
    _FakeAsyncClient.response = _resp(200)
    body = c.post("/api/config/test").json()
    assert body["success"] is True
    # 反假 GREEN 斷言核心：打的是 VK Host {VK_HEALTH_PATH}（存檔值、尾斜線 rstrip），不是 supergateway :8000
    assert _FakeAsyncClient.calls == [f"http://vk-host:3000{VK_HEALTH_PATH}"]
    assert all(":8000" not in u for u in _FakeAsyncClient.calls)


def test_test_ignores_request_body(store_env):
    c = _client()
    c.put("/api/config/connection", json={"vibe_backend_url": "http://saved:3000", "restart": False}, headers=revision_headers(c, None, read_path='/api/config'))
    _FakeAsyncClient.response = _resp(200)
    c.post("/api/config/test", json={"vibe_backend_url": "http://evil:9"})   # body 必須被忽略
    assert _FakeAsyncClient.calls == [f"http://saved:3000{VK_HEALTH_PATH}"]


def test_test_unconfigured_url_fails(store_env):
    c = _client()
    body = c.post("/api/config/test").json()
    assert body["success"] is False           # 未設定不誤綠
    assert _FakeAsyncClient.calls == []


def test_test_connect_error_fails_with_readable_message(store_env):
    c = _client()
    c.put("/api/config/connection", json={"vibe_backend_url": "http://vk-host:3000", "restart": False}, headers=revision_headers(c, None, read_path='/api/config'))
    _FakeAsyncClient.raise_exc = httpx.ConnectError("refused")
    body = c.post("/api/config/test").json()
    assert body["success"] is False           # ConnectError → 可讀訊息、不誤綠
    assert "Connection refused" in body["message"]


def test_test_timeout_fails(store_env):
    c = _client()
    c.put("/api/config/connection", json={"vibe_backend_url": "http://vk-host:3000", "restart": False}, headers=revision_headers(c, None, read_path='/api/config'))
    _FakeAsyncClient.raise_exc = httpx.TimeoutException("slow")
    body = c.post("/api/config/test").json()
    assert body["success"] is False
    assert "timed out" in body["message"].lower()


def test_test_http_error_fails(store_env):
    c = _client()
    c.put("/api/config/connection", json={"vibe_backend_url": "http://vk-host:3000", "restart": False}, headers=revision_headers(c, None, read_path='/api/config'))
    _FakeAsyncClient.response = _resp(500)    # raise_for_status → HTTPStatusError
    body = c.post("/api/config/test").json()
    assert body["success"] is False
    assert "500" in body["message"]           # message 帶狀態碼


# ---------------------------------------------------------------------------
# 沒宣告的欄位回 422、不默默忽略（以前回 200 什麼都沒存；MCP:Hermes 2026-11-05 同型）
# ---------------------------------------------------------------------------
SECRET = "vk-canary-5d21e7-secret"


@pytest.fixture
def vk_admin(tmp_config_path, monkeypatch):
    client, apply = product_admin("vk", tmp_config_path, monkeypatch)
    yield client, apply
    client.close()


def test_gui_payload_is_accepted(vk_admin, tmp_config_path):
    c, apply = vk_admin
    gui = {"vibe_backend_url": "http://vk-host:3000"}
    assert set(gui) == gui_connection_fields("vk")
    r = c.put("/api/config/connection", json=gui, headers=revision_headers(c, None, read_path="/api/config"))
    assert r.status_code == 200, r.text
    assert json.loads(tmp_config_path.read_text())["connection"] == gui
    apply.assert_awaited_once()


def test_misspelled_url_field_is_rejected_without_echo_or_effect(vk_admin, tmp_config_path):
    """vibe_backend_uri（拼錯）＋缺必填的 vibe_backend_url：422；缺欄位那筆的 input 是整個 body，也不回顯。"""
    c, apply = vk_admin
    headers = revision_headers(c, None, read_path="/api/config")
    before = tmp_config_path.read_bytes()
    r = c.put("/api/config/connection", headers=headers,
              json={"vibe_backend_uri": f"http://admin:{SECRET}@vk-host:3000", "restart": False})
    assert_unknown_field_rejected(r, "vibe_backend_uri", SECRET)
    assert tmp_config_path.read_bytes() == before
    apply.assert_not_awaited()
