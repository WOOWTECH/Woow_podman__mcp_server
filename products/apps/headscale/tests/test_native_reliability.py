"""Pinned input contracts and injected faults, not real client enrollment.

Evidence: artifacts/headscale-mcp-comprehensive-20261110/coverage-audit.json;
official juanfont/headscale commit 8106636c7f8d0cf8c9d4fb0beae94415069b768b.
Reviewed source SHA256 (matched audit manifest, not inferred from examples):
hscontrol/grpcv1.go: 050562313e9bb6380811f12d16da54ad663a5c30e1a156234e4ab84741a6e93e
hscontrol/state/state.go: bb639ae35c30aa592b3576c94c85cafd981bb626ddd7b7e20ff7a644cc794951
hscontrol/util/dns.go: 7303e7bd4ef425f45323160d70ac8a2e3b06d13cd7b3ab8d80f1e6ddd999fda6
hscontrol/policy/v2/types.go: fb7299a6d62885879a77072d8fb9c8eccde832cc91d16ce18bf512c938a52c36

grpcv1.go:47/69 -> state.go:402/430/492 saves user names without calling
util.ValidateUsername or the db.RenameUser OIDC guard. Thus uppercase, Unicode,
spaces, plus signs, leading punctuation and single-character names must not be
rejected on the basis of that unused helper. Local controls/path-delimiter and
2048-character limits are deliberate MCP safety bounds, not upstream limits.
grpcv1.go:419 validates lowercase tag: and Fields; policy/v2/types.go:554 only
checks the first suffix letter. SetTags additionally checks policy existence
and disallows []; CreatePreAuthKey allows [] with a user, does not call the
policy Tag.Validate, and independently accepts reusable/ephemeral booleans.
"""
import asyncio
import json
from urllib.parse import quote

import httpx
import pytest
from jsonschema import Draft202012Validator

from test_native_headscale import (
    API_SECRET, AUTH, CASES, DIGEST, EXPIRY, LEGACY, P0, P1,
    client_for, policy, response_for,
)
from headscale_mcp_server.errors import HeadscaleError
from headscale_mcp_server.native import NATIVE_OPERATIONS
from headscale_mcp_server.registry import OPERATION_POLICIES, TOOL_REGISTRY, input_schema
from headscale_mcp_server.server import build_server, execute_tool, policy_digest


NAME_CALLS = [
    ("headscale_users", {"operation": "create"}, "name", "CreateUser"),
    ("headscale_users", {"operation": "rename", "user_id": "101"}, "new_name", "RenameUser"),
    ("headscale_users", {"operation": "list"}, "name", "ListUsers"),
    ("headscale_nodes", {"operation": "list"}, "user", "ListNodes"),
    ("headscale_register", {"operation": "register", "auth_id": AUTH}, "user", "AuthRegister"),
    ("headscale_register", {"operation": "legacy", "auth_id": AUTH}, "user", "RegisterNode"),
    ("headscale_debug_node", {"auth_id": AUTH, "name": "fixture", "confirm": True}, "user", "DebugCreateNode"),
]


@pytest.mark.parametrize("value", ["UpperCase", "A.b_C-9+X@example.com", "1", "a@@b", "_Name", ".Name", "a" * 255, "李雷", " Alice & Bob "])
@pytest.mark.parametrize("tool,args,field,native", NAME_CALLS)
async def test_names_preserve_identity_in_all_shared_call_sites(value, tool, args, field, native, policy):
    calls = []
    arguments = {**args, field: value}
    Draft202012Validator(input_schema(tool)).validate(arguments)
    def handler(request):
        calls.append(request)
        if native == "RenameUser":
            assert request.url.raw_path == ("/api/v1/user/101/rename/" + quote(value, safe="")).encode()
            return httpx.Response(200, json={"user": {"name": value, "provider": "oidc", "providerIdentifier": "fixture-subject"}})
        elif native in ("ListUsers", "ListNodes", "RegisterNode"):
            assert request.url.params["name" if native == "ListUsers" else "user"] == value
        else:
            assert json.loads(request.content)["name" if native == "CreateUser" else "user"] == value
        return httpx.Response(200, json=response_for(native))
    async with client_for(handler) as client:
        result = await execute_tool(tool, arguments, client, config_path=policy)
    assert len(calls) == 1
    if native == "RenameUser":
        assert result["user"] == {"name": value, "provider": "oidc", "providerIdentifier": "fixture-subject"}


@pytest.mark.parametrize("value", ["", ".", "..", "../user", "x/y", "x\\y", "x%2fy", "x?y", "x#y", "x\x00y", "x\ny", "x\x7fy", "x\x85y", "a" * 2049])
@pytest.mark.parametrize("tool,args,field,native", NAME_CALLS)
async def test_name_safety_limits_reject_without_io(value, tool, args, field, native, policy):
    calls = []
    async with client_for(lambda r: calls.append(r)) as client:
        with pytest.raises(HeadscaleError, match="INVALID_ARGUMENTS"):
            await execute_tool(tool, {**args, field: value}, client, config_path=policy)
    assert calls == []


@pytest.mark.parametrize("tags", [[], ["tag:prod.web"], ["tag:a_b+c@d", "tag:a" + "x" * 80], ["tag:a中文"], ["tag:1"], ["tag:"]])
@pytest.mark.parametrize("ownership", ["user", "tags", "both"])
@pytest.mark.parametrize("reusable,ephemeral", [(False, False), (True, False), (False, True), (True, True)])
async def test_preauth_flags_and_tag_combinations(tags, ownership, reusable, ephemeral, policy):
    args = {"operation": "create", "expiration": EXPIRY, "reveal_once": True,
            "reusable": reusable, "ephemeral": ephemeral}
    if ownership != "tags":
        args["user_id"] = "101"
    args["tags"] = tags if ownership != "user" else []
    Draft202012Validator(input_schema("headscale_preauth_keys")).validate(args)
    calls = []
    def handler(request):
        calls.append(request)
        body = json.loads(request.content)
        assert body["reusable"] is reusable and body["ephemeral"] is ephemeral
        assert body["aclTags"] == args["tags"]
        assert body.get("user") == args.get("user_id")
        return httpx.Response(200, json=response_for("CreatePreAuthKey"))
    async with client_for(handler) as client:
        if not args.get("user_id") and not args["tags"]:
            with pytest.raises(HeadscaleError, match="INVALID_ARGUMENTS"):
                await execute_tool("headscale_preauth_keys", args, client, config_path=policy)
            assert not calls
        else:
            await execute_tool("headscale_preauth_keys", args, client, config_path=policy)
            assert len(calls) == 1


@pytest.mark.parametrize("tags", [["tag:prod.web"], ["tag:a_b+c@d", "tag:a中文"]])
async def test_node_tags_preserved_and_policy_rejection_left_to_backend(tags, policy):
    def handler(request):
        assert json.loads(request.content) == {"tags": tags}
        return httpx.Response(400, json={"code": 3, "message": "tag not in policy", "details": []})
    async with client_for(handler) as client:
        with pytest.raises(HeadscaleError) as error:
            await execute_tool("headscale_nodes", {"operation": "set_tags", "node_id": "201", "tags": tags}, client, config_path=policy)
    assert error.value.code == "HEADSCALE_UPSTREAM_ERROR" and error.value.grpc_code == 3


@pytest.mark.parametrize("tag", ["prod.web", "Tag:prod", "tag:Prod", "tag:éÉ", "tag:a b", "tag:a\n", "tag:a\x00", "tag:a\x7f", "tag:a" + "x" * 2048])
@pytest.mark.parametrize("tool,args", [("headscale_nodes", {"operation": "set_tags", "node_id": "201"}),
                                      ("headscale_preauth_keys", {"operation": "create", "expiration": EXPIRY, "reveal_once": True})])
async def test_tag_safety_and_upstream_case_checks(tag, tool, args, policy):
    calls = []
    async with client_for(lambda r: calls.append(r)) as client:
        with pytest.raises(HeadscaleError, match="INVALID_ARGUMENTS"):
            await execute_tool(tool, {**args, "tags": [tag]}, client, config_path=policy)
    assert calls == []


async def test_policy_put_is_independently_read_back(policy):
    calls = []
    def handler(request):
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json={"policy": P1 if "PUT" in calls else P0, "updatedAt": "from-get"})
        return httpx.Response(200, json={"policy": P1, "updatedAt": "from-put"} if request.method == "PUT" else {})
    async with client_for(handler) as client:
        result = await execute_tool("headscale_policy", {"operation": "apply", "policy": P1, "expected_digest": DIGEST}, client, config_path=policy)
    assert calls == ["GET", "POST", "GET", "PUT", "GET"]
    assert result["readback_verified"] is True and result["atomic_cas"] is False
    assert result["updatedAt"] == "from-get" and result["digest"] == policy_digest(P1)


@pytest.mark.parametrize("failure", ["conflict", "timeout", "invalid", "absent", "revoked"])
async def test_postwrite_failure_is_explicit_and_never_retries_put(failure, policy):
    calls = []
    def handler(request):
        calls.append(request.method)
        if request.method == "PUT":
            if failure == "revoked":
                policy.write_text(json.dumps({"tools": {"disabled": ["headscale_policy"]}}))
            return httpx.Response(200, json={"policy": P1})
        if request.method == "GET" and "PUT" in calls:
            if failure == "timeout":
                raise httpx.ReadTimeout(API_SECRET, request=request)
            if failure == "invalid":
                return httpx.Response(200, json={"policy": None})
            if failure == "absent":
                from test_native_headscale import POLICY_ABSENT_ERROR
                return httpx.Response(500, json=POLICY_ABSENT_ERROR)
        return httpx.Response(200, json={"policy": P0} if request.method == "GET" else {})
    async with client_for(handler) as client:
        with pytest.raises(HeadscaleError) as error:
            await execute_tool("headscale_policy", {"operation": "apply", "policy": P1, "expected_digest": DIGEST}, client, config_path=policy)
    conflict = failure in ("conflict", "absent")
    assert error.value.code == ("HEADSCALE_POLICY_CONFLICT" if conflict else "HEADSCALE_POLICY_UNVERIFIED")
    public = error.value.as_dict()
    assert public["outcome"] == "unknown" and public["retryable"] is False
    assert public["policy_verification"]["status"] == ("conflict" if conflict else "unverified")
    assert public["policy_verification"]["atomic_cas"] is False
    assert public["policy_verification"]["write_acknowledged"] is True
    assert calls.count("PUT") == 1 and len(calls) == (4 if failure == "revoked" else 5)
    assert API_SECRET not in json.dumps(public)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c[0])
@pytest.mark.parametrize("status,grpc,category", [(401, 16, "authentication"), (403, 7, "permission"), (404, 5, "not_found"), (409, 6, "conflict"), (429, 8, "rate_limit"), (503, 14, "unavailable")])
async def test_all_native_http_failures_classified_without_retry(case, status, grpc, category):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"code": grpc, "message": API_SECRET + LEGACY, "details": []})
    async with client_for(handler) as client:
        with pytest.raises(HeadscaleError) as error:
            await client.request(case[0], **case[1])
    public = error.value.as_dict()
    assert public["category"] == category and public["http_status"] == status and public["grpc_code"] == grpc
    assert public["outcome"] == ("unknown" if status >= 500 and not NATIVE_OPERATIONS[case[0]].read_only else "rejected")
    assert public["retryable"] is False and len(calls) == 1
    assert API_SECRET not in str(error.value) + json.dumps(public) and LEGACY not in json.dumps(public)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c[0])
@pytest.mark.parametrize("fault,category,not_sent", [(httpx.ConnectTimeout, "timeout", True), (httpx.PoolTimeout, "timeout", True), (httpx.ConnectError, "connection", True), (httpx.ReadTimeout, "timeout", False), (httpx.WriteError, "transport", False), (httpx.RemoteProtocolError, "protocol", False)])
async def test_all_native_transport_faults_classified_without_retry(case, fault, category, not_sent):
    calls = []
    def handler(request):
        calls.append(request)
        raise fault(API_SECRET + str(request.url), request=request)
    async with client_for(handler) as client:
        with pytest.raises(HeadscaleError) as error:
            await client.request(case[0], **case[1])
    public = error.value.as_dict()
    assert public["category"] == category
    assert public["outcome"] == ("not_sent" if not_sent else "rejected" if NATIVE_OPERATIONS[case[0]].read_only else "unknown")
    assert public["retryable"] is False and len(calls) == 1
    assert API_SECRET not in str(error.value) + json.dumps(public) and "https:" not in json.dumps(public)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c[0])
async def test_all_task_whole_tool_denial_and_unexpected_argument_rejection(case, policy):
    tool = "headscale_" + case[2]
    policy.write_text(json.dumps({"tools": {"disabled": [tool]}}))
    calls = []
    async with client_for(lambda r: calls.append(r)) as client:
        with pytest.raises(HeadscaleError, match="POLICY_DENIED"):
            await execute_tool(tool, case[3], client, config_path=policy)
        policy.write_text(json.dumps({"tools": {}}))
        with pytest.raises(HeadscaleError, match="INVALID_ARGUMENTS"):
            await execute_tool(tool, {**case[3], "path": "/other"}, client, config_path=policy)
    assert calls == []


def test_oidc_description_does_not_invent_a_guard():
    description = next(s.description for s in TOOL_REGISTRY if s.name == "headscale_users")
    assert "cannot be renamed" not in description
    assert "identity provider account" in description and "not guaranteed" in description


@pytest.mark.parametrize("field", ["reusable", "ephemeral"])
@pytest.mark.parametrize("value", [1, 0, "true", "false", None, [], {}])
async def test_preauth_booleans_are_not_coerced(field, value, policy):
    args = {"operation": "create", "user_id": "101", "expiration": EXPIRY, "reveal_once": True, field: value}
    assert not Draft202012Validator(input_schema("headscale_preauth_keys")).is_valid(args)
    calls = []
    async with client_for(lambda r: calls.append(r)) as client:
        with pytest.raises(HeadscaleError, match="INVALID_ARGUMENTS"):
            await execute_tool("headscale_preauth_keys", args, client, config_path=policy)
    assert calls == []


@pytest.mark.parametrize("case", CASES, ids=lambda c: c[0])
async def test_each_native_rejects_wrong_type_before_io(case):
    args = dict(case[1])
    if args:
        args[next(iter(args))] = {"unexpected": "shape"}
    else:
        args["unexpected"] = "shape"
    calls = []
    async with client_for(lambda r: calls.append(r)) as client:
        with pytest.raises(HeadscaleError, match="INVALID_ARGUMENTS"):
            await client.request(case[0], **args)
    assert calls == []


@pytest.mark.parametrize("case", CASES, ids=lambda c: c[0])
async def test_direct_mcp_operation_denials_cannot_be_bypassed(case, policy):
    from fastmcp import Client
    tool = "headscale_" + case[2]
    operation = case[3].get("operation", OPERATION_POLICIES[tool].default)
    policy.write_text(json.dumps({"tools": {"disabled_operations": {tool: [operation]}}}))
    calls = []
    backend = client_for(lambda r: calls.append(r))
    async with Client(build_server(backend, config_path=policy)) as mcp:
        # Call by name even if list_tools hides the tool. Both entry paths deny.
        result = await mcp.call_tool(tool, case[3], raise_on_error=False)
        assert result.is_error and "HEADSCALE_POLICY_DENIED" in result.content[0].text
        if "operation" in case[3]:
            for selector in [operation.upper(), " " + operation, [operation], {"operation": operation}]:
                malformed = await mcp.call_tool(tool, {**case[3], "operation": selector}, raise_on_error=False)
                assert malformed.is_error
    assert calls == [] and backend.is_closed


@pytest.mark.parametrize("body", [None, [], {"node": None}, "not json"])
@pytest.mark.parametrize("native,args", [("GetNode", {"nodeId": "201"}), ("DeleteNode", {"nodeId": "201"}), ("SetPolicy", {"policy": P1})])
async def test_malformed_success_has_conservative_outcome(native, args, body):
    if native == "DeleteNode" and isinstance(body, dict):
        # Delete's defined response is an object with no mandatory fields.
        return
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=body.encode() if isinstance(body, str) else json.dumps(body).encode())
    async with client_for(handler) as client:
        with pytest.raises(HeadscaleError) as error:
            await client.request(native, **args)
    assert error.value.code == "HEADSCALE_INVALID_RESPONSE"
    assert error.value.outcome == ("rejected" if NATIVE_OPERATIONS[native].read_only else "unknown")
    assert len(calls) == 1


@pytest.mark.parametrize("status", [201, 202, 204, 301, 307, 408])
async def test_unexpected_write_status_is_not_evidence_of_rejection(status):
    calls = []
    async with client_for(lambda r: calls.append(r) or httpx.Response(status, json={})) as client:
        with pytest.raises(HeadscaleError) as error:
            await client.request("DeleteNode", nodeId="201")
    assert error.value.outcome == "unknown" and len(calls) == 1


@pytest.mark.parametrize("fault", [httpx.ReadError, httpx.ConnectError])
async def test_partial_response_stream_is_closed_without_replaying_write(fault):
    class BrokenStream(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            yield b'{"node":'
            raise fault(API_SECRET)
        async def aclose(self):
            self.closed = True
    stream, calls = BrokenStream(), []
    async with client_for(lambda r: calls.append(r) or httpx.Response(200, stream=stream)) as client:
        with pytest.raises(HeadscaleError) as error:
            await client.request("RenameNode", nodeId="201", newName="renamed")
    assert stream.closed and len(calls) == 1
    assert error.value.outcome == "unknown" and API_SECRET not in json.dumps(error.value.as_dict())


async def test_cancelled_write_closes_response_and_propagates_cancellation():
    started = asyncio.Event()
    class PendingStream(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            started.set()
            await asyncio.Event().wait()
            yield b"{}"
        async def aclose(self):
            self.closed = True
    stream, calls = PendingStream(), []
    async with client_for(lambda r: calls.append(r) or httpx.Response(200, stream=stream)) as client:
        pending = asyncio.create_task(client.request("DeleteNode", nodeId="201"))
        await asyncio.wait_for(started.wait(), 2)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    assert stream.closed and len(calls) == 1


@pytest.mark.parametrize("message,reason", [
    ("tag:secret references undefined tag", "undefined_tag"),
    ("tag names must start with a letter, after 'tag:'", "tag_name"),
    ("group members cannot be recursive", "recursive_group"),
    ('checkPeriod is only valid with action "check"', "ssh_check_period"),
    ("arbitrary unknown parser error", "policy_validation"),
    ("references undefined tag" + "x" * 4096, "policy_validation"),
])
async def test_policy_diagnostics_are_bounded_static_hints(message, reason, policy):
    raw = message + "\n\x1b[31m" + API_SECRET + LEGACY + P1
    async with client_for(lambda r: httpx.Response(400, json={"code": 3, "message": raw, "details": [{"secret": LEGACY}]})) as client:
        with pytest.raises(HeadscaleError) as error:
            await execute_tool("headscale_policy", {"operation": "check", "policy": P1}, client, config_path=policy)
    public = error.value.as_dict()
    assert public["diagnostic"]["reason"] == reason
    assert len(json.dumps(public)) < 512
    for secret in (API_SECRET, LEGACY, P1, "tag:secret", "\x1b"):
        assert secret not in json.dumps(public) + str(error.value)


@pytest.mark.parametrize("native,status,code", [("SetPolicy", 400, 3), ("CheckPolicy", 500, 3), ("CheckPolicy", 400, "3"), ("CheckPolicy", 400, True)])
async def test_policy_diagnostics_only_on_known_validation_envelope(native, status, code):
    async with client_for(lambda r: httpx.Response(status, json={"code": code, "message": "references undefined tag " + API_SECRET})) as client:
        with pytest.raises(HeadscaleError) as error:
            await client.request(native, policy=P1)
    assert "diagnostic" not in error.value.as_dict()


async def test_same_client_concurrent_apply_rechecks_stale_digest(policy):
    current, calls = P0, []
    async def handler(request):
        nonlocal current
        calls.append(request.method)
        await asyncio.sleep(0)  # Give the second task a deterministic chance to contend.
        if request.method == "PUT":
            current = json.loads(request.content)["policy"]
        return httpx.Response(200, json={"policy": current} if request.method in ("GET", "PUT") else {})
    async with client_for(handler) as client:
        results = await asyncio.gather(*[
            execute_tool("headscale_policy", {"operation": "apply", "policy": candidate, "expected_digest": DIGEST}, client, config_path=policy)
            for candidate in (P1, '{"acls": [], "groups": {}}')
        ], return_exceptions=True)
    assert sum(isinstance(r, dict) for r in results) == 1
    conflict = next(r for r in results if isinstance(r, HeadscaleError))
    assert conflict.code == "HEADSCALE_POLICY_CONFLICT" and conflict.outcome == "not_sent"
    assert calls == ["GET", "POST", "GET", "PUT", "GET", "GET"]


async def test_separate_clients_readback_cannot_guarantee_atomic_cas(policy):
    """Both prechecks can pass before either PUT; a later PUT can overwrite.

    This test deliberately demonstrates the limit rather than claiming the
    per-client lock solves GUI/CLI/Headplane or cross-process races.
    """
    current = P0
    observed = set()
    both_observed, first_finished = asyncio.Event(), asyncio.Event()
    calls = []
    p2 = '{"acls": [], "groups": {}}'
    def handler_for(name):
        gets = 0
        async def handler(request):
            nonlocal current, gets
            calls.append((name, request.method))
            if request.method == "GET":
                gets += 1
                snapshot = current
                if gets == 2:
                    observed.add(name)
                    if len(observed) == 2:
                        both_observed.set()
                    await both_observed.wait()
                    if name == "later":
                        await first_finished.wait()
                return httpx.Response(200, json={"policy": snapshot})
            if request.method == "PUT":
                current = json.loads(request.content)["policy"]
                return httpx.Response(200, json={"policy": current})
            return httpx.Response(200, json={})
        return handler
    async def apply(client, candidate, first=False):
        result = await execute_tool("headscale_policy", {"operation": "apply", "policy": candidate, "expected_digest": DIGEST}, client, config_path=policy)
        if first:
            first_finished.set()
        return result
    async with client_for(handler_for("first")) as first, client_for(handler_for("later")) as later:
        results = await asyncio.wait_for(asyncio.gather(apply(first, P1, True), apply(later, p2)), 3)
    assert current == p2 and sum(method == "PUT" for _, method in calls) == 2
    assert all(r["readback_verified"] is True and r["atomic_cas"] is False for r in results)
    assert results[0]["digest"] != results[1]["digest"]


async def test_policy_put_timeout_is_not_replayed_or_claimed_verified(policy):
    calls = []
    def handler(request):
        calls.append(request.method)
        if request.method == "PUT":
            raise httpx.ReadTimeout(API_SECRET, request=request)
        return httpx.Response(200, json={"policy": P0} if request.method == "GET" else {})
    async with client_for(handler) as client:
        with pytest.raises(HeadscaleError) as error:
            await execute_tool("headscale_policy", {"operation": "apply", "policy": P1, "expected_digest": DIGEST}, client, config_path=policy)
    assert calls == ["GET", "POST", "GET", "PUT"]
    assert error.value.outcome == "unknown" and error.value.code == "HEADSCALE_TRANSPORT_ERROR"


async def test_mcp_exposes_readback_failure_as_error_and_bumped_version(policy):
    from fastmcp import Client
    def handler(request):
        return httpx.Response(200, json={"policy": P1 if request.method == "PUT" else P0})
    backend = client_for(handler)
    server = build_server(backend, config_path=policy)
    async with Client(server) as mcp:
        assert mcp.server_info.version == "0.1.1"
        result = await mcp.call_tool("headscale_policy", {"operation": "apply", "policy": P1, "expected_digest": DIGEST}, raise_on_error=False)
    assert result.is_error
    error = json.loads(result.content[0].text)
    assert error["code"] == "HEADSCALE_POLICY_CONFLICT" and error["policy_verification"]["status"] == "conflict"
