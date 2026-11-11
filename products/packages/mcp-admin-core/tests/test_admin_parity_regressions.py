"""深度管理契約已證實缺陷：遮罩、顯式刪除、輪替、損壞設定與套用失敗。"""

from management_client import revision_headers
import json
from types import SimpleNamespace

import pytest

from mcp_admin_core.config.store import ConfigStore, ConfigUnavailable
from mcp_admin_core.launch_policy import LaunchPolicy, set_launch_policy
from mcp_admin_core.routers import settings


def auth(c):
    token = c.post("/api/auth/login", json={"password": "admin"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("section,value", [("admin_password", "admin"), ("mcp_auth_token", "test-token-value"),
    ("custom_secret", "keep-private"), ("admin_session_generation", "set-at-login")])
def test_generic_scalar_secrets_masked(make_app, section, value):
    c = make_app({section: value})
    response = c.get(f"/api/settings/{section}", headers=auth(c))
    assert response.status_code == 200
    assert response.json() == {"value": "********"}


def test_env_explicit_deletion_preserves_other_and_locked_keys(make_app, tmp_config_path, monkeypatch):
    async def applied():
        return True
    monkeypatch.setattr(settings, "get_process_manager", lambda: SimpleNamespace(is_running=True, start_or_restart=applied))
    c = make_app({"mcp_server": {"env": {"EDIT": "1", "KEEP": "2", "AUTH_TOKEN": "secret"}}})
    set_launch_policy(LaunchPolicy(command="fixed", env_keys=frozenset({"EDIT", "KEEP", "AUTH_TOKEN"}), editable_env_keys=frozenset({"EDIT", "KEEP"})))
    headers = auth(c)
    # Omission still means preserve; explicit deletion removes only an editable key.
    assert c.put("/api/settings/mcp_server", headers=revision_headers(c, headers, read_path='/api/settings'), json={"env": {"KEEP": "3"}}).status_code == 200
    response = c.put("/api/settings/mcp_server", headers=revision_headers(c, headers, read_path='/api/settings'), json={"env_delete": ["EDIT"]})
    assert response.json()["apply_state"] == "applied"
    assert json.loads(tmp_config_path.read_text())["mcp_server"]["env"] == {"KEEP": "3", "AUTH_TOKEN": "secret"}
    before = tmp_config_path.read_bytes()
    for payload in ({"env_delete": ["AUTH_TOKEN"]}, {"env_delete": ["KEEP"], "env": {"KEEP": "4"}},
                    {"env_delete": "KEEP"}, {"env_delete": [None]}):
        assert c.put("/api/settings/mcp_server", headers=revision_headers(c, headers, read_path='/api/settings'), json=payload).status_code == 400
        assert tmp_config_path.read_bytes() == before


@pytest.mark.parametrize("endpoint", ["/api/tokens/rotate", "/api/settings/mcp_auth_token/rotate", "/api/tokens"])
def test_rotation_retains_newest_five(make_app, tmp_config_path, endpoint):
    c = make_app({"mcp_auth_token": "initial-token-000000000"})
    headers = auth(c)
    last_old = None
    for i in range(7):
        last_old = json.loads(tmp_config_path.read_text())["mcp_auth_token"]
        if endpoint == "/api/tokens":
            response = c.put(endpoint, headers=revision_headers(c, headers), json={"token": f"fixture-token-number-{i:04d}"})
        else:
            response = c.post(endpoint, headers=revision_headers(c, headers))
        assert response.status_code == 200
    history = json.loads(tmp_config_path.read_text())["token_history"]
    assert len(history) == 5
    assert [h["rotated_at"] for h in history] == sorted([h["rotated_at"] for h in history], reverse=True)
    masked = history[0].get("previous_token_masked", history[0].get("token_masked"))
    assert masked.startswith(last_old[:2]) and masked.endswith(last_old[-2:])
    assert "initial" not in json.dumps(history)


@pytest.mark.parametrize("broken", ['{broken', 'null', '[]', '{"tools": []}', '{"admin_password": null}', '{"tools":{"disabled":false}}'])
async def test_corrupt_state_never_defaults_or_overwrites(tmp_config_path, broken):
    tmp_config_path.write_text(broken)
    store = ConfigStore(tmp_config_path)
    with pytest.raises(ConfigUnavailable):
        await store.load()
    with pytest.raises(ConfigUnavailable):
        await store.put("tools", {"disabled": []})
    assert tmp_config_path.read_text() == broken


async def test_reload_corruption_clears_old_cache_and_blocks_writes(fresh_store):
    await fresh_store.put("mcp_auth_token", "old-private-token")
    fresh_store.path.write_text("{broken")
    with pytest.raises(ConfigUnavailable):
        await fresh_store.reload()
    with pytest.raises(ConfigUnavailable):
        await fresh_store.get_admin_auth()
    with pytest.raises(ConfigUnavailable):
        await fresh_store.put("connection", {})
    assert fresh_store.path.read_text() == "{broken"


async def test_missing_existing_state_never_reseeds(fresh_store):
    fresh_store.path.unlink()
    with pytest.raises(ConfigUnavailable):
        await fresh_store.load()
    assert not fresh_store.path.exists()


def test_corruption_reports_503_without_default_login(make_app, tmp_config_path, monkeypatch):
    import mcp_admin_core.config.store as store_module
    c = make_app()
    headers = auth(c)
    tmp_config_path.write_text("{broken")
    monkeypatch.setattr(store_module, "_instance", ConfigStore(tmp_config_path))
    for response in (c.get("/api/settings", headers=headers), c.post("/api/auth/login", json={"password": "admin"})):
        assert response.status_code == 503
        assert response.json()["code"] == "state_unavailable"
    assert tmp_config_path.read_text() == "{broken"


@pytest.mark.parametrize("throws", [False, True])
def test_saved_settings_apply_failure_is_partial(make_app, tmp_config_path, monkeypatch, throws):
    async def fail():
        if throws:
            raise RuntimeError("private upstream details")
        return False
    monkeypatch.setattr(settings, "get_process_manager", lambda: SimpleNamespace(is_running=True, start_or_restart=fail))
    c = make_app()
    response = c.put("/api/settings/mcp_server", headers=revision_headers(c, auth(c), read_path='/api/settings'), json={"env": {"FEATURE": "1"}})
    body = response.json()
    assert body["saved"] is True and body["status"] == "partial" and body["apply_state"] == "failed"
    assert "private" not in response.text
    assert json.loads(tmp_config_path.read_text())["mcp_server"]["env"] == {"FEATURE": "1"}


@pytest.mark.parametrize("throws", [False, True])
def test_token_change_no_longer_depends_on_child_restart(make_app, tmp_config_path, monkeypatch, throws):
    import mcp_admin_core.process as process
    async def restart():
        if throws:
            raise RuntimeError("private details")
        return False
    monkeypatch.setattr(process, "get_process_manager", lambda: SimpleNamespace(is_running=True, restart=restart))
    c = make_app()
    body = c.post("/api/tokens/rotate", headers=revision_headers(c, auth(c), read_path='/api/settings')).json()
    assert body["status"] == "ok"
    assert body["token_state"] == "applied" and body["child_state"] == "unchanged"
    assert json.loads(tmp_config_path.read_text())["mcp_auth_token"] == body["token"]
    assert "private details" not in body["message"]
