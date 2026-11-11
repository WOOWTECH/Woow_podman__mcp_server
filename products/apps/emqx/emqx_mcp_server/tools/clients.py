"""Client (connection and session) management tools."""

from __future__ import annotations

from typing import Annotated, Any

import httpx
from fastmcp import FastMCP
from fastmcp.dependencies import Depends
from pydantic import Field

from ..deps import emqx_client
from ..errors import emqx_request, json_body
from ..gating import ToolGate
from ..models import ClientInfo, ClientList, KickResult
from ..settings import settings
from ._common import destructive, page_of, path_segment, read_only

QOS = Annotated[int, Field(description="MQTT QoS: 0, 1 or 2.", ge=0, le=2)]


def _to_info(row: dict) -> ClientInfo:
    return ClientInfo(
        clientid=row.get("clientid", ""),
        username=row.get("username"),
        connected=bool(row.get("connected", False)),
        ip_address=row.get("ip_address"),
        proto_ver=row.get("proto_ver"),
        connected_at=row.get("connected_at"),
    )


def register(mcp: FastMCP, gate: ToolGate) -> None:
    on = gate.is_tool_enabled

    if on("emqx_list_clients"):

        @mcp.tool(name="emqx_list_clients", tags={"emqx", "read", "clients"},
                  annotations=read_only("List EMQX Clients"))
        async def list_clients(
            username: Annotated[str | None, Field(
                description="Exact username match.")] = None,
            clientid: Annotated[str | None, Field(
                description="Exact client id match.")] = None,
            ip_address: Annotated[str | None, Field(
                description="Exact peer IP match.")] = None,
            connected_only: Annotated[bool, Field(
                description="Return only currently connected clients.")] = False,
            page: Annotated[int, Field(description="1-based page number.", ge=1)] = 1,
            limit: Annotated[int, Field(
                description="Rows per page.", ge=1, le=200)] = settings.default_limit,
            emqx: httpx.AsyncClient = Depends(emqx_client),
        ) -> ClientList:
            """List MQTT clients known to the broker.

            Answers "which devices are online", "is device X connected", and
            "who is connecting from this IP". Walk large fleets by raising
            `page` rather than `limit`.
            """
            params: dict[str, Any] = {
                "page": page, "limit": min(limit, settings.max_limit)
            }
            if username:
                params["username"] = username
            if clientid:
                params["clientid"] = clientid
            if ip_address:
                params["ip_address"] = ip_address
            if connected_only:
                params["conn_state"] = "connected"

            rows, meta = page_of(
                json_body(await emqx_request(emqx, "GET", "/clients", params=params))
            )
            total = meta.get("count", len(rows))
            return ClientList(
                clients=[_to_info(r) for r in rows],
                total=total,
                has_more=bool(meta.get("hasnext", page * limit < total)),
            )

    if on("emqx_get_client"):

        @mcp.tool(name="emqx_get_client", tags={"emqx", "read", "clients"},
                  annotations=read_only("Get EMQX Client"))
        async def get_client(
            clientid: Annotated[str, Field(
                description="Exact client id, as returned by emqx_list_clients.")],
            emqx: httpx.AsyncClient = Depends(emqx_client),
        ) -> dict[str, Any]:
            """Full session detail for one client: keepalive, in-flight queue,
            protocol, subscriptions count, connect and disconnect timestamps."""
            return json_body(
                await emqx_request(emqx, "GET", f"/clients/{path_segment(clientid, 'clientid')}")
            )

    if on("emqx_client_subscriptions"):

        @mcp.tool(name="emqx_client_subscriptions", tags={"emqx", "read", "clients"},
                  annotations=read_only("EMQX Client Subscriptions"))
        async def client_subscriptions(
            clientid: Annotated[str, Field(description="Exact client id.")],
            emqx: httpx.AsyncClient = Depends(emqx_client),
        ) -> dict[str, Any]:
            """Which topics one client is subscribed to, with QoS.

            Use this when a device "isn't receiving anything" — usually the
            subscription filter does not match the publish topic.
            """
            body = json_body(await emqx_request(
                emqx, "GET", f"/clients/{path_segment(clientid, 'clientid')}/subscriptions"))
            rows = body if isinstance(body, list) else body.get("data", [])
            return {"clientid": clientid, "count": len(rows), "subscriptions": rows}

    if on("emqx_kick_client"):

        @mcp.tool(name="emqx_kick_client",
                  tags={"emqx", "write", "destructive", "clients"},
                  annotations=destructive("Kick Out MQTT Client"))
        async def kick_client(
            clientid: Annotated[str, Field(
                description="Client id to disconnect. Call emqx_list_clients "
                            "first to get an exact id.")],
            emqx: httpx.AsyncClient = Depends(emqx_client),
        ) -> KickResult:
            """[DESTRUCTIVE] Disconnect an MQTT client and clear its session.

            Queued and in-flight messages for that session are lost. Devices
            with auto-reconnect usually return within seconds, so this forces
            a fresh connection rather than blocking one — use emqx_ban to
            keep a client out.
            """
            await emqx_request(emqx, "DELETE", f"/clients/{path_segment(clientid, 'clientid')}")
            return KickResult(clientid=clientid, kicked=True)

    if on("emqx_client_subscribe"):

        @mcp.tool(name="emqx_client_subscribe",
                  tags={"emqx", "write", "destructive", "clients"},
                  annotations=destructive("Subscribe Client To Topic"))
        async def client_subscribe(
            clientid: Annotated[str, Field(description="Client id to act for.")],
            topic: Annotated[str, Field(
                description="Topic filter to subscribe, e.g. 'sensors/+/temp'.")],
            qos: QOS = 0,
            emqx: httpx.AsyncClient = Depends(emqx_client),
        ) -> dict[str, Any]:
            """[DESTRUCTIVE] Add a subscription to a client on its behalf.

            The device does not know this happened and will not re-create the
            subscription after a clean-session reconnect.
            """
            await emqx_request(
                emqx, "POST", f"/clients/{path_segment(clientid, 'clientid')}/subscribe",
                json={"topic": topic, "qos": qos},
            )
            return {"clientid": clientid, "topic": topic, "qos": qos,
                    "subscribed": True}

    if on("emqx_client_unsubscribe"):

        @mcp.tool(name="emqx_client_unsubscribe",
                  tags={"emqx", "write", "destructive", "clients"},
                  annotations=destructive("Unsubscribe Client From Topic"))
        async def client_unsubscribe(
            clientid: Annotated[str, Field(description="Client id to act for.")],
            topic: Annotated[str, Field(
                description="Exact topic filter to remove.")],
            emqx: httpx.AsyncClient = Depends(emqx_client),
        ) -> dict[str, Any]:
            """[DESTRUCTIVE] Remove a subscription from a client on its behalf.

            The device stops receiving that topic immediately and has no way
            to tell why.
            """
            await emqx_request(
                emqx, "POST", f"/clients/{path_segment(clientid, 'clientid')}/unsubscribe",
                json={"topic": topic},
            )
            return {"clientid": clientid, "topic": topic, "unsubscribed": True}
