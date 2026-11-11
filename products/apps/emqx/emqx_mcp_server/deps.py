"""Dependency-injection helpers.

Parameters resolved through `Depends` are stripped from the JSON Schema, so
the model never sees (or can override) the HTTP client.

Why the handle exists: FastMCP's DI enters and exits anything the provider
returns that looks like an async context manager. Returning the pooled
`httpx.AsyncClient` therefore closed it as soon as the first tool finished,
and the second tool in the same session died with "Cannot reopen a client
instance, once it has been closed". An async generator does not help either —
this FastMCP passes the generator object straight through. So the provider
hands back a plain object that owns nothing and is not a context manager.
`tests/test_client_lifetime.py` pins the behaviour.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_context


class EmqxHttp:
    """A borrowed reference to the pooled client. Deliberately not closeable."""

    __slots__ = ("_client",)

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        return await self._client.request(method, url, **kwargs)

    @property
    def is_closed(self) -> bool:
        return self._client.is_closed

    @property
    def timeout(self) -> httpx.Timeout:
        return self._client.timeout


def emqx_client() -> EmqxHttp:
    """The pooled EMQX client for the current request."""
    try:
        client = get_context().lifespan_context["emqx"]
    except (RuntimeError, KeyError, TypeError) as exc:
        raise ToolError(
            "The EMQX connection is not initialised. Restart the MCP server "
            "from the Admin console and try again."
        ) from exc

    if client.is_closed:
        raise ToolError(
            "The EMQX connection was closed unexpectedly. Restart the MCP "
            "server from the Admin console and try again."
        )
    return EmqxHttp(client)
