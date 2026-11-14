"""Tools 頁變更只影響選定的 Nextcloud profile，包含掛載路徑。"""
from __future__ import annotations

import asyncio
import copy
import json
import shutil
import socket

import httpx
import pytest
from fastmcp import Client
from starlette.applications import Starlette
from starlette.routing import Mount

from mcp_admin_core.config.store import _DEFAULT_CONFIG
from mcp_admin_core.owner_defaults import APPLIED_FRESH, MARKER, SOURCE_NEW_CONFIG, marker_value
from mcp_admin_core.profiles import get_profile_registry
from nextcloud_mcp_admin.main import app

pytestmark = pytest.mark.skipif(shutil.which("nextcloud-mcp-server") is None,
                                reason="nextcloud-mcp-server is not installed")

_TOOL = "create_text_file"
_PROFILE = "0123456789abcdef01234567"
_TOKENS = {"default": "synthetic-default-token", _PROFILE: "synthetic-second-token"}
_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _payload(response):
    text = response.text
    if text.startswith(("event:", "data:")):
        text = next(line[5:].strip() for line in text.splitlines() if line.startswith("data:"))
    return json.loads(text)


async def _wait_tools(port):
    deadline = asyncio.get_running_loop().time() + 30
    last = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            async with Client(f"http://127.0.0.1:{port}/mcp") as client:
                return {tool.name for tool in await client.list_tools()}
        except Exception as exc:  # startup can take several seconds
            last = exc
            await asyncio.sleep(.2)
    raise AssertionError(f"synthetic child on port {port} did not start: {last!r}")


async def _initialize(client, prefix, token):
    path = f"{prefix}/private_{token}/mcp"
    response = await client.post(path, headers=_HEADERS, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "policy-profile-test", "version": "1"}}})
    assert response.status_code == 200, response.text
    session = response.headers["mcp-session-id"]
    assert not session.startswith("woow-mcp-setup-")
    response = await client.post(path, headers={**_HEADERS, "Mcp-Session-Id": session}, json={
        "jsonrpc": "2.0", "method": "notifications/initialized"})
    assert response.status_code in (200, 202, 204)
    return session


async def _rpc(client, prefix, token, session, method, params=None):
    return await client.post(f"{prefix}/private_{token}/mcp",
                             headers={**_HEADERS, "Mcp-Session-Id": session},
                             json={"jsonrpc": "2.0", "id": 2, "method": method, "params": params or {}})


async def _names(client, prefix, token, session):
    response = await _rpc(client, prefix, token, session, "tools/list")
    assert response.status_code == 200, response.text
    return {tool["name"] for tool in _payload(response)["result"]["tools"]}


@pytest.mark.parametrize(("mode", "selected"), [("root_path", _PROFILE), ("mount", "default")])
async def test_policy_update_only_restarts_selected_real_child(tmp_path, monkeypatch, mode, selected):
    import mcp_admin_core.config.store as store_mod
    import mcp_admin_core.process as process_mod
    import mcp_admin_core.proxy as proxy

    ports = {_port(), _port()}
    while len(ports) < 2:
        ports.add(_port())
    default_port, second_port = sorted(ports)
    configs = []
    for token, port in ((_TOKENS["default"], default_port), (_TOKENS[_PROFILE], second_port)):
        config = copy.deepcopy(_DEFAULT_CONFIG)
        config.update({"mcp_auth_token": token,
                       "connection": {"base_url": "https://nextcloud.invalid", "username": "synthetic",
                                      "app_password": "synthetic-only"},
                       "mcp_server": {"command": "nextcloud-mcp-server", "args": ["--port", str(port)],
                                      "port": port, "env": {}, "env_prefix": "NEXTCLOUD_MCP_"}})
        config["tools"] = {"disabled": [_TOOL], "nextcloud_policy_seeded": True,
                           MARKER: marker_value(APPLIED_FRESH, SOURCE_NEW_CONFIG)}
        configs.append(config)
    configs[0]["profile_index"] = {_PROFILE: {"label": "Second", "port": second_port}}
    root = tmp_path / "config.json"
    child = tmp_path / "profiles" / _PROFILE / "config.json"
    child.parent.mkdir(parents=True, mode=0o700)
    for path, config in ((root, configs[0]), (child, configs[1])):
        path.write_text(json.dumps(config)); path.chmod(0o600)
    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(root))
    monkeypatch.setattr(store_mod, "_instance", None)
    monkeypatch.setattr(process_mod, "_instance", None)
    monkeypatch.setattr(proxy, "_client", None)
    registry = get_profile_registry()
    sibling = "default" if selected == _PROFILE else _PROFILE
    managers = {profile: registry.manager_for(profile) for profile in ("default", _PROFILE)}
    if mode == "root_path":
        transport = httpx.ASGITransport(app=app, root_path="/gateway")
    else:
        transport = httpx.ASGITransport(app=Starlette(routes=[Mount("/gateway", app=app)]))
    prefix = "/gateway"
    try:
        for profile in ("default", _PROFILE):
            with registry.activate(profile):
                assert await managers[profile].start()
        for port in (default_port, second_port):
            assert _TOOL not in await _wait_tools(port)
        initial_pids = {profile: manager.child_pid for profile, manager in managers.items()}
        assert all(initial_pids.values()) and initial_pids["default"] != initial_pids[_PROFILE]
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            login = await client.post(prefix + "/api/auth/login", json={"password": "admin"})
            assert login.status_code == 200
            client.headers["Authorization"] = "Bearer " + login.json()["token"]
            sessions = {profile: await _initialize(client, prefix, token) for profile, token in _TOKENS.items()}
            for profile, token in _TOKENS.items():
                assert _TOOL not in await _names(client, prefix, token, sessions[profile])

            async def update(disabled):
                headers = {"X-MCP-Profile": selected}
                before = await client.get(prefix + "/api/tools", headers=headers)
                assert before.status_code == 200
                response = await client.put(prefix + "/api/tools", headers={**headers, "If-Match": before.headers["etag"]},
                                            json={"disabled_tools": disabled})
                assert response.status_code == 200, response.text

            await update([])
            selected_pid = managers[selected].child_pid
            assert selected_pid and selected_pid != initial_pids[selected]
            assert managers[sibling].child_pid == initial_pids[sibling]
            selected_port = default_port if selected == "default" else second_port
            assert _TOOL in await _wait_tools(selected_port)
            assert _TOOL not in await _names(client, prefix, _TOKENS[sibling], sessions[sibling])
            assert (await _rpc(client, prefix, _TOKENS[selected], sessions[selected], "tools/list")).status_code == 404
            sessions[selected] = await _initialize(client, prefix, _TOKENS[selected])
            assert _TOOL in await _names(client, prefix, _TOKENS[selected], sessions[selected])

            await update([_TOOL])
            assert managers[selected].child_pid == selected_pid
            assert managers[sibling].child_pid == initial_pids[sibling]
            assert _TOOL not in await _names(client, prefix, _TOKENS[selected], sessions[selected])
            denied = await _rpc(client, prefix, _TOKENS[selected], sessions[selected], "tools/call",
                                {"name": _TOOL, "arguments": {}})
            assert denied.status_code == 200 and _payload(denied)["result"]["isError"] is True
            assert _TOOL in await _wait_tools(selected_port)  # child unchanged; proxy enforces the restriction
            assert _TOOL not in await _names(client, prefix, _TOKENS[sibling], sessions[sibling])
            for profile, session in sessions.items():
                await client.delete(f"{prefix}/private_{_TOKENS[profile]}/mcp",
                                    headers={**_HEADERS, "Mcp-Session-Id": session})
    finally:
        if proxy._client is not None:
            await proxy._client.aclose()
        for profile in (_PROFILE, "default"):
            with registry.activate(profile):
                await managers[profile].stop()
