"""Tool and reviewed operation management; saved proxy restrictions apply live."""
from __future__ import annotations

from typing import Annotated
from fastapi import Request, Header, Response
from ..configuration import ConfigurationResponse, configuration_status, revision_header, run_configuration_transition

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from mcp_admin_core.config import get_config_store
from mcp_admin_core.config.store import ConfigUnavailable
from mcp_admin_core.operation_policy import (
    conditional_writes_for, editable_denies, operation_notes_for, operation_status, policies_for, read_policy,
    validate_operation_update,
)
from mcp_admin_core.process import get_process_manager

logger = logging.getLogger(__name__)


class ToolStatus(BaseModel):
    name: str
    category: str
    description: str
    operations: list[str] = Field(default_factory=list)
    operation_policy_supported: bool = False
    operation_parameter: str | None = None
    operation_default: str | None = None
    operation_selector: str | None = None
    operation_dependencies: dict[str, list[str]] = Field(default_factory=dict)
    operation_flags: dict[str, str] = Field(default_factory=dict)
    operation_legacy_denies: dict[str, list[str]] = Field(default_factory=dict)
    conditional_writes: list[dict[str, Any]] = Field(default_factory=list)
    # 產品覆核過的說明（Permissions 頁顯示在工具旁），例如 Odoo 審批工具的限制擋不住 execute_method。
    operation_note: str | None = None
    dangerous: bool = False
    enabled: bool = True  # whole-tool toggle, independent of operation restrictions
    policy_enabled: bool = True


class ToolListResponse(ConfigurationResponse):
    total: int
    enabled: int
    disabled: int
    disabled_tools: list[str]
    disabled_operations: dict[str, list[str]]
    editable_disabled_operations: dict[str, list[str]]
    operation_policy_supported: bool
    categories: dict[str, list[ToolStatus]]
    tools: list[ToolStatus] = Field(default_factory=list)


class ToolsUpdateRequest(BaseModel):
    disabled_tools: list[str] = Field(default_factory=list)
    tools: Any = None


class OperationsUpdateRequest(BaseModel):
    disabled_operations: dict[str, list[str]] = Field(default_factory=dict)


def create_tools_router(*, registry, mcp_label: str, operations_supported: bool = False) -> APIRouter:
    """Use reviewed registry selectors; operations_supported retains n8n env sync.

    Products with OperationSpec are enforced by the shared runtime proxy. n8n
    additionally restarts its child to synchronize native env/schema filtering.
    """
    router = APIRouter(prefix="/api/tools", tags=["tools"])

    async def _refresh_registry() -> None:
        refresh = getattr(registry, "refresh", None)
        if refresh is None:
            return
        try:
            await refresh()
        except Exception as exc:
            logger.warning("Tool registry refresh failed for %s: %s", mcp_label, exc)

    def _build(tool, disabled, disabled_ops) -> ToolStatus:
        enabled = tool.name not in disabled
        state = operation_status(policies_for(registry).get(tool.name), disabled_ops.get(tool.name, []), enabled)
        return ToolStatus(
            name=tool.name, category=tool.category.value, description=tool.description,
            dangerous=tool.dangerous, enabled=enabled,
            conditional_writes=conditional_writes_for(registry, tool.name, disabled, disabled_ops),
            operation_note=operation_notes_for(registry).get(tool.name),
            **{**state, "operations": [operation["name"] for operation in state["operations"]]},
        )

    @router.get("", response_model=ToolListResponse)
    async def list_tools(response: Response = None) -> ToolListResponse:
        await _refresh_registry()
        store = get_config_store()
        snapshot = await store.load()
        configuration = configuration_status(store, snapshot, get_process_manager())
        revision_header(response, configuration)
        disabled, disabled_ops = read_policy(snapshot.get("tools", {}))
        categories: dict[str, list[ToolStatus]] = {category.value: [] for category in registry.ToolCategory}
        tools = [_build(tool, disabled, disabled_ops) for tool in registry.TOOL_REGISTRY]
        for tool in tools:
            categories[tool.category].append(tool)
        enabled_count = sum(tool.enabled for tool in tools)
        return ToolListResponse(
            configuration=configuration,
            total=len(tools), enabled=enabled_count, disabled=len(tools) - enabled_count,
            disabled_tools=sorted(disabled), disabled_operations=disabled_ops,
            editable_disabled_operations=editable_denies(disabled_ops, registry),
            operation_policy_supported=bool(policies_for(registry)), categories=categories, tools=tools,
        )

    @router.put("", response_model=dict[str, Any])
    async def update_tools(payload: ToolsUpdateRequest, response: Response = None, if_match: Annotated[str | None, Header(alias="If-Match")] = None, request: Request = None) -> dict[str, Any]:
        async def apply_update():
            disabled = list(payload.disabled_tools)
            if payload.tools and not disabled:
                if isinstance(payload.tools, dict):
                    disabled = [name for name, enabled in payload.tools.items() if not enabled]
                elif isinstance(payload.tools, list):
                    disabled = [tool.get("name") for tool in payload.tools
                                if isinstance(tool, dict) and not tool.get("enabled", True)]
            if any(not isinstance(name, str) or not name for name in disabled):
                raise HTTPException(400, "Disabled tool names must be nonempty strings")
            store = get_config_store()
            try:
                read_policy(await store.get("tools", {}))
                # Atomic sibling update: never restore an earlier disabled_operations.
                current = await store.patch("tools", {"disabled": sorted(set(disabled))})
                disabled_set, disabled_ops = read_policy(current)
            except ConfigUnavailable:
                raise
            except Exception as exc:
                logger.error("Failed to update disabled tools (%s)", type(exc).__name__)
                raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Failed to save tool policy") from exc
            return {
                "status": "ok", "saved": True, "apply_state": "proxy_policy_applied",
                "updated": len(disabled_set),
                "message": "Tool policy saved. The proxy blocks disabled tools immediately; refresh availability to verify enabled tools in the child.",
                "disabled_tools": sorted(disabled_set),
                "tools": [_build(tool, disabled_set, disabled_ops).model_dump() for tool in registry.TOOL_REGISTRY],
            }

        return await run_configuration_transition(get_config_store(), get_process_manager(), if_match, apply_update, response, request=request)

    @router.put("/operations", response_model=dict[str, Any])
    async def update_operations(payload: OperationsUpdateRequest, response: Response = None, if_match: Annotated[str | None, Header(alias="If-Match")] = None, request: Request = None) -> dict[str, Any]:
        async def apply_update():
            await _refresh_registry()
            if not policies_for(registry):
                raise HTTPException(400, f"{mcp_label} has no reviewed operation selectors; disable the whole tool in Tools instead")
            store = get_config_store()
            try:
                normalized = validate_operation_update(payload.disabled_operations, await store.get("tools", {}), registry)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            try:
                await store.patch("tools", {"disabled_operations": normalized})
            except ConfigUnavailable:
                raise
            except Exception as exc:
                logger.error("Failed to update disabled operations (%s)", type(exc).__name__)
                raise HTTPException(500, "Failed to save operation policy") from exc
            result = {
                "status": "ok", "saved": True, "apply_state": "proxy_policy_applied",
                "message": f"{mcp_label} operation policy saved. The proxy enforces restrictions immediately; child settings may further limit allowed operations.",
                "disabled_operations": normalized,
            }
            if operations_supported:
                # Preserve n8n's native env/filter handling, especially on re-enable.
                try:
                    ok = await get_process_manager().start_or_restart()
                except Exception as exc:
                    ok = False
                    logger.error("Failed to sync %s operation policy (%s)", mcp_label, type(exc).__name__)
                result["child_apply_state"] = "applied" if ok else "pending"
                if not ok:
                    result.update(status="partial", message=f"{mcp_label} restrictions are enforced by the proxy. Child policy synchronization is pending; newly allowed operations may require a child restart.")
            return result

        return await run_configuration_transition(get_config_store(), get_process_manager(), if_match, apply_update, response, request=request)

    return router
