"""Health dashboard router for Hermes MCP Admin.

Returns dual health data checking both Gateway and Dashboard APIs,
plus summary counts for the Dashboard cards (model, tools, skills,
sessions, mcp_servers).

Endpoints:
    GET /api/health - Dashboard health data with dual health checks
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from fastapi import APIRouter

from mcp_admin_core.config import get_config_store
from mcp_admin_core.process import get_process_manager
from mcp_admin_core.routers.health import mcp_server_health

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/health", tags=["health"])

# Cookie cache shared with dashboard_proxy
_cookie_cache: dict[str, tuple[str, float]] = {}


async def _check_gateway(gateway_url: str, api_key: str) -> dict[str, Any]:
    """Check Hermes Gateway health via GET /health with Bearer auth."""
    if not gateway_url:
        return {"healthy": False, "url": "", "error": "Not configured"}
    url = f"{gateway_url.rstrip('/')}/health"
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            return {"healthy": True, "url": gateway_url, "status_code": resp.status_code}
    except httpx.ConnectError:
        return {"healthy": False, "url": gateway_url, "error": "Connection refused"}
    except httpx.TimeoutException:
        return {"healthy": False, "url": gateway_url, "error": "Timed out"}
    except httpx.HTTPStatusError as exc:
        return {"healthy": False, "url": gateway_url, "error": f"HTTP {exc.response.status_code}"}
    except Exception as exc:
        return {"healthy": False, "url": gateway_url, "error": str(exc)}


async def _dashboard_login(dashboard_url: str, username: str, password: str) -> str:
    """Login to Dashboard and return session cookie (cached 10 min)."""
    cached = _cookie_cache.get(dashboard_url)
    if cached and cached[1] > time.time():
        return cached[0]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{dashboard_url.rstrip('/')}/auth/password-login",
                json={"provider": "basic", "username": username, "password": password},
            )
            resp.raise_for_status()
            for h in resp.headers.get_list("set-cookie"):
                if "hermes_session_at=" in h:
                    cookie = h.split("hermes_session_at=")[1].split(";")[0].strip('"')
                    _cookie_cache[dashboard_url] = (cookie, time.time() + 600)
                    return cookie
            return ""
    except Exception:
        return ""


async def _check_dashboard(dashboard_url: str, username: str, password: str) -> dict[str, Any]:
    """Check Hermes Dashboard health via cookie-based auth.

    Returns both health status and the /api/status data for reuse.
    """
    if not dashboard_url:
        return {"healthy": False, "url": "", "error": "Not configured"}
    try:
        cookie = await _dashboard_login(dashboard_url, username, password)
        if not cookie:
            return {"healthy": False, "url": dashboard_url, "error": "Login failed"}
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{dashboard_url.rstrip('/')}/api/status",
                headers={"Cookie": f'hermes_session_at="{cookie}"'},
            )
            resp.raise_for_status()
            status_data = resp.json() if resp.status_code == 200 else {}
            return {
                "healthy": True,
                "url": dashboard_url,
                "status_code": resp.status_code,
                "_status": status_data,  # carry status data for reuse
            }
    except httpx.ConnectError:
        return {"healthy": False, "url": dashboard_url, "error": "Connection refused"}
    except httpx.TimeoutException:
        return {"healthy": False, "url": dashboard_url, "error": "Timed out"}
    except httpx.HTTPStatusError as exc:
        return {"healthy": False, "url": dashboard_url, "error": f"HTTP {exc.response.status_code}"}
    except Exception as exc:
        return {"healthy": False, "url": dashboard_url, "error": str(exc)}


@router.get("")
async def get_health() -> dict[str, Any]:
    """Return health data in the format the Dashboard frontend expects."""
    store = get_config_store()
    pm = get_process_manager()

    conn = await store.get("connection", {})
    gateway_url = conn.get("gateway_url", "")
    gateway_key = conn.get("gateway_api_key", "")
    dashboard_url = conn.get("dashboard_url", "")
    dashboard_user = conn.get("dashboard_username", "")
    dashboard_pw = conn.get("dashboard_password", "")

    pm_status = await pm.status()

    # MCP server status
    mcp_server = mcp_server_health(pm_status)

    # Gateway health
    gateway_health = await _check_gateway(gateway_url, gateway_key)

    # Dashboard health (includes /api/status data)
    dashboard_health = await _check_dashboard(dashboard_url, dashboard_user, dashboard_pw)

    # Extract /api/status data if available
    status_data = dashboard_health.pop("_status", {}) if dashboard_health.get("healthy") else {}

    # Version from /api/status
    hermes_version = status_data.get("version") if isinstance(status_data, dict) else None

    gw_healthy = gateway_health.get("healthy", False)
    db_healthy = dashboard_health.get("healthy", False)

    if gw_healthy and db_healthy:
        overall = "ok"
    elif gw_healthy or db_healthy:
        overall = "degraded"
    else:
        overall = "error"

    # Summary data from Dashboard
    model_info: dict[str, Any] = {}
    tools_info: dict[str, Any] = {}
    skills_info: dict[str, Any] = {}
    mcp_servers_info: dict[str, Any] = {}
    sessions_info: dict[str, Any] = {}

    if db_healthy:
        try:
            cookie = await _dashboard_login(dashboard_url, dashboard_user, dashboard_pw)
            if cookie:
                headers = {"Cookie": f'hermes_session_at="{cookie}"'}
                async with httpx.AsyncClient(timeout=10.0) as client:
                    # /api/config → model, toolsets, lsp servers
                    try:
                        r = await client.get(
                            f"{dashboard_url.rstrip('/')}/api/config", headers=headers,
                        )
                        if r.status_code == 200:
                            cfg = r.json()
                            if isinstance(cfg, dict):
                                # Model: may be a string or empty
                                model_val = cfg.get("model", "")
                                model_name = str(model_val) if model_val else "Default"
                                # Provider: try providers dict, fallback to model name
                                providers_cfg = cfg.get("providers", {})
                                provider = ""
                                if isinstance(providers_cfg, dict) and providers_cfg:
                                    provider = next(iter(providers_cfg))
                                elif model_name.startswith("MiniMax"):
                                    provider = "minimax"
                                elif model_name.startswith("gpt"):
                                    provider = "openai"
                                elif model_name.startswith("claude"):
                                    provider = "anthropic"
                                model_info = {
                                    "provider": provider or "auto",
                                    "name": model_name,
                                }
                                # Toolsets count
                                toolsets = cfg.get("toolsets", [])
                                if isinstance(toolsets, list):
                                    tools_info = {
                                        "enabled": len(toolsets),
                                        "total": len(toolsets),
                                    }
                                # MCP servers from lsp.servers
                                lsp = cfg.get("lsp", {})
                                if isinstance(lsp, dict):
                                    servers = lsp.get("servers", {})
                                    if isinstance(servers, dict):
                                        mcp_servers_info = {"count": len(servers)}
                    except Exception:
                        pass

                    # /api/skills
                    try:
                        r = await client.get(
                            f"{dashboard_url.rstrip('/')}/api/skills", headers=headers,
                        )
                        if r.status_code == 200:
                            sdata = r.json()
                            if isinstance(sdata, list):
                                enabled = sum(
                                    1
                                    for s in sdata
                                    if isinstance(s, dict) and s.get("enabled", True)
                                )
                                skills_info = {
                                    "enabled": enabled,
                                    "total": len(sdata),
                                }
                    except Exception:
                        pass

                    # Sessions: use active_sessions from status, total from /api/sessions
                    active = (
                        status_data.get("active_sessions", 0)
                        if isinstance(status_data, dict)
                        else 0
                    )
                    try:
                        r = await client.get(
                            f"{dashboard_url.rstrip('/')}/api/sessions",
                            headers=headers,
                        )
                        if r.status_code == 200:
                            sess = r.json()
                            if isinstance(sess, dict):
                                sessions_info = {
                                    "active": active,
                                    "total": sess.get("total", 0),
                                    "recent": sess.get("sessions", [])[:5],
                                }
                            elif isinstance(sess, list):
                                sessions_info = {
                                    "active": active,
                                    "total": len(sess),
                                    "recent": sess[:5],
                                }
                    except Exception:
                        sessions_info = {"active": active}
        except Exception as exc:
            logger.debug("Failed to get Dashboard summary: %s", exc)

    return {
        "app_type": "hermes",
        "overall_status": overall,
        "mcp_server": mcp_server,
        "gateway": gateway_health,
        "dashboard": dashboard_health,
        "gateway_health": gateway_health,
        "dashboard_health": dashboard_health,
        "model": model_info,
        "tools": tools_info,
        "skills": skills_info,
        "mcp_servers": mcp_servers_info,
        "sessions": sessions_info,
        "hermes_version": hermes_version,
        "version": hermes_version,
    }
