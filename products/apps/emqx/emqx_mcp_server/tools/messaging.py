"""Publishing and retained-message tools."""

from __future__ import annotations

import base64
from typing import Annotated, Any, Literal

import httpx
from fastmcp import FastMCP
from fastmcp.dependencies import Depends
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from ..deps import emqx_client
from ..errors import EmqxApiError, emqx_request, json_body
from ..gating import ToolGate
from ..models import RetainedMessage
from ..settings import settings
from ._common import destructive, page_of, path_segment, read_only, writing

_SCAN_PAGES = 20


def _decode(payload: Any) -> str | None:
    """EMQX returns retained payloads base64-encoded."""
    if not isinstance(payload, str):
        return None
    try:
        return base64.b64decode(payload, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return payload


def _as_retained(row: dict, topic: str, lookup: str) -> RetainedMessage:
    return RetainedMessage(
        topic=topic,
        found=True,
        payload=_decode(row.get("payload")),
        qos=row.get("qos"),
        publish_at=row.get("publish_at"),
        lookup=lookup,
    )


async def _direct_lookup(emqx: httpx.AsyncClient, topic: str) -> dict | None:
    """Read one retained message by topic.

    EMQX accepts hierarchical topics here as long as the whole topic is
    percent-encoded, slashes included: `woow%2Ftest%2Fx` returns 200 where the
    raw form returns 404. A 404 means nothing is retained, which is an ordinary
    answer rather than a failure. '', '.' and '..' are valid MQTT topics but no
    URL path segment can carry them (httpx drops dot segments), so they are left
    to the listing scan.
    """
    if topic in ("", ".", ".."):
        return None
    try:
        body = json_body(await emqx_request(
            emqx, "GET", f"/mqtt/retainer/message/{path_segment(topic, 'topic')}"))
    except EmqxApiError as exc:
        if exc.status_code == 404:
            return None
        raise
    return body if isinstance(body, dict) and body else None


async def _scan_for_topic(emqx: httpx.AsyncClient, topic: str) -> dict | None:
    """Walk the retained listing looking for one topic.

    Fallback only. The listing rows carry no payload, so a hit here proves the
    topic is retained without telling you what it holds.
    """
    for page in range(1, _SCAN_PAGES + 1):
        rows, meta = page_of(json_body(await emqx_request(
            emqx, "GET", "/mqtt/retainer/messages",
            params={"page": page, "limit": 200},
        )))
        for row in rows:
            if row.get("topic") == topic:
                return row
        if not rows or not meta.get("hasnext"):
            return None
    return None


class OutgoingMessage(BaseModel):
    """One message in a bulk publish."""

    topic: str = Field(description="Target topic. Wildcards are not allowed.")
    payload: str = Field(description="Message body.")
    qos: int = Field(0, description="MQTT QoS: 0, 1 or 2.", ge=0, le=2)
    retain: bool = Field(False, description="Retain this message on the broker.")


def register(mcp: FastMCP, gate: ToolGate) -> None:
    on = gate.is_tool_enabled

    if on("emqx_publish"):

        @mcp.tool(name="emqx_publish", tags={"emqx", "write", "messaging"},
                  annotations=writing("Publish MQTT Message"))
        async def publish(
            topic: Annotated[str, Field(
                description="Target topic, e.g. 'woow/test/switch/state'. "
                            "Wildcards (+ and #) are not allowed when publishing.",
                min_length=1)],
            payload: Annotated[str, Field(
                description="Message body, encoded per `payload_encoding`.")],
            qos: Annotated[int, Field(
                description="MQTT QoS: 0, 1 or 2.", ge=0, le=2)] = 0,
            retain: Annotated[bool, Field(
                description="Keep this as the topic's retained message, so new "
                            "subscribers receive it on connect.")] = False,
            payload_encoding: Annotated[Literal["plain", "base64"], Field(
                description="Use base64 for binary payloads.")] = "plain",
            emqx: httpx.AsyncClient = Depends(emqx_client),
        ) -> dict[str, Any]:
            """Publish a single MQTT message through the broker.

            This is how you drive a device or feed a Home Assistant MQTT
            entity. Publishing to a topic nobody subscribes to succeeds
            silently — check emqx_list_subscriptions if nothing reacts.
            """
            if "+" in topic or "#" in topic:
                raise ToolError(
                    f"Topic {topic!r} contains a wildcard. Publish to a concrete "
                    "topic; wildcards are only valid in subscriptions."
                )
            body = json_body(await emqx_request(
                emqx, "POST", "/publish",
                json={"topic": topic, "payload": payload, "qos": qos,
                      "retain": retain, "payload_encoding": payload_encoding},
            ))
            body = body if isinstance(body, dict) else {}

            # EMQX answers 202 with reason_code 16 when the message was
            # accepted but nothing was subscribed — the usual explanation for
            # "I published and nothing happened".
            note = body.get("message")
            delivered = body.get("reason_code") != 16
            if not delivered and not note:
                note = "no_matching_subscribers"

            return {
                "topic": topic, "qos": qos, "retain": retain,
                "id": body.get("id"), "published": True,
                "delivered_to_subscribers": delivered,
                "broker_note": note,
            }

    if on("emqx_publish_bulk"):

        @mcp.tool(name="emqx_publish_bulk", tags={"emqx", "write", "messaging"},
                  annotations=writing("Publish MQTT Messages In Bulk"))
        async def publish_bulk(
            messages: Annotated[list[OutgoingMessage], Field(
                description="Messages to publish, at most 50 per call.",
                max_length=50)],
            emqx: httpx.AsyncClient = Depends(emqx_client),
        ) -> dict[str, Any]:
            """Publish a batch of MQTT messages in one round trip.

            Prefer this over many emqx_publish calls when seeding several
            topics — for example bringing up a set of test entities.
            """
            if not messages:
                raise ToolError("Provide at least one message to publish.")
            payload = [m.model_dump() for m in messages]
            body = json_body(
                await emqx_request(emqx, "POST", "/publish/bulk", json=payload)
            )

            # Same reason_code 16 rule as emqx_publish: raw rows make a message
            # nobody received look exactly like a delivered one.
            rows = body if isinstance(body, list) else [body]
            results = []
            for message, row in zip(messages, rows):
                row = row if isinstance(row, dict) else {}
                note = row.get("message")
                delivered = row.get("reason_code") != 16
                if not delivered and not note:
                    note = "no_matching_subscribers"
                results.append({"topic": message.topic, "id": row.get("id"),
                                "delivered_to_subscribers": delivered,
                                "broker_note": note})

            undelivered = [r["topic"] for r in results
                           if not r["delivered_to_subscribers"]]
            return {"count": len(messages), "published": True,
                    "delivered_count": len(results) - len(undelivered),
                    "undelivered_topics": undelivered, "results": results}

    if on("emqx_list_retained"):

        @mcp.tool(name="emqx_list_retained", tags={"emqx", "read", "messaging"},
                  annotations=read_only("List Retained Messages"))
        async def list_retained(
            page: Annotated[int, Field(description="1-based page number.", ge=1)] = 1,
            limit: Annotated[int, Field(
                description="Rows per page.", ge=1, le=200)] = settings.default_limit,
            emqx: httpx.AsyncClient = Depends(emqx_client),
        ) -> dict[str, Any]:
            """Retained messages the broker is holding.

            Retained messages are the broker's memory of "last known value" per
            topic — stale ones are a common cause of ghost entities appearing
            after a device is removed. The listing carries no payloads; use
            emqx_get_retained to read one.
            """
            rows, meta = page_of(json_body(await emqx_request(
                emqx, "GET", "/mqtt/retainer/messages",
                params={"page": page, "limit": min(limit, settings.max_limit)},
            )))
            return {"count": len(rows), "total": meta.get("count", len(rows)),
                    "messages": rows}

    if on("emqx_get_retained"):

        @mcp.tool(name="emqx_get_retained", tags={"emqx", "read", "messaging"},
                  annotations=read_only("Get Retained Message"))
        async def get_retained(
            topic: Annotated[str, Field(
                description="Exact topic whose retained message to read, "
                            "e.g. 'woow/test/switch/state'.")],
            emqx: httpx.AsyncClient = Depends(emqx_client),
        ) -> RetainedMessage:
            """Read the retained message stored for one topic.

            Returns found=false when the topic has nothing retained — that is
            an ordinary answer, not an error.
            """
            row = await _direct_lookup(emqx, topic)
            if row is not None:
                return _as_retained(row, topic, "direct")

            # Fallback only: the listing carries no payload, so lookup=listing
            # means "this topic is retained" without the contents.
            row = await _scan_for_topic(emqx, topic)
            if row is None:
                return RetainedMessage(topic=topic, found=False, lookup="listing")
            return _as_retained(row, topic, "listing")

    if on("emqx_delete_retained"):

        @mcp.tool(name="emqx_delete_retained",
                  tags={"emqx", "write", "destructive", "messaging"},
                  annotations=destructive("Delete Retained Message"))
        async def delete_retained(
            topic: Annotated[str, Field(
                description="Exact topic whose retained message to remove.")],
            emqx: httpx.AsyncClient = Depends(emqx_client),
        ) -> dict[str, Any]:
            """[DESTRUCTIVE] Delete the retained message for one topic.

            This is the correct way to remove a stale Home Assistant discovery
            entry or a ghost device state. It cannot be undone — the previous
            payload is gone unless something republishes it.
            """
            # A failure may follow a committed write. Never follow an ambiguous
            # delete with a publish: subscribers would receive a new message.
            await emqx_request(
                emqx, "DELETE", f"/mqtt/retainer/message/{path_segment(topic, 'topic')}")
            return {"topic": topic, "deleted": True, "method": "delete"}
