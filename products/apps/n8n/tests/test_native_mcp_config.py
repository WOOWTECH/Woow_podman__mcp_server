"""Exercise persisted native MCP token configuration through the real admin API."""
from __future__ import annotations

from connection_forms import assert_unknown_field_rejected, gui_connection_fields
from management_client import revision_headers

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import pytest

from mcp_admin_core.launch_policy import set_launch_policy
from mcp_admin_core.process import _build_subprocess_env
from n8n_mcp_admin.main import app, LAUNCH_POLICY

NATIVE_TOKEN = "synthetic-native-token-0123456789-canary"
# 存檔會以子行程的 SSRF 規則檢查網址（含 DNS 解析）：用不需要 DNS 的公開位址字面值（TEST-NET-3）。
N8N_URL = "https://203.0.113.10"


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    """Authenticate a fresh local app/store; never launch the upstream process."""
    import mcp_admin_core.config.store as store_mod
    from n8n_mcp_admin import tool_registry

    path = tmp_path / "config.json"
    cfg = json.loads(json.dumps(store_mod._DEFAULT_CONFIG))
    cfg["connection"] = {"n8n_api_url": N8N_URL, "n8n_api_key": "synthetic-rest-key"}
    path.write_text(json.dumps(cfg))
    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(path))
    monkeypatch.setattr(store_mod, "_instance", None)

    async def no_refresh():
        """Avoid an npm/network lookup in this API security test."""
        return None

    monkeypatch.setattr(tool_registry, "refresh", no_refresh)
    set_launch_policy(LAUNCH_POLICY)
    c = TestClient(app)
    c.headers["Authorization"] = "Bearer " + c.post("/api/auth/login", json={"password": "admin"}).json()["token"]
    yield c, path
    set_launch_policy(None)


def save(c: TestClient, **updates):
    """Save using the actual API; restart=false isolates config behavior."""
    return c.put("/api/config/connection", json={"n8n_api_url": N8N_URL, "n8n_api_key": "", "restart": False, **updates}, headers=revision_headers(c, None, read_path='/api/config'))


def test_native_token_saved_masked_and_passed_to_child(client) -> None:
    """The product path can use the separate native token without returning it."""
    c, path = client
    r = save(c, n8n_mcp_access_token="  " + NATIVE_TOKEN + "  ")
    assert r.status_code == 200
    cfg = json.loads(path.read_text())
    assert cfg["connection"]["n8n_mcp_access_token"] == NATIVE_TOKEN
    assert NATIVE_TOKEN not in r.text
    body = c.get("/api/config")
    assert body.status_code == 200 and NATIVE_TOKEN not in body.text
    assert "*" in body.json()["n8n_mcp_access_token_masked"]
    for endpoint in ("/api/settings", "/api/settings/connection"):
        response = c.get(endpoint)
        assert response.status_code == 200 and NATIVE_TOKEN not in response.text
        settings = response.json()["connection"] if endpoint == "/api/settings" else response.json()
        assert "*" in settings["n8n_mcp_access_token"]
    env = _build_subprocess_env(cfg["connection"], cfg["mcp_server"], LAUNCH_POLICY, cfg)
    assert env["N8N_MCP_ACCESS_TOKEN"] == NATIVE_TOKEN
    assert env["N8N_API_KEY"] == "synthetic-rest-key"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_omitted_or_blank_native_token_preserves_existing(client, value) -> None:
    """Unrelated settings saves must not erase the credential."""
    c, path = client
    assert save(c, n8n_mcp_access_token=NATIVE_TOKEN).status_code == 200
    updates = {} if value is None else {"n8n_mcp_access_token": value}
    assert save(c, **updates).status_code == 200
    assert json.loads(path.read_text())["connection"]["n8n_mcp_access_token"] == NATIVE_TOKEN


def test_explicit_clear_removes_native_token_and_resets_mask(client) -> None:
    """Clear is explicit and also prevents inherited env from restoring the token."""
    c, path = client
    assert save(c, n8n_mcp_access_token=NATIVE_TOKEN).status_code == 200
    assert save(c, clear_n8n_mcp_access_token=True).status_code == 200
    cfg = json.loads(path.read_text())
    assert cfg["connection"]["n8n_mcp_access_token"] == ""
    assert c.get("/api/config").json()["n8n_mcp_access_token_masked"] == "(not set)"
    assert _build_subprocess_env(cfg["connection"], cfg["mcp_server"], LAUNCH_POLICY, cfg)["N8N_MCP_ACCESS_TOKEN"] == ""


def test_native_token_read_and_write_require_admin_auth(client) -> None:
    """Unauthenticated requests neither see the masked config nor change it."""
    c, path = client
    before = path.read_bytes()
    c.headers.pop("Authorization")
    c.cookies.clear()
    assert c.get("/api/config").status_code == 401
    # Authentication negative: raw request must reach auth without a helper GET.
    assert c.put('/api/config/connection', json={'n8n_mcp_access_token': NATIVE_TOKEN}).status_code == 401
    assert path.read_bytes() == before


def test_native_token_support_does_not_allow_arbitrary_child_env() -> None:
    """Adding this one connection key keeps the existing launch allowlist intact."""
    env = _build_subprocess_env(
        {"n8n_mcp_access_token": NATIVE_TOKEN, "clear_n8n_mcp_access_token": True, "webhook_security_mode": "permissive", "node_options": "--inspect"},
        {"env": {"N8N_MCP_ACCESS_TOKEN": "untrusted override", "USE_FIXED_HTTP": "true"}},
        LAUNCH_POLICY,
    )
    assert env["N8N_MCP_ACCESS_TOKEN"] == NATIVE_TOKEN
    assert "WEBHOOK_SECURITY_MODE" not in env and "USE_FIXED_HTTP" not in env
    assert "CLEAR_N8N_MCP_ACCESS_TOKEN" not in env


# ---------------------------------------------------------------------------
# 沒宣告的欄位回 422、不默默忽略（以前回 200 什麼都沒存；MCP:Hermes 2026-11-05 同型）
# ---------------------------------------------------------------------------
SECRET = "n8n-canary-6e1b8d-secret"


@pytest.fixture
def apply(monkeypatch):
    """子行程的套用（restart／start）換成 AsyncMock：不啟動 n8n-mcp。"""
    from n8n_mcp_admin.routers import config as config_mod

    action = AsyncMock(return_value=True)
    monkeypatch.setattr(config_mod, "get_process_manager", lambda: SimpleNamespace(
        is_running=True, restart=action, start=action, start_or_restart=action))
    return action


def test_gui_payload_is_accepted(client, apply) -> None:
    """Connection 頁送六欄（清除 token 的下拉送字串 "false"、兩個數字欄送數字）、不送 restart（預設套用）。"""
    c, path = client
    gui = {"n8n_api_url": N8N_URL, "n8n_api_key": "synthetic-gui-rest-key", "n8n_mcp_access_token": "",
           "clear_n8n_mcp_access_token": "false", "mcp_session_timeout": 900, "mcp_max_sessions": 20}
    assert set(gui) == gui_connection_fields("n8n")
    r = c.put("/api/config/connection", json=gui, headers=revision_headers(c, None, read_path='/api/config'))
    assert r.status_code == 200, r.text
    assert json.loads(path.read_text())["connection"] == {
        "n8n_api_url": N8N_URL, "n8n_api_key": "synthetic-gui-rest-key",
        "mcp_session_timeout": "900", "mcp_max_sessions": "20"}
    apply.assert_awaited_once()


def test_misspelled_api_key_field_is_rejected_without_echo_or_effect(client, apply) -> None:
    """n8n_api_token（拼錯）＋缺必填的 n8n_api_key：422；缺欄位那筆的 input 是整個 body，也不回顯 key。"""
    c, path = client
    headers = revision_headers(c, None, read_path='/api/config')
    before = path.read_bytes()
    r = c.put("/api/config/connection", headers=headers,
              json={"n8n_api_url": N8N_URL, "n8n_api_token": SECRET, "restart": False})
    assert_unknown_field_rejected(r, "n8n_api_token", SECRET)
    assert {"type": "missing", "loc": ["body", "n8n_api_key"], "msg": "Field required"} in r.json()["detail"]
    assert path.read_bytes() == before
    apply.assert_not_awaited()
