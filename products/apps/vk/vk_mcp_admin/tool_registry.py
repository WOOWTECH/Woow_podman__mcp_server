"""vibe-kanban-mcp 全部 34 個工具的 registry，分四類（--mode global 全集）。

比照 apps/odoo-manage/odoo_manage_mcp_admin/tool_registry.py 對齊共用 core 的
create_tools_router 介面：提供 ToolCategory / TOOL_REGISTRY / get_all_tool_names /
get_tool_by_name，並在 ToolDefinition 補上 operations 欄位（vk 工具無 per-op 概念，預設空 list）。

工具數 canon = 34（A - Execution 10、B - Repo 5、C - Issue / Kanban 18、D - Context 1），
逐條覆核來源 tool_registry.py:66-244 實得——來源 docstring 標「37」與 deny-list／README
彼此矛盾，皆為錯值，一律不沿用。其中 3 個標記為 dangerous
（start_workspace / delete_workspace / delete_issue）；dangerous 旗標僅為 UI 標示。
Tool Manager 的 per-tool 停用（config.json tools.disabled）自 #1384 起由 core MCP proxy 強制：
tools/list 隱藏、tools/call 拒絕（binary 本身仍無過濾 flag）。
註記：binary 以 --mode global 啟動、執行期抓不到 workspace context 時會移除 get_context
（執行期 33 或 34 個），此 registry 為靜態 UI 展示，一律採全集 34。

每筆工具條目包含：
- name：MCP 工具函式名
- description：工具用途說明（UI 字串，照抄來源上游，維持英文）
- category：UI 分類
- enabled_by_default：首次部署時是否啟用
- dangerous：是否為破壞性/寫入操作（UI 紅標；停用由 core proxy 強制，#1384）
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel


class ToolCategory(str, Enum):
    """組織 VK MCP 工具的分類（沿用來源四值，字串為 UI 顯示用途、維持英文）。"""

    EXECUTION = "A - Execution"
    REPO = "B - Repo"
    ISSUE_KANBAN = "C - Issue / Kanban"
    CONTEXT = "D - Context"


class ToolDefinition(BaseModel):
    """單一 MCP 工具定義的 schema。"""

    name: str
    description: str
    category: ToolCategory
    enabled_by_default: bool = True
    dangerous: bool = False
    operations: list[str] = []        # 對齊正典 registry 介面（vk 工具無 per-op 概念，預設空）


class ToolState(BaseModel):
    """工具的執行期狀態（定義 + 當前啟用與否）。"""

    name: str
    description: str
    category: ToolCategory
    enabled_by_default: bool
    dangerous: bool
    enabled: bool


class ToolUpdateRequest(BaseModel):
    """更新工具啟用狀態的請求 body。"""

    tools: Any  # 前端傳入 dict[str, bool] 或 list[dict]


class ToolUpdateResponse(BaseModel):
    """更新工具狀態後的回應。"""

    updated: int
    tools: list[ToolState]


# ---------------------------------------------------------------------------
# vibe-kanban-mcp 全部 34 個工具的完整 registry（--mode global）
# ---------------------------------------------------------------------------

TOOL_REGISTRY: list[ToolDefinition] = [
    # ── A - Execution (10) ─────────────────────────────────────────────
    ToolDefinition(
        name="start_workspace",
        description="Create a new workspace and start the first session with an executor",
        category=ToolCategory.EXECUTION,
        dangerous=True,
    ),
    ToolDefinition(
        name="create_session",
        description="Create a new session in a workspace",
        category=ToolCategory.EXECUTION,
    ),
    ToolDefinition(
        name="run_session_prompt",
        description="Run a coding agent turn in an existing session (async, returns immediately)",
        category=ToolCategory.EXECUTION,
    ),
    ToolDefinition(
        name="get_execution",
        description="Get status and final message for an execution",
        category=ToolCategory.EXECUTION,
    ),
    ToolDefinition(
        name="list_sessions",
        description="List sessions for a workspace",
        category=ToolCategory.EXECUTION,
    ),
    ToolDefinition(
        name="update_session",
        description="Update a session's display name",
        category=ToolCategory.EXECUTION,
    ),
    ToolDefinition(
        name="list_workspaces",
        description="List local workspaces with filters and pagination",
        category=ToolCategory.EXECUTION,
    ),
    ToolDefinition(
        name="update_workspace",
        description="Update workspace properties (archived, pinned, name)",
        category=ToolCategory.EXECUTION,
    ),
    ToolDefinition(
        name="delete_workspace",
        description="Delete a local workspace and optionally its branches",
        category=ToolCategory.EXECUTION,
        dangerous=True,
    ),
    ToolDefinition(
        name="link_workspace_issue",
        description="Link an existing workspace to a remote issue",
        category=ToolCategory.EXECUTION,
    ),
    # ── B - Repo (5) ──────────────────────────────────────────────────
    ToolDefinition(
        name="list_repos",
        description="List all repositories",
        category=ToolCategory.REPO,
    ),
    ToolDefinition(
        name="get_repo",
        description="Get repository details including setup/cleanup/dev server scripts",
        category=ToolCategory.REPO,
    ),
    ToolDefinition(
        name="update_setup_script",
        description="Update the setup script that runs on workspace initialization",
        category=ToolCategory.REPO,
    ),
    ToolDefinition(
        name="update_cleanup_script",
        description="Update the cleanup script that runs on workspace teardown",
        category=ToolCategory.REPO,
    ),
    ToolDefinition(
        name="update_dev_server_script",
        description="Update the dev server startup script",
        category=ToolCategory.REPO,
    ),
    # ── C - Issue / Kanban (18) ────────────────────────────────────────
    ToolDefinition(
        name="list_projects",
        description="List all projects in an organization",
        category=ToolCategory.ISSUE_KANBAN,
    ),
    ToolDefinition(
        name="list_organizations",
        description="List all available organizations",
        category=ToolCategory.ISSUE_KANBAN,
    ),
    ToolDefinition(
        name="list_org_members",
        description="List members of an organization",
        category=ToolCategory.ISSUE_KANBAN,
    ),
    ToolDefinition(
        name="create_issue",
        description="Create a new issue with title, description, and priority",
        category=ToolCategory.ISSUE_KANBAN,
    ),
    ToolDefinition(
        name="list_issues",
        description="List issues with filtering by status, priority, assignee, tag",
        category=ToolCategory.ISSUE_KANBAN,
    ),
    ToolDefinition(
        name="get_issue",
        description="Get detailed issue info including tags, relationships, sub-issues",
        category=ToolCategory.ISSUE_KANBAN,
    ),
    ToolDefinition(
        name="update_issue",
        description="Update issue title, description, status, or priority",
        category=ToolCategory.ISSUE_KANBAN,
    ),
    ToolDefinition(
        name="delete_issue",
        description="Permanently delete an issue",
        category=ToolCategory.ISSUE_KANBAN,
        dangerous=True,
    ),
    ToolDefinition(
        name="list_issue_priorities",
        description="List allowed priority values (urgent, high, medium, low)",
        category=ToolCategory.ISSUE_KANBAN,
    ),
    ToolDefinition(
        name="assign_issue",
        description="Assign a user to an issue",
        category=ToolCategory.ISSUE_KANBAN,
    ),
    ToolDefinition(
        name="unassign_issue",
        description="Remove an assignee from an issue",
        category=ToolCategory.ISSUE_KANBAN,
    ),
    ToolDefinition(
        name="list_issue_assignees",
        description="List all assignees for an issue",
        category=ToolCategory.ISSUE_KANBAN,
    ),
    ToolDefinition(
        name="add_issue_tag",
        description="Attach a tag to an issue",
        category=ToolCategory.ISSUE_KANBAN,
    ),
    ToolDefinition(
        name="remove_issue_tag",
        description="Remove a tag from an issue",
        category=ToolCategory.ISSUE_KANBAN,
    ),
    ToolDefinition(
        name="list_issue_tags",
        description="List tags attached to an issue",
        category=ToolCategory.ISSUE_KANBAN,
    ),
    ToolDefinition(
        name="list_tags",
        description="List all tags for a project",
        category=ToolCategory.ISSUE_KANBAN,
    ),
    ToolDefinition(
        name="create_issue_relationship",
        description="Create a relationship between two issues (blocking, related, has_duplicate)",
        category=ToolCategory.ISSUE_KANBAN,
    ),
    ToolDefinition(
        name="delete_issue_relationship",
        description="Delete an issue relationship",
        category=ToolCategory.ISSUE_KANBAN,
    ),
    # ── D - Context (1) ───────────────────────────────────────────────
    ToolDefinition(
        name="get_context",
        description="Return project, issue, workspace, and orchestrator-session metadata",
        category=ToolCategory.CONTEXT,
    ),
]

# 依工具名的預建查表，供快速存取
TOOL_BY_NAME: dict[str, ToolDefinition] = {t.name: t for t in TOOL_REGISTRY}

# 依分類的預建查表
TOOLS_BY_CATEGORY: dict[ToolCategory, list[ToolDefinition]] = {}
for _tool in TOOL_REGISTRY:
    TOOLS_BY_CATEGORY.setdefault(_tool.category, []).append(_tool)


def get_tool_states(enabled_overrides: dict[str, bool] | None = None) -> list[ToolState]:
    """建立完整工具狀態清單，套用任何啟用覆寫值。

    Args:
        enabled_overrides: 工具名 → 啟用 bool 的字典；未列入者採 enabled_by_default。

    Returns:
        解析出啟用狀態後的 ToolState 清單。
    """
    overrides = enabled_overrides or {}
    return [
        ToolState(
            name=t.name,
            description=t.description,
            category=t.category,
            enabled_by_default=t.enabled_by_default,
            dangerous=t.dangerous,
            enabled=overrides.get(t.name, t.enabled_by_default),
        )
        for t in TOOL_REGISTRY
    ]


def get_category_summary() -> dict[str, dict[str, Any]]:
    """回傳各分類的工具摘要。

    Returns:
        分類值 → {count, tools} 的字典。
    """
    return {
        cat.value: {
            "count": len(tools),
            "tools": [t.name for t in tools],
        }
        for cat, tools in TOOLS_BY_CATEGORY.items()
    }


def get_all_tool_names() -> list[str]:
    """回傳所有工具名的扁平清單。"""
    return [t.name for t in TOOL_REGISTRY]


def get_tool_by_name(name: str) -> ToolDefinition | None:
    """依名稱查找工具。"""
    return TOOL_BY_NAME.get(name)
