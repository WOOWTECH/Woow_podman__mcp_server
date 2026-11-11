"""/api/config 連線設定 router：base_url、username ＋ secret app_password（只寫不讀、不進 log）。"""
from __future__ import annotations

from connection_forms import assert_unknown_field_rejected, gui_connection_fields
from management_client import revision_headers

import logging

import pytest

from nextcloud_mcp_admin import health_probes, log_redaction
from nextcloud_mcp_admin.routers import config as config_router

from .conftest import CONN, stored_config
from .fake_nextcloud import BASE_URL, LOGIN, PASSWORD


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


@pytest.fixture(autouse=True)
def _clear_secrets():
    log_redaction.set_secrets(None)
    yield
    log_redaction.set_secrets(None)


# -- GET -----------------------------------------------------------------------


def test_get_masks_password_and_never_returns_it(make_client):
    client = make_client(connection=dict(CONN))
    resp = client.get("/api/config")
    body = resp.json()
    assert body["base_url"] == BASE_URL and body["username"] == LOGIN
    assert body["app_password_set"] is True
    assert PASSWORD not in resp.text
    assert body["app_password_masked"].startswith(PASSWORD[:2]) and body["app_password_masked"].endswith(PASSWORD[-2:])
    assert "*" in body["app_password_masked"]


def test_get_unset(make_client):
    response = make_client(connection={}).get("/api/config")
    body = response.json()
    configuration = body.pop("configuration")
    assert response.headers["etag"] == '"' + configuration["revision"] + '"'
    assert body == {"base_url": "", "username": "", "app_password_masked": "(not set)", "app_password_set": False}


def test_config_requires_login(make_client):
    client = make_client(connection={})
    client.headers.pop("Authorization")
    client.cookies.clear()
    assert client.get("/api/config").status_code == 401
    assert client.put("/api/config/connection", json={"base_url": BASE_URL}).status_code == 401


# -- PUT -----------------------------------------------------------------------


def test_put_saves_strips_and_starts(make_client, config_path, fake_pm):
    client = make_client(connection={})
    resp = client.put("/api/config/connection", json={
        "base_url": f"  {BASE_URL}/  ", "username": f" {LOGIN} ", "app_password": f" {PASSWORD} ",
    }, headers=revision_headers(client, None, read_path='/api/config'))
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True and body["restarted"] is True and "已啟動" in body["message"]
    assert stored_config(config_path)["connection"] == CONN
    assert fake_pm.calls == 1
    assert PASSWORD not in resp.text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://cloud.example.com/index.php/apps/files/?dir=/", None),  # 含 ? → 拒絕
        ("https://cloud.example.com/index.php/apps/files", "https://cloud.example.com"),
        ("https://example.com/nextcloud/remote.php/dav/files/alice/", "https://example.com/nextcloud"),
        ("https://example.com/nextcloud/apps/dashboard/", "https://example.com/nextcloud"),
        ("http://192.168.1.10:8080/", "http://192.168.1.10:8080"),
        ("HTTPS://Cloud.Example.com", "https://Cloud.Example.com"),
    ],
)
def test_normalize_base_url(raw, expected):
    if expected is None:
        with pytest.raises(Exception):
            config_router.normalize_base_url(raw)
    else:
        assert config_router.normalize_base_url(raw).lower() == expected.lower()


def test_put_blank_password_keeps_existing(make_client, config_path, fake_pm):
    client = make_client(connection=dict(CONN))
    client.put("/api/config/connection", json={"base_url": "https://new.example.com", "username": "bob",
                                               "app_password": ""}, headers=revision_headers(client, None, read_path='/api/config'))
    assert stored_config(config_path)["connection"] == {
        "base_url": "https://new.example.com", "username": "bob", "app_password": PASSWORD,
    }


def test_put_echoed_mask_keeps_existing(make_client, config_path, fake_pm):
    client = make_client(connection=dict(CONN))
    masked = client.get("/api/config").json()["app_password_masked"]
    client.put("/api/config/connection", json={**CONN, "app_password": masked}, headers=revision_headers(client, None, read_path='/api/config'))
    assert stored_config(config_path)["connection"]["app_password"] == PASSWORD


def test_put_new_password_overwrites(make_client, config_path, fake_pm):
    client = make_client(connection=dict(CONN))
    client.put("/api/config/connection", json={**CONN, "app_password": "Brand-New-App-Pass"}, headers=revision_headers(client, None, read_path='/api/config'))
    assert stored_config(config_path)["connection"]["app_password"] == "Brand-New-App-Pass"


def test_put_can_clear_url_and_username(make_client, config_path, fake_pm):
    client = make_client(connection=dict(CONN))
    client.put("/api/config/connection", json={"base_url": "", "username": "", "app_password": ""}, headers=revision_headers(client, None, read_path='/api/config'))
    assert stored_config(config_path)["connection"] == {"base_url": "", "username": "", "app_password": PASSWORD}


@pytest.mark.parametrize(
    "bad",
    ["cloud.example.com", "ftp://cloud", "http://", "https://alice:pw@cloud.example.com",
     "https://cloud.example.com/?x=1", "https://cloud.example.com/#frag", "https://cloud example.com",
     "https://cloud.example.com:99999"],
)
def test_put_rejects_bad_url(make_client, config_path, fake_pm, bad):
    client = make_client(connection={"base_url": "https://keep.example.com"})
    resp = client.put("/api/config/connection", json={"base_url": bad}, headers=revision_headers(client, None, read_path='/api/config'))
    assert resp.status_code == 400
    assert stored_config(config_path)["connection"]["base_url"] == "https://keep.example.com"
    assert fake_pm.calls == 0


@pytest.mark.parametrize("bad", ["ali:ce", "ali\nce"])
def test_put_rejects_bad_username(make_client, fake_pm, bad):
    client = make_client(connection={})
    resp = client.put("/api/config/connection", json={"base_url": BASE_URL, "username": bad}, headers=revision_headers(client, None, read_path='/api/config'))
    assert resp.status_code == 400


def test_put_restart_failure_still_saves(make_client, config_path, monkeypatch):
    pm = _FakePM(running=True, ok=False)
    monkeypatch.setattr(config_router, "get_process_manager", lambda: pm)
    client = make_client(connection={})
    body = client.put("/api/config/connection", json=CONN, headers=revision_headers(client, None, read_path='/api/config')).json()
    assert body["success"] is True and body["restarted"] is False and "啟動失敗" in body["message"]
    assert stored_config(config_path)["connection"]["base_url"] == BASE_URL


def test_put_without_restart(make_client, fake_pm):
    client = make_client(connection={})
    body = client.put("/api/config/connection", json={**CONN, "restart": False}, headers=revision_headers(client, None, read_path='/api/config')).json()
    assert body["restarted"] is False and fake_pm.calls == 0


def test_put_never_logs_password_and_registers_it_for_redaction(make_client, fake_pm, caplog):
    caplog.set_level(logging.DEBUG)
    client = make_client(connection={})
    client.put("/api/config/connection", json=CONN, headers=revision_headers(client, None, read_path='/api/config'))
    assert PASSWORD not in caplog.text
    assert log_redaction.registered_secret_count() == 2  # 本身＋HTTP Basic base64
    logging.getLogger("mcp_admin_core.process").info("[mcp-server] leaked %s", PASSWORD)
    assert PASSWORD not in caplog.text and "leaked ***" in caplog.text


# -- 沒宣告的欄位回 422、不默默忽略（以前回 200 什麼都沒存；MCP:Hermes 2026-11-05 同型）-----------

SECRET = "nextcloud-canary-c48d02-secret"


def test_put_gui_payload_accepted(make_client, config_path, fake_pm):
    """Connection 頁送 base_url／username／app_password 三欄、不送 restart（預設套用）；用正式組裝的 main.app。"""
    client = make_client(connection={}, app="main")
    gui = dict(CONN)
    assert set(gui) == gui_connection_fields("nextcloud")
    resp = client.put("/api/config/connection", json=gui, headers=revision_headers(client, None, read_path='/api/config'))
    assert resp.status_code == 200, resp.text
    assert stored_config(config_path)["connection"] == CONN
    assert fake_pm.calls == 1


def test_put_misspelled_password_field_rejected_without_echo_or_effect(make_client, config_path, fake_pm):
    """把 app_password 寫成 password：422、不回顯密碼、不存檔也不套用。"""
    client = make_client(connection={}, app="main")
    headers = revision_headers(client, None, read_path='/api/config')
    before = config_path.read_bytes()
    resp = client.put("/api/config/connection", headers=headers,
                      json={"base_url": BASE_URL, "username": LOGIN, "password": SECRET})
    assert_unknown_field_rejected(resp, "password", SECRET)
    assert config_path.read_bytes() == before
    assert fake_pm.calls == 0


# -- POST /test ----------------------------------------------------------------


def test_test_uses_saved_values_and_ignores_body(make_client, fake_pm, monkeypatch):
    seen = {}

    async def fake_check(base, user, password, **_):
        seen.update(base=base, user=user, password=password)
        return {"healthy": True, "auth_ok": True, "item_count": 3, "truncated": False, "version": "35.0.1"}

    monkeypatch.setattr(health_probes, "check_nextcloud", fake_check)
    client = make_client(connection=dict(CONN))
    body = client.post("/api/config/test", json={"base_url": "http://evil:1", "app_password": "x"}).json()
    assert seen == {"base": BASE_URL, "user": LOGIN, "password": PASSWORD}
    assert body["success"] is True
    assert "35.0.1" in body["message"] and "第一層 3 項" in body["message"]
    assert PASSWORD not in str(body)


def test_test_reports_rejected_credentials(make_client, fake_pm, monkeypatch):
    async def fake_check(base, user, password, **_):
        return {"healthy": False, "auth_ok": False, "error": "Nextcloud rejected the username or app password."}

    monkeypatch.setattr(health_probes, "check_nextcloud", fake_check)
    body = make_client(connection=dict(CONN)).post("/api/config/test").json()
    assert body["success"] is False and "rejected" in body["message"]
    assert body["details"]["auth_ok"] is False


def test_test_without_url(make_client, fake_pm):
    body = make_client(connection={}).post("/api/config/test").json()
    assert body["success"] is False and "base_url" in body["message"]


def test_test_end_to_end_against_fake(make_client, fake_pm, monkeypatch, authed):
    """不 mock check_nextcloud：用 fake Nextcloud 走完 OCS＋PROPFIND。"""
    real = health_probes.check_nextcloud

    async def with_transport(base, user, password, **_):
        return await real(base, user, password, transport=authed.transport)

    monkeypatch.setattr(health_probes, "check_nextcloud", with_transport)
    body = make_client(connection=dict(CONN)).post("/api/config/test").json()
    assert body["success"] is True, body
    assert body["details"]["item_count"] == 3 and body["details"]["version"] == "35.0.1"
