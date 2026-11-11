"""Task-oriented schemas and reviewed operation selectors shared by UI and MCP."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Literal, Union

from mcp_admin_core.operation_policy import OperationSpec
from pydantic import Field, TypeAdapter, ValidationError, create_model

from .errors import HeadscaleError
from .models import Arguments, AuthId, Bool, Digest, Id, NonemptyTags, NodeName, PolicyText, Prefix, Routes, Tags, Text, Timestamp, Username
from .native import NATIVE_OPERATIONS


class ToolCategory(str, Enum):
    USERS = "Users"
    ENROLLMENT = "Enrollment & SSH authentication"
    DEVICES = "Devices"
    ROUTES = "Routes"
    POLICY = "Access policy"
    CREDENTIALS = "API credentials"
    MAINTENANCE = "Maintenance"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    category: ToolCategory
    description: str
    operations: tuple[str, ...]
    dangerous: bool
    access: str


# Native operation -> tool and task operation. Composite policy apply also checks
# and reads policy; diff is local and does not invent a thirtieth native operation.
_GROUPS = {
    "headscale_users": (ToolCategory.USERS, "Manage users; rename updates the Headscale user name, including OIDC users. It does not change the identity provider account. Name synchronization at the next IdP login is not guaranteed.", {
        "list": ("ListUsers", {"user_id": (Id, None), "name": (Username, None), "email": (Text, None)}),
        "create": ("CreateUser", {"name": (Username, ...), "display_name": (Text, ""), "email": (Text, ""), "picture_url": (Text, "")}),
        "rename": ("RenameUser", {"user_id": (Id, ...), "new_name": (Username, ...)}),
        "delete": ("DeleteUser", {"user_id": (Id, ...)})}),
    "headscale_nodes": (ToolCategory.DEVICES, "Inspect devices, rename, set expiry, delete or convert to tagged ownership. Tags must exist in policy; tagging cannot be undone.", {
        "list": ("ListNodes", {"user": (Username, None)}),
        "get": ("GetNode", {"node_id": (Id, ...)}),
        "rename": ("RenameNode", {"node_id": (Id, ...), "new_name": (NodeName, ...)}),
        "set_tags": ("SetTags", {"node_id": (Id, ...), "tags": (NonemptyTags, ...)}),
        "expire": ("ExpireNode", {"node_id": (Id, ...), "mode": (Literal["now", "at", "disable"], "now"), "expiry": (Timestamp, None)}),
        "delete": ("DeleteNode", {"node_id": (Id, ...)})}),
    "headscale_register": (ToolCategory.ENROLLMENT, "Complete an existing client registration using user name and auth ID. Legacy selects the query-based compatibility endpoint.", {
        "register": ("AuthRegister", {"user": (Username, ...), "auth_id": (AuthId, ...)}),
        "legacy": ("RegisterNode", {"user": (Username, ...), "auth_id": (AuthId, ...)})}),
    "headscale_preauth_keys": (ToolCategory.ENROLLMENT, "Manage enrollment keys. Existing secrets are always redacted; creation requires explicit reveal_once and expiration.", {
        "list": ("ListPreAuthKeys", {}),
        "create": ("CreatePreAuthKey", {"user_id": (Id, None), "tags": (Tags, []), "reusable": (Bool, False), "ephemeral": (Bool, False), "expiration": (Timestamp, ...), "reveal_once": (Bool, ...)}),
        "expire": ("ExpirePreAuthKey", {"key_id": (Id, ...)}),
        "delete": ("DeletePreAuthKey", {"key_id": (Id, ...)})}),
    "headscale_auth": (ToolCategory.ENROLLMENT, "Approve/reject a pending auth verdict, used by SSH check. This never registers a device or assigns its user; auth IDs come from the client flow.", {
        "approve": ("AuthApprove", {"auth_id": (AuthId, ...)}),
        "reject": ("AuthReject", {"auth_id": (AuthId, ...)})}),
    "headscale_api_keys": (ToolCategory.CREDENTIALS, "Manage instance API credentials. New secrets are returned only on explicit creation; use prefix for REST delete.", {
        "list": ("ListApiKeys", {}),
        "create": ("CreateApiKey", {"expiration": (Timestamp, ...), "reveal_once": (Bool, ...)}),
        "expire": ("ExpireApiKey", {"key_id": (Id, None), "prefix": (Prefix, None)}),
        "delete": ("DeleteApiKey", {"prefix": (Prefix, ...)})}),
    "headscale_policy": (ToolCategory.POLICY, "Read, validate, diff or apply a whole policy. Get returns exists=false with an absence digest when database policy is not set; use that digest for first apply. Apply validates and compares the digest before writing, then independently reads back. Failed verification or a differing readback is an error; upstream has no atomic CAS and other writers can still race.", {
        "get": ("GetPolicy", {}),
        "check": ("CheckPolicy", {"policy": (PolicyText, ...)}),
        "diff": (None, {"policy": (PolicyText, ...)}),
        "apply": ("SetPolicy", {"policy": (PolicyText, ...), "expected_digest": (Digest, ...)})}),
}
_FIXED = {
    "headscale_routes_replace": (ToolCategory.ROUTES, "Replace the ENTIRE approved route set; [] revokes all. Exit IPv4/IPv6 defaults are paired. This does not advertise routes on clients.", "replace", "SetApprovedRoutes", {"node_id": (Id, ...), "routes": (Routes, ...)}),
    "headscale_health": (ToolCategory.MAINTENANCE, "Check Headscale database connectivity through the authenticated v1 API.", "read", "Health", {}),
    "headscale_debug_node": (ToolCategory.MAINTENANCE, "Explicit test maintenance: seed a synthetic registration. This is not real client enrollment evidence.", "create", "DebugCreateNode", {"user": (Username, ...), "auth_id": (AuthId, ...), "name": (NodeName, ...), "routes": (Routes, []), "confirm": (Bool, ...)}),
    "headscale_backfill_ips": (ToolCategory.MAINTENANCE, "Explicit maintenance: backfill node IPs. This is a write, not a dry run.", "update", "BackfillNodeIPs", {"confirm": (Bool, ...)})
}

MODELS = {}
TASK_NATIVE = {}
_registry = []
OPERATION_POLICIES = {}
for name, (category, description, operations) in _GROUPS.items():
    _registry.append(ToolSpec(name, category, description, tuple(operations), True, "write"))
    OPERATION_POLICIES[name] = OperationSpec(tuple(operations), parameter="operation", includes=
        {"apply": ("get", "check"), "diff": ("get",)} if name == "headscale_policy" else {})
    models = []
    for operation, (native, fields) in operations.items():
        model = create_model(name + "_" + operation, __base__=Arguments,
                             operation=(Literal[operation], ...), **fields)
        models.append(model)
        TASK_NATIVE[(name, operation)] = native
    MODELS[name] = TypeAdapter(Annotated[Union[tuple(models)], Field(discriminator="operation")])
for name, (category, description, operation, native, fields) in _FIXED.items():
    read = NATIVE_OPERATIONS[native].read_only
    _registry.append(ToolSpec(name, category, description, (operation,), not read, "read" if read else "write"))
    OPERATION_POLICIES[name] = OperationSpec((operation,), default=operation)
    MODELS[name] = TypeAdapter(create_model(name + "Arguments", __base__=Arguments, **fields))
    TASK_NATIVE[(name, operation)] = native

TOOL_REGISTRY = tuple(_registry)
TOOLS_BY_NAME = {spec.name: spec for spec in TOOL_REGISTRY}
COVERAGE_MATRIX = tuple({"operation_id": native, "method": NATIVE_OPERATIONS[native].method,
                         "path": NATIVE_OPERATIONS[native].path, "tool": name, "operation": operation}
                        for (name, operation), native in TASK_NATIVE.items() if native)
assert len(COVERAGE_MATRIX) == 29 and {r["operation_id"] for r in COVERAGE_MATRIX} == set(NATIVE_OPERATIONS)
OPERATION_NOTES = {name: spec.description for name, spec in TOOLS_BY_NAME.items()}


def get_all_tool_names():
    return list(TOOLS_BY_NAME)


def get_tool_by_name(name):
    return TOOLS_BY_NAME.get(name)


def get_operation_policies():
    return OPERATION_POLICIES


def input_schema(name):
    try:
        return {"type": "object", **MODELS[name].json_schema()}
    except KeyError:
        raise HeadscaleError("HEADSCALE_UNKNOWN_TOOL") from None


def normalize_arguments(name, arguments):
    try:
        if not isinstance(arguments, dict):
            raise ValueError()
        args = MODELS[name].validate_python(arguments).model_dump(exclude_none=True)
        operation = args.get("operation", OPERATION_POLICIES[name].default)
        if name == "headscale_users" and operation == "list" and sum(k in args for k in ("user_id", "name", "email")) > 1:
            raise ValueError()
        if name == "headscale_nodes" and operation == "expire" and ((args["mode"] == "at") != ("expiry" in args)):
            raise ValueError()
        if name == "headscale_preauth_keys" and operation == "create" and not (args.get("user_id") or args["tags"]):
            raise ValueError()
        if name == "headscale_api_keys" and operation == "expire" and sum(k in args for k in ("key_id", "prefix")) != 1:
            raise ValueError()
        if "reveal_once" in args and args["reveal_once"] is not True:
            raise ValueError()
        if "confirm" in args and args["confirm"] is not True:
            raise ValueError()
        if "policy" in args and len(args["policy"].encode("utf-8")) > 262144:
            raise ValueError()
        return args
    except (KeyError, TypeError, ValueError, ValidationError):
        raise HeadscaleError("HEADSCALE_INVALID_ARGUMENTS") from None

