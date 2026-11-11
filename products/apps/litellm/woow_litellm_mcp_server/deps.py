"""Dependency-injection helpers for tool functions.

Tools call :func:`litellm_client` with their FastMCP ``Context`` to obtain a
:class:`LiteLLMHttp` handle. That handle *borrows* the pooled client owned by
the lifespan — it must NOT be closed by the tool (load-bearing gotcha: closing
it would break every subsequent tool call in the same process).

If the lifespan context is missing or the underlying client is already closed,
a ``ToolError`` is raised so the failure is actionable rather than a raw
AttributeError.
"""

from __future__ import annotations

from typing import Any

import httpx

from .errors import ToolError, json_body, litellm_request
from .lifespan import LITELLM_CLIENT_KEY


class LiteLLMHttp:
    """A thin, non-closeable wrapper around the pooled httpx client.

    Provides ``get``/``post``/``put``/``patch``/``delete``/``request`` methods
    that return decoded JSON and route through :func:`litellm_request` so all
    error mapping is centralised.
    """

    __slots__ = ("_client",)

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    # -- raw pooled client (read-only view) -------------------------------
    @property
    def raw(self) -> httpx.AsyncClient:
        return self._client

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_data: Any | None = None,
        **kwargs: Any,
    ) -> Any:
        response = await litellm_request(
            self._client,
            method,
            path,
            params=params,
            json_data=json_data,
            **kwargs,
        )
        return json_body(response)

    async def get(
        self, path: str, *, params: dict[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        return await self.request("GET", path, params=params, **kwargs)

    async def post(
        self,
        path: str,
        *,
        json_data: Any | None = None,
        params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        return await self.request(
            "POST", path, json_data=json_data, params=params, **kwargs
        )

    async def put(
        self, path: str, *, json_data: Any | None = None, **kwargs: Any
    ) -> Any:
        return await self.request("PUT", path, json_data=json_data, **kwargs)

    async def patch(
        self, path: str, *, json_data: Any | None = None, **kwargs: Any
    ) -> Any:
        return await self.request("PATCH", path, json_data=json_data, **kwargs)

    async def delete(self, path: str, **kwargs: Any) -> Any:
        return await self.request("DELETE", path, **kwargs)

    # Intentionally NO aclose()/close(): this handle is borrowed, never owned.


def _lifespan_context(ctx: Any) -> dict[str, Any] | None:
    """Best-effort extraction of the lifespan context dict from a Context."""
    if ctx is None:
        return None
    # FastMCP exposes it via ctx.request_context.lifespan_context.
    req_ctx = getattr(ctx, "request_context", None)
    lifespan_ctx = getattr(req_ctx, "lifespan_context", None)
    if lifespan_ctx is None:
        # Some FastMCP versions expose it directly on the context.
        lifespan_ctx = getattr(ctx, "lifespan_context", None)
    if isinstance(lifespan_ctx, dict):
        return lifespan_ctx
    return None


def litellm_client(ctx: Any) -> LiteLLMHttp:
    """Return a borrowed :class:`LiteLLMHttp` handle for the current request.

    Raises ``ToolError`` if the lifespan context is unavailable or the pooled
    client has been closed.
    """
    lifespan_ctx = _lifespan_context(ctx)
    if lifespan_ctx is None:
        raise ToolError(
            "LiteLLM client is unavailable: the server lifespan context is "
            "missing. This usually means the tool was invoked outside a running "
            "MCP server."
        )
    client = lifespan_ctx.get(LITELLM_CLIENT_KEY)
    if client is None:
        raise ToolError(
            "LiteLLM client is not initialised in the lifespan context."
        )
    if getattr(client, "is_closed", False):
        raise ToolError(
            "LiteLLM client has already been closed; the server is shutting down."
        )
    return LiteLLMHttp(client)
