"""Reviewed PaaS catalog and selectors; no authorization inferred from schemas."""
from dataclasses import dataclass

from mcp_admin_core.operation_policy import OperationSpec
from emqx_mcp_server.registry import TOOL_REGISTRY as VENDOR_TOOLS, ToolCategory

SQL_TOOL = "emqx_test_rule_sql"
MIXED = frozenset({"emqx_manage_authn_users", "emqx_manage_authz_rules"})


@dataclass(frozen=True)
class ToolSpec:
    name: str
    category: ToolCategory
    description: str
    operations: tuple[str, ...]
    dangerous: bool
    access: str


TOOL_REGISTRY = tuple(ToolSpec(
    t.name, t.category, t.description,
    ("execute",) if t.name == SQL_TOOL else tuple(t.operations),
    t.dangerous or t.name == SQL_TOOL,
    "write" if t.name == SQL_TOOL else "destructive" if t.dangerous else "read",
) for t in VENDOR_TOOLS)
TOOLS_BY_NAME = {t.name: t for t in TOOL_REGISTRY}
OPERATION_POLICIES = {
    t.name: OperationSpec(t.operations, parameter="operation" if t.name in MIXED else None,
                          default=None if t.name in MIXED else t.operations[0])
    for t in TOOL_REGISTRY
}
OPERATION_NOTES = {
    SQL_TOOL: "SQL testing executes broker-side work and is not classified as a read. Its execute permission starts enabled under the all-on default and can be revoked independently.",
}


def get_all_tool_names():
    return list(TOOLS_BY_NAME)


def get_tool_by_name(name):
    return TOOLS_BY_NAME.get(name)


def normalize_arguments(name, arguments):
    if name not in TOOLS_BY_NAME or not isinstance(arguments, dict):
        raise ValueError("Unknown EMQX tool or invalid arguments")
    if name in MIXED and OPERATION_POLICIES[name].resolve(arguments) is None:
        raise ValueError("An exact read/create/delete operation is required")
    return dict(arguments)
