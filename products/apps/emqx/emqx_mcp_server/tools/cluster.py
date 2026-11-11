"""Cluster and monitoring tools (all read-only)."""

from __future__ import annotations

import re
from typing import Annotated, Any

import httpx
from fastmcp import FastMCP
from fastmcp.dependencies import Depends
from pydantic import Field

from ..deps import emqx_client
from ..errors import EmqxApiError, emqx_request, json_body
from ..gating import ToolGate
from ..models import BrokerStats
from ._common import path_segment, read_only


def register(mcp: FastMCP, gate: ToolGate) -> None:
    on = gate.is_tool_enabled

    if on("emqx_cluster_status"):

        @mcp.tool(name="emqx_cluster_status", tags={"emqx", "read", "cluster"},
                  annotations=read_only("EMQX Cluster Status"))
        async def cluster_status(
            emqx: httpx.AsyncClient = Depends(emqx_client),
        ) -> dict[str, Any]:
            """Nodes in the EMQX cluster with version, uptime and resource use.

            Start here when asked "is EMQX healthy", "what version is running",
            or before any node-specific call, to learn valid node names.
            """
            nodes = json_body(await emqx_request(emqx, "GET", "/nodes"))
            # Never turn a wrong service's generic HTTP-200 object into health.
            if (not isinstance(nodes, list) or not nodes or
                    any(not isinstance(n, dict) or n.get('error') is not None or
                        not isinstance(n.get('node'), str) or not re.fullmatch(r'[^@\s]+@[^@\s]+', n['node']) or
                        not isinstance(n.get('version'), str) or not n['version'].strip()
                        for n in nodes) or
                    len({n['node'] for n in nodes}) != len(nodes)):
                raise EmqxApiError('BACKEND_INVALID_NODE_RESPONSE')
            rows = nodes
            return {
                "node_count": len(rows),
                "nodes": [
                    {
                        "node": n.get("node"),
                        "version": n.get("version"),
                        "role": n.get("role"),
                        "status": n.get("node_status"),
                        "uptime_ms": n.get("uptime"),
                        "connections": n.get("connections"),
                        "load1": n.get("load1"),
                        "memory_used": n.get("memory_used"),
                    }
                    for n in rows
                ],
            }

    if on("emqx_node_detail"):

        @mcp.tool(name="emqx_node_detail", tags={"emqx", "read", "cluster"},
                  annotations=read_only("EMQX Node Detail"))
        async def node_detail(
            node: Annotated[str, Field(
                description="Node name exactly as emqx_cluster_status reports it, "
                            "e.g. 'emqx@127.0.0.1'.")],
            emqx: httpx.AsyncClient = Depends(emqx_client),
        ) -> dict[str, Any]:
            """Everything EMQX knows about one node."""
            return json_body(await emqx_request(emqx, "GET", f"/nodes/{path_segment(node, 'node', safe='@')}"))

    if on("emqx_broker_stats"):

        @mcp.tool(name="emqx_broker_stats", tags={"emqx", "read", "cluster"},
                  annotations=read_only("EMQX Broker Stats"))
        async def broker_stats(
            emqx: httpx.AsyncClient = Depends(emqx_client),
        ) -> BrokerStats:
            """Live counters for the whole cluster.

            Answers "how many devices are online" and "how many subscriptions
            exist" in one call — cheaper than listing clients.
            """
            body = json_body(await emqx_request(emqx, "GET", "/stats"))
            row = body[0] if isinstance(body, list) and body else body
            row = row if isinstance(row, dict) else {}
            return BrokerStats(
                connections=row.get("connections.count", 0),
                sessions=row.get("sessions.count", 0),
                subscriptions=row.get("subscriptions.count", 0),
                topics=row.get("topics.count", 0),
                retained=row.get("retained.count", 0),
            )

    if on("emqx_metrics_current"):

        @mcp.tool(name="emqx_metrics_current", tags={"emqx", "read", "cluster"},
                  annotations=read_only("EMQX Current Metrics"))
        async def metrics_current(
            emqx: httpx.AsyncClient = Depends(emqx_client),
        ) -> dict[str, Any]:
            """Current throughput gauges (messages/s in and out, dropped, rate)."""
            return json_body(await emqx_request(emqx, "GET", "/monitor_current"))

    if on("emqx_metrics_history"):

        @mcp.tool(name="emqx_metrics_history", tags={"emqx", "read", "cluster"},
                  annotations=read_only("EMQX Metrics History"))
        async def metrics_history(
            latest_seconds: Annotated[int, Field(
                description="Length of the window to return, in seconds.",
                ge=60, le=86400)] = 3600,
            emqx: httpx.AsyncClient = Depends(emqx_client),
        ) -> dict[str, Any]:
            """Time-series metrics over a recent window.

            Returns a per-series summary plus a downsampled trend, so "was there
            a spike" is answerable without reading every sample. Series that sat
            at zero for the whole window are listed but not plotted.
            """
            body = json_body(
                await emqx_request(emqx, "GET", "/monitor",
                                   params={"latest": latest_seconds})
            )
            points = body if isinstance(body, list) else []

            # EMQX samples every 10s, so an hour is 361 points x 16 series.
            # Returned raw that is ~87 KB, past any per-result budget, and the
            # call fails instead of answering. Summarise, then downsample.
            series: dict[str, list] = {}
            for point in points:
                for key, value in point.items():
                    if key == "time_stamp" or not isinstance(value, (int, float)):
                        continue
                    series.setdefault(key, []).append(value)

            summary = {}
            for key, values in series.items():
                if not values or not any(values):
                    continue  # a series that is zero throughout says nothing
                summary[key] = {"min": min(values), "max": max(values),
                                "avg": round(sum(values) / len(values), 3),
                                "last": values[-1]}

            step = max(1, len(points) // 60)
            sampled = points[::step][-60:]
            trend = [{k: v for k, v in point.items()
                      if k == "time_stamp" or k in summary} for point in sampled]

            return {"window_seconds": latest_seconds,
                    "point_count": len(points),
                    "sample_interval_seconds": (latest_seconds // len(points)
                                                if points else None),
                    "active_series": sorted(summary),
                    "idle_series_omitted": sorted(set(series) - set(summary)),
                    "summary": summary,
                    "trend_points": len(trend),
                    "trend": trend}

    if on("emqx_list_alarms"):

        @mcp.tool(name="emqx_list_alarms", tags={"emqx", "read", "cluster"},
                  annotations=read_only("EMQX Alarms"))
        async def list_alarms(
            active_only: Annotated[bool, Field(
                description="True for currently firing alarms, False for history."
            )] = True,
            emqx: httpx.AsyncClient = Depends(emqx_client),
        ) -> dict[str, Any]:
            """Broker alarms — the first place to look when something feels wrong."""
            body = json_body(await emqx_request(
                emqx, "GET", "/alarms",
                params={"activated": "true" if active_only else "false"}))
            rows = body.get("data", []) if isinstance(body, dict) else body
            return {"active_only": active_only,
                    "count": len(rows or []),
                    "alarms": rows or []}

    if on("emqx_prometheus_stats"):

        @mcp.tool(name="emqx_prometheus_stats", tags={"emqx", "read", "cluster"},
                  annotations=read_only("EMQX Prometheus Stats"))
        async def prometheus_stats(
            max_bytes: Annotated[int, Field(
                description="Truncate the exposition text at this many bytes.",
                ge=1024, le=262144)] = 65536,
            emqx: httpx.AsyncClient = Depends(emqx_client),
        ) -> dict[str, Any]:
            """Raw Prometheus exposition text. Large — prefer emqx_broker_stats."""
            resp = await emqx_request(emqx, "GET", "/prometheus/stats")
            text = resp.text
            return {"truncated": len(text) > max_bytes, "text": text[:max_bytes]}
