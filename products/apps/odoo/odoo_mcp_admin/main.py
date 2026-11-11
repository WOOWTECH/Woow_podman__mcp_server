"""Odoo MCP Admin - FastAPI application entry point.

Run with:
    uvicorn odoo_mcp_admin.main:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from mcp_admin_core.app import create_app
from mcp_admin_core.launch_policy import LaunchPolicy
from mcp_admin_core.routers.health import make_health_router
from mcp_admin_core.routers.logs import create_logs_router
from mcp_admin_core.routers.tools import create_tools_router

from . import allowed_methods, health_probes, seed_defaults, tool_registry
from .routers import config

# command／args 固定；connection 只注入四個連線欄位。
# 四個寫入開關（審核寫入、直接寫入、未審查方法、chatter 直接張貼）都是 Settings 頁開關：新安裝全開
# （負責人 2026-10-25「預設全開」，見 seed_defaults），既有設定缺鍵時維持原本的值；任意 env 仍不開放，
# 也不取消 Odoo 帳號自己的 ACL。
# command 是 ``woow-odoo-mcp``（launcher.py）：與上游 ``odoo-mcp`` 的參數／環境變數／輸出相同，
# 只在啟動前讓唯讀工具改在工作緒執行，多個 session 並發時不再互相卡住（DATA-ODOO-011-1）。
# 後端連線欄位（backend_connection）：沒有連線子行程照樣起來、列出 41 支工具，但上游只有在 ODOO_URL／ODOO_DB／
# ODOO_USERNAME／ODOO_PASSWORD 四個都在時才建 Odoo 連線，XML-RPC 的 authenticate 也要資料庫、帳號與密碼（API Key），
# 缺任何一個每支碰 Odoo 的工具都失敗——/healthz 的 backend 是 missing，平台顯示較輕的「尚未設定後端連線」提示。
# 標籤與 frontend/src/appConfig.jsx 的 Connection 頁相同（tests/test_backend_status.py 比對）。
LAUNCH_POLICY = LaunchPolicy(
    command="woow-odoo-mcp",
    args=("--transport", "streamable-http", "--host", "127.0.0.1", "--port", "{port}"),
    env_prefix="",
    connection_keys=frozenset({"odoo_url", "odoo_db", "odoo_username", "odoo_password"}),
    identity_connection_keys=config.ODOO_IDENTITY_KEYS,
    env_keys=frozenset(seed_defaults.POLICY_KEYS),
    editable_env_keys=frozenset(seed_defaults.POLICY_KEYS),
    derived_env=allowed_methods.child_env,
    fresh_env=seed_defaults.FRESH_ENV,
    backend_connection=(
        ("odoo_url", "Odoo URL"),
        ("odoo_db", "Database"),
        ("odoo_username", "Username"),
        ("odoo_password", "API Key"),
    ),
    normalize_connection=config.normalize_connection_update,
    max_extra_profiles=5,
    profile_child_memory_mib=100,
)

app = create_app(
    title="Odoo MCP Admin",
    extra_routers=[
        config.router,
        allowed_methods.router,
        create_tools_router(registry=tool_registry, mcp_label="odoo-mcp"),
        make_health_router(app_type="odoo",
                           probe_target=health_probes.probe_target,
                           fetch_info=health_probes.fetch_info),
        create_logs_router("odoo_mcp_admin"),
    ],
    launch_policy=LAUNCH_POLICY,
    product_id="odoo",
    deployment_kind="podman",
    tool_registry=tool_registry,
)

_core_lifespan = app.router.lifespan_context


@asynccontextmanager
async def _lifespan_with_seed_defaults(a):
    await seed_defaults.apply_seed_defaults()
    async with _core_lifespan(a):
        yield


app.router.lifespan_context = _lifespan_with_seed_defaults
