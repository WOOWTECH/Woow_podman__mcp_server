"""MCP SDK 2.x 遮掉工具例外文字；``keep_error_text`` 換回 mcp 1.x 的「Error executing tool X: 原因」（#78）。

前提測試釘住 SDK 的行為（若哪天 SDK 不再遮蔽，這裡會先紅、提醒重新評估），其餘驗證包裝後 client 實際拿到的文字。
各產品（hermes、opendesign、odoo）的端到端錯誤文字見 test_hermes_*、test_opendesign_native_launcher、
test_odoo_read_concurrency。
"""
from __future__ import annotations

import httpx
import pytest
from mcp.client import Client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError

from mcp_admin_core.tool_error_text import keep_error_text


def _server() -> MCPServer:
    server = MCPServer("error-text")

    @server.tool()
    def sync_boom() -> str:
        """Sync tool raising a plain exception."""
        raise RuntimeError("backend down: BACKEND_UNAVAILABLE")

    @server.tool()
    async def async_boom() -> str:
        """Async tool raising a ValueError subclass (like hermes GuardError)."""
        raise LookupError("session 'x' not found (不存在)")

    @server.tool()
    def httpx_boom() -> str:
        """Raw httpx error (what vendored od_mcp_server lets through)."""
        raise httpx.ConnectError("[Errno 111] Connection refused")

    @server.tool()
    def deliberate() -> str:
        """Already a ToolError."""
        raise ToolError("visible tool error text")

    @server.tool()
    def protocol() -> str:
        """MCPError stays a JSON-RPC error."""
        raise MCPError(code=-32602, message="protocol level")

    @server.tool()
    def add(a: int, b: int) -> int:
        """Normal tool."""
        return a + b

    return server


async def _call(server, name, args=None):
    async with Client(server, mode="legacy") as client:
        return await client.call_tool(name, args or {})


async def test_premise_sdk2_masks_plain_exceptions():
    result = await _call(_server(), "sync_boom")
    assert result.is_error is True
    assert result.content[0].text == "Error executing tool sync_boom"


@pytest.mark.parametrize(("name", "text"), [
    ("sync_boom", "Error executing tool sync_boom: backend down: BACKEND_UNAVAILABLE"),
    ("async_boom", "Error executing tool async_boom: session 'x' not found (不存在)"),
    ("httpx_boom", "Error executing tool httpx_boom: [Errno 111] Connection refused"),
    ("deliberate", "Error executing tool deliberate: visible tool error text"),
])
async def test_wrapped_tools_keep_the_mcp1_error_text(name, text):
    server = _server()
    keep_error_text(server)
    result = await _call(server, name)
    assert result.is_error is True
    assert result.content[0].text == text


async def test_mcp_errors_and_successes_are_unchanged():
    server = _server()
    keep_error_text(server)
    assert (await _call(server, "add", {"a": 2, "b": 3})).content[0].text == "5"
    async with Client(server, mode="legacy") as client:
        with pytest.raises(MCPError) as caught:
            await client.call_tool("protocol", {})
    assert caught.value.code == -32602
    # 參數驗證本來就是 ToolError（SDK 自己丟），文字照舊
    bad = await _call(server, "add", {"a": "x", "b": 1})
    assert bad.is_error is True and bad.content[0].text.startswith("Error executing tool add: ")


def test_wrapper_keeps_sync_async_schema_and_is_idempotent():
    server = _server()
    before = {t.name: (t.is_async, t.parameters) for t in server._tool_manager.list_tools()}
    assert keep_error_text(server) == sorted(before)
    assert keep_error_text(server) == []
    after = {t.name: (t.is_async, t.parameters) for t in server._tool_manager.list_tools()}
    assert after == before
