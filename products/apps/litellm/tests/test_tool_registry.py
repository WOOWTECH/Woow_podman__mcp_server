"""tool_registry adapter 對齊 core create_tools_router 介面，且與子行程註冊面一致。"""
from __future__ import annotations

from management_client import revision_headers

from fastapi import FastAPI
from fastapi.testclient import TestClient

from litellm_mcp_admin import tool_registry
from woow_litellm_mcp_server.registry import TOOL_REGISTRY

from .conftest import build_gate, register_all
import pytest


def test_counts():
    assert len(tool_registry.TOOL_REGISTRY) == 40
    assert len(list(tool_registry.ToolCategory)) == 8
    assert len(tool_registry.read_only_tool_names()) == 15
    assert len(tool_registry.dangerous_tool_names()) == 8


def test_lookup_helpers():
    names = tool_registry.get_all_tool_names()
    assert names == [s.name for s in TOOL_REGISTRY]
    assert tool_registry.get_tool_by_name("litellm_delete_key").dangerous is True
    assert tool_registry.get_tool_by_name("nope") is None


def test_registry_matches_child_surface():
    """Tools 頁列出的名稱＝子行程全開時實際註冊的名稱。"""
    assert set(register_all(build_gate()).tools) == set(tool_registry.get_all_tool_names())


def test_readonly_list_matches_child_gate():
    child = set(register_all(build_gate(readonly=True)).tools)
    assert child == set(tool_registry.read_only_tool_names())


def test_core_tools_router_serves_registry(tmp_path, monkeypatch):
    # bare FastAPI（比照 core test_tools_router 的 odoo-manage 前例），不經 auth。
    import mcp_admin_core.config.store as store_mod
    from mcp_admin_core.routers.tools import create_tools_router

    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr(store_mod, "_instance", None)
    app = FastAPI()
    app.include_router(create_tools_router(registry=tool_registry, mcp_label="litellm-mcp"))
    body = TestClient(app).get("/api/tools").json()
    assert body["total"] == 40
    assert set(body["categories"]) == {c.value for c in tool_registry.ToolCategory}
    spec = next(t for t in body["tools"] if t["name"] == "litellm_generate_key")
    assert spec["operations"] == ["create"] and spec["category"] == "keys"


@pytest.mark.parametrize("name", sorted(tool_registry.ACTIVE_OPERATION_TOOLS))
def test_legacy_read_deny_is_visible_editable_and_enforced(tmp_path, monkeypatch, name):
    import json
    import mcp_admin_core.config.store as store_mod
    from mcp_admin_core.operation_policy import disabled_operation_calls
    from mcp_admin_core.routers.tools import create_tools_router

    path = tmp_path / "config.json"
    cfg = json.loads(json.dumps(store_mod._DEFAULT_CONFIG))
    cfg["tools"]["disabled_operations"] = {name: ["read"]}
    path.write_text(json.dumps(cfg))
    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(path))
    monkeypatch.setattr(store_mod, "_instance", None)
    app = FastAPI()
    app.include_router(create_tools_router(registry=tool_registry, mcp_label="litellm-mcp"))
    with TestClient(app) as client:
        body = client.get("/api/tools").json()
        spec = next(tool for tool in body["tools"] if tool["name"] == name)
        assert spec["operations"] == ["create"] and spec["operation_default"] == "create"
        assert spec["operation_legacy_denies"] == {"read": ["create"]}
        assert spec["operation_note"] and spec["policy_enabled"] is False
        assert body["editable_disabled_operations"] == {name: ["create"]}
        call = {"method": "tools/call", "params": {"name": name, "arguments": {}}}
        assert disabled_operation_calls(call, cfg["tools"], tool_registry) == [call]
        updated = client.put("/api/tools/operations", json={"disabled_operations": {name: ["create"]}}, headers=revision_headers(client, None, read_path='/api/tools'))
        assert updated.status_code == 200
        saved = json.loads(path.read_text())["tools"]
        assert saved["disabled_operations"] == {name: ["create"]}
        assert disabled_operation_calls(call, saved, tool_registry) == [call]
        assert client.put("/api/tools/operations", json={"disabled_operations": {}}, headers=revision_headers(client, None, read_path='/api/tools')).status_code == 200
        saved = json.loads(path.read_text())["tools"]
        assert disabled_operation_calls(call, saved, tool_registry) == []
