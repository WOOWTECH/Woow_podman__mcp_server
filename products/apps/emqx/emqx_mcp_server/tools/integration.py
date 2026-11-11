"""Rule engine, connectors and actions (data integration)."""

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
from ._common import destructive, page_of, path_segment, read_only, writing


def register(mcp: FastMCP, gate: ToolGate) -> None:
    on = gate.is_tool_enabled

    if on("emqx_list_rules"):

        @mcp.tool(name="emqx_list_rules", tags={"emqx", "read", "integration"},
                  annotations=read_only("List EMQX Rules"))
        async def list_rules(
            enabled_only: Annotated[bool, Field(
                description="Return only rules that are switched on.")] = False,
            page: Annotated[int, Field(description="1-based page number.", ge=1)] = 1,
            limit: Annotated[int, Field(
                description="Rows per page.", ge=1, le=200)] = settings.default_limit,
            emqx: httpx.AsyncClient = Depends(emqx_client),
        ) -> dict[str, Any]:
            """Rule-engine rules, with their SQL and enabled state.

            Rules are how EMQX forwards or reshapes messages without any code —
            for example pushing selected topics into a database or a webhook.
            """
            params: dict[str, Any] = {
                "page": page, "limit": min(limit, settings.max_limit)
            }
            if enabled_only:
                params["enable"] = "true"
            rows, meta = page_of(
                json_body(await emqx_request(emqx, "GET", "/rules", params=params)))
            return {"count": len(rows), "total": meta.get("count", len(rows)),
                    "rules": rows}

    if on("emqx_get_rule_metrics"):

        @mcp.tool(name="emqx_get_rule_metrics", tags={"emqx", "read", "integration"},
                  annotations=read_only("EMQX Rule Metrics"))
        async def rule_metrics(
            rule_id: Annotated[str, Field(
                description="Rule id as listed by emqx_list_rules.")],
            emqx: httpx.AsyncClient = Depends(emqx_client),
        ) -> dict[str, Any]:
            """Match, pass and failure counters for one rule.

            A rule with matched=0 is not seeing traffic — usually its FROM
            topic filter does not match what devices actually publish.
            """
            return json_body(await emqx_request(
                emqx, "GET", f"/rules/{path_segment(rule_id, 'rule_id')}/metrics"))

    if on("emqx_toggle_rule"):

        @mcp.tool(name="emqx_toggle_rule",
                  tags={"emqx", "write", "destructive", "integration"},
                  annotations=destructive("Enable Or Disable EMQX Rule"))
        async def toggle_rule(
            rule_id: Annotated[str, Field(description="Rule id to change.")],
            enable: Annotated[bool, Field(
                description="True to switch the rule on, False to switch it off.")],
            emqx: httpx.AsyncClient = Depends(emqx_client),
        ) -> dict[str, Any]:
            """[DESTRUCTIVE] Switch a rule on or off.

            Disabling a rule silently stops whatever pipeline depends on it —
            confirm which downstream system it feeds before turning it off.
            """
            await emqx_request(
                emqx, "PUT", f"/rules/{path_segment(rule_id, 'rule_id')}",
                json={"enable": enable})
            return {"rule_id": rule_id, "enabled": enable}

    if on("emqx_test_rule_sql"):

        @mcp.tool(name="emqx_test_rule_sql", tags={"emqx", "write", "integration"},
                  annotations=writing("Test EMQX Rule SQL (write permission)"))
        async def test_rule_sql(
            sql: Annotated[str, Field(
                description="Rule SQL to evaluate, e.g. "
                            "\"SELECT payload FROM 'sensors/#'\".")],
            topic: Annotated[str, Field(
                description="Topic of the sample event.")] = "t/1",
            payload: Annotated[str, Field(
                description="Payload of the sample event.")] = "{}",
            emqx: httpx.AsyncClient = Depends(emqx_client),
        ) -> dict[str, Any]:
            """Dry-run rule SQL against a sample event. Nothing is saved.

            Use this to check a rule before creating it — it reports whether
            the event would match and what columns the rule would emit.
            """
            body = json_body(await emqx_request(emqx, "POST", "/rule_test", json={
                "sql": sql,
                "context": {"topic": topic, "payload": payload,
                            "event_type": "message_publish"},
            }))
            return {"sql": sql, "matched": bool(body), "result": body}

    if on("emqx_list_connectors"):

        @mcp.tool(name="emqx_list_connectors", tags={"emqx", "read", "integration"},
                  annotations=read_only("List EMQX Connectors"))
        async def list_connectors(
            emqx: httpx.AsyncClient = Depends(emqx_client),
        ) -> dict[str, Any]:
            """Data-integration connectors and their connection health.

            Open-source EMQX ships HTTP and MQTT connectors; the rest are
            enterprise-only, so an empty list is normal on OSS.
            """
            body = json_body(await emqx_request(emqx, "GET", "/connectors"))
            rows, _ = page_of(body)
            return {"count": len(rows), "connectors": rows}

    if on("emqx_list_actions"):

        @mcp.tool(name="emqx_list_actions", tags={"emqx", "read", "integration"},
                  annotations=read_only("List EMQX Actions"))
        async def list_actions(
            emqx: httpx.AsyncClient = Depends(emqx_client),
        ) -> dict[str, Any]:
            """Outbound actions (bridges) attached to rules, with their health."""
            body = json_body(await emqx_request(emqx, "GET", "/actions"))
            rows, _ = page_of(body)
            return {"count": len(rows), "actions": rows}
