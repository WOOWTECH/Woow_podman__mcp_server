"""Dual connection configuration router for Hermes MCP Admin.

Hermes has TWO backends:
  - Gateway API  (:8642) — OpenAI-compatible API with Bearer auth
  - Dashboard API (:9119) — Management REST API with Basic auth

Endpoints:
  GET  /api/config                 - current dual connection settings (masked)
  PUT  /api/config/connection      - update dual connection settings
  POST /api/config/test/gateway    - test Gateway API connectivity
  POST /api/config/test/dashboard  - test Dashboard API connectivity
"""

from __future__ import annotations

from typing import Annotated
from fastapi import Request, Header, Response
from mcp_admin_core.configuration import (ConfigurationResponse, configuration_status, revision_header, run_configuration_transition)

from mcp_admin_core.connection_apply import connection_outcome
import logging
from typing import Any, ClassVar

import httpx
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from mcp_admin_core.config import get_config_store
from mcp_admin_core.process import get_process_manager
from mcp_admin_core.util import ssrf
from mcp_admin_core.util.mask import mask_secret
from mcp_admin_core.util.secrets import drop_blank_secrets

from hermes_mcp_server import errors

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/config", tags=["config"])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class DualConnectionConfig(ConfigurationResponse):
    """Current Hermes dual connection configuration (read-only view)."""

    gateway_url: str = ""
    gateway_key_masked: str = ""
    dashboard_url: str = ""
    dashboard_username: str = ""
    dashboard_password_masked: str = ""


class DualConnectionUpdate(BaseModel):
    """Payload for updating Hermes dual connection settings（partial patch）。

    五個欄位皆 optional，語意分三種：

    ============================  ======================================
    未送出（欄位不在 payload）    保留存檔值
    送出空字串（URL / username）  有意清空
    送出空字串（secret 兩欄）      保留存檔值（見 ``drop_blank_secrets``）
    ============================  ======================================

    以 ``None`` 當 sentinel（而非「空字串＝不變」），否則 URL 就永遠清不掉。
    曾經五欄全 required：只想補 dashboard 三格的呼叫者會把 gateway 兩欄清成空字串、
    當場打爛服務。前端一律送五欄，故此改動向後相容。

    沒宣告的欄位回 422：MCP:Hermes 2026-11-05 把 ``gateway_api_key`` 拼成 ``gateway_key``（GET 回的是
    ``gateway_key_masked``）時回 200、卻什麼都沒存。422 不回顯送出的值（core ``validation_errors``）。
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    gateway_url: str | None = Field(default=None, description="Hermes Gateway API URL (e.g. http://hermes:8642)")
    gateway_api_key: str | None = Field(default=None, description="Gateway API key for Bearer authentication")
    dashboard_url: str | None = Field(default=None, description="Hermes Dashboard API URL (e.g. http://hermes:9119)")
    dashboard_username: str | None = Field(default=None, description="Dashboard username for Basic auth")
    dashboard_password: str | None = Field(default=None, description="Dashboard password for Basic auth")
    restart: bool = Field(default=True, description="Restart MCP server after update")

    # 只有這五個是連線欄位；restart 是旗標、不進 store。
    CONNECTION_FIELDS: ClassVar[tuple[str, ...]] = (
        "gateway_url", "gateway_api_key", "dashboard_url",
        "dashboard_username", "dashboard_password",
    )

    def connection_updates(self) -> dict[str, str]:
        """回傳「本次真的要寫進 store 的欄位」，值已 strip。

        防呆：strip 掉前後空白。貼上時多帶一個看不見的空白會讓 URL / key / 帳密認證失敗。
        """
        return {
            name: value.strip()
            for name, value in ((f, getattr(self, f)) for f in self.CONNECTION_FIELDS)
            if value is not None
        }


class ConnectionTestResult(BaseModel):
    """Result of a connectivity test."""

    success: bool
    message: str
    version: str | None = None
    details: dict[str, Any] = {}


def _connection_failure(exc: Exception, backend: str, url: str) -> ConnectionTestResult:
    """測試連線失敗：訊息只講狀態碼＋JSON detail＋建議動作，不轉述回應本文（AI-HMCP-003-3）。

    曾經回 ``HTTP 502: <exc.response.text[:200]>``，Cloudflare 502 時就是一段整頁 HTML。
    """
    details: dict[str, Any] = {}
    if isinstance(exc, httpx.HTTPStatusError):
        details["status_code"] = exc.response.status_code
        detail = errors.response_detail(exc.response)
        if detail:
            details["detail"] = detail
    return ConnectionTestResult(success=False, message=errors.describe(exc, backend, url), details=details)


# ---------------------------------------------------------------------------
# SSRF（AI-HMCP-002-1）
# ---------------------------------------------------------------------------
# 249 實測：gateway_url／dashboard_url 的儲存與 Test 接受 127.0.0.1、169.254.169.254、叢集內位址、file://，
# Test 還會把回應本文回顯，管理 API 因此成了探測內網的跳板。儲存與 Test 都套 mcp_admin_core 的 SSRF 檢查
# （模式讀部署端的 WEBHOOK_SECURITY_MODE，預設 strict；docker-compose 直連內網主機要設 permissive）。
URL_FIELDS = ("gateway_url", "dashboard_url")


async def _ssrf_block_reason(url: str) -> str | None:
    result = await ssrf.check_url(url)
    return None if result.ok else result.reason


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.get("", response_model=DualConnectionConfig)
async def get_config(response: Response = None) -> DualConnectionConfig:
    """Return current Hermes dual connection settings with masked secrets."""
    store = get_config_store()
    snapshot = await store.load()
    configuration = configuration_status(store, snapshot, get_process_manager())
    revision_header(response, configuration)
    conn = snapshot.get("connection", {})

    gateway_key = conn.get("gateway_api_key", "")
    dashboard_pw = conn.get("dashboard_password", "")

    return DualConnectionConfig(configuration=configuration,
        gateway_url=conn.get("gateway_url", ""),
        gateway_key_masked=mask_secret(gateway_key) if gateway_key else "(not set)",
        dashboard_url=conn.get("dashboard_url", ""),
        dashboard_username=conn.get("dashboard_username", ""),
        dashboard_password_masked=mask_secret(dashboard_pw) if dashboard_pw else "(not set)",
    )


@router.put("/connection", response_model=dict)
async def update_connection(payload: DualConnectionUpdate, response: Response = None, if_match: Annotated[str | None, Header(alias="If-Match")] = None, request: Request = None) -> dict:
    async def apply_update():
        """Update Hermes dual connection settings and optionally restart MCP server.

        Partial patch：未送出的欄位保留存檔值（見 ``DualConnectionUpdate``）。
        """
        store = get_config_store()
        updates = payload.connection_updates()
        for field in URL_FIELDS:
            # 空字串＝有意清空，不檢查；其餘一律先過 SSRF 再寫入 store。
            reason = await _ssrf_block_reason(updates[field]) if updates.get(field) else None
            if reason:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"{field} blocked by SSRF protection: {reason}",
                )
        # 留空的 secret 欄不覆寫既有存檔值（避免只改 URL 沒重打 key/密碼就清空）
        updates = drop_blank_secrets(updates, ["gateway_api_key", "dashboard_password"])
        if updates:
            await store.patch("connection", updates)
        logger.info("Updated Hermes dual connection config: %s", sorted(updates) or "(no field changed)")

        applied = False
        started = False
        if payload.restart:
            try:
                pm = get_process_manager()
                if pm.is_running:
                    applied = bool(await pm.restart())
                else:
                    started = True
                    applied = bool(await pm.start())
            except Exception:
                # Persistence has already succeeded; do not disguise apply failure as save failure.
                logger.warning("MCP process application failed after saving connection")
        outcome = connection_outcome(payload.restart, applied, started=started and applied)
        return outcome

    return await run_configuration_transition(get_config_store(), get_process_manager(), if_match, apply_update, response, request=request)


@router.post("/test/gateway", response_model=ConnectionTestResult)
async def test_gateway() -> ConnectionTestResult:
    """Test connectivity to the Hermes Gateway API via GET /v1/capabilities."""
    store = get_config_store()
    conn = await store.get("connection", {})

    gateway_url = conn.get("gateway_url", "")
    api_key = conn.get("gateway_api_key", "")

    if not gateway_url:
        return ConnectionTestResult(
            success=False,
            message="gateway_url is not configured",
        )
    # 升級前就存進 config.json 的值也要擋：被擋時不發出任何請求。
    reason = await _ssrf_block_reason(gateway_url)
    if reason:
        return ConnectionTestResult(success=False, message=f"gateway_url blocked by SSRF protection: {reason}")

    url = f"{gateway_url.rstrip('/')}/v1/capabilities"
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            body = resp.json()
    except errors.TRANSLATABLE as exc:
        return _connection_failure(exc, "gateway", gateway_url)
    except Exception as exc:  # catch-all
        return ConnectionTestResult(
            success=False,
            message=f"Unexpected error: {exc}",
        )

    version = None
    if isinstance(body, dict):
        version = body.get("version")

    return ConnectionTestResult(
        success=True,
        message="Hermes Gateway API is reachable",
        version=version,
        details={"url": url, "status_code": resp.status_code},
    )


@router.post("/test/dashboard", response_model=ConnectionTestResult)
async def test_dashboard() -> ConnectionTestResult:
    """Test connectivity to the Hermes Dashboard API via GET /api/config."""
    store = get_config_store()
    conn = await store.get("connection", {})

    dashboard_url = conn.get("dashboard_url", "")
    username = conn.get("dashboard_username", "")
    password = conn.get("dashboard_password", "")

    if not dashboard_url:
        return ConnectionTestResult(
            success=False,
            message="dashboard_url is not configured",
        )
    reason = await _ssrf_block_reason(dashboard_url)
    if reason:
        return ConnectionTestResult(success=False, message=f"dashboard_url blocked by SSRF protection: {reason}")

    try:
        # Login to get session cookie (v0.17.0 uses cookie-based auth)
        async with httpx.AsyncClient(timeout=10.0) as client:
            login_resp = await client.post(
                f"{dashboard_url.rstrip('/')}/auth/password-login",
                json={"provider": "basic", "username": username, "password": password},
            )
            login_resp.raise_for_status()
            cookie = ""
            for h in login_resp.headers.get_list("set-cookie"):
                if "hermes_session_at=" in h:
                    cookie = h.split("hermes_session_at=")[1].split(";")[0].strip('"')
                    break
            if not cookie:
                return ConnectionTestResult(success=False, message="Login succeeded but no session cookie returned")
            resp = await client.get(
                f"{dashboard_url.rstrip('/')}/api/config",
                headers={"Cookie": f'hermes_session_at="{cookie}"'},
            )
            resp.raise_for_status()
            body = resp.json()
    except errors.TRANSLATABLE as exc:
        return _connection_failure(exc, "dashboard", dashboard_url)
    except Exception as exc:  # catch-all
        return ConnectionTestResult(
            success=False,
            message=f"Unexpected error: {exc}",
        )

    return ConnectionTestResult(
        success=True,
        message="Hermes Dashboard API is reachable",
        details={"url": dashboard_url, "status_code": resp.status_code, "config_keys": len(body)},
    )
