"""Authenticated capability snapshot: catalog ∪ observed child tools, without writes.

An effective tool is advertised by the child and allowed by the proxy policy.
This does not prove backend credentials, per-operation access, or public reachability.
"""
from __future__ import annotations

import asyncio
import json
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import httpx
from fastapi import APIRouter

from .config import get_config_store
from .process import get_process_manager
from .operation_policy import conditional_writes_for, operation_status, policies_for, read_policy

PROBE_TIMEOUT = 8.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_PAGES = 100


class DiscoveryError(Exception):
    """Incomplete or invalid discovery; never expose raw upstream error bodies."""


async def _rpc(client, url, headers, payload, session, budget) -> dict[str, Any]:
    async with client.stream("POST", url, headers=headers, json=payload) as response:
        response.raise_for_status()
        if response.headers.get("mcp-session-id"):
            session["Mcp-Session-Id"] = response.headers["mcp-session-id"]
        data = bytearray()
        sse = "text/event-stream" in response.headers.get("content-type", "")
        async for chunk in response.aiter_bytes():
            budget[0] += len(chunk)
            if budget[0] > MAX_RESPONSE_BYTES:
                raise DiscoveryError("response_limit")
            data.extend(chunk)
            if not sse:
                continue
            # SSE may remain open after the response. Stop at the matching ID;
            # skip notifications and support multi-line data plus CRLF framing.
            data = bytearray(data.replace(b"\r\n", b"\n"))
            while b"\n\n" in data:
                event, _, rest = data.partition(b"\n\n")
                data = bytearray(rest)
                lines = [line[5:].lstrip(b" ") for line in event.split(b"\n") if line.startswith(b"data:")]
                if lines:
                    message = json.loads(b"\n".join(lines))
                    if isinstance(message, dict) and message.get("id") == payload["id"]:
                        return _result(message, payload["id"])
        if sse:
            raise DiscoveryError("missing_response")
        return _result(json.loads(data), payload["id"])


def _result(message, request_id) -> dict[str, Any]:
    if (not isinstance(message, dict) or message.get("id") != request_id
            or "error" in message or not isinstance(message.get("result"), dict)):
        raise DiscoveryError("invalid_response")
    return message["result"]


async def discover_tools(config: dict, *, transport=None) -> list[dict]:
    """Bounded loopback initialize/list with pagination and session cleanup.

    Uses the same child port and bearer credential as the existing public proxy.
    No tool calls, redirects, environment HTTP proxies, or lifecycle mutations.
    """
    port = int(config.get("mcp_server", {}).get("port", 8000))
    url = f"http://127.0.0.1:{port}/mcp"
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    bearer = config.get("proxy", {}).get("bearer_token")
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    session: dict[str, str] = {}
    budget = [0]  # Bound total discovery bytes, including all pages.
    async with httpx.AsyncClient(timeout=PROBE_TIMEOUT, trust_env=False, transport=transport) as client:
        try:
            async with asyncio.timeout(PROBE_TIMEOUT):
                initialized = await _rpc(client, url, headers, {
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                               "clientInfo": {"name": "mcp-admin-capabilities", "version": "1"}},
                }, session, budget)
                protocol = initialized.get("protocolVersion")
                if not isinstance(protocol, str) or not protocol:
                    raise DiscoveryError("invalid_initialize")
                headers.update(session)
                headers["MCP-Protocol-Version"] = protocol
                response = await client.post(url, headers=headers,
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"})
                response.raise_for_status()
                tools: dict[str, dict] = {}
                cursors: set[str] = set()
                cursor = None
                for page in range(MAX_PAGES):
                    result = await _rpc(client, url, headers, {
                        "jsonrpc": "2.0", "id": page + 2, "method": "tools/list",
                        "params": {"cursor": cursor} if cursor is not None else {},
                    }, session, budget)
                    if not isinstance(result.get("tools"), list):
                        raise DiscoveryError("invalid_tools")
                    for tool in result["tools"]:
                        if (not isinstance(tool, dict) or not isinstance(tool.get("name"), str)
                                or not tool["name"] or not isinstance(tool.get("inputSchema"), dict)):
                            raise DiscoveryError("invalid_tool")
                        tools[tool["name"]] = tool
                    cursor = result.get("nextCursor")
                    if cursor is None:
                        return list(tools.values())
                    if not isinstance(cursor, str) or not cursor or cursor in cursors:
                        raise DiscoveryError("invalid_cursor")
                    cursors.add(cursor)
                raise DiscoveryError("page_limit")
        finally:
            if session:
                # DELETE may be unsupported (405); release sessions where supported.
                try:
                    async with asyncio.timeout(1):
                        await client.delete(url, headers={**headers, **session})
                except Exception:
                    pass


def tool_snapshot(registry, config: dict, observed: list[dict] | None, reason: str | None) -> dict:
    policy = config.get("tools", {})
    disabled, disabled_ops = read_policy(policy)
    specs = policies_for(registry)
    catalog = {tool.name: tool for tool in registry.TOOL_REGISTRY}
    runtime = {tool["name"]: tool for tool in observed or []}
    items = []
    # Retain denies for tools absent from both current sources; editing another
    # toggle must never drop an operator's saved deny for a temporarily absent tool.
    saved_operation_names = {name for name, operations in disabled_ops.items() if operations}
    for name in sorted(catalog.keys() | runtime.keys() | disabled | saved_operation_names):
        definition, tool = catalog.get(name), runtime.get(name)
        state = operation_status(specs.get(name), disabled_ops.get(name, []), name not in disabled)
        enabled = state["policy_enabled"]
        item = {
            "name": name,
            "description": (tool or {}).get("description") or getattr(definition, "description", ""),
            "category": getattr(getattr(definition, "category", None), "value", "Runtime tools" if tool else "Saved policy"),
            "dangerous": bool(getattr(definition, "dangerous", False) or ((tool or {}).get("annotations") or {}).get("destructiveHint")),
            **state,
            "conditional_writes": conditional_writes_for(registry, name, disabled, disabled_ops),
            "tool_policy_enabled": name not in disabled,
            "effective": (None if enabled else False) if observed is None else enabled and tool is not None,
            "runtime_only": definition is None and tool is not None,
        }
        if tool is not None:
            item["inputSchema"] = tool["inputSchema"]
        if not enabled:
            item["reason"] = "policy_disabled" if name in disabled else "operation_policy_disabled"
        elif observed is None:
            item["reason"] = reason or "child_probe_failed"
        elif tool is None:
            item["reason"] = "not_advertised_by_child"
        items.append(item)
    result = {"catalog_count": len(items), "policy_enabled_count": sum(t["policy_enabled"] for t in items), "effective_count": None if observed is None else sum(t["effective"] for t in items),
              "status": "unknown" if observed is None else "observed", "items": items,
              "operation_count": sum(len(item["operations"]) for item in items),
              "operation_policy_enabled_count": sum(operation["policy_enabled"] for item in items for operation in item["operations"]),
              "disabled_operations": disabled_ops}
    if reason:
        result["reason"] = reason
    return result


def create_capabilities_router(*, product_id: str, registry, deployment_kind: str = "paas",
                               product_version: str | None = None, operations_supported: bool = False,
                               backend_mode: str = "manual") -> APIRouter:
    router = APIRouter(tags=["capabilities"])
    if product_version is None:
        try:
            product_version = version("n8n-mcp-admin" if product_id == "n8n" else f"woow-{product_id}-mcp-server")
        except PackageNotFoundError:
            product_version = "unknown"

    @router.get("/api/capabilities")
    async def capabilities() -> dict:
        refresh = getattr(registry, "refresh", None)
        if refresh:
            try:
                async with asyncio.timeout(2):
                    await refresh()
            except Exception:
                pass  # Keep the full fallback catalog; effective comes only from the child.
        store = get_config_store()
        config = await store.load()
        pm = get_process_manager()
        before = await pm.status()
        observed, reason = None, "child_stopped"
        if before.get("running"):
            try:
                observed = await discover_tools(config)
                reason = None
            except Exception:
                reason = "child_probe_failed"
        after = await pm.status()
        latest = await store.load()
        identity_keys = ("running", "pid", "restart_count", "auto_restart_count")
        if (any(before.get(k) != after.get(k) for k in identity_keys)
                or any(config.get(k) != latest.get(k) for k in ("mcp_server", "connection", "tools", "proxy"))):
            observed, reason = None, "state_changed_during_probe"
        return {
            "schema_version": 1,
            "deployment": {"kind": deployment_kind},
            "product": {"id": product_id, "version": product_version},
            "admin": {"authority": "paas-password"},
            "backend": {"mode": backend_mode, "can_test": True, "can_edit": backend_mode == "manual"},
            "capabilities": {"tool_schema": True, "effective_tools": True, "operation_policy": bool(policies_for(registry)),
                             "logs": True, "restart_child": True, "revoke_token": True},
            "health": {"management": "ready", "child": "running" if after.get("running") else "stopped",
                       "backend": "unknown", "public_endpoint": "unverified"},
            "tools": tool_snapshot(registry, latest, observed, reason),
        }

    return router
