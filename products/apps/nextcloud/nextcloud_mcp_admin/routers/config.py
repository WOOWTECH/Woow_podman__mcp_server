"""Nextcloud 連線設定 router（``base_url``、``username`` ＋ secret ``app_password``）。

連線鍵經 ``mcp_admin_core.process._build_subprocess_env`` 加 ``mcp_server.env_prefix``
（``NEXTCLOUD_MCP_``，見 ``seed_defaults``）大寫後注入子行程：``base_url`` → ``NEXTCLOUD_MCP_BASE_URL``、
``username`` → ``NEXTCLOUD_MCP_USERNAME``、``app_password`` → ``NEXTCLOUD_MCP_APP_PASSWORD``，對齊 vendored
``nextcloud_mcp_server.settings.Settings``（``env_prefix="NEXTCLOUD_MCP_"``）。**鍵名不可改**：後端 router、
前端 appConfig、PaaS seed 三處必須同名。

- ``base_url``：Nextcloud 根網址（例 ``https://cloud.example.com`` 或 ``https://example.com/nextcloud``）。
  Save 時 strip、去尾斜線，誤貼的 ``/index.php/...``、``/remote.php/...``、``/apps/...`` 尾段會切掉；
  只接受 http(s)，不可含帳密、query、fragment。可清空。
- ``username``：登入名稱（可為 e-mail；子行程會用 OCS 查真正的 user id）。不可含 ``:`` 或控制字元。可清空。
- ``app_password``：secret，**只寫不讀**——GET 只回遮罩摘要；Save 留空（或送回遮罩摘要）＝沿用既有值
  （``drop_blank_secrets``）。存檔後登記到 ``log_redaction``，之後任何 log 出現都會被遮掉。
  請用專用帳號在「個人設定 → 安全性」產生的 app password，不要用登入密碼。

POST /api/config/test 一律測**存檔值**、不吃 body：以 vendored ``get_file_tree("", 1)`` 真正讀一次家目錄
（見 ``health_probes``），並附上 MCP 子行程是否執行中。

Endpoints:
    GET  /api/config             - base_url、username ＋ app_password 遮罩摘要
    PUT  /api/config/connection  - 更新連線，可選 restart（子行程沒在跑則 start，#1350）
    POST /api/config/test        - 測存檔值
"""
from __future__ import annotations

from typing import Annotated
from fastapi import Request, Header, Response
from mcp_admin_core.configuration import (ConfigurationResponse, configuration_status, revision_header, run_configuration_transition)

from mcp_admin_core.connection_apply import connection_outcome
import logging
import re
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from mcp_admin_core.config import get_config_store
from mcp_admin_core.process import get_process_manager
from mcp_admin_core.util.mask import mask_secret
from mcp_admin_core.util.secrets import drop_blank_secrets

from nextcloud_mcp_admin import health_probes, log_redaction

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/config", tags=["config"])

CONNECTION_KEYS = ("base_url", "username", "app_password")
SECRET_FIELDS = ("app_password",)
NOT_SET = "(not set)"

# 使用者常從瀏覽器網址列整段貼上；這些路徑段之後都不屬於 Nextcloud 根網址。
_SUFFIX_RE = re.compile(r"/(index\.php|remote\.php|ocs|apps|login|settings)(/.*)?$", re.IGNORECASE)


def mask_password(value: str) -> str:
    return mask_secret(value, prefix=2, suffix=2, min_len=8) if value else NOT_SET


def _bad_request(message: str) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, message)


def normalize_base_url(raw: str) -> str:
    """strip、去尾斜線、切掉誤貼的頁面路徑；非空時只接受乾淨的 http(s) 網址。"""
    url = (raw or "").strip()
    if not url:
        return ""
    if any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in url):
        raise _bad_request("base_url 不可含空白或控制字元")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise _bad_request("base_url 需為 http(s):// 開頭的 Nextcloud 網址，例 https://cloud.example.com")
    if parts.username or parts.password or "@" in parts.netloc:
        raise _bad_request("base_url 不可含帳號密碼；帳號與 app password 請填在各自的欄位")
    if parts.query or parts.fragment or "?" in url or "#" in url:
        raise _bad_request("base_url 不可含 ? 或 #（只填 Nextcloud 根網址）")
    try:
        _ = parts.port
    except ValueError:
        raise _bad_request("base_url 的連接埠不正確") from None
    path = _SUFFIX_RE.sub("", parts.path).rstrip("/")
    return f"{parts.scheme}://{parts.netloc}{path}"


def normalize_username(raw: str) -> str:
    user = (raw or "").strip()
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in user) or ":" in user:
        raise _bad_request("username 不可含 : 或控制字元")
    return user


def normalize_password(raw: str) -> str:
    pw = (raw or "").strip()
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in pw):
        raise _bad_request("app_password 不可含控制字元")
    return pw


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ConnectionConfig(ConfigurationResponse):
    base_url: str = ""
    username: str = ""
    app_password_masked: str = NOT_SET
    app_password_set: bool = False


class ConnectionUpdateRequest(BaseModel):
    # 沒宣告的欄位（拼錯的鍵）回 422、不再默默忽略；422 不回顯送出的值（core validation_errors）。
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    base_url: str = Field(default="", description="Nextcloud 根網址（例 https://cloud.example.com）")
    username: str = Field(default="", description="Nextcloud 登入名稱（建議專用帳號）")
    app_password: str = Field(default="", description="Nextcloud app password（secret；留空＝沿用既有值）")
    restart: bool = Field(default=True, description="更新後是否套用到 MCP 子行程（restart／start）")


class ConnectionUpdateResponse(ConfigurationResponse):
    saved: bool = True
    status: str = "ok"
    apply_state: str = "not_requested"
    success: bool
    message: str
    restarted: bool = False


class ConnectionTestResponse(BaseModel):
    success: bool
    message: str
    details: dict | None = None


# ---------------------------------------------------------------------------
# GET /api/config
# ---------------------------------------------------------------------------


@router.get("", response_model=ConnectionConfig)
async def get_config(response: Response = None) -> ConnectionConfig:
    store = get_config_store()
    snapshot = await store.load()
    configuration = configuration_status(store, snapshot, get_process_manager())
    revision_header(response, configuration)
    conn = snapshot.get("connection", {}) or {}
    pw = str(conn.get("app_password") or "")
    return ConnectionConfig(configuration=configuration,
        base_url=str(conn.get("base_url") or ""),
        username=str(conn.get("username") or ""),
        app_password_masked=mask_password(pw),
        app_password_set=bool(pw),
    )


# ---------------------------------------------------------------------------
# PUT /api/config/connection
# ---------------------------------------------------------------------------


@router.put("/connection", response_model=ConnectionUpdateResponse)
async def update_connection(req: ConnectionUpdateRequest, response: Response = None, if_match: Annotated[str | None, Header(alias="If-Match")] = None, request: Request = None) -> ConnectionUpdateResponse:
    async def apply_update():
        store = get_config_store()
        current = await store.get("connection", {}) or {}

        updates = {
            "base_url": normalize_base_url(req.base_url),
            "username": normalize_username(req.username),
            "app_password": normalize_password(req.app_password),
        }
        # 表單若把 GET 的遮罩摘要原樣送回，視同沒改（不可把遮罩字串存成密碼、打掛子行程）。
        stored_pw = str(current.get("app_password") or "")
        if stored_pw and updates["app_password"] == mask_password(stored_pw):
            updates["app_password"] = ""
        updates = drop_blank_secrets(updates, SECRET_FIELDS)
        merged = await store.patch("connection", updates)
        log_redaction.set_secrets(str(merged.get("app_password") or ""), str(merged.get("username") or ""))
        logger.info(
            "Updated Nextcloud connection config (base_url=%s, username=%s, app_password=%s)",
            updates["base_url"] or "<empty>",
            updates["username"] or "<empty>",
            "updated" if "app_password" in updates else "unchanged",
        )

        applied = False
        started = False
        if req.restart:
            try:
                pm = get_process_manager()
                started = not pm.is_running
                applied = bool(await pm.start_or_restart())
            except Exception:
                # Persistence has already succeeded; do not disguise apply failure as save failure.
                logger.warning("MCP process application failed after saving connection")
        outcome = connection_outcome(req.restart, applied, started=started and applied)
        return ConnectionUpdateResponse(success=True, **outcome)

    return await run_configuration_transition(get_config_store(), get_process_manager(), if_match, apply_update, response, request=request)


# ---------------------------------------------------------------------------
# POST /api/config/test
# ---------------------------------------------------------------------------


@router.post("/test", response_model=ConnectionTestResponse)
async def test_connection() -> ConnectionTestResponse:
    """測存檔值（不吃 body）：以 get_file_tree("", 1) 真正讀一次家目錄。"""
    store = get_config_store()
    conn = await store.get("connection", {}) or {}
    base, user, password = health_probes.connection_target(conn)
    child = "執行中" if get_process_manager().is_running else "未執行（存檔後會自動啟動）"

    if not base:
        return ConnectionTestResponse(
            success=False,
            message="尚未設定 Nextcloud 網址（base_url）。",
            details={"mcp_server": child},
        )

    probe = await health_probes.check_nextcloud(base, user, password)
    details = {
        "url": base,
        "username": user,
        "auth_ok": probe.get("auth_ok"),
        "item_count": probe.get("item_count"),
        "version": probe.get("version"),
        "mcp_server": child,
    }
    if not probe.get("healthy"):
        return ConnectionTestResponse(
            success=False,
            message=f"{probe.get('error') or 'Nextcloud 無法連線'}；MCP 子行程：{child}",
            details=details,
        )

    parts = [f"連線成功：Nextcloud {probe.get('version') or ''}".rstrip()]
    parts.append(f"帳號 {user}")
    if probe.get("item_count") is not None:
        more = "以上" if probe.get("truncated") else ""
        parts.append(f"家目錄第一層 {probe['item_count']} 項{more}")
    parts.append(f"MCP 子行程{child}")
    return ConnectionTestResponse(success=True, message="；".join(parts), details=details)
