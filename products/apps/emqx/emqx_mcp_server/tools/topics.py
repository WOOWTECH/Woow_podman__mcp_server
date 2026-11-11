"""Topic and subscription discovery (read-only)."""

from __future__ import annotations

from typing import Annotated, Any

import httpx
from fastmcp import FastMCP
from fastmcp.dependencies import Depends
from pydantic import Field

from ..deps import emqx_client
from ..errors import emqx_request, json_body
from ..gating import ToolGate
from ..settings import settings
from ._common import page_of, read_only


def register(mcp: FastMCP, gate: ToolGate) -> None:
    on = gate.is_tool_enabled

    if on("emqx_list_topics"):

        @mcp.tool(name="emqx_list_topics", tags={"emqx", "read", "topics"},
                  annotations=read_only("List EMQX Topics"))
        async def list_topics(
            topic: Annotated[str | None, Field(
                description="Exact topic to look up. Omit to list all.")] = None,
            page: Annotated[int, Field(description="1-based page number.", ge=1)] = 1,
            limit: Annotated[int, Field(
                description="Rows per page.", ge=1, le=200)] = settings.default_limit,
            emqx: httpx.AsyncClient = Depends(emqx_client),
        ) -> dict[str, Any]:
            """Topics that currently have a route (i.e. at least one subscriber).

            A topic missing here means nobody is subscribed — which is the usual
            reason a published message appears to vanish.
            """
            params: dict[str, Any] = {
                "page": page, "limit": min(limit, settings.max_limit)
            }
            if topic:
                params["topic"] = topic
            rows, meta = page_of(
                json_body(await emqx_request(emqx, "GET", "/topics", params=params))
            )
            return {"count": len(rows), "total": meta.get("count", len(rows)),
                    "topics": rows}

    if on("emqx_list_subscriptions"):

        @mcp.tool(name="emqx_list_subscriptions", tags={"emqx", "read", "topics"},
                  annotations=read_only("List EMQX Subscriptions"))
        async def list_subscriptions(
            clientid: Annotated[str | None, Field(
                description="Only subscriptions belonging to this client.")] = None,
            topic: Annotated[str | None, Field(
                description="Exact subscription filter to match.")] = None,
            match_topic: Annotated[str | None, Field(
                description="Find subscriptions whose filter would match this "
                            "concrete topic — use this to answer 'who receives "
                            "messages published here?'.")] = None,
            qos: Annotated[int | None, Field(
                description="Filter by QoS level.", ge=0, le=2)] = None,
            page: Annotated[int, Field(description="1-based page number.", ge=1)] = 1,
            limit: Annotated[int, Field(
                description="Rows per page.", ge=1, le=200)] = settings.default_limit,
            emqx: httpx.AsyncClient = Depends(emqx_client),
        ) -> dict[str, Any]:
            """Every subscription in the cluster, with filters.

            `match_topic` is the powerful one: given a concrete topic, it tells
            you exactly which subscribers would receive a message on it.
            """
            params: dict[str, Any] = {
                "page": page, "limit": min(limit, settings.max_limit)
            }
            if clientid:
                params["clientid"] = clientid
            if topic:
                params["topic"] = topic
            if match_topic:
                params["match_topic"] = match_topic
            if qos is not None:
                params["qos"] = qos

            rows, meta = page_of(
                json_body(
                    await emqx_request(emqx, "GET", "/subscriptions", params=params)
                )
            )
            return {"count": len(rows), "total": meta.get("count", len(rows)),
                    "subscriptions": rows}
