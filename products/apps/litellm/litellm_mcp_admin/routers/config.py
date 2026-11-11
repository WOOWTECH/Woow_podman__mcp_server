"""LiteLLM 連線設定 router（``base_url`` ＋ secret ``master_key``）。

連線鍵經 ``mcp_admin_core.process._build_subprocess_env`` 加 ``mcp_server.env_prefix``
（``LITELLM_MCP_``，見 ``seed_defaults``）大寫後注入子行程：``base_url`` →
``LITELLM_MCP_BASE_URL``、``master_key`` → ``LITELLM_MCP_MASTER_KEY``，對齊 vendored
``woow_litellm_mcp_server.settings.Settings``（``env_prefix="LITELLM_MCP_"``）。**鍵名不可改**：
後端 router、前端 appConfig、PaaS seed（平台同工作區自動串接預填 ``connection.base_url``）
三處必須同名。

- ``base_url``：LiteLLM 網址，**不含 /v1**（子行程自己組 ``/v1/models``、``/key/list``…）。
  Save 時 strip、去尾斜線，只移除根路徑 ``/v1``；保留 ``/proxy/v1`` 等掛載。可清空。
- ``master_key``：secret，**只寫不讀**——GET 只回遮罩摘要；Save 留空（或送回遮罩摘要）＝沿用
  既有值（PR #9/#10/#11 canon，``drop_blank_secrets``）。建議填 LiteLLM ``proxy_admin_viewer``
  使用者的 key，而不是 master key（見 README「權限較小的 key」）。

POST /api/config/test 一律測**存檔值**、不吃 body（PR #11 canon）：直打帶 key 的
``GET {base_url}/v1/models``（驗網址＋金鑰）＋免認證 ``/health/readiness``（補 DB；版本讀 ``/openapi.json``），
並附上 MCP 子行程是否執行中。

Endpoints:
    GET  /api/config             - base_url ＋ master_key 遮罩摘要
    PUT  /api/config/connection  - 更新連線，可選 restart（子行程沒在跑則 start，#1350）
    POST /api/config/test        - 測存檔值
"""
from __future__ import annotations

from typing import Annotated
from fastapi import Request, Header, Response
from mcp_admin_core.configuration import (ConfigurationResponse, configuration_status, revision_header, run_configuration_transition)

from mcp_admin_core.connection_apply import connection_outcome
import logging
from woow_litellm_mcp_server.urls import normalize_base_url as _normalize_gateway_root

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from mcp_admin_core.config import get_config_store
from mcp_admin_core.process import get_process_manager
from mcp_admin_core.util.mask import mask_secret
from mcp_admin_core.util.secrets import drop_blank_secrets

from litellm_mcp_admin import health_probes

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/config", tags=["config"])

CONNECTION_KEYS = ("base_url", "master_key")
SECRET_FIELDS = ("master_key",)
NOT_SET = "(not set)"


def mask_key(value: str) -> str:
    return mask_secret(value, prefix=2, suffix=2, min_len=6) if value else NOT_SET


def normalize_base_url(raw: str) -> str:
    """Normalize only root /v1; preserve prefixed reverse-proxy mounts."""
    try:
        return _normalize_gateway_root(raw or "")
    except (ValueError, TypeError):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "base_url 需為 http(s):// LiteLLM 根網址；不可含帳號密碼、query 或 fragment。"
            "根路徑 /v1 會移除；/proxy/v1 等掛載路徑會保留。",
        ) from None


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ConnectionConfig(ConfigurationResponse):
    base_url: str = ""
    master_key_masked: str = NOT_SET
    master_key_set: bool = False


class ConnectionUpdateRequest(BaseModel):
    # 沒宣告的欄位（拼錯的鍵）回 422、不再默默忽略；422 不回顯送出的值（core validation_errors）。
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    base_url: str = Field(default="", description="LiteLLM 根網址；根路徑 /v1 自動移除，/proxy/v1 等掛載保留")
    master_key: str = Field(default="", description="LiteLLM 金鑰（secret；留空＝沿用既有值）")
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
    key = str(conn.get("master_key") or "")
    return ConnectionConfig(configuration=configuration,
        base_url=str(conn.get("base_url") or ""),
        master_key_masked=mask_key(key),
        master_key_set=bool(key),
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
            "master_key": req.master_key.strip(),
        }
        # 表單若把 GET 的遮罩摘要原樣送回，視同沒改（不可把遮罩字串存成金鑰、打掛子行程）。
        stored_key = str(current.get("master_key") or "")
        if stored_key and updates["master_key"] == mask_key(stored_key):
            updates["master_key"] = ""
        updates = drop_blank_secrets(updates, SECRET_FIELDS)
        await store.patch("connection", updates)
        logger.info("Updated LiteLLM connection config (base_url=%s)", updates["base_url"] or "<empty>")

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
    """測存檔值（不吃 body）：帶 key 的 /v1/models 決定成敗，readiness 只補細節。"""
    store = get_config_store()
    conn = await store.get("connection", {}) or {}
    base, key = health_probes.connection_target(conn)
    child = "執行中" if get_process_manager().is_running else "未執行（存檔後會自動啟動）"

    if not base:
        return ConnectionTestResponse(
            success=False,
            message="尚未設定 LiteLLM 網址（base_url）。",
            details={"mcp_server": child},
        )

    probe = await health_probes.check_gateway(base, key)
    details = {
        "url": base,
        "status_code": probe.get("status_code"),
        "auth_ok": probe.get("auth_ok"),
        "model_count": probe.get("model_count"),
        "version": probe.get("version"),
        "db": probe.get("db"),
        "mcp_server": child,
    }
    if not probe.get("healthy"):
        return ConnectionTestResponse(
            success=False,
            message=f"{probe.get('error') or 'LiteLLM 無法連線'}；MCP 子行程：{child}",
            details=details,
        )

    parts = [f"連線成功：LiteLLM {probe.get('version') or ''}".rstrip()]
    if probe.get("db"):
        parts.append(f"DB {probe['db']}")
    if probe.get("model_count") is not None:
        parts.append(f"模型 {probe['model_count']} 個")
    parts.append(f"MCP 子行程{child}")
    return ConnectionTestResponse(success=True, message="；".join(parts), details=details)
