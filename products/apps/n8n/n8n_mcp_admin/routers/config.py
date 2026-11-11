"""Connection configuration router for n8n MCP Admin.

Manages n8n MCP connection settings stored in the config store.

Endpoints:
  GET  /api/config             - current n8n connection settings (masked) + permissions
  PUT  /api/config/connection   - update connection settings
  POST /api/config/test         - test n8n REST API connectivity（套用對齊 n8n-mcp 的 SSRF 規則）
  PUT  /api/config/permissions  - 依操作停用（→ 上游 DISABLED_TOOL_OPERATIONS，woow_paas_platform#1531）
"""

from __future__ import annotations

from typing import Annotated
from fastapi import Request, Header, Response
from mcp_admin_core.configuration import (ConfigurationResponse, configuration_status, revision_header, run_configuration_transition)

from mcp_admin_core.connection_apply import connection_outcome, refuse_blocked_urls
import logging
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from mcp_admin_core.config import get_config_store
from mcp_admin_core.operation_policy import read_policy, validate_operation_update
from mcp_admin_core.process import get_process_manager
from mcp_admin_core.util.mask import mask_secret
from mcp_admin_core.util.secrets import drop_blank_secrets
from mcp_admin_core.util.ssrf import check_url

from .. import child_env, tool_registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/config", tags=["config"])

#: 後端網址欄位：存檔時就用對齊子行程（上游 n8n-mcp）的 SSRF 規則檢查（main.py 的 LaunchPolicy.url_connection_keys 也指向這裡，
#: 共用的 PUT /api/settings/connection 同樣會擋）。過去會存下子行程一定會拒絕的網址，到呼叫工具時才失敗。
URL_FIELDS = frozenset({"n8n_api_url"})


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class ConnectionConfig(ConfigurationResponse):
    """Current n8n connection configuration (read-only view)."""

    n8n_api_url: str = ""
    n8n_api_key_masked: str = ""
    n8n_mcp_access_token_masked: str = ""
    mcp_session_timeout: int = child_env.SESSION_TIMEOUT_DEFAULT_SECONDS
    mcp_max_sessions: int = child_env.MAX_SESSIONS_DEFAULT
    # 共用 PermissionEditor 讀 /api/config 的 permissions（JSON 編輯器）：
    # {"<tool>": ["<op>", ...]}，存成 tools.disabled_operations、子行程以 DISABLED_TOOL_OPERATIONS 生效。
    permissions: dict[str, list[str]] = {}
    # 可依操作停用的工具與合法操作名（上游 TOOL_OPERATION_PARAM／getValidOperations）
    permission_operations: dict[str, list[str]] = {}


class PermissionsUpdate(BaseModel):
    permissions: dict[str, list[str]] = Field(default_factory=dict)


class ConnectionUpdate(BaseModel):
    """Payload for updating n8n connection settings."""

    # 沒宣告的欄位（拼錯的鍵）回 422、不再默默忽略；422 不回顯送出的值（core validation_errors）。
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    n8n_api_url: str = Field(..., description="Full n8n REST API URL (e.g. http://n8n:5678)")
    n8n_api_key: str = Field(..., description="n8n API key for REST authentication")
    n8n_mcp_access_token: str = Field("", description="Optional separate native MCP API key; blank preserves the saved token")
    clear_n8n_mcp_access_token: bool = Field(False, description="Explicitly clear the saved native MCP token")
    mcp_session_timeout: int = Field(child_env.SESSION_TIMEOUT_DEFAULT_SECONDS, ge=child_env.SESSION_TIMEOUT_MIN_SECONDS,
                                     le=child_env.SESSION_TIMEOUT_MAX_SECONDS, description="MCP session timeout in seconds")
    mcp_max_sessions: int = Field(child_env.MAX_SESSIONS_DEFAULT, ge=child_env.MAX_SESSIONS_MIN,
                                  le=child_env.MAX_SESSIONS_MAX, description="Maximum concurrent MCP sessions")
    restart: bool = Field(default=True, description="Restart MCP server after update")


class ConnectionTestResult(BaseModel):
    """Result of an n8n connectivity test."""

    success: bool
    message: str
    n8n_version: str | None = None
    details: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.get("", response_model=ConnectionConfig)
async def get_config(response: Response = None) -> ConnectionConfig:
    """Return current n8n MCP connection settings with masked secrets."""
    store = get_config_store()
    snapshot = await store.load()
    configuration = configuration_status(store, snapshot, get_process_manager())
    revision_header(response, configuration)
    conn = snapshot.get("connection", {})

    api_key = conn.get("n8n_api_key", "")
    _, disabled_ops = read_policy(snapshot.get("tools", {}))
    try:
        await tool_registry.refresh()
    except Exception as exc:
        logger.warning("n8n tool registry refresh failed: %s", exc)
    return ConnectionConfig(configuration=configuration,
        n8n_api_url=conn.get("n8n_api_url", ""),
        n8n_api_key_masked=mask_secret(api_key) if api_key else "(not set)",
        n8n_mcp_access_token_masked=mask_secret(conn["n8n_mcp_access_token"]) if conn.get("n8n_mcp_access_token") else "(not set)",
        mcp_session_timeout=child_env.session_timeout_seconds(conn),
        mcp_max_sessions=child_env.max_sessions(conn),
        permissions=disabled_ops,
        permission_operations=tool_registry.operation_tools(),
    )


@router.put("/connection", response_model=dict)
async def update_connection(payload: ConnectionUpdate, response: Response = None, if_match: Annotated[str | None, Header(alias="If-Match")] = None, request: Request = None) -> dict:
    async def apply_update():
        """Update n8n connection settings and optionally restart MCP server."""
        store = get_config_store()
        # 防呆：strip 掉前後空白。貼上時多帶一個看不見的空白會讓 URL / API key 認證失敗。
        updates = {
            "n8n_api_url": payload.n8n_api_url.strip(),
            "n8n_api_key": payload.n8n_api_key.strip(),
            "n8n_mcp_access_token": payload.n8n_mcp_access_token.strip(),
            "mcp_session_timeout": str(payload.mcp_session_timeout),
            "mcp_max_sessions": str(payload.mcp_max_sessions),
        }
        # 子行程（n8n-mcp）連 n8n 前會以 SSRF 規則擋私網／loopback／metadata；存檔就比照，被擋回 400、不存檔
        # （帶帳號密碼的網址也拒絕；舊設定裡還留著的，launcher 拿掉帳密再交給 n8n-mcp 並提醒）。
        await refuse_blocked_urls(updates, URL_FIELDS)
        # 留空的 secret 欄不覆寫既有存檔值（避免只改 URL 沒重打 API key 就清空）
        updates = drop_blank_secrets(updates, ["n8n_api_key", "n8n_mcp_access_token"])
        if payload.clear_n8n_mcp_access_token:
            updates["n8n_mcp_access_token"] = ""
        await store.patch("connection", updates)
        logger.info("Updated n8n connection config")

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


@router.post("/test", response_model=ConnectionTestResult)
async def test_connection() -> ConnectionTestResult:
    """Verify the saved API key with a minimal authenticated Public API read."""
    store = get_config_store()
    conn = await store.get("connection", {})

    api_url = conn.get("n8n_api_url", "")
    api_key = conn.get("n8n_api_key", "")

    if not api_url:
        return ConnectionTestResult(
            success=False,
            message="n8n_api_url is not configured",
        )
    if not api_key:
        return ConnectionTestResult(
            success=False,
            message="n8n_api_key is not configured",
        )

    # #1530：子行程（n8n-mcp）連 n8n 前會以 SSRF 規則擋私網／loopback／metadata；測試連線比照，
    # 不然會出現「測試通過、工具全被擋」的矛盾，也避免管理 API 被拿來探測叢集內網。
    ssrf = await check_url(api_url)
    if not ssrf.ok:
        return ConnectionTestResult(
            success=False,
            message=(
                f"Blocked by SSRF protection: {ssrf.reason}. n8n-mcp refuses private, loopback and "
                "metadata addresses — use the n8n public URL (例如 https://<n8n 對外網址>)."
            ),
        )

    base_url = api_url.rstrip("/")
    if not base_url.endswith("/api/v1"):
        base_url += "/api/v1"
    url = f"{base_url}/workflows?limit=1"
    headers = {"X-N8N-API-KEY": api_key}

    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            body = resp.json()
    except httpx.ConnectError as exc:
        return ConnectionTestResult(
            success=False,
            message=f"Connection refused: {exc}",
        )
    except httpx.TimeoutException:
        return ConnectionTestResult(
            success=False,
            message=f"Connection timed out after 10 s to {url}",
        )
    except httpx.HTTPStatusError as exc:
        return ConnectionTestResult(
            success=False,
            message=f"HTTP {exc.response.status_code}: n8n Public API authentication or access failed",
        )
    except Exception as exc:
        return ConnectionTestResult(
            success=False,
            message=f"Unexpected error: {exc}",
        )

    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        return ConnectionTestResult(
            success=False,
            message="Unexpected n8n Public API workflows response",
        )

    return ConnectionTestResult(
        success=True,
        message="n8n Public API authentication and workflow list access verified",
        details={
            "url": url,
            "status_code": resp.status_code,
        },
    )


@router.put("/permissions", response_model=dict[str, Any])
async def update_permissions(payload: PermissionsUpdate, response: Response = None, if_match: Annotated[str | None, Header(alias="If-Match")] = None, request: Request = None) -> dict[str, Any]:
    async def apply_update():
        """Legacy alias: use the same reviewed policy and preserve saved denies."""
        try:
            await tool_registry.refresh()
        except Exception as exc:
            logger.warning("n8n tool registry refresh failed: %s", exc)
        store = get_config_store()
        try:
            normalized = validate_operation_update(payload.permissions, await store.get("tools", {}), tool_registry)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        await store.patch("tools", {"disabled_operations": normalized})
        try:
            ok = await get_process_manager().start_or_restart()
        except Exception:
            ok = False
            logger.warning("n8n child policy synchronization failed after saving permissions")
        return {
            "status": "ok" if ok else "partial",
            "saved": True,
            "apply_state": "proxy_policy_applied",
            "child_apply_state": "applied" if ok else "pending",
            "message": (
                "Permissions saved and applied (n8n-mcp restarted)"
                if ok
                else "Restrictions are enforced by the proxy; newly allowed operations may require a child restart"
            ),
            "disabled_operations": normalized,
            "env": {"DISABLED_TOOL_OPERATIONS": child_env.disabled_tool_operations(normalized)},
        }

    return await run_configuration_transition(get_config_store(), get_process_manager(), if_match, apply_update, response, request=request)
