"""Public n8n policy must inspect the same effective arguments as its child."""
import asyncio
import json

import httpx
import pytest

from mcp_admin_core import operation_policy, proxy
from mcp_admin_core.request_limits import McpRequestLimits
from n8n_mcp_admin import tool_registry

# Blocker 3: these tests own their upstream; stand in for the OS listener check (conftest.py).
pytestmark = pytest.mark.usefixtures("synthetic_child")

MIXED = [
    ("n8n_manage_credentials", "action", "list", "delete", {"id": "synthetic"}),
    ("n8n_manage_datatable", "action", "listTables", "deleteTable", {"tableId": "synthetic"}),
    ("n8n_manage_folders", "action", "list", "delete", {"folderId": "synthetic"}),
    ("n8n_executions", "action", "list", "delete", {"id": "synthetic"}),
    ("n8n_evaluations", "action", "list_runs", "run", {"workflowId": "synthetic"}),
    ("n8n_workflow_versions", "mode", "list", "rollback", {"workflowId": "synthetic"}),
    ("n8n_manage_agents", "action", "reference", "delete", {"agentId": "synthetic"}),
]


def call(name, arguments, id=71):
    return {"jsonrpc": "2.0", "id": id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}


def client(make_app, tools):
    c = make_app({"mcp_auth_token": "secret", "tools": tools})
    c.app.state.tool_registry = tool_registry
    c.app.state.mcp_request_limits = McpRequestLimits(4 * 1024 * 1024, 10_000)
    return c


def test_wrapped_credential_delete_never_constructs_upstream_request(make_app, monkeypatch):
    c = client(make_app, {"disabled_operations": {"n8n_manage_credentials": ["delete"]}})
    def forbidden(*args, **kwargs):
        raise AssertionError("denied wrapped write reached upstream transport")
    monkeypatch.setattr(proxy, "_get_client", forbidden)
    response = c.post("/private_secret/mcp", json=call("n8n_manage_credentials", {
        "action": "list", "output": json.dumps({"action": "delete", "id": "synthetic"})}))
    assert response.status_code == 200
    assert response.json()["id"] == 71
    assert response.json()["result"]["isError"] is True


@pytest.fixture
def capture(monkeypatch):
    calls = []
    def handler(request):
        value = json.loads(request.content) if request.content else None
        calls.append(value)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 71, "result": {"ok": True}})
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    monkeypatch.setattr(proxy, "_get_client", lambda *args: upstream)
    yield calls
    asyncio.run(upstream.aclose())


@pytest.mark.parametrize("name,key,read,write,extra", MIXED)
@pytest.mark.parametrize("path", ["", "/mcp", "/other-public-path"])
def test_all_mixed_wrapped_denies_ignore_stale_child_env(make_app, monkeypatch, name, key, read, write, extra, path):
    c = client(make_app, {"disabled_operations": {name: [write.lower()]}})
    monkeypatch.setattr(proxy, "_get_client", lambda *args: pytest.fail("denied call reached transport"))
    r = c.post("/private_secret" + path, json=call(name, {key: read, "output": json.dumps({key: write, **extra})}))
    assert r.status_code == 200 and r.json()["result"]["isError"] is True and r.json()["id"] == 71


@pytest.mark.parametrize("path", ["/mcp", "/messages/?session_id=synthetic"])
def test_mixed_batch_preserves_ids_and_canonical_allowed_member(make_app, capture, path):
    c = client(make_app, {"disabled_operations": {"n8n_manage_credentials": ["delete"]}})
    denied = call("n8n_manage_credentials", {"action": "list", "output": '{"action":"delete","id":"synthetic"}'}, "denied")
    allowed = call("search_nodes", {"query": "outer", "output": '{"query":"inner"}'}, "read")
    note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    response = c.post("/private_secret" + path, json=[denied, allowed, note])
    assert response.status_code == 200 and len(capture) == 1
    result = capture[0]
    assert result[0]["id"] == "denied" and result[0]["params"]["name"] == ""
    assert result[1] == call("search_nodes", {"query": "inner"}, "read")
    assert result[2] == note


def test_legacy_single_denial_cannot_dispatch_original_tool(make_app, capture):
    c = client(make_app, {"disabled_operations": {"n8n_manage_credentials": ["delete"]}})
    r = c.post("/private_secret/messages/?session_id=synthetic", json=call("n8n_manage_credentials", {
        "action": "list", "output": '{"action":"delete","id":"synthetic"}'}))
    assert r.status_code == 200 and len(capture) == 1
    assert capture[0]["params"]["name"] == "" and capture[0]["id"] == 71


@pytest.mark.parametrize("name,key,read", [("n8n_workflow_versions", "mode", "list"), ("n8n_test_workflow", "method", "prepare")])
@pytest.mark.parametrize("flag", [True, " true ", "\ufefftrue"])
@pytest.mark.parametrize("restriction", ["updater", "expose", "retained-updater-deny"])
def test_exposure_dependency_enforced_from_current_saved_policy(make_app, monkeypatch, name, key, read, flag, restriction):
    tools = ({"disabled": ["n8n_update_partial_workflow"]} if restriction == "updater" else
             {"disabled_operations": {name: ["expose"]}} if restriction == "expose" else
             {"disabled_operations": {"n8n_update_partial_workflow": ["retired-restriction"]}})
    c = client(make_app, tools)
    monkeypatch.setattr(proxy, "_get_client", lambda *args: pytest.fail("exposure reached upstream transport"))
    r = c.post("/private_secret/mcp", json=call(name, {key: read, "workflowId": "synthetic", "exposeToMcp": flag}))
    assert r.json()["result"]["isError"] is True


@pytest.mark.parametrize("flag", [False, " false ", "omitted"])
def test_updater_disabled_keeps_non_exposing_reads(make_app, capture, flag):
    c = client(make_app, {"disabled": ["n8n_update_partial_workflow"]})
    args = {"mode": "list", "workflowId": "synthetic"}
    if flag != "omitted":
        args["exposeToMcp"] = flag
    assert c.post("/private_secret/mcp", json=call("n8n_workflow_versions", args)).status_code == 200
    assert len(capture) == 1
    assert capture[0]["params"]["arguments"].get("exposeToMcp", False) is False


def test_all_on_keeps_explicit_exposure_and_original_primary_write(make_app, capture):
    c = client(make_app, {})
    desired = {"mode": "rollback", "workflowId": "synthetic", "exposeToMcp": True}
    r = c.post("/private_secret/mcp", json=call("n8n_workflow_versions", {"mode": "list", "output": json.dumps(desired)}))
    assert r.status_code == 200 and capture[0]["params"]["arguments"] == desired


def test_policy_saved_during_body_read_is_enforced(make_app, monkeypatch):
    c = client(make_app, {})
    store = proxy.get_config_store()
    read_body = McpRequestLimits.read_body
    async def changing_body(self, request):
        raw = await read_body(self, request)
        await store.update({"tools": {"disabled_operations": {"n8n_manage_credentials": ["delete"]}}})
        return raw
    monkeypatch.setattr(McpRequestLimits, "read_body", changing_body)
    monkeypatch.setattr(proxy, "_get_client", lambda *args: pytest.fail("fresh denial ignored"))
    r = c.post("/private_secret/mcp", json=call("n8n_manage_credentials", {"action": "list", "output": '{"action":"delete","id":"synthetic"}'}))
    assert r.json()["result"]["isError"] is True


def test_policy_save_before_leased_send_is_rechecked(make_app, monkeypatch):
    c = client(make_app, {})
    store = proxy.get_config_store()
    saved = []
    class Forbidden:
        def build_request(self, **kwargs):
            pytest.fail("fresh policy was not checked before building upstream request")
    def get_client(*args):
        async def save():
            await store.update({"tools": {"disabled_operations": {"n8n_manage_credentials": ["delete"]}}})
            saved.append(True)
        asyncio.create_task(save())
        return Forbidden()
    monkeypatch.setattr(proxy, "_get_client", get_client)
    r = c.post("/private_secret/mcp", json=call("n8n_manage_credentials", {"action": "list", "output": '{"action":"delete","id":"synthetic"}'}))
    assert saved == [True] and r.json()["result"]["isError"] is True


def test_invalid_token_rejected_before_normalizer(make_app, monkeypatch):
    c = client(make_app, {})
    monkeypatch.setattr(tool_registry, "normalize_arguments", lambda *args: pytest.fail("normalizer ran before auth"))
    assert c.post("/private_wrong/mcp", content=b"invalid").status_code == 403


def test_other_products_do_not_get_n8n_wrapper_semantics(make_app, capture):
    c = make_app({"mcp_auth_token": "secret"})
    original = call("other_tool", {"output": '{"action":"delete"}'})
    assert c.post("/private_secret/mcp", json=original).status_code == 200
    assert capture == [original]


@pytest.mark.parametrize("name,key,outer,inner,denied", [
    ("n8n_manage_credentials", "action", "list", '"action":"delete","id":"synthetic"', "delete"),
    ("n8n_workflow_versions", "mode", "list", '"mode":"list","workflowId":"synthetic","exposeToMcp":true', "expose"),
    ("n8n_test_workflow", "method", "prepare", '"method":"prepare","workflowId":"synthetic","exposeToMcp":true', "expose"),
])
def test_python_integer_limit_cannot_hide_child_wrapped_write(make_app, monkeypatch, name, key, outer, inner, denied):
    c = client(make_app, {"disabled_operations": {name: [denied]}})
    monkeypatch.setattr(proxy, "_get_client", lambda *args: pytest.fail("giant-number hidden write reached transport"))
    encoded = '{' + inner + ',"unrecognized":' + '9' * 5000 + '}'
    r = c.post("/private_secret/mcp", json=call(name, {key: outer, "exposeToMcp": False, "output": encoded}))
    assert r.status_code == 200 and r.json()["result"]["isError"] is True


def test_existing_encoded_depth_limit_runs_before_normalizer(make_app, monkeypatch):
    c = client(make_app, {})
    monkeypatch.setattr(tool_registry, "normalize_arguments", lambda *args: pytest.fail("normalizer ran before depth bound"))
    encoded = '{"action":"delete","unknown":' + '[' * 65 + '0' + ']' * 65 + '}'
    r = c.post("/private_secret/mcp", json=call("n8n_manage_credentials", {"action": "list", "output": encoded}))
    assert r.status_code == 413 and r.json()["id"] == 71


@pytest.mark.parametrize("path,kind", [
    ("/mcp", "allowed"), ("/messages/?session_id=synthetic", "allowed"),
    ("/mcp", "mixed"), ("/messages/?session_id=synthetic", "mixed"),
    ("/messages/?session_id=synthetic", "denied"),
])
@pytest.mark.parametrize("overflow", [0, 1])
def test_canonical_final_wire_bound_after_batch_and_legacy_rewrite(make_app, monkeypatch, path, kind, overflow):
    maximum = 4 * 1024 * 1024
    c = client(make_app, {"disabled_operations": {"n8n_manage_credentials": ["delete"]}})
    allowed = call("search_nodes", {"query": ""})
    denied = call("n8n_manage_credentials", {"action": "delete", "id": "synthetic", "unknown": ""})
    # Valid JSON 1e1 expands to 10.0 after parsing. This leaves the inbound
    # bytes below the limit even when compact final wire bytes exceed it.
    allowed["numbers"] = denied["numbers"] = [10.0] * 64
    rpc = [denied, allowed] if kind == "mixed" else denied if kind == "denied" else allowed
    # Expected final wire contract: only the denied name is replaced, IDs and
    # valid arguments remain. Compute padding against bytes, not code points.
    expected = json.loads(json.dumps(rpc))
    if kind == "mixed":
        expected[0]["params"]["name"] = ""
        filler = expected[1]["params"]["arguments"]
        original_filler = allowed["params"]["arguments"]
        key = "query"
    else:
        if kind == "denied":
            expected["params"]["name"] = ""
        filler = expected["params"]["arguments"]
        original_filler = rpc["params"]["arguments"]
        key = "unknown" if kind == "denied" else "query"
    space = maximum - len(json.dumps(expected, ensure_ascii=False, separators=(",", ":")).encode())
    padding = "é" * (space // 2) + "a" * (space % 2 + overflow)
    filler[key] = original_filler[key] = padding
    final_wire = json.dumps(expected, ensure_ascii=False, separators=(",", ":")).encode()
    original_wire = json.dumps(rpc, ensure_ascii=False, separators=(",", ":")).encode().replace(b"10.0", b"1e1")
    assert len(final_wire) == maximum + overflow
    assert len(original_wire) < maximum  # passes original auth/body-size gate
    sent = []
    def handler(request):
        sent.append(request.content)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 71, "result": {}})
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    def get_client(*args):
        assert not overflow, "oversize canonical bytes reached upstream transport"
        return upstream
    monkeypatch.setattr(proxy, "_get_client", get_client)
    try:
        response = c.post("/private_secret" + path, content=original_wire,
                          headers={"content-type": "application/json"})
        if overflow:
            assert response.status_code == 413 and sent == []
            assert response.json() == {"jsonrpc": "2.0", "id": None if kind == "mixed" else 71,
                "error": {"code": -32602, "message": f"MCP request exceeds {maximum} bytes"}}
        else:
            assert response.status_code == 200 and sent == [final_wire]
    finally:
        asyncio.run(upstream.aclose())


@pytest.mark.parametrize("wire,overflow", [(b"[]", False), (b"[ ]", True)])
def test_empty_batch_preserves_original_wire_and_bound(make_app, monkeypatch, capture, wire, overflow):
    c = client(make_app, {})
    c.app.state.mcp_request_limits = McpRequestLimits(2, 10_000)
    monkeypatch.setattr(tool_registry, "normalize_arguments", lambda *args: pytest.fail("empty batch has no tool to normalize"))
    if overflow:
        monkeypatch.setattr(proxy, "_get_client", lambda *args: pytest.fail("oversize empty batch reached transport"))
    response = c.post("/private_secret/mcp", content=wire, headers={"content-type": "application/json"})
    assert response.status_code == (413 if overflow else 200)
    assert capture == ([] if overflow else [[]])


def test_n8n_mixed_rewrite_preserves_escaped_unpaired_surrogate(make_app, capture):
    c = client(make_app, {"disabled_operations": {"n8n_manage_credentials": ["delete"]}})
    rpc = [call("n8n_manage_credentials", {"action": "delete", "unknown": "\ud800"}),
           call("search_nodes", {"query": "\udfff"}, 72)]
    response = c.post("/private_secret/messages/?session_id=synthetic", content=json.dumps(rpc).encode(),
                      headers={"content-type": "application/json"})
    assert response.status_code == 200 and len(capture) == 1
    assert capture[0][0]["params"]["name"] == ""
    assert capture[0][0]["params"]["arguments"]["unknown"] == "\ud800"
    assert capture[0][1] == rpc[1]


@pytest.mark.parametrize("path", ["/mcp", "/messages/?session_id=synthetic"])
def test_large_legitimate_unicode_is_not_rejected_by_ascii_expansion(make_app, monkeypatch, path):
    c = client(make_app, {})
    rpc = call("search_nodes", {"query": "é" * (1024 * 1024)})
    raw = json.dumps(rpc, ensure_ascii=False, separators=(",", ":")).encode()
    assert 2 * 1024 * 1024 < len(raw) < 4 * 1024 * 1024
    assert len(json.dumps(rpc, ensure_ascii=True).encode()) > 6 * 1024 * 1024
    sent = []
    def handler(request):
        sent.append(request.content)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 71, "result": {}})
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    monkeypatch.setattr(proxy, "_get_client", lambda *args: upstream)
    try:
        response = c.post("/private_secret" + path, content=raw, headers={"content-type": "application/json"})
        assert response.status_code == 200 and sent == [raw]
    finally:
        asyncio.run(upstream.aclose())


def test_canonical_serializer_roundtrips_unicode_surrogates_and_default_rewrite_bytes():
    value = {"ordinary": "é😀", "high": "\ud800", "low": "\udfff", "literal": r"\ud800"}
    wire = operation_policy.canonical_call_bytes(value)
    assert json.loads(wire) == value
    assert "é😀".encode() in wire and b'"high":"\\ud800"' in wire and b'"low":"\\udfff"' in wire
    message = {"method": "tools/call", "params": {"name": "blocked", "arguments": {"query": "é"}}}
    assert operation_policy.rewrite_blocked_calls(message, [message]) == (
        '{"method": "tools/call", "params": {"name": "", "arguments": {"query": "é"}}}'.encode())
