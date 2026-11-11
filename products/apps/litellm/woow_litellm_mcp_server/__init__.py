"""Woow LiteLLM MCP Server.

A FastMCP server that exposes the LiteLLM admin/proxy API surface as MCP tools,
so that Claude (and any other MCP client) can operate a LiteLLM gateway:
manage models, virtual keys, teams, users, spend reporting, health and the
Claude-Code skill-hub plugins.

The design mirrors WOOWTECH/Woow_emqx_mcp_server, adapted for LiteLLM.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
