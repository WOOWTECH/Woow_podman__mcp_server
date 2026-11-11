"""Packet tracing and listener inspection."""

from __future__ import annotations

import time
from typing import Annotated, Any, Literal

import httpx
from fastmcp import FastMCP
from fastmcp.dependencies import Depends
from pydantic import Field

from ..deps import emqx_client
from ..errors import emqx_request, json_body
from ..gating import ToolGate
from ._common import destructive, page_of, path_segment, read_only, writing

TraceTarget = Literal["clientid", "topic", "ip_address"]


def register(mcp: FastMCP, gate: ToolGate) -> None:
    on = gate.is_tool_enabled

    if on("emqx_list_traces"):

        @mcp.tool(name="emqx_list_traces", tags={"emqx", "read", "diagnostics"},
                  annotations=read_only("List EMQX Traces"))
        async def list_traces(
            emqx: httpx.AsyncClient = Depends(emqx_client),
        ) -> dict[str, Any]:
            """Packet traces defined on the broker, with status and expiry."""
            body = json_body(await emqx_request(emqx, "GET", "/trace"))
            rows = body if isinstance(body, list) else body.get("data", [])
            return {"count": len(rows or []), "traces": rows or []}

    if on("emqx_create_trace"):

        @mcp.tool(name="emqx_create_trace", tags={"emqx", "write", "diagnostics"},
                  annotations=writing("Create EMQX Trace"))
        async def create_trace(
            name: Annotated[str, Field(
                description="Unique trace name; you will reuse it to read the log.")],
            target_type: Annotated[TraceTarget, Field(
                description="What to trace on.")],
            target: Annotated[str, Field(
                description="The client id, topic filter or IP to capture.")],
            duration_seconds: Annotated[int, Field(
                description="How long to capture for.", ge=10, le=3600)] = 300,
            emqx: httpx.AsyncClient = Depends(emqx_client),
        ) -> dict[str, Any]:
            """Start capturing MQTT packets for one client, topic or IP.

            Traces are the tool for "the device connects but nothing arrives".
            The capture is written as it happens, so emqx_get_trace_log returns
            events while the trace is still running. Deleting a trace removes
            its log, so read it first.
            """
            # EMQX has no "duration" field: it wants an explicit window as epoch
            # seconds. Sending duration was accepted and ignored, leaving every
            # trace at the broker default of 10 minutes.
            start_at = int(time.time())
            end_at = start_at + duration_seconds
            payload = {"name": name, "type": target_type, target_type: target,
                       "start_at": start_at, "end_at": end_at}
            body = json_body(await emqx_request(emqx, "POST", "/trace", json=payload))
            body = body if isinstance(body, dict) else {}

            # EMQX echoes end_at == start_at in the create response even though
            # it stores the window correctly, so report what we asked for and
            # point at the tool that shows the stored value.
            return {"name": name, "type": target_type, "target": target,
                    "duration_seconds": duration_seconds,
                    "start_epoch": start_at, "end_epoch": end_at,
                    "status": body.get("status"), "created": True,
                    "note": "emqx_list_traces shows the window as the broker stored it."}

    if on("emqx_get_trace_log"):

        @mcp.tool(name="emqx_get_trace_log", tags={"emqx", "read", "diagnostics"},
                  annotations=read_only("Read EMQX Trace Log"))
        async def get_trace_log(
            name: Annotated[str, Field(description="Trace name to read.")],
            max_bytes: Annotated[int, Field(
                description="Truncate the log at this many bytes.",
                ge=1024, le=1048576)] = 65536,
            emqx: httpx.AsyncClient = Depends(emqx_client),
        ) -> dict[str, Any]:
            """Read what a trace captured.

            Returns the captured text per node. An empty log means the trace has
            matched nothing yet, not that it failed.
            """
            # log_detail reports only the file size per node. The captured text
            # lives behind /log, which needs an explicit node.
            safe_name = path_segment(name, 'name')
            detail = json_body(await emqx_request(
                emqx, "GET", f"/trace/{safe_name}/log_detail"))
            rows = detail if isinstance(detail, list) else []

            logs = []
            for row in rows:
                node = row.get("node")
                if not node:
                    continue
                body = json_body(await emqx_request(
                    emqx, "GET", f"/trace/{safe_name}/log",
                    params={"node": node, "bytes": max_bytes}))
                body = body if isinstance(body, dict) else {}
                position = body.get("meta", {}).get("position", 0)
                logs.append({"node": node, "size": row.get("size"),
                             "text": body.get("items") or "",
                             "truncated": bool(position >= max_bytes)})

            total = sum(len(entry["text"]) for entry in logs)
            return {"name": name, "nodes": logs, "captured_bytes": total,
                    "empty": total == 0, "max_bytes": max_bytes}

    if on("emqx_delete_trace"):

        @mcp.tool(name="emqx_delete_trace",
                  tags={"emqx", "write", "destructive", "diagnostics"},
                  annotations=destructive("Delete EMQX Trace"))
        async def delete_trace(
            name: Annotated[str, Field(description="Trace name to delete.")],
            emqx: httpx.AsyncClient = Depends(emqx_client),
        ) -> dict[str, Any]:
            """[DESTRUCTIVE] Delete a trace and its captured log.

            The log file goes with it. Call emqx_get_trace_log first if you
            still need what it captured.
            """
            await emqx_request(emqx, "DELETE", f"/trace/{path_segment(name, 'name')}")
            return {"name": name, "deleted": True}

    if on("emqx_list_listeners"):

        @mcp.tool(name="emqx_list_listeners", tags={"emqx", "read", "diagnostics"},
                  annotations=read_only("List EMQX Listeners"))
        async def list_listeners(
            emqx: httpx.AsyncClient = Depends(emqx_client),
        ) -> dict[str, Any]:
            """Listeners, their bind addresses and running state.

            This is where you confirm that 1883 (MQTT), 8883 (TLS), 8083/8084
            (WebSocket) are actually up before blaming a device.
            """
            body = json_body(await emqx_request(emqx, "GET", "/listeners"))
            rows, _ = page_of(body)
            return {"count": len(rows), "listeners": rows}
