"""settings router 共用碼補丁契約測試（Task 1 / 2 / 2b）。

涵蓋：
- Task 1：GET /api/settings 與 GET /api/settings/connection 對 connection
  祕密欄（含 kubeconfig heuristic、max_stars=8 上限）遮罩。
- Task 2：PUT /api/settings/connection 套 strip + drop_blank_secrets（淺合併）。
- Task 2b：PUT /api/settings/mcp_server 淺合併保留 env_prefix。

/api/settings* 受 AuthMiddleware 保護，故所有請求先登入取 token（預設密碼 admin）。
存檔後直接讀 config.json 檔案驗證真實存檔值（GET 會遮罩、無法驗 secret 明文）。
"""
from __future__ import annotations

from management_client import revision_headers

import json
from pathlib import Path

# 大於 1KB 的假 kubeconfig（含 client-key-data 型敏感內容）
BIG_KUBECONFIG = (
    "apiVersion: v1\n"
    "clusters:\n"
    "- cluster:\n"
    "    certificate-authority-data: " + ("A" * 1200) + "\n"
    "    server: https://10.0.0.1:6443\n"
    "  name: prod\n"
    "contexts:\n"
    "- context:\n"
    "    cluster: prod\n"
    "    user: admin\n"
    "  name: prod\n"
    "current-context: prod\n"
    "users:\n"
    "- name: admin\n"
    "  user:\n"
    "    client-key-data: " + ("K" * 800) + "\n"
)


def _login(c) -> str:
    return c.post("/api/auth/login", json={"password": "admin"}).json()["token"]


def _auth(c) -> dict[str, str]:
    return {"Authorization": f"Bearer {_login(c)}"}


def _read_config(path: Path) -> dict:
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# Task 1：GET 遮罩
# ---------------------------------------------------------------------------


def test_get_settings_masks_kubeconfig(make_app):
    c = make_app({"connection": {"kubeconfig": BIG_KUBECONFIG, "in_cluster": "false"}})
    h = _auth(c)
    r = c.get("/api/settings", headers=h)
    assert r.status_code == 200
    conn = r.json()["connection"]
    masked = conn["kubeconfig"]
    # 不含原文、含星號、且長度有界（max_stars=8 → 2 + 8 + 2）
    assert BIG_KUBECONFIG not in masked
    assert "A" * 1200 not in masked
    assert "*" in masked
    assert len(masked) <= 2 + 8 + 2
    # 非祕密欄原樣
    assert conn["in_cluster"] == "false"


def test_get_section_connection_masked(make_app):
    c = make_app(
        {
            "connection": {
                "kubeconfig": BIG_KUBECONFIG,
                "odoo_api_key": "supersecretapikey1234",
                "in_cluster": "false",
            }
        }
    )
    h = _auth(c)
    r = c.get("/api/settings/connection", headers=h)
    assert r.status_code == 200
    conn = r.json()
    assert BIG_KUBECONFIG not in conn["kubeconfig"]
    assert "*" in conn["kubeconfig"]
    assert len(conn["kubeconfig"]) <= 2 + 8 + 2
    # 既有 key heuristic 也套上
    assert conn["odoo_api_key"] != "supersecretapikey1234"
    assert "*" in conn["odoo_api_key"]
    assert conn["in_cluster"] == "false"


def test_get_section_non_connection_unchanged(make_app):
    c = make_app({"proxy": {"timeout": 12345}})
    h = _auth(c)
    r = c.get("/api/settings/proxy", headers=h)
    assert r.status_code == 200
    assert r.json() == {"timeout": 12345}


def test_settings_masks_proxy_credentials_and_roundtrips_masked_proxy(make_app, tmp_config_path):
    mcp_token = "mcp-auth-token-for-settings-regression"
    bearer_token = "proxy-bearer-token-for-settings-regression"
    c = make_app({"mcp_auth_token": mcp_token, "proxy": {"timeout": 12345, "bearer_token": bearer_token}})
    h = _auth(c)

    full = c.get("/api/settings", headers=h)
    assert full.status_code == 200
    settings = full.json()
    assert mcp_token not in full.text
    assert settings["mcp_auth_token"] != mcp_token
    assert "*" in settings["mcp_auth_token"]
    token_section = c.get("/api/settings/mcp_auth_token", headers=h)
    assert mcp_token not in token_section.text
    assert "*" in token_section.json()["value"]
    assert bearer_token not in full.text
    assert settings["proxy"]["bearer_token"] != bearer_token
    assert "*" in settings["proxy"]["bearer_token"]

    section = c.get("/api/settings/proxy", headers=h)
    assert section.status_code == 200
    assert bearer_token not in section.text
    assert "*" in section.json()["bearer_token"]

    # Reposting the masked proxy section must preserve the stored credential.
    saved = c.put("/api/settings/proxy", json=section.json(), headers=revision_headers(c, h, read_path='/api/settings'))
    assert saved.status_code == 200
    assert _read_config(tmp_config_path)["proxy"] == {"timeout": 12345, "bearer_token": bearer_token}

    # Both the empty UI value and an all-mask placeholder mean "keep the stored value".
    blank_saved = c.put(
        "/api/settings/proxy", json={"timeout": 12346, "bearer_token": ""}, headers=revision_headers(c, h, read_path='/api/settings'),
    )
    assert blank_saved.status_code == 200
    persisted = _read_config(tmp_config_path)["proxy"]
    assert persisted == {"timeout": 12346, "bearer_token": bearer_token}


def test_settings_masks_short_proxy_secret_as_stars_and_roundtrips_it(make_app, tmp_config_path):
    bearer_token = "tiny"
    c = make_app({"proxy": {"timeout": 12345, "bearer_token": bearer_token}})
    h = _auth(c)
    section = c.get("/api/settings/proxy", headers=h)
    assert section.json()["bearer_token"] == "****"
    assert c.put("/api/settings/proxy", json=section.json(), headers=revision_headers(c, h, read_path='/api/settings')).status_code == 200
    assert _read_config(tmp_config_path)["proxy"]["bearer_token"] == bearer_token


def test_settings_secret_reveal_is_explicit_and_preserves_auth_material(make_app):
    mcp_token = "mcp-auth-token-for-reveal-regression"
    bearer_token = "proxy-bearer-token-for-reveal-regression"
    c = make_app({"mcp_auth_token": mcp_token, "proxy": {"bearer_token": bearer_token}})
    h = _auth(c)

    token = c.post("/api/settings/mcp_auth_token/reveal", headers=h)
    bearer = c.post("/api/settings/proxy/bearer_token/reveal", headers=h)
    assert token.status_code == 200
    assert token.json()["value"] == mcp_token
    assert bearer.status_code == 200
    assert bearer.json()["value"] == bearer_token


def test_settings_mask_reveal_and_revoke_share_current_token_state(make_app):
    original = "mcp-auth-token-before-revocation"
    c = make_app({"mcp_auth_token": original})
    h = _auth(c)
    assert c.post("/api/tokens/revoke", headers=revision_headers(c, h, read_path='/api/settings')).status_code == 200
    settings = c.get("/api/settings", headers=h).json()
    assert settings["mcp_auth_token"] == settings["mcp_auth_token_masked"] == ""
    assert settings["mcp_auth_token_set"] is False
    revealed = c.post("/api/settings/mcp_auth_token/reveal", headers=h)
    assert revealed.json() == {"value": ""}
    assert revealed.headers["cache-control"] == "no-store"
    replacement = c.post("/api/tokens/rotate", headers=revision_headers(c, h, read_path='/api/settings')).json()["token"]
    assert replacement != original
    settings_response = c.get("/api/settings", headers=h)
    assert replacement not in settings_response.text
    assert settings_response.json()["mcp_auth_token_set"] is True
    assert c.post("/api/settings/mcp_auth_token/reveal", headers=h).json()["value"] == replacement


# ---------------------------------------------------------------------------
# Task 2：PUT connection strip + drop_blank_secrets
# ---------------------------------------------------------------------------


def test_put_section_connection_preserves_blank_secrets(make_app, tmp_config_path):
    c = make_app(
        {
            "connection": {
                "kubeconfig": "SECRET-YAML",
                "odoo_api_key": "K1",
                "in_cluster": "false",
            }
        }
    )
    h = _auth(c)
    r = c.put(
        "/api/settings/connection",
        json={"kubeconfig": "", "odoo_api_key": "", "in_cluster": "true"},
        headers=revision_headers(c, h, read_path='/api/settings'),
    )
    assert r.status_code == 200
    conn = _read_config(tmp_config_path)["connection"]
    # 留空 secret 沿用既有值
    assert conn["kubeconfig"] == "SECRET-YAML"
    assert conn["odoo_api_key"] == "K1"
    # 非 secret 欄更新
    assert conn["in_cluster"] == "true"


def test_put_section_connection_strips_whitespace(make_app, tmp_config_path):
    c = make_app({"connection": {"in_cluster": "false"}})
    h = _auth(c)
    r = c.put("/api/settings/connection", json={"in_cluster": "  true  "}, headers=revision_headers(c, h, read_path='/api/settings'))
    assert r.status_code == 200
    conn = _read_config(tmp_config_path)["connection"]
    assert conn["in_cluster"] == "true"


def test_put_section_other_sections_full_replace(make_app, tmp_config_path):
    # proxy section 走全量替換：seed 多一個 custom 鍵，PUT 不帶它 → 應被移除
    c = make_app({"proxy": {"timeout": 86400, "custom": "keepme?"}})
    h = _auth(c)
    r = c.put("/api/settings/proxy", json={"timeout": 5}, headers=revision_headers(c, h, read_path='/api/settings'))
    assert r.status_code == 200
    proxy = _read_config(tmp_config_path)["proxy"]
    assert proxy.get("timeout") == 5
    assert "custom" not in proxy


# ---------------------------------------------------------------------------
# #1530：PUT mcp_server — command／args／port／env_prefix 唯讀，env 只收可編輯鍵
# （沒有 app 政策時：可編輯＝不在黑名單；有政策的情境見 test_launch_policy.py）
# ---------------------------------------------------------------------------

_SEEDED_MCP = {
    "command": "old",
    "args": ["--port", "8000"],
    "port": 8000,
    "env": {"OLD": "1"},
    "env_prefix": "K8S_MCP_",
}


def test_put_mcp_server_same_locked_values_updates_env(make_app, tmp_config_path):
    c = make_app({"mcp_server": dict(_SEEDED_MCP)})
    h = _auth(c)
    # 模擬共用 SettingsPage 實際送出：command/args/port/env 四鍵，只改 env
    r = c.put(
        "/api/settings/mcp_server",
        json={"command": "old", "args": ["--port", "8000"], "port": 8000, "env": {"OLD": "1", "A": "B"}},
        headers=revision_headers(c, h, read_path='/api/settings'),
    )
    assert r.status_code == 200
    mcp = _read_config(tmp_config_path)["mcp_server"]
    assert mcp["env_prefix"] == "K8S_MCP_"
    assert mcp["command"] == "old"
    assert mcp["args"] == ["--port", "8000"]
    assert mcp["env"] == {"OLD": "1", "A": "B"}


def test_put_mcp_server_rejects_command_change(make_app, tmp_config_path):
    c = make_app({"mcp_server": dict(_SEEDED_MCP)})
    r = c.put("/api/settings/mcp_server", json={"command": "sh", "args": ["-c", "id"]}, headers=revision_headers(c, _auth(c), read_path='/api/settings'))
    assert r.status_code == 400
    assert _read_config(tmp_config_path)["mcp_server"]["command"] == "old"


def test_put_mcp_server_rejects_args_port_and_env_prefix_change(make_app, tmp_config_path):
    c = make_app({"mcp_server": dict(_SEEDED_MCP)})
    h = _auth(c)
    for body in ({"args": ["--x"]}, {"port": 9000}, {"env_prefix": "X_"}, {"unknown": 1}):
        r = c.put("/api/settings/mcp_server", json=body, headers=revision_headers(c, h, read_path='/api/settings'))
        assert r.status_code == 400, body
    assert _read_config(tmp_config_path)["mcp_server"] == {**_SEEDED_MCP}


def test_put_mcp_server_rejects_blocked_env(make_app, tmp_config_path):
    c = make_app({"mcp_server": dict(_SEEDED_MCP)})
    h = _auth(c)
    for key in ("NODE_OPTIONS", "LD_PRELOAD", "PYTHONPATH", "PATH", "WEBHOOK_SECURITY_MODE", "BAD=NAME"):
        r = c.put("/api/settings/mcp_server", json={"env": {key: "x"}}, headers=revision_headers(c, h, read_path='/api/settings'))
        assert r.status_code == 400, key
    assert _read_config(tmp_config_path)["mcp_server"]["env"] == {"OLD": "1"}


def test_put_mcp_server_cannot_drop_seeded_env(make_app, tmp_config_path):
    # 不帶某個鍵不等於刪除（部署 seed 的 env 只能由部署改）
    c = make_app({"mcp_server": dict(_SEEDED_MCP)})
    r = c.put("/api/settings/mcp_server", json={"env": {"A": "B"}}, headers=revision_headers(c, _auth(c), read_path='/api/settings'))
    assert r.status_code == 200
    assert _read_config(tmp_config_path)["mcp_server"]["env"] == {"OLD": "1", "A": "B"}


def test_put_unwritable_sections_rejected(make_app, tmp_config_path):
    c = make_app({"mcp_auth_token": "tok"})
    h = _auth(c)
    for section in ("mcp_auth_token", "token_history", "anything"):
        r = c.put(f"/api/settings/{section}", json={"value": "x"}, headers=revision_headers(c, h))
        assert r.status_code == 400, section
    assert _read_config(tmp_config_path)["mcp_auth_token"] == "tok"


def test_put_connection_rejects_blocked_env_names(make_app, tmp_config_path):
    c = make_app({"connection": {"odoo_url": "http://odoo:8069"}})
    h = _auth(c)
    for key in ("node_options", "ld_preload", "webhook_security_mode", "path"):
        r = c.put("/api/settings/connection", json={key: "x"}, headers=revision_headers(c, h, read_path='/api/settings'))
        assert r.status_code == 400, key
    assert _read_config(tmp_config_path)["connection"] == {"odoo_url": "http://odoo:8069"}


# ---------------------------------------------------------------------------
# #1350：存 connection／mcp_server 時，子行程未運行要 start、運行中才 restart
# ---------------------------------------------------------------------------


class _FakePM:
    def __init__(self, running: bool, ok: bool = True):
        self.is_running = running
        self._ok = ok
        self.calls: list[str] = []

    async def start_or_restart(self) -> bool:
        self.calls.append("restart" if self.is_running else "start")
        return self._ok


def _patch_settings_pm(monkeypatch, pm):
    from mcp_admin_core.routers import settings as settings_mod

    monkeypatch.setattr(settings_mod, "get_process_manager", lambda: pm)


def test_put_connection_starts_mcp_when_not_running(make_app, monkeypatch):
    pm = _FakePM(running=False)
    _patch_settings_pm(monkeypatch, pm)
    c = make_app()
    r = c.put("/api/settings/connection", json={"odoo_url": "http://odoo:8069"}, headers=revision_headers(c, _auth(c), read_path='/api/settings'))
    assert r.status_code == 200
    assert pm.calls == ["start"]
    assert "started" in r.json()["message"]


def test_put_connection_restarts_mcp_when_running(make_app, monkeypatch):
    pm = _FakePM(running=True)
    _patch_settings_pm(monkeypatch, pm)
    c = make_app()
    r = c.put("/api/settings/connection", json={"odoo_url": "http://odoo:8069"}, headers=revision_headers(c, _auth(c), read_path='/api/settings'))
    assert r.status_code == 200
    assert pm.calls == ["restart"]
    assert "restarted" in r.json()["message"]


def test_put_mcp_server_start_failure_still_saves(make_app, monkeypatch, tmp_config_path):
    pm = _FakePM(running=False, ok=False)
    _patch_settings_pm(monkeypatch, pm)
    c = make_app()
    r = c.put("/api/settings/mcp_server", json={"env": {"FEATURE_X": "1"}}, headers=revision_headers(c, _auth(c), read_path='/api/settings'))
    assert r.status_code == 200
    assert pm.calls == ["start"]
    assert "failed to start" in r.json()["message"]
    assert _read_config(tmp_config_path)["mcp_server"]["env"] == {"FEATURE_X": "1"}


def test_put_mcp_server_without_changes_does_not_touch_mcp(make_app, monkeypatch):
    pm = _FakePM(running=True)
    _patch_settings_pm(monkeypatch, pm)
    c = make_app()
    r = c.put("/api/settings/mcp_server", json={"command": "", "args": [], "port": 8000, "env": {}}, headers=revision_headers(c, _auth(c), read_path='/api/settings'))
    assert r.status_code == 200
    assert pm.calls == []


def test_put_other_section_does_not_touch_mcp(make_app, monkeypatch):
    pm = _FakePM(running=False)
    _patch_settings_pm(monkeypatch, pm)
    c = make_app()
    r = c.put("/api/settings/proxy", json={"timeout": 5}, headers=revision_headers(c, _auth(c), read_path='/api/settings'))
    assert r.status_code == 200
    assert pm.calls == []


# ---------------------------------------------------------------------------
# #1500：/api/settings/mcp/status 要把看門狗欄位傳出去
# （McpProcessStatus 是 response_model，沒宣告的鍵會被 FastAPI 靜默濾掉）
# ---------------------------------------------------------------------------


def test_mcp_status_exposes_auto_restart_fields(make_app, monkeypatch):
    from mcp_admin_core.process import get_process_manager

    c = make_app()
    h = _auth(c)
    r = c.get("/api/settings/mcp/status", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["auto_restart_count"] == 0
    assert body["auto_restart_pending"] is False
    assert body["next_auto_restart_in"] is None

    pm = get_process_manager()
    monkeypatch.setattr(pm, "_auto_restart_count", 3)
    assert c.get("/api/settings/mcp/status", headers=h).json()["auto_restart_count"] == 3
