"""main.py 組裝：core factory、/healthz、四個 extra router、新安裝全開（既有政策不動）、政策放寬時重啟、祕密不進 log。"""
from __future__ import annotations

from management_client import revision_headers

import json

import pytest
from fastapi.testclient import TestClient

from nextcloud_mcp_admin import tool_policy, tool_registry

from .conftest import CONN, stored_config
from .fake_nextcloud import PASSWORD

WRITE_DELETE = tool_registry.write_and_dangerous_tool_names()


def _collect_paths(routes) -> set[str]:
    paths: set[str] = set()
    for r in routes:
        p = getattr(r, "path", None)
        if p:
            paths.add(p)
        orig = getattr(r, "original_router", None)
        if orig is not None:
            paths |= _collect_paths(orig.routes)
    return paths


@pytest.mark.parametrize(("connection", "mcp"), [({}, "not_configured"), (CONN, "not_running")])
def test_healthz_ok_without_login(make_client, connection, mcp):
    """/healthz 恆 200（k8s probe 只看狀態碼）；mcp 是給平台的粗略狀態：連線三個欄位沒填＝not_configured。

    沒有 backend 欄位：三個欄位都是必要欄位（required_connection），不另外宣告後端連線欄位（第五輪）。"""
    from mcp_admin_core.launch_policy import get_launch_policy, set_launch_policy

    from nextcloud_mcp_admin.main import LAUNCH_POLICY

    client = make_client(connection=connection, app="main")
    client.headers.pop("Authorization")
    previous = get_launch_policy()
    set_launch_policy(LAUNCH_POLICY)  # 別的測試以 create_app() 組 app 時會把全域政策換掉
    try:
        resp = client.get("/healthz")
    finally:
        set_launch_policy(previous)
    assert resp.status_code == 200 and resp.json() == {"status": "ok", "mcp": mcp}


def test_routes_mounted():
    from nextcloud_mcp_admin.main import app

    paths = _collect_paths(app.routes)
    for p in ("/healthz", "/api/config", "/api/config/connection", "/api/config/test", "/api/tools",
              "/api/tools/operations", "/api/health", "/api/logs/stream", "/api/logs/search", "/api/tokens",
              "/api/settings", "/private_{token}/{path:path}"):
        assert p in paths, p
    assert app.title == "Nextcloud MCP Admin"


def test_tools_api_serves_nine_with_one_dangerous(make_client):
    body = make_client(app="main").get("/api/tools").json()
    assert body["total"] == 9
    assert [t["name"] for t in body["tools"] if t["dangerous"]] == ["delete_file_checked"]


def test_tools_disable_is_persisted_for_proxy(make_client, config_path):
    client = make_client(app="main")
    assert client.put("/api/tools", json={"disabled_tools": ["upload_file"]}, headers=revision_headers(client, None, read_path='/api/tools')).status_code == 200
    assert stored_config(config_path)["tools"]["disabled"] == ["upload_file"]


def test_health_api_shape_without_secret(make_client, monkeypatch):
    from nextcloud_mcp_admin import health_probes

    async def fake_check(base, user, password, **_):
        return {"healthy": True, "url": base, "version": "35.0.1", "item_count": 3}

    monkeypatch.setattr(health_probes, "check_nextcloud", fake_check)
    monkeypatch.setattr(health_probes, "_last_probe", None)
    body = make_client(app="main", connection=dict(CONN)).get("/api/health").json()
    assert body["app_type"] == "nextcloud" and body["target_app"]["healthy"] is True
    assert body["version"] == "35.0.1" and body["item_count"] == 3 and body["db_name"] == CONN["username"]
    assert PASSWORD not in json.dumps(body)


# -- seed defaults（新安裝全開；既有服務的停用清單與標記不動） --------------------------------


async def _apply(make_client, **cfg):
    make_client(**cfg)  # 只為了把 store singleton 指到暫存 config
    from nextcloud_mcp_admin import seed_defaults

    return await seed_defaults.apply_seed_defaults()


async def test_seed_defaults_fresh_config(make_client, config_path):
    """沒有標記＝從未啟動過的新安裝：只寫上標記，9 支全開（負責人 2026-10-25「預設全開」）。"""
    changed = await _apply(make_client)
    assert changed == ["tools.disabled", "env_prefix"]
    cfg = stored_config(config_path)
    assert cfg["tools"]["disabled"] == []
    assert cfg["tools"]["nextcloud_policy_seeded"] is True
    assert cfg["mcp_server"]["env_prefix"] == "NEXTCLOUD_MCP_"


async def test_seed_defaults_keep_existing_disabled(make_client, config_path):
    await _apply(make_client, tools={"disabled": ["list_tasks", "upload_file"], "disabled_operations": {}})
    assert stored_config(config_path)["tools"]["disabled"] == ["list_tasks", "upload_file"]


async def test_seed_defaults_never_reopen_existing_read_only_service(make_client, config_path):
    """2026-10-25 之前開通的服務（已有標記、出廠停用 4 支）：換新映像後停用清單與標記原封不動。"""
    changed = await _apply(
        make_client,
        tools={"disabled": list(WRITE_DELETE), "nextcloud_policy_seeded": True},
        mcp_server={"command": "nextcloud-mcp-server", "env_prefix": "NEXTCLOUD_MCP_"},
    )
    assert changed == []
    assert stored_config(config_path)["tools"]["disabled"] == WRITE_DELETE


async def test_seed_defaults_respect_tenant_unlock(make_client, config_path):
    changed = await _apply(
        make_client,
        tools={"disabled": [], "nextcloud_policy_seeded": True},
        mcp_server={"command": "nextcloud-mcp-server", "env_prefix": "NEXTCLOUD_MCP_"},
    )
    assert changed == []
    cfg = stored_config(config_path)
    assert cfg["tools"]["disabled"] == [] and cfg["mcp_server"]["command"] == "nextcloud-mcp-server"


async def test_seed_defaults_keep_custom_prefix(make_client, config_path):
    await _apply(make_client, mcp_server={"env_prefix": "CUSTOM_"})
    assert stored_config(config_path)["mcp_server"]["env_prefix"] == "CUSTOM_"


def test_lifespan_applies_seed_defaults_before_core(make_client, config_path):
    from nextcloud_mcp_admin.main import app

    make_client(mcp_server={"command": "", "env": {}, "env_prefix": ""})
    with TestClient(app) as client:  # 進 lifespan；command 空 → core 不 spawn 子行程
        assert client.get("/healthz").status_code == 200
    cfg = stored_config(config_path)
    assert cfg["tools"]["disabled"] == [] and cfg["tools"]["nextcloud_policy_seeded"] is True
    assert cfg["mcp_server"]["env_prefix"] == "NEXTCLOUD_MCP_"


def test_fresh_tools_page_shows_all_enabled(make_client):
    from nextcloud_mcp_admin.main import app

    client = make_client(app="main")
    with TestClient(app) as lifespan_client:
        lifespan_client.get("/healthz")
    body = client.get("/api/tools").json()
    assert body["enabled"] == 9 and body["disabled_tools"] == []


# -- Tools 政策放寬 → 重啟子行程 ------------------------------------------------------


class _FakePM:
    def __init__(self, running=True):
        self.is_running = running
        self.restarts = 0

    async def restart(self):
        self.restarts += 1
        return True


@pytest.fixture
def pm(monkeypatch):
    fake = _FakePM()
    monkeypatch.setattr(tool_policy, "get_process_manager", lambda: fake)
    return fake


def _seeded(make_client):
    return make_client(app="main", tools={"disabled": list(WRITE_DELETE), "nextcloud_policy_seeded": True})


def test_enabling_write_tool_restarts_child(make_client, pm, config_path):
    client = _seeded(make_client)
    remaining = [n for n in WRITE_DELETE if n != "create_text_file"]
    assert client.put("/api/tools", json={"disabled_tools": remaining}, headers=revision_headers(client, None, read_path='/api/tools')).status_code == 200
    assert pm.restarts == 1
    assert stored_config(config_path)["tools"]["nextcloud_policy_seeded"] is True  # 標記不被 core 洗掉


def test_disabling_more_tools_does_not_restart(make_client, pm):
    client = _seeded(make_client)
    client.put("/api/tools", json={"disabled_tools": WRITE_DELETE + ["list_tasks"]}, headers=revision_headers(client, None, read_path='/api/tools'))
    assert pm.restarts == 0


def test_unchanged_policy_does_not_restart(make_client, pm):
    client = _seeded(make_client)
    client.put("/api/tools", json={"disabled_tools": list(WRITE_DELETE)}, headers=revision_headers(client, None, read_path='/api/tools'))
    assert pm.restarts == 0


def test_widening_while_stopped_does_not_start(make_client, pm):
    pm.is_running = False
    client = _seeded(make_client)
    client.put("/api/tools", json={"disabled_tools": []}, headers=revision_headers(client, None, read_path='/api/tools'))
    assert pm.restarts == 0


def test_unauthenticated_put_does_not_restart(make_client, pm, config_path):
    client = _seeded(make_client)
    client.headers.pop("Authorization")
    client.cookies.clear()
    assert client.put("/api/tools", json={"disabled_tools": []}).status_code == 401
    assert pm.restarts == 0 and stored_config(config_path)["tools"]["disabled"] == WRITE_DELETE


def test_other_routes_pass_through(make_client, pm):
    client = _seeded(make_client)
    assert client.get("/api/tools").status_code == 200
    assert pm.restarts == 0


# -- log 衛生 ---------------------------------------------------------------------


def test_lifespan_registers_password_for_redaction(make_client):
    from nextcloud_mcp_admin import log_redaction
    from nextcloud_mcp_admin.main import app

    log_redaction.set_secrets(None)
    make_client(connection=dict(CONN), mcp_server={"command": ""})
    with TestClient(app) as client:
        client.get("/healthz")
    assert log_redaction.redact_secrets(f"x {PASSWORD} y") == "x *** y"
    log_redaction.set_secrets(None)
