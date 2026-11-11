"""FastAPI application entry point for Hermes MCP Admin."""

from __future__ import annotations

from mcp_admin_core.app import create_app
from mcp_admin_core.launch_policy import LaunchPolicy
from mcp_admin_core.routers.logs import create_logs_router
from mcp_admin_core.routers.tools import create_tools_router

from . import dashboard_auth, tool_registry
from .routers import config, dashboard_proxy, health

# 子行程政策（woow_paas_platform#1530）：command／args 固定、connection 只注入五個連線欄位（HERMES_ 前綴）。
# 後端連線欄位（backend_connection）＝五個都是：沒有連線子行程照樣起來、列出 11 支工具，但 10 支要 Dashboard（網址＋帳密登入）、
# hermes_chat 與 hermes_inspect 要 Gateway（網址＋API Key；PaaS 的 Hermes Agent 兩邊都要認證），缺哪個就有工具回
# 「… is not configured」。/healthz 的 backend 只要有一欄沒填就是 missing。標籤與 Connection 頁相同（tests/test_backend_status.py）。
LAUNCH_POLICY = LaunchPolicy(
    command="hermes-mcp-server",
    args=("--host", "127.0.0.1", "--port", "{port}"),
    env_prefix="HERMES_",
    connection_keys=frozenset(
        {"gateway_url", "gateway_api_key", "dashboard_url", "dashboard_username", "dashboard_password"}
    ),
    backend_connection=(
        ("gateway_url", "Gateway URL"),
        ("gateway_api_key", "API Key"),
        ("dashboard_url", "Dashboard URL"),
        ("dashboard_username", "Username"),
        ("dashboard_password", "Password"),
    ),
)

app = create_app(
    title="Hermes MCP Admin",
    extra_routers=[
        config.router,
        create_tools_router(registry=tool_registry, mcp_label="hermes-mcp"),
        health.router,
        create_logs_router("hermes_mcp_admin"),
        dashboard_proxy.router,
    ],
    launch_policy=LAUNCH_POLICY,
    product_id="hermes",
    deployment_kind="podman",
    tool_registry=tool_registry,
)
app.state.profile_deleted = dashboard_auth.remove_profile
