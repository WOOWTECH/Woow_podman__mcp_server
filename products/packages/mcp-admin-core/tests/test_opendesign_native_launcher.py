"""opendesign 原生 launcher 與舊 supergateway seed 遷移（#1369）。

- launcher：同步工具改丟 worker thread（不卡 event loop）、/mcp stateless（不發 session id）、
  tools/list＋tools/call 經原生 Streamable HTTP 可用；Open Design daemon 的錯誤（vendored 檔直接丟 httpx 例外）
  照 mcp 1.x 的文字回給 client（mcp 2.x 預設只回 "Error executing tool X"，#78）；
- seed 遷移：只改寫「supergateway + python -m opendesign_mcp_server」舊形狀，port／env 保留、
  冪等；自訂 command 不動；admin lifespan 在拉起子行程「之前」完成遷移。
"""
from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "mcp-protocol-version": "2025-03-26",
}
_INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
               "clientInfo": {"name": "t", "version": "0"}},
}

LEGACY_SEED = {
    "command": "supergateway",
    "args": ["--stdio", "python -m opendesign_mcp_server", "--outputTransport", "streamableHttp",
             "--streamableHttpPath", "/mcp", "--port", "8000", "--healthEndpoint", "/healthz"],
    "port": 8000,
    "env": {"FOO": "bar"},
    "env_prefix": "",
}


# ---------------------------------------------------------------------------
# launcher
# ---------------------------------------------------------------------------


def test_offload_sync_tools_makes_all_tools_async_and_is_idempotent():
    from mcp.server.mcpserver import MCPServer as FastMCP

    from opendesign_mcp_server.launcher import offload_sync_tools

    server = FastMCP("t")

    @server.tool()
    def add(a: int, b: int) -> int:
        return a + b

    @server.tool()
    async def already_async() -> str:
        return "x"

    assert offload_sync_tools(server) == 1
    assert offload_sync_tools(server) == 0  # 冪等
    assert all(t.is_async for t in server._tool_manager.list_tools())

    import anyio

    # 參數驗證仍由原 fn_metadata 負責、結果不變
    result = anyio.run(lambda: server._tool_manager.get_tool("add").run({"a": 2, "b": "3"}, None))
    assert result == 5


def test_offloaded_tool_runs_in_worker_thread():
    import threading

    import anyio
    from mcp.server.mcpserver import MCPServer as FastMCP

    from opendesign_mcp_server.launcher import offload_sync_tools

    server = FastMCP("t")
    seen: dict = {}

    @server.tool()
    def where() -> str:
        seen["thread"] = threading.current_thread()
        return "ok"

    offload_sync_tools(server)
    anyio.run(lambda: server._tool_manager.get_tool("where").run({}, None))
    assert seen["thread"] is not threading.main_thread()


@pytest.fixture()
def native_client(monkeypatch):
    from opendesign_mcp_server import launcher
    from opendesign_mcp_server import od_mcp_server as srv

    # 不連真 daemon：攔 vendored 的 _api_get（工具函式在呼叫時才查 module global）
    monkeypatch.setattr(srv, "_api_get", lambda path: {"ok": True, "path": path})
    with TestClient(launcher.build_app()) as c:
        yield c


def test_native_mcp_is_stateless_and_serves_tools(native_client):
    r = native_client.post("/mcp", json=_INIT, headers=_HEADERS)
    assert r.status_code == 200
    assert "mcp-session-id" not in {k.lower() for k in r.headers}

    r = native_client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, headers=_HEADERS
    )
    assert r.status_code == 200
    for name in ("health", "list_projects", "send_message", "delete_project"):
        assert f'"name":"{name}"' in r.text

    r = native_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": "health", "arguments": {}}},
        headers=_HEADERS,
    )
    assert r.status_code == 200
    assert "/api/health" in r.text
    assert '"isError":false' in r.text


@pytest.fixture()
def failing_client(monkeypatch):
    """vendored 工具打 Open Design daemon 失敗：連不上、或回 4xx（raise_for_status 的 httpx 例外）。"""
    import httpx

    from opendesign_mcp_server import launcher
    from opendesign_mcp_server import od_mcp_server as srv

    def api_get(path):
        if path == "/api/health":
            raise httpx.ConnectError("[Errno 111] Connection refused")
        request = httpx.Request("GET", "http://open-design-svc:7457" + path)
        raise httpx.HTTPStatusError("Client error '404 Not Found' for url '%s'" % request.url,
                                    request=request, response=httpx.Response(404, request=request))

    monkeypatch.setattr(srv, "_api_get", api_get)
    with TestClient(launcher.build_app()) as c:
        yield c


@pytest.mark.parametrize(("tool", "expected"), [
    ("health", "Error executing tool health: [Errno 111] Connection refused"),
    ("list_projects", "Error executing tool list_projects: Client error '404 Not Found' for url "
                      "'http://open-design-svc:7457/api/projects'"),
])
def test_backend_error_text_reaches_the_client(failing_client, tool, expected):
    r = failing_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": tool, "arguments": {}}},
        headers=_HEADERS,
    )
    assert r.status_code == 200
    data = [line[5:].strip() for line in r.text.splitlines() if line.startswith("data:")]
    result = json.loads(data[-1] if data else r.text)["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == expected


def test_bare_mcp_path_no_redirect(native_client):
    # 裸 /mcp 不得 307（Location 會洩漏 loopback upstream）
    r = native_client.post("/mcp", json=_INIT, headers=_HEADERS, follow_redirects=False)
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# seed 遷移
# ---------------------------------------------------------------------------


def test_is_legacy_supergateway_seed():
    from opendesign_mcp_admin.seed_migration import is_legacy_supergateway_seed

    assert is_legacy_supergateway_seed(LEGACY_SEED) is True
    assert is_legacy_supergateway_seed({"command": "opendesign-mcp-server", "args": []}) is False
    # 別的 supergateway 橋（例如有人手動接 vk 類 binary）不動
    assert is_legacy_supergateway_seed({"command": "supergateway", "args": ["--stdio", "other"]}) is False
    assert is_legacy_supergateway_seed({}) is False


async def test_migrate_legacy_seed_rewrites_and_keeps_other_keys(store_env):
    from opendesign_mcp_admin.seed_migration import migrate_legacy_seed

    await store_env.put("mcp_server", dict(LEGACY_SEED, port=8123))
    assert await migrate_legacy_seed() is True
    mcp = await store_env.get("mcp_server")
    assert mcp["command"] == "opendesign-mcp-server"
    assert mcp["args"] == ["--host", "127.0.0.1", "--port", "8123"]
    assert mcp["port"] == 8123
    assert mcp["env"] == {"FOO": "bar"}
    assert mcp["env_prefix"] == ""
    # 冪等
    assert await migrate_legacy_seed() is False


async def test_migrate_leaves_custom_command_untouched(store_env):
    from opendesign_mcp_admin.seed_migration import migrate_legacy_seed

    custom = {"command": "my-od-server", "args": ["--x"], "port": 8000, "env": {}}
    await store_env.put("mcp_server", custom)
    assert await migrate_legacy_seed() is False
    assert (await store_env.get("mcp_server"))["command"] == "my-od-server"


def test_admin_lifespan_migrates_seed_before_starting_mcp(tmp_config_path, monkeypatch):
    import mcp_admin_core.app as app_mod
    import mcp_admin_core.config.store as store_mod

    cfg = json.loads(json.dumps(store_mod._DEFAULT_CONFIG))
    cfg["mcp_server"] = dict(LEGACY_SEED)
    tmp_config_path.write_text(json.dumps(cfg))
    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(tmp_config_path))
    monkeypatch.setattr(store_mod, "_instance", None)
    store_mod.get_config_store()

    seen_at_start: dict = {}

    class _PM:
        async def start(self):
            seen_at_start.update(json.loads(tmp_config_path.read_text())["mcp_server"])
            return True

        async def stop(self):
            return None

    monkeypatch.setattr(app_mod, "get_process_manager", lambda: _PM())

    from opendesign_mcp_admin.main import app

    with TestClient(app) as c:
        assert c.get("/healthz").status_code == 200

    # core lifespan 呼叫 pm.start() 時，config.json 已是原生 launcher
    assert seen_at_start["command"] == "opendesign-mcp-server"
    assert seen_at_start["args"] == ["--host", "127.0.0.1", "--port", "8000"]
