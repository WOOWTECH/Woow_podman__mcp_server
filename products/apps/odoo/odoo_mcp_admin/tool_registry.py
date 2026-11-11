"""Registry of all 41 Odoo MCP tools organized into 10 categories.

Each tool entry contains:
- name: The MCP tool function name
- description: Human-readable description of what the tool does
- category: Grouping category for UI display
- enabled_by_default: Whether the tool is enabled when first deployed
- dangerous: Whether the tool performs write/modify operations
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel

from mcp_admin_core.operation_policy import OperationSpec


class ToolCategory(str, Enum):
    """Categories for organizing Odoo MCP tools."""

    READ_DISCOVER = "Read & Discover"
    WRITE_OPERATE = "Write & Operate"
    DIAGNOSE = "Diagnose"
    MIGRATE = "Migrate"
    AUDIT_PLAN = "Audit & Plan"
    KNOWLEDGE = "Knowledge"
    ACCOUNTING = "Accounting"
    BACKGROUND_TASKS = "Background Tasks"
    CROSS_INSTANCE = "Cross-Instance"
    UTILITY = "Utility"


class ToolDefinition(BaseModel):
    """Schema for a single MCP tool definition."""

    name: str
    description: str
    category: ToolCategory
    enabled_by_default: bool = True
    dangerous: bool = False
    operations: list[str] = []


class ToolState(BaseModel):
    """Runtime state of a tool (definition + current enabled status)."""

    name: str
    description: str
    category: ToolCategory
    enabled_by_default: bool
    dangerous: bool
    enabled: bool


class ToolUpdateRequest(BaseModel):
    """Request body for updating tool enabled states.

    Accepts either format:
    - dict: ``{"tools": {"tool_name": true/false, ...}}``
    - list: ``{"tools": [{"name": "...", "enabled": true/false, ...}, ...]}``
    """

    tools: Any  # dict[str, bool] or list[dict] from frontend


class ToolUpdateResponse(BaseModel):
    """Response after updating tool states."""

    updated: int
    tools: list[ToolState]


# ---------------------------------------------------------------------------
# Complete registry of all 41 Odoo MCP tools
# ---------------------------------------------------------------------------

TOOL_REGISTRY: list[ToolDefinition] = [
    # ── Read & Discover (11) ──────────────────────────────────────────────
    ToolDefinition(
        name="list_models",
        description="List all available Odoo models with optional filtering",
        category=ToolCategory.READ_DISCOVER,
    ),
    ToolDefinition(
        name="get_model_fields",
        description="Get field definitions for a specific Odoo model",
        category=ToolCategory.READ_DISCOVER,
    ),
    ToolDefinition(
        name="search_records",
        description="Search and read records with domain filters and field selection",
        category=ToolCategory.READ_DISCOVER,
    ),
    ToolDefinition(
        name="read_record",
        description="Read a single record by ID with specified fields",
        category=ToolCategory.READ_DISCOVER,
    ),
    ToolDefinition(
        name="aggregate_records",
        description="Perform grouped aggregation queries (sum, avg, count, etc.)",
        category=ToolCategory.READ_DISCOVER,
    ),
    ToolDefinition(
        name="search_employee",
        description="Search HR employees with name/department/job filters",
        category=ToolCategory.READ_DISCOVER,
    ),
    ToolDefinition(
        name="search_holidays",
        description="Search leave/time-off records for employees",
        category=ToolCategory.READ_DISCOVER,
    ),
    ToolDefinition(
        name="get_odoo_profile",
        description="Get Odoo instance profile: version, DB name, installed modules",
        category=ToolCategory.READ_DISCOVER,
    ),
    ToolDefinition(
        name="schema_catalog",
        description="Browse the full model schema catalog with relationship mapping",
        category=ToolCategory.READ_DISCOVER,
    ),
    ToolDefinition(
        name="build_domain",
        description="Interactively build Odoo domain filters with field validation",
        category=ToolCategory.READ_DISCOVER,
    ),
    ToolDefinition(
        name="read_attachment",
        description="Read binary attachment content by ID",
        category=ToolCategory.READ_DISCOVER,
    ),
    # ── Write & Operate (5) ───────────────────────────────────────────────
    ToolDefinition(
        name="preview_write",
        description="Preview a write operation without executing (dry run)",
        category=ToolCategory.WRITE_OPERATE,
        dangerous=True,
    ),
    ToolDefinition(
        name="validate_write",
        description="Validate a write operation and return approval token",
        category=ToolCategory.WRITE_OPERATE,
        dangerous=True,
    ),
    ToolDefinition(
        name="execute_approved_write",
        description="Execute a previously validated and approved write operation",
        category=ToolCategory.WRITE_OPERATE,
        dangerous=True,
    ),
    ToolDefinition(
        name="execute_method",
        description="Call an arbitrary Odoo model method with arguments",
        category=ToolCategory.WRITE_OPERATE,
        dangerous=True,
    ),
    ToolDefinition(
        name="chatter_post",
        description="Post a message to a record's chatter/mail thread",
        category=ToolCategory.WRITE_OPERATE,
        dangerous=True,
    ),
    # ── Diagnose (4) ─────────────────────────────────────────────────────
    ToolDefinition(
        name="diagnose_odoo_call",
        description="Diagnose a failed Odoo XML-RPC call with error analysis",
        category=ToolCategory.DIAGNOSE,
    ),
    ToolDefinition(
        name="diagnose_access",
        description="Diagnose access rights issues for a model/user combination",
        category=ToolCategory.DIAGNOSE,
    ),
    ToolDefinition(
        name="inspect_model_relationships",
        description="Inspect relational fields and model dependencies",
        category=ToolCategory.DIAGNOSE,
    ),
    ToolDefinition(
        name="analyze_upgrade_log",
        description="Classify Odoo upgrade log failures without contacting Odoo",
        category=ToolCategory.DIAGNOSE,
    ),
    # ── Migrate (3) ──────────────────────────────────────────────────────
    ToolDefinition(
        name="generate_json2_payload",
        description="Generate JSON2 import payload for data migration",
        category=ToolCategory.MIGRATE,
    ),
    ToolDefinition(
        name="upgrade_risk_report",
        description="Analyze upgrade risks between Odoo versions",
        category=ToolCategory.MIGRATE,
    ),
    ToolDefinition(
        name="lookup_model_history",
        description="Look up model field changes across Odoo version history",
        category=ToolCategory.MIGRATE,
    ),
    # ── Audit & Plan (4) ─────────────────────────────────────────────────
    ToolDefinition(
        name="scan_addons_source",
        description="Scan custom addon source code for patterns and issues",
        category=ToolCategory.AUDIT_PLAN,
    ),
    ToolDefinition(
        name="fit_gap_report",
        description="Generate fit/gap analysis report for requirements vs modules",
        category=ToolCategory.AUDIT_PLAN,
    ),
    ToolDefinition(
        name="business_pack_report",
        description="Generate business pack report for installed module analysis",
        category=ToolCategory.AUDIT_PLAN,
    ),
    ToolDefinition(
        name="data_quality_report",
        description="Read sampled Odoo records to report data quality without modifying them",
        category=ToolCategory.AUDIT_PLAN,
    ),
    # ── Knowledge (3) ────────────────────────────────────────────────────
    ToolDefinition(
        name="index_knowledge",
        description="Index Odoo knowledge base articles for semantic search",
        category=ToolCategory.KNOWLEDGE,
    ),
    ToolDefinition(
        name="search_knowledge",
        description="Search indexed knowledge base with semantic matching",
        category=ToolCategory.KNOWLEDGE,
    ),
    ToolDefinition(
        name="knowledge_stats",
        description="Get knowledge index statistics and coverage metrics",
        category=ToolCategory.KNOWLEDGE,
    ),
    # ── Accounting (2) ───────────────────────────────────────────────────
    ToolDefinition(
        name="receivable_payable_aging",
        description="Generate accounts receivable/payable aging report",
        category=ToolCategory.ACCOUNTING,
    ),
    ToolDefinition(
        name="accounting_health_summary",
        description="Generate accounting health summary with key metrics",
        category=ToolCategory.ACCOUNTING,
    ),
    # ── Background Tasks (4) ─────────────────────────────────────────────
    ToolDefinition(
        name="submit_async_task",
        description="Submit a long-running task for background execution",
        category=ToolCategory.BACKGROUND_TASKS,
    ),
    ToolDefinition(
        name="get_async_task",
        description="Get status and result of a background task by ID",
        category=ToolCategory.BACKGROUND_TASKS,
    ),
    ToolDefinition(
        name="cancel_async_task",
        description="Cancel a running or queued background task",
        category=ToolCategory.BACKGROUND_TASKS,
    ),
    ToolDefinition(
        name="list_async_tasks",
        description="List all background tasks with status filtering",
        category=ToolCategory.BACKGROUND_TASKS,
    ),
    # ── Cross-Instance (3) ───────────────────────────────────────────────
    ToolDefinition(
        name="search_across_instances",
        description="Search records across multiple Odoo instances simultaneously",
        category=ToolCategory.CROSS_INSTANCE,
    ),
    ToolDefinition(
        name="aggregate_across_instances",
        description="Aggregate data across multiple Odoo instances",
        category=ToolCategory.CROSS_INSTANCE,
    ),
    ToolDefinition(
        name="accounting_health_across_instances",
        description="Compare accounting health across multiple Odoo instances",
        category=ToolCategory.CROSS_INSTANCE,
    ),
    # ── Utility (2) ──────────────────────────────────────────────────────
    ToolDefinition(
        name="health_check",
        description="Check MCP server health and Odoo connectivity",
        category=ToolCategory.UTILITY,
    ),
    ToolDefinition(
        name="list_instances",
        description="List all configured Odoo instances and connection status",
        category=ToolCategory.UTILITY,
    ),
]

# Pre-built lookup for fast access by tool name
TOOL_BY_NAME: dict[str, ToolDefinition] = {t.name: t for t in TOOL_REGISTRY}

# Pre-built lookup by category
TOOLS_BY_CATEGORY: dict[ToolCategory, list[ToolDefinition]] = {}
for _tool in TOOL_REGISTRY:
    TOOLS_BY_CATEGORY.setdefault(_tool.category, []).append(_tool)


def get_tool_states(enabled_overrides: dict[str, bool] | None = None) -> list[ToolState]:
    """Build the full tool state list, applying any enabled overrides.

    Args:
        enabled_overrides: Dict mapping tool name to enabled bool.
            Tools not in this dict use their ``enabled_by_default`` value.

    Returns:
        List of ToolState objects with resolved enabled status.
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
    """Return a summary of tools per category.

    Returns:
        Dict mapping category value to {count, tool_names}.
    """
    return {
        cat.value: {
            "count": len(tools),
            "tools": [t.name for t in tools],
        }
        for cat, tools in TOOLS_BY_CATEGORY.items()
    }


def get_all_tool_names() -> list[str]:
    """Return a flat list of all tool names."""
    return [t.name for t in TOOL_REGISTRY]


def get_tool_by_name(name: str) -> ToolDefinition | None:
    """Look up a tool by its name."""
    return TOOL_BY_NAME.get(name)


# Reviewed odoo-mcp 1.1.0, re-checked for 1.3.0 (#78, selectors and ASYNC_OPERATIONS unchanged):
# tools_write.py and tools_async.ASYNC_OPERATIONS.
# execute_method accepts arbitrary model methods, so it has no finite selector
# policy here and must be disabled as a whole when that boundary is required.
OPERATION_POLICIES = {
    "preview_write": OperationSpec(("create", "write", "unlink"), "operation", normalization="strip_lower"),
    "validate_write": OperationSpec(("create", "write", "unlink"), "operation", normalization="strip_lower"),
    "execute_approved_write": OperationSpec(("create", "write", "unlink"), "approval.operation", normalization="strip_lower"),
    "submit_async_task": OperationSpec(("data_quality_report", "scan_addons_source", "index_knowledge",
        "receivable_payable_aging", "search_across_instances", "aggregate_across_instances",
        "accounting_health_across_instances"), "operation"),
}
for _tool in TOOL_REGISTRY:
    if _tool.name in OPERATION_POLICIES:
        _tool.operations = list(OPERATION_POLICIES[_tool.name].operations)

#: Permissions 頁在這幾支工具旁邊顯示的說明（/api/tools 的 operation_note）。三支審批工具的 create／write／unlink 限制擋不住
#: execute_method：全開之後直接寫入是預設，execute_method 照樣直接 create／write／unlink（上面說過它沒有有限的 selector，
#: 只能整支停用）。不寫清楚的話，管理員在這裡關掉 unlink 會以為不能刪（審查第三輪；不是第三輪引入的）。
APPROVAL_TOOLS_NOTE = (
    "這裡的 create／write／unlink 只限制三階段審批（preview_write → validate_write → execute_approved_write）。"
    "Allow Direct Writes 開著時（預設），execute_method 可以直接 create／write／unlink（Allow Unreviewed Methods 也開時"
    "還能直接執行 action_*、copy 之類會改資料的方法），不受這裡的限制；要禁止，請到 Settings 關掉 Allow Direct Writes，"
    "或在 Tools 頁停用 execute_method。"
)
OPERATION_NOTES = {name: APPROVAL_TOOLS_NOTE for name in ("preview_write", "validate_write", "execute_approved_write")}
