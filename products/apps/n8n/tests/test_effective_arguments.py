"""Real pinned preprocessing/native guard compared with the public canonical form."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from n8n_mcp_admin import effective_arguments as effective
from n8n_mcp_admin import upstream_policy_patch, upstream_result_patch

FIXTURES = Path(__file__).parent / "fixtures"
MANIFEST = json.loads((FIXTURES / "effective-policy-manifest.json").read_text())


@pytest.fixture
def package(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"version": "2.96.2"}))
    for name, meta in MANIFEST["files"].items():
        raw = gzip.decompress((FIXTURES / meta["fixture"]).read_bytes())
        assert hashlib.sha256(raw).hexdigest() == meta["sha256"]
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    upstream_result_patch.patch_package(tmp_path)
    return tmp_path


def child(package, payload, denied=""):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node required for actual pinned JS preprocessing")
    result = subprocess.run([node, str(FIXTURES / "effective-policy-probe.cjs"), str(package)],
                            input=json.dumps(payload), text=True, capture_output=True, timeout=10,
                            env={"PATH": os.environ.get("PATH", ""), "MCP_MODE": "http",
                                 "DISABLED_TOOL_OPERATIONS": denied})
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_all_28_schema_projections_equal_actual_pinned_definitions(package):
    assert child(package, {"schemas": True}) == effective.SCHEMAS
    assert len(effective.SCHEMAS) == 28


@pytest.mark.parametrize("name,args", [
    ("n8n_manage_credentials", {"action": "list", "output": '{"action":"delete","id":"synthetic"}'}),
    ("n8n_manage_datatable", {"action": "listTables", "output": '{"action":"deleteTable","tableId":"synthetic"}'}),
    ("n8n_workflow_versions", {"mode": "list", "output": '{"mode":"rollback","workflowId":"synthetic"}'}),
    ("n8n_workflow_versions", {"mode": "list", "exposeToMcp": " \ufefftrue\n"}),
    ("n8n_test_workflow", {"workflowId": "synthetic", "method": "prepare", "exposeToMcp": " false "}),
    ("n8n_test_workflow", {"workflowId": "outer", "output": '{"method":"direct"}'}),  # missing required inner workflowId
    ("n8n_workflow_versions", {"exposeToMcp": False, "output": '{"mode":"list","exposeToMcp":"true"}'}),  # extract rejects type before coercion
    ("search_nodes", {"query": "outer", "output": '{"query":"inner"}'}),
    ("tools_documentation", {"output": '{"topic":"overview"}'}),
])
def test_canonical_policy_fields_match_actual_child_and_repeated_checks(package, name, args):
    normalized = effective.normalize_arguments(name, args)
    assert effective.normalize_arguments(name, normalized) == normalized
    original = child(package, {"name": name, "arguments": args})["calls"]
    forwarded = child(package, {"name": name, "arguments": normalized})["calls"]
    assert original == forwarded and len(forwarded) == 1
    assert all(forwarded[0]["args"].get(k) == normalized.get(k)
               for k in ("action", "mode", "method", "exposeToMcp"))


def test_second_recognized_wrapper_rejected_before_any_forwarding():
    nested = {"mode": "list", "output": json.dumps({"mode": "rollback", "workflowId": "synthetic"})}
    with pytest.raises(ValueError, match="Ambiguous"):
        effective.normalize_arguments("n8n_workflow_versions", {"output": json.dumps(nested)})


@pytest.mark.parametrize("name,outer,inner,key,expected", [
    ("n8n_manage_credentials", {"action": "list"}, '"action":"delete","id":"synthetic"', "action", "delete"),
    ("n8n_workflow_versions", {"mode": "list", "exposeToMcp": False}, '"mode":"list","exposeToMcp":true', "exposeToMcp", True),
    ("n8n_test_workflow", {"workflowId": "synthetic", "method": "prepare", "exposeToMcp": False}, '"workflowId":"synthetic","method":"prepare","exposeToMcp":true', "exposeToMcp", True),
])
def test_python_parser_limit_refuses_a_wrapper_the_actual_child_accepts(package, name, outer, inner, key, expected):
    args = {**outer, "output": '{' + inner + ',"unknown":' + '9' * 5000 + '}'}
    # Applied child env may be stale/empty while public saved denies are newer.
    # JSON.parse really accepts the number and selects the hidden operation.
    raw = child(package, {"name": name, "arguments": args}, denied="")
    assert len(raw["calls"]) == 1 and raw["calls"][0]["args"][key] == expected
    with pytest.raises(ValueError, match="Unverifiable"):
        effective.normalize_arguments(name, args)


@pytest.mark.parametrize("invalid", ["NaN", "Infinity", "-Infinity", "not-json"])
def test_json_syntax_errors_keep_the_same_outer_fallback_as_child(package, invalid):
    args = {"action": "list", "output": '{"action":"delete","id":"synthetic","unknown":' + invalid + '}'}
    assert effective.normalize_arguments("n8n_manage_credentials", args) == args
    assert child(package, {"name": "n8n_manage_credentials", "arguments": args})["calls"][0]["args"]["action"] == "list"


def test_python_recursion_limit_does_not_fall_back_to_outer_read():
    args = {"mode": "list", "output": '{"mode":"rollback","unknown":' + '[' * 10000 + '0' + ']' * 10000 + '}'}
    with pytest.raises(ValueError, match="Unverifiable"):
        effective.normalize_arguments("n8n_workflow_versions", args)


@pytest.mark.parametrize("value", [None, 1, [], {}, "TRUE", "maybe", "\u001ctrue"])
def test_malformed_exposure_is_not_silently_converted_to_false(value):
    with pytest.raises(ValueError, match="exposure"):
        effective.normalize_arguments("n8n_workflow_versions", {"exposeToMcp": value})


def test_native_patch_blocks_actual_wrapped_credentials_after_unwrap(package):
    payload = {"name": "n8n_manage_credentials", "arguments": {
        "action": "list", "output": '{"action":"delete","id":"synthetic"}'}}
    denied = "n8n_manage_credentials:delete"
    assert len(child(package, payload, denied)["calls"]) == 1  # original omission is observable
    before = (package / upstream_policy_patch.TOOLS).read_bytes()
    assert upstream_policy_patch.patch_package(package)
    after = (package / upstream_policy_patch.TOOLS).read_bytes()
    assert after.replace(upstream_policy_patch.AFTER, upstream_policy_patch.BEFORE, 1) == before
    result = child(package, payload, denied)
    assert result["calls"] == []
    assert json.loads(result["response"]["content"][0]["text"])["error"] == "OPERATION_DISABLED"
    assert upstream_policy_patch.patch_package(package) is False
    # Allowed reads still work after the same deny is applied.
    payload["arguments"] = {"action": "list"}
    assert len(child(package, payload, denied)["calls"]) == 1


@pytest.mark.parametrize("change", ["version", "bytes"])
def test_native_patch_rejects_unreviewed_source_before_write(package, change):
    if change == "version":
        (package / "package.json").write_text('{"version":"2.96.3"}')
    else:
        with (package / upstream_policy_patch.TOOLS).open("ab") as f:
            f.write(b"\n// unreviewed\n")
    before = (package / upstream_policy_patch.TOOLS).read_bytes()
    with pytest.raises(ValueError, match="Unreviewed"):
        upstream_policy_patch.patch_package(package)
    assert (package / upstream_policy_patch.TOOLS).read_bytes() == before
