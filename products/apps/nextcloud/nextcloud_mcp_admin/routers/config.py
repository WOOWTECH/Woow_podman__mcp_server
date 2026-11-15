"""Nextcloud 連線設定 router（``base_url``、``username`` ＋ secret ``app_password``）。

連線鍵經 ``mcp_admin_core.process._build_subprocess_env`` 加 ``mcp_server.env_prefix``
（``NEXTCLOUD_MCP_``，見 ``seed_defaults``）大寫後注入子行程：``base_url`` → ``NEXTCLOUD_MCP_BASE_URL``、
``username`` → ``NEXTCLOUD_MCP_USERNAME``、``app_password`` → ``NEXTCLOUD_MCP_APP_PASSWORD``，對齊 vendored
``nextcloud_mcp_server.settings.Settings``（``env_prefix="NEXTCLOUD_MCP_"``）。**鍵名不可改**：後端 router、
前端 appConfig、PaaS seed 三處必須同名。

- ``base_url``：Nextcloud 根網址（例 ``https://cloud.example.com`` 或 ``https://example.com/nextcloud``）。
  Save 時 strip、去尾斜線，誤貼的 ``/index.php/...``、``/remote.php/...``、``/apps/...`` 尾段會切掉；
  只接受 http(s)，不可含帳密、query、fragment。可清空。**存檔前過核心的 SSRF 檢查**（``URL_FIELDS``）：私網、loopback、
  雲端 metadata、解析到這些位址的主機名一律回 400 ``ssrf_blocked``、不存檔（2026-11-14 實測 F-1；與 n8n、EMQX 同一套）。
- ``username``：登入名稱（可為 e-mail；子行程會用 OCS 查真正的 user id）。不可含 ``:`` 或控制字元。可清空。
- ``app_password``：secret，**只寫不讀**——GET 只回遮罩摘要；Save 留空（或送回遮罩摘要）＝沿用既有值
  （``drop_blank_secrets``）。存檔後登記到 ``log_redaction``，之後任何 log 出現都會被遮掉。
  請用專用帳號在「個人設定 → 安全性」產生的 app password，不要用登入密碼。

**身分綁定（2026-11-14 實測 F-3）**：``base_url``、``username`` 是這個 MCP 網址所代表的 Nextcloud 帳號（``IDENTITY_KEYS``，宣告在
``main.py`` 的 ``LaunchPolicy.identity_connection_keys``）。換掉其中一個，舊的 MCP 網址就在**同一次原子寫入**被撤銷、換成新 token
（核心 ``save_bound_connection``，與 Odoo 同一套；token 歷史多一筆 ``backend_identity_change``，不含帳號、網址或密碼），舊的子行程
先停、再寫入；身分換了一定重啟子行程（``restart:false`` 也一樣），回應 ``token_rotated: true`` 並提示到 Tokens 頁複製新網址。
``app_password`` 不是身分：只換密碼不換 token。第一次設定（之前沒有完整的 ``base_url``＋``username``）、已撤銷或沒有 token 時不換。
``base_url`` 用**正規化後**的值比（見 ``normalize_base_url``）：大小寫、尾斜線、誤貼的 ``/index.php/…`` 尾段、預設埠不同都不算換身分。
共用的 ``PUT /api/settings/connection`` 與額外帳號（``X-MCP-Profile``）走同一套。log 不印使用者、網址或任何憑證。

POST /api/config/test 一律測**存檔值**、不吃 body：以 vendored ``get_file_tree("", 1)`` 真正讀一次家目錄
（見 ``health_probes``），並附上 MCP 子行程是否執行中。連出去之前同樣過 SSRF 檢查（在 ``health_probes.check_nextcloud``，
``/api/health`` 共用）：存檔前的舊設定或之後才改指私網的主機名，一個請求都不送。

Endpoints:
    GET  /api/config             - base_url、username ＋ app_password 遮罩摘要
    PUT  /api/config/connection  - 更新連線，可選 restart（子行程沒在跑則 start，#1350）；換使用者或網址會換 MCP token
    POST /api/config/test        - 測存檔值
"""
from __future__ import annotations

from typing import Annotated, Any, Mapping
from fastapi import Request, Header, Response
from mcp_admin_core.configuration import (ConfigurationResponse, configuration_status, revision_header, run_configuration_transition)

from mcp_admin_core.connection_apply import connection_outcome, refuse_blocked_urls, save_bound_connection
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
#: 後端網址欄位：存檔時就過核心的 SSRF 檢查（``connection_apply.refuse_blocked_urls``，與 n8n、EMQX 同一套；main.py 的
#: ``LaunchPolicy.url_connection_keys`` 也指向這裡，共用的 ``PUT /api/settings/connection`` 與額外帳號同樣會擋）。
URL_FIELDS = frozenset({"base_url"})
#: 身分欄位：換使用者或網址＝換了這個 MCP 網址所代表的 Nextcloud 帳號，舊網址撤銷、換新 token（見模組說明；main.py 的
#: ``LaunchPolicy.identity_connection_keys`` 指向這裡，核心共用的 ``PUT /api/settings/connection`` 與額外帳號同樣生效）。
#: ``app_password`` 刻意不在裡面：同一個帳號重新產生 app password 是例行維護，不該讓使用者每次都換 MCP 網址。
IDENTITY_KEYS = frozenset({"base_url", "username"})
NOT_SET = "(not set)"
#: 身分換了、舊的 MCP 網址已撤銷時，接在回應 ``message`` 後面的提示（英文與 Odoo 相同；共用前端把 ``message`` 原樣顯示給管理員）。
TOKEN_ROTATED_NOTICE = (" Account changed: old MCP URL revoked. Copy the new URL from Tokens."
                        " 連線的帳號或網址已變更：舊的 MCP 網址已撤銷，請到 Tokens 頁複製新網址。")

# 使用者常從瀏覽器網址列整段貼上；這些路徑段之後都不屬於 Nextcloud 根網址。
_SUFFIX_RE = re.compile(r"/(index\.php|remote\.php|ocs|apps|login|settings)(/.*)?$", re.IGNORECASE)


def mask_password(value: str) -> str:
    return mask_secret(value, prefix=2, suffix=2, min_len=8) if value else NOT_SET


def _bad_request(message: str) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, message)


def normalize_base_url(raw: str) -> str:
    """strip、去尾斜線、切掉誤貼的頁面路徑；非空時只接受乾淨的 http(s) 網址。

    協定與主機名一律轉小寫、預設埠（http 80、https 443）去掉：這些寫法不同的網址是同一個 Nextcloud。存下來的就是這個正規化後的
    值，所以也是「有沒有換身分」的比對基準（``IDENTITY_KEYS``）。路徑**保留大小寫**：伺服器可能分大小寫，不同寫法的路徑視為
    不同的網址（寧可多換一次 token，也不漏換）。
    """
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
    host = parts.hostname  # urlsplit 已轉小寫、IPv6 不含方括號；帳密與空的 ``:`` 前面已經擋掉
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    if port is not None and (parts.scheme, port) not in (("http", 80), ("https", 443)):
        host = f"{host}:{port}"
    return f"{parts.scheme}://{host}{path}"


def _keep_stored_spelling(stored: Any, normalized: str) -> str:
    """存檔值與新值只差寫法（正規化後相同）時沿用存檔值：不算換身分，也不為了改寫法而多寫一次。

    存檔值可能不是這一版的正規化形式（共用路徑以前原樣存檔、或 PaaS seed 帶的寫法）；Connection 頁把它原樣送回來時，
    正規化後的新值會與存檔值逐字不同，核心的比對是逐字的，會誤判成換了網址。
    """
    if not isinstance(stored, str) or not stored or not normalized or stored == normalized:
        return normalized
    try:
        return stored if normalize_base_url(stored) == normalized else normalized
    except HTTPException:  # 存檔值本身格式不對（手改過 config.json）：照新值存
        return normalized


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


def normalize_connection_update(values: Mapping[str, Any]) -> dict[str, str]:
    """共用的 ``PUT /api/settings/connection`` 用（``LaunchPolicy.normalize_connection``）：與 Connection 頁同一套驗證與正規化。

    只處理有送的欄位，沒送的維持原值。不這樣做的話，共用路徑會把 ``https://Cloud.Example.com/`` 原樣存下來，下一次從 Connection
    頁（或同一條路徑）送 ``https://cloud.example.com`` 就被核心逐字比對成「換了網址」。值不是字串、或格式不對時整筆不存
    （錯誤訊息是固定文字，不回顯送來的值）。
    """
    normalizers = {"base_url": normalize_base_url, "username": normalize_username, "app_password": normalize_password}
    cleaned: dict[str, str] = {}
    for key, value in values.items():
        if not isinstance(value, str):
            raise ValueError("connection field values must be strings")
        normalizer = normalizers.get(key)
        cleaned[key] = normalizer(value) if normalizer else value.strip()
    return cleaned


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
    restart: bool = Field(default=True, description="更新後是否套用到 MCP 子行程（restart／start）；換使用者或網址時一律套用")


class ConnectionUpdateResponse(ConfigurationResponse):
    saved: bool = True
    #: 這次存檔換了身分（使用者或網址），舊的 MCP 網址已撤銷；新網址到 Tokens 頁複製（回應不帶 token）。
    token_rotated: bool = False
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
        # SSRF：私網／loopback／metadata／帶帳密的網址回 400（detail.code="ssrf_blocked"）、不存檔、不套用；模式讀部署端的
        # WEBHOOK_SECURITY_MODE（預設 strict，租戶改不了）。空字串＝尚未設定／有意清空，不檢查。必須在任何寫入之前。
        await refuse_blocked_urls(updates, URL_FIELDS)
        # 表單若把 GET 的遮罩摘要原樣送回，視同沒改（不可把遮罩字串存成密碼、打掛子行程）。
        stored_pw = str(current.get("app_password") or "")
        if stored_pw and updates["app_password"] == mask_password(stored_pw):
            updates["app_password"] = ""
        updates = drop_blank_secrets(updates, SECRET_FIELDS)
        # 寫法不同的同一個網址沿用存檔值（見 _keep_stored_spelling）：不算換身分。
        updates["base_url"] = _keep_stored_spelling(current.get("base_url"), updates["base_url"])
        # 身分（base_url、username）換了：舊子行程先停、新連線與新 token 在同一次寫入、舊的 MCP 網址撤銷（核心 save_bound_connection，
        # 與 Odoo 同一套）。第一次設定、已撤銷或沒有 token、只換 app password 都不換。
        from mcp_admin_core.routers.tokens import new_token
        pm = get_process_manager()
        was_running = pm.is_running  # 要在存檔之前看：身分換了時，核心會先把舊子行程停掉
        merged, token_rotated = await save_bound_connection(
            store, pm, updates, identity_keys=IDENTITY_KEYS, make_token=new_token)
        from mcp_admin_core.profiles import current_profile_id
        log_redaction.set_profile_secrets(current_profile_id(), str(merged.get("app_password") or ""),
                                          str(merged.get("username") or ""))
        # log 不印使用者、網址或任何憑證（身分變更的紀錄在 token 歷史的 backend_identity_change）。
        logger.info("Updated Nextcloud connection config (app_password=%s)",
                    "updated" if "app_password" in updates else "unchanged")
        if token_rotated:
            logger.info("Nextcloud connection identity changed; MCP token rotated")

        applied = False
        started = False
        # 身分換了，舊子行程已經被停掉：不管有沒有要求套用，都要用新身分重新啟動（修正前 restart:false 時舊身分一直跑到下次重啟）。
        should_restart = req.restart or token_rotated
        if should_restart:
            try:
                started = not was_running
                applied = bool(await pm.start_or_restart())
            except Exception:
                # Persistence has already succeeded; do not disguise apply failure as save failure.
                logger.warning("MCP process application failed after saving connection")
        outcome = connection_outcome(should_restart, applied, started=started and applied)
        outcome["token_rotated"] = token_rotated
        if token_rotated:
            outcome["message"] += TOKEN_ROTATED_NOTICE
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
