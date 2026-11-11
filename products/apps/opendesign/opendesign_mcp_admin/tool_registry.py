"""vendored od_mcp_server 全部 15 個工具的 registry，分三類（附錄 A canon）。

比照 apps/odoo-manage/odoo_manage_mcp_admin/tool_registry.py 對齊共用 core 的
create_tools_router 介面：提供 ToolCategory / TOOL_REGISTRY / get_all_tool_names /
get_tool_by_name，並在 ToolDefinition 補上 operations 欄位（od 工具無 per-op 概念，預設空 list）。

工具數 canon = 15（System & Discovery 7、Projects & Files – Read 5、Projects – Write 3）。
其中 3 個標記為 dangerous（create_project / send_message / delete_project），皆為寫入或
長時（最長 300s）操作，UI 紅標從嚴。

停用治理：dangerous 只是 UI 紅標；Tool Manager 的 tools.disabled 由共用 core proxy
（mcp_admin_core/tool_filter.py，#1384）強制——tools/list 隱藏、tools/call 拒絕、存檔即時生效。
vendored FastMCP 本身不過濾，也不要在 vendored 檔內加治理邏輯，詳見 README。

每筆工具條目包含：
- name：MCP 工具函式名（對齊 vendored od_mcp_server.py 的 @mcp.tool 名）
- description：工具用途說明（UI 字串，維持英文）
- category：UI 分類
- enabled_by_default：首次部署時是否啟用（G2-D 已定 A：全 15 支 True）
- dangerous：是否為破壞性/寫入/長時操作（UI 紅標，見上）
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel


class ToolCategory(str, Enum):
    """組織 Open Design MCP 工具的分類（字串為 UI 顯示用途、維持英文）。"""

    SYSTEM_DISCOVERY = "System & Discovery"
    PROJECTS_FILES_READ = "Projects & Files – Read"
    PROJECTS_WRITE = "Projects – Write"


class ToolDefinition(BaseModel):
    """單一 MCP 工具定義的 schema。"""

    name: str
    description: str
    category: ToolCategory
    enabled_by_default: bool = True
    dangerous: bool = False
    operations: list[str] = []        # 對齊正典 registry 介面（od 工具無 per-op 概念，預設空）


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
# vendored od_mcp_server 全部 15 個工具的完整 registry（附錄 A canon）
# ---------------------------------------------------------------------------

TOOL_REGISTRY: list[ToolDefinition] = [
    # ── System & Discovery (7) ────────────────────────────────────────────
    ToolDefinition(
        name="health",
        description="Check Open Design daemon health (unauthenticated probe)",
        category=ToolCategory.SYSTEM_DISCOVERY,
    ),
    ToolDefinition(
        name="version",
        description="Get Open Design daemon version information",
        category=ToolCategory.SYSTEM_DISCOVERY,
    ),
    ToolDefinition(
        name="list_agents",
        description="List agents filtered by availability (returns available/total counts)",
        category=ToolCategory.SYSTEM_DISCOVERY,
    ),
    ToolDefinition(
        name="list_connectors",
        description="List all configured connectors",
        category=ToolCategory.SYSTEM_DISCOVERY,
    ),
    ToolDefinition(
        name="list_runs",
        description="List recent runs",
        category=ToolCategory.SYSTEM_DISCOVERY,
    ),
    ToolDefinition(
        name="list_plugins",
        description="List installed plugins (summarized: id/title/source/trust)",
        category=ToolCategory.SYSTEM_DISCOVERY,
    ),
    ToolDefinition(
        name="list_skills",
        description="List available skills (summarized: id/name/description/mode/surface/triggers)",
        category=ToolCategory.SYSTEM_DISCOVERY,
    ),
    # ── Projects & Files – Read (5) ───────────────────────────────────────
    ToolDefinition(
        name="list_projects",
        description="List all projects",
        category=ToolCategory.PROJECTS_FILES_READ,
    ),
    ToolDefinition(
        name="get_project",
        description="Get details for a specific project by ID",
        category=ToolCategory.PROJECTS_FILES_READ,
    ),
    ToolDefinition(
        name="list_project_files",
        description="List files within a project",
        category=ToolCategory.PROJECTS_FILES_READ,
    ),
    ToolDefinition(
        name="read_file",
        description="Read the raw contents of a file within a project",
        category=ToolCategory.PROJECTS_FILES_READ,
    ),
    ToolDefinition(
        name="get_file_info",
        description="Get metadata for a specific file within a project",
        category=ToolCategory.PROJECTS_FILES_READ,
    ),
    # ── Projects – Write (3) ──────────────────────────────────────────────
    ToolDefinition(
        name="create_project",
        description="Create a new project via an AI chat run (long-running, up to 300s)",
        category=ToolCategory.PROJECTS_WRITE,
        dangerous=True,
    ),
    ToolDefinition(
        name="send_message",
        description="Send a chat message to drive an existing project (long-running, up to 300s)",
        category=ToolCategory.PROJECTS_WRITE,
        dangerous=True,
    ),
    ToolDefinition(
        name="delete_project",
        description="Permanently delete a project (irreversible, no confirmation)",
        category=ToolCategory.PROJECTS_WRITE,
        dangerous=True,
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
