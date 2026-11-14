"""Settings router — full config.json management via Web GUI.

Provides CRUD for all config sections: connection, mcp_server,
proxy, tools, admin_password, mcp_auth_token.

Endpoints:
    GET  /api/settings           - Full config (password masked)
    GET  /api/settings/{section} - Get one section
    PUT  /api/settings/{section} - Replace one section（connection／mcp_server／proxy／tools／admin_password）
    POST /api/settings/mcp/restart - Restart MCP server process
    GET  /api/settings/mcp/status  - MCP server process status
"""

from __future__ import annotations

import logging
import secrets
from typing import Any, Annotated

from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from ..config import get_config_store
from ..config.store import get_default_config_store
from ..configuration import ConfigurationResponse, configuration_status, revision_header, run_configuration_transition
from ..connection_apply import refuse_blocked_urls, save_bound_connection
from ..launch_policy import get_launch_policy, is_blocked_env_name
from ..owner_defaults import owner_defaults_view
from ..process import get_process_manager, resolve_launch
from ..util.mask import mask_secret
from ..util.secrets import drop_blank_secrets

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class McpServerConfig(BaseModel):
    """MCP server process configuration."""

    command: str = Field("", description="Executable command (e.g. 'odoo-mcp-server')")
    args: list[str] = Field(default_factory=list, description="Command-line arguments")
    port: int = Field(8000, ge=1, le=65535, description="MCP server listen port")
    env: dict[str, str] = Field(default_factory=dict, description="Environment variables")


class ProxyConfig(BaseModel):
    """MCP proxy configuration."""

    timeout: int = Field(86400, description="Proxy timeout in seconds")
    bearer_token: str | None = Field(None, description="Optional Bearer token for upstream")


class ConnectionConfig(BaseModel):
    """Upstream service connection settings (Odoo or n8n)."""

    # Generic — routers use whatever keys are present
    model_config = {"extra": "allow"}


class ToolsConfig(BaseModel):
    """Tool enable/disable settings."""

    disabled: list[str] = Field(default_factory=list)
    disabled_operations: dict[str, list[str]] = Field(default_factory=dict)


class FullConfig(ConfigurationResponse):
    """Complete config for GET /api/settings (password masked)."""

    admin_password_masked: str = ""
    # Keep the legacy key for response compatibility, but never return the stored token here.
    mcp_auth_token: str = ""
    mcp_auth_token_masked: str = ""
    mcp_auth_token_set: bool = False
    connection: dict[str, Any] = {}
    mcp_server: dict[str, Any] = {}
    proxy: dict[str, Any] = {}
    tools: dict[str, Any] = {}
    # #1530：子行程 command／args／port／env_prefix 由部署與 app 決定，管理頁只能看。
    # editable_env_keys＝管理頁可改的 mcp_server.env 鍵；None＝app 未宣告政策（沿用黑名單）。
    mcp_server_locked: bool = True
    editable_env_keys: list[str] | None = None
    # 負責人 2026-10-25「預設全開」是否已套用在這份設定（新安裝／既有設定一次性遷移），見 owner_defaults。
    owner_defaults: dict[str, Any] | None = None


class McpProcessStatus(ConfigurationResponse):
    """MCP server process status."""

    running: bool
    pid: int | None = None
    restart_count: int = 0
    command: str = ""
    port: int = 8000
    exit_code: int | None = None
    # 子行程未運行時，最後幾行輸出（stdout+stderr），供管理頁顯示 exit 原因
    last_output: list[str] = Field(default_factory=list)
    # #1500 看門狗：子行程意外結束後的自動重啟次數／是否已排程／距下次重試秒數
    auto_restart_count: int = 0
    auto_restart_pending: bool = False
    next_auto_restart_in: float | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_secret_key(key: str) -> bool:
    """connection 祕密欄判定 heuristic（含 kubeconfig）。

    kubeconfig 常內含 client-key-data / token（等同 cluster-admin 憑證），
    必須與 password/key/secret 一併遮罩，避免經 GET 端點明文外洩。
    """
    k = key.lower()
    return "password" in k or "key" in k or "secret" in k or "token" in k or "kubeconfig" in k


def _mask_connection(conn: dict[str, Any]) -> dict[str, Any]:
    """回傳 *conn* 的複本，對祕密欄套遮罩。

    ``max_stars=8`` 為必要上限：3KB 的 kubeconfig 若無上限會在 GET 回應塞入
    約三千個星號；帶上限後輸出長度有界（前 2 + 至多 8 星 + 後 2）。
    """
    masked = dict(conn)
    for key in list(masked.keys()):
        if _is_secret_key(key):
            value = masked[key]
            masked[key] = (
                mask_secret(str(value), prefix=2, suffix=2, min_len=4, max_stars=8)
                if value
                else ""
            )
    return masked


def _mask_proxy(proxy: dict[str, Any]) -> dict[str, Any]:
    """Return proxy settings with the bearer credential masked for read responses."""
    masked = dict(proxy)
    value = masked.get("bearer_token")
    placeholder = (
        mask_secret(str(value), prefix=4, suffix=4, min_len=8, max_stars=20)
        if value
        else ""
    )
    if value or "bearer_token" in masked:
        masked["bearer_token"] = placeholder
        masked["bearer_token_masked"] = placeholder
    return masked


def _mask_mcp_auth_token(value: Any) -> str:
    """Mask the configured MCP token in all settings read responses."""
    return mask_secret(str(value), prefix=4, suffix=4, min_len=8, max_stars=20) if value else ""


def _drop_unchanged_masked_proxy_secret(
    updates: dict[str, Any], current: dict[str, Any]
) -> dict[str, Any]:
    """Treat the exact masked bearer value from a GET response as unchanged on PUT."""
    result = dict(updates)
    # Response-only metadata must not become persisted proxy configuration.
    result.pop("bearer_token_masked", None)
    value = current.get("bearer_token")
    if value:
        placeholder = mask_secret(str(value), prefix=4, suffix=4, min_len=8, max_stars=20)
        if result.get("bearer_token") == placeholder:
            result.pop("bearer_token")
    return result


# 管理 API 可以寫入的 config 區段（#1530）。其他頂層鍵（mcp_auth_token、token_history…）
# 只能經各自的專用端點（rotate 等）變更。
WRITABLE_SECTIONS = frozenset({"connection", "mcp_server", "proxy", "tools", "admin_password"})

# mcp_server 裡由部署／app 決定、管理頁不能改的鍵（#1530：改 command／args 等於在容器裡執行任意程式）。
LOCKED_MCP_SERVER_KEYS = ("command", "args", "port", "env_prefix")


def _env_key_editable(key: str) -> bool:
    policy = get_launch_policy()
    if policy is not None:
        return key in policy.editable_env_keys
    return not is_blocked_env_name(key)


def _effective_mcp_server(stored: dict[str, Any]) -> dict[str, Any]:
    """實際生效的 command／args／port／env_prefix（有 app 政策時以政策為準）。"""
    policy = get_launch_policy()
    command, args, port = resolve_launch(stored, policy)
    env_prefix = policy.env_prefix if policy is not None else stored.get("env_prefix", "")
    return {"command": command, "args": args, "port": port, "env_prefix": env_prefix}


def _mcp_server_view(stored: dict[str, Any]) -> dict[str, Any]:
    """GET 用：顯示實際生效值；env 只列管理頁可改的鍵（部署 seed 的 AUTH_TOKEN 等不外露）。"""
    view = dict(stored)
    view.update(_effective_mcp_server(stored))
    env = stored.get("env") or {}
    policy = get_launch_policy()
    if policy is not None:
        env = {k: v for k, v in env.items() if k in policy.editable_env_keys}
    view["env"] = dict(env)
    return view


def _locked_value_matches(key: str, sent: Any, effective: Any) -> bool:
    if key == "port":
        try:
            return int(sent) == int(effective)
        except (TypeError, ValueError):
            return False
    if key == "args":
        return isinstance(sent, list) and [str(a) for a in sent] == [str(a) for a in effective]
    return sent == effective


def _mcp_server_env_update(body: dict[str, Any], stored: dict[str, Any]) -> dict[str, str] | None:
    """驗證 PUT mcp_server 的 body，回傳更新後的 env（沒有要改 env 時回 None）。

    - command／args／port／env_prefix：只能原樣送回（共用 SettingsPage 會整包送），值不同就 400。
    - env：只接受可編輯的鍵；其他鍵只能原樣送回（沒改）；不能藉此刪除部署 seed 的鍵。
    """
    effective = _effective_mcp_server(stored)
    for key, sent in body.items():
        if key in LOCKED_MCP_SERVER_KEYS:
            if not _locked_value_matches(key, sent, effective[key]):
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"mcp_server.{key} is fixed by the deployment and cannot be changed from the admin page "
                    f"(MCP 子行程的 {key} 由部署決定，不能從管理頁修改)",
                )
        elif key not in ("env", "env_delete"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown mcp_server field: {key}")

    if "env" not in body and "env_delete" not in body:
        return None
    sent_env = body.get("env", {})
    if not isinstance(sent_env, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "mcp_server.env must be an object")
    current_env = {str(k): str(v) for k, v in (stored.get("env") or {}).items()}
    new_env = dict(current_env)
    deleted = body.get("env_delete", [])
    if not isinstance(deleted, list) or any(not isinstance(k, str) for k in deleted):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "mcp_server.env_delete must be a list of names")
    if set(deleted) & sent_env.keys():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot set and delete the same environment variable")
    for key in deleted:
        if not _env_key_editable(key):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Environment variable cannot be deleted: {key}")
        new_env.pop(key, None)
    rejected: list[str] = []
    for key, value in sent_env.items():
        if isinstance(value, (dict, list)) or value is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"mcp_server.env.{key} must be a string")
        value = str(value)
        if _env_key_editable(str(key)):
            new_env[str(key)] = value
        elif current_env.get(str(key)) != value:
            rejected.append(str(key))
    if rejected:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"These environment variables cannot be changed from the admin page: {sorted(rejected)} "
            "(這些環境變數不能從管理頁修改)",
        )
    return new_env if new_env != current_env else None


def _validate_connection_keys(body: dict[str, Any], stored_mcp: dict[str, Any]) -> None:
    """connection 的每個鍵都會變成子行程環境變數（#1530）：只收 app 宣告的鍵。"""
    policy = get_launch_policy()
    if policy is not None:
        unknown = sorted(k for k in body if k not in policy.connection_keys)
        if unknown:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Unknown connection field(s): {unknown}. Allowed: {sorted(policy.connection_keys)}",
            )
        return
    prefix = stored_mcp.get("env_prefix", "")
    blocked = sorted(k for k in body if is_blocked_env_name(f"{prefix}{str(k).upper()}"))
    if blocked:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Connection field(s) not allowed: {blocked}")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=FullConfig)
async def get_settings(response: Response = None) -> FullConfig:
    """Return full config with sensitive values masked."""
    store = get_config_store()
    cfg = await store.load()
    configuration = configuration_status(store, cfg, get_process_manager())
    revision_header(response, configuration)

    # One admin identity protects every profile. Extra profile files carry an
    # internal placeholder password that must never be presented as a login.
    from mcp_admin_core.profiles import DEFAULT_PROFILE, current_profile_id
    password = (cfg if current_profile_id() == DEFAULT_PROFILE
                else await get_default_config_store().load()).get("admin_password", "")
    conn = _mask_connection(dict(cfg.get("connection", {})))

    policy = get_launch_policy()
    mcp_token = "" if cfg.get("mcp_auth_revoked", False) else cfg.get("mcp_auth_token", "") or ""
    masked_mcp_token = _mask_mcp_auth_token(mcp_token)
    return FullConfig(
        configuration=configuration,
        admin_password_masked=mask_secret(password, prefix=2, suffix=2, min_len=4) if password else "(not set)",
        mcp_auth_token=masked_mcp_token,
        mcp_auth_token_masked=masked_mcp_token,
        mcp_auth_token_set=bool(mcp_token),
        connection=conn,
        mcp_server=_mcp_server_view(cfg.get("mcp_server", {}) or {}),
        proxy=_mask_proxy(cfg.get("proxy", {}) or {}),
        tools=cfg.get("tools", {}),
        editable_env_keys=sorted(policy.editable_env_keys) if policy is not None else None,
        owner_defaults=owner_defaults_view(cfg),
    )


@router.get("/{section}")
async def get_section(section: str, response: Response = None) -> dict[str, Any]:
    """Get a single config section."""
    store = get_config_store()
    snapshot = await store.load()
    revision_header(response, configuration_status(store, snapshot, get_process_manager()))
    value = snapshot.get(section)
    if value is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown section: {section}")
    if section == "mcp_auth_token":
        return {"value": "********" if value else ""}
    if isinstance(value, dict):
        if section == "connection":
            return _mask_connection(dict(value))
        if section == "mcp_server":
            return _mcp_server_view(value)
        if section == "proxy":
            return _mask_proxy(value)
        return value
    if _is_secret_key(section) or section == "admin_session_generation":
        return {"value": "********" if value else ""}
    return {"value": value}


@router.post("/mcp_auth_token/reveal")
async def reveal_mcp_auth_token(response: Response) -> dict[str, str]:
    """Reveal the MCP token only for the explicit Token Manager action."""
    response.headers["Cache-Control"] = "no-store"
    store = get_config_store()
    cfg = await store.load()
    return {"value": "" if cfg.get("mcp_auth_revoked", False) else cfg.get("mcp_auth_token", "") or ""}


@router.post("/proxy/bearer_token/reveal")
async def reveal_proxy_bearer_token(response: Response) -> dict[str, str]:
    """Reveal the proxy bearer token only for its explicit Settings action."""
    response.headers["Cache-Control"] = "no-store"
    store = get_config_store()
    proxy = await store.get("proxy", {}) or {}
    return {"value": proxy.get("bearer_token", "") or ""}


@router.put("/{section}")
async def put_section(section: str, body: dict[str, Any], request: Request, response: Response = None, if_match: Annotated[str | None, Header(alias="If-Match")] = None) -> dict[str, Any]:
    async def apply_update():
        """Replace a config section. Restarts MCP server if connection or mcp_server changed."""
        store = get_config_store()
        token_rotated = False

        if section not in WRITABLE_SECTIONS:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Section '{section}' cannot be changed here")

        # Special handling for admin_password
        if section == "admin_password":
            from mcp_admin_core.profiles import DEFAULT_PROFILE, current_profile_id
            if current_profile_id() != DEFAULT_PROFILE:
                raise HTTPException(status.HTTP_409_CONFLICT,
                                    "Admin password is global; select the default profile")
            new_pass = body.get("value", "")
            if not new_pass or len(new_pass) < 4:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Password must be at least 4 characters")
            await store.put("admin_password", new_pass)
            return {"status": "ok", "message": "Admin password updated"}

        if section == "connection":
            _validate_connection_keys(body, await store.get("mcp_server", {}) or {})
            # 全量替換易靜默清空 secret，故 strip 後套 drop_blank_secrets + 淺合併：
            # 留空的 secret 欄沿用既有存檔值（比照連線表單載入即清空 secret 的慣例）。
            stripped = {k: (v.strip() if isinstance(v, str) else v) for k, v in body.items()}
            # 後端網址欄位（LaunchPolicy.url_connection_keys）存檔前過 SSRF：與產品自己的 Connection 頁同一套檢查，
            # 這條共用路徑不能拿來繞過。
            policy = get_launch_policy()
            if policy is not None and policy.normalize_connection is not None:
                # Allowed secret nulls explicitly clear the saved credential.
                # Validate all other values before restoring these nulls or saving.
                cleared_secrets = {k: None for k, v in body.items()
                                   if v is None and _is_secret_key(k)}
                try:
                    stripped = policy.normalize_connection(
                        {k: v for k, v in body.items() if k not in cleared_secrets})
                except ValueError:
                    # Validation errors may contain submitted credentials.
                    raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                        "Invalid connection field value") from None
                stripped.update(cleared_secrets)
            if policy is not None:
                await refuse_blocked_urls(stripped, policy.url_connection_keys)
            secret_fields = [k for k in stripped if _is_secret_key(k)]
            cleaned = drop_blank_secrets(stripped, secret_fields)
            if policy is not None and policy.identity_connection_keys:
                from .tokens import new_token
                merged, token_rotated = await save_bound_connection(store, get_process_manager(),
                    cleaned, identity_keys=policy.identity_connection_keys, make_token=new_token)
            else:
                merged = await store.patch("connection", cleaned)
            on_connection_saved = getattr(request.app.state, "connection_saved", None)
            if on_connection_saved is not None:
                on_connection_saved(merged)
        elif section == "tools":
            from mcp_admin_core.operation_policy import read_policy, validate_operation_update
            from mcp_admin_core.config.store import ConfigUnavailable
            current = await store.get("tools", {})
            try:
                read_policy(body)
            except ConfigUnavailable:
                raise HTTPException(400, detail="Invalid tool policy")
            if "disabled_operations" in body:
                try:
                    body["disabled_operations"] = validate_operation_update(
                        body["disabled_operations"], current, getattr(request.app.state, "tool_registry", None))
                except ValueError as exc:
                    raise HTTPException(400, detail=str(exc)) from exc
            await store.patch("tools", body)
            return {"status": "ok", "saved": True, "apply_state": "proxy_policy_applied",
                    "message": "Tool policy saved and enforced by the proxy. Child-specific restrictions may require a restart."}
        elif section == "mcp_server":
            # #1530：command／args／port／env_prefix 唯讀；env 只收可編輯的鍵（淺合併，部署 seed 的鍵保留）。
            new_env = _mcp_server_env_update(body, await store.get("mcp_server", {}) or {})
            if new_env is None:
                return {"status": "ok", "saved": False, "apply_state": "unchanged",
                        "message": "No changes to the MCP server settings"}
            await store.patch("mcp_server", {"env": new_env})
        elif section == "proxy":
            current = await store.get("proxy", {}) or {}
            updates = _drop_unchanged_masked_proxy_secret(body, current)
            updates = drop_blank_secrets(updates, ("bearer_token",))
            if "bearer_token" not in updates and current.get("bearer_token"):
                updates["bearer_token"] = current["bearer_token"]
            await store.put("proxy", updates)
        else:
            await store.put(section, body)
        logger.info("Updated config section: %s", section)

        # 連線／子行程設定變更 → 套用到子行程：運行中 restart；未運行（如開機因缺設定
        # 已 exit）則 start（#1350：舊版只在 is_running 時 restart，存設定也拉不起子行程）。
        if section in ("connection", "mcp_server"):
            pm = get_process_manager()
            was_running = pm.is_running
            try:
                ok = await pm.start_or_restart()
            except Exception:
                logger.error("Saved settings but MCP server could not start")
                ok = False
            if ok:
                action = "restarted" if was_running else "started"
                message = f"Section '{section}' saved, MCP server {action}; backend not verified"
                if token_rotated:
                    message += ". Account changed: old MCP URL revoked. Copy the new URL from Tokens."
                return {"status": "ok", "saved": True, "apply_state": "applied",
                        "token_rotated": token_rotated, "message": message}
            message = f"Section '{section}' saved, but MCP server failed to start; check process logs"
            if token_rotated:
                message += ". Account changed: old MCP URL revoked. Copy the new URL from Tokens."
            return {
                "status": "partial", "saved": True, "apply_state": "failed",
                "token_rotated": token_rotated, "message": message,
            }

        return {"status": "ok", "message": f"Section '{section}' updated"}

    return await run_configuration_transition(get_config_store(), get_process_manager(), if_match, apply_update, response, request=request)


@router.post("/mcp_auth_token/rotate")
async def rotate_mcp_token(response: Response = None,
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
        request: Request = None) -> dict[str, Any]:
    """Compatibility route using the same token lifecycle as /api/tokens."""
    from .tokens import rotate_token
    return await rotate_token(response=response, if_match=if_match, request=request)


@router.get("/mcp/status", response_model=McpProcessStatus)
async def mcp_status(response: Response = None) -> McpProcessStatus:
    """Return MCP server process status."""
    pm = get_process_manager()
    info = await pm.status()
    if info.get("configuration"):
        revision_header(response, info["configuration"])
    return McpProcessStatus(**info)


@router.post("/mcp/apply")
async def mcp_apply(response: Response, if_match: Annotated[str | None, Header(alias="If-Match")] = None, request: Request = None) -> dict[str, Any]:
    """Retry the saved snapshot without re-submitting an editor's values."""
    store, pm = get_config_store(), get_process_manager()
    async def apply_update():
        current = configuration_status(store, await store.load(), pm)
        if current["apply_state"] == "applied":
            return {"status": "ok", "saved": True, "apply_state": "applied", "message": "目前設定已套用。後端連線仍需另行驗證。"}
        if getattr(pm, "_verifying_configuration", False) and pm._attempted_revision == current["saved_revision"]:
            return {"status": "ok", "saved": True, "apply_state": "pending", "message": "正在確認子程序設定，請稍候。"}
        ok = await pm.start_or_restart()
        return {"status": "ok" if ok else "partial", "saved": True,
                "apply_state": "pending" if ok else "failed",
                "message": "正在確認子程序設定。" if ok else "設定已儲存，但子程序無法啟動。請檢查服務狀態後重試。"}
    return await run_configuration_transition(store, pm, if_match, apply_update, response, request=request)


@router.post("/mcp/restart")
async def mcp_restart(response: Response = None, if_match: Annotated[str | None, Header(alias="If-Match")] = None, request: Request = None) -> dict[str, Any]:
    async def apply_update():
        """Restart the MCP server process."""
        pm = get_process_manager()
        ok = await pm.restart()
        if ok:
            return {"status": "ok", "message": "MCP server restarted"}
        return {"status": "error", "message": "Failed to restart MCP server — check command config"}

    return await run_configuration_transition(get_config_store(), get_process_manager(), if_match, apply_update, response, request=request, operation_kind="mcp.restart", two_phase_restart=True)
