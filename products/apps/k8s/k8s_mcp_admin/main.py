"""K8s MCP Admin - FastAPI application entry point.

打包上游 containers/kubernetes-mcp-server（Go binary、原生 Streamable HTTP）。與其他
app 同分層：共用 mcp_admin_core 的 app factory + hoisted routers（tools / health / logs），
只保留 app-specific 的 connection config、tool registry、health probes。

Run with:
    uvicorn k8s_mcp_admin.main:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

from mcp_admin_core.app import create_app
from mcp_admin_core.launch_policy import LaunchPolicy
from mcp_admin_core.routers.health import make_health_router
from mcp_admin_core.routers.logs import create_logs_router
from mcp_admin_core.routers.tools import create_tools_router

from . import health_probes, tool_registry
from .launcher import FRESH_ENV
from .routers import config

# 子行程政策（woow_paas_platform#1530）：command／args 固定；connection（kubeconfig／in_cluster）以
# K8S_MCP_ 前綴注入；mcp_server.env 只收 Settings 頁的治理開關（唯讀、擋破壞性、Secret、多叢集）與部署 seed 的
# toolsets。全開（負責人 2026-10-25「預設全開」）：本行程建立的 config.json 直接帶 FRESH_ENV；既有 config.json 第一次
# 以本映像啟動時由 core 的 owner_defaults 一次性改成同一組值（PaaS 範本 seed 也明寫同一組值）。
# 不宣告 backend_connection（也不宣告 required_connection）：kubeconfig 不是「子行程起得來、只是沒接上後端」的欄位——
# 沒有 kubeconfig 又不在叢集內時，依上游 v0.0.65 原始碼會退回 kubeconfig provider、找不到任何 context 就啟動失敗
# （子行程結束，/healthz 的 mcp 是 not_running，Logs 頁有 launcher 印的「尚未設定 kubeconfig」）；而且這是
# 「kubeconfig 或 in-cluster」的條件式需求，(鍵, 標籤) 全部要填的宣告表達不了（in_cluster=true 的自架部署會被誤報）。
_GOVERNANCE_ENV_KEYS = frozenset({
    "K8S_MCP_READ_ONLY", "K8S_MCP_DISABLE_DESTRUCTIVE", "K8S_MCP_ALLOW_SECRETS", "K8S_MCP_TOOLSETS",
    "K8S_MCP_MULTI_CLUSTER",
})
LAUNCH_POLICY = LaunchPolicy(
    command="k8s-mcp-http",
    args=("--port", "{port}"),
    env_prefix="K8S_MCP_",
    connection_keys=frozenset({"kubeconfig", "in_cluster"}),
    env_keys=_GOVERNANCE_ENV_KEYS,
    editable_env_keys=_GOVERNANCE_ENV_KEYS,
    fresh_env=FRESH_ENV,
    # 一次性遷移時 toolsets 取舊值與全開值的聯集：管理員自己加的 kiali、netobserv 等保留（見 owner_defaults）。
    fresh_env_union_keys=frozenset({"K8S_MCP_TOOLSETS"}),
)

app = create_app(
    title="K8s MCP Admin",
    extra_routers=[
        config.router,
        create_tools_router(registry=tool_registry, mcp_label="k8s-mcp"),
        make_health_router(app_type="k8s",
                           probe_target=health_probes.probe_target,
                           fetch_info=health_probes.fetch_info),
        create_logs_router("k8s_mcp_admin"),
    ],
    launch_policy=LAUNCH_POLICY,
    product_id="k8s",
    deployment_kind="podman",
    tool_registry=tool_registry,
)
