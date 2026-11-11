"""Nextcloud 專屬 health 探測（供 mcp_admin_core ``make_health_router`` 與連線 Test 共用）。

連線鍵（config.json ``connection``）：``base_url``、``username``、``app_password``（secret）。

判定規則：
- **healthy 只由一次真正帶帳密的唯讀工具呼叫決定**：直接用 vendored ``NextcloudTools.get_file_tree``
  （path ``""``、depth 1）——和子行程走同一段程式（OCS 解析 user id → WebDAV PROPFIND 家目錄），
  所以網址、帳號、app password、反向代理轉址任何一項有錯，這裡就會紅燈，訊息也和工具錯誤一致。
  不會寫入任何東西，也不需要子行程在跑。
- ``/status.php``（免認證）只拿來補 Nextcloud 版本，永遠不決定成敗、永不 raise；每個網址快取 10 分鐘。

回傳 dict 不含 app password；錯誤訊息來自 vendored 錯誤模型（保證不含憑證、header 或長段伺服器回應）。
"""
from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

import httpx
from fastmcp.exceptions import ToolError

from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.errors import AUTH_MESSAGE
from nextcloud_mcp_server.settings import SettingsError, load_settings
from nextcloud_mcp_server.tools import NextcloudTools

logger = logging.getLogger(__name__)

STATUS_PATH = "/status.php"
PROBE_TIMEOUT = 8.0
VERSION_CACHE_TTL = 600.0
_version_cache: dict[str, tuple[float, str | None]] = {}

# /api/health 會先後呼叫 probe_target 與 fetch_info；同一組連線在幾秒內重用同一次探測結果，
# 不對 Nextcloud 連打兩輪。key 以雜湊表示，記憶體裡不另存 app password。
PROBE_REUSE_SECONDS = 5.0
_last_probe: tuple[str, float, dict[str, Any]] | None = None


def _probe_key(base: str, user: str, password: str) -> str:
    return hashlib.sha256(f"{base}\0{user}\0{password}".encode()).hexdigest()


def connection_target(conn: dict | None) -> tuple[str, str, str]:
    """從 connection 取出 (base_url, username, app_password)；base_url 去尾斜線。"""
    conn = conn or {}
    base = str(conn.get("base_url") or "").strip().rstrip("/")
    user = str(conn.get("username") or "").strip()
    password = str(conn.get("app_password") or "").strip()
    return base, user, password


async def server_version(base: str, *, transport: httpx.AsyncBaseTransport | None = None) -> str | None:
    """``GET {base}/status.php`` 的 ``versionstring``（免認證、快取、永不 raise）。"""
    if not base:
        return None
    now = time.monotonic()
    hit = _version_cache.get(base)
    if hit is not None and now - hit[0] < VERSION_CACHE_TTL:
        return hit[1]
    version = None
    try:
        async with httpx.AsyncClient(
            timeout=PROBE_TIMEOUT, transport=transport, follow_redirects=False, trust_env=False
        ) as client:
            resp = await client.get(f"{base}{STATUS_PATH}")
        if resp.status_code == 200:
            body = resp.json()
            if isinstance(body, dict):
                raw = body.get("versionstring") or body.get("version")
                version = raw if isinstance(raw, str) and raw else None
    except Exception:  # noqa: BLE001 — detail only, never fatal
        version = None
    _version_cache[base] = (now, version)
    return version


async def check_nextcloud(
    base: str,
    username: str,
    app_password: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """以 get_file_tree("", 1) 判定 healthy，再以 /status.php 補版本。

    回傳形狀（dashboard ``target_app`` 與 Test 共用）::

        {healthy, url, error, auth_ok, item_count, truncated, version}
    """
    result: dict[str, Any] = {
        "healthy": False,
        "url": base,
        "error": None,
        "auth_ok": None,
        "item_count": None,
        "truncated": None,
        "version": None,
    }
    if not base:
        result["error"] = "Not configured（尚未設定 Nextcloud 網址）"
        return result
    missing = [label for label, value in (("帳號", username), ("app password", app_password)) if not value]
    if missing:
        result["error"] = f"尚未設定 Nextcloud {'與'.join(missing)}"
        return result

    try:
        settings = load_settings(
            base_url=base,
            username=username,
            app_password=app_password,
            readonly=True,
            request_timeout=PROBE_TIMEOUT,
        )
    except SettingsError as exc:  # 訊息只含變數名與原因，不含值
        result["error"] = f"設定不正確：{exc}"
        return result

    nc = NextcloudClient(settings, transport=transport)
    try:
        tree = await NextcloudTools(nc, settings).get_file_tree("", 1)
    except ToolError as exc:
        message = str(exc)
        if message.startswith(AUTH_MESSAGE):
            # 子程式的 401 鎖存訊息會叫人「重啟 server」；在管理介面只要到 Connection 頁存新的 app password
            # （新的帳密不受舊鎖存影響），所以這裡只顯示原因本身。
            message = AUTH_MESSAGE
            result["auth_ok"] = False
        result["error"] = message
        result["version"] = await server_version(base, transport=transport)
        return result
    except Exception as exc:  # noqa: BLE001 — surfaced to the dashboard, not raised
        result["error"] = f"Cannot reach Nextcloud at {base}: {type(exc).__name__}"
        return result
    finally:
        await nc.aclose()

    entries = tree.get("entries") if isinstance(tree, dict) else None
    result["item_count"] = len(entries) if isinstance(entries, list) else None
    result["truncated"] = bool(tree.get("truncated")) if isinstance(tree, dict) else None
    result["auth_ok"] = True
    result["healthy"] = True
    result["version"] = await server_version(base, transport=transport)
    return result


async def probe_target(conn: dict) -> dict[str, Any]:
    global _last_probe
    target = connection_target(conn)
    result = await check_nextcloud(*target)
    _last_probe = (_probe_key(*target), time.monotonic(), dict(result))
    return result


def _recent_probe(target: tuple[str, str, str]) -> dict[str, Any] | None:
    if _last_probe is None:
        return None
    key, at, result = _last_probe
    if key != _probe_key(*target) or time.monotonic() - at > PROBE_REUSE_SECONDS:
        return None
    return result


async def fetch_info(conn: dict) -> dict[str, Any]:
    """dashboard 細節：version＝Nextcloud 版本、db_name＝登入帳號、item_count＝家目錄第一層項目數。

    best-effort、永不 raise。
    """
    base, user, password = connection_target(conn)
    info: dict[str, Any] = {"version": None, "db_name": user or None, "item_count": None}
    if not base:
        return info
    try:
        probe = _recent_probe((base, user, password)) or await check_nextcloud(base, user, password)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to get Nextcloud info (best-effort): %s", type(exc).__name__)
        return info
    info["version"] = probe.get("version")
    info["item_count"] = probe.get("item_count")
    return info
