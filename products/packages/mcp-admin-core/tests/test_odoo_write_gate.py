"""Odoo 寫入開關：新安裝全開、既有設定維持原樣、啟動白名單的回歸測試。

負責人 2026-10-25「預設全開」：新安裝的四個寫入開關（審核寫入、直接寫入、未審查方法、chatter 直接張貼）都開；
既有 config.json 缺鍵時維持改版前的值（審核寫入開、其餘關），管理員存過的值一律不動。
"""

from management_client import revision_headers
import json
from contextlib import asynccontextmanager

import pytest

from mcp_admin_core.process import _build_subprocess_env
from odoo_mcp_admin.main import LAUNCH_POLICY

ALL_ON = {
    "ODOO_MCP_ENABLE_WRITES": "1",
    "ODOO_MCP_ALLOW_DIRECT_WRITES": "1",
    "ODOO_MCP_ALLOW_UNKNOWN_METHODS": "1",
    "MCP_CHATTER_DIRECT": "1",
}
PREVIOUS = {
    "ODOO_MCP_ENABLE_WRITES": "1",
    "ODOO_MCP_ALLOW_DIRECT_WRITES": "0",
    "ODOO_MCP_ALLOW_UNKNOWN_METHODS": "0",
    "MCP_CHATTER_DIRECT": "0",
}


@pytest.fixture
def no_inherited_switches(monkeypatch):
    for key in ALL_ON:
        monkeypatch.delenv(key, raising=False)


def _child(cfg):
    return _build_subprocess_env({}, cfg["mcp_server"], LAUNCH_POLICY, cfg)


def test_policy_constants_are_the_owner_decision():
    from odoo_mcp_admin import seed_defaults

    assert seed_defaults.FRESH_ENV == ALL_ON
    assert seed_defaults.LEGACY_DEFAULTS == PREVIOUS
    assert dict(LAUNCH_POLICY.fresh_env) == ALL_ON


@pytest.mark.parametrize("stored, expected", [({}, "1"), ({"ODOO_MCP_ENABLE_WRITES": "1"}, "1"), ({"ODOO_MCP_ENABLE_WRITES": "0"}, "0")])
def test_write_gate_reaches_child(stored, expected, no_inherited_switches):
    cfg = {"connection": {}, "mcp_server": {"env": stored}}
    assert _child(cfg).get("ODOO_MCP_ENABLE_WRITES") == expected


def test_existing_config_without_keys_keeps_previous_behaviour(no_inherited_switches):
    """既有設定缺鍵（例如 2026-10-12 之前開通、之後沒換過映像）：換映像不會替它打開直接寫入等開關。"""
    cfg = {"mcp_server": {"env": {}}}
    assert {key: _child(cfg)[key] for key in ALL_ON} == PREVIOUS


def test_all_four_switches_are_editable_and_reach_child(no_inherited_switches):
    assert set(ALL_ON) <= LAUNCH_POLICY.env_keys
    assert set(ALL_ON) <= LAUNCH_POLICY.editable_env_keys
    assert "PYTHONPATH" not in LAUNCH_POLICY.env_keys
    cfg = {"mcp_server": {"env": dict(ALL_ON)}}
    assert {key: _child(cfg)[key] for key in ALL_ON} == ALL_ON


def test_saved_values_are_preserved(no_inherited_switches):
    saved = {"ODOO_MCP_ENABLE_WRITES": "1", "ODOO_MCP_ALLOW_DIRECT_WRITES": "1",
             "ODOO_MCP_ALLOW_UNKNOWN_METHODS": "0", "MCP_CHATTER_DIRECT": "1"}
    cfg = {"mcp_server": {"env": dict(saved)}}
    assert {key: _child(cfg)[key] for key in ALL_ON} == saved


def test_fresh_install_config_is_all_on(tmp_path, no_inherited_switches):
    """沒有部署 seed、由本行程建立 config.json（全新安裝）：四個開關都開，子行程拿到的也是全開。"""
    from mcp_admin_core.config.store import ConfigStore
    from mcp_admin_core.launch_policy import set_launch_policy

    set_launch_policy(LAUNCH_POLICY)  # conftest 每個測試後還原
    path = tmp_path / "config.json"
    ConfigStore(path=path)
    cfg = json.loads(path.read_text())
    assert cfg["mcp_server"]["env"] == ALL_ON
    assert {key: _child(cfg)[key] for key in ALL_ON} == ALL_ON


def test_existing_file_is_not_rewritten_with_fresh_values(tmp_path):
    from mcp_admin_core.config.store import ConfigStore
    from mcp_admin_core.launch_policy import set_launch_policy

    set_launch_policy(LAUNCH_POLICY)
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"mcp_server": {"env": {"ODOO_MCP_ALLOW_DIRECT_WRITES": "0"}}}))
    before = path.read_bytes()
    ConfigStore(path=path)
    assert path.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [{}, {"ODOO_MCP_ENABLE_WRITES": "0"}, {"ODOO_MCP_ENABLE_WRITES": "1", "ODOO_MCP_ALLOW_DIRECT_WRITES": "1"}, dict(ALL_ON)])
async def test_seed_is_persistent_idempotent_and_preserves_other_config(store_env, tmp_config_path, stored):
    from odoo_mcp_admin.seed_defaults import LEGACY_DEFAULTS, POLICY_KEYS, apply_seed_defaults

    config = {
        "connection": {"odoo_url": "https://example.invalid", "odoo_password": "dummy-test-password"},
        "mcp_auth_token": "dummy-test-token",
        "tools": {"disabled": ["unlink"]},
        "mcp_server": {"port": 8123, "env": {"unrelated": "keep", **stored}},
    }
    await store_env.change_mcp_token(config["mcp_auth_token"], action="set")
    # Whole-config restores preserve policy; set fixture policy explicitly.
    await store_env.put("tools", config["tools"])
    await store_env.save(config)
    first = await apply_seed_defaults()
    assert set(first) == set(POLICY_KEYS) - set(stored)
    persisted = json.loads(tmp_config_path.read_text())
    assert persisted["mcp_server"]["env"] == {**LEGACY_DEFAULTS, "unrelated": "keep", **stored}
    assert persisted["mcp_server"]["port"] == 8123
    for key in ("connection", "mcp_auth_token", "tools"):
        assert persisted[key] == config[key]
    before = tmp_config_path.read_bytes()
    assert await apply_seed_defaults() == []
    assert tmp_config_path.read_bytes() == before
    assert tmp_config_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_seed_runs_before_core_starts_child(store_env, monkeypatch):
    """既有設定（這裡是不帶政策建立的空設定）缺鍵：core 拉起子行程前已補上原本的值。"""
    import odoo_mcp_admin.main as main

    @asynccontextmanager
    async def fake_core(app):
        cfg = await store_env.load()
        assert {key: cfg["mcp_server"]["env"][key] for key in ALL_ON} == PREVIOUS
        yield

    monkeypatch.setattr(main, "_core_lifespan", fake_core)
    async with main._lifespan_with_seed_defaults(main.app):
        pass


@pytest.mark.asyncio
async def test_fresh_install_lifespan_starts_child_all_on(tmp_config_path, monkeypatch):
    """全新安裝走真的 lifespan：config.json 由本行程以 fresh_env 建立，core 拉起子行程時四個開關都開。"""
    import mcp_admin_core.config.store as store_mod
    import odoo_mcp_admin.main as main
    from mcp_admin_core.launch_policy import set_launch_policy

    set_launch_policy(LAUNCH_POLICY)
    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(tmp_config_path))
    monkeypatch.setattr(store_mod, "_instance", None)
    seen = {}

    @asynccontextmanager
    async def fake_core(app):
        cfg = await store_mod.get_config_store().load()
        seen.update(_child(cfg))
        yield

    monkeypatch.setattr(main, "_core_lifespan", fake_core)
    async with main._lifespan_with_seed_defaults(main.app):
        pass
    assert {key: seen[key] for key in ALL_ON} == ALL_ON
    assert json.loads(tmp_config_path.read_text())["mcp_server"]["env"] == ALL_ON


@pytest.mark.asyncio
async def test_admin_save_off_survives_seed_and_relaunch(make_app, tmp_config_path, monkeypatch):
    from unittest.mock import AsyncMock

    from mcp_admin_core.launch_policy import set_launch_policy
    from mcp_admin_core.process import get_process_manager
    from odoo_mcp_admin.seed_defaults import apply_seed_defaults

    # Settings API 存檔會自動重啟；此測試只驗證配置，不能啟動真子行程污染其他測試。
    monkeypatch.setattr(get_process_manager(), "start_or_restart", AsyncMock(return_value=True))
    client = make_app({"mcp_server": {"env": dict(ALL_ON)}})
    set_launch_policy(LAUNCH_POLICY)
    token = client.post("/api/auth/login", json={"password": "admin"}).json()["token"]
    off = {key: "0" for key in ALL_ON}
    response = client.put("/api/settings/mcp_server", json={"env": off}, headers=revision_headers(client, {"Authorization": f"Bearer {token}"}, read_path='/api/settings'))
    assert response.status_code == 200
    assert await apply_seed_defaults() == []
    cfg = json.loads(tmp_config_path.read_text())
    child_env = _build_subprocess_env({}, cfg["mcp_server"], LAUNCH_POLICY, cfg)
    assert {key: child_env[key] for key in ALL_ON} == off
