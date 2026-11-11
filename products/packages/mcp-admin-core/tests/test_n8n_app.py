"""#1531：n8n MCP——Permissions 轉成上游 DISABLED_TOOL_OPERATIONS、Session 變數與單位、
子行程改跑 dist/mcp/index.js、工具清單由上游產生；#1530：測試連線套 SSRF 規則。

沙盒重現（woow_paas_platform#1531 證據）：停用 n8n_executions:delete 後經 MCP 仍刪得掉；子行程只拿到
MCP_SESSION_TIMEOUT／MCP_MAX_SESSIONS；入口是 deprecated 的 dist/http-server.js；清單 24 支（上游 28 支）。
"""
from __future__ import annotations

from management_client import revision_headers

import json
import os
import socket
from pathlib import Path

import pytest

from mcp_admin_core.launch_policy import set_launch_policy
from mcp_admin_core.process import _build_subprocess_env

from n8n_mcp_admin import child_env, launcher, tool_registry, upstream
from n8n_mcp_admin.main import LAUNCH_POLICY

UPSTREAM_28 = {
    "tools_documentation", "search_nodes", "get_node", "validate_node", "get_template", "search_templates",
    "validate_workflow", "n8n_create_workflow", "n8n_get_workflow", "n8n_update_full_workflow",
    "n8n_update_partial_workflow", "n8n_delete_workflow", "n8n_list_workflows", "n8n_validate_workflow",
    "n8n_autofix_workflow", "n8n_test_workflow", "n8n_executions", "n8n_evaluations", "n8n_health_check",
    "n8n_workflow_versions", "n8n_deploy_template", "n8n_manage_datatable", "n8n_manage_folders",
    "n8n_manage_credentials", "n8n_audit_instance", "n8n_manage_agents", "n8n_explore_node_resources",
    "n8n_list_catalog",
}


# ---------------------------------------------------------------------------
# 推導環境變數
# ---------------------------------------------------------------------------


def test_disabled_tool_operations_format():
    value = child_env.disabled_tool_operations(
        {"n8n_workflow_versions": ["Rollback", "delete", "delete"], "n8n_executions": ["delete"],
         "bad;tool": ["x"], "n8n_list_catalog": ["tags;evil"], "n8n_manage_folders": []}
    )
    assert value == "n8n_executions:delete;n8n_workflow_versions:delete,rollback"


@pytest.mark.parametrize(
    ("timeout", "minutes"),
    # 預設 900 秒（2026-10-26 起；名額滿時由閘道回收閒置 session，見 test_n8n_session_reclaim.py）
    [(None, "15"), (600, "10"), ("61", "2"), (60, "1"), (10, "1"), (999999, "1440"), ("abc", "15")],
)
def test_session_timeout_minutes(timeout, minutes):
    conn = {} if timeout is None else {"mcp_session_timeout": timeout}
    assert child_env.derived_env({"connection": conn})["SESSION_TIMEOUT_MINUTES"] == minutes


def test_max_sessions():
    assert child_env.derived_env({"connection": {"mcp_max_sessions": "2"}})["N8N_MCP_MAX_SESSIONS"] == "2"
    assert child_env.derived_env({})["N8N_MCP_MAX_SESSIONS"] == "20"
    assert child_env.derived_env({"connection": {"mcp_max_sessions": 0}})["N8N_MCP_MAX_SESSIONS"] == "1"


def test_child_env_uses_upstream_variable_names(monkeypatch):
    cfg = {
        "connection": {"n8n_api_url": "https://n8n.example.com", "n8n_api_key": "k",
                       "mcp_session_timeout": "600", "mcp_max_sessions": "2",
                       "webhook_security_mode": "permissive"},
        "mcp_server": {"env": {"AUTH_TOKEN": "t", "USE_FIXED_HTTP": "true"}},
        "tools": {"disabled_operations": {"n8n_executions": ["delete"]}},
    }
    env = _build_subprocess_env(cfg["connection"], cfg["mcp_server"], LAUNCH_POLICY, cfg)
    assert env["N8N_API_URL"] == "https://n8n.example.com"
    assert env["N8N_API_KEY"] == "k"
    assert env["AUTH_TOKEN"] == "t"
    assert env["SESSION_TIMEOUT_MINUTES"] == "10"
    assert env["N8N_MCP_MAX_SESSIONS"] == "2"
    assert env["DISABLED_TOOL_OPERATIONS"] == "n8n_executions:delete"
    for name in ("MCP_SESSION_TIMEOUT", "MCP_MAX_SESSIONS", "WEBHOOK_SECURITY_MODE", "USE_FIXED_HTTP"):
        assert name not in env


# ---------------------------------------------------------------------------
# launcher：dist/mcp/index.js（SingleSessionHTTPServer）
# ---------------------------------------------------------------------------


def test_launcher_execs_mcp_index_in_http_mode(monkeypatch):
    monkeypatch.setattr(upstream, "package_dir", lambda: "/usr/lib/node_modules/n8n-mcp")
    monkeypatch.setattr(launcher.os.path, "exists", lambda p: True)
    monkeypatch.setattr(launcher, "prepare_database", lambda *_: "/data/n8n-mcp/nodes.db")
    monkeypatch.setenv("USE_FIXED_HTTP", "true")
    captured: dict = {}

    def fake_execvpe(file, argv, env):
        captured.update(file=file, argv=argv, env=env)
        raise SystemExit(0)

    monkeypatch.setattr(launcher.os, "execvpe", fake_execvpe)
    with pytest.raises(SystemExit):
        launcher.main(["--port", "3000"])
    assert captured["argv"] == ["node", "/usr/lib/node_modules/n8n-mcp/dist/mcp/index.js"]
    env = captured["env"]
    assert env["MCP_MODE"] == "http" and env["PORT"] == "3000" and env["HOST"] == "127.0.0.1"
    assert "USE_FIXED_HTTP" not in env
    assert env["AUTH_RATE_LIMIT_MAX"] == launcher.AUTH_RATE_LIMIT_MAX_DEFAULT


def test_launcher_policy_matches_seed():
    assert LAUNCH_POLICY.build_command(3000) == ["n8n-mcp-http", "--port", "3000"]


# ---------------------------------------------------------------------------
# 工具清單
# ---------------------------------------------------------------------------


def test_fallback_registry_matches_upstream_28():
    names = {t.name for t in tool_registry.FALLBACK_TOOLS}
    assert names == UPSTREAM_28
    assert "n8n_generate_workflow" not in names
    ops = {t.name: t.operations for t in tool_registry.FALLBACK_TOOLS if t.operations}
    assert ops["n8n_executions"] == ["get", "list", "delete"]


def test_tools_from_catalog():
    catalog = {"version": "9.9.9", "tools": [
        {"name": "search_nodes", "group": "documentation", "description": "Search.\n\nMore", "annotations": {}},
        {"name": "n8n_executions", "group": "management", "description": "Manage executions",
         "annotations": {"destructiveHint": False}, "operation_param": "action",
         "operations": ["get", "list", "delete"], "destructive_operations": ["delete"]},
        {"name": "weird", "group": "unknown"},
    ]}
    tools = tool_registry.tools_from_catalog(catalog)
    assert [t.name for t in tools] == ["search_nodes", "n8n_executions"]
    assert tools[0].description == "Search." and tools[0].category.value == "Core Reference"
    assert tools[1].operations == ["get", "list", "delete"] and tools[1].dangerous is True


@pytest.fixture
def reset_registry(monkeypatch):
    monkeypatch.setattr(tool_registry, "TOOL_REGISTRY", list(tool_registry.FALLBACK_TOOLS))
    monkeypatch.setattr(tool_registry, "_loaded", False)
    monkeypatch.setattr(tool_registry, "UPSTREAM_VERSION", None)


async def test_refresh_replaces_registry_in_place(reset_registry, monkeypatch):
    ref = tool_registry.TOOL_REGISTRY

    async def fake_load():
        return {"version": "2.96.2", "tools": [{"name": "n8n_list_catalog", "group": "management",
                                                 "operations": ["projects", "tags"]}]}

    monkeypatch.setattr(tool_registry, "load_upstream_catalog", fake_load)
    await tool_registry.refresh()
    assert ref is tool_registry.TOOL_REGISTRY and [t.name for t in ref] == ["n8n_list_catalog"]
    assert tool_registry.UPSTREAM_VERSION == "2.96.2"


_REAL_PKG = os.environ.get("N8N_MCP_PACKAGE_DIR")


@pytest.mark.skipif(not _REAL_PKG or not Path(_REAL_PKG, "dist").is_dir(),
                    reason="needs N8N_MCP_PACKAGE_DIR pointing at an installed n8n-mcp")
async def test_real_upstream_catalog(reset_registry):
    catalog = await tool_registry.load_upstream_catalog()
    assert {t["name"] for t in catalog["tools"]} >= UPSTREAM_28


# ---------------------------------------------------------------------------
# 管理 API：Permissions、tools/operations、測試連線
# ---------------------------------------------------------------------------


class _PM:
    def __init__(self):
        self.calls = 0
        self.is_running = True

    async def start_or_restart(self):
        self.calls += 1
        return True


@pytest.fixture
def n8n_client(tmp_config_path, monkeypatch, reset_registry):
    import mcp_admin_core.config.store as store_mod
    from fastapi.testclient import TestClient
    from mcp_admin_core.routers import tools as tools_mod

    from n8n_mcp_admin.routers import config as config_mod

    full = json.loads(json.dumps(store_mod._DEFAULT_CONFIG))
    full["connection"] = {"n8n_api_url": "http://10.43.5.6:5678", "n8n_api_key": "k"}
    tmp_config_path.write_text(json.dumps(full))
    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(tmp_config_path))
    monkeypatch.setattr(store_mod, "_instance", None)
    store_mod.get_config_store()

    async def no_refresh():
        return None

    monkeypatch.setattr(tool_registry, "refresh", no_refresh)
    pm = _PM()
    monkeypatch.setattr(config_mod, "get_process_manager", lambda: pm)
    monkeypatch.setattr(tools_mod, "get_process_manager", lambda: pm)

    from n8n_mcp_admin.main import app

    set_launch_policy(LAUNCH_POLICY)
    c = TestClient(app)
    token = c.post("/api/auth/login", json={"password": "admin"}).json()["token"]
    c.headers["Authorization"] = f"Bearer {token}"
    c.pm = pm
    return c


def test_permissions_roundtrip(n8n_client, tmp_config_path):
    r = n8n_client.put("/api/config/permissions",
                       json={"permissions": {"n8n_executions": ["DELETE"], "n8n_workflow_versions": ["rollback"]}}, headers=revision_headers(n8n_client, None, read_path='/api/config'))
    assert r.status_code == 200, r.text
    assert r.json()["env"]["DISABLED_TOOL_OPERATIONS"] == "n8n_executions:delete;n8n_workflow_versions:rollback"
    assert n8n_client.pm.calls == 1
    stored = json.loads(tmp_config_path.read_text())["tools"]["disabled_operations"]
    assert stored == {"n8n_executions": ["delete"], "n8n_workflow_versions": ["rollback"]}
    body = n8n_client.get("/api/config").json()
    assert body["permissions"] == stored
    assert body["permission_operations"]["n8n_executions"] == ["get", "list", "delete"]
    assert "n8n_delete_workflow" not in body["permission_operations"]


@pytest.mark.parametrize("perms", [{"n8n_delete_workflow": ["delete"]}, {"n8n_executions": ["read"]},
                                   {"ghost": ["x"]}])
def test_permissions_reject_non_upstream(n8n_client, perms):
    assert n8n_client.put("/api/config/permissions", json={"permissions": perms}, headers=revision_headers(n8n_client, None, read_path='/api/config')).status_code == 400


async def test_legacy_permissions_retain_unknown_denies_and_concurrent_tool_changes(n8n_client, monkeypatch):
    from mcp_admin_core.config import get_config_store
    from n8n_mcp_admin.routers.config import PermissionsUpdate, update_permissions
    store = get_config_store()
    await store.patch('tools', {'disabled_operations': {'retired_tool': ['delete']}})
    original_get = store.get
    async def interleaved_get(key, default=None):
        snapshot = await original_get(key, default)
        if key == 'tools':
            await store.patch('tools', {'disabled': ['n8n_delete_workflow'], 'sibling': 42})
        return snapshot
    monkeypatch.setattr(store, 'get', interleaved_get)
    result = await update_permissions(PermissionsUpdate(permissions={'n8n_executions': ['delete']}))
    saved = await original_get('tools')
    assert saved['disabled'] == ['n8n_delete_workflow'] and saved['sibling'] == 42
    assert saved['disabled_operations'] == {'n8n_executions': ['delete'], 'retired_tool': ['delete']}
    assert result['apply_state'] == 'proxy_policy_applied'


async def test_legacy_permissions_corruption_is_not_silently_repaired(n8n_client):
    from mcp_admin_core.config import get_config_store
    store = get_config_store()
    await store.patch('tools', {'disabled_operations': {'n8n_executions': False}})
    before = store.path.read_bytes()
    assert n8n_client.get('/api/config').status_code == 503
    assert n8n_client.put('/api/config/permissions', json={'permissions': {}}, headers=revision_headers(n8n_client, None, read_path='/api/settings')).status_code == 503
    assert store.path.read_bytes() == before


def test_legacy_permissions_reports_proxy_enforcement_when_child_restart_fails(n8n_client, monkeypatch):
    async def failed_restart():
        raise RuntimeError('fixture restart failure')
    monkeypatch.setattr(n8n_client.pm, 'start_or_restart', failed_restart)
    r = n8n_client.put('/api/config/permissions', json={'permissions': {'n8n_executions': ['delete']}}, headers=revision_headers(n8n_client, None, read_path='/api/config'))
    assert r.status_code == 200
    assert r.json()['status'] == 'partial' and r.json()['saved'] is True
    assert r.json()['apply_state'] == 'proxy_policy_applied' and r.json()['child_apply_state'] == 'pending'


def test_tools_operations_use_upstream_names(n8n_client):
    ok = n8n_client.put("/api/tools/operations", json={"disabled_operations": {"n8n_executions": ["delete"]}}, headers=revision_headers(n8n_client, None, read_path='/api/tools'))
    assert ok.status_code == 200 and ok.json()["status"] == "ok"
    bad = n8n_client.put("/api/tools/operations", json={"disabled_operations": {"n8n_executions": ["read"]}}, headers=revision_headers(n8n_client, None, read_path='/api/tools'))
    assert bad.status_code == 400


def test_tools_list_has_all_upstream_tools(n8n_client):
    body = n8n_client.get("/api/tools").json()
    assert {t["name"] for t in body["tools"]} == UPSTREAM_28
    assert set(body["categories"]) == {"Core Reference", "Instance Management"}


def test_test_connection_applies_ssrf(n8n_client, monkeypatch):
    monkeypatch.delenv("WEBHOOK_SECURITY_MODE", raising=False)
    called = []

    class _NoHttp:
        def __init__(self, *a, **kw):
            called.append(1)

    monkeypatch.setattr("n8n_mcp_admin.routers.config.httpx.AsyncClient", _NoHttp)
    r = n8n_client.post("/api/config/test").json()
    assert r["success"] is False and "SSRF" in r["message"]
    assert called == []


def test_connection_rejects_ssrf_bypass_keys(n8n_client):
    r = n8n_client.put("/api/settings/connection", json={"webhook_security_mode": "permissive"}, headers=revision_headers(n8n_client, None, read_path='/api/settings'))
    assert r.status_code == 400


def test_health_probe_applies_ssrf(monkeypatch):
    import asyncio

    from n8n_mcp_admin import health_probes

    monkeypatch.delenv("WEBHOOK_SECURITY_MODE", raising=False)

    async def fake_getaddrinfo(self, host, port, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.43.5.6", port))]

    monkeypatch.setattr("asyncio.base_events.BaseEventLoop.getaddrinfo", fake_getaddrinfo)
    res = asyncio.run(health_probes.probe_target({"n8n_api_url": "http://svc-n8n:5678"}))
    assert res["healthy"] is False and "SSRF" in res["error"]
