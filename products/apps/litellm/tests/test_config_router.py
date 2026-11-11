"""/api/config 連線設定 router：base_url ＋ secret master_key（只寫不讀）。"""
from __future__ import annotations

from connection_forms import assert_unknown_field_rejected, gui_connection_fields
from management_client import revision_headers

import pytest

from litellm_mcp_admin import health_probes
from litellm_mcp_admin.routers import config as config_router

from .conftest import stored_config

KEY = "sk-test-0123456789abcdef"


class _FakePM:
    def __init__(self, running: bool = False, ok: bool = True):
        self.is_running = running
        self.ok = ok
        self.calls = 0

    async def start_or_restart(self):
        self.calls += 1
        return self.ok


@pytest.fixture
def fake_pm(monkeypatch):
    pm = _FakePM()
    monkeypatch.setattr(config_router, "get_process_manager", lambda: pm)
    return pm


# -- GET -----------------------------------------------------------------------


def test_get_masks_key_and_never_returns_it(make_client):
    client = make_client(connection={"base_url": "http://lite:4000", "master_key": KEY})
    body = client.get("/api/config").json()
    assert body["base_url"] == "http://lite:4000"
    assert body["master_key_set"] is True
    assert body["master_key_masked"] != KEY
    assert KEY not in client.get("/api/config").text
    assert body["master_key_masked"].startswith("sk") and body["master_key_masked"].endswith("ef")


def test_get_unset(make_client):
    response = make_client(connection={}).get("/api/config")
    body = response.json()
    configuration = body.pop("configuration")
    assert response.headers["etag"] == '"' + configuration["revision"] + '"'
    assert body == {"base_url": "", "master_key_masked": "(not set)", "master_key_set": False}


def test_config_requires_login(make_client):
    client = make_client(connection={})
    client.headers.pop("Authorization")
    client.cookies.clear()  # login 也會發 httpOnly cookie
    assert client.get("/api/config").status_code == 401
    assert client.put("/api/config/connection", json={"base_url": "http://x:4000"}).status_code == 401


# -- PUT -----------------------------------------------------------------------


def test_put_saves_strips_and_restarts(make_client, config_path, fake_pm):
    client = make_client(connection={})
    resp = client.put("/api/config/connection", json={"base_url": "  http://lite:4000/  ", "master_key": f" {KEY} "}, headers=revision_headers(client, None, read_path='/api/config'))
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True and body["restarted"] is True
    assert "已啟動" in body["message"]  # 子行程原本沒在跑 → start（#1350）
    conn = stored_config(config_path)["connection"]
    assert conn == {"base_url": "http://lite:4000", "master_key": KEY}
    assert fake_pm.calls == 1


def test_put_drops_trailing_v1(make_client, config_path, fake_pm):
    client = make_client(connection={})
    client.put("/api/config/connection", json={"base_url": "http://lite:4000/v1/", "master_key": KEY}, headers=revision_headers(client, None, read_path='/api/config'))
    assert stored_config(config_path)["connection"]["base_url"] == "http://lite:4000"


def test_put_blank_key_keeps_existing(make_client, config_path, fake_pm):
    client = make_client(connection={"base_url": "http://old:4000", "master_key": KEY})
    client.put("/api/config/connection", json={"base_url": "http://new:4000", "master_key": ""}, headers=revision_headers(client, None, read_path='/api/config'))
    conn = stored_config(config_path)["connection"]
    assert conn == {"base_url": "http://new:4000", "master_key": KEY}


def test_put_echoed_mask_keeps_existing(make_client, config_path, fake_pm):
    client = make_client(connection={"base_url": "http://lite:4000", "master_key": KEY})
    masked = client.get("/api/config").json()["master_key_masked"]
    client.put("/api/config/connection", json={"base_url": "http://lite:4000", "master_key": masked}, headers=revision_headers(client, None, read_path='/api/config'))
    assert stored_config(config_path)["connection"]["master_key"] == KEY


def test_put_new_key_overwrites(make_client, config_path, fake_pm):
    client = make_client(connection={"base_url": "http://lite:4000", "master_key": KEY})
    client.put("/api/config/connection", json={"base_url": "http://lite:4000", "master_key": "sk-new-key-xyz"}, headers=revision_headers(client, None, read_path='/api/config'))
    assert stored_config(config_path)["connection"]["master_key"] == "sk-new-key-xyz"


def test_put_can_clear_base_url(make_client, config_path, fake_pm):
    client = make_client(connection={"base_url": "http://lite:4000", "master_key": KEY})
    client.put("/api/config/connection", json={"base_url": "", "master_key": ""}, headers=revision_headers(client, None, read_path='/api/config'))
    assert stored_config(config_path)["connection"] == {"base_url": "", "master_key": KEY}


@pytest.mark.parametrize("bad", ["lite:4000", "ftp://lite", "http://", "http://user:pw@lite:4000"])
def test_put_rejects_bad_url(make_client, config_path, fake_pm, bad):
    client = make_client(connection={"base_url": "http://keep:4000"})
    resp = client.put("/api/config/connection", json={"base_url": bad, "master_key": ""}, headers=revision_headers(client, None, read_path='/api/config'))
    assert resp.status_code == 400
    assert stored_config(config_path)["connection"]["base_url"] == "http://keep:4000"
    assert fake_pm.calls == 0


def test_put_restart_failure_still_saves(make_client, config_path, monkeypatch):
    pm = _FakePM(running=True, ok=False)
    monkeypatch.setattr(config_router, "get_process_manager", lambda: pm)
    client = make_client(connection={})
    body = client.put("/api/config/connection", json={"base_url": "http://lite:4000", "master_key": KEY}, headers=revision_headers(client, None, read_path='/api/config')).json()
    assert body["success"] is True and body["restarted"] is False
    assert "啟動失敗" in body["message"]
    assert stored_config(config_path)["connection"]["base_url"] == "http://lite:4000"


def test_put_without_restart(make_client, fake_pm):
    client = make_client(connection={})
    body = client.put("/api/config/connection", json={"base_url": "http://lite:4000", "restart": False}, headers=revision_headers(client, None, read_path='/api/config')).json()
    assert body["restarted"] is False and fake_pm.calls == 0


# -- 沒宣告的欄位回 422、不默默忽略（以前回 200 什麼都沒存；MCP:Hermes 2026-11-05 同型）-----------

SECRET = "litellm-canary-a17e5f-secret"


def test_put_gui_payload_accepted(make_client, config_path, fake_pm):
    """Connection 頁送 base_url／master_key 兩欄、不送 restart（預設套用）；用正式組裝的 main.app。"""
    client = make_client(connection={}, app="main")
    gui = {"base_url": "http://lite:4000", "master_key": KEY}
    assert set(gui) == gui_connection_fields("litellm")
    resp = client.put("/api/config/connection", json=gui, headers=revision_headers(client, None, read_path='/api/config'))
    assert resp.status_code == 200, resp.text
    assert stored_config(config_path)["connection"] == gui
    assert fake_pm.calls == 1


def test_put_misspelled_key_field_rejected_without_echo_or_effect(make_client, config_path, fake_pm):
    """把 master_key 寫成 api_key：422、不回顯金鑰、不存檔也不套用。"""
    client = make_client(connection={}, app="main")
    headers = revision_headers(client, None, read_path='/api/config')
    before = config_path.read_bytes()
    resp = client.put("/api/config/connection", json={"base_url": "http://lite:4000", "api_key": SECRET}, headers=headers)
    assert_unknown_field_rejected(resp, "api_key", SECRET)
    assert config_path.read_bytes() == before
    assert fake_pm.calls == 0


# -- POST /test ----------------------------------------------------------------


def test_test_uses_saved_values_and_ignores_body(make_client, fake_pm, monkeypatch):
    seen = {}

    async def fake_check(base, key, **_):
        seen["base"], seen["key"] = base, key
        return {"healthy": True, "status_code": 200, "auth_ok": True, "model_count": 5,
                "version": "1.83.14", "db": "connected"}

    monkeypatch.setattr(health_probes, "check_gateway", fake_check)
    client = make_client(connection={"base_url": "http://saved:4000", "master_key": KEY})
    body = client.post("/api/config/test", json={"base_url": "http://evil:1", "master_key": "x"}).json()
    assert seen == {"base": "http://saved:4000", "key": KEY}
    assert body["success"] is True
    assert "1.83.14" in body["message"] and "模型 5 個" in body["message"]
    assert KEY not in str(body)


def test_test_reports_key_rejected(make_client, fake_pm, monkeypatch):
    async def fake_check(base, key, **_):
        return {"healthy": False, "status_code": 401, "auth_ok": False, "error": "LiteLLM 拒絕了設定的金鑰（HTTP 401）"}

    monkeypatch.setattr(health_probes, "check_gateway", fake_check)
    body = make_client(connection={"base_url": "http://saved:4000", "master_key": KEY}).post("/api/config/test").json()
    assert body["success"] is False
    assert "拒絕" in body["message"]
    assert body["details"]["auth_ok"] is False


def test_test_without_url(make_client, fake_pm):
    body = make_client(connection={}).post("/api/config/test").json()
    assert body["success"] is False
    assert "base_url" in body["message"]


def test_normalize_base_url_unit():
    # URL paths are case-sensitive: only exact lowercase origin /v1 is an API suffix.
    assert config_router.normalize_base_url(" https://lite.example.com/V1 ") == "https://lite.example.com/V1"
    assert config_router.normalize_base_url("") == ""
