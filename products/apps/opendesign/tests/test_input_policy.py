"""OpenDesign child MCP calls against a synthetic, recording daemon only."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from opendesign_mcp_server import input_policy, launcher, od_mcp_server
from starlette.testclient import TestClient

PROJECT = "38C010DC-F7C8-4A35-A325-89D6F56E45A4"
HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "mcp-protocol-version": "2025-03-26",
}


def call(client: TestClient, name: str, arguments: dict) -> dict:
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 7, "method": "tools/call",
              "params": {"name": name, "arguments": arguments}},
        headers=HEADERS,
    )
    assert response.status_code == 200
    messages = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]
    return (messages[-1] if messages else response.json())["result"]


@pytest.fixture()
def fake_daemon(monkeypatch):
    events: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        events.append((request.method, request.url.path))
        if request.url.path.endswith("/files/資料/說明.txt"):
            return httpx.Response(200, text="中文檔案\n")
        if request.url.path.endswith("/files"):
            return httpx.Response(200, json={"files": [
                {"path": "資料/說明.txt", "name": "說明.txt", "type": "file", "size": 13},
            ]})
        return httpx.Response(200, json={"id": PROJECT})

    daemon = httpx.Client(base_url="http://synthetic.invalid", transport=httpx.MockTransport(handler))
    monkeypatch.setattr(od_mcp_server, "_client", daemon)

    def fake_chat(path: str, data: dict) -> dict:
        events.append(("POST", path))
        return {"projectId": data["projectId"], "status": "succeeded"}

    monkeypatch.setattr(od_mcp_server, "_api_post_sse", fake_chat)
    with TestClient(launcher.build_app()) as client:
        yield client, events
    daemon.close()


@pytest.mark.parametrize("path", [
    "../secret", "資料/../secret", "/etc/passwd", "a//b", "a/./b", ".env", "資料/.env",
    "server.pem", "keys/id_rsa", "CONFIG/PRIVATE.KEY", "credentials.json", "a%2e%2e", "a\\b", "a?b",
    "資料／說明.txt", "資料/說明\u202e.txt", "x" * 257,
])
def test_read_file_rejects_without_a_daemon_request(fake_daemon, path):
    client, events = fake_daemon
    result = call(client, "read_file", {"project_id": PROJECT, "file_path": path})
    assert result["isError"] is True
    assert events == []


@pytest.mark.parametrize("name,arguments", [
    ("get_project", {"project_id": "../secret"}),
    ("get_project", {"project_id": ".."}),
    ("get_project", {"project_id": "id%2fsecret"}),
    ("delete_project", {"project_id": "../secret"}),
    ("list_project_files", {"project_id": "../secret"}),
    ("read_file", {"project_id": "../secret", "file_path": "index.html"}),
    ("get_file_info", {"project_id": "../secret", "file_path": "index.html"}),
    ("send_message", {"project_id": "../secret", "prompt": "synthetic"}),
])
def test_bad_project_id_is_rejected_before_daemon_request(fake_daemon, name, arguments):
    client, events = fake_daemon
    result = call(client, name, arguments)
    assert result["isError"] is True
    assert events == []


@pytest.mark.parametrize("path", ["../secret", "/abs", "a//b", "a%2e", "a\\b", "資料/../secret"])
def test_get_file_info_rejects_unsafe_path_before_listing(fake_daemon, path):
    client, events = fake_daemon
    result = call(client, "get_file_info", {"project_id": PROJECT, "file_path": path})
    assert result["isError"] is True
    assert events == []


def test_uppercase_uuid_and_chinese_path_reach_synthetic_daemon_unchanged(fake_daemon):
    client, events = fake_daemon
    project = call(client, "get_project", {"project_id": PROJECT})
    content = call(client, "read_file", {"project_id": PROJECT, "file_path": "資料/說明.txt"})
    info = call(client, "get_file_info", {"project_id": PROJECT, "file_path": "資料/說明.txt"})
    assert project["isError"] is False
    assert content["isError"] is False
    assert "中文檔案" in json.dumps(content, ensure_ascii=False)
    assert info["isError"] is False
    assert events == [
        ("GET", f"/api/projects/{PROJECT}"),
        ("GET", f"/api/projects/{PROJECT}/files/資料/說明.txt"),
        ("GET", f"/api/projects/{PROJECT}/files"),
    ]


def test_send_message_preserves_uppercase_project_id_to_fake_chat(fake_daemon):
    client, events = fake_daemon
    result = call(client, "send_message", {"project_id": PROJECT, "prompt": "synthetic"})
    assert result["isError"] is False
    assert PROJECT in json.dumps(result)
    assert events == [("POST", "/api/chat")]


def test_safe_imported_non_uuid_project_id_remains_usable(fake_daemon):
    client, events = fake_daemon
    imported_id = "imported-project_1"
    result = call(client, "get_project", {"project_id": imported_id})
    assert result["isError"] is False
    assert events == [("GET", f"/api/projects/{imported_id}")]


def test_metadata_lookup_keeps_the_ha_fileinfo_contract(fake_daemon):
    # HA 0.2.13 excludes secret-like names for read_file, not get_file_info.
    client, events = fake_daemon
    result = call(client, "get_file_info", {"project_id": PROJECT, "file_path": ".env"})
    assert result["isError"] is False
    assert events == [("GET", f"/api/projects/{PROJECT}/files")]


def test_guards_are_installed_once_and_keep_the_declared_tool_schema():
    tools = {t.name: t for t in od_mcp_server.mcp._tool_manager.list_tools()}
    assert set(tools) == input_policy.EXPECTED_TOOLS
    assert input_policy.install_input_guards(od_mcp_server.mcp) == 0
    assert {name for name, tool in tools.items() if getattr(tool.fn, "__od_input_guarded__", False)} == (
        input_policy.PROJECT_TOOLS
    )
    assert tools["read_file"].parameters["properties"]["file_path"]["type"] == "string"


def test_new_vendored_tool_fails_closed_until_policy_review():
    existing = od_mcp_server.mcp._tool_manager.list_tools()
    changed = SimpleNamespace(_tool_manager=SimpleNamespace(
        list_tools=lambda: [*existing, SimpleNamespace(name="new_file_tool")],
    ))
    with pytest.raises(RuntimeError, match="tool set changed"):
        input_policy.install_input_guards(changed)
