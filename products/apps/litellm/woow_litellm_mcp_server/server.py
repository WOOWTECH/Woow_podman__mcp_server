"""Entry point: builds the FastMCP server and runs it (stdio or http).

``build_server`` constructs a :class:`fastmcp.FastMCP` with the lifespan that
owns the pooled LiteLLM client, then loops over the tool modules calling each
module's ``register(mcp, gate)`` so only gate-enabled tools are exposed.

In the deployed topology this module is *not* launched directly. The admin console
spawns it as a child process bound to ``127.0.0.1:3000`` and publishes it through the
token-checking proxy at ``/private_{token}/mcp/``; loopback binding is what makes that
proxy the only door. Nothing in this module authenticates anything.

For local development, run over Streamable-HTTP with::

    python -m woow_litellm_mcp_server.server --transport http \\
        --host 127.0.0.1 --port 8000 --path /mcp/

or over stdio for local MCP clients::

    python -m woow_litellm_mcp_server.server --transport stdio

Binding a non-loopback ``--host`` exposes all tools, including the dangerous ones, to
anything that can reach that address. Do so only behind an authenticating proxy.
"""

from __future__ import annotations

import argparse
import logging

from fastmcp import FastMCP

from .gating import ToolGate
from .lifespan import lifespan
from .middleware import install as install_gating_middleware
from .settings import get_settings
from .tools import MODULES

logger = logging.getLogger(__name__)

INSTRUCTIONS = """\
This MCP server administers a LiteLLM gateway. Use its tools to list and manage
models, run OpenAI-compatible chat completions, mint and govern virtual keys,
manage teams and internal users, pull spend logs and cost reports, check gateway
health, and curate the Claude-Code skill-hub plugins.

Tool names are prefixed ``litellm_``. Destructive tools (delete/block) have a
``[DESTRUCTIVE]`` docstring prefix; confirm intent before calling them. When a
tool reports an error it surfaces the LiteLLM error body so you can correct the
parameters and retry.
"""


def build_server(gate: ToolGate | None = None) -> FastMCP:
    """Construct and populate the FastMCP server instance."""
    gate = gate or ToolGate(get_settings())
    mcp = FastMCP(
        name="woow-litellm-mcp-server",
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
        mask_error_details=True,
    )
    for module in MODULES:
        module.register(mcp, gate)
    # Gated tools are unregistered, not stubbed, so calling one yields FastMCP's
    # bare "Unknown tool: 'x'". This middleware intercepts tools/call for names
    # that ARE in the registry but are gated off and replaces that with the
    # actual reason. Clients cache tools/list, so this path is hit routinely.
    install_gating_middleware(mcp, gate)
    logger.info(
        "Registered tools from %d modules; %d tools enabled by the gate.",
        len(MODULES),
        len(gate.enabled_tool_names()),
    )
    return mcp


# Module-global server so ``fastmcp run woow_litellm_mcp_server.server:mcp`` and
# the admin console's subprocess launcher both have a stable target.
mcp = build_server()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="woow_litellm_mcp_server",
        description="Woow LiteLLM MCP server.",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http", "streamable-http", "sse"],
        default="http",
        help="Transport to serve on (default: http / streamable-http).",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Bind host for http.")
    parser.add_argument(
        "--port", type=int, default=8000, help="Bind port for http."
    )
    parser.add_argument(
        "--path", default="/mcp/", help="URL path for the http endpoint."
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (DEBUG/INFO/WARNING/ERROR).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.transport == "stdio":
        logger.info("Starting LiteLLM MCP server on stdio")
        mcp.run(transport="stdio")
        return

    transport = "http" if args.transport == "streamable-http" else args.transport
    logger.info(
        "Starting LiteLLM MCP server on %s://%s:%s%s",
        transport,
        args.host,
        args.port,
        args.path,
    )
    mcp.run(
        transport=transport,
        host=args.host,
        port=args.port,
        path=args.path,
    )


if __name__ == "__main__":
    main()
