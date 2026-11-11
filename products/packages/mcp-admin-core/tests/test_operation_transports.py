"""操作政策 HTTP/batch/SSE 整合；真實 Hermes 子行程只連記錄式假 Dashboard。"""

from management_client import revision_headers, async_revision_headers
import asyncio
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.shared.exceptions import MCPError

from hermes_mcp_admin import tool_registry
from mcp_admin_core.app import create_app
from mcp_admin_core.routers.tools import create_tools_router
from test_proxy_disabled_tools import _Upstream, _free_port, _Session, _INIT


def app():
    return create_app("operation-test", tool_registry=tool_registry, extra_routers=[
        create_tools_router(registry=tool_registry, mcp_label="Hermes")])


def login(client):
    token = client.post("/api/auth/login", json={"password": "admin"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def hermes_process(tmp_path):
    port = _free_port()
    audit = tmp_path / "backend.jsonl"
    audit.touch()
    stderr = (tmp_path / "child.log").open("w")
    process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("_operation_transport_child.py")), str(port), str(audit)], stdout=stderr, stderr=stderr)
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=.2).close()
                break
            except OSError:
                if process.poll() is not None or time.monotonic() > deadline:
                    pytest.fail((tmp_path / "child.log").read_text())
                time.sleep(.05)
        yield port, audit
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        stderr.close()


@pytest.fixture
def running_proxy(hermes_process, store_env, monkeypatch):
    import mcp_admin_core.app as app_module
    import mcp_admin_core.proxy as proxy

    class PM:
        async def start(self): pass
        async def stop(self): pass

    monkeypatch.setattr(app_module, "get_process_manager", lambda: PM())
    monkeypatch.setattr(proxy, "_client", None)
    port, audit = hermes_process
    asyncio.run(store_env.update({"mcp_auth_token": "secret", "mcp_server": {"port": port}}))
    with _Upstream(app()) as admin:
        yield f"http://127.0.0.1:{admin.port}", audit
    # Shared httpx belongs to the stopped server loop; don't reuse it elsewhere.
    proxy._client = None


def test_real_hermes_streamable_http_policy_applies_without_restart(running_proxy):
    url, audit = running_proxy
    with httpx.Client(base_url=url, timeout=5) as client:
        auth = login(client)
        session = _Session(client)
        session.call("initialize", _INIT)
        assert "hermes_session" in session.tools()
        assert session.call("tools/call", {"name": "hermes_session", "arguments": {}})["result"]["isError"] is False
        before = audit.read_text()
        saved = client.put("/api/tools/operations", json={"disabled_operations": {"hermes_session": ["delete"]}}, headers=revision_headers(client, auth, read_path='/api/tools'))
        assert saved.status_code == 200 and saved.json()["apply_state"] == "proxy_policy_applied"
        for arguments in [{"action": "delete", "session_id": "fixture"}, {"action": []}, {"action": "unknown"}]:
            result = session.call("tools/call", {"name": "hermes_session", "arguments": arguments})
            assert result["result"]["isError"] is True
            assert "Operation blocked by administrator policy" in result["result"]["content"][0]["text"]
        assert audit.read_text() == before
        assert session.call("tools/call", {"name": "hermes_session", "arguments": {}})["result"]["isError"] is False
        client.put("/api/tools/operations", json={"disabled_operations": {}}, headers=revision_headers(client, auth, read_path='/api/tools'))
        before_conflict = len(audit.read_text().splitlines())
        conflict = session.call("tools/call", {"name": "hermes_session", "arguments": {
            "action": "delete", "session_id": "fixture", "request_id": "owned-stale",
            "expected_revision": 6, "expected_generation": 3}})
        assert conflict["result"]["isError"] is True
        assert "409" in conflict["result"]["content"][0]["text"]
        refused = [json.loads(row) for row in audit.read_text().splitlines()[before_conflict:]]
        assert [row["method"] for row in refused] == ["GET", "DELETE"]
        assert all(row["session_exists"] and not row["committed"] for row in refused)
        before_delete = len(audit.read_text().splitlines())
        assert session.call("tools/call", {"name": "hermes_session", "arguments": {"action": "delete", "session_id": "fixture"}})["result"]["isError"] is False
        deleted = [json.loads(row) for row in audit.read_text().splitlines()[before_delete:]]
        assert [row["method"] for row in deleted] == ["GET", "DELETE", "GET"]
        assert [row["session_exists"] for row in deleted] == [True, False, False]
        assert [row["committed"] for row in deleted] == [False, True, False]
        identity = deleted[1]["params"]
        assert set(identity) == {"request_id", "expected_revision", "expected_generation"}
        assert identity["expected_revision"] == "7" and identity["expected_generation"] == "3"
        replay = session.call("tools/call", {"name": "hermes_session", "arguments": {
            "action": "delete", "session_id": "fixture", "request_id": identity["request_id"],
            "expected_revision": 7, "expected_generation": 3}})
        assert replay["result"]["isError"] is False
        assert sum(json.loads(row)["committed"] for row in audit.read_text().splitlines()) == 1


async def test_real_legacy_sse_denied_operation_never_dispatches_and_read_survives(running_proxy):
    url, audit = running_proxy
    async with httpx.AsyncClient(base_url=url, timeout=5) as admin:
        token = (await admin.post("/api/auth/login", json={"password": "admin"})).json()["token"]
        auth = {"Authorization": f"Bearer {token}"}
        async with sse_client(url + "/private_secret/sse", timeout=5) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                assert not (await session.call_tool("hermes_session", {})).is_error
                response = await admin.put("/api/tools/operations", json={"disabled_operations": {"hermes_session": ["delete"]}}, headers=await async_revision_headers(admin, auth, read_path='/api/tools'))
                assert response.status_code == 200
                before = audit.read_text()
                # The POST must acknowledge on the SSE transport, then the
                # actual child returns an unknown-tool error through the GET.
                try:
                    result = await asyncio.wait_for(session.call_tool("hermes_session", {"action": "delete", "session_id": "fixture"}), 5)
                    assert result.is_error
                except MCPError as error:
                    assert error.code in {-32602, -32603}
                assert audit.read_text() == before
                assert not (await session.call_tool("hermes_session", {})).is_error
                assert len(audit.read_text().splitlines()) == len(before.splitlines()) + 1


def test_json_mixed_batch_rewrites_only_denied_calls(store_env, monkeypatch):
    from mcp_admin_core import proxy
    from test_operation_policy import call
    asyncio.run(store_env.put("mcp_auth_token", "secret"))
    asyncio.run(store_env.patch("tools", {"disabled_operations": {"hermes_session": ["delete"]}}))
    received, executed = [], []

    def child(request):
        batch = json.loads(request.content)
        received.append(batch)
        results = []
        for message in batch:
            if message.get("method") == "tools/call":
                name = message["params"]["name"]
                if name:
                    executed.append((name, message["params"]["arguments"]))
                    result = {"result": {"content": []}}
                else:
                    result = {"error": {"code": -32602, "message": "Unknown tool"}}
                results.append({"jsonrpc": "2.0", "id": message["id"], **result})
        return httpx.Response(200, json=results)

    monkeypatch.setattr(proxy, "_get_client", lambda *_: httpx.AsyncClient(transport=httpx.MockTransport(child)))
    client = TestClient(app())
    batch = [call("hermes_session", {"action": "delete"}, "denied"), call("hermes_session", {}, "allowed"), {"jsonrpc": "2.0", "method": "notifications/initialized"}]
    response = client.post("/private_secret/mcp", json=batch)
    assert response.status_code == 200
    assert response.json()[0]["id"] == "denied" and "error" in response.json()[0]
    assert response.json()[1]["id"] == "allowed" and "result" in response.json()[1]
    assert executed == [("hermes_session", {})]
    assert received[0][0]["params"]["name"] == ""
    assert received[0][1:] == batch[1:]
    # An all-denied HTTP batch never reaches the child.
    before = len(received)
    result = client.post("/private_secret/mcp", json=[batch[0]]).json()
    assert result[0]["result"]["isError"] is True and len(received) == before
