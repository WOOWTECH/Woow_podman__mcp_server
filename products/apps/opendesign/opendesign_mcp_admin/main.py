"""Open Design MCP Admin —— FastAPI 應用進入點。

包 vendored od_mcp_server＋原生 launcher（15 工具、包 Open Design daemon REST API）。與
apps/vk 同分層：共用 mcp_admin_core 的 app factory + hoisted routers（tools / health / logs），
只保留 app-specific 的 connection config、tool registry、health probes。

Run with:
    uvicorn opendesign_mcp_admin.main:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from mcp_admin_core.app import create_app
from mcp_admin_core.launch_policy import LaunchPolicy
from mcp_admin_core.routers.health import make_health_router
from mcp_admin_core.routers.logs import create_logs_router
from mcp_admin_core.routers.tools import create_tools_router

from . import health_probes, seed_migration, tool_registry
from .routers import config

# 子行程政策（woow_paas_platform#1530）：固定跑原生 launcher（#1369），舊 supergateway seed 不再被執行；
# connection 只注入連線頁的四個欄位（OD_API_BASE／OD_BASIC_USER／OD_BASIC_PASSWORD／OD_CHAT_TIMEOUT）。
# 後端連線欄位（backend_connection）只有 daemon 網址：沒填時子行程照樣起來、列出 15 支工具，但連不到 daemon（存成空字串時
# 網址是空的；從沒存過時是上游預設的 http://open-design-svc:7457，PaaS 裡沒有這個名字），每支工具都失敗——管理頁自己的 Test
# 也說「od_api_base 未設定」。Basic Auth 兩欄是選填（自架、沒有認證的 daemon 留空）、Chat Timeout 有預設值，都不算。
# 標籤與 Connection 頁相同（mcp-admin-core tests/test_backend_status.py 比對）。
LAUNCH_POLICY = LaunchPolicy(
    command="opendesign-mcp-server",
    args=("--host", "127.0.0.1", "--port", "{port}"),
    env_prefix="",
    connection_keys=frozenset({"od_api_base", "od_basic_user", "od_basic_password", "od_chat_timeout"}),
    backend_connection=(("od_api_base", "Open Design Daemon URL"),),
)

app = create_app(
    title="Open Design MCP Admin",
    extra_routers=[
        config.router,
        create_tools_router(registry=tool_registry, mcp_label="opendesign-mcp"),
        make_health_router(app_type="opendesign",
                           probe_target=health_probes.probe_target,
                           fetch_info=health_probes.fetch_info),
        create_logs_router("opendesign_mcp_admin"),
    ],
    launch_policy=LAUNCH_POLICY,
    product_id="opendesign",
    deployment_kind="podman",
    tool_registry=tool_registry,
)

# #1369：core lifespan 會依 config.json 拉起 MCP 子行程；在那之前先把舊 supergateway seed
# 遷移成原生 launcher（PaaS seed-if-absent，既有服務的 PVC 不會自己更新）。
_core_lifespan = app.router.lifespan_context


@asynccontextmanager
async def _lifespan_with_seed_migration(a):
    await seed_migration.migrate_legacy_seed()
    async with _core_lifespan(a):
        yield


app.router.lifespan_context = _lifespan_with_seed_migration
