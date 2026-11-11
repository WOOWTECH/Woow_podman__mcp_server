"""Tool modules for the LiteLLM MCP server.

``server.build_server`` iterates :data:`MODULES` and calls each module's
``register(mcp, gate)`` function. Adding a new family of tools is just: create a
module with a ``register`` function and append it here (and declare the tools in
:mod:`woow_litellm_mcp_server.registry`).
"""

from __future__ import annotations

from . import chat, health, keys, models, plugins, spend, teams, users

MODULES = [
    models,
    chat,
    keys,
    teams,
    users,
    spend,
    health,
    plugins,
]

__all__ = ["MODULES"]
