"""FastAPI application factory for MCP Admin GUI services."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator, Sequence

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import log_redaction, setup_status
from .auth.login_throttle import LoginThrottle
from .auth.middleware import AdminSessionPolicy, AuthMiddleware, login_router, logout_router
from .auth.sessions import ConfigAdminSessions
from .capabilities import create_capabilities_router
from .config.store import ConfigAvailabilityMiddleware
from .launch_policy import LaunchPolicy, set_launch_policy
from .initial_token import ensure_initial_mcp_token
from .owner_defaults import apply_owner_defaults
from .request_limits import McpRequestLimits
from .process import get_process_manager
from .profiles import ProfileContextMiddleware, get_profile_registry
from .session_reclaim import SessionReclaimer
from .proxy import router as proxy_router
from .routers.settings import router as settings_router
from .routers.tokens import router as tokens_router
from .routers.operations import router as operations_router
from .routers.profiles import router as profiles_router
from .security_headers import SecurityHeadersMiddleware
from .validation_errors import validation_error_without_input

logger = logging.getLogger(__name__)

# 前端（@woow/mcp-admin-ui AdminApp）的 BrowserRouter 路由。SPA fallback 只對這些路徑回
# index.html，其餘一律 404（anonymous-openapi-catchall：/.env、/.git/config、/metrics 不再回 200）。
# 前端新增頁面時要同步加在這裡（或由 create_app(spa_routes=...) 覆寫）。
SPA_ROUTES: frozenset[str] = frozenset(
    {"", "login", "config", "tools", "tokens", "logs", "permissions", "settings"}
)

# FastAPI 自動文件（/docs、/redoc、/openapi.json）預設關閉：匿名可讀會洩漏整份管理 API。
# 本機開發要看時設 MCP_ADMIN_API_DOCS=1。
API_DOCS_ENV = "MCP_ADMIN_API_DOCS"


def _api_docs_enabled() -> bool:
    return os.environ.get(API_DOCS_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def create_app(
    title: str,
    extra_routers: Sequence[APIRouter] | None = None,
    *,
    static_dir: str | Path | None = None,
    cors_origins: list[str] | None = None,
    launch_policy: LaunchPolicy | None = None,
    spa_routes: frozenset[str] | set[str] | None = None,
    product_id: str | None = None,
    deployment_kind: str = "paas",
    tool_registry=None,
    operations_supported: bool = False,
    backend_mode: str = "manual",
    mcp_request_limits: McpRequestLimits | None = None,
    session_reclaimer: SessionReclaimer | None = None,
    admin_session_policy: AdminSessionPolicy | None = None,
) -> FastAPI:
    """Create and configure a FastAPI application.

    Parameters
    ----------
    title:
        The application title shown in the OpenAPI docs.
    extra_routers:
        Additional ``APIRouter`` instances to include in the app.
    static_dir:
        Path to a directory of static files to serve.
        Defaults to ``./static`` if it exists.
    cors_origins:
        List of allowed CORS origins.  Defaults to ``["*"]``.
    launch_policy:
        MCP 子行程政策（#1530）：固定 command／args、connection 與 env 白名單。
        每個 app 都應該宣告；None 只用於測試或自行組裝（沿用 config.json＋黑名單）。
    spa_routes:
        SPA fallback 會回 index.html 的前端路由（不含開頭斜線、"" 代表 /）。
        預設 :data:`SPA_ROUTES`；其他路徑一律 404。
    session_reclaimer:
        子行程的 Streamable HTTP session 有名額上限的產品（n8n）：名額已滿時由閘道回收最久沒活動的閒置
        session 再重試 initialize（見 :mod:`mcp_admin_core.session_reclaim`）。None＝不記錄、不回收。
    admin_session_policy:
        Optional product policy; the default persists per-login revocation in the
        private config volume. Existing JWTs without a session ID require login.
    """
    set_launch_policy(launch_policy)
    # #1614：URL 裡的憑證（/private_<token>、Logs 頁 EventSource 的 ?token=<JWT>）不進 log。
    # 每個 app 都經 create_app 組裝，所以在這裡統一安裝（idempotent）。
    log_redaction.install()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        # 負責人 2026-10-25「預設全開」：沒有標記的既有 config.json 在子行程啟動前一次性改成全開
        # （見 owner_defaults）；讀不到設定時這裡拋 ConfigUnavailable，子行程不會啟動。
        await apply_owner_defaults()
        # 第三輪 G4：從來沒有過 MCP token 的設定（PaaS 新服務、本行程新建的設定）第一次啟動就發一個，不必先到 Tokens 頁
        # 輪替才連得上（見 initial_token）；撤銷過的不動。放在 owner_defaults 之後，標記的 source 照舊看第一次啟動時的狀態。
        await ensure_initial_mcp_token()
        # Startup: start MCP server if configured
        pm = get_process_manager()
        await pm.start()
        await get_profile_registry().start_all()
        yield
        # Shutdown: stop MCP server
        await get_profile_registry().stop_all()
        await pm.stop()

    docs_on = _api_docs_enabled()
    app = FastAPI(
        title=title,
        lifespan=lifespan,
        docs_url="/docs" if docs_on else None,
        redoc_url="/redoc" if docs_on else None,
        openapi_url="/openapi.json" if docs_on else None,
    )
    # 管理登入的暴力破解限流（每個 app 一份；環境變數見 auth/login_throttle.py）
    app.state.login_throttle = LoginThrottle.from_env()
    # Per-app registry: proxy operation selectors must not leak across products.
    app.state.tool_registry = tool_registry
    app.state.mcp_request_limits = mcp_request_limits
    app.state.session_reclaimer = session_reclaimer
    app.state.admin_session_policy = admin_session_policy if admin_session_policy is not None else ConfigAdminSessions()
    # 422 不回顯送出的值（連線設定帶密碼／API key；見 validation_errors）。
    app.add_exception_handler(RequestValidationError, validation_error_without_input)

    # -- Auth middleware -------------------------------------------------------
    app.state.admin_auth_required = True
    # AuthMiddleware is added after ProfileContextMiddleware and therefore runs
    # first. Admin JWTs always validate against the default store before a
    # management request can select a profile.
    app.add_middleware(ProfileContextMiddleware)
    app.add_middleware(AuthMiddleware)
    app.add_middleware(ConfigAvailabilityMiddleware)

    # -- CORS ------------------------------------------------------------------
    origins = cors_origins if cors_origins is not None else ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # -- Security headers（#1372）-----------------------------------------------
    # 最後加入＝最外層：401／CORS preflight／SPA／API 回應都帶到；/private_* MCP proxy
    # 由 middleware 自行略過，不影響 SSE／串流。
    app.add_middleware(SecurityHeadersMiddleware)

    # -- Core routers ----------------------------------------------------------
    app.include_router(login_router)
    if admin_session_policy is None:
        app.include_router(logout_router)
    app.include_router(settings_router)
    app.include_router(tokens_router)
    app.include_router(operations_router)
    app.include_router(profiles_router)
    app.include_router(proxy_router)
    if product_id is not None and tool_registry is not None:
        app.include_router(create_capabilities_router(
            product_id=product_id, deployment_kind=deployment_kind, registry=tool_registry,
            operations_supported=operations_supported, backend_mode=backend_mode,
        ))

    if extra_routers:
        for router in extra_routers:
            app.include_router(router)

    # -- Health endpoint (before SPA fallback) ---------------------------------
    @app.get("/healthz", tags=["system"])
    async def healthz() -> dict[str, str]:
        # k8s probe 只看狀態碼（恆 200）。mcp 是給平台服務頁的粗略狀態，不含任何細節：running｜not_configured
        # （必要連線欄位沒填）｜not_running（見 setup_status.mcp_state）。宣告了後端連線欄位（LaunchPolicy.backend_connection）
        # 的產品另帶 backend：configured｜missing（讀不到設定就不帶）。不等設定檔的鎖：寫檔卡住時 probe 也不會逾時。
        return {"status": "ok", **await setup_status.health_fields()}

    # -- Static files + SPA fallback (must be last) ----------------------------
    if static_dir is None:
        static_dir = Path.cwd() / "static"
    else:
        static_dir = Path(static_dir)

    if static_dir.is_dir():
        assets_dir = static_dir / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

        index_html = static_dir / "index.html"
        if index_html.is_file():
            _index_content = index_html.read_text()
            _spa_routes = frozenset(SPA_ROUTES if spa_routes is None else spa_routes)

            @app.get("/{path:path}", include_in_schema=False)
            async def spa_fallback(path: str) -> HTMLResponse:
                # 只回前端已知路由；/api/*、點開頭（/.env、/.git）、缺檔與任意路徑都 404。
                if path.strip("/") not in _spa_routes:
                    raise HTTPException(status_code=404, detail="Not Found")
                return HTMLResponse(_index_content)

        logger.info("Serving SPA from %s", static_dir)
    else:
        logger.debug("No static directory found at %s; skipping", static_dir)

    logger.info("Created application '%s'", title)
    return app
