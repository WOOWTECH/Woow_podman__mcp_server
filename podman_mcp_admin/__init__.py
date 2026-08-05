"""Podman MCP Admin — GUI, admin API and MCP proxy on a single port.

The heavy lifting lives in the product-agnostic ``mcp_admin_core`` package
(FastAPI factory, JWT middleware, config store, subprocess manager, MCP reverse
proxy).  This package only contributes the thin Podman-specific layer: the
health/log/token routers and the tool-switch persistence that translate between
the Admin GUI and ``woow_podman_mcp_server``.

Phase 1 scope: the console boots, authenticates, supervises the MCP child and
proxies ``/private_<token>/mcp`` to it.  The connection and tool-management
routers land in Phase 2/3; until then their pages render "N/A" rather than
live data, by design.
"""

__version__ = "0.1.0"
