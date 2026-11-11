"""Reviewed operation selectors shared by the proxy, catalog and admin API.

Registries opt in with OperationSpec; arbitrary JSON-schema enums are never
interpreted as authorization. No I/O or global product state lives here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .config.store import ConfigUnavailable
from .tool_filter import messages, normalize_disabled, refusal_result

_MISSING = object()
# Every supported product registers nonempty tool names. Unlike appending a
# suffix, this cannot collide with a product tool (including runtime additions).
BLOCKED_TOOL_NAME = ""


@dataclass(frozen=True)
class OperationSpec:
    operations: tuple[str, ...]
    parameter: str | None = None  # dotted paths are explicit, e.g. approval.operation
    default: str | None = None
    normalization: str = "exact"
    empty_uses_default: bool = False
    # Nextcloud: omitted/null etag creates, nonempty string replaces.
    optional_string: tuple[str, str] | None = None
    # Operations which also execute other operations (Hermes inspect/all).
    includes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # n8n virtual operation, independent of the primary selector.
    boolean_operations: dict[str, str] = field(default_factory=dict)
    # Existing UI labels that predate the reviewed selector metadata.
    legacy_denies: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def normalize(self, value: str) -> str:
        if self.normalization == "strip_lower":
            return value.strip().lower()
        if self.normalization == "lower":
            return value.lower()
        return value

    def normalize_saved(self, value: str) -> str:
        return self.normalize(value.strip() if self.empty_uses_default else value)

    def denied(self, values: list[str]) -> set[str]:
        result = {self.normalize_saved(value) for value in values}
        for value in tuple(result):
            result.update(self.legacy_denies.get(value, ()))
        return result

    def requirements(self, operation: str) -> set[str]:
        return {operation, *self.includes.get(operation, ())}

    def resolve(self, arguments: Any) -> set[str] | None:
        if not isinstance(arguments, dict):
            return None
        virtual = set(self.boolean_operations.values())
        if self.parameter is None:
            # Explicit fixed operations only; never guess from tool labels.
            selected = self.default
        else:
            value: Any = arguments
            for part in self.parameter.split("."):
                if not isinstance(value, dict):
                    return None
                value = value.get(part, _MISSING)
                if value is _MISSING:
                    break
            if self.optional_string:
                if value is _MISSING or value is None:
                    selected = self.optional_string[0]
                elif isinstance(value, str) and value.strip():
                    selected = self.optional_string[1]
                else:
                    return None
            elif value is _MISSING or (self.empty_uses_default and (
                    value is None or isinstance(value, str) and not value.strip())):
                selected = self.default
            elif isinstance(value, str):
                selected = self.normalize(value)
            else:
                return None
        if selected not in self.operations or selected in virtual:
            return None
        result = self.requirements(selected)
        for parameter, operation in self.boolean_operations.items():
            value = arguments.get(parameter, False)
            if type(value) is not bool:
                return None
            if value:
                result.add(operation)
        return result

    def metadata(self) -> dict:
        return {"operation_parameter": self.parameter, "operation_default": self.default,
                "operation_selector": "optional_string" if self.optional_string else "value" if self.parameter else "fixed",
                "operation_policy_supported": True,
                "operation_dependencies": {key: list(value) for key, value in self.includes.items()},
                "operation_flags": dict(self.boolean_operations),
                "operation_legacy_denies": {key: list(value) for key, value in self.legacy_denies.items()}}


def policies_for(registry) -> dict[str, OperationSpec]:
    if registry is None:
        return {}
    factory = getattr(registry, "get_operation_policies", None)
    return factory() if factory else getattr(registry, "OPERATION_POLICIES", {})


def operation_notes_for(registry) -> dict[str, str]:
    """產品覆核過、要在 Permissions 頁顯示的工具說明（registry 的 ``OPERATION_NOTES``，例如 Odoo 的三支審批工具：
    它們的 create／write／unlink 限制擋不住 execute_method）；沒有宣告就是空的。"""
    notes = getattr(registry, "OPERATION_NOTES", None) if registry is not None else None
    return {str(name): str(note) for name, note in (notes or {}).items() if note}


def conditional_writes_for(registry, name, disabled_tools, disabled_operations) -> list[dict]:
    """僅顯示產品覆核的條件描述，不由 schema 推測權限。"""
    describe = getattr(registry, "get_conditional_writes", None)
    return describe(name, disabled_tools, disabled_operations) if describe else []


def read_policy(tools_config: Any) -> tuple[frozenset[str], dict[str, list[str]]]:
    """Validate saved state; corrupt state must never become an empty policy."""
    if not isinstance(tools_config, dict):
        raise ConfigUnavailable("Tool policy unavailable")
    disabled = tools_config.get("disabled", [])
    if not isinstance(disabled, (str, list)) or isinstance(disabled, list) and any(
            not isinstance(name, str) or not name for name in disabled):
        raise ConfigUnavailable("Tool policy unavailable")
    operations = tools_config.get("disabled_operations", {})
    if not isinstance(operations, dict) or any(
            not isinstance(name, str) or not name or not isinstance(values, list)
            or any(not isinstance(value, str) or not value.strip() for value in values)
            for name, values in operations.items()):
        raise ConfigUnavailable("Operation policy unavailable")
    return normalize_disabled(disabled), {name: list(values) for name, values in operations.items()}


def disabled_operation_calls(data: Any, tools_config: Any, registry) -> list[dict]:
    """Return original blocked message objects, suitable for identity deduping.

    Saved restrictions on an unrecognized tool still block its calls. Under a
    restriction, unknown/malformed selectors fail closed; known allowed reads
    remain usable. An opt-in product normalizer can refuse ambiguous arguments
    even without saved restrictions; no policy is migrated or persisted here.
    """
    disabled_tools, disabled = read_policy(tools_config)
    policies = policies_for(registry)
    normalize = getattr(registry, "normalize_arguments", None)
    conditional_disabled = getattr(registry, "conditional_call_disabled", None)
    blocked = []
    for message in messages(data):
        if message.get("method") != "tools/call":
            continue
        params = message.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            # Invalid protocol params cannot select a valid tool upstream.
            continue
        name = params["name"]
        if normalize is not None:
            try:
                arguments = normalize(name, params.get("arguments", {}))
            except ValueError:
                blocked.append(message)
                continue
            # Authorization and forwarding share these exact wire arguments.
            # The opt-in normalizer must be idempotent for fresh-policy checks.
            params["arguments"] = arguments
            if conditional_disabled is not None and conditional_disabled(
                    name, arguments, disabled_tools, disabled):
                blocked.append(message)
                continue
        denied = disabled.get(name, [])
        if not denied:
            continue
        spec = policies.get(name)
        requested = spec.resolve(params.get("arguments", {})) if spec else None
        if requested is None or requested & spec.denied(denied):
            blocked.append(message)
    return blocked


def operation_error(message: dict) -> dict:
    """被停用（或在限制下無法辨識）的操作：isError 的工具結果，不回顯參數（見 tool_filter.refusal_result）。"""
    return refusal_result(message, "Operation blocked by administrator policy (操作已停用或無法辨識)")


def canonical_call_bytes(data: Any) -> bytes:
    """Compact UTF-8 for opt-in products; escape lone surrogates as valid JSON."""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "backslashreplace")


def rewrite_blocked_calls(data: Any, blocked: list[dict], *, canonical: bool = False) -> bytes:
    """Serialize a copy with denied call names replaced, retaining IDs/batch order.

    Used for legacy SSE and mixed batches whose results must come from the
    child response channel. Non-call messages and allowed calls are untouched.
    """
    denied_ids = {id(message) for message in blocked}

    def rewrite(message):
        if (not isinstance(message, dict) or id(message) not in denied_ids
                or message.get("method") != "tools/call"):
            return message
        return {**message, "params": {**message["params"], "name": BLOCKED_TOOL_NAME}}

    result = [rewrite(message) for message in data] if isinstance(data, list) else rewrite(data)
    return canonical_call_bytes(result) if canonical else json.dumps(result, ensure_ascii=False).encode()


def operation_status(spec: OperationSpec | None, saved: list[str], tool_enabled: bool) -> dict:
    """Expose supported metadata and retain denies whose source has disappeared."""
    if spec is None:
        return {"operations": [], "operation_policy_supported": False,
                "disabled_operations": saved, "policy_enabled": tool_enabled and not saved}
    denied = spec.denied(saved)
    operations = [{"name": operation, "write": None,
                   "policy_enabled": tool_enabled and not (spec.requirements(operation) & denied)}
                  for operation in spec.operations]
    return {**spec.metadata(), "operations": operations, "disabled_operations": saved,
            "policy_enabled": any(operation["policy_enabled"] for operation in operations
                                  if operation["name"] not in spec.boolean_operations.values())}


def editable_denies(saved: dict[str, list[str]], registry) -> dict[str, list[str]]:
    """Canonical known labels for editing; preserve unknown saved text verbatim."""
    policies = policies_for(registry)
    result = {}
    for name, values in saved.items():
        spec = policies.get(name)
        canonical = set()
        for value in values:
            label = spec.normalize_saved(value) if spec else value
            if spec and label in set(spec.operations) | set(spec.legacy_denies):
                canonical.update(spec.legacy_denies.get(label, (label,)))
            else:
                canonical.add(value)
        result[name] = sorted(canonical)
    return result


def validate_operation_update(value: Any, current_tools_config: Any, registry) -> dict[str, list[str]]:
    """Validate replacement denies, retaining saved entries lacking metadata.

    ValueError denotes invalid user input (HTTP 400). ConfigUnavailable denotes
    corrupt existing state (HTTP 503). A UI editing supported operations cannot
    silently remove a retired tool/operation's saved deny.
    """
    _, current = read_policy(current_tools_config)
    policies = policies_for(registry)
    if not isinstance(value, dict):
        raise ValueError("disabled_operations must be an object")
    result: dict[str, list[str]] = {}
    for name, operations in value.items():
        if (not isinstance(name, str) or not name or not isinstance(operations, list)
                or any(not isinstance(operation, str) for operation in operations)):
            raise ValueError("Operation policy must map tool names to lists of operation names")
        spec = policies.get(name)
        if spec is None:
            if name not in current or any(operation not in current[name] for operation in operations):
                raise ValueError(f"Tool '{name}' has no supported operation metadata")
            normalized = list(operations)
        else:
            # n8n's env parser trims saved labels and compares case-insensitively.
            normalize = spec.normalize_saved
            normalized = sorted({normalize(operation) for operation in operations if operation.strip()})
            valid = set(spec.operations) | set(spec.legacy_denies)
            saved = {normalize(operation) for operation in current.get(name, [])}
            invalid = set(normalized) - valid - saved
            if invalid:
                raise ValueError(f"Tool '{name}' does not have operations: {sorted(invalid)}")
        if normalized:
            result[name] = normalized
    for name, operations in current.items():
        spec = policies.get(name)
        # Only reviewed operation names can be removed through this editor.
        retained = operations if spec is None else [operation for operation in operations
            if spec.normalize_saved(operation) not in set(spec.operations) | set(spec.legacy_denies)]
        if retained:
            result[name] = sorted(set(result.get(name, [])) | set(retained))
    return result
