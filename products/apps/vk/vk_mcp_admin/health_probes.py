"""vk plug-specific health 探測（供 mcp_admin_core make_health_router 使用）。

形比照 apps/odoo-manage/odoo_manage_mcp_admin/health_probes.py，但換成 VK Host 語意：
單一連線鍵 vibe_backend_url、健康端點走 VK_HEALTH_PATH（Gate 2 定案 /api/health）。
VK Host 無 db/模組概念，db_name/item_count 恆 None，故不照抄 Odoo 那套 XML-RPC 版本／
模組數探測殘骸。
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# VK Host 健康端點路徑的單一來源——probe_target / fetch_info 與 config router 的 Test
# 一律引用此常數，嚴禁另寫死 "/api/health" 字串。端點已於 Gate 2 由 user 實測定案
# （產線 /api/health 可通、上游 root Dockerfile 的 /health 不可通），未來若再變只改
# 這一行與測試引用即可。
VK_HEALTH_PATH = "/api/health"


async def _check_vk_host(vk_url: str) -> dict[str, Any]:
    """打 VK Host 健康端點確認可達性；VK Host 現無認證，故不帶 header/token。"""
    if not vk_url:
        return {"healthy": False, "url": "", "error": "Not configured"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{vk_url.rstrip('/')}{VK_HEALTH_PATH}")
            return {
                "healthy": resp.status_code == 200,
                "url": vk_url,
                "status_code": resp.status_code,
            }
    except httpx.ConnectError:
        return {"healthy": False, "url": vk_url, "error": "Connection refused"}
    except httpx.TimeoutException:
        return {"healthy": False, "url": vk_url, "error": "Timed out"}
    except Exception as exc:
        return {"healthy": False, "url": vk_url, "error": str(exc)}


async def _get_vk_info(vk_url: str) -> dict[str, Any]:
    """best-effort 抓 VK Host 版本；vk 無 db/模組概念，db_name/item_count 恆 None。

    只在 HTTP 200 且 body 為 JSON dict 且 version 為 str 時才填 version，其餘一律
    None、**永不 raise**（連不上／逾時／非 JSON 都降級為 None，前端顯 N/A 或省略卡片）。
    """
    info: dict[str, Any] = {"version": None, "db_name": None, "item_count": None}
    if not vk_url:
        return info
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{vk_url.rstrip('/')}{VK_HEALTH_PATH}")
            if resp.status_code == 200:
                body = resp.json()
                if isinstance(body, dict) and isinstance(body.get("version"), str):
                    info["version"] = body["version"]
    except Exception as exc:
        # 版本抓取為 best-effort：連線層/逾時/非 JSON 一律降級，不讓 dashboard 因抓
        # 版本失敗而報錯（healthy 由 probe_target 另行判定）。
        logger.warning("Failed to get VK Host info (best-effort): %s", exc)
    return info


async def probe_target(conn: dict) -> dict[str, Any]:
    return await _check_vk_host(conn.get("vibe_backend_url", ""))


async def fetch_info(conn: dict) -> dict[str, Any]:
    return await _get_vk_info(conn.get("vibe_backend_url", ""))
