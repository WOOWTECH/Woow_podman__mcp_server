"""FastMCP server factory and command line entry point."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import fastmcp
import httpx
from fastmcp import FastMCP
from fastmcp.server.http import HostOriginGuardMiddleware
from starlette.middleware import Middleware

from . import __version__
from .client import BackendPolicyError, NextcloudClient
from .settings import Settings, SettingsError, load_settings
from .tools import register_tools

SERVER_NAME = "Woow Nextcloud"

logger = logging.getLogger("nextcloud_mcp_server")

INSTRUCTIONS = "\n".join(
    (
        "This server works with ONE Nextcloud account chosen by the administrator; you cannot switch accounts.",  # noqa: E501
        'Paths are relative to that account\'s home folder ("" or "/" is the home); use \'/\' as separator.',  # noqa: E501
        "Start with get_file_tree to see folders and files, then read_text_file to read a text file.",  # noqa: E501
        "Always read a file first: update_text_file, upload_file (replace) and delete_file_checked need its etag.",  # noqa: E501
        "If a write fails because the file changed (etag mismatch, HTTP 412), never retry blindly: read it again, merge, then retry.",  # noqa: E501
        "create_text_file and upload_file without expected_etag never overwrite an existing file.",
        "Deleted files go to the Nextcloud trash bin (when the Deleted files app is enabled).",
        "Text tools refuse folders, binary (non UTF-8) files and files over the configured size limit.",  # noqa: E501
        "list_calendars shows calendar ids; list_tasks reads tasks (VTODO) from them.",
        "Some tools may be absent because the administrator made the server read-only or disabled them.",  # noqa: E501
    )
)


def create_server(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastMCP:
    """Build the MCP server for ``settings``. No request is sent to Nextcloud here.

    Raises :class:`BackendPolicyError` when a ``backend_policy`` module exists but is
    unusable (resolved now, at start-up, not on the first tool call).
    """
    nc = NextcloudClient(settings, transport=transport)
    if nc.policy is not None:
        logger.info("backend_policy.async_client will build the HTTP client")
        if not settings.verify_tls or settings.ca_bundle:
            logger.warning(
                "NEXTCLOUD_MCP_VERIFY_TLS / NEXTCLOUD_MCP_CA_BUNDLE are ignored: "
                "backend_policy owns TLS verification."
            )
    for warning in settings.startup_warnings():
        logger.warning(warning)

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[dict[str, Any]]:
        try:
            yield {"nextcloud": nc}
        finally:
            await nc.aclose()

    server: FastMCP = FastMCP(
        SERVER_NAME,
        instructions=INSTRUCTIONS,
        version=__version__,
        lifespan=lifespan,
        mask_error_details=True,
        on_duplicate="error",
    )
    register_tools(server, nc, settings)
    # For gateways: ``await server.nextcloud_client.probe()`` is a cheap health check.
    server.nextcloud_client = nc  # type: ignore[attr-defined]
    return server


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("must be between 1 and 65535")
    return port


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nextcloud-mcp-server",
        description="MCP server for one Nextcloud account (settings: NEXTCLOUD_MCP_* env).",
    )
    parser.add_argument("--transport", choices=("stdio", "http", "sse"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (http/sse)")
    parser.add_argument("--port", type=_port, default=3000, help="port (http/sse)")
    parser.add_argument(
        "--path", default=None, help="endpoint path (http: /mcp, sse: /sse by default)"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _quiet_fastmcp_defaults() -> None:
    """Default to no banner and no update check unless the environment says otherwise.

    FastMCP reads its settings when it is imported, so the values are set on
    ``fastmcp.settings`` as well as in the environment (for child processes).
    """
    if "FASTMCP_CHECK_FOR_UPDATES" not in os.environ:
        os.environ["FASTMCP_CHECK_FOR_UPDATES"] = "off"
        fastmcp.settings.check_for_updates = "off"
    if "FASTMCP_SHOW_SERVER_BANNER" not in os.environ:
        os.environ["FASTMCP_SHOW_SERVER_BANNER"] = "false"
        fastmcp.settings.show_server_banner = False


def http_guard(settings: Settings, host: str, transport: str = "http") -> dict[str, Any]:
    """Host/Origin protection for the HTTP and SSE transports (DNS-rebinding defence).

    FastMCP's guard in strict mode answers 421 to a Host header that is not localhost,
    127.0.0.1, ::1, the bound address or one of NEXTCLOUD_MCP_ALLOWED_HOSTS (ports are
    ignored, ``*`` patterns allowed), and 403 to a browser Origin that is neither
    same-origin nor loopback. FastMCP 3.4.5 wires the guard only into the Streamable
    HTTP app, so for SSE the same middleware is added explicitly.
    """
    allowed = list(settings.allowed_host_names)
    if host not in ("0.0.0.0", "::", "") and host not in allowed:  # noqa: S104
        allowed.append(host)
    if transport == "sse":
        guard = Middleware(HostOriginGuardMiddleware, allowed_hosts=allowed, mode="strict")
        return {"middleware": [guard]}
    return {"host_origin_protection": True, "allowed_hosts": allowed}


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _quiet_fastmcp_defaults()
    try:
        settings = load_settings()
        server = create_server(settings)
    except (SettingsError, BackendPolicyError) as exc:
        print(f"nextcloud-mcp-server: configuration error: {exc}", file=sys.stderr)
        return 2
    if args.transport == "stdio":
        server.run(transport="stdio")
    else:
        path = args.path or ("/mcp" if args.transport == "http" else "/sse")
        if not path.startswith("/"):
            path = "/" + path
        server.run(
            transport=args.transport,
            host=args.host,
            port=args.port,
            path=path,
            **http_guard(settings, args.host, args.transport),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
