"""測試用的真實 Streamable HTTP MCP 子行程（被 test_proxy_tool_deadline_streaming.py 啟動）。

用 mcp 套件內建的 ``MCPServer``（1.x 的 FastMCP）、預設設定（``json_response=False``）——跟正式的 odoo-mcp 一樣，
``tools/call`` 的回應是 ``text/event-stream``：回應標頭立刻送出，工具結果之後才以 SSE 事件送達。

用法：``python _streamable_slow_server.py <port>``
"""

from __future__ import annotations

import sys
import time

import anyio
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("deadline-test", log_level="WARNING")


@mcp.tool()
async def slow(seconds: float) -> str:
    """非同步睡 ``seconds`` 秒後回傳。"""
    await anyio.sleep(seconds)
    return f"done after {seconds}"


@mcp.tool()
async def slow_blocking(seconds: float) -> str:
    """在事件迴圈上同步睡（卡住子行程的事件迴圈），模擬 odoo 子行程釘在事件迴圈上的同步寫入工具。

    mcp 2.x 會把同步 ``def`` 工具丟工作緒，不再卡住事件迴圈；這裡刻意寫成 async 裡直接 ``time.sleep``，
    重現 mcp 1.x 的同步工具、以及 odoo ``concurrency`` 釘回事件迴圈的寫入類（#78）。
    """
    time.sleep(seconds)
    return f"blocking done after {seconds}"


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="127.0.0.1", port=int(sys.argv[1]))
