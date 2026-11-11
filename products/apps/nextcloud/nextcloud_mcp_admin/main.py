"""Nextcloud MCP Admin —— FastAPI 應用進入點。

包 vendored ``nextcloud_mcp_server``（FastMCP，9 支工具，來源 WOOWTECH/Woow_nextcloud_mcp_server，MIT）。
與 apps/litellm 同分層：共用 mcp_admin_core 的 app factory＋hoisted routers（tools／health／logs），只保留
app-specific 的 connection config、tool registry、health probes、Tools 政策 → 子行程的重啟掛勾。

啟動順序：先補預設（``seed_defaults``：新安裝全開、env_prefix）、登記 app password 供 log 遮罩，
再進 core lifespan 依 config.json 拉起 MCP 子行程（``nextcloud-mcp-server`` launcher）。

Run with:
    uvicorn nextcloud_mcp_admin.main:app --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from mcp_admin_core.app import create_app
from mcp_admin_core.config import get_config_store
from mcp_admin_core.profiles import get_profile_registry
from mcp_admin_core.profiles import current_profile_id
from mcp_admin_core.launch_policy import LaunchPolicy
from mcp_admin_core.routers.health import make_health_router
from mcp_admin_core.routers.logs import create_logs_router
from mcp_admin_core.routers.tools import create_tools_router

from . import health_probes, log_redaction, seed_defaults, tool_policy, tool_registry
from .routers import config

# 子行程政策（woow_paas_platform#1530）：command／args 固定；connection（base_url／username／app_password）以
# NEXTCLOUD_MCP_ 前綴注入。工具開關（NEXTCLOUD_MCP_READONLY／ALLOW_DELETE／DISABLED_TOOLS）不走 mcp_server.env，
# 由 launcher 每次啟動依 Tools 頁的 tools.disabled 重新計算（見 launcher.py），所以 env_keys 為空。
# 三個連線欄位缺任何一個，vendored server 就以 exit 2 結束（Settings 必填）：在 Connection 頁存好之前，MCP 用戶端拿到的是
# 閘道的佔位 MCP（說明要填哪些欄位，mcp_admin_core/setup_status.py），/healthz 的 mcp 是 not_configured。
# 標籤與 frontend/src/appConfig.jsx 的 Connection 頁相同（tests/test_setup_status.py 比對）。
# 不宣告 backend_connection：三個連線欄位都已是 required_connection（缺了子行程就起不來），/healthz 的 mcp 已經是
# not_configured；一個鍵也只能是其中一種。
LAUNCH_POLICY = LaunchPolicy(
    command="nextcloud-mcp-server",
    args=("--port", "{port}"),
    env_prefix="NEXTCLOUD_MCP_",
    connection_keys=frozenset({"base_url", "username", "app_password"}),
    required_connection=(
        ("base_url", "Nextcloud 網址"),
        ("username", "Nextcloud 帳號"),
        ("app_password", "App password"),
    ),
)

app = create_app(
    title="Nextcloud MCP Admin",
    extra_routers=[
        config.router,
        create_tools_router(registry=tool_registry, mcp_label="nextcloud-mcp"),
        make_health_router(app_type="nextcloud",
                           probe_target=health_probes.probe_target,
                           fetch_info=health_probes.fetch_info),
        create_logs_router("nextcloud_mcp_admin"),
    ],
    launch_policy=LAUNCH_POLICY,
    product_id="nextcloud",
    deployment_kind="podman",
    tool_registry=tool_registry,
)

# Tools 頁開放原本被停用的工具時重啟子行程（子行程啟動時才依政策註冊工具，見 tool_policy）。
app.add_middleware(tool_policy.ToolPolicyMiddleware)

# path token 與 app password 不進 log。
log_redaction.install()
app.state.profile_deleted = log_redaction.remove_profile_secrets
app.state.connection_saved = lambda conn: log_redaction.set_profile_secrets(
    current_profile_id(), str(conn.get("app_password") or ""), str(conn.get("username") or ""))

_core_lifespan = app.router.lifespan_context


async def _register_secrets() -> None:
    registry = get_profile_registry()
    for profile_id in ("default", *await registry.index()):
        with registry.activate(profile_id):
            conn = await get_config_store().get("connection", {}) or {}
        log_redaction.set_profile_secrets(profile_id, str(conn.get("app_password") or ""),
                                          str(conn.get("username") or ""))


@asynccontextmanager
async def _lifespan_with_seed_defaults(a):
    await seed_defaults.apply_seed_defaults()
    await _register_secrets()
    async with _core_lifespan(a):
        yield


app.router.lifespan_context = _lifespan_with_seed_defaults
