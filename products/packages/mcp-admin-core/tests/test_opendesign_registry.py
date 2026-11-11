"""opendesign tool_registry 契約測試：15 tools／3 類／dangerous 集合／全預設啟用。

比照 apps/opendesign/opendesign_mcp_admin/tool_registry.py 對齊共用 core 的
create_tools_router 介面，並鎖定附錄 A 的 canon（3 類、dangerous 3 支、operations 皆空）。
"""
from __future__ import annotations

from opendesign_mcp_admin import tool_registry as reg


def test_registry_has_15_tools():
    assert len(reg.TOOL_REGISTRY) == 15


def test_three_category_names():
    values = {c.value for c in reg.ToolCategory}
    assert values == {
        "System & Discovery",
        "Projects & Files – Read",
        "Projects – Write",
    }


def test_category_counts_match_canon():
    counts: dict[str, int] = {}
    for t in reg.TOOL_REGISTRY:
        counts[t.category.value] = counts.get(t.category.value, 0) + 1
    assert counts == {
        "System & Discovery": 7,
        "Projects & Files – Read": 5,
        "Projects – Write": 3,
    }


def test_get_all_tool_names():
    names = reg.get_all_tool_names()
    assert len(names) == 15
    assert set(names) == {
        "health", "version", "list_agents", "list_connectors", "list_runs",
        "list_plugins", "list_skills",
        "list_projects", "get_project", "list_project_files", "read_file",
        "get_file_info",
        "create_project", "send_message", "delete_project",
    }


def test_get_tool_by_name():
    assert reg.get_tool_by_name("delete_project").name == "delete_project"
    assert reg.get_tool_by_name("health").category.value == "System & Discovery"
    assert reg.get_tool_by_name("does_not_exist") is None


def test_dangerous_set_is_three_write_tools():
    dangerous = {t.name for t in reg.TOOL_REGISTRY if t.dangerous}
    assert dangerous == {"create_project", "send_message", "delete_project"}


def test_all_enabled_by_default():
    # G2-D 已定 A：全 15 支出廠啟用（停用由租戶在 Tools 頁決定，core proxy 強制，#1384）。
    assert all(t.enabled_by_default for t in reg.TOOL_REGISTRY)


def test_all_operations_empty():
    # opendesign 工具無 per-op 概念，operations 一律空 list。
    assert all(t.operations == [] for t in reg.TOOL_REGISTRY)
