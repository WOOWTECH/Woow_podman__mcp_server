"""Hermes MCP server 的雙 transport launcher（SSE + StreamableHTTP）。

把 :data:`hermes_mcp_server.server.server`（``MCPServer`` 內部的 low-level ``Server``）交給
core 共用的 :class:`~mcp_admin_core.mcp_sse_wrapper.DualTransportApp`，同時以
SSE（``/sse`` + ``/messages/``）與 StreamableHTTP（``/mcp``）兩套 transport 跑成 HTTP
伺服器，綁在 loopback，讓 mcp-admin 的反向代理
（``/private_{token}/...`` → ``127.0.0.1:{port}/...``）能把 MCP client 請求轉進來。

對外契約：admin 容器對外仍是 ``:8080``（GUI 在 ``/``、MCP client 在
``/private_{token}/``）；本子行程只綁 ``127.0.0.1:8000``，經 proxy 以
``/sse`` + ``/messages/``（既有 SSE client）與 ``/mcp``（StreamableHTTP client）對外。
預設值對齊 config ``mcp_server.port``（預設 8000）。

部署時於 config.json seed（command 不變，仍為 console script ``hermes-mcp-server``）::

    "mcp_server": {
      "command": "hermes-mcp-server",
      "args": ["--host", "127.0.0.1", "--port", "8000"],
      "port": 8000,
      "env_prefix": "HERMES_"
    }
"""
from __future__ import annotations

import argparse

import uvicorn
from mcp_admin_core.mcp_sse_wrapper import DualTransportApp

from hermes_mcp_server.server import server


def main(argv: list[str] | None = None) -> None:
    """Console script 進入點（``hermes-mcp-server``）。"""
    parser = argparse.ArgumentParser(
        description="Run the Hermes MCP server over SSE + StreamableHTTP (fronted by the mcp-admin proxy)."
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

    # 以 DualTransportApp 同時暴露 /sse + /messages/（SSE）與 /mcp（StreamableHTTP）。
    uvicorn.run(DualTransportApp(server), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
