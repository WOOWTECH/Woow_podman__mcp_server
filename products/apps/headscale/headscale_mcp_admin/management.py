"""Authenticated GUI operations sharing the native MCP validators and policy."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from mcp_admin_core.config import get_config_store
from mcp_admin_core.operation_policy import read_policy
from headscale_mcp_server.client import HeadscaleClient
from headscale_mcp_server.errors import HeadscaleError

router = APIRouter(prefix="/api/headscale", tags=["Headscale management"])


async def client_from_saved():
    conn = await get_config_store().get("connection", {}) or {}
    if not conn.get("base_url") or not conn.get("api_key"):
        raise HTTPException(409, "HEADSCALE_SETUP_REQUIRED")
    return HeadscaleClient(conn["base_url"], conn["api_key"], allowed_private_cidrs=tuple(
        s.strip() for s in os.environ.get("HEADSCALE_MCP_ALLOWED_PRIVATE_CIDRS", "").split(",") if s.strip()))


@router.post("/connection/test")
async def test_connection():
    async with await client_from_saved() as client:
        # A health response alone cannot verify that this is a complete management API.
        health, users = await asyncio.gather(client.request("Health"), client.request("ListUsers"))
    valid = health.get("databaseConnectivity") is True and isinstance(users.get("users"), list)
    return {"verified": valid, "verified_at": datetime.now(timezone.utc).isoformat(),
            "checks": {"database": health.get("databaseConnectivity") is True, "authenticated_api": valid},
            "scope": "control_plane_api_only", "client_connectivity": "not_tested"}


@router.get("/overview")
async def overview(response: Response):
    response.headers["Cache-Control"] = "no-store"
    async with await client_from_saved() as client:
        health, users, nodes = await asyncio.gather(
            client.request("Health"), client.request("ListUsers"), client.request("ListNodes"))
    return {"database": health.get("databaseConnectivity"), "users": users.get("users", []),
            "nodes": nodes.get("nodes", []), "scope": "control_plane_api_only",
            "capabilities": {"native_api_version": "0.29.4", "native_operations": 29,
                             "client_daemon_control": False, "employee_password_signup": False,
                             "headplane_local_roles_api": False,
                             "server_config_source": "platform_or_deployment"}}


@router.get("/catalog")
async def catalog(response: Response):
    from . import tool_registry as registry
    response.headers["Cache-Control"] = "no-store"
    disabled, denied_operations = read_policy((await get_config_store().load()).get("tools", {}))
    return {"tools": [{"name": t.name, "category": t.category.value, "description": t.description,
                       "dangerous": t.dangerous, "access": t.access, "operations": list(t.operations),
                       "enabled": t.name not in disabled,
                       "denied_operations": denied_operations.get(t.name, []),
                       "input_schema": registry.input_schema(t.name)} for t in registry.TOOL_REGISTRY]}


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    arguments: dict = Field(default_factory=dict)


@router.post("/tools/{name}")
async def call_tool(name: str, payload: ToolCall, response: Response):
    from . import tool_registry as registry
    from headscale_mcp_server.server import execute_tool
    response.headers["Cache-Control"] = "no-store"
    snapshot = await get_config_store().load()
    disabled, denied_operations = read_policy(snapshot.get("tools", {}))
    try:
        arguments = registry.normalize_arguments(name, payload.arguments)
        spec = registry.OPERATION_POLICIES[name]
        requested = spec.resolve(arguments)
        allowed = name not in disabled and requested is not None and not requested & spec.denied(denied_operations.get(name, []))
    except (ValueError, KeyError):
        raise HTTPException(422, "HEADSCALE_INVALID_ARGUMENTS") from None
    if not allowed:
        raise HTTPException(403, "HEADSCALE_POLICY_DENIED")
    from .platform import NAME, execute_platform_tool
    if name == NAME:
        return await execute_platform_tool(arguments, snapshot.get("connection", {}), config_path=get_config_store().path)
    async with await client_from_saved() as client:
        return await execute_tool(name, arguments, client, config_path=get_config_store().path)


async def headscale_error(request, exc: HeadscaleError):
    return JSONResponse(status_code=409 if exc.outcome == "unknown" else 422,
                        content={"detail": exc.as_dict()}, headers={"Cache-Control": "no-store"})
