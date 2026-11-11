"""Reviewed selectors: actual product contracts and fail-closed behavior."""
import ast
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_admin_core.config.store import ConfigUnavailable
from mcp_admin_core.operation_policy import (
    disabled_operation_calls, operation_error, operation_status, policies_for,
    read_policy, rewrite_blocked_calls, validate_operation_update,
)

# apps/odoo-manage 已封存且移出 uv workspace（#78：它的 mcp-server-odoo 0.7.1 要求 mcp<2），不再測試。
PRODUCTS = ["hermes", "n8n", "nextcloud", "litellm", "odoo", "k8s", "vk", "opendesign"]


def registry(product):
    return importlib.import_module(f"{product}_mcp_admin.tool_registry")


def call(name, arguments=None, id=1):
    return {"jsonrpc": "2.0", "id": id, "method": "tools/call", "params": {"name": name, "arguments": arguments if arguments is not None else {}}}


def blocked(product, name, denied, arguments):
    message = call(name, arguments)
    return bool(disabled_operation_calls(message, {"disabled_operations": {name: denied}}, registry(product)))


@pytest.mark.parametrize("product,name,denied,allowed,rejected", [
    ("hermes", "hermes_session", ["delete"], {}, {"action": "delete"}),
    ("hermes", "hermes_config", ["set"], {}, {"action": "set"}),
    ("hermes", "hermes_gateway", ["restart"], {}, {"action": "restart"}),
    ("hermes", "hermes_cron", ["delete", "trigger"], {}, {"action": "trigger"}),
    ("hermes", "hermes_webhook", ["enable_platform"], {}, {"action": "enable_platform"}),
    ("hermes", "hermes_model", ["set"], {"action": "list_providers"}, {"action": "set"}),
    ("hermes", "hermes_tools", ["disable"], {}, {"action": "disable"}),
    ("hermes", "hermes_skill", ["disable"], {"action": "list"}, {"action": "disable"}),
    ("hermes", "hermes_mcp", ["remove"], {"action": "list"}, {"action": "remove"}),
    ("hermes", "hermes_inspect", ["config"], {"target": "status"}, {}),
    ("n8n", "n8n_executions", ["delete"], {}, {"action": "DELETE"}),
    ("n8n", "n8n_manage_credentials", ["create", "update", "delete"], {"action": "getSchema"}, {"action": "delete"}),
    ("n8n", "n8n_manage_datatable", ["deleterows"], {"action": "getRows"}, {"action": "deleteRows"}),
    ("n8n", "n8n_test_workflow", ["auto"], {"method": "prepare"}, {}),
    ("n8n", "n8n_workflow_versions", ["rollback"], {}, {"mode": "rollback"}),
    ("n8n", "n8n_manage_agents", ["publish"], {"action": "reference"}, {"action": "publish"}),
    ("nextcloud", "upload_file", ["update"], {}, {"expected_etag": '"etag"'}),
    ("nextcloud", "upload_file", ["create"], {"expected_etag": '"etag"'}, {"expected_etag": None}),
    ("odoo", "execute_approved_write", ["unlink"], {"approval": {"operation": "write"}}, {"approval": {"operation": " UNLINK "}}),
    ("odoo", "preview_write", ["unlink"], {"operation": "create"}, {"operation": " UNLINK "}),
    ("odoo", "submit_async_task", ["index_knowledge"], {"operation": "data_quality_report"}, {"operation": "index_knowledge"}),
])
def test_restrict_exact_operation_while_preserving_allowed_reads(product, name, denied, allowed, rejected):
    assert not blocked(product, name, denied, allowed)
    assert blocked(product, name, denied, rejected)


@pytest.mark.parametrize("product,name,operation", [("litellm", "litellm_delete_key", "delete"), ("nextcloud", "read_text_file", "read"), ("hermes", "hermes_chat", "chat")])
def test_reviewed_fixed_operations_cannot_be_overridden_by_fake_selectors(product, name, operation):
    assert blocked(product, name, [operation], {"operation": "read", "action": "list"})
    assert not blocked(product, name, [], {})


@pytest.mark.parametrize("value", [None, "", "  "])
@pytest.mark.parametrize("name,param,default", [("n8n_executions", "action", "list"), ("n8n_test_workflow", "method", "auto"), ("n8n_workflow_versions", "mode", "list")])
def test_n8n_empty_aware_defaults(name, param, default, value):
    assert blocked("n8n", name, [default], {param: value})
    assert not blocked("n8n", name, ["delete" if default == "list" else "direct"], {param: value})


@pytest.mark.parametrize("name,param,read", [("n8n_test_workflow", "method", "prepare"), ("n8n_workflow_versions", "mode", "list")])
def test_n8n_virtual_expose_is_checked_in_addition_to_read_selector(name, param, read):
    assert blocked("n8n", name, ["expose"], {param: read, "exposeToMcp": True})
    assert not blocked("n8n", name, ["expose"], {param: read, "exposeToMcp": False})
    assert not blocked("n8n", name, ["expose"], {param: read, "exposeToMcp": " false "})
    assert blocked("n8n", name, ["expose"], {param: read, "exposeToMcp": " true "})
    for value in [None, 1, "FALSE", []]:
        assert blocked("n8n", name, ["expose"], {param: read, "exposeToMcp": value})
    assert blocked("n8n", name, ["auto"], {param: "expose"})  # virtual label is not a selector


@pytest.mark.parametrize("product", PRODUCTS)
def test_all_reviewed_selectors_fail_closed_on_malformed_arguments(product):
    reg = registry(product)
    for name, spec in policies_for(reg).items():
        if not spec.parameter:
            continue
        policy = {"disabled_operations": {name: [spec.operations[-1]]}}
        for arguments in [None, [], 2, "get", {spec.parameter.split('.')[0]: {}}, {spec.parameter.split('.')[0]: 1}]:
            message = call(name)
            message["params"]["arguments"] = arguments
            assert disabled_operation_calls(message, policy, reg) == [message], (name, arguments)
        if not spec.optional_string:
            parts = spec.parameter.split('.')
            arguments = {parts[-1]: "not-an-operation"}
            for part in reversed(parts[:-1]):
                arguments = {part: arguments}
            assert blocked(product, name, [spec.operations[-1]], arguments)


def test_saved_legacy_labels_still_enforced():
    assert blocked("hermes", "hermes_inspect", ["read"], {"target": "status"})
    assert blocked("hermes", "hermes_model", ["list"], {"action": "list_providers"})
    assert not blocked("hermes", "hermes_model", ["list"], {})


@pytest.mark.parametrize("value", [None, [], {"disabled": [False]}, {"disabled_operations": []}, {"disabled_operations": {"hermes_session": "delete"}}, {"disabled_operations": {"x": [1]}}, {"disabled_operations": {"x": [""]}}])
def test_corrupt_config_never_becomes_no_policy(value):
    with pytest.raises(ConfigUnavailable):
        read_policy(value)
    with pytest.raises(ConfigUnavailable):
        disabled_operation_calls(call("hermes_session"), value, registry("hermes"))


def test_unreviewed_schema_enums_never_create_policy_but_saved_deny_blocks():
    reg = SimpleNamespace(TOOL_REGISTRY=[SimpleNamespace(name="dynamic", operations=["read", "write"], inputSchema={"properties": {"color": {"enum": ["read", "write"]}}})])
    assert policies_for(reg) == {}
    message = call("dynamic", {"color": "read"})
    assert disabled_operation_calls(message, {}, reg) == []
    assert disabled_operation_calls(message, {"disabled_operations": {"dynamic": ["write"]}}, reg) == [message]


def test_mixed_rewrite_is_pure_and_preserves_ids_and_unrelated_members():
    messages = [call("hermes_session", {"action": "delete"}, "denied"), call("hermes_session", {}, "read"), {"jsonrpc": "2.0", "method": "notifications/initialized"}, 42]
    before = json.dumps(messages)
    denied = disabled_operation_calls(messages, {"disabled_operations": {"hermes_session": ["delete"]}}, registry("hermes"))
    assert denied[0] is messages[0]
    result = json.loads(rewrite_blocked_calls(messages, denied))
    assert result[0]["params"]["name"] == ""
    assert result[0]["id"] == "denied" and result[1:] == messages[1:]
    assert json.dumps(messages) == before
    assert operation_error(messages[0])["id"] == "denied"
    assert "arguments" not in json.dumps(operation_error(messages[0]))


@pytest.mark.parametrize("product", PRODUCTS)
def test_empty_name_is_never_registered_by_any_product(product):
    assert all(tool.name for tool in registry(product).TOOL_REGISTRY)


def test_update_preserves_absent_metadata_and_validates_supported_operations():
    current = {"disabled": ["whole"], "disabled_operations": {"retired": ["destroy"], "hermes_model": ["retired_operation", "set"]}}
    updated = validate_operation_update({"hermes_model": []}, current, registry("hermes"))
    assert updated == {"retired": ["destroy"], "hermes_model": ["retired_operation"]}
    for value in [{"unknown": ["get"]}, {"hermes_model": ["unknown"]}, {"hermes_session": "delete"}, {"hermes_session": [True]}, []]:
        with pytest.raises(ValueError):
            validate_operation_update(value, current, registry("hermes"))
    assert current["disabled_operations"]["hermes_model"] == ["retired_operation", "set"]


def test_all_operations_disabled_includes_virtual_operations_correctly():
    spec = policies_for(registry("n8n"))["n8n_test_workflow"]
    state = operation_status(spec, ["auto", "trigger", "prepare", "pinned", "direct"], True)
    assert state["policy_enabled"] is False  # expose alone cannot execute a primary operation


def test_hermes_reviewed_defaults_match_real_function_signatures():
    from hermes_mcp_server import server
    tree = ast.parse(Path(server.__file__).read_text())
    functions = {node.name: node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)}
    for name, spec in policies_for(registry("hermes")).items():
        function = functions[name]
        if spec.parameter:
            defaults = dict(zip([arg.arg for arg in function.args.args][-len(function.args.defaults):], function.args.defaults))
            assert spec.parameter in [arg.arg for arg in function.args.args]
            assert (ast.literal_eval(defaults[spec.parameter]) if spec.parameter in defaults else None) == spec.default
    # Correct old fabricated read/list labels using real dispatch branches.
    assert "list_providers" in policies_for(registry("hermes"))["hermes_model"].operations
    assert "read" not in policies_for(registry("hermes"))["hermes_inspect"].operations


def test_reviewed_legacy_alias_can_be_replaced_or_explicitly_cleared():
    current = {"disabled_operations": {"hermes_model": ["list"], "hermes_inspect": ["read"]}}
    assert validate_operation_update({"hermes_model": ["list_providers"]}, current, registry("hermes")) == {"hermes_model": ["list_providers"]}
    assert validate_operation_update({}, current, registry("hermes")) == {}


def test_n8n_saved_labels_match_native_trim_and_case_normalization():
    assert blocked("n8n", "n8n_executions", [" DELETE "], {"action": "delete"})
    current = {"disabled_operations": {"n8n_executions": [" DELETE "]}}
    assert validate_operation_update({}, current, registry("n8n")) == {}
