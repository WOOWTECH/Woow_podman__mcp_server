"""Tool Manager 停用在 MCP proxy 層強制生效的端到端測試（#1384）。

起一個真的上游 MCP server（core DualTransportApp＋低階 Server，uvicorn 跑在背景 thread），
admin app 的 proxy 指向它；用 Streamable HTTP 走 initialize → tools/list → tools/call，
中途經 /api/tools 停用／啟用工具，驗證：同一個 session 內立即生效、上游從未收到被停用工具的呼叫、
不重啟子行程。另以假 JSON 上游驗證 JSON 回應與 legacy SSE /messages/ 改名路徑。
"""
from __future__ import annotations

from management_client import revision_headers

import json
import socket
import threading
import time

import mcp.types as types
import pytest
import uvicorn
from mcp.server.lowlevel import Server

from mcp_admin_core.mcp_sse_wrapper import DualTransportApp

_H = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Upstream:
    def __init__(self, app):
        self.port = _free_port()
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("upstream did not start")
            time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=10)


def _mcp_server(calls: list[str]) -> Server:
    async def _list_tools(ctx, params):
        return types.ListToolsResult(tools=[types.Tool(name=n, description=n, input_schema={"type": "object"}) for n in ("alpha", "beta")])

    async def _call_tool(ctx, params):
        calls.append(params.name)
        return types.CallToolResult(content=[types.TextContent(type="text", text=f"called {params.name}")])

    return Server("fake", on_list_tools=_list_tools, on_call_tool=_call_tool)


def _registry():
    from enum import Enum
    from types import SimpleNamespace

    class Cat(str, Enum):
        A = "A"

    reg = [SimpleNamespace(name=n, category=Cat.A, description=n, operations=[], dangerous=False)
           for n in ("alpha", "beta")]
    return SimpleNamespace(TOOL_REGISTRY=reg, ToolCategory=Cat,
                           get_all_tool_names=lambda: [t.name for t in reg],
                           get_tool_by_name=lambda n: next((t for t in reg if t.name == n), None))


@pytest.fixture
def admin(tmp_config_path, monkeypatch):
    """回傳 factory(port) → 進入 lifespan 的 TestClient（單一 event loop，proxy 的 httpx client 可重用）。"""
    import mcp_admin_core.config.store as store_mod
    import mcp_admin_core.proxy as proxy_mod
    from fastapi.testclient import TestClient

    from mcp_admin_core import create_app
    from mcp_admin_core.routers.tools import create_tools_router

    monkeypatch.setattr(proxy_mod, "_client", None)
    restarts: list[int] = []

    def factory(port: int):
        cfg = json.loads(json.dumps(store_mod._DEFAULT_CONFIG))
        cfg.update({"mcp_auth_token": "secret",
                    "mcp_server": {"command": "", "args": [], "port": port, "env": {}}})
        tmp_config_path.write_text(json.dumps(cfg))
        monkeypatch.setenv("MCP_ADMIN_CONFIG", str(tmp_config_path))
        monkeypatch.setattr(store_mod, "_instance", None)
        store_mod.get_config_store()

        import mcp_admin_core.process as process_mod

        async def _restart(self):  # 停用工具不得觸發重啟
            restarts.append(1)

        monkeypatch.setattr(process_mod.McpProcessManager, "restart", _restart)
        app = create_app(title="t", extra_routers=[create_tools_router(registry=_registry(), mcp_label="fake")])
        return TestClient(app)

    factory.restarts = restarts
    yield factory
    monkeypatch.setattr(proxy_mod, "_client", None)


def _rpc(resp):
    if "event-stream" in resp.headers.get("content-type", ""):
        msgs = [json.loads(ln[5:]) for ln in resp.text.splitlines() if ln.startswith("data:") and ln[5:].strip()]
        return [m for m in msgs if "id" in m][-1]
    return resp.json()


class _Session:
    def __init__(self, c):
        self.c, self.sid, self.n = c, None, 0

    def call(self, method, params=None):
        self.n += 1
        h = dict(_H)
        if self.sid:
            h["mcp-session-id"] = self.sid
        r = self.c.post("/private_secret/mcp", headers=h,
                        json={"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}})
        assert r.status_code == 200, r.text
        self.sid = r.headers.get("mcp-session-id", self.sid)
        if method == "initialize":
            h["mcp-session-id"] = self.sid
            self.c.post("/private_secret/mcp", headers=h,
                        json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        return _rpc(r)

    def tools(self):
        return [t["name"] for t in self.call("tools/list")["result"]["tools"]]


_INIT = {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}


def test_disable_applies_immediately_without_restart(admin):
    calls: list[str] = []
    with _Upstream(DualTransportApp(_mcp_server(calls))) as up, admin(up.port) as c:
        s = _Session(c)
        s.call("initialize", _INIT)
        assert s.tools() == ["alpha", "beta"]

        tok = c.post("/api/auth/login", json={"password": "admin"}).json()["token"]
        auth = {"Authorization": f"Bearer {tok}"}
        r = c.put("/api/tools", json={"disabled_tools": ["beta"]}, headers=revision_headers(c, auth, read_path='/api/tools'))
        assert r.status_code == 200 and r.json()["status"] == "ok"

        # 同一個 session、不重啟：tools/list 立即隱藏、tools/call 被拒且不到上游
        assert s.tools() == ["alpha"]
        res = s.call("tools/call", {"name": "beta", "arguments": {}})
        assert res["result"]["isError"] is True and "beta" in res["result"]["content"][0]["text"]
        assert "beta" not in calls
        assert s.call("tools/call", {"name": "alpha", "arguments": {}})["result"]["content"][0]["text"] == "called alpha"

        # 新 session 一樣看不到
        s2 = _Session(c)
        s2.call("initialize", _INIT)
        assert s2.tools() == ["alpha"]

        # 重新啟用 → 立即恢復
        c.put("/api/tools", json={"disabled_tools": []}, headers=revision_headers(c, auth, read_path='/api/tools'))
        assert s.tools() == ["alpha", "beta"]
        assert s.call("tools/call", {"name": "beta", "arguments": {}})["result"]["content"][0]["text"] == "called beta"
        assert admin.restarts == []


def _json_upstream(received: list):
    """假上游：一律回 application/json；記錄收到的 path 與 body。"""
    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        received.append((scope["path"], json.loads(body) if body else None))
        if scope["path"].startswith("/messages"):
            await send({"type": "http.response.start", "status": 202, "headers": []})
            await send({"type": "http.response.body", "body": b"Accepted"})
            return
        req = json.loads(body)
        payload = {"jsonrpc": "2.0", "id": req.get("id") if isinstance(req, dict) else None,
                   "result": {"tools": [{"name": "alpha"}, {"name": "beta"}]}}
        out = json.dumps(payload).encode()
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(out)).encode())]})
        await send({"type": "http.response.body", "body": out})
    return app


def test_json_response_filtered_and_legacy_sse_call_renamed(admin):
    received: list = []
    with _Upstream(_json_upstream(received)) as up, admin(up.port) as c:
        tok = c.post("/api/auth/login", json={"password": "admin"}).json()["token"]
        c.put("/api/tools", json={"disabled_tools": ["beta"]}, headers=revision_headers(c, {"Authorization": f"Bearer {tok}"}, read_path='/api/tools'))

        r = c.post("/private_secret/mcp", headers=_H, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.headers["content-type"].startswith("application/json")
        assert [t["name"] for t in r.json()["result"]["tools"]] == ["alpha"]

        # legacy SSE：回應只能走 GET 串流 → 不能直接回錯，改名轉發讓上游回 unknown tool
        r = c.post("/private_secret/messages/?session_id=abc", headers=_H,
                   json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "beta"}})
        assert r.status_code == 202
        path, body = received[-1]
        assert path == "/messages/" and body["params"]["name"] != "beta"
        assert body["params"]["name"] == ""

        # 混合 batch：只改被停用的那一筆
        c.post("/private_secret/mcp", headers=_H, json=[
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "alpha"}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "beta"}},
        ])
        _, body = received[-1]
        assert body[0]["params"]["name"] == "alpha" and body[1]["params"]["name"] != "beta"


def test_real_sdk_client_gets_a_tool_result_with_is_error(store_env, monkeypatch):
    """MCP Python SDK 2.3 的 ClientSession 經真的閘道（uvicorn）呼叫被停用的工具：拿到 isError=true 的工具結果與管理員
    停用的說明，不是丟出 MCPError（過去回 JSON-RPC -32602，SDK 直接拋例外）。MCP 2025-11-25：Tool Execution Error，
    client SHOULD 交給模型；子行程（n8n-mcp、SDK 的 Unknown tool）對同一種拒絕也是 isError。"""
    import asyncio

    import mcp_admin_core.app as app_module
    import mcp_admin_core.proxy as proxy_mod
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from mcp_admin_core import create_app

    class _PM:
        async def start(self):
            return False

        async def stop(self):
            return None

    monkeypatch.setattr(app_module, "get_process_manager", lambda: _PM())
    monkeypatch.setattr(proxy_mod, "_client", None)
    calls: list[str] = []
    with _Upstream(DualTransportApp(_mcp_server(calls))) as up:
        # 已有全開標記（管理員之後自己停用了 beta）：lifespan 的一次性遷移不會再清掉它
        marker = {"applied": "migrated", "source": "existing_config", "at": "2026-10-25T00:00:00+00:00"}
        asyncio.run(store_env.update({"mcp_auth_token": "secret", "mcp_server": {"port": up.port},
                                      "tools": {"disabled": ["beta"], "disabled_operations": {},
                                                "owner_defaults_20261025": marker}}))
        with _Upstream(create_app(title="t")) as gateway:
            async def scenario():
                async with streamable_http_client(f"http://127.0.0.1:{gateway.port}/private_secret/mcp") as (r, w, *_):
                    async with ClientSession(r, w) as session:
                        await session.initialize()
                        listed = [tool.name for tool in (await session.list_tools()).tools]
                        refused = await session.call_tool("beta", {})
                        allowed = await session.call_tool("alpha", {})
                        return listed, refused, allowed

            listed, refused, allowed = asyncio.run(scenario())
    proxy_mod._client = None
    assert listed == ["alpha"]
    assert refused.is_error is True
    assert refused.content[0].text == "Tool 'beta' is disabled by the administrator (此工具已被管理員停用)"
    assert allowed.is_error is not True and allowed.content[0].text == "called alpha"
    assert calls == ["alpha"]  # 被停用的沒到上游
