"""Hermes MCP tool registry with all 11 tools grouped into 3 categories."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel

from mcp_admin_core.operation_policy import OperationSpec


class ToolCategory(str, Enum):
    """Categories for Hermes MCP tools."""

    READ = "Read"
    WRITE = "Write"
    AGENT = "Agent"


class ToolDefinition(BaseModel):
    """Definition of a single Hermes MCP tool."""

    name: str
    category: ToolCategory
    description: str
    operations: list[str] = []
    dangerous: bool = False


# ---------------------------------------------------------------------------
# Tool Registry: 11 tools in 3 categories (2 Read + 8 Write + 1 Agent)
# ---------------------------------------------------------------------------

TOOL_REGISTRY: list[ToolDefinition] = [
    # -----------------------------------------------------------------------
    # Read (2 tools) - read-only inspection and session queries
    # -----------------------------------------------------------------------
    ToolDefinition(
        name="hermes_inspect",
        category=ToolCategory.READ,
        description="Inspect Hermes Gateway capabilities, Dashboard config, and model info",
        operations=["all", "capabilities", "config", "model", "status"],
    ),
    ToolDefinition(
        name="hermes_session",
        category=ToolCategory.READ,
        description="List, get, or delete Hermes chat sessions",
        operations=["list", "get", "delete"],
    ),

    # -----------------------------------------------------------------------
    # Write (8 tools) - mutating operations with dry_run support
    # -----------------------------------------------------------------------
    ToolDefinition(
        name="hermes_skill",
        category=ToolCategory.WRITE,
        description="Manage Hermes skills (list, enable, disable). Supports dry_run mode",
        operations=["list", "enable", "disable"],
        dangerous=True,
    ),
    ToolDefinition(
        name="hermes_mcp",
        category=ToolCategory.WRITE,
        description="Manage Hermes MCP server connections (list, add, remove). Supports dry_run mode",
        operations=["list", "add", "remove"],
        dangerous=True,
    ),
    ToolDefinition(
        name="hermes_model",
        category=ToolCategory.WRITE,
        description="Manage Hermes model configuration (info, set, list providers). Supports dry_run mode",
        operations=["info", "set", "list_providers"],
        dangerous=True,
    ),
    ToolDefinition(
        name="hermes_config",
        category=ToolCategory.WRITE,
        description="Read or update Hermes Dashboard configuration keys. Supports dry_run mode",
        operations=["get", "set"],
        dangerous=True,
    ),
    ToolDefinition(
        name="hermes_tools",
        category=ToolCategory.WRITE,
        description="Manage Hermes toolsets (list, enable, disable). Supports dry_run mode",
        operations=["list", "enable", "disable"],
        dangerous=True,
    ),
    ToolDefinition(
        name="hermes_gateway",
        category=ToolCategory.WRITE,
        description="Manage Hermes Gateway (status, restart). Supports dry_run mode",
        operations=["status", "restart"],
        dangerous=True,
    ),
    ToolDefinition(
        name="hermes_cron",
        category=ToolCategory.WRITE,
        description="Manage Hermes cron/scheduled jobs (list, create, update, delete, trigger, pause, resume)",
        operations=["list", "create", "update", "delete", "trigger", "pause", "resume"],
        dangerous=True,
    ),
    ToolDefinition(
        name="hermes_webhook",
        category=ToolCategory.WRITE,
        description="Manage Hermes webhooks (list, enable_platform, create, delete, toggle)",
        operations=["list", "enable_platform", "create", "delete", "toggle"],
        dangerous=True,
    ),

    # -----------------------------------------------------------------------
    # Agent (1 tool) - interactive chat
    # -----------------------------------------------------------------------
    ToolDefinition(
        name="hermes_chat",
        category=ToolCategory.AGENT,
        description="Send a chat message to Hermes and receive a response",
        operations=["chat"],
    ),
]


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


def get_categorized_tools() -> dict[str, list[dict[str, Any]]]:
    """Return tools grouped by category as serialisable dicts."""
    result: dict[str, list[dict[str, Any]]] = {}
    for cat in ToolCategory:
        result[cat.value] = [
            t.model_dump() for t in TOOL_REGISTRY if t.category == cat
        ]
    return result


# Reviewed against hermes_mcp_server/server.py handlers, not schema enums.
OPERATION_POLICIES = {
    "hermes_inspect": OperationSpec(
        ("all", "capabilities", "config", "model", "status"), "target", "all",
        includes={"all": ("capabilities", "config", "model", "status")},
        legacy_denies={"read": ("all", "capabilities", "config", "model", "status")}),
    "hermes_session": OperationSpec(("list", "get", "delete"), "action", "list"),
    "hermes_skill": OperationSpec(("list", "enable", "disable"), "action"),
    "hermes_mcp": OperationSpec(("list", "add", "remove"), "action"),
    "hermes_model": OperationSpec(("info", "set", "list_providers"), "action", "info",
                                  legacy_denies={"list": ("list_providers",)}),
    "hermes_config": OperationSpec(("get", "set"), "action", "get"),
    "hermes_tools": OperationSpec(("list", "enable", "disable"), "action", "list"),
    "hermes_gateway": OperationSpec(("status", "restart"), "action", "status"),
    "hermes_chat": OperationSpec(("chat",), default="chat"),
    "hermes_cron": OperationSpec(("list", "create", "update", "delete", "trigger", "pause", "resume"), "action", "list"),
    "hermes_webhook": OperationSpec(("list", "enable_platform", "create", "delete", "toggle"), "action", "list"),
}
