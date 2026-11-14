"""FastAPI application entry point for n8n MCP Admin."""

from __future__ import annotations

from mcp_admin_core.app import create_app
from mcp_admin_core.launch_policy import LaunchPolicy
from mcp_admin_core.request_limits import McpRequestLimits
from mcp_admin_core.routers.health import make_health_router
from mcp_admin_core.routers.logs import create_logs_router
from mcp_admin_core.routers.tools import create_tools_router
from mcp_admin_core.session_reclaim import SessionReclaimer

from . import child_env, health_probes, tool_registry
from .routers import config

# 子行程政策（woow_paas_platform#1530／#1531）：command／args 固定（launcher 跑上游 dist/mcp/index.js）；
# connection 只收管理頁宣告的欄位，session 兩欄改由 derived_env 換算成上游變數；mcp_server.env 只注入
# 部署 seed 的 AUTH_TOKEN（與 proxy.bearer_token 同值），管理頁不能改。
# 後端連線欄位（backend_connection）＝API 網址與 API key：上游 n8n-mcp 2.96.2 的 getN8nApiConfig() 兩個都有才開管理工具，
# 缺一個就是文件模式（只有 7 支文件工具、21 支管理工具都不列出，不會報錯）——/healthz 的 backend 是 missing。選填的
# Native MCP Access Token（direct／prepare／pinned 才用）與 session 兩欄不算。標籤與 Connection 頁相同（tests/test_backend_status.py）。
LAUNCH_POLICY = LaunchPolicy(
    command="n8n-mcp-http",
    args=("--port", "{port}"),
    env_prefix="",
    connection_keys=frozenset({"n8n_api_url", "n8n_api_key", "n8n_mcp_access_token", "clear_n8n_mcp_access_token", "mcp_session_timeout", "mcp_max_sessions"}),
    non_env_connection_keys=frozenset({"clear_n8n_mcp_access_token", "mcp_session_timeout", "mcp_max_sessions"}),
    env_keys=frozenset({"AUTH_TOKEN"}),
    editable_env_keys=frozenset(),
    derived_env=child_env.derived_env,
    url_connection_keys=config.URL_FIELDS,
    backend_connection=(("n8n_api_url", "n8n API URL"), ("n8n_api_key", "n8n API Key")),
    # Production n8n children can consume ~228 MiB RSS each. Allow 300 MiB
    # per child for catalog pages and runtime peaks: a 1 GiB pod fits two.
    max_extra_profiles=2,
    profile_child_memory_mib=300,
)

# 上游 session 有名額上限（Max Sessions）；用戶端重連時常不送 DELETE。本版自動回收維持關閉
# （proxy._AUTOMATIC_RECLAIM_EXPERIMENTAL=False）：名額滿時閘道原樣傳回上游 429，直到舊 session 逾時
# （Session Timeout 加上上游最多 5 分鐘的清掃週期）；維運可調高 Max Sessions、縮短 Session Timeout 或重啟子行程。
# 這個物件只保留 session 紀錄與測試用，正式路徑不會用它送 DELETE。
SESSION_RECLAIMER = SessionReclaimer(
    is_session_limit=child_env.is_session_limit_reply,
    session_lifetime=child_env.upstream_session_lifetime_seconds,
    label="n8n",
)

app = create_app(
    title="n8n MCP Admin",
    extra_routers=[
        config.router,
        create_tools_router(registry=tool_registry, mcp_label="n8n-mcp", operations_supported=True),
        make_health_router(app_type="n8n",
                           probe_target=health_probes.probe_target,
                           fetch_info=health_probes.fetch_info),
        create_logs_router("n8n_mcp_admin"),
    ],
    launch_policy=LAUNCH_POLICY,
    product_id="n8n",
    deployment_kind="podman",
    tool_registry=tool_registry,
    operations_supported=True,
    mcp_request_limits=McpRequestLimits(max_bytes=4 * 1024 * 1024, max_argument_elements=10_000),
    session_reclaimer=SESSION_RECLAIMER,
)
