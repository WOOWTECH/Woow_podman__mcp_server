"""n8n plug-specific health 探測（供 mcp_admin_core make_health_router 使用）。"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from mcp_admin_core.config import get_config_store
from mcp_admin_core.util.ssrf import check_url

logger = logging.getLogger(__name__)


async def _check_n8n(api_url: str, api_key: str) -> dict[str, Any]:
    """Check n8n health via GET /rest/settings."""
    if not api_url:
        return {"healthy": False, "url": "", "error": "Not configured"}
    # #1530：對齊 n8n-mcp 子行程的 SSRF 規則（私網／loopback／metadata 會被子行程擋下）
    ssrf = await check_url(api_url)
    if not ssrf.ok:
        return {"healthy": False, "url": api_url, "error": f"Blocked by SSRF protection: {ssrf.reason}"}
    url = f"{api_url.rstrip('/')}/rest/settings"
    headers: dict[str, str] = {}
    if api_key:
        headers["X-N8N-API-KEY"] = api_key
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            return {"healthy": True, "url": api_url, "status_code": resp.status_code}
    except httpx.ConnectError:
        return {"healthy": False, "url": api_url, "error": "Connection refused"}
    except httpx.TimeoutException:
        return {"healthy": False, "url": api_url, "error": "Timed out"}
    except httpx.HTTPStatusError as exc:
        return {"healthy": False, "url": api_url, "error": f"HTTP {exc.response.status_code}"}
    except Exception as exc:
        return {"healthy": False, "url": api_url, "error": str(exc)}


async def _get_n8n_info(api_url: str, api_key: str) -> dict[str, Any]:
    """Get n8n version and workflow count."""
    info: dict[str, Any] = {"version": None, "db_name": "n8n", "item_count": None}
    if not api_url or not (await check_url(api_url)).ok:
        return info
    headers: dict[str, str] = {}
    if api_key:
        headers["X-N8N-API-KEY"] = api_key
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Get version — /rest/settings returns versionCli only with session auth.
            # Try public settings first, then fall back to n8n-mcp /health endpoint.
            resp = await client.get(f"{api_url.rstrip('/')}/rest/settings", headers=headers)
            if resp.status_code == 200:
                body = resp.json()
                settings = body.get("data", body) if isinstance(body, dict) else {}
                ver = settings.get("versionCli")
                if ver:
                    info["version"] = ver

            # Fallback: check n8n-mcp server /health which reports n8n version
            if not info["version"]:
                store = get_config_store()
                mcp_cfg = await store.get("mcp_server", {})
                mcp_port = mcp_cfg.get("port", 3000)
                try:
                    mcp_health = await client.get(f"http://127.0.0.1:{mcp_port}/health", timeout=3)
                    if mcp_health.status_code == 200:
                        mh = mcp_health.json()
                        info["version"] = mh.get("version", mh.get("n8nVersion"))
                except Exception:
                    pass

            # Get workflow count via API
            try:
                resp2 = await client.get(f"{api_url.rstrip('/')}/api/v1/workflows", headers=headers)
                if resp2.status_code == 200:
                    data = resp2.json()
                    if isinstance(data, dict) and "data" in data:
                        info["item_count"] = len(data["data"])
                    elif isinstance(data, list):
                        info["item_count"] = len(data)
            except Exception:
                pass
    except Exception as exc:
        logger.debug("Failed to get n8n info: %s", exc)
    return info


async def probe_target(conn: dict) -> dict[str, Any]:
    return await _check_n8n(conn.get("n8n_api_url", ""), conn.get("n8n_api_key", ""))


async def fetch_info(conn: dict) -> dict[str, Any]:
    return await _get_n8n_info(conn.get("n8n_api_url", ""), conn.get("n8n_api_key", ""))
