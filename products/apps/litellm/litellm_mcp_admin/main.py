"""LiteLLM MCP Admin —— FastAPI 應用進入點。

包 vendored ``woow_litellm_mcp_server``（FastMCP，40 支工具＋三層 gating，來源
WOOWTECH/Woow_litellm_mcp_server）。與其他 app 同分層：共用 mcp_admin_core 的 app factory
＋hoisted routers（tools／health／logs），只保留 app-specific 的 connection config、
tool registry adapter、health probes。

啟動順序：先補預設（``seed_defaults``：新安裝全開、env_prefix），再進 core lifespan
依 config.json 拉起 MCP 子行程（``litellm-mcp-server`` launcher）。

Run with:
    uvicorn litellm_mcp_admin.main:app --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from mcp_admin_core.app import create_app
from mcp_admin_core.launch_policy import LaunchPolicy
from mcp_admin_core.routers.health import make_health_router
from mcp_admin_core.routers.logs import create_logs_router
from mcp_admin_core.routers.tools import create_tools_router

from . import health_probes, log_redaction, seed_defaults, tool_registry
from .routers import config

# 子行程政策（woow_paas_platform#1530）：command／args 固定；connection（base_url／master_key）以
# LITELLM_MCP_ 前綴注入；mcp_server.env 只有 Settings 頁的唯讀開關。全開（負責人 2026-10-25「預設全開」）＝
# 唯讀關：本行程建立的 config.json 直接帶這個值，既有設定第一次以本映像啟動時由 core 的 owner_defaults 改成這個值。
# 後端連線欄位（backend_connection）：沒有連線子行程照樣起來、列出 40 支工具，但沒有網址時連 http://localhost:4000（容器裡
# 沒有 LiteLLM）、沒有金鑰時不帶 Authorization（LiteLLM 管理 API 回 401），工具呼叫都會失敗。平台會替同工作區的 LiteLLM
# 預填網址、金鑰一律要租戶自己貼，所以新開的服務常是「有網址、沒金鑰」——一樣算 missing。標籤與 Connection 頁相同
# （tests/test_main.py 比對 appConfig.jsx）。
LAUNCH_POLICY = LaunchPolicy(
    command="litellm-mcp-server",
    args=("--port", "{port}"),
    env_prefix="LITELLM_MCP_",
    connection_keys=frozenset({"base_url", "master_key"}),
    env_keys=frozenset({"LITELLM_MCP_READONLY"}),
    editable_env_keys=frozenset({"LITELLM_MCP_READONLY"}),
    fresh_env={seed_defaults.READONLY_ENV: seed_defaults.FRESH_READONLY},
    backend_connection=(("base_url", "LiteLLM 網址"), ("master_key", "LiteLLM 金鑰")),
)

app = create_app(
    title="LiteLLM MCP Admin",
    extra_routers=[
        config.router,
        create_tools_router(registry=tool_registry, mcp_label="litellm-mcp"),
        make_health_router(app_type="litellm",
                           probe_target=health_probes.probe_target,
                           fetch_info=health_probes.fetch_info),
        create_logs_router("litellm_mcp_admin"),
    ],
    launch_policy=LAUNCH_POLICY,
    product_id="litellm",
    deployment_kind="podman",
    tool_registry=tool_registry,
)

# path token 不進 log（uvicorn access log 會印 /private_{token}/mcp；FINDING-006 同類）。
log_redaction.install()

_core_lifespan = app.router.lifespan_context


@asynccontextmanager
async def _lifespan_with_seed_defaults(a):
    await seed_defaults.apply_seed_defaults()
    async with _core_lifespan(a):
        yield


app.router.lifespan_context = _lifespan_with_seed_defaults
