"""LiteLLM 專屬 health 探測（供 mcp_admin_core ``make_health_router`` 與連線 Test 共用）。

連線鍵（config.json ``connection``）：
- ``base_url``：LiteLLM 網址，**不含 /v1**（例 ``http://svc-xxxxxxxx-litellm:4000``）。
- ``master_key``：送成 ``Authorization: Bearer``（建議用權限較小的 key，見 README）。

判定規則（沿用來源 repo 的教訓）：
- **healthy 只由帶 key 的 ``GET {base}/v1/models`` 決定**。``/health/readiness`` 不驗 key，
  拿它判斷會在 key 錯／過期／沒填時照樣顯示綠燈，然後 40 支工具全部 401。
- ``/health/readiness`` 只拿來補細節（DB 狀態；舊版 LiteLLM 也帶版本），永遠不決定成敗、永不 raise。
- 版本：LiteLLM 1.103.x 的 readiness 已不回 ``litellm_version``，改讀免認證的 ``/openapi.json``
  ``info.version``（約 1.6 MB，故每個 base_url 快取 10 分鐘；讀不到就 None）。

回傳 dict 不含 key 本身；錯誤訊息只帶 URL 與 HTTP 狀態。
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from woow_litellm_mcp_server.urls import normalize_base_url

logger = logging.getLogger(__name__)

# 單一來源：probe／fetch_info／config router 的 Test 都引用這兩個常數。
MODELS_PATH = "/v1/models"
READINESS_PATH = "/health/readiness"
OPENAPI_PATH = "/openapi.json"

PROBE_TIMEOUT = 8.0
VERSION_CACHE_TTL = 600.0
_version_cache: dict[str, tuple[float, str | None]] = {}


def connection_target(conn: dict | None) -> tuple[str, str]:
    """從 connection 取出 (base_url, master_key)；base_url 去尾斜線。"""
    conn = conn or {}
    base = normalize_base_url(str(conn.get("base_url") or ""))
    key = str(conn.get("master_key") or "").strip()
    return base, key


async def readiness(base: str, *, transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """``GET {base}/health/readiness``（免認證）——只給細節，失敗回 {}。"""
    if not base:
        return {}
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT, transport=transport) as client:
            resp = await client.get(f"{base}{READINESS_PATH}")
        body = resp.json() if resp.status_code < 400 else {}
    except Exception:  # noqa: BLE001 — detail only, never fatal
        return {}
    return body if isinstance(body, dict) else {}


async def gateway_version(
    base: str, ready: dict[str, Any], *, transport: httpx.AsyncBaseTransport | None = None
) -> str | None:
    """LiteLLM 版本：readiness 有就用；否則讀 /openapi.json 的 info.version（快取、永不 raise）。"""
    version = ready.get("litellm_version") or ready.get("version")
    if isinstance(version, str) and version:
        return version
    now = time.monotonic()
    hit = _version_cache.get(base)
    if hit is not None and now - hit[0] < VERSION_CACHE_TTL:
        return hit[1]
    version = None
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT, transport=transport) as client:
            resp = await client.get(f"{base}{OPENAPI_PATH}")
        if resp.status_code == 200:
            info = resp.json().get("info")
            if isinstance(info, dict) and isinstance(info.get("version"), str):
                version = info["version"]
    except Exception:  # noqa: BLE001 — detail only, never fatal
        version = None
    _version_cache[base] = (now, version)
    return version


async def check_gateway(
    base: str, key: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Any]:
    """打帶 key 的 ``/v1/models`` 判定 healthy，再以 readiness 補版本／DB。

    回傳形狀（dashboard ``target_app`` 與 Test 共用）::

        {healthy, url, error, auth_ok, status_code, model_count, version, db, status}
    """
    result: dict[str, Any] = {
        "healthy": False,
        "url": base,
        "error": None,
        "auth_ok": None,
        "status_code": None,
        "model_count": None,
        "version": None,
        "db": None,
        "status": None,
    }
    if not base:
        result["error"] = "Not configured（尚未設定 LiteLLM 網址）"
        return result

    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT, headers=headers, transport=transport) as client:
            resp = await client.get(f"{base}{MODELS_PATH}")
    except httpx.ConnectError:
        result["error"] = f"Connection refused（連不到 {base}）"
        return result
    except httpx.TimeoutException:
        result["error"] = f"Timed out（{base} 逾時）"
        return result
    except Exception as exc:  # noqa: BLE001 — surfaced to the dashboard, not raised
        result["error"] = f"Cannot reach LiteLLM at {base}: {type(exc).__name__}"
        return result

    result["status_code"] = resp.status_code
    ready = await readiness(base, transport=transport)
    result["version"] = await gateway_version(base, ready, transport=transport)
    result["db"] = ready.get("db")
    result["status"] = ready.get("status")

    if resp.status_code in (401, 403):
        result["auth_ok"] = False
        result["error"] = (
            f"LiteLLM 拒絕了設定的金鑰（HTTP {resp.status_code}）"
            if key
            else f"尚未設定 LiteLLM 金鑰（HTTP {resp.status_code}）"
        )
        return result
    if resp.status_code >= 400:
        result["error"] = f"LiteLLM 回 HTTP {resp.status_code}（{MODELS_PATH}）——網址是否多帶了 /v1？"
        return result

    try:
        data = resp.json().get("data")
    except Exception:  # noqa: BLE001
        data = None
    result["model_count"] = len(data) if isinstance(data, list) else None
    result["auth_ok"] = True
    result["healthy"] = True
    return result


async def probe_target(conn: dict) -> dict[str, Any]:
    return await check_gateway(*connection_target(conn))


async def fetch_info(conn: dict) -> dict[str, Any]:
    """dashboard 細節：version＝LiteLLM 版本、db_name＝DB 狀態、item_count＝模型數。

    best-effort、永不 raise；模型數要帶 key 才拿得到。
    """
    base, key = connection_target(conn)
    info: dict[str, Any] = {"version": None, "db_name": None, "item_count": None}
    if not base:
        return info
    try:
        probe = await check_gateway(base, key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to get LiteLLM info (best-effort): %s", type(exc).__name__)
        return info
    info["version"] = probe.get("version")
    info["db_name"] = probe.get("db")
    info["item_count"] = probe.get("model_count")
    return info
