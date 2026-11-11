"""hermes config router：PUT /api/config/connection 的 partial patch 語意。

事故背景：五個欄位原本都是 required，handler 無條件全寫回 store ⇒ 任何「只想補
dashboard 三格」的呼叫者都會把 `gateway_url` / `gateway_api_key` 清成空字串，當場打爛
服務。改成 partial patch 後語意分三種：

  未送出（欄位不在 payload）＝ 保留原值
  送出空字串（非 secret）    ＝ 有意清空
  送出空字串（secret）       ＝ 保留原值（drop_blank_secrets：表單載入時 secret 欄為空）
"""
from __future__ import annotations

from connection_forms import assert_unknown_field_rejected, gui_connection_fields, product_admin
from management_client import revision_headers

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

FULL = {
    "gateway_url": "http://hermes-agent-svc:8642",
    "gateway_api_key": "gw-key",
    "dashboard_url": "http://hermes-agent-svc:9119",
    "dashboard_username": "admin",
    "dashboard_password": "dash-pw",
    "restart": False,
}


def _client():
    from hermes_mcp_admin.routers import config as hermes_config
    app = FastAPI()
    app.include_router(hermes_config.router)
    return TestClient(app)


def _seeded_client():
    c = _client()
    assert c.put("/api/config/connection", json=FULL, headers=revision_headers(c, None, read_path='/api/config')).status_code == 200
    return c


# ---------------------------------------------------------------------------
# partial patch：未送出＝保留
# ---------------------------------------------------------------------------
def test_partial_put_keeps_unsent_gateway_fields(store_env):
    c = _seeded_client()

    r = c.put("/api/config/connection", json={
        "dashboard_url": "http://hermes-agent-svc/",
        "dashboard_username": "ops",
        "dashboard_password": "new-pw",
        "restart": False,
    }, headers=revision_headers(c, None, read_path='/api/config'))
    assert r.status_code == 200

    body = c.get("/api/config").json()
    assert body["gateway_url"] == "http://hermes-agent-svc:8642"   # 沒被清空
    assert body["gateway_key_masked"] != "(not set)"               # 沒被清空
    assert body["dashboard_url"] == "http://hermes-agent-svc/"
    assert body["dashboard_username"] == "ops"


def test_partial_put_single_field(store_env):
    c = _seeded_client()

    assert c.put("/api/config/connection",
                 json={"gateway_url": "http://new-gw:8642", "restart": False}, headers=revision_headers(c, None, read_path='/api/config')).status_code == 200

    body = c.get("/api/config").json()
    assert body["gateway_url"] == "http://new-gw:8642"
    assert body["dashboard_url"] == "http://hermes-agent-svc:9119"
    assert body["dashboard_username"] == "admin"
    assert body["dashboard_password_masked"] != "(not set)"


# ---------------------------------------------------------------------------
# 明送空字串：非 secret 可清空、secret 沿用（區分「未送出」與「送空字串」）
# ---------------------------------------------------------------------------
def test_explicit_blank_clears_non_secret_field(store_env):
    c = _seeded_client()

    c.put("/api/config/connection", json={"dashboard_url": "", "restart": False}, headers=revision_headers(c, None, read_path='/api/config'))

    assert c.get("/api/config").json()["dashboard_url"] == ""


def test_explicit_blank_secret_keeps_stored_value(store_env):
    # 表單載入時 secret 欄一律為空 → 空字串不得覆寫既有 key（drop_blank_secrets 既有行為）
    c = _seeded_client()

    c.put("/api/config/connection", json={"gateway_api_key": "", "restart": False}, headers=revision_headers(c, None, read_path='/api/config'))

    assert c.get("/api/config").json()["gateway_key_masked"] != "(not set)"


# ---------------------------------------------------------------------------
# 向後相容：前端一律送五欄
# ---------------------------------------------------------------------------
def test_full_five_field_payload_still_works(store_env):
    c = _client()

    r = c.put("/api/config/connection", json=FULL, headers=revision_headers(c, None, read_path='/api/config'))
    assert r.status_code == 200 and r.json()["status"] == "ok"

    body = c.get("/api/config").json()
    assert body["gateway_url"] == FULL["gateway_url"]
    assert body["dashboard_url"] == FULL["dashboard_url"]
    assert body["dashboard_username"] == "admin"
    assert body["gateway_key_masked"] != "(not set)"
    assert body["dashboard_password_masked"] != "(not set)"


def test_put_strips_whitespace(store_env):
    c = _client()

    c.put("/api/config/connection",
          json={"gateway_url": "  http://hermes-agent-svc:8642  ", "restart": False}, headers=revision_headers(c, None, read_path='/api/config'))

    assert c.get("/api/config").json()["gateway_url"] == "http://hermes-agent-svc:8642"


def test_empty_payload_is_a_no_op(store_env):
    c = _seeded_client()

    r = c.put("/api/config/connection", json={"restart": False}, headers=revision_headers(c, None, read_path='/api/config'))
    assert r.status_code == 200

    body = c.get("/api/config").json()
    assert body["gateway_url"] == FULL["gateway_url"]
    assert body["dashboard_url"] == FULL["dashboard_url"]


# ---------------------------------------------------------------------------
# restart 旗標維持原語意（未送出時預設重啟）
# ---------------------------------------------------------------------------
def test_restart_defaults_to_true_and_restarts_running_process(store_env, monkeypatch):
    from hermes_mcp_admin.routers import config as hermes_config

    calls: list[str] = []

    class _PM:
        is_running = True

        async def restart(self):
            calls.append("restart")
            return True

    monkeypatch.setattr(hermes_config, "get_process_manager", lambda: _PM())
    c = _client()

    body = c.put("/api/config/connection", json={"gateway_url": "http://gw:8642"}, headers=revision_headers(c, None, read_path='/api/config')).json()

    assert calls == ["restart"]
    assert "restarted" in body["message"]


def test_restart_requested_but_not_running_starts_process(store_env, monkeypatch):
    """#1350：子行程沒在跑 → 存檔後 start（不是只在運行中才 restart）。"""
    from hermes_mcp_admin.routers import config as hermes_config

    calls: list[str] = []

    class _PM:
        is_running = False

        async def start(self):
            calls.append("start")
            return True

    monkeypatch.setattr(hermes_config, "get_process_manager", lambda: _PM())
    c = _client()

    body = c.put("/api/config/connection", json={"gateway_url": "http://gw:8642"}, headers=revision_headers(c, None, read_path='/api/config')).json()

    assert calls == ["start"]
    assert "started" in body["message"]


# ---------------------------------------------------------------------------
# 沒宣告的欄位回 422（MCP:Hermes 2026-11-05：gateway_key 回 200、什麼都沒存）
# ---------------------------------------------------------------------------
SECRET = "hermes-canary-0f3a9c-secret"


@pytest.fixture
def hermes_admin(tmp_config_path, monkeypatch):
    client, apply = product_admin("hermes", tmp_config_path, monkeypatch)
    yield client, apply
    client.close()


def test_gui_payload_is_accepted(hermes_admin, tmp_config_path):
    """Connection 頁一律送五欄、不送 restart（預設重啟）。"""
    c, apply = hermes_admin
    gui = {"gateway_url": "http://hermes-agent-svc:8642", "gateway_api_key": SECRET,
           "dashboard_url": "http://hermes-agent-svc:9119", "dashboard_username": "admin",
           "dashboard_password": "dash-" + SECRET}
    assert set(gui) == gui_connection_fields("hermes")
    r = c.put("/api/config/connection", json=gui, headers=revision_headers(c, None, read_path="/api/config"))
    assert r.status_code == 200, r.text
    assert json.loads(tmp_config_path.read_text())["connection"] == gui
    apply.assert_awaited_once()


def test_misspelled_key_field_is_rejected_without_echo_or_effect(hermes_admin, tmp_config_path):
    """GET 回的是 gateway_key_masked，照著寫成 gateway_key 存檔：以前 200、key 沒存；現在 422、不回顯 key。"""
    c, apply = hermes_admin
    headers = revision_headers(c, None, read_path="/api/config")
    before = tmp_config_path.read_bytes()
    r = c.put("/api/config/connection", headers=headers,
              json={"gateway_url": "http://hermes-agent-svc:8642", "gateway_key": SECRET, "restart": False})
    assert_unknown_field_rejected(r, "gateway_key", SECRET)
    assert tmp_config_path.read_bytes() == before
    apply.assert_not_awaited()
