"""能力 v1：認證、聯集、未知狀態、HTTP/SSE 分頁與九產品組裝。"""
import importlib
import json
from enum import Enum
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from mcp_admin_core.app import create_app
from mcp_admin_core import capabilities as cap
from mcp_admin_core.operation_policy import OperationSpec


class Category(Enum):
    READ = "Read"


def registry():
    return SimpleNamespace(OPERATION_POLICIES={name: OperationSpec(("read", "write"), "action", "read") for name in ("listed", "disabled", "absent")}, TOOL_REGISTRY=[SimpleNamespace(name=n, description=f"catalog {n}",
        category=Category.READ, operations=["read", "write"], dangerous=False) for n in ("listed", "disabled", "absent")])


class PM:
    def __init__(self, running=True):
        self.running = running

    async def status(self):
        return {"running": self.running, "pid": 123 if self.running else None}


def client(monkeypatch, store_env, running=True):
    monkeypatch.setattr(cap, "get_process_manager", lambda: PM(running))
    c = TestClient(create_app("Test", product_id="test", tool_registry=registry()))
    token = c.post("/api/auth/login", json={"password": "admin"}).json()["token"]
    c.headers["Authorization"] = f"Bearer {token}"
    return c


def tool(name):
    return {"name": name, "description": f"runtime {name}",
            "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}}


async def test_union_schemas_policy_and_no_mutation(monkeypatch, store_env):
    async def discover(config):
        return [tool("listed"), tool("disabled"), tool("runtime_only")]
    monkeypatch.setattr(cap, "discover_tools", discover)
    await store_env.put("tools", {"disabled": ["disabled", "retired_deny"], "disabled_operations": {"listed": ["write"]}})
    c = client(monkeypatch, store_env)
    before = store_env.path.read_bytes()
    response = c.get("/api/capabilities")
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == 1
    assert body["admin"]["authority"] == "paas-password"
    assert body["health"] == {"management": "ready", "child": "running", "backend": "unknown", "public_endpoint": "unverified"}
    assert body["tools"]["catalog_count"] == 5
    assert body["tools"]["effective_count"] == 2
    items = {t["name"]: t for t in body["tools"]["items"]}
    assert items["listed"]["inputSchema"] == tool("listed")["inputSchema"]
    assert items["listed"]["disabled_operations"] == ["write"]
    assert items["listed"]["operations"] == [
        {"name": "read", "write": None, "policy_enabled": True},
        {"name": "write", "write": None, "policy_enabled": False}]
    assert body["tools"]["policy_enabled_count"] == 3
    assert items["disabled"]["reason"] == "policy_disabled"
    assert items["disabled"]["effective"] is False
    assert items["absent"]["policy_enabled"] is True
    assert items["absent"]["reason"] == "not_advertised_by_child"
    assert "inputSchema" not in items["absent"]
    assert items["runtime_only"]["runtime_only"] is True
    assert items["retired_deny"]["policy_enabled"] is False
    assert store_env.path.read_bytes() == before


@pytest.mark.parametrize("running", [True, False])
def test_unavailable_is_unknown_not_empty_or_enabled(monkeypatch, store_env, running):
    async def discover(config):
        raise RuntimeError("upstream secret must not leak")
    monkeypatch.setattr(cap, "discover_tools", discover)
    body = client(monkeypatch, store_env, running).get("/api/capabilities").json()
    assert body["tools"]["status"] == "unknown"
    assert body["tools"]["effective_count"] is None
    assert all(t["effective"] is None for t in body["tools"]["items"])
    assert body["tools"]["reason"] == ("child_probe_failed" if running else "child_stopped")
    assert "secret" not in json.dumps(body)


def test_requires_admin_auth(monkeypatch, store_env):
    c = client(monkeypatch, store_env, False)
    c.headers.clear()
    c.cookies.clear()
    assert c.get("/api/capabilities").status_code == 401
    assert c.get("/api/capabilities", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_empty_observation_is_zero(monkeypatch, store_env):
    async def discover(config):
        return []
    monkeypatch.setattr(cap, "discover_tools", discover)
    tools = client(monkeypatch, store_env).get("/api/capabilities").json()["tools"]
    assert tools["status"] == "observed" and tools["effective_count"] == 0
    assert all(t["effective"] is False for t in tools["items"])


def test_concurrent_policy_change_invalidates_observation(monkeypatch, store_env):
    async def discover(config):
        await store_env.put("tools", {"disabled": ["listed"]})
        return [tool("listed")]
    monkeypatch.setattr(cap, "discover_tools", discover)
    tools = client(monkeypatch, store_env).get("/api/capabilities").json()["tools"]
    assert tools["effective_count"] is None
    assert tools["reason"] == "state_changed_during_probe"


# apps/odoo-manage 已封存且移出 uv workspace（#78：它的 mcp-server-odoo 0.7.1 要求 mcp<2），不再測試。
@pytest.mark.parametrize("product", ["hermes", "k8s", "litellm", "n8n", "nextcloud", "odoo", "opendesign", "vk"])
def test_all_apps_explicit_identity_and_capabilities(product, monkeypatch, store_env):
    monkeypatch.setattr(cap, "get_process_manager", lambda: PM(False))
    app = importlib.import_module(product.replace("-", "_") + "_mcp_admin.main").app
    c = TestClient(app)
    token = c.post("/api/auth/login", json={"password": "admin"}).json()["token"]
    response = c.get("/api/capabilities", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    body = response.json()
    assert body["product"] == {"id": product, "version": "1.0.0"}
    assert body["deployment"] == {"kind": "podman"}
    assert body["backend"]["can_edit"] is True  # Standalone OpenDesign is editable; builtin is separate.
    assert body["capabilities"]["operation_policy"] is (product in {"hermes", "n8n", "litellm", "nextcloud", "odoo"})
    assert body["capabilities"]["logs"] is True
    assert body["tools"]["catalog_count"] > 0


@pytest.mark.parametrize("sse", [False, True])
async def test_protocol_pages_bearer_session_cleanup(sse):
    requests = []
    def handler(request):
        requests.append(request)
        assert request.url.host == "127.0.0.1" and request.url.port == 8888
        assert request.headers["authorization"] == "Bearer child-secret"
        if request.method == "DELETE":
            assert request.headers["mcp-session-id"] == "test-session"
            return httpx.Response(204)
        body = json.loads(request.content)
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        if body["method"] == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                "result": {"protocolVersion": "2025-03-26"}}, headers={"Mcp-Session-Id": "test-session"})
        assert body["method"] == "tools/list"
        assert request.headers["mcp-session-id"] == "test-session"
        assert request.headers["mcp-protocol-version"] == "2025-03-26"
        result = {"tools": [tool("second")]} if body["params"].get("cursor") else {"tools": [tool("first")], "nextCursor": "page2"}
        message = {"jsonrpc": "2.0", "id": body["id"], "result": result}
        if sse:
            text = 'data: {"jsonrpc":"2.0","method":"notifications/progress"}\r\n\r\n'
            text += "event: message\r\ndata: " + json.dumps(message, indent=2).replace("\n", "\r\ndata: ") + "\r\n\r\n"
            return httpx.Response(200, text=text, headers={"Content-Type": "text/event-stream"})
        return httpx.Response(200, json=message)
    result = await cap.discover_tools({"mcp_server": {"port": 8888}, "proxy": {"bearer_token": "child-secret"}},
                                      transport=httpx.MockTransport(handler))
    assert [t["name"] for t in result] == ["first", "second"]
    assert len(requests) == 5 and requests[-1].method == "DELETE"


@pytest.mark.parametrize("failure", ["rpc", "http", "malformed", "cursor", "schema", "size"])
async def test_incomplete_discovery_fails_and_releases_session(failure, monkeypatch):
    deleted = []
    if failure == "size":
        monkeypatch.setattr(cap, "MAX_RESPONSE_BYTES", 200)
    def handler(request):
        if request.method == "DELETE":
            deleted.append(True)
            return httpx.Response(204)
        body = json.loads(request.content)
        if body["method"] == "initialize":
            return httpx.Response(200, json={"id": 1, "result": {"protocolVersion": "2025-03-26"}},
                                  headers={"mcp-session-id": "test"})
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        if failure == "http":
            return httpx.Response(403)
        if failure == "malformed":
            return httpx.Response(200, text="invalid")
        if failure == "rpc":
            return httpx.Response(200, json={"id": body["id"], "error": {"message": "no"}})
        result = {"tools": [tool("tool")], "nextCursor": "same"}
        if failure == "schema":
            result["tools"] = [{"name": "no-schema"}]
        return httpx.Response(200, json={"id": body["id"], "result": result})
    with pytest.raises((cap.DiscoveryError, httpx.HTTPStatusError, ValueError)):
        await cap.discover_tools({}, transport=httpx.MockTransport(handler))
    assert deleted == [True]


async def test_real_mcp_child_list_without_tool_calls():
    from test_proxy_disabled_tools import _Upstream, _mcp_server
    from mcp_admin_core.mcp_sse_wrapper import DualTransportApp
    calls = []
    with _Upstream(DualTransportApp(_mcp_server(calls))) as upstream:
        result = await cap.discover_tools({"mcp_server": {"port": upstream.port}})
    assert {t["name"] for t in result} == {"alpha", "beta"}
    assert all(t["inputSchema"]["type"] == "object" for t in result)
    assert calls == []


async def test_cancelled_registry_refresh_reaps_subprocess(monkeypatch):
    import asyncio
    from n8n_mcp_admin import tool_registry
    calls = []
    started = asyncio.Event()
    class Process:
        returncode = None
        async def communicate(self):
            started.set()
            await asyncio.Event().wait()
        def kill(self):
            calls.append("kill")
        async def wait(self):
            calls.append("wait")
    async def spawn(*args, **kwargs):
        return Process()
    monkeypatch.setattr(tool_registry.upstream, "package_dir", lambda: "/fixture")
    monkeypatch.setattr(tool_registry.asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(tool_registry.load_upstream_catalog())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == ["kill", "wait"]
