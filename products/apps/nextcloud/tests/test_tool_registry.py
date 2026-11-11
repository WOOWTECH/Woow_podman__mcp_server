"""tool_registry 對齊 core create_tools_router 介面，且與 vendored 子行程的工具面一致。"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nextcloud_mcp_admin import tool_registry
from nextcloud_mcp_server.tools import ALL_TOOLS, DELETE_TOOLS, DESCRIPTIONS, READ_TOOLS, WRITE_TOOLS

READ = ["get_file_tree", "get_file_content", "read_text_file", "list_calendars", "list_tasks"]
WRITE = ["create_text_file", "update_text_file", "upload_file"]
DESTRUCTIVE = ["delete_file_checked"]


def test_nine_tools_in_three_levels():
    assert len(tool_registry.TOOL_REGISTRY) == 9
    assert tool_registry.read_only_tool_names() == READ
    assert tool_registry.write_tool_names() == WRITE
    assert tool_registry.dangerous_tool_names() == DESTRUCTIVE


def test_registry_matches_vendored_surface():
    """Tools 頁列出的名稱＝vendored 子行程會註冊的名稱（同順序）。"""
    assert tool_registry.get_all_tool_names() == list(ALL_TOOLS)
    assert list(READ_TOOLS) == READ and list(WRITE_TOOLS) == WRITE and list(DELETE_TOOLS) == DESTRUCTIVE
    assert set(DESCRIPTIONS) == set(tool_registry.get_all_tool_names())


def test_spec_fields():
    spec = tool_registry.get_tool_by_name("delete_file_checked")
    assert spec.dangerous is True and spec.operations == ("delete",) and spec.access == "destructive"
    assert tool_registry.get_tool_by_name("upload_file").operations == ("create", "update")
    assert tool_registry.get_tool_by_name("list_tasks").category is tool_registry.ToolCategory.CALENDAR
    assert tool_registry.get_tool_by_name("nope") is None
    assert all(not s.dangerous for s in tool_registry.TOOL_REGISTRY if s.name != "delete_file_checked")
    assert all(s.description for s in tool_registry.TOOL_REGISTRY)


def test_fresh_install_policy_disables_nothing():
    """新安裝 9 支全開（負責人 2026-10-25「預設全開」）；唯讀政策＝停用 4 支寫入／刪除。"""
    assert tool_registry.default_disabled_tools() == []
    assert tool_registry.write_and_dangerous_tool_names() == WRITE + DESTRUCTIVE


def test_core_tools_router_serves_registry(tmp_path, monkeypatch):
    import mcp_admin_core.config.store as store_mod
    from mcp_admin_core.routers.tools import create_tools_router

    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr(store_mod, "_instance", None)
    app = FastAPI()
    app.include_router(create_tools_router(registry=tool_registry, mcp_label="nextcloud-mcp"))
    body = TestClient(app).get("/api/tools").json()
    assert body["total"] == 9
    assert set(body["categories"]) == {"files", "calendar"}
    assert len(body["categories"]["calendar"]) == 2 and len(body["categories"]["files"]) == 7
    spec = next(t for t in body["tools"] if t["name"] == "delete_file_checked")
    assert spec["dangerous"] is True and spec["operations"] == ["delete"]
