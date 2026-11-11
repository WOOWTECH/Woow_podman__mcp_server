"""LiteLLM MCP 工具 registry 的 core 介面 adapter。

正典清單在 vendored ``woow_litellm_mcp_server.registry``（子行程依它註冊工具、gating 依它判斷），
這裡只 re-export 成 ``mcp_admin_core.routers.tools.create_tools_router`` 要的介面：
``ToolCategory``（可疊代 str-Enum）、``TOOL_REGISTRY``（項目有 name／description／category／
operations／dangerous）、``get_all_tool_names()``、``get_tool_by_name()``。如此 Tools 頁永遠不會
列出子行程沒有的工具（``tests/test_tool_registry.py`` 守線）。

40 支、8 類；``operations`` 只含 ``read`` 的 15 支是唯讀模式下仍會曝露的工具；``dangerous``
（破壞性、不可逆）8 支：delete_model、delete_key、block_key、regenerate_key、delete_team、
team_member_delete、delete_user、delete_plugin。

停用語意：Tools 頁的 per-tool 停用寫入 ``tools.disabled``，由 core MCP proxy 即時強制
（tools/list 隱藏、tools/call 拒絕，#1384，不需重啟）；唯讀模式（``LITELLM_MCP_READONLY``）
與分類／環境操作停用由子行程 gate 在註冊時移除工具，需重啟子行程才同步。
Tools API 的操作停用由下方 ``OPERATION_POLICIES`` 經 core proxy 即時強制；
解除限制後，子行程的舊環境 gate 仍可能需要重啟。
"""
from __future__ import annotations

from mcp_admin_core.operation_policy import OperationSpec

from woow_litellm_mcp_server.registry import (  # noqa: F401
    ACTIVE_OPERATION_TOOLS,
    OP_CREATE,
    OP_READ,
    TOOL_REGISTRY,
    TOOLS_BY_NAME,
    ToolCategory,
    ToolSpec,
)


def get_tool_by_name(name: str) -> ToolSpec | None:
    return TOOLS_BY_NAME.get(name)


def get_all_tool_names() -> list[str]:
    return [spec.name for spec in TOOL_REGISTRY]


def read_only_tool_names() -> list[str]:
    """唯讀模式下仍曝露的工具（operations 只有 read）。"""
    return [spec.name for spec in TOOL_REGISTRY if spec.operations == (OP_READ,)]


def dangerous_tool_names() -> list[str]:
    return [spec.name for spec in TOOL_REGISTRY if spec.dangerous]


# Vendored ToolGate/handlers use these exact fixed operation labels. No input
# field is an operation selector; body enums (model, role, etc.) are unrelated.
OPERATION_POLICIES = {
    spec.name: OperationSpec(
        tuple(spec.operations), default=spec.operations[0],
        legacy_denies={OP_READ: (OP_CREATE,)} if spec.name in ACTIVE_OPERATION_TOOLS else {},
    )
    for spec in TOOL_REGISTRY if len(spec.operations) == 1
}

# Consumed by the shared Permissions UI/API without changing shared UI code.
OPERATION_NOTES = {
    spec.name: spec.description + " Policy label: create (active work). Legacy read-denies still apply."
    for spec in TOOL_REGISTRY if spec.name in ACTIVE_OPERATION_TOOLS
}
