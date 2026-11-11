"""n8n MCP tool registry——由映像內的上游 n8n-mcp 動態產生（woow_paas_platform#1531）。

舊版是手寫的 24 支清單（少了 n8n_evaluations／n8n_manage_folders／n8n_manage_agents／
n8n_explore_node_resources／n8n_list_catalog，又多列一支不存在的 n8n_generate_workflow），
操作名也是自編的 read／write，和上游 ``DISABLED_TOOL_OPERATIONS`` 比對的 enum（list／get／delete…）
對不上。現在 ``refresh()`` 以 ``upstream_catalog.js`` 讀上游 tools/list 的來源定義：

- 工具＝``n8nDocumentationToolsFinal``（Core Reference）＋``n8nManagementTools``（Instance Management）。
- ``operations``＝上游 ``TOOL_OPERATION_PARAM`` 有列的工具才有，值是 ``getValidOperations()``
  （小寫，＝上游 DISABLED_TOOL_OPERATIONS 的比對基準）；其他工具只能在 Tools 頁整支停用。
- ``n8n_manage_credentials`` 的 action 分流已覆核；映像 build patch 補上原生 env gate 的漏項，proxy 仍以最新 saved policy 檢查。
- ``dangerous``＝``annotations.destructiveHint`` 為真，或有破壞性操作。

上游套件版本固定在映像裡（Dockerfile 釘版），所以每個行程只需成功讀一次；讀取失敗（例如本機
沒有 node）時沿用 ``FALLBACK_TOOLS``（n8n-mcp 2.96.2 的清單，與 2.92.1 相同）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from mcp_admin_core.operation_policy import OperationSpec

from . import upstream
from .effective_arguments import EXPOSURE_TOOLS, normalize_arguments, conditional_call_disabled

logger = logging.getLogger(__name__)

CATALOG_SCRIPT = Path(__file__).with_name("upstream_catalog.js")
CATALOG_TIMEOUT_SECONDS = 30
_DESCRIPTION_LIMIT = 280


class ToolCategory(str, Enum):
    """Categories for n8n MCP tools（對應上游 documentation／management 兩組）。"""

    CORE_REFERENCE = "Core Reference"
    INSTANCE_MANAGEMENT = "Instance Management"


_GROUP_CATEGORY = {
    "documentation": ToolCategory.CORE_REFERENCE,
    "management": ToolCategory.INSTANCE_MANAGEMENT,
}


class ToolDefinition(BaseModel):
    """Definition of a single n8n MCP tool."""

    name: str
    category: ToolCategory
    description: str
    operations: list[str] = []
    dangerous: bool = False
    operation_param: str | None = None
    operation_default: str | None = None


# n8n-mcp 2.96.2：(name, group, operation_param, operations, dangerous)。只在讀不到上游時使用。
_FALLBACK_ROWS: list[tuple[str, str, str | None, list[str], bool]] = [
    ("tools_documentation", "documentation", None, [], False),
    ("search_nodes", "documentation", None, [], False),
    ("get_node", "documentation", None, [], False),
    ("validate_node", "documentation", None, [], False),
    ("get_template", "documentation", None, [], False),
    ("search_templates", "documentation", None, [], False),
    ("validate_workflow", "documentation", None, [], False),
    ("n8n_create_workflow", "management", None, [], False),
    ("n8n_get_workflow", "management", None, [], False),
    ("n8n_update_full_workflow", "management", None, [], False),
    ("n8n_update_partial_workflow", "management", None, [], False),
    ("n8n_delete_workflow", "management", None, [], True),
    ("n8n_list_workflows", "management", None, [], False),
    ("n8n_validate_workflow", "management", None, [], False),
    ("n8n_autofix_workflow", "management", None, [], False),
    ("n8n_test_workflow", "management", "method", ["auto", "trigger", "prepare", "pinned", "direct", "expose"], True),
    ("n8n_executions", "management", "action", ["get", "list", "delete"], True),
    ("n8n_evaluations", "management", "action", ["list_runs", "get_run", "list_cases", "run", "cancel"], True),
    ("n8n_health_check", "management", None, [], False),
    ("n8n_workflow_versions", "management", "mode",
     ["list", "get", "rollback", "delete", "prune", "diff", "expose"], True),
    ("n8n_deploy_template", "management", None, [], False),
    ("n8n_manage_datatable", "management", "action",
     ["createtable", "listtables", "gettable", "updatetable", "deletetable", "getrows", "insertrows",
      "updaterows", "upsertrows", "deleterows", "addcolumn", "deletecolumn", "renamecolumn"], True),
    ("n8n_manage_folders", "management", "action", ["create", "list", "get", "rename", "move", "delete"], True),
    ("n8n_manage_credentials", "management", "action", ["list", "get", "create", "update", "delete", "getschema"], True),
    ("n8n_audit_instance", "management", None, [], False),
    ("n8n_manage_agents", "management", "action",
     ["reference", "search", "get", "create", "mutate", "validate", "call", "publish", "unpublish", "revert",
      "versions", "delete", "discover_assets", "verify_mcp_server", "update_integration"], True),
    ("n8n_explore_node_resources", "management", None, [], False),
    ("n8n_list_catalog", "management", "kind", ["projects", "tags"], False),
]

FALLBACK_TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name=name,
        category=_GROUP_CATEGORY[group],
        description="",
        operations=list(ops),
        dangerous=dangerous,
        operation_param=param,
        operation_default={"n8n_executions": "list", "n8n_test_workflow": "auto",
                           "n8n_workflow_versions": "list"}.get(name),
    )
    for name, group, param, ops, dangerous in _FALLBACK_ROWS
]

# create_tools_router 讀模組屬性 TOOL_REGISTRY；refresh() 原地替換內容，讓既有參照也看到新清單。
TOOL_REGISTRY: list[ToolDefinition] = list(FALLBACK_TOOLS)
UPSTREAM_VERSION: str | None = None
_loaded = False


def _short_description(text: str) -> str:
    first = text.strip().split("\n\n", 1)[0].replace("\n", " ").strip()
    return first if len(first) <= _DESCRIPTION_LIMIT else first[: _DESCRIPTION_LIMIT - 1].rstrip() + "…"


def tools_from_catalog(catalog: dict[str, Any]) -> list[ToolDefinition]:
    tools: list[ToolDefinition] = []
    for raw in catalog.get("tools", []):
        category = _GROUP_CATEGORY.get(raw.get("group", ""))
        name = raw.get("name")
        if category is None or not isinstance(name, str) or not name:
            continue
        annotations = raw.get("annotations") or {}
        operations = [str(o) for o in raw.get("operations") or []]
        # Keep reviewed metadata available for an unpatched local catalog too.
        # The image's source-bound policy patch adds the same native selector.
        if name == "n8n_manage_credentials":
            operations = ["list", "get", "create", "update", "delete", "getschema"]
        tools.append(
            ToolDefinition(
                name=name,
                category=category,
                description=_short_description(str(raw.get("description", ""))),
                operations=operations,
                dangerous=bool(annotations.get("destructiveHint")) or bool(raw.get("destructive_operations")),
                operation_param="action" if name == "n8n_manage_credentials" else raw.get("operation_param"),
                operation_default=raw.get("operation_default", {
                    "n8n_executions": "list", "n8n_test_workflow": "auto",
                    "n8n_workflow_versions": "list"}.get(name)),
            )
        )
    return tools


async def load_upstream_catalog() -> dict[str, Any]:
    """跑 ``node upstream_catalog.js <套件目錄>``，回傳解析後的 JSON。"""
    pkg_dir = await asyncio.to_thread(upstream.package_dir)
    env = {k: v for k, v in os.environ.items() if k != "JWT_SECRET"}
    proc = await asyncio.create_subprocess_exec(
        "node",
        str(CATALOG_SCRIPT),
        pkg_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=CATALOG_TIMEOUT_SECONDS)
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise RuntimeError("upstream catalog timed out")
    if proc.returncode != 0:
        tail = stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
        raise RuntimeError(f"upstream catalog exited {proc.returncode}: {' | '.join(tail)}")
    lines = [ln for ln in stdout.decode("utf-8", "replace").splitlines() if ln.strip()]
    return json.loads(lines[-1])


async def refresh() -> None:
    """第一次被呼叫時讀上游清單（成功後不再重讀；失敗時下次再試）。"""
    global _loaded, UPSTREAM_VERSION  # noqa: PLW0603
    if _loaded:
        return
    # 併發的兩次 refresh 只會各讀一次、結果相同，不另加鎖（避免跨 event loop 的 Lock）。
    catalog = await load_upstream_catalog()
    tools = tools_from_catalog(catalog)
    if not tools:
        raise RuntimeError("upstream catalog returned no tools")
    TOOL_REGISTRY[:] = tools
    UPSTREAM_VERSION = str(catalog.get("version") or "") or None
    _loaded = True
    logger.info("Loaded %d tools from upstream n8n-mcp %s", len(tools), UPSTREAM_VERSION)


def get_tool_by_name(name: str) -> ToolDefinition | None:
    """Look up a tool by its name."""
    for tool in TOOL_REGISTRY:
        if tool.name == name:
            return tool
    return None


def get_tools_by_category(category: ToolCategory) -> list[ToolDefinition]:
    """Return all tools in a given category."""
    return [t for t in TOOL_REGISTRY if t.category == category]


def get_all_tool_names() -> list[str]:
    """Return a flat list of all tool names."""
    return [t.name for t in TOOL_REGISTRY]


def operation_tools() -> dict[str, list[str]]:
    """可依操作停用的工具 → 合法操作名（上游 TOOL_OPERATION_PARAM 範圍）。"""
    return {t.name: list(t.operations) for t in TOOL_REGISTRY if t.operations}


def get_categorized_tools() -> dict[str, list[dict[str, Any]]]:
    """Return tools grouped by category as serialisable dicts."""
    result: dict[str, list[dict[str, Any]]] = {}
    for cat in ToolCategory:
        result[cat.value] = [t.model_dump() for t in TOOL_REGISTRY if t.category == cat]
    return result


def get_operation_policies() -> dict[str, OperationSpec]:
    # n8n-mcp TOOL_OPERATION_PARAM/DEFAULT + getValidOperations are authoritative.
    # Credentials use the reviewed server.ts switch + native build patch.
    # optionalEmptyAware maps null/blank to defaults; matching lowercases without
    # trimming nonempty values. exposeToMcp is a separate consent write.
    return {tool.name: OperationSpec(
        tuple(tool.operations), tool.operation_param, tool.operation_default,
        normalization="lower", empty_uses_default=True,
        boolean_operations={"exposeToMcp": "expose"}
            if tool.name in {"n8n_test_workflow", "n8n_workflow_versions"} else {},
    ) for tool in TOOL_REGISTRY if tool.operation_param and tool.operations}


def get_conditional_writes(name, disabled_tools, disabled_operations) -> list[dict]:
    """顯示既有條件寫入的相依權限，不新增授權或改寫儲存政策。"""
    if name not in EXPOSURE_TOOLS:
        return []
    return [{"parameter": "exposeToMcp", "requires_tool": "n8n_update_partial_workflow",
             "policy_enabled": not conditional_call_disabled(
                 name, {"exposeToMcp": True}, disabled_tools, disabled_operations)}]
