"""Headscale product composition: dedicated workflows, shared security primitives."""
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from mcp_admin_core.app import create_app
from mcp_admin_core.auth.middleware import authenticate_request
from mcp_admin_core.config import get_config_store
from mcp_admin_core.profiles import get_profile_registry
from mcp_admin_core.launch_policy import LaunchPolicy
from mcp_admin_core.request_limits import McpRequestLimits
from mcp_admin_core.routers.tools import create_tools_router
from headscale_mcp_server.errors import HeadscaleError
from . import connection, log_redaction, management, tool_registry as registry
from .cookie_security import secure_cookie_enabled, secure_session_cookie
from .origin_security import allowed_origins, origin_allowed
from .admin_sessions import AdminSessions, AdminSessionStreams

LAUNCH_POLICY = LaunchPolicy(
    command="headscale-mcp-server", args=("--port", "{port}"), env_prefix="HEADSCALE_MCP_",
    connection_keys=frozenset((*connection.PUBLIC_FIELDS, *connection.SECRET_FIELDS)),
    required_connection=(("base_url", "Headscale API 位址"), ("api_key", "Headscale API 金鑰")),
)

app = create_app(
    title="Headscale 管理與配對", product_id="headscale", deployment_kind="podman",
    launch_policy=LAUNCH_POLICY, tool_registry=registry, cors_origins=[],
    static_dir=Path(__file__).resolve().parents[1] / "static", spa_routes={"", "login", "logs", "config", "tokens"},
    mcp_request_limits=McpRequestLimits(max_bytes=1024 * 1024, max_argument_elements=8192, max_depth=32),
    admin_session_policy=AdminSessions(),
    extra_routers=[connection.router, management.router,
                   create_tools_router(registry=registry, mcp_label="headscale-mcp")],
)

app.add_exception_handler(HeadscaleError, management.headscale_error)
log_redaction.install()


@app.exception_handler(RequestValidationError)
async def invalid_input(request, exc):
    return JSONResponse(status_code=422, content={"detail": "HEADSCALE_INVALID_REQUEST"})


@app.middleware("http")
async def admin_boundary(request: Request, call_next):
    # Enforce the product surface before routing (including lazily included routers).
    # General connection editing would bypass the explicit credential/origin rules.
    path = request.url.path.rstrip("/")
    if path.startswith("/private_") or (
            path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS")):
        try:
            permitted = origin_allowed(request)
        except RuntimeError:
            return JSONResponse({"detail": "ORIGIN_POLICY_UNAVAILABLE"}, status_code=503)
        if not permitted:
            return JSONResponse({"detail": "ORIGIN_DENIED"}, status_code=403)
    if path.startswith("/api/settings") and path not in (
            "/api/settings/mcp/status", "/api/settings/mcp/restart"):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    if request.url.path.startswith("/api/"):
        if request.method in ("POST", "PUT", "PATCH"):
            length = request.headers.get("content-length", "0")
            if not length.isdigit() or int(length) > 1024 * 1024:
                return JSONResponse({"detail": "REQUEST_TOO_LARGE"}, status_code=413)
            # Bound chunked request bodies too; Starlette caches this for the route.
            data = bytearray()
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > 1024 * 1024:
                    return JSONResponse({"detail": "REQUEST_TOO_LARGE"}, status_code=413)
            request._body = bytes(data)
    result = await call_next(request)
    secure_session_cookie(result)
    if request.url.path.startswith("/api/"):
        result.headers["Cache-Control"] = "no-store"
    return result


@app.post("/api/auth/logout")
async def logout(request: Request):
    # A retry of the same signed, unexpired session remains safe and idempotent.
    payload = await authenticate_request(request, allow_revoked=True)
    await request.app.state.admin_session_policy.revoke(payload)
    response = JSONResponse({"logged_out": True})
    response.delete_cookie("mcp-admin-token")
    return response


_core_lifespan = app.router.lifespan_context


@asynccontextmanager
async def lifespan(application):
    secure_cookie_enabled()  # Reject invalid deployment flags before starting the child.
    allowed_origins()
    password, _ = await get_config_store().get_admin_auth()
    if not password or password == "admin" or len(password) < 12:
        raise RuntimeError("Seed a unique management password of at least 12 characters before startup")
    await application.state.admin_session_policy.initialize()
    profile_registry = get_profile_registry()
    for profile_id in ("default", *await profile_registry.index()):
        with profile_registry.activate(profile_id):
            conn = await get_config_store().get("connection", {}) or {}
        log_redaction.set_secrets(*(conn.get(k, "") for k in connection.SECRET_FIELDS))
    async with _core_lifespan(application):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(application.state.admin_session_policy.maintain)
            try:
                yield
            finally:
                tasks.cancel_scope.cancel()


app.router.lifespan_context = lifespan
app.add_middleware(AdminSessionStreams)
