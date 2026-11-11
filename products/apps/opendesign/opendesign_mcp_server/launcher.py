"""Open Design MCP server 的原生 HTTP launcher（Streamable HTTP + SSE，單一行程）。

#1369：舊架構以 supergateway（stdio → Streamable HTTP 橋）包 ``python -m
opendesign_mcp_server``，每個 MCP session／請求都 spawn 一個 python 子行程（含 mcp＋httpx，
約 115MB）且不回收，第 7～8 個 session 即 OOM。改為本 launcher 直接在單一行程內跑 vendored
FastMCP server：

- ``/mcp``：Streamable HTTP，**stateless**（不發 Mcp-Session-Id、每請求獨立 transport、不留
  per-session 狀態）——與舊 supergateway stateless 對外語意一致，且多 session 不累積記憶體；
- ``/sse`` + ``/messages/``：legacy SSE（core ``DualTransportApp`` 免費附帶）。

vendored ``od_mcp_server.py`` 的 15 支工具都是同步函式（httpx 同步 client；``send_message``
可阻塞最長 ``OD_CHAT_TIMEOUT`` 秒）。mcp 1.x 的 FastMCP 會在 event loop 上直接呼叫同步工具，單一行程下
會卡住所有 session，故啟動前把同步工具包成「丟 worker thread 跑」的 async 版本（不改 vendored
檔，維持 byte-faithful）。mcp 2.x（#78）的 ``MCPServer`` 本身就把同步工具丟 worker thread，這層包裝
維持原樣（行為相同、不依賴 SDK 預設）。

mcp 2.x 另外會把工具丟出的一般例外遮成 ``Error executing tool <name>``；vendored 檔讓 httpx 例外直接
往上拋（例如 Open Design daemon 連不上），所以 :func:`build_app` 再以 core 的 ``keep_error_text``
把例外文字照 mcp 1.x 的樣子交給 client。

部署 seed（config.json）::

    "mcp_server": {
      "command": "opendesign-mcp-server",
      "args": ["--host", "127.0.0.1", "--port", "8000"],
      "port": 8000,
      "env_prefix": ""
    }

舊 seed（command=supergateway + ``python -m opendesign_mcp_server``）由 admin 啟動時自動
遷移，見 :mod:`opendesign_mcp_admin.seed_migration`。
"""
from __future__ import annotations

import argparse
import functools
from typing import Any

import anyio.to_thread
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp_admin_core.mcp_sse_wrapper import DualTransportApp
from mcp_admin_core.tool_error_text import keep_error_text

from opendesign_mcp_server.od_mcp_server import mcp


def offload_sync_tools(server: MCPServer) -> int:
    """把 *server* 上的同步工具改成在 worker thread 執行的 async 版本；回傳改寫數。

    只換 ``Tool.fn`` 與 ``Tool.is_async``：參數驗證（``fn_metadata``）、schema、名稱都不變。
    冪等：已是 async 的工具略過。
    """
    count = 0
    for tool in server._tool_manager.list_tools():
        if tool.is_async:
            continue
        sync_fn = tool.fn

        @functools.wraps(sync_fn)
        async def _run_in_thread(*args: Any, __fn=sync_fn, **kwargs: Any) -> Any:
            return await anyio.to_thread.run_sync(functools.partial(__fn, *args, **kwargs))

        tool.fn = _run_in_thread
        tool.is_async = True
        count += 1
    return count


def build_app() -> DualTransportApp:
    """組出 ASGI app（供 launcher 與測試共用）。"""
    offload_sync_tools(mcp)
    keep_error_text(mcp)
    return DualTransportApp(mcp._lowlevel_server, stateless=True)


def main(argv: list[str] | None = None) -> None:
    """Console script 進入點（``opendesign-mcp-server``）。"""
    parser = argparse.ArgumentParser(
        description="Run the Open Design MCP server over Streamable HTTP (stateless) + SSE "
        "(fronted by the mcp-admin proxy)."
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind host；僅供 in-container proxy 反代用，務必維持 loopback（預設 127.0.0.1）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="bind port；必須等於 config mcp_server.port（預設 8000）",
    )
    args = parser.parse_args(argv)

    uvicorn.run(build_app(), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
