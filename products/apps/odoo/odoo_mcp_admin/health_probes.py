"""odoo plug-specific health 探測（供 mcp_admin_core make_health_router 使用）。"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

import httpx

from mcp_admin_core.util.net_errors import describe_connect_error
from mcp_admin_core.util.xmlrpc_async import make_proxy, run_blocking

logger = logging.getLogger(__name__)


async def _check_odoo(odoo_url: str) -> dict[str, Any]:
    """Check Odoo health via /web/health."""
    if not odoo_url:
        return {"healthy": False, "url": "", "error": "Not configured"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{odoo_url.rstrip('/')}/web/health")
            return {
                "healthy": resp.status_code == 200,
                "url": odoo_url,
                "status_code": resp.status_code,
            }
    except httpx.ConnectError as exc:
        # DATA-OMCP-007-2：DNS 失敗、拒絕連線、路由不通要分開報，不再一律說 Connection refused。
        host = urlsplit(odoo_url).hostname or ""
        return {"healthy": False, "url": odoo_url, "error": describe_connect_error(exc, host=host)}
    except httpx.TimeoutException:
        return {"healthy": False, "url": odoo_url, "error": "Timed out"}
    except Exception as exc:
        return {"healthy": False, "url": odoo_url, "error": str(exc)}


def _get_odoo_info_sync(odoo_url: str, db: str, username: str, password: str) -> dict[str, Any]:
    """同步版（在工作緒內執行）：每個 RPC 都帶 socket 逾時。"""
    info: dict[str, Any] = {"version": None, "db_name": db, "item_count": None}
    if not odoo_url or not username:
        return info
    try:
        import xmlrpc.client
        base = odoo_url.rstrip("/")
        common = make_proxy(f"{base}/xmlrpc/2/common")
        ver = common.version()
        info["version"] = ver.get("server_version", "unknown") if isinstance(ver, dict) else str(ver)

        # Odoo 18+ changed authenticate() signature: (db, credential_dict, user_agent_env)
        # Try new format first, fall back to old format for Odoo <=17 compatibility.
        try:
            result = common.authenticate(
                db,
                {"login": username, "password": password, "type": "password"},
                {},
            )
        except (xmlrpc.client.Fault, TypeError):
            result = common.authenticate(db, username, password, {})
        uid = result.get("uid") if isinstance(result, dict) else result
        if uid:
            models = make_proxy(f"{base}/xmlrpc/2/object")
            count = models.execute_kw(db, uid, password, "ir.module.module", "search_count", [[["state", "=", "installed"]]])
            info["item_count"] = count
    except Exception as exc:
        logger.debug("Failed to get Odoo info: %s", exc)
    return info


async def _get_odoo_info(odoo_url: str, db: str, username: str, password: str) -> dict[str, Any]:
    """Get Odoo version and module count via XML-RPC.

    DATA-OMCP-007-1：同步 XML-RPC 不可在事件迴圈上直接跑——Odoo URL 指到黑洞位址時會卡到
    TCP 逾時，連 /healthz 都回不出去而被 liveness 殺掉。改丟工作緒並加總期限。
    """
    try:
        return await run_blocking(_get_odoo_info_sync, odoo_url, db, username, password)
    except Exception as exc:  # asyncio.TimeoutError 等：回空資訊，不影響 health 回應
        logger.debug("Odoo info probe gave up: %r", exc)
        return {"version": None, "db_name": db, "item_count": None}


async def probe_target(conn: dict) -> dict[str, Any]:
    return await _check_odoo(conn.get("odoo_url", ""))


async def fetch_info(conn: dict) -> dict[str, Any]:
    return await _get_odoo_info(
        conn.get("odoo_url", ""), conn.get("odoo_db", ""),
        conn.get("odoo_username", ""), conn.get("odoo_password", ""),
    )
