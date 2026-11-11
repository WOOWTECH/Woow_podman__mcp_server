"""Health dashboard router factory（hoist 自 n8n+odoo 同形骨架）。

各 app 注入兩個 async hook：
- probe_target(conn) -> {healthy, url, ...}
- fetch_info(conn)  -> {version, db_name, item_count}
hermes 為雙後端離群者，不使用本 factory（keep-app-local）。
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from fastapi import APIRouter

from mcp_admin_core.config import get_config_store
from mcp_admin_core.process import get_process_manager

logger = logging.getLogger(__name__)

ProbeTarget = Callable[[dict], Awaitable[dict]]
FetchInfo = Callable[[dict], Awaitable[dict]]


def mcp_server_health(pm_status: dict[str, Any]) -> dict[str, Any]:
    """``/api/health`` 的 ``mcp_server`` 區塊（各 app 共用，含 hermes）。

    除了手動 restart 次數，也帶看門狗狀態（商用驗收 DATA-NMCP-004-1：過去只在
    ``/api/settings/mcp/status`` 看得到）：自動重啟次數、是否已排程、距下次重試幾秒、
    子行程最後的 exit code。
    """
    running = bool(pm_status.get("running", False))
    return {
        "healthy": running,
        "process": f"pid={pm_status.get('pid')}" if running else "stopped",
        "restart_count": pm_status.get("restart_count", 0),
        "auto_restart_count": pm_status.get("auto_restart_count", 0),
        "auto_restart_pending": bool(pm_status.get("auto_restart_pending", False)),
        "next_auto_restart_in": pm_status.get("next_auto_restart_in"),
        "exit_code": None if running else pm_status.get("exit_code"),
    }


def make_health_router(
    *,
    app_type: str,
    probe_target: ProbeTarget,
    fetch_info: FetchInfo,
) -> APIRouter:
    router = APIRouter(prefix="/api/health", tags=["health"])

    @router.get("")
    async def get_health() -> dict[str, Any]:
        store = get_config_store()
        pm = get_process_manager()

        conn = await store.get("connection", {})
        pm_status = await pm.status()

        mcp_running = pm_status.get("running", False)
        mcp_server = mcp_server_health(pm_status)

        target_app = await probe_target(conn)
        proxy = {"healthy": True, "process": "built-in reverse proxy"}
        info = await fetch_info(conn)

        all_healthy = mcp_running and target_app.get("healthy", False)
        return {
            "app_type": app_type,
            "overall_status": "ok" if all_healthy else "degraded" if mcp_running or target_app.get("healthy") else "error",
            "mcp_server": mcp_server,
            "target_app": target_app,
            "proxy": proxy,
            "version": info.get("version"),
            "db_name": info.get("db_name"),
            "item_count": info.get("item_count"),
        }

    return router
