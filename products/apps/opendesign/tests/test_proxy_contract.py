"""Real OpenDesign child and shared proxy with synthetic state and backend."""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import mcp_admin_core.config.store as store_mod
import mcp_admin_core.proxy as proxy_mod
import pytest
import uvicorn
from fastapi.testclient import TestClient
from mcp_admin_core import create_app
from opendesign_mcp_admin import tool_registry
from opendesign_mcp_server import launcher, od_mcp_server

_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
_INIT = {"protocolVersion": "2025-03-26", "capabilities": {},
         "clientInfo": {"name": "synthetic", "version": "0"}}


class _Child:
    def __init__(self, app):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("synthetic child did not start")
            time.sleep(0.05)
        return self

    def __exit__(self, *_exc):
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture()
def proxy_factory(tmp_path, monkeypatch):
    monkeypatch.setattr(proxy_mod, "_client", None)

    def factory(port):
        config = json.loads(json.dumps(store_mod._DEFAULT_CONFIG))
        config.update({"mcp_auth_token": "synthetic-token",
                       "mcp_server": {"command": "", "args": [], "port": port, "env": {}}})
        path = tmp_path / "config.json"
        path.write_text(json.dumps(config))
        monkeypatch.setenv("MCP_ADMIN_CONFIG", str(path))
        monkeypatch.setattr(store_mod, "_instance", None)
        store_mod.get_config_store()
        return TestClient(create_app(title="synthetic OpenDesign", tool_registry=tool_registry))

    yield factory
    monkeypatch.setattr(proxy_mod, "_client", None)


def _rpc(proxy, method, params, request_id):
    response = proxy.post("/private_synthetic-token/mcp", headers=_HEADERS, json={
        "jsonrpc": "2.0", "id": request_id, "method": method, "params": params,
    })
    assert response.status_code == 200
    if "event-stream" in response.headers.get("content-type", ""):
        messages = [json.loads(line[5:]) for line in response.text.splitlines()
                    if line.startswith("data:") and line[5:].strip()]
        return messages[-1]
    return response.json()


def test_open_design_strict_schema_and_refusal_survive_the_proxy(proxy_factory, monkeypatch):
    reads = []

    def fake_get(path):
        reads.append(path)
        return {"ok": True}

    monkeypatch.setattr(od_mcp_server, "_api_get", fake_get)
    fixture_path = Path(__file__).parent / "fixtures/ha-0.2.15-input-schemas.json"
    expected = json.loads(fixture_path.read_text())["input_schemas"]

    with _Child(launcher.build_app()) as child, proxy_factory(child.port) as proxy:
        _rpc(proxy, "initialize", _INIT, 1)
        listed = _rpc(proxy, "tools/list", {}, 2)["result"]["tools"]
        assert {tool["name"]: tool["inputSchema"] for tool in listed} == expected

        marker = "SYNTHETIC_PRIVATE_MARKER"
        refused = _rpc(proxy, "tools/call", {
            "name": "health", "arguments": {"acceptance_extra": marker},
        }, 3)
        assert refused["error"]["code"] == -32602
        assert refused["error"]["message"] == "Invalid params"
        assert marker not in json.dumps(refused)
        assert reads == []

        good = _rpc(proxy, "tools/call", {"name": "health", "arguments": {}}, 4)
        assert good["result"]["isError"] is False
        assert reads == ["/api/health"]
