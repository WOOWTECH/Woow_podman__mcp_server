"""Connection configuration router.

Manages Odoo MCP connection settings stored in the config store.

Endpoints:
    GET  /api/config            - Current connection config
    PUT  /api/config/connection  - Update connection credentials
    POST /api/config/test        - Test XML-RPC connectivity
"""

from __future__ import annotations

from typing import Annotated
from fastapi import Request, Header, Response
from mcp_admin_core.configuration import (ConfigurationResponse, configuration_status, revision_header, run_configuration_transition)

from mcp_admin_core.connection_apply import connection_outcome
import logging
import xmlrpc.client
from typing import Any, Mapping

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from mcp_admin_core.config import get_config_store
from mcp_admin_core.process import get_process_manager
from mcp_admin_core.util.mask import mask_secret
from mcp_admin_core.util.secrets import drop_blank_secrets
from mcp_admin_core.util.xmlrpc_async import make_proxy, run_blocking

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/config", tags=["config"])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ConnectionConfig(ConfigurationResponse):
    odoo_url: str = ""
    odoo_db: str = ""
    odoo_username: str = ""
    odoo_password_masked: str = "********"


class ConnectionUpdateRequest(BaseModel):
    # 沒宣告的欄位（拼錯的鍵）回 422、不再默默忽略；422 不回顯送出的值（core validation_errors）。
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    odoo_url: str = Field(..., description="Odoo instance URL")
    odoo_db: str = Field(..., description="PostgreSQL database name")
    odoo_username: str = Field(..., description="Odoo login username")
    odoo_password: str = Field(..., description="Odoo login password")
    restart: bool = Field(default=True, description="Restart MCP server after update")


def normalize_connection_update(values: Mapping[str, Any]) -> dict[str, str]:
    """Validate supplied connection strings without requiring omitted fields."""
    validated = TypeAdapter(dict[str, str]).validate_python(dict(values))
    return {key: value.strip() for key, value in validated.items()}


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
    uid: int | None = None
    server_version: str | None = None
    details: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=ConnectionConfig)
async def get_config(response: Response = None) -> ConnectionConfig:
    """Return current Odoo connection config (password masked)."""
    store = get_config_store()
    snapshot = await store.load()
    configuration = configuration_status(store, snapshot, get_process_manager())
    revision_header(response, configuration)
    conn = snapshot.get("connection", {})
    pw = conn.get("odoo_password", "")
    return ConnectionConfig(configuration=configuration,
        odoo_url=conn.get("odoo_url", ""),
        odoo_db=conn.get("odoo_db", ""),
        odoo_username=conn.get("odoo_username", ""),
        odoo_password_masked=mask_secret(pw, prefix=1, suffix=1, min_len=2) if pw else "(not set)",
    )


@router.put("/connection", response_model=ConnectionUpdateResponse)
async def update_connection(req: ConnectionUpdateRequest, response: Response = None, if_match: Annotated[str | None, Header(alias="If-Match")] = None, request: Request = None) -> ConnectionUpdateResponse:
    async def apply_update():
        """Update Odoo connection credentials and optionally restart MCP server."""
        store = get_config_store()
        # 防呆：strip 掉前後空白。Odoo 的 XML-RPC login 不會 trim，貼上時多帶一個
        # 看不見的空白（如 " user@example.com"）就會讓 authenticate 回 False → 認證失敗。
        updates = normalize_connection_update(req.model_dump(exclude={"restart"}))
        # 留空的 secret 欄不覆寫既有存檔值（避免只改 URL 沒重打密碼就清空）
        await store.patch("connection", drop_blank_secrets(updates, ["odoo_password"]))
        logger.info("Updated Odoo connection config")

        applied = False
        started = False
        if req.restart:
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
        outcome = connection_outcome(req.restart, applied, started=started and applied)
        return ConnectionUpdateResponse(success=True, **outcome)

    return await run_configuration_transition(get_config_store(), get_process_manager(), if_match, apply_update, response, request=request)


@router.post("/test", response_model=ConnectionTestResponse)
async def test_connection() -> ConnectionTestResponse:
    """Test XML-RPC connectivity using the saved Odoo connection config.

    讀 config store（與 MCP server 子行程相同的設定來源），而非 request body。
    連線表單的密碼欄是遮罩顯示（odoo_password_masked），若改由前端送出只會送到
    遮罩值而必定認證失敗；因此測試已存檔的憑證才是唯一可靠的檢查，測試通過也代表
    MCP 子行程會用同一份設定連上 Odoo。
    """
    store = get_config_store()
    conn = await store.get("connection", {})

    url = str(conn.get("odoo_url", "")).rstrip("/")
    db = conn.get("odoo_db", "")
    username = conn.get("odoo_username", "")
    password = conn.get("odoo_password", "")

    if not url:
        return ConnectionTestResponse(success=False, message="odoo_url is not configured")

    # DATA-OMCP-007-1：同步 XML-RPC 丟進工作緒（帶 socket 逾時＋總期限），不卡事件迴圈。
    try:
        return await run_blocking(_test_connection_sync, url, db, username, password, deadline=30.0)
    except Exception as exc:  # asyncio.TimeoutError 等
        return ConnectionTestResponse(success=False, message=f"Cannot reach Odoo: {exc!r}")


def _test_connection_sync(url: str, db: str, username: str, password: str) -> ConnectionTestResponse:
    try:
        common = make_proxy(f"{url}/xmlrpc/2/common")
        version_info = common.version()
    except Exception as exc:
        return ConnectionTestResponse(success=False, message=f"Cannot reach Odoo: {exc}")

    server_version = version_info.get("server_version", "unknown") if isinstance(version_info, dict) else str(version_info)

    # Odoo 18 changed authenticate() signature: (db, credential_dict, user_agent_env)
    # Try new format first, fall back to old format for Odoo <=17 compatibility.
    try:
        try:
            # Odoo 18+ format
            result = common.authenticate(
                db,
                {"login": username, "password": password, "type": "password"},
                {},
            )
            uid = result.get("uid") if isinstance(result, dict) else result
        except (xmlrpc.client.Fault, TypeError):
            # Odoo <=17 format
            result = common.authenticate(db, username, password, {})
            uid = result.get("uid") if isinstance(result, dict) else result
    except xmlrpc.client.Fault as fault:
        return ConnectionTestResponse(success=False, message=f"XML-RPC fault: {fault.faultString}", server_version=server_version)
    except Exception as exc:
        return ConnectionTestResponse(success=False, message=f"Auth failed: {exc}", server_version=server_version)

    if not uid:
        return ConnectionTestResponse(success=False, message="Invalid credentials (uid=False)", server_version=server_version)

    return ConnectionTestResponse(
        success=True,
        message=f"Authenticated as UID {uid}",
        uid=uid,
        server_version=server_version,
        details={"database": db, "username": username},
    )
