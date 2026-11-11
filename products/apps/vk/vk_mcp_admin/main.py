"""VK MCP Admin —— FastAPI 應用進入點。

包 supergateway + vibe-kanban-mcp（Rust binary、34 工具）。與 apps/odoo-manage 同分層：
共用 mcp_admin_core 的 app factory + hoisted routers（tools / health / logs），只保留
app-specific 的 connection config、tool registry、health probes。

Run with:
    uvicorn vk_mcp_admin.main:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

from mcp_admin_core.app import create_app
from mcp_admin_core.launch_policy import LaunchPolicy
from mcp_admin_core.routers.health import make_health_router
from mcp_admin_core.routers.logs import create_logs_router
from mcp_admin_core.routers.tools import create_tools_router

from . import health_probes, tool_registry
from .routers import config

# 子行程政策（woow_paas_platform#1530）：supergateway 的 --stdio 參數會被當成指令執行，必須固定；
# connection 只注入 vibe_backend_url（VIBE_BACKEND_URL）。
# 後端連線欄位（backend_connection）：VK Host 一律在容器外，沒有 VIBE_BACKEND_URL 時 vibe-kanban-mcp（v0.1.44）改找本機的
# port 檔，容器裡沒有就結束；我們管的子行程是 supergateway，它照樣在跑（/healthz 的 mcp 是 running），每個 MCP 請求卻都失敗。
# 沒填就是 backend: missing。標籤與 Connection 頁相同（mcp-admin-core tests/test_backend_status.py 比對）。
LAUNCH_POLICY = LaunchPolicy(
    command="supergateway",
    args=(
        "--stdio", "vibe-kanban-mcp --mode global",
        "--port", "{port}",
        "--outputTransport", "streamableHttp",
        "--healthEndpoint", "/healthz",
    ),
    env_prefix="",
    connection_keys=frozenset({"vibe_backend_url"}),
    backend_connection=(("vibe_backend_url", "VK Host API URL"),),
)

app = create_app(
    title="VK MCP Admin",
    extra_routers=[
        config.router,
        create_tools_router(registry=tool_registry, mcp_label="vibe-kanban-mcp"),
        make_health_router(app_type="vk",
                           probe_target=health_probes.probe_target,
                           fetch_info=health_probes.fetch_info),
        create_logs_router("vk_mcp_admin"),
    ],
    launch_policy=LAUNCH_POLICY,
    product_id="vk",
    deployment_kind="podman",
    tool_registry=tool_registry,
)
