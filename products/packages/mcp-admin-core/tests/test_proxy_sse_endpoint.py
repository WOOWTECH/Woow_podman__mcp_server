"""legacy SSE 的 ``endpoint`` 事件經 proxy 時要補上 ``/private_<token>`` 前綴。

子行程（core ``DualTransportApp``／n8n-mcp／k8s-mcp-server）只知道自己在 127.0.0.1 根路徑，
``GET /sse`` 的第一個事件回 ``event: endpoint`` + ``data: /messages/?session_id=…``（root-relative）。
client 依規格用 ``urljoin(<sse url>, data)`` 算 POST 位址，結果掉了 ``/private_<token>``，
POST 打到 admin app 根路徑 → 405，官方 SDK ``sse_client`` 握手失敗
（商用驗收 DATA-NMCP-005-1、AI-HMCP-002-3）。

這裡驗兩層：
- ``SseToolsFilter`` 的 endpoint 改寫（各種 data 形式、分段 chunk、CRLF）。
- 端到端：真的上游 ``DualTransportApp`` ＋ 真的 admin app（都跑 uvicorn），用官方
  ``mcp.client.sse.sse_client`` 透過 proxy 握手、``tools/list``、``tools/call``。
"""
from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import anyio
import mcp.types as types
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.server.lowlevel import Server

from mcp_admin_core.mcp_sse_wrapper import DualTransportApp
from mcp_admin_core.tool_filter import SseToolsFilter

PREFIX = "/private_secret"


async def _no_disabled() -> frozenset[str]:
    return frozenset()


def _run(coro):
    return asyncio.run(coro)


async def _feed_all(chunks: list[bytes], prefix: str | None = PREFIX) -> bytes:
    f = SseToolsFilter(_no_disabled, endpoint_prefix=prefix)
    out = b""
    for c in chunks:
        out += await f.feed(c)
    return out + f.flush()


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        # core DualTransportApp／mcp SDK SseServerTransport（Hermes、Odoo、OpenDesign）
        ("/messages/?session_id=abc", "/private_secret/messages/?session_id=abc"),
        # n8n-mcp（Express）
        ("/messages?sessionId=abc", "/private_secret/messages?sessionId=abc"),
        # k8s-mcp-server（mark3labs/mcp-go）
        ("/message?sessionId=abc", "/private_secret/message?sessionId=abc"),
        # 絕對網址指回 loopback：SDK 會因 origin 不符拒絕，改成帶前綴的 root-relative
        ("http://127.0.0.1:8000/messages/?session_id=abc", "/private_secret/messages/?session_id=abc"),
        ("http://localhost:3000/messages?sessionId=abc", "/private_secret/messages?sessionId=abc"),
        # 已經帶前綴（子行程自己知道 base path）不可重複加
        ("/private_secret/messages/?session_id=abc", "/private_secret/messages/?session_id=abc"),
        # 相對路徑（不以 / 開頭）client 端 urljoin 本來就會留在 /private_<token>/ 底下，不動
        ("messages/?session_id=abc", "messages/?session_id=abc"),
        # 指向別的主機的絕對網址不碰
        ("https://example.com/messages", "https://example.com/messages"),
        # protocol-relative（//host/path）不是本機路徑，不碰
        ("//example.com/messages", "//example.com/messages"),
    ],
)
def test_endpoint_event_data_rewritten(data, expected):
    raw = f"event: endpoint\ndata: {data}\n\n".encode()
    out = _run(_feed_all([raw])).decode()
    assert out == f"event: endpoint\ndata: {expected}\n\n"


def test_endpoint_event_split_across_chunks_and_crlf():
    raw = b"event: endpoint\r\ndata: /messages/?session_id=xyz\r\n\r\n"
    chunks = [raw[:7], raw[7:20], raw[20:]]
    out = _run(_feed_all(chunks))
    assert out == b"event: endpoint\r\ndata: /private_secret/messages/?session_id=xyz\r\n\r\n"


def test_endpoint_event_without_space_after_colon():
    out = _run(_feed_all([b"event:endpoint\ndata:/messages?sessionId=1\n\n"]))
    assert out == b"event:endpoint\ndata: /private_secret/messages?sessionId=1\n\n"


def test_non_endpoint_events_untouched():
    msg = b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"url":"/messages/"}}\n\n'
    ping = b": ping\n\n"
    assert _run(_feed_all([msg + ping])) == msg + ping


def test_no_prefix_means_no_endpoint_rewrite():
    raw = b"event: endpoint\ndata: /messages/?session_id=abc\n\n"
    assert _run(_feed_all([raw], prefix=None)) == raw


# ---------------------------------------------------------------------------
# 端到端：官方 SDK sse_client 透過 proxy 握手
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Uvicorn:
    def __init__(self, app):
        self.port = _free_port()
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="on")
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.time() + 15
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("server did not start")
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


@pytest.fixture
def admin_app(tmp_config_path, monkeypatch):
    """回傳 factory(upstream_port) → 組好的 admin ASGI app（proxy 指向上游）。"""
    import mcp_admin_core.config.store as store_mod
    import mcp_admin_core.proxy as proxy_mod

    from mcp_admin_core import create_app

    monkeypatch.setattr(proxy_mod, "_client", None)

    def factory(port: int):
        cfg = json.loads(json.dumps(store_mod._DEFAULT_CONFIG))
        cfg.update({"mcp_auth_token": "secret",
                    "mcp_server": {"command": "", "args": [], "port": port, "env": {}}})
        tmp_config_path.write_text(json.dumps(cfg))
        monkeypatch.setenv("MCP_ADMIN_CONFIG", str(tmp_config_path))
        monkeypatch.setattr(store_mod, "_instance", None)
        return create_app(title="t")

    yield factory
    monkeypatch.setattr(proxy_mod, "_client", None)


def test_official_sse_client_handshake_through_proxy(admin_app):
    calls: list[str] = []
    with _Uvicorn(DualTransportApp(_mcp_server(calls))) as upstream:
        with _Uvicorn(admin_app(upstream.port)) as admin:
            url = f"http://127.0.0.1:{admin.port}/private_secret/sse"

            async def _session():
                with anyio.fail_after(20):
                    async with sse_client(url, timeout=5, sse_read_timeout=10) as (read, write):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            tools = await session.list_tools()
                            result = await session.call_tool("alpha", {})
                            return [t.name for t in tools.tools], result

            names, result = anyio.run(_session)

    assert names == ["alpha", "beta"]
    assert result.content[0].text == "called alpha"
    assert calls == ["alpha"]
