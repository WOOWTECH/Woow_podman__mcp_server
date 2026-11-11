"""main.py 組裝：core factory、/healthz、四個 extra router、出廠唯讀預設、path token 不進 log。"""
from __future__ import annotations

from management_client import revision_headers

import json
import logging

import pytest
from fastapi.testclient import TestClient

from .conftest import stored_config


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


def _public_healthz(client):
    """不帶登入打 /healthz，全域政策先換回 litellm 的（別的測試以 create_app() 組 app 時會把它換掉）。"""
    from mcp_admin_core.launch_policy import get_launch_policy, set_launch_policy

    from litellm_mcp_admin.main import LAUNCH_POLICY

    client.headers.pop("Authorization", None)
    previous = get_launch_policy()
    set_launch_policy(LAUNCH_POLICY)
    try:
        return client.get("/healthz")
    finally:
        set_launch_policy(previous)


@pytest.mark.parametrize(
    ("connection", "backend"),
    [
        ({}, "missing"),
        ({"base_url": "http://svc-xxxxxxxx-litellm:4000"}, "missing"),  # 平台預填了同工作區的網址、金鑰還沒貼
        ({"master_key": "sk-test-SECRET-0123"}, "missing"),  # 沒有網址：子行程連 http://localhost:4000
        ({"base_url": "http://svc-xxxxxxxx-litellm:4000", "master_key": " "}, "missing"),
        ({"base_url": "http://svc-xxxxxxxx-litellm:4000", "master_key": "sk-test-SECRET-0123"}, "configured"),
    ],
)
def test_healthz_ok_without_login(make_client, connection, backend):
    resp = _public_healthz(make_client(connection=connection, app="main"))
    # mcp：給平台的粗略狀態（litellm 沒有連線也會啟動，不宣告必要欄位；這裡沒有子行程）。backend：網址與金鑰都填了才是
    # configured（第五輪），平台據此顯示「尚未設定後端連線」；不含欄位名稱或值。
    assert resp.status_code == 200 and resp.json() == {"status": "ok", "mcp": "not_running", "backend": backend}
    assert "sk-test" not in resp.text and "base_url" not in resp.text


def test_healthz_leaves_backend_out_when_the_config_is_unreadable(make_client, config_path, monkeypatch):
    import mcp_admin_core.config.store as store_mod

    client = make_client(app="main")
    config_path.write_text("{broken")
    monkeypatch.setattr(store_mod, "_instance", None)  # 重開的 store 沒有快照：/healthz 會去讀檔、讀不到
    resp = _public_healthz(client)
    assert resp.status_code == 200 and resp.json() == {"status": "ok", "mcp": "not_running"}


def test_backend_fields_are_the_connection_page_fields():
    import re
    from pathlib import Path

    from litellm_mcp_admin.main import LAUNCH_POLICY

    page = (Path(__file__).resolve().parents[1] / "frontend" / "src" / "appConfig.jsx").read_text(encoding="utf-8")
    labels = dict(re.findall(r"\{ name: '(\w+)', label: '([^']+)'", page))
    assert LAUNCH_POLICY.backend_connection == tuple((key, labels[key]) for key in ("base_url", "master_key"))
    assert LAUNCH_POLICY.required_connection == ()  # 沒有連線也起得來：不是必要欄位


def test_routes_mounted():
    from litellm_mcp_admin.main import app

    paths = _collect_paths(app.routes)
    for p in (
        "/healthz",
        "/api/config",
        "/api/config/connection",
        "/api/config/test",
        "/api/tools",
        "/api/tools/operations",
        "/api/health",
        "/api/logs/stream",
        "/api/logs/search",
        "/api/tokens",
        "/api/settings",
        "/private_{token}/{path:path}",
    ):
        assert p in paths, p
    assert app.title == "LiteLLM MCP Admin"


def test_tools_api_serves_all_40_with_8_dangerous(make_client):
    client = make_client(app="main")
    body = client.get("/api/tools").json()
    assert body["total"] == 40 and body["enabled"] == 40
    assert len(body["categories"]) == 8
    dangerous = sorted(t["name"] for t in body["tools"] if t["dangerous"])
    assert dangerous == sorted([
        "litellm_delete_model", "litellm_delete_key", "litellm_block_key", "litellm_regenerate_key",
        "litellm_delete_team", "litellm_team_member_delete", "litellm_delete_user", "litellm_delete_plugin",
    ])


def test_tools_disable_is_persisted_for_proxy(make_client, config_path):
    client = make_client(app="main")
    resp = client.put("/api/tools", json={"disabled_tools": ["litellm_delete_key"]}, headers=revision_headers(client, None, read_path='/api/tools'))
    assert resp.status_code == 200
    assert stored_config(config_path)["tools"]["disabled"] == ["litellm_delete_key"]


def test_health_api_shape(make_client, monkeypatch):
    from litellm_mcp_admin import health_probes

    async def fake_check(base, key, **_):
        return {"healthy": True, "url": base, "version": "1.83.14", "db": "connected", "model_count": 5}

    monkeypatch.setattr(health_probes, "check_gateway", fake_check)
    client = make_client(app="main", connection={"base_url": "http://lite:4000", "master_key": "sk-x-123456"})
    body = client.get("/api/health").json()
    assert body["app_type"] == "litellm"
    assert body["target_app"]["healthy"] is True
    assert body["version"] == "1.83.14" and body["item_count"] == 5 and body["db_name"] == "connected"
    assert "sk-x-123456" not in json.dumps(body)


# -- seed defaults（新安裝全開；既有服務存的值不動） --------------------------------------


async def _apply(make_client, **mcp_server):
    make_client(mcp_server=mcp_server)  # 只為了把 store singleton 指到暫存 config
    from litellm_mcp_admin import seed_defaults

    return await seed_defaults.apply_seed_defaults()


async def test_seed_defaults_fill_readonly_and_prefix(make_client, config_path):
    """缺鍵＝從未啟動過的新安裝：補 "0"（40 支全開，負責人 2026-10-25「預設全開」）。"""
    changed = await _apply(make_client, env={}, env_prefix="")
    assert changed == ["LITELLM_MCP_READONLY", "env_prefix"]
    mcp = stored_config(config_path)["mcp_server"]
    assert mcp["env"]["LITELLM_MCP_READONLY"] == "0"
    assert mcp["env_prefix"] == "LITELLM_MCP_"


async def test_seed_defaults_keep_existing_read_only_service(make_client, config_path):
    """2026-10-25 之前開通的服務（seed 寫 "1"）：換新映像後仍是唯讀，不重新打開。"""
    changed = await _apply(make_client, command="litellm-mcp-server",
                           env={"LITELLM_MCP_READONLY": "1"}, env_prefix="LITELLM_MCP_")
    assert changed == []
    assert stored_config(config_path)["mcp_server"]["env"] == {"LITELLM_MCP_READONLY": "1"}


async def test_seed_defaults_respect_tenant_unlock(make_client, config_path):
    changed = await _apply(
        make_client,
        command="litellm-mcp-server",
        env={"LITELLM_MCP_READONLY": "0", "OTHER": "x"},
        env_prefix="LITELLM_MCP_",
    )
    assert changed == []
    mcp = stored_config(config_path)["mcp_server"]
    assert mcp["env"] == {"LITELLM_MCP_READONLY": "0", "OTHER": "x"}
    assert mcp["command"] == "litellm-mcp-server"


async def test_seed_defaults_keep_other_env_and_custom_prefix(make_client, config_path):
    changed = await _apply(make_client, env={"OTHER": "x"}, env_prefix="CUSTOM_")
    assert changed == ["LITELLM_MCP_READONLY"]
    mcp = stored_config(config_path)["mcp_server"]
    assert mcp["env"] == {"OTHER": "x", "LITELLM_MCP_READONLY": "0"}
    assert mcp["env_prefix"] == "CUSTOM_"


def test_lifespan_applies_seed_defaults_before_core(make_client, config_path):
    from litellm_mcp_admin.main import app

    make_client(mcp_server={"command": "", "env": {}, "env_prefix": ""})
    with TestClient(app) as client:  # 進 lifespan；command 空 → core 不 spawn 子行程
        assert client.get("/healthz").status_code == 200
    mcp = stored_config(config_path)["mcp_server"]
    assert mcp["env"]["LITELLM_MCP_READONLY"] == "0" and mcp["env_prefix"] == "LITELLM_MCP_"


# -- path token 不進 log ---------------------------------------------------------


def test_access_log_redacts_path_token(make_client, caplog):
    import litellm_mcp_admin.main  # noqa: F401 — 確保 filter 已安裝

    token = "f" * 64
    access = logging.getLogger("uvicorn.access")
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        access.info('%s - "%s %s HTTP/%s" %d', "10.0.0.1:5555", "POST", f"/private_{token}/mcp", "1.1", 200)
    text = caplog.text
    assert token not in text
    assert "/private_***/mcp" in text


def test_logs_page_buffer_has_no_token(make_client):
    import litellm_mcp_admin.main  # noqa: F401
    from mcp_admin_core.routers import logs as logs_mod

    token = "e" * 64
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "10.0.0.1:5555", "GET", f"/private_{token}", "1.1", 403
    )
    with logs_mod._buffer_lock:
        messages = [e["message"] for e in logs_mod._log_buffer]
    assert not any(token in m for m in messages)
    assert any("/private_***" in m for m in messages)
