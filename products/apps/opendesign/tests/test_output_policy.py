"""MCP-wire checks against a synthetic OpenDesign daemon, with no real data."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from opendesign_mcp_server import launcher, od_mcp_server, output_policy
from starlette.testclient import TestClient

HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "mcp-protocol-version": "2025-03-26",
}


def call(client: TestClient, name: str) -> dict:
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 8, "method": "tools/call",
              "params": {"name": name, "arguments": {}}},
        headers=HEADERS,
    )
    assert response.status_code == 200
    messages = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]
    return (messages[-1] if messages else response.json())["result"]


def test_metadata_tools_return_ha_compatible_safe_payload(monkeypatch):
    events = []
    connector = {
        "id": "demo", "name": "Demo\u202e connector", "provider": "demo",
        "category": "external", "status": "ready", "auth": {
            "configured": True, "token": "SYNTHETIC_SECRET_MARKER"},
        "url": "http://synthetic-internal.invalid", "cwd": "/tmp/synthetic",
        "tools": [{"name": "list_records", "description": "SYNTHETIC_SECRET_MARKER"}],
    }
    run = {
        "id": "run-1", "projectId": "project-1", "status": "running",
        "error": "SYNTHETIC_SECRET_MARKER", "cwd": "/tmp/synthetic",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        events.append((request.method, request.url.path))
        if request.url.path == "/api/connectors":
            return httpx.Response(200, json={"connectors": [connector]})
        if request.url.path == "/api/runs":
            return httpx.Response(200, json={"runs": [run]})
        return httpx.Response(404)

    daemon = httpx.Client(base_url="http://synthetic.invalid", transport=httpx.MockTransport(handler))
    monkeypatch.setattr(od_mcp_server, "_client", daemon)
    try:
        with TestClient(launcher.build_app()) as client:
            connectors = call(client, "list_connectors")
            runs = call(client, "list_runs")
    finally:
        daemon.close()

    assert connectors["isError"] is False and runs["isError"] is False
    assert json.loads(connectors["content"][0]["text"]) == {
        "total": 1, "connectors": [{
            "id": "demo", "name": "Demo connector", "provider": "demo",
            "category": "external", "status": "ready", "configured": True,
            "toolCount": 1, "tools": ["list_records"],
        }], "truncated": False,
    }
    assert json.loads(runs["content"][0]["text"]) == {
        "runs": [{"id": "run-1", "projectId": "project-1", "status": "running"}],
        "truncated": False,
    }
    wire = json.dumps([connectors, runs])
    assert "SYNTHETIC_SECRET_MARKER" not in wire
    assert "synthetic-internal.invalid" not in wire and "/tmp/synthetic" not in wire
    assert events == [("GET", "/api/connectors"), ("GET", "/api/runs")]


def test_bounds_and_malformed_metadata_are_fail_closed():
    connectors = output_policy.project_connectors({"connectors": [
        {"id": f"c{i}", "name": "x" * 500,
         "tools": [{"name": f"tool{j}"} for j in range(60)]}
        for i in range(301)
    ]})
    assert connectors["total"] == 301 and len(connectors["connectors"]) == 300
    assert connectors["truncated"] is True
    assert len(connectors["connectors"][0]["name"]) == 128
    assert len(connectors["connectors"][0]["tools"]) == 50
    assert connectors["connectors"][0]["toolsTruncated"] is True

    runs = output_policy.run_rows({"runs": [
        {"id": "x" * 500, "projectId": "p", "status": "running", "error": "never return"}
        for _ in range(101)
    ]})
    assert len(runs["runs"]) == 100 and runs["truncated"] is True
    assert len(runs["runs"][0]["id"]) == 256
    assert "error" not in runs["runs"][0]
    with pytest.raises(ValueError, match="unexpected connector list"):
        output_policy.project_connectors({"connectors": {"auth": "bad"}})
    with pytest.raises(ValueError, match="unexpected metadata envelope"):
        output_policy.run_rows({"runs": "bad"})


def test_malformed_daemon_metadata_is_an_mcp_error_without_raw_payload(monkeypatch):
    events = []

    def handler(request: httpx.Request) -> httpx.Response:
        events.append((request.method, request.url.path))
        return httpx.Response(200, json={"connectors": {
            "token": "SYNTHETIC_SECRET_MARKER"}})

    daemon = httpx.Client(base_url="http://synthetic.invalid", transport=httpx.MockTransport(handler))
    monkeypatch.setattr(od_mcp_server, "_client", daemon)
    try:
        with TestClient(launcher.build_app()) as client:
            result = call(client, "list_connectors")
    finally:
        daemon.close()
    assert result["isError"] is True
    assert "BACKEND_INVALID_RESPONSE" in json.dumps(result)
    assert "SYNTHETIC_SECRET_MARKER" not in json.dumps(result)
    assert events == [("GET", "/api/connectors")]


@pytest.mark.parametrize("failure,expected", [
    ("status", "BACKEND_HTTP_ERROR status=503"),
    ("connect", "BACKEND_UNAVAILABLE"),
    ("timeout", "BACKEND_TIMEOUT"),
])
def test_backend_errors_use_fixed_public_codes(monkeypatch, failure, expected):
    events = []

    def handler(request: httpx.Request) -> httpx.Response:
        events.append((request.method, request.url.path))
        if failure == "status":
            return httpx.Response(503, text="SYNTHETIC_SECRET_MARKER")
        if failure == "connect":
            raise httpx.ConnectError("SYNTHETIC_SECRET_MARKER")
        raise httpx.ReadTimeout("SYNTHETIC_SECRET_MARKER")

    daemon = httpx.Client(base_url="http://synthetic.invalid", transport=httpx.MockTransport(handler))
    monkeypatch.setattr(od_mcp_server, "_client", daemon)
    try:
        with TestClient(launcher.build_app()) as client:
            result = call(client, "list_runs")
    finally:
        daemon.close()
    text = json.dumps(result)
    assert result["isError"] is True and expected in text
    assert "SYNTHETIC_SECRET_MARKER" not in text
    assert "synthetic.invalid" not in text
    assert events == [("GET", "/api/runs")]


def test_projection_install_is_idempotent_and_rejects_signature_change():
    tools = {tool.name: tool for tool in od_mcp_server.mcp._tool_manager.list_tools()}
    assert output_policy.install_output_guards(od_mcp_server.mcp) == 0
    assert all(getattr(tools[name].fn, "__od_output_guarded__", False)
               for name in output_policy.PROJECTORS)
    changed = SimpleNamespace(_tool_manager=SimpleNamespace(list_tools=lambda: [
        SimpleNamespace(name="list_connectors", fn=lambda unexpected: None),
        tools["list_runs"],
    ]))
    with pytest.raises(RuntimeError, match="arguments changed"):
        output_policy.install_output_guards(changed)
