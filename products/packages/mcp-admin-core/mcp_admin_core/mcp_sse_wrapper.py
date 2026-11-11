"""Run a low-level MCP server over BOTH SSE and StreamableHTTP.

Usage:
    python -m mcp_admin_core.mcp_sse_wrapper mcp_server_odoo.server:server --port 8000

Imports the low-level ``Server`` object named by ``server_spec`` and serves it via:
  - SSE            : GET /sse + POST /messages/   (legacy transport; kept for existing clients)
  - StreamableHTTP : /mcp                         (MCP 2025-03; what ``--transport http`` clients use)

The admin reverse proxy forwards ``/private_<token>/{sse,messages/,mcp}`` to this app.

Both transports accept request bodies up to ``request_body.MAX_REQUEST_BODY_BYTES`` (16 MiB, the PaaS ceiling)
instead of the MCP SDK default of 4 MiB (1.30.0 and 2.x), which answers larger bodies with 413 before handling them.

``/mcp`` is served at the ASGI layer rather than via a Starlette route: a bare ``/mcp``
(no trailing slash) — what StreamableHTTP clients and the proxy send — would otherwise
trigger Starlette's trailing-slash 307 redirect to ``/mcp/``, whose ``Location`` leaks the
loopback upstream (``http://127.0.0.1:<port>/mcp/``) back through the proxy and breaks the client.
"""

from __future__ import annotations

import argparse
import importlib
from contextlib import asynccontextmanager

from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from starlette.types import Receive, Scope, Send

from .request_body import MAX_REQUEST_BODY_BYTES


class DualTransportApp(Starlette):
    """Serve one low-level MCP ``Server`` over SSE (/sse, /messages/) and StreamableHTTP (/mcp)."""

    def __init__(self, server, *, stateless: bool = False,
                 max_request_body_size: int = MAX_REQUEST_BODY_BYTES) -> None:
        # stateful（預設）：發 Mcp-Session-Id、session 存活到 client DELETE。
        # stateless=True：每個請求獨立 transport、不留 session 狀態（工具本身無狀態時用，
        # 例如 opendesign——多 session 不累積記憶體，#1369）。
        # max_request_body_size：兩個 transport 都明確設成 PaaS 上限（16 MiB，見 request_body.py）；
        # MCP SDK（1.30.0 起，2.x 相同）的預設 4 MiB 會在處理前回 413，擋下大型工具參數（例如 base64 上傳）。
        self._manager = StreamableHTTPSessionManager(
            app=server, stateless=stateless, max_request_body_size=max_request_body_size
        )
        sse = SseServerTransport("/messages/", max_request_body_size=max_request_body_size)

        async def handle_sse(request):
            async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
                await server.run(streams[0], streams[1], server.create_initialization_options())

        @asynccontextmanager
        async def lifespan(_app):
            async with self._manager.run():  # StreamableHTTP 需 lifespan 跑其 task group
                yield

        super().__init__(
            routes=[
                Route("/sse", endpoint=handle_sse),
                Mount("/messages/", app=sse.handle_post_message),
            ],
            lifespan=lifespan,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # 裸 /mcp 與 /mcp/ 皆直接交給 StreamableHTTP（避開 Starlette 的 307 轉址）。
        if scope["type"] == "http" and scope["path"].rstrip("/") == "/mcp":
            await self._manager.handle_request(scope, receive, send)
            return
        await super().__call__(scope, receive, send)  # lifespan + /sse + /messages/


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run an MCP server over SSE + StreamableHTTP (fronted by the mcp-admin proxy)."
    )
    parser.add_argument("server_spec", help="module.path:server_object (e.g. mcp_server_odoo.server:server)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    module_path, obj_name = args.server_spec.rsplit(":", 1)
    server = getattr(importlib.import_module(module_path), obj_name)

    import uvicorn

    uvicorn.run(DualTransportApp(server), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
