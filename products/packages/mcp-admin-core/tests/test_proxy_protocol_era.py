"""閘道對 MCP 2026-07-28 世代專屬方法回 -32601，client 維持 2025 世代握手（#78，protocol_era.py）。

上游是真的 MCP SDK 2.x ``MCPServer``（與 hermes／opendesign 相同，掛在 core ``DualTransportApp`` 後面），
admin 閘道與上游都跑真的 uvicorn。重點：

- ``server/discover``（真實 Claude.ai 連線時先送的，評估報告 §8.4）與 ``subscriptions/listen`` 由閘道自己回
  HTTP 200＋JSON-RPC -32601（通知回 202），**不轉給子行程**；
- 前提：SDK 2.x 子行程自己會回 discover 結果（不攔的話 client 會被升級到 2026-07-28 世代）；
- mcp 2.x ``Client`` 預設 ``mode="auto"`` 經閘道後落在 2025-11-25；明確指定 2026-07-28 的 client 照舊可用；
- n8n 這種有自己輸入界線（``McpRequestLimits``）的產品也一樣攔。
"""
from __future__ import annotations

import json

import anyio
import httpx
import pytest
from mcp.client import Client
from mcp.server.mcpserver import MCPServer

from mcp_admin_core.mcp_sse_wrapper import DualTransportApp
from mcp_admin_core.protocol_era import MODERN_ONLY_METHODS
from test_proxy_sse_endpoint import _Uvicorn, admin_app  # noqa: F401 — admin_app 是 fixture

_H = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
# 真實 Claude.ai 送的 discover（評估報告 §8.4 引 HA release-decision-0.1.14 的觀察：header 2026-07-28、params 只有 _meta）
_DISCOVER = {"jsonrpc": "2.0", "id": 9, "method": "server/discover", "params": {"_meta": {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "Anthropic/ClaudeAI", "version": "1.0.0"}}}}


class _Recorder:
    """記錄送到上游的每個 POST 的 JSON-RPC method（不改內容）。"""

    def __init__(self, app) -> None:
        self.app = app
        self.methods: list = []

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        messages, body = [], b""
        while True:
            message = await receive()
            messages.append(message)
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        try:
            self.methods.append(json.loads(body).get("method"))
        except (ValueError, AttributeError):
            self.methods.append(None)
        pending = iter(messages)

        async def replay():
            try:
                return next(pending)
            except StopIteration:
                return await receive()

        return await self.app(scope, replay, send)


def _child(stateless: bool = False):
    server = MCPServer("era-probe")

    @server.tool()
    def alpha() -> str:
        """Returns a constant."""
        return "called alpha"

    return _Recorder(DualTransportApp(server._lowlevel_server, stateless=stateless))


def _rpc(response: httpx.Response) -> dict:
    if "event-stream" in response.headers.get("content-type", ""):
        data = [line[5:].strip() for line in response.text.splitlines() if line.startswith("data:")]
        return json.loads(data[-1])
    return response.json()


def test_child_alone_would_switch_to_the_2026_era():
    """前提：SDK 2.x 子行程直接回 discover 結果——不在閘道攔下，client 就會改用 2026-07-28 世代。"""
    with _Uvicorn(_child()) as child:
        response = httpx.post(f"http://127.0.0.1:{child.port}/mcp", json=_DISCOVER,
                              headers={**_H, "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "server/discover"})
    assert response.status_code == 200, response.text
    assert "2026-07-28" in _rpc(response)["result"]["supportedVersions"]


@pytest.mark.parametrize("headers", [
    {"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "server/discover"},
    {"MCP-Protocol-Version": "2026-07-28"},
    {},
], ids=["full-2026-headers", "version-header-only", "no-headers"])
def test_gateway_answers_discover_with_method_not_found(admin_app, headers):
    child = _child()
    with _Uvicorn(child) as upstream, _Uvicorn(admin_app(upstream.port)) as admin:
        response = httpx.post(f"http://127.0.0.1:{admin.port}/private_secret/mcp", json=_DISCOVER, headers={**_H, **headers})
    assert response.status_code == 200
    assert response.json() == {"jsonrpc": "2.0", "id": 9, "error": {"code": -32601, "message": "Method not found"}}
    assert response.headers["cache-control"] == "no-store"
    assert child.methods == [], "server/discover 不得轉給子行程"


def test_gateway_refuses_every_2026_only_method_and_notifications(admin_app):
    child = _child()
    with _Uvicorn(child) as upstream, _Uvicorn(admin_app(upstream.port)) as admin:
        base = f"http://127.0.0.1:{admin.port}"
        for rpc_id, method in enumerate(sorted(MODERN_ONLY_METHODS), start=20):
            response = httpx.post(base + "/private_secret/mcp", headers=_H,
                                  json={"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": {}})
            assert response.json()["error"]["code"] == -32601 and response.json()["id"] == rpc_id, method
        # 根路徑（/private_<token>）也一樣
        root = httpx.post(base + "/private_secret", headers=_H, json={**_DISCOVER, "id": "root"})
        assert root.json()["error"]["code"] == -32601 and root.json()["id"] == "root"
        # 沒有 id 的同名通知：202、不轉發
        note = httpx.post(base + "/private_secret/mcp", headers=_H, json={"jsonrpc": "2.0", "method": "server/discover"})
        assert note.status_code == 202 and note.content == b""
        # token 不對時照舊 403（先驗 token、再談方法）
        assert httpx.post(base + "/private_wrong/mcp", headers=_H, json=_DISCOVER).status_code == 403
        # 2025 世代照常轉發
        init = httpx.post(base + "/private_secret/mcp", headers=_H, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}})
        assert _rpc(init)["result"]["protocolVersion"] == "2025-11-25"
    assert child.methods == ["initialize"]


@pytest.mark.parametrize("stateless", [False, True], ids=["stateful", "stateless"])
def test_sdk2_auto_client_negotiates_the_2025_era_through_the_gateway(admin_app, stateless):
    """mcp 2.x Client 預設 mode="auto"：先送 discover，被閘道回 -32601 後退回 initialize（2025-11-25）。"""
    child = _child(stateless)
    seen = {}
    with _Uvicorn(child) as upstream, _Uvicorn(admin_app(upstream.port)) as admin:
        url = f"http://127.0.0.1:{admin.port}/private_secret/mcp"

        async def go():
            with anyio.fail_after(30):
                async with Client(url) as client:
                    seen["protocol"] = client.protocol_version
                    seen["tools"] = [t.name for t in (await client.list_tools()).tools]
                    seen["alpha"] = (await client.call_tool("alpha", {})).content[0].text

        anyio.run(go)
    assert seen == {"protocol": "2025-11-25", "tools": ["alpha"], "alpha": "called alpha"}
    assert child.methods[0] == "initialize" and "server/discover" not in child.methods


def test_client_pinned_to_2026_is_still_served(admin_app):
    """明確指定 2026-07-28、不送 discover 的 client 不受影響（閘道只攔 2026 專屬方法）。"""
    child = _child()
    seen = {}
    with _Uvicorn(child) as upstream, _Uvicorn(admin_app(upstream.port)) as admin:
        url = f"http://127.0.0.1:{admin.port}/private_secret/mcp"

        async def go():
            with anyio.fail_after(30):
                async with Client(url, mode="2026-07-28") as client:
                    seen["protocol"] = client.protocol_version
                    seen["alpha"] = (await client.call_tool("alpha", {})).content[0].text

        anyio.run(go)
    assert seen == {"protocol": "2026-07-28", "alpha": "called alpha"}
    assert "server/discover" not in child.methods


def test_products_with_their_own_input_limits_are_covered(admin_app, monkeypatch):
    """n8n 走 McpRequestLimits 讀 body；2026 專屬方法同樣在閘道回 -32601。"""
    from mcp_admin_core.request_limits import McpRequestLimits

    child = _child()
    with _Uvicorn(child) as upstream:
        app = admin_app(upstream.port)
        app.state.mcp_request_limits = McpRequestLimits(max_bytes=4 * 1024 * 1024, max_argument_elements=10_000)
        with _Uvicorn(app) as admin:
            response = httpx.post(f"http://127.0.0.1:{admin.port}/private_secret/mcp", json=_DISCOVER, headers=_H)
    assert response.json()["error"]["code"] == -32601
    assert child.methods == []
