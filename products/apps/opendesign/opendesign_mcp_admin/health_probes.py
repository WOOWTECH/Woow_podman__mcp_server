"""opendesign 專屬 health 探測（供 mcp_admin_core make_health_router 使用）。

形比照 apps/vk/vk_mcp_admin/health_probes.py，但換成 Open Design daemon 語意：
單一連線鍵 od_api_base、健康端點走 OD_HEALTH_PATH（/api/health）。
Open Design daemon 無 db/模組概念，db_name/item_count 恆 None，故不照抄 Odoo 那套
XML-RPC 版本／模組數探測殘骸。
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from opendesign_mcp_admin.basic_auth import resolve_target

logger = logging.getLogger(__name__)

# Open Design daemon 健康端點路徑的單一來源——probe_target / fetch_info 與 config
# router 的 Test 一律引用此常數，嚴禁另寫死 "/api/health" 字串，未來若再變只改這一行
# 與測試引用即可。daemon 自身的 /api/health 免認證；但 PaaS 的 open-design 前面有 basic auth
# （#1354），故 probe 會帶 od_basic_user／od_basic_password 組的 BasicAuth（未設則不帶）。
OD_HEALTH_PATH = "/api/health"


async def _check_daemon(od_url: str, auth: httpx.BasicAuth | None = None) -> dict[str, Any]:
    """打 Open Design daemon 健康端點確認可達性；有設 basic auth 就帶上（od_url 須已去掉 userinfo）。"""
    if not od_url:
        return {"healthy": False, "url": "", "error": "Not configured"}
    try:
        async with httpx.AsyncClient(timeout=10.0, auth=auth) as client:
            resp = await client.get(f"{od_url.rstrip('/')}{OD_HEALTH_PATH}")
            result: dict[str, Any] = {
                "healthy": resp.status_code == 200,
                "url": od_url,
                "status_code": resp.status_code,
            }
            if resp.status_code == 401:
                result["error"] = "Unauthorized（Open Design 前置 basic auth：請確認帳號／密碼）"
            return result
    except httpx.ConnectError:
        return {"healthy": False, "url": od_url, "error": "Connection refused"}
    except httpx.TimeoutException:
        return {"healthy": False, "url": od_url, "error": "Timed out"}
    except Exception as exc:
        return {"healthy": False, "url": od_url, "error": str(exc)}


async def _get_daemon_info(od_url: str, auth: httpx.BasicAuth | None = None) -> dict[str, Any]:
    """best-effort 抓 Open Design daemon 版本；無 db/模組概念，db_name/item_count 恆 None。

    只在 HTTP 200 且 body 為 JSON dict 且 version 為 str 時才填 version，其餘一律
    None、**永不 raise**（連不上／逾時／非 JSON 都降級為 None，前端顯 N/A 或省略卡片）。
    """
    info: dict[str, Any] = {"version": None, "db_name": None, "item_count": None}
    if not od_url:
        return info
    try:
        async with httpx.AsyncClient(timeout=10.0, auth=auth) as client:
            resp = await client.get(f"{od_url.rstrip('/')}{OD_HEALTH_PATH}")
            if resp.status_code == 200:
                body = resp.json()
                if isinstance(body, dict) and isinstance(body.get("version"), str):
                    info["version"] = body["version"]
    except Exception as exc:
        # 版本抓取為 best-effort：連線層/逾時/非 JSON 一律降級，不讓 dashboard 因抓
        # 版本失敗而報錯（healthy 由 probe_target 另行判定）。
        logger.warning("Failed to get Open Design daemon info (best-effort): %s", exc)
    return info


async def probe_target(conn: dict) -> dict[str, Any]:
    # resolve_target：URL 去掉 userinfo（dashboard 顯示與錯誤訊息都不外露密碼）＋組 BasicAuth
    return await _check_daemon(*resolve_target(conn))


async def fetch_info(conn: dict) -> dict[str, Any]:
    return await _get_daemon_info(*resolve_target(conn))
