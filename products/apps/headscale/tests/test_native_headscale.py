"""External wire fixtures, authorization, network pinning and MCP contract tests.

Fixtures are synthetic. These tests do not claim real SSH/client enrollment.
The expected 29 bindings below are an independent copy of the pinned upstream
Swagger, not generated from the adapter implementation.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import sys

import httpcore
import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "apps/headscale"), str(ROOT / "packages/mcp-admin-core")]

from headscale_mcp_server.client import HeadscaleClient, MAX_RESPONSE_BYTES
from headscale_mcp_server.errors import HeadscaleError
from headscale_mcp_server.http_policy import Destination, PinnedNetwork, normalize_base_url
from headscale_mcp_server.native import NATIVE_OPERATIONS
from headscale_mcp_server.policy import SavedPolicy
from headscale_mcp_server.registry import COVERAGE_MATRIX, OPERATION_POLICIES, TOOL_REGISTRY, input_schema, normalize_arguments
from headscale_mcp_server.server import ABSENT_POLICY_DIGEST, build_server, execute_tool, policy_digest

AUTH = "hskey-authreq-" + "a" * 24
EXPIRY = "2035-01-01T00:00:00Z"
API_SECRET = "fixture-upstream-credential"
LEGACY = "fixture-legacy-secret"
CREATED = "fixture-created-secret"
P0 = '{"acls":[]}'
P1 = '{"acls":[{"action":"accept","src":["*"],"dst":["*:*"]}]}'
DIGEST = hashlib.sha256(P0.encode()).hexdigest()
PREFIX = "AbCdEf012345"


@pytest.mark.parametrize("cidr,url", [
    ("100.100.100.200/32", "http://100.100.100.200"),
    ("fd00:ec2::254/128", "http://[fd00:ec2::254]"),
])
def test_private_allowlist_never_authorizes_cloud_metadata(monkeypatch, cidr, url):
    monkeypatch.setenv("HEADSCALE_MCP_ALLOWED_PRIVATE_CIDRS", cidr)
    with pytest.raises(HeadscaleError, match="HEADSCALE_DESTINATION_DENIED"):
        Destination(url)

# native, native arguments, task, task arguments, HTTP verb, path, query, JSON
CASES = [
 ("CreateUser", {"name":"mcp-test","displayName":"Test","email":"test@example.invalid","pictureUrl":""}, "users", {"operation":"create","name":"mcp-test","display_name":"Test","email":"test@example.invalid","picture_url":""}, "POST", "/user", {}, {"name":"mcp-test","displayName":"Test","email":"test@example.invalid","pictureUrl":""}),
 ("RenameUser", {"oldId":"101","newName":"mcp-new"}, "users", {"operation":"rename","user_id":"101","new_name":"mcp-new"}, "POST", "/user/101/rename/mcp-new", {}, None),
 ("DeleteUser", {"id":"101"}, "users", {"operation":"delete","user_id":"101"}, "DELETE", "/user/101", {}, None),
 ("ListUsers", {"id":"101"}, "users", {"operation":"list","user_id":"101"}, "GET", "/user", {"id":"101"}, None),
 ("CreatePreAuthKey", {"user":"101","reusable":False,"ephemeral":False,"expiration":EXPIRY,"aclTags":[]}, "preauth_keys", {"operation":"create","user_id":"101","expiration":EXPIRY,"reveal_once":True}, "POST", "/preauthkey", {}, {"user":"101","reusable":False,"ephemeral":False,"expiration":EXPIRY,"aclTags":[]}),
 ("ExpirePreAuthKey", {"id":"301"}, "preauth_keys", {"operation":"expire","key_id":"301"}, "POST", "/preauthkey/expire", {}, {"id":"301"}),
 ("DeletePreAuthKey", {"id":"301"}, "preauth_keys", {"operation":"delete","key_id":"301"}, "DELETE", "/preauthkey", {"id":"301"}, None),
 ("ListPreAuthKeys", {}, "preauth_keys", {"operation":"list"}, "GET", "/preauthkey", {}, None),
 ("DebugCreateNode", {"user":"mcp-test","key":AUTH,"name":"test-device","routes":[]}, "debug_node", {"user":"mcp-test","auth_id":AUTH,"name":"test-device","confirm":True}, "POST", "/debug/node", {}, {"user":"mcp-test","key":AUTH,"name":"test-device","routes":[]}),
 ("GetNode", {"nodeId":"201"}, "nodes", {"operation":"get","node_id":"201"}, "GET", "/node/201", {}, None),
 ("SetTags", {"nodeId":"201","tags":["tag:test"]}, "nodes", {"operation":"set_tags","node_id":"201","tags":["tag:test"]}, "POST", "/node/201/tags", {}, {"tags":["tag:test"]}),
 ("SetApprovedRoutes", {"nodeId":"201","routes":[]}, "routes_replace", {"node_id":"201","routes":[]}, "POST", "/node/201/approve_routes", {}, {"routes":[]}),
 ("RegisterNode", {"user":"mcp-test","key":AUTH}, "register", {"operation":"legacy","user":"mcp-test","auth_id":AUTH}, "POST", "/node/register", {"user":"mcp-test","key":AUTH}, None),
 ("DeleteNode", {"nodeId":"201"}, "nodes", {"operation":"delete","node_id":"201"}, "DELETE", "/node/201", {}, None),
 ("ExpireNode", {"nodeId":"201","disableExpiry":True}, "nodes", {"operation":"expire","node_id":"201","mode":"disable"}, "POST", "/node/201/expire", {"disableExpiry":"true"}, None),
 ("RenameNode", {"nodeId":"201","newName":"renamed"}, "nodes", {"operation":"rename","node_id":"201","new_name":"renamed"}, "POST", "/node/201/rename/renamed", {}, None),
 ("ListNodes", {"user":"mcp-test"}, "nodes", {"operation":"list","user":"mcp-test"}, "GET", "/node", {"user":"mcp-test"}, None),
 ("BackfillNodeIPs", {"confirmed":True}, "backfill_ips", {"confirm":True}, "POST", "/node/backfillips", {"confirmed":"true"}, None),
 ("AuthRegister", {"user":"mcp-test","authId":AUTH}, "register", {"operation":"register","user":"mcp-test","auth_id":AUTH}, "POST", "/auth/register", {}, {"user":"mcp-test","authId":AUTH}),
 ("AuthApprove", {"authId":AUTH}, "auth", {"operation":"approve","auth_id":AUTH}, "POST", "/auth/approve", {}, {"authId":AUTH}),
 ("AuthReject", {"authId":AUTH}, "auth", {"operation":"reject","auth_id":AUTH}, "POST", "/auth/reject", {}, {"authId":AUTH}),
 ("CreateApiKey", {"expiration":EXPIRY}, "api_keys", {"operation":"create","expiration":EXPIRY,"reveal_once":True}, "POST", "/apikey", {}, {"expiration":EXPIRY}),
 ("ExpireApiKey", {"id":"401"}, "api_keys", {"operation":"expire","key_id":"401"}, "POST", "/apikey/expire", {}, {"id":"401"}),
 ("ListApiKeys", {}, "api_keys", {"operation":"list"}, "GET", "/apikey", {}, None),
 ("DeleteApiKey", {"prefix":PREFIX}, "api_keys", {"operation":"delete","prefix":PREFIX}, "DELETE", "/apikey/"+PREFIX, {}, None),
 ("GetPolicy", {}, "policy", {"operation":"get"}, "GET", "/policy", {}, None),
 ("SetPolicy", {"policy":P1}, "policy", {"operation":"apply","policy":P1,"expected_digest":DIGEST}, "PUT", "/policy", {}, {"policy":P1}),
 ("CheckPolicy", {"policy":P1}, "policy", {"operation":"check","policy":P1}, "POST", "/policy/check", {}, {"policy":P1}),
 ("Health", {}, "health", {}, "GET", "/health", {}, None),
]


def response_for(native):
    node = {"id":"201", "givenName":"test-device", "preAuthKey":{"id":"301","key":LEGACY}, "tags":[]}
    field = NATIVE_OPERATIONS[native].response_field
    if not field:
        return {}
    return {field: {"node":node,"user":{"id":"101","name":"mcp-test"},"nodes":[node],
                   "users":[{"id":"101","name":"mcp-test"}],"preAuthKey":{"id":"301","key":CREATED},
                   "preAuthKeys":[{"id":"301","key":LEGACY}],"apiKey":CREATED,
                   "apiKeys":[{"id":"401","prefix":PREFIX}],"policy":P1 if native == "SetPolicy" else P0,
                   "changes":[],"databaseConnectivity":True}[field]}


@pytest.fixture
def policy(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"tools":{"disabled":[],"disabled_operations":{}}}))
    path.chmod(0o600)
    return path


def client_for(handler):
    return HeadscaleClient("https://headscale.example.com/api/v1", API_SECRET, transport=httpx.MockTransport(handler))


def assert_wire(req, case):
    native, parameters, tool, arguments, method, path, query, body = case
    assert req.method == method
    assert req.url.path == "/api/v1" + path
    assert dict(req.url.params) == query
    assert (json.loads(req.content) if req.content else None) == body
    assert req.headers["authorization"] == "Bearer " + API_SECRET
    assert req.url.host == "headscale.example.com"


def test_exact_upstream_29_and_task_metadata():
    expected = {case[0] for case in CASES}
    assert len(expected) == len(CASES) == 29
    assert set(NATIVE_OPERATIONS) == expected
    assert {row["operation_id"] for row in COVERAGE_MATRIX} == expected
    assert len(COVERAGE_MATRIX) == 29
    assert len(TOOL_REGISTRY) == 11
    for case in CASES:
        op = NATIVE_OPERATIONS[case[0]]
        assert op.method == case[4]
    assert NATIVE_OPERATIONS["CheckPolicy"].read_only


@pytest.mark.parametrize("case", CASES, ids=lambda c:c[0])
async def test_native_29_wire_and_no_existing_secret_leak(case):
    calls = []
    def handler(req):
        calls.append(req)
        assert_wire(req, case)
        return httpx.Response(200, json=response_for(case[0]))
    async with client_for(handler) as client:
        result = await client.request(case[0], **case[1])
    assert len(calls) == 1
    assert LEGACY not in json.dumps(result)
    assert CREATED not in json.dumps(result)
    assert client.is_closed


@pytest.mark.parametrize("case", CASES, ids=lambda c:c[0])
async def test_task_29_dispatches_native_contract(case, policy):
    calls = []
    def handler(req):
        calls.append(req)
        if case[0] == "SetPolicy" and req.method == "GET":
            return httpx.Response(200,json={"policy":P1 if any(r.method == "PUT" for r in calls) else P0})
        if case[0] == "SetPolicy" and req.url.path.endswith("/check"):
            return httpx.Response(200,json={})
        assert_wire(req, case)
        return httpx.Response(200,json=response_for(case[0]))
    async with client_for(handler) as client:
        result = await execute_tool("headscale_"+case[2],case[3],client,config_path=policy)
    assert isinstance(result,dict)
    assert len(calls) == (5 if case[0] == "SetPolicy" else 1)
    assert LEGACY not in json.dumps(result)
    assert (CREATED in json.dumps(result)) == (case[0] in ("CreateApiKey","CreatePreAuthKey"))
    if case[0] == "SetPolicy":
        assert [r.method for r in calls] == ["GET","POST","GET","PUT","GET"]
        assert result["atomic_cas"] is False
        assert result["readback_verified"] is True
    if case[0] in ("AuthApprove","AuthReject"):
        assert result["registered"] is False


@pytest.mark.parametrize("case", CASES, ids=lambda c:c[0])
async def test_each_task_operation_denial_has_zero_upstream_requests(case, policy):
    name = "headscale_"+case[2]
    operation = case[3].get("operation",OPERATION_POLICIES[name].default)
    policy.write_text(json.dumps({"tools":{"disabled_operations":{name:[operation]}}}))
    calls = []
    async with client_for(lambda req: calls.append(req)) as client:
        with pytest.raises(HeadscaleError,match="POLICY_DENIED"):
            await execute_tool(name,case[3],client,config_path=policy)
    assert calls == []


@pytest.mark.parametrize("value", [True,1,0,-1,"0","01","-1","1.0"," 1","1 ","١","18446744073709551616","../user","1%2fuser",None])
async def test_strict_ids_deny_before_io(value, policy):
    calls = []
    async with client_for(lambda r:calls.append(r)) as client:
        with pytest.raises(HeadscaleError,match="INVALID_ARGUMENTS"):
            await execute_tool("headscale_nodes",{"operation":"get","node_id":value},client,config_path=policy)
    assert calls == []


async def test_large_id_keeps_exact_decimal_string(policy):
    calls = []
    async with client_for(lambda r:(calls.append(r) or httpx.Response(200,json={"node":{"id":"9007199254740993"}}))) as c:
        result = await execute_tool("headscale_nodes",{"operation":"get","node_id":"9007199254740993"},c,config_path=policy)
    assert calls[0].url.path.endswith("/9007199254740993")
    assert result["node"]["id"] == "9007199254740993"


@pytest.mark.parametrize("name,args", [
 ("headscale_users",{"operation":"update","user_id":"101","email":"changed"}),
 ("headscale_users",{"operation":"list","user_id":"101","name":"test"}),
 ("headscale_users",{"operation":"create","name":"test","provider":"oidc"}),
 ("headscale_nodes",{"operation":"set_tags","node_id":"201","tags":[]}),
 ("headscale_nodes",{"operation":"expire","node_id":"201","mode":"disable","expiry":EXPIRY}),
 ("headscale_nodes",{"operation":"expire","node_id":"201","mode":"at"}),
 ("headscale_register",{"operation":"register","user":"test","auth_id":"a"*24}),
 ("headscale_auth",{"operation":"approve","auth_id":AUTH,"user":"test"}),
 ("headscale_preauth_keys",{"operation":"create","expiration":EXPIRY,"reveal_once":True}),
 ("headscale_api_keys",{"operation":"expire","key_id":"401","prefix":PREFIX}),
 ("headscale_api_keys",{"operation":"delete","key_id":"401"}),
 ("headscale_api_keys",{"operation":"create","expiration":EXPIRY,"reveal_once":False}),
 ("headscale_api_keys",{"operation":"create","expiration":EXPIRY,"reveal_once":1}),
 ("headscale_backfill_ips",{"confirm":1}),
 ("headscale_backfill_ips",{"confirm":False}),
 ("headscale_health",{"base_url":"https://other.example"}),
 ("headscale_routes_replace",{"node_id":"201","routes":["192.168.1.1"]}),
])
async def test_task_constraints_cannot_be_bypassed(name,args,policy):
    calls=[]
    async with client_for(lambda r:calls.append(r)) as c:
        with pytest.raises(HeadscaleError,match="INVALID_ARGUMENTS"):
            await execute_tool(name,args,c,config_path=policy)
    assert calls==[]


@pytest.mark.parametrize("broken", ['{}','not-json','{"tools":null}','{"tools":{"disabled_operations":[]}}','{"tools":{"disabled":[],"disabled":["headscale_health"]}}'])
async def test_corrupt_saved_policy_fails_closed(broken,policy):
    policy.write_text(broken)
    calls=[]
    async with client_for(lambda r:calls.append(r)) as c:
        with pytest.raises(HeadscaleError,match="POLICY_UNAVAILABLE"):
            await execute_tool("headscale_health",{},c,config_path=policy)
    assert calls==[]


@pytest.mark.parametrize("kind", ["missing","symlink","hardlink","permissions","directory"])
def test_unsafe_saved_policy_is_never_repaired(kind,policy):
    backup=policy.with_name("backup.json")
    if kind=="missing":policy.unlink()
    elif kind=="symlink":policy.rename(backup);policy.symlink_to(backup)
    elif kind=="hardlink":os.link(policy,backup)
    elif kind=="permissions":policy.chmod(0o640)
    else:policy.unlink();policy.mkdir()
    with pytest.raises(HeadscaleError,match="POLICY_UNAVAILABLE"):SavedPolicy(policy).read()
    if kind=="missing":assert not policy.exists()
    if kind=="permissions":assert policy.stat().st_mode & 0o777 == 0o640


async def test_policy_revoke_during_validation_prevents_put(policy):
    calls=[]
    def handler(r):
        calls.append(r.method)
        if r.method=="POST":
            policy.write_text(json.dumps({"tools":{"disabled_operations":{"headscale_policy":["apply"]}}}))
            return httpx.Response(200,json={})
        return httpx.Response(200,json={"policy":P0})
    async with client_for(handler) as c:
        with pytest.raises(HeadscaleError,match="POLICY_DENIED"):
            await execute_tool("headscale_policy",{"operation":"apply","policy":P1,"expected_digest":DIGEST},c,config_path=policy)
    assert calls==["GET","POST"]


# Captured from an untouched v0.29.4 database-mode instance. Other 500s,
# including file-mode loading failures, must never stand in for absence.
POLICY_ABSENT_ERROR = {"code": 2, "message": "loading ACL from database: acl policy not found", "details": []}


async def test_virgin_policy_get_and_first_apply(policy):
    calls = []
    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            if ("PUT", "/api/v1/policy") in calls:
                return httpx.Response(200, json={"policy": P1})
            return httpx.Response(500, json=POLICY_ABSENT_ERROR)
        if request.method == "POST":
            assert request.url.path == "/api/v1/policy/check"
            assert json.loads(request.content) == {"policy": P1}
            return httpx.Response(200, json={})
        assert request.method == "PUT"
        assert json.loads(request.content) == {"policy": P1}
        return httpx.Response(200, json={"policy": P1})
    async with client_for(handler) as client:
        absent = await execute_tool("headscale_policy", {"operation": "get"}, client, config_path=policy)
        assert absent["exists"] is False and absent["policy"] == ""
        assert len(absent["digest"]) == 64 and absent["digest"] != policy_digest("")
        same = await execute_tool("headscale_policy", {"operation": "get"}, client, config_path=policy)
        assert same == absent
        calls.clear()
        result = await execute_tool("headscale_policy", {"operation": "apply", "policy": P1,
                                    "expected_digest": absent["digest"]}, client, config_path=policy)
    assert calls == [("GET", "/api/v1/policy"), ("POST", "/api/v1/policy/check"),
                     ("GET", "/api/v1/policy"), ("PUT", "/api/v1/policy"), ("GET", "/api/v1/policy")]
    assert result["exists"] is True and result["previous_exists"] is False
    assert result["digest"] == policy_digest(P1) and result["atomic_cas"] is False


async def test_native_policy_absence_is_a_safe_specific_error():
    async with client_for(lambda request: httpx.Response(500, json=POLICY_ABSENT_ERROR)) as client:
        with pytest.raises(HeadscaleError) as error:
            await client.request("GetPolicy")
    assert error.value.code == "HEADSCALE_POLICY_NOT_SET"
    assert error.value.http_status == 500 and error.value.grpc_code == 2
    assert POLICY_ABSENT_ERROR["message"] not in str(error.value)


@pytest.mark.parametrize("status,body", [
    (404, POLICY_ABSENT_ERROR),
    (503, POLICY_ABSENT_ERROR),
    (500, {**POLICY_ABSENT_ERROR, "code": 5}),
    (500, {**POLICY_ABSENT_ERROR, "code": 2.0}),
    (500, {**POLICY_ABSENT_ERROR, "code": "2"}),
    (500, {**POLICY_ABSENT_ERROR, "message": "loading ACL from database: connection refused"}),
    (500, {**POLICY_ABSENT_ERROR, "message": 'reading policy from path "/config/policy.hujson": no such file or directory'}),
    (500, {**POLICY_ABSENT_ERROR, "message": "no supported policy mode found in configuration"}),
    (500, {**POLICY_ABSENT_ERROR, "message": POLICY_ABSENT_ERROR["message"] + "; " + API_SECRET}),
    (500, {**POLICY_ABSENT_ERROR, "details": [{"reason": "other error"}]}),
    (500, {**POLICY_ABSENT_ERROR, "extra": "unrecognized"}),
    (500, {"code": 2, "message": POLICY_ABSENT_ERROR["message"]}),
    (500, "acl policy not found"),
])
async def test_policy_other_errors_never_initialize(status, body, policy):
    calls = []
    def handler(request):
        calls.append(request.method)
        return httpx.Response(status, json=body)
    async with client_for(handler) as client:
        with pytest.raises(HeadscaleError) as error:
            await execute_tool("headscale_policy", {"operation": "apply", "policy": P1,
                "expected_digest": ABSENT_POLICY_DIGEST}, client, config_path=policy)
    assert calls == ["GET"]
    assert error.value.code == "HEADSCALE_UPSTREAM_ERROR"
    assert API_SECRET not in str(error.value) and "loading ACL" not in str(error.value)


@pytest.mark.parametrize("operation,args", [("SetPolicy", {"policy": P1}), ("CheckPolicy", {"policy": P1}), ("ListUsers", {})])
async def test_policy_absence_signature_is_not_recognized_on_other_operations(operation, args):
    async with client_for(lambda request: httpx.Response(500, json=POLICY_ABSENT_ERROR)) as client:
        with pytest.raises(HeadscaleError) as error:
            await client.request(operation, **args)
    assert error.value.code == "HEADSCALE_UPSTREAM_ERROR"
    if operation == "SetPolicy":
        assert error.value.outcome == "unknown"


@pytest.mark.parametrize("appears_at", [1, 2])
@pytest.mark.parametrize("created_policy", ["", P0])
async def test_virgin_policy_concurrent_create_prevents_overwrite(appears_at, created_policy, policy):
    gets = 0
    calls = []
    def handler(request):
        nonlocal gets
        calls.append(request.method)
        if request.method == "GET":
            gets += 1
            if gets >= appears_at:
                return httpx.Response(200, json={"policy": created_policy})
            return httpx.Response(500, json=POLICY_ABSENT_ERROR)
        return httpx.Response(200, json={})
    async with client_for(handler) as client:
        with pytest.raises(HeadscaleError, match="HEADSCALE_POLICY_CONFLICT"):
            await execute_tool("headscale_policy", {"operation": "apply", "policy": P1,
                "expected_digest": ABSENT_POLICY_DIGEST}, client, config_path=policy)
    assert calls == (["GET"] if appears_at == 1 else ["GET", "POST", "GET"])


async def test_virgin_policy_check_rejection_prevents_put(policy):
    calls = []
    def handler(request):
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(500, json=POLICY_ABSENT_ERROR)
        return httpx.Response(400, json={"code": 3, "message": API_SECRET, "details": []})
    async with client_for(handler) as client:
        with pytest.raises(HeadscaleError, match="HEADSCALE_UPSTREAM_ERROR"):
            await execute_tool("headscale_policy", {"operation": "apply", "policy": P1,
                "expected_digest": ABSENT_POLICY_DIGEST}, client, config_path=policy)
    assert calls == ["GET", "POST"]


async def test_virgin_policy_observed_read_failure_prevents_put(policy):
    gets = 0
    calls = []
    def handler(request):
        nonlocal gets
        calls.append(request.method)
        if request.method == "GET":
            gets += 1
            if gets == 1:
                return httpx.Response(500, json=POLICY_ABSENT_ERROR)
            return httpx.Response(500, json={"code": 2, "message": "database offline", "details": []})
        return httpx.Response(200, json={})
    async with client_for(handler) as client:
        with pytest.raises(HeadscaleError, match="HEADSCALE_UPSTREAM_ERROR"):
            await execute_tool("headscale_policy", {"operation": "apply", "policy": P1,
                "expected_digest": ABSENT_POLICY_DIGEST}, client, config_path=policy)
    assert calls == ["GET", "POST", "GET"]


async def test_virgin_policy_diff_does_not_persist(policy):
    calls = []
    def handler(request):
        calls.append(request.method)
        return httpx.Response(500, json=POLICY_ABSENT_ERROR)
    async with client_for(handler) as client:
        result = await execute_tool("headscale_policy", {"operation": "diff", "policy": P1},
                                    client, config_path=policy)
    assert calls == ["GET"]
    assert result["exists"] is False and result["persisted"] is False
    assert result["digest"] == ABSENT_POLICY_DIGEST and "+" + P1 in result["diff"]


@pytest.mark.parametrize("change_at", [1,2])
async def test_policy_digest_conflict_never_puts(change_at,policy):
    gets=0;calls=[]
    def handler(r):
        nonlocal gets
        calls.append(r.method)
        if r.method=="GET":
            gets+=1
            return httpx.Response(200,json={"policy":P1 if gets>=change_at else P0})
        return httpx.Response(200,json={})
    async with client_for(handler) as c:
        with pytest.raises(HeadscaleError,match="POLICY_CONFLICT"):
            await execute_tool("headscale_policy",{"operation":"apply","policy":P1,"expected_digest":DIGEST},c,config_path=policy)
    assert "PUT" not in calls


@pytest.mark.parametrize("dependency", ["get","check"])
async def test_apply_dependency_policy_denial_before_io(dependency,policy):
    policy.write_text(json.dumps({"tools":{"disabled_operations":{"headscale_policy":[dependency]}}}))
    calls=[]
    async with client_for(lambda r:calls.append(r)) as c:
        with pytest.raises(HeadscaleError,match="POLICY_DENIED"):
            await execute_tool("headscale_policy",{"operation":"apply","policy":P1,"expected_digest":DIGEST},c,config_path=policy)
    assert calls==[]


@pytest.mark.parametrize("status", [301,302,307,308,400,401,403,500,503])
async def test_no_redirect_or_write_retry_no_raw_error_leak(status):
    calls=[]
    def handler(r):
        calls.append(r)
        return httpx.Response(status,headers={"location":"https://attacker.example/"},json={"message":API_SECRET+LEGACY,"code":13})
    async with client_for(handler) as c:
        with pytest.raises(HeadscaleError) as error:await c.request("CreateUser",name="test")
    assert len(calls)==1
    assert API_SECRET not in str(error.value) and LEGACY not in str(error.value)
    assert error.value.outcome==("unknown" if status>=500 or status<400 else "rejected")


async def test_disconnected_write_outcome_unknown_and_not_retried():
    calls=[]
    def handler(r):calls.append(r);raise httpx.ReadTimeout("may contain secret",request=r)
    async with client_for(handler) as c:
        with pytest.raises(HeadscaleError) as error:await c.request("DeleteNode",nodeId="201")
    assert len(calls)==1 and error.value.outcome=="unknown"
    assert "may contain secret" not in str(error.value)


@pytest.mark.parametrize("body", [{"databaseConnectivity":"true"},{"databaseConnectivity":1},{},[],None])
async def test_health_response_requires_actual_boolean(body):
    async with client_for(lambda r:httpx.Response(200,json=body)) as c:
        with pytest.raises(HeadscaleError,match="INVALID_RESPONSE"):await c.request("Health")


@pytest.mark.parametrize("value", [True,False])
async def test_health_keeps_boolean_even_when_unhealthy(value):
    async with client_for(lambda r:httpx.Response(200,json={"databaseConnectivity":value})) as c:
        assert await c.request("Health")=={"databaseConnectivity":value}


async def test_oversize_response_bounded():
    async with client_for(lambda r:httpx.Response(200,content=b"x"*(MAX_RESPONSE_BYTES+1))) as c:
        with pytest.raises(HeadscaleError,match="RESPONSE_TOO_LARGE"):await c.request("ListNodes")


@pytest.mark.parametrize("url", ["http://u:p@example.com","https://example.com/other","https://example.com?x=1","https://example.com#x","https://example.com/%2e","https://example.com/../api/v1","https://example.com\\@localhost"," http://example.com","http://example.com:0","http://127.0.0.1","http://169.254.169.254","http://[::ffff:127.0.0.1]"])
def test_bad_origin_or_always_denied_ip(url,monkeypatch):
    monkeypatch.delenv("HEADSCALE_MCP_ALLOWED_PRIVATE_CIDRS",raising=False)
    with pytest.raises(HeadscaleError):Destination(url)


def test_private_allowlist_is_exact_deployment_env_only(monkeypatch):
    monkeypatch.delenv("HEADSCALE_MCP_ALLOWED_PRIVATE_CIDRS",raising=False)
    with pytest.raises(HeadscaleError):Destination("http://10.43.1.2")
    with pytest.raises(HeadscaleError):Destination("http://10.43.1.2",("10.43.0.0/16",))
    monkeypatch.setenv("HEADSCALE_MCP_ALLOWED_PRIVATE_CIDRS","10.43.0.0/16")
    assert Destination("http://10.43.1.2").answers()==["10.43.1.2"]
    with pytest.raises(HeadscaleError):Destination("http://10.42.1.2")
    with pytest.raises(HeadscaleError):Destination("http://10.43.1.2",("10.0.0.0/8",))
    monkeypatch.setenv("HEADSCALE_MCP_ALLOWED_PRIVATE_CIDRS","0.0.0.0/0")
    with pytest.raises(HeadscaleError):Destination("https://headscale.example.com")


async def test_dns_is_pinned_to_validated_numeric_tcp_address(monkeypatch):
    monkeypatch.setenv("HEADSCALE_MCP_ALLOWED_PRIVATE_CIDRS","10.43.0.0/16")
    queries=[];tcp=[]
    def lookup(host,port,**kw):
        queries.append(host)
        return [(socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_TCP,"",("10.43.1.2",port))]
    async def connect(self,host,port,**kw):tcp.append((host,port));return "stream"
    monkeypatch.setattr(socket,"getaddrinfo",lookup)
    monkeypatch.setattr("httpcore._backends.anyio.AnyIOBackend.connect_tcp",connect)
    backend=PinnedNetwork(Destination("http://headscale.sandbox.svc:8080"))
    assert await backend.connect_tcp("headscale.sandbox.svc",8080)=="stream"
    assert queries==["headscale.sandbox.svc"] and tcp==[("10.43.1.2",8080)]


async def test_mixed_dns_answer_is_rejected_before_tcp(monkeypatch):
    monkeypatch.setenv("HEADSCALE_MCP_ALLOWED_PRIVATE_CIDRS","10.43.0.0/16")
    tcp=[]
    monkeypatch.setattr(socket,"getaddrinfo",lambda host,port,**kw:[(socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_TCP,"",(ip,port)) for ip in ["10.43.1.2","169.254.169.254"]])
    async def connect(self,*a,**kw):tcp.append(a)
    monkeypatch.setattr("httpcore._backends.anyio.AnyIOBackend.connect_tcp",connect)
    with pytest.raises(HeadscaleError,match="DESTINATION_DENIED"):
        await PinnedNetwork(Destination("http://headscale.sandbox.svc:8080")).connect_tcp("headscale.sandbox.svc",8080)
    assert tcp==[]


async def test_mcp_wire_schemas_live_policy_and_structured_output(policy):
    from fastmcp import Client
    from jsonschema import Draft202012Validator
    calls=[]
    def handler(r):calls.append(r);return httpx.Response(200,json={"databaseConnectivity":True})
    backend=client_for(handler)
    async with Client(build_server(backend,config_path=policy)) as mcp:
        tools=await mcp.list_tools()
        assert {t.name for t in tools}=={s.name for s in TOOL_REGISTRY}
        for tool in tools:
            Draft202012Validator.check_schema(tool.input_schema)
            # FastMCP inlines refs for MCP clients; compare accepted public inputs.
            for case in CASES:
                if "headscale_"+case[2] == tool.name:
                    Draft202012Validator(tool.input_schema).validate(case[3])
                    Draft202012Validator(input_schema(tool.name)).validate(case[3])
        result=await mcp.call_tool("headscale_health",{})
        assert not result.is_error
        assert result.structured_content=={"databaseConnectivity":True}
        policy.write_text(json.dumps({"tools":{"disabled":["headscale_health"]}}))
        denied=await mcp.call_tool("headscale_health",{},raise_on_error=False)
        assert denied.is_error and "HEADSCALE_POLICY_DENIED" in denied.content[0].text
        assert "headscale_health" not in {t.name for t in await mcp.list_tools()}
        policy.write_text(json.dumps({"tools":{"disabled":[]}}))
        assert not (await mcp.call_tool("headscale_health",{})).is_error
    assert len(calls)==2 and backend.is_closed


async def test_trusted_extension_availability_tracks_saved_revocation(policy):
    from fastmcp import Client
    from mcp_admin_core.operation_policy import OperationSpec

    calls = []
    backend = client_for(lambda request: calls.append(request))
    extension = "headscale_platform"
    spec = OperationSpec(("read", "write"), parameter="operation", includes={"write": ("read",)})
    server = build_server(backend, policy, extra_tool_policies={extension: spec})

    @server.tool(name=extension)
    async def platform(operation: str) -> dict:
        return {"operation": operation}

    @server.tool(name="unreviewed_extension")
    async def unreviewed() -> dict:
        return {}

    async with Client(server) as mcp:
        names = {tool.name for tool in await mcp.list_tools()}
        assert names == {tool.name for tool in TOOL_REGISTRY} | {extension}
        # Whole-tool denial and operation dependencies both affect live listing.
        for restriction in ({"disabled": [extension]},
                            {"disabled_operations": {extension: ["read"]}}):
            policy.write_text(json.dumps({"tools": restriction}))
            assert extension not in {tool.name for tool in await mcp.list_tools()}
        # The allowed read keeps a partially revoked extension discoverable.
        policy.write_text(json.dumps({"tools": {"disabled_operations": {extension: ["write"]}}}))
        assert extension in {tool.name for tool in await mcp.list_tools()}
        policy.write_text(json.dumps({"tools": {}}))
        assert extension in {tool.name for tool in await mcp.list_tools()}
    assert backend.is_closed and not calls

