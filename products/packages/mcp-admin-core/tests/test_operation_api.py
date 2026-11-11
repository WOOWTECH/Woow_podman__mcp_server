"""Saved operation policy API, strict reads, atomic siblings and effective counts."""

from management_client import revision_headers, async_revision_headers
import asyncio
import importlib
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from hermes_mcp_admin import tool_registry as hermes
from mcp_admin_core import capabilities
from mcp_admin_core.app import create_app
from mcp_admin_core.operation_policy import policies_for
from mcp_admin_core.operation_policy import disabled_operation_calls
from mcp_admin_core.routers.tools import create_tools_router


def application(registry=hermes, sync=False):
    return create_app("Operation API", product_id="fixture", tool_registry=registry,
        extra_routers=[create_tools_router(registry=registry, mcp_label="fixture", operations_supported=sync)])


def logged_in(app):
    client = TestClient(app)
    token = client.post("/api/auth/login", json={"password": "admin"}).json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client


async def test_supported_metadata_and_saved_restrictions_round_trip(store_env):
    await store_env.patch("tools", {"disabled": ["hermes_session", "retired_tool"],
        "disabled_operations": {"hermes_session": ["delete"], "old_tool": ["remove"]}, "custom_sibling": 42})
    client = logged_in(application())
    data = client.get("/api/tools").json()
    tools = {tool["name"]: tool for tool in data["tools"]}
    assert tools["hermes_session"]["enabled"] is False
    assert tools["hermes_session"]["operation_default"] == "list"
    assert tools["hermes_inspect"]["operation_parameter"] == "target"
    assert tools["hermes_model"]["operations"] == ["info", "set", "list_providers"]
    assert data["disabled_operations"] == {"hermes_session": ["delete"], "old_tool": ["remove"]}
    # A supported edit doesn't drop saved denies for absent metadata or siblings.
    response = client.put("/api/tools/operations", json={"disabled_operations": {"hermes_config": ["set"]}}, headers=revision_headers(client, None, read_path='/api/tools'))
    assert response.status_code == 200
    assert response.json()["disabled_operations"] == {"hermes_config": ["set"], "old_tool": ["remove"]}
    stored = await store_env.get("tools")
    assert stored["disabled"] == ["hermes_session", "retired_tool"] and stored["custom_sibling"] == 42
    assert client.put("/api/tools", json={"disabled_tools": ["hermes_chat"]}, headers=revision_headers(client, None, read_path='/api/tools')).status_code == 200
    assert (await store_env.get("tools"))["disabled_operations"] == response.json()["disabled_operations"]


async def test_canonical_editor_readback_can_clear_legacy_padded_deny(store_env, monkeypatch):
    from n8n_mcp_admin import tool_registry as n8n
    async def no_refresh(): pass
    monkeypatch.setattr(n8n, 'refresh', no_refresh)
    await store_env.patch('tools', {'disabled_operations': {'n8n_executions': [' DELETE '], 'retired': [' RAW ']}})
    client = logged_in(application(n8n))
    data = client.get('/api/tools').json()
    assert data['disabled_operations']['n8n_executions'] == [' DELETE ']
    assert data['editable_disabled_operations'] == {'n8n_executions': ['delete'], 'retired': [' RAW ']}
    call = {'method': 'tools/call', 'params': {'name': 'n8n_executions', 'arguments': {'action': 'delete'}}}
    assert disabled_operation_calls(call, await store_env.get('tools'), n8n)
    edited = data['editable_disabled_operations']
    edited.pop('n8n_executions')
    assert client.put('/api/tools/operations', json={'disabled_operations': edited}, headers=revision_headers(client, None, read_path='/api/tools')).status_code == 200
    assert not disabled_operation_calls(call, await store_env.get('tools'), n8n)
    assert (await store_env.get('tools'))['disabled_operations'] == {'retired': [' RAW ']}


async def test_parallel_tools_and_operations_do_not_restore_stale_siblings(store_env, monkeypatch):
    app = application()
    token = logged_in(app).headers["Authorization"]
    original_get = store_env.get
    from mcp_admin_core.routers import tools as tools_router
    original_transition = tools_router.run_configuration_transition
    ready = asyncio.Event()
    reads = 0

    async def simultaneous_transition(*args, **kwargs):
        nonlocal reads
        reads += 1
        if reads == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), 2)
        return await original_transition(*args, **kwargs)

    # Both legacy requests arrive together; the shared transition now serializes
    # their read/validate/save phases. A barrier inside the lock would deadlock.
    # Neither request may restore the other sibling's previous policy.
    monkeypatch.setattr(tools_router, "run_configuration_transition", simultaneous_transition)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": token}) as client:
        condition = await async_revision_headers(client, read_path='/api/tools')
        writes = [("/api/tools", {"disabled_tools": ["hermes_chat"]}),
                  ("/api/tools/operations", {"disabled_operations": {"hermes_session": ["delete"]}})]
        responses = await asyncio.gather(*(client.put(path, json=body, headers=condition) for path, body in writes))
        assert sorted(response.status_code for response in responses) == [200, 409]
        rejected = next(i for i, response in enumerate(responses) if response.status_code == 409)
        assert responses[rejected].json()['detail']['code'] == 'config_conflict'
        # The other editor explicitly reloads and chooses to apply its independent
        # sibling edit. The original request never silently changes its revision.
        latest = await async_revision_headers(client, read_path='/api/tools')
        response = await client.put(writes[rejected][0], json=writes[rejected][1], headers=latest)
        assert response.status_code == 200
    policy = await original_get("tools")
    assert policy["disabled"] == ["hermes_chat"]
    assert policy["disabled_operations"] == {"hermes_session": ["delete"]}


@pytest.mark.parametrize("path", ["/api/tools/operations", "/api/settings/tools"])
async def test_all_write_routes_validate_real_metadata(path, store_env):
    client = logged_in(application())
    before = await store_env.get("tools")
    for value in [{"hermes_session": ["not-real"]}, {"unknown": ["delete"]}]:
        response = client.put(path, json={"disabled_operations": value}, headers=revision_headers(client))
        assert response.status_code == 400
        assert await store_env.get("tools") == before
    assert client.put(path, json={"disabled_operations": {"hermes_session": ["delete"]}}, headers=revision_headers(client)).status_code == 200


@pytest.mark.parametrize("value", [{"hermes_session": "delete"}, {"hermes_session": [False]}, {"hermes_session": [""]}])
async def test_corrupt_policy_never_reads_as_enabled_or_repairs_itself(value, store_env, monkeypatch):
    client = logged_in(application())
    await store_env.put("tools", {"disabled_operations": value})
    before = store_env.path.read_bytes()
    pm = SimpleNamespace(status=lambda: stopped())
    monkeypatch.setattr(capabilities, "get_process_manager", lambda: pm)
    assert client.get("/api/tools").status_code == 503
    assert client.get("/api/capabilities").status_code == 503
    assert client.put("/api/tools", json={"disabled_tools": []}, headers=revision_headers(client, None, read_path='/api/settings')).status_code == 503
    assert client.put("/api/tools/operations", json={"disabled_operations": {}}, headers=revision_headers(client, None, read_path='/api/settings')).status_code == 503
    assert store_env.path.read_bytes() == before


async def stopped():
    return {"running": False}


def test_snapshot_distinguishes_advertisement_from_allowed_calls():
    observed = [{"name": "hermes_session", "inputSchema": {"type": "object"}},
                {"name": "hermes_config", "inputSchema": {"type": "object"}},
                {"name": "runtime_enum", "inputSchema": {"type": "object", "properties": {"operation": {"enum": ["red", "blue"]}}}}]
    snapshot = capabilities.tool_snapshot(hermes, {"tools": {"disabled_operations": {
        "hermes_session": ["list", "get", "delete"], "hermes_config": ["set"], "retired": ["delete"]}}}, observed, None)
    items = {tool["name"]: tool for tool in snapshot["items"]}
    assert len(observed) == 3 and snapshot["effective_count"] == 2
    assert items["hermes_session"]["inputSchema"] == {"type": "object"}  # still advertised
    assert items["hermes_session"]["policy_enabled"] is False
    assert items["hermes_session"]["tool_policy_enabled"] is True
    assert items["hermes_session"]["effective"] is False
    assert items["hermes_session"]["reason"] == "operation_policy_disabled"
    assert items["hermes_config"]["effective"] is True
    assert items["runtime_enum"]["operations"] == []
    assert items["runtime_enum"]["operation_policy_supported"] is False
    assert items["retired"]["policy_enabled"] is False
    assert snapshot["operation_count"] == 37
    assert snapshot["operation_policy_enabled_count"] == 33
    assert snapshot["policy_enabled_count"] == 11  # 10 catalog + unknown runtime; saved deny excluded
    assert all(operation["write"] is None for tool in snapshot["items"] for operation in tool["operations"])


def test_snapshot_inspect_all_depends_on_component_denies():
    snapshot = capabilities.tool_snapshot(hermes, {"tools": {"disabled_operations": {"hermes_inspect": ["config"]}}}, None, "child_stopped")
    inspect = next(tool for tool in snapshot["items"] if tool["name"] == "hermes_inspect")
    allowed = {operation["name"]: operation["policy_enabled"] for operation in inspect["operations"]}
    assert allowed == {"all": False, "capabilities": True, "config": False, "model": True, "status": True}
    assert inspect["effective"] is None and inspect["policy_enabled"] is True


async def test_n8n_native_sync_kept_and_proxy_remains_applied_when_child_stopped(store_env, monkeypatch):
    from n8n_mcp_admin import tool_registry as n8n
    from mcp_admin_core.routers import tools
    monkeypatch.setattr(n8n, "_loaded", True)
    calls = []

    async def restart():
        calls.append(await store_env.get("tools"))
        return False

    monkeypatch.setattr(tools, "get_process_manager", lambda: SimpleNamespace(start_or_restart=restart))
    client = logged_in(application(n8n, sync=True))
    response = client.put("/api/tools/operations", json={"disabled_operations": {"n8n_manage_datatable": ["deleteRows"]}}, headers=revision_headers(client, read_path='/api/tools'))
    assert response.status_code == 200
    assert response.json()["apply_state"] == "proxy_policy_applied"
    assert response.json()["child_apply_state"] == "pending"
    assert calls[0]["disabled_operations"] == {"n8n_manage_datatable": ["deleterows"]}
