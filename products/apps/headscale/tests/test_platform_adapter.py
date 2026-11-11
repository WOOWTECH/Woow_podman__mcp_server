import json
import os

import httpx
import pytest

from headscale_mcp_admin.platform import execute_platform_tool
from headscale_mcp_server.errors import HeadscaleError

CONNECTION = {"paas_base_url": "https://paas.example", "paas_api_token": "woow_pat_synthetic_private",
              "workspace_id": "208", "headscale_service_id": "324"}
KEY = "99999999-1111-4222-8333-444444444444"


@pytest.fixture
def policy(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"tools": {"disabled": [], "disabled_operations": {}}}))
    os.chmod(path, 0o600)
    return path


@pytest.mark.asyncio
async def test_service_discovery_is_filtered_to_bound_workspace_tailscale(policy):
    def respond(request):
        assert request.url.path == "/api/woow/v1/workspaces/208/services"
        assert request.url.params["limit"] == "100"
        return httpx.Response(200, json={"services": [
            {"id": 325, "workspace_id": 208, "name": "VPN node", "state": "running", "template": {"slug": "tailscale"}},
            {"id": 326, "workspace_id": 208, "name": "Unrelated", "template": {"slug": "odoo"}},
            {"id": 327, "workspace_id": 999, "name": "Other", "template": {"slug": "tailscale"}},
        ], "next_cursor": None})
    result = await execute_platform_tool({"operation": "tailscale_services"}, CONNECTION, config_path=policy,
                                        transport=httpx.MockTransport(respond))
    assert result == {"services": [{"id": 325, "name": "VPN node", "state": "running"}], "next_cursor": None}


@pytest.mark.asyncio
async def test_fixed_target_pair_and_stable_idempotency(policy):
    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(202, json={"operation": {"id": KEY, "state": "queued"}})
    result = await execute_platform_tool({"operation": "pair", "tailscale_service_id": "325",
            "headscale_user_id": "7", "expected_binding_version": 2, "idempotency_key": KEY}, CONNECTION,
            config_path=policy, transport=httpx.MockTransport(respond))
    assert result["operation"]["state"] == "queued"
    assert len(seen) == 1
    assert str(seen[0].url) == "https://paas.example/api/woow/v1/workspaces/208/services/325/vpn/pair"
    assert json.loads(seen[0].content) == {"headscale_service_id": 324, "headscale_user_id": 7, "expected_binding_version": 2}
    assert seen[0].headers["Idempotency-Key"] == KEY


@pytest.mark.asyncio
async def test_configuration_revision_and_null_reset(policy):
    def respond(request):
        assert request.headers["If-Match"] == '"' + 'a' * 64 + '"'
        assert json.loads(request.content) == {"values": {"headscale.logLevel": None}}
        return httpx.Response(202, json={"operation": {"id": KEY, "workspace_id": 208, "service_id": 324}})
    await execute_platform_tool({"operation": "configure", "values": {"headscale.logLevel": None},
          "expected_revision": "a" * 64, "idempotency_key": KEY}, CONNECTION, config_path=policy,
          transport=httpx.MockTransport(respond))


@pytest.mark.asyncio
async def test_configuration_field_identifiers_survive_redaction(policy):
    snapshot = {"workspace_id": 208, "service_id": 324, "revision": "a" * 64,
                "fields": [{"key": "headscale.logLevel", "type": "select", "default": "info",
                            "options": ["info", "debug"], "required": False}],
                "values": {"headscale.logLevel": "info"}, "unrelated": {"key": "must-remain-redacted"}}
    result = await execute_platform_tool({"operation": "configuration"}, CONNECTION, config_path=policy,
                                        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=snapshot)))
    assert result["fields"] == snapshot["fields"]
    assert result["values"] == snapshot["values"]
    assert result["unrelated"]["key"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_full_twelve_tool_composition_validates_for_standard_sdk_client(policy):
    from fastmcp import Client
    from headscale_mcp_server.client import HeadscaleClient
    from headscale_mcp_server.server import build_server
    from headscale_mcp_admin.platform import NAME, POLICY, add_platform_tool
    from headscale_mcp_admin import tool_registry
    upstream = HeadscaleClient("https://headscale.example", "synthetic-api-key",
                               transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"databaseConnectivity": True})))
    server = build_server(upstream, config_path=policy, extra_tool_policies={NAME: POLICY})
    add_platform_tool(server, CONNECTION, policy)
    async with Client(server) as client:
        listed = await client.list_tools()
        assert len(listed) == 12
        assert all(t.input_schema["type"] == "object" for t in listed)
        assert (await client.call_tool("headscale_health", {})).data["databaseConnectivity"] is True
    assert tool_registry.input_schema(NAME)["type"] == "object"


@pytest.mark.asyncio
async def test_denial_and_arbitrary_parameter_never_send(policy):
    seen = []
    transport = httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, json={}))
    policy.write_text(json.dumps({"tools": {"disabled": [], "disabled_operations": {"headscale_platform": ["restart"]}}}))
    for args, code in [({"operation": "restart", "idempotency_key": KEY}, "HEADSCALE_POLICY_DENIED"),
                       ({"operation": "configuration", "workspace_id": "999"}, "PAAS_INVALID_ARGUMENTS"),
                       ({"operation": "configure", "values": {"ngrok.authtoken": "private"}, "expected_revision": "a"*64, "idempotency_key": KEY}, "PAAS_INVALID_ARGUMENTS")]:
        with pytest.raises(HeadscaleError) as error:
            await execute_platform_tool(args, CONNECTION, config_path=policy, transport=transport)
        assert error.value.code == code
    assert not seen


@pytest.mark.asyncio
async def test_operation_cannot_disclose_other_service(policy):
    with pytest.raises(HeadscaleError, match="PAAS_TARGET_MISMATCH"):
        await execute_platform_tool({"operation": "operation", "operation_id": KEY}, CONNECTION,
              config_path=policy, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
                  "operation": {"id": KEY, "workspace_id": 208, "service_id": 999}})))


@pytest.mark.asyncio
async def test_write_timeout_is_unknown_without_retry(policy):
    seen = []
    def respond(request):
        seen.append(request)
        raise httpx.ReadTimeout("upstream disconnected")
    with pytest.raises(HeadscaleError) as error:
        await execute_platform_tool({"operation": "restart", "idempotency_key": KEY}, CONNECTION,
              config_path=policy, transport=httpx.MockTransport(respond))
    assert error.value.outcome == "unknown"
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_redirect_and_credential_echo_are_not_forwarded(policy):
    seen = []
    def redirect(request):
        seen.append(request)
        return httpx.Response(302, headers={"location": "https://elsewhere.example"}, json={})
    with pytest.raises(HeadscaleError):
        await execute_platform_tool({"operation": "configuration"}, CONNECTION, config_path=policy,
                                    transport=httpx.MockTransport(redirect))
    assert len(seen) == 1
    result = await execute_platform_tool({"operation": "configuration"}, CONNECTION, config_path=policy,
          transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"workspace_id":208,"service_id":324,
                                    "unsafe_echo":CONNECTION["paas_api_token"]})))
    assert CONNECTION["paas_api_token"] not in json.dumps(result)
