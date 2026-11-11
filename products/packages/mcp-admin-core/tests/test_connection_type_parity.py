"""Dedicated and generic Odoo writes must reject invalid types before effects."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from connection_forms import assert_unknown_field_rejected, gui_connection_fields


BASELINE = {
    "odoo_url": "",
    "odoo_db": "original-db",
    "odoo_username": "synthetic-user",
    "odoo_password": "synthetic-password",
}


@pytest.fixture
def odoo_client(tmp_config_path, monkeypatch):
    from mcp_admin_core.config import store as store_mod
    from mcp_admin_core.launch_policy import set_launch_policy
    from mcp_admin_core.routers import settings
    from odoo_mcp_admin.main import LAUNCH_POLICY, app
    from odoo_mcp_admin.routers import config

    snapshot = json.loads(json.dumps(store_mod._DEFAULT_CONFIG))
    snapshot["connection"] = BASELINE.copy()
    tmp_config_path.write_text(json.dumps(snapshot))
    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(tmp_config_path))
    monkeypatch.setattr(store_mod, "_instance", None)
    store_mod.get_config_store()
    set_launch_policy(LAUNCH_POLICY)
    # Only the external lifecycle boundary is replaced. Routes, auth, revision
    # locking, ConfigStore validation and actual file writes remain real.
    apply = AsyncMock(return_value=True)
    pm = SimpleNamespace(is_running=True, start=apply, restart=apply, stop=AsyncMock(),
                         start_or_restart=apply)
    monkeypatch.setattr(config, "get_process_manager", lambda: pm)
    monkeypatch.setattr(settings, "get_process_manager", lambda: pm)
    client = TestClient(app)  # no lifespan: never spawn a child or contact Odoo
    login = client.post("/api/auth/login", json={"password": "admin"})
    assert login.status_code == 200
    client.headers["Authorization"] = "Bearer " + login.json()["token"]
    yield client, tmp_config_path, apply
    client.close()


@pytest.mark.parametrize("route", ["/api/config/connection", "/api/settings/connection"])
@pytest.mark.parametrize("value", [["tracker43_admin_probe"], {"bad": "shape"}, None, 17, True],
                         ids=["list", "object", "null", "number", "boolean"])
def test_non_string_connection_rejected_without_effects(odoo_client, route, value):
    client, path, apply = odoo_client
    before = client.get("/api/settings")
    assert before.status_code == 200
    raw = path.read_bytes()
    payload = {**BASELINE, "odoo_db": value}
    response = client.put(route, json=payload,
                          headers={"If-Match": before.headers["etag"]})
    after = client.get("/api/settings")
    observed = (response.status_code, path.read_bytes() == raw,
                after.headers["etag"] == before.headers["etag"], apply.await_count)
    assert observed in ((400, True, True, 0), (422, True, True, 0)), observed
    assert path.read_bytes() == raw
    assert after.headers["etag"] == before.headers["etag"]
    assert after.json()["configuration"]["saved_revision"] == before.json()["configuration"]["saved_revision"]
    apply.assert_not_awaited()


@pytest.mark.parametrize("route", ["/api/config/connection", "/api/settings/connection"])
def test_trim_and_blank_secret_keep_saved_credentials(odoo_client, route):
    client, path, apply = odoo_client
    before = client.get("/api/settings")
    response = client.put(route, json={**BASELINE, "odoo_db": "  next-db  ",
                                      "odoo_username": "  next-user  ", "odoo_password": "  "},
                          headers={"If-Match": before.headers["etag"]})
    assert response.status_code == 200
    assert json.loads(path.read_text())["connection"] == {
        **BASELINE, "odoo_db": "next-db", "odoo_username": "next-user"}
    after = client.get("/api/settings")
    assert after.headers["etag"] != before.headers["etag"]
    assert after.json()["configuration"]["saved_revision"] != before.json()["configuration"]["saved_revision"]
    apply.assert_awaited_once()


@pytest.mark.parametrize("route", ["/api/config/connection", "/api/settings/connection"])
def test_odoo_account_rebind_revokes_old_url_without_rotating_on_password_change(odoo_client, route):
    from mcp_admin_core.config import store as store_mod
    from odoo_mcp_admin.routers import config

    client, path, apply = odoo_client
    store = store_mod.get_config_store()
    asyncio.run(store.patch("connection", {"odoo_url": "https://odoo.example.test"}))
    asyncio.run(store.change_mcp_token("original-account-token", action="set"))
    assert client.get("/private_original-account-token/health").status_code != 403

    read_path = "/api/config" if route.startswith("/api/config/") else "/api/settings"
    read = client.get(read_path)
    payload = {**BASELINE, "odoo_url": "https://odoo.example.test", "odoo_username": "other-user"}
    if route.startswith("/api/settings/"):
        payload = {"odoo_username": "other-user"}
    else:
        payload["restart"] = False  # Identity changes must still stop/apply the old child.
    changed = client.put(route, json=payload, headers={"If-Match": read.headers["etag"]})
    assert changed.status_code == 200, changed.text
    assert changed.json()["token_rotated"] is True
    assert "Tokens" in changed.json()["message"]
    config.get_process_manager().stop.assert_awaited_once()
    assert apply.await_count == 1
    assert client.get("/private_original-account-token/health").status_code == 403

    current_token = client.post("/api/settings/mcp_auth_token/reveal").json()["value"]
    assert current_token and current_token != "original-account-token"
    assert current_token not in changed.text
    assert client.get(f"/private_{current_token}/health").status_code != 403

    read = client.get(read_path)
    payload = {**BASELINE, "odoo_url": "https://odoo.example.test",
               "odoo_username": "other-user", "odoo_password": "replacement-password"}
    if route.startswith("/api/settings/"):
        payload = {"odoo_password": "replacement-password"}
    password = client.put(route, json=payload, headers={"If-Match": read.headers["etag"]})
    assert password.status_code == 200, password.text
    assert password.json()["token_rotated"] is False
    assert client.post("/api/settings/mcp_auth_token/reveal").json()["value"] == current_token
    assert json.loads(path.read_text())["connection"]["odoo_password"] == "replacement-password"


def test_generic_partial_update_and_empty_patch_keep_omitted_fields(odoo_client):
    client, path, apply = odoo_client
    before = client.get("/api/settings")
    response = client.put("/api/settings/connection", json={"odoo_db": "  partial-db  "},
                          headers={"If-Match": before.headers["etag"]})
    assert response.status_code == 200
    assert json.loads(path.read_text())["connection"] == {**BASELINE, "odoo_db": "partial-db"}
    current = client.get("/api/settings")
    raw = path.read_bytes()
    empty = client.put("/api/settings/connection", json={},
                       headers={"If-Match": current.headers["etag"]})
    assert empty.status_code == 200
    assert path.read_bytes() == raw
    assert client.get("/api/settings").headers["etag"] == current.headers["etag"]
    # Preserve existing lifecycle semantics even for an unchanged connection PUT.
    assert apply.await_count == 2


@pytest.mark.parametrize("route", ["/api/config/connection", "/api/settings/connection"])
def test_missing_and_stale_revision_do_not_save_or_apply(odoo_client, route):
    client, path, apply = odoo_client
    before = client.get("/api/settings")
    raw = path.read_bytes()
    payload = {**BASELINE, "odoo_db": "next-db"}
    for headers, status, code in [({}, 428, "config_revision_required"),
                                  ({"If-Match": '"stale-revision"'}, 409, "config_conflict")]:
        response = client.put(route, json=payload, headers=headers)
        assert response.status_code == status
        assert response.json()["detail"]["code"] == code
        assert path.read_bytes() == raw
        assert client.get("/api/settings").headers["etag"] == before.headers["etag"]
    apply.assert_not_awaited()


def test_unknown_keys_are_rejected_on_both_routes(odoo_client):
    client, path, apply = odoo_client
    before = client.get("/api/settings")
    raw = path.read_bytes()
    rejected = client.put("/api/settings/connection",
        json={"unexpected": "value", "odoo_db": ["bad"]},
        headers={"If-Match": before.headers["etag"]})
    assert rejected.status_code == 400
    assert "Unknown connection field" in rejected.json()["detail"]
    assert path.read_bytes() == raw
    apply.assert_not_awaited()
    # 產品的連線模型以前是 pydantic 預設的 extra='ignore'：多送的欄位回 200、什麼都沒存
    # （MCP:Hermes 2026-11-05 拼錯欄位名就是這樣）。現在與共用路徑一樣拒收，只是狀態碼是 422。
    dedicated = client.put("/api/config/connection",
        json={**BASELINE, "unexpected": "unexpected-canary-value", "restart": False},
        headers={"If-Match": before.headers["etag"]})
    assert_unknown_field_rejected(dedicated, "unexpected", "unexpected-canary-value")
    assert path.read_bytes() == raw
    assert client.get("/api/settings").headers["etag"] == before.headers["etag"]
    apply.assert_not_awaited()


def test_gui_payload_is_accepted(odoo_client):
    """Connection 頁送四欄（API Key 存在 odoo_password）、不送 restart（預設套用）。"""
    client, path, apply = odoo_client
    gui = {"odoo_url": "https://odoo.example.com", "odoo_db": "gui-db",
           "odoo_username": "gui-user", "odoo_password": "odoo-canary-91e0b4-secret"}
    assert set(gui) == gui_connection_fields("odoo")
    before = client.get("/api/config")
    response = client.put("/api/config/connection", json=gui, headers={"If-Match": before.headers["etag"]})
    assert response.status_code == 200, response.text
    assert json.loads(path.read_text())["connection"] == gui
    apply.assert_awaited_once()


def test_misspelled_api_key_field_is_rejected_without_echo_or_effect(odoo_client):
    """頁面上 odoo_password 的標籤是「API Key」：送成 odoo_api_key 時缺 odoo_password（必填，那筆錯誤的 input
    是整個 body）又多一個沒宣告的欄位。422、兩筆都不回顯 key、不存檔也不套用。"""
    client, path, apply = odoo_client
    secret = "odoo-canary-91e0b4-secret"
    before = client.get("/api/config")
    raw = path.read_bytes()
    payload = {k: v for k, v in BASELINE.items() if k != "odoo_password"}
    response = client.put("/api/config/connection", json={**payload, "odoo_api_key": secret},
                          headers={"If-Match": before.headers["etag"]})
    assert_unknown_field_rejected(response, "odoo_api_key", secret)
    assert {"type": "missing", "loc": ["body", "odoo_password"], "msg": "Field required"} in response.json()["detail"]
    assert path.read_bytes() == raw
    apply.assert_not_awaited()


def test_generic_invalid_secret_has_no_echo_or_effect(odoo_client):
    client, path, apply = odoo_client
    before = client.get("/api/settings")
    raw = path.read_bytes()
    response = client.put("/api/settings/connection",
        json={"odoo_password": ["synthetic-secret-no-echo"]},
        headers={"If-Match": before.headers["etag"]})
    assert response.status_code == 400
    assert "synthetic-secret-no-echo" not in response.text
    assert path.read_bytes() == raw
    assert client.get("/api/settings").headers["etag"] == before.headers["etag"]
    apply.assert_not_awaited()


def test_generic_explicit_secret_clear_preserves_null_and_normalizes_other_fields(odoo_client, monkeypatch):
    from mcp_admin_core.process import _build_subprocess_env
    from odoo_mcp_admin.main import LAUNCH_POLICY

    client, path, apply = odoo_client
    before = client.get("/api/settings")
    saved = json.loads(path.read_text())
    env_name = LAUNCH_POLICY.env_name_for_connection_key("odoo_password")
    monkeypatch.setenv(env_name, "synthetic-inherited-password")
    response = client.put("/api/settings/connection",
        json={"odoo_password": None, "odoo_db": "  cleared-db  "},
        headers={"If-Match": before.headers["etag"]})
    assert response.status_code == 200 and response.json()["saved"] is True
    after = json.loads(path.read_text())
    assert after["connection"] == {**BASELINE, "odoo_password": None, "odoo_db": "cleared-db"}
    assert {k: v for k, v in after.items() if k != "connection"} == {
        k: v for k, v in saved.items() if k != "connection"}
    readback = client.get("/api/settings")
    assert readback.headers["etag"] != before.headers["etag"]
    assert readback.json()["configuration"]["saved_revision"] != before.json()["configuration"]["saved_revision"]
    assert client.get("/api/config").json()["odoo_password_masked"] == "(not set)"
    env = _build_subprocess_env(after["connection"], after["mcp_server"], LAUNCH_POLICY, after)
    assert env[env_name] == ""
    apply.assert_awaited_once()


@pytest.mark.parametrize("invalid", [
    {"odoo_db": None}, {"odoo_db": ["bad"]}, {"odoo_db": 17},
    {"unexpected_password": None},
], ids=["nonsecret-null", "nonsecret-list", "nonsecret-number", "unknown-secret-null"])
def test_generic_clear_with_invalid_update_is_atomic(odoo_client, invalid):
    client, path, apply = odoo_client
    before = client.get("/api/settings")
    raw = path.read_bytes()
    response = client.put("/api/settings/connection",
        json={"odoo_password": None, **invalid},
        headers={"If-Match": before.headers["etag"]})
    assert response.status_code == 400
    assert path.read_bytes() == raw
    assert json.loads(path.read_text())["connection"]["odoo_password"] == BASELINE["odoo_password"]
    after = client.get("/api/settings")
    assert after.headers["etag"] == before.headers["etag"]
    assert after.json()["configuration"]["saved_revision"] == before.json()["configuration"]["saved_revision"]
    apply.assert_not_awaited()


def test_n8n_numeric_non_env_keys_keep_their_declared_contract(make_app, tmp_config_path, monkeypatch):
    from mcp_admin_core.launch_policy import set_launch_policy
    from mcp_admin_core.routers import settings
    from n8n_mcp_admin.main import LAUNCH_POLICY

    client = make_app({"connection": {"n8n_api_url": "", "n8n_api_key": "synthetic-key",
                                      "mcp_session_timeout": 600, "mcp_max_sessions": 10}})
    set_launch_policy(LAUNCH_POLICY)
    assert {"mcp_session_timeout", "mcp_max_sessions"} <= LAUNCH_POLICY.non_env_connection_keys
    apply = AsyncMock(return_value=True)
    monkeypatch.setattr(settings, "get_process_manager",
                        lambda: SimpleNamespace(is_running=True, start_or_restart=apply))
    try:
        login = client.post("/api/auth/login", json={"password": "admin"})
        client.headers["Authorization"] = "Bearer " + login.json()["token"]
        before = client.get("/api/settings")
        response = client.put("/api/settings/connection",
            json={"mcp_session_timeout": 1200, "mcp_max_sessions": 11},
            headers={"If-Match": before.headers["etag"]})
        assert response.status_code == 200
        connection = json.loads(tmp_config_path.read_text())["connection"]
        assert connection == {"n8n_api_url": "", "n8n_api_key": "synthetic-key",
                              "mcp_session_timeout": 1200, "mcp_max_sessions": 11}
        assert type(connection["mcp_session_timeout"]) is int
        assert type(connection["mcp_max_sessions"]) is int
        apply.assert_awaited_once()
    finally:
        client.close()
