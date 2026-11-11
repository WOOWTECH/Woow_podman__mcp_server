"""containers/kubernetes-mcp-server 35 支工具的靜態 registry（錨定 v0.0.65）。

比照 apps/odoo-manage/tool_registry.py 對齊共用 core 的 create_tools_router 介面：
提供 ToolCategory / TOOL_REGISTRY / get_all_tool_names / get_tool_by_name，並補上
mode-aware 對帳所需的欄位與推導函式。

三組正交語意（切勿混為一談）：
- ``dangerous``：app 自有的 UI 紅標語意（8 支寫操作），供 Tools 頁紅標與 per-tool toggle。
- ``read_only_hint`` / ``destructive_hint``：**照上游 v0.0.65 原始碼 annotations 抄錄**，
  對映上游 `--read-only` / `--disable-destructive` 的實際 tools/list 過濾行為
  （mode-aware 對帳 smoke 依此值運算，不看 dangerous）。
- ``toolset``：工具所屬的上游工具集。新安裝開 config／core／helm／kcp／kubevirt／tekton 六個（不需另外設定
  就能用的全部工具集，負責人 2026-10-25「預設全開」）；kiali（要 Kiali 網址）與 netobserv（預設網址只對
  MCP 所在叢集有效）不開，所以不在 registry。既有設定若仍是 core,config,helm，子行程只曝露那三集的 23 支。
- 錨定版本 v0.0.65；**升版須重抄** `pkg/toolsets/{core,config,helm,kcp,kubevirt,tekton}/` 的 annotations
  （以 tools/list 實測對照），尤其 pods_run / helm_install 這類 dangerous 但 destructiveHint=false 的例外可能隨上游調整。

===========================================================================
23 支 annotations 抄錄表（附錄 E，錨定 v0.0.65；tag 56d6dfe）
---------------------------------------------------------------------------
 #  name                        toolset  category      ReadOnly  Destructive  dangerous
 1  pods_list                   core     CORE_READ     true      false
 2  pods_list_in_namespace      core     CORE_READ     true      false
 3  pods_get                    core     CORE_READ     true      false
 4  pods_log                    core     CORE_READ     true      false
 5  pods_top                    core     CORE_READ     true      false
 6  namespaces_list             core     CORE_READ     true      false
 7  events_list                 core     CORE_READ     true      false
 8  nodes_log                   core     CORE_READ     true      false
 9  nodes_top                   core     CORE_READ     true      false
10  nodes_stats_summary         core     CORE_READ     true      false
11  resources_list              core     CORE_READ     true      false
12  resources_get               core     CORE_READ     true      false
13  projects_list               core     CORE_READ     true      false
14  pods_exec                   core     CORE_WRITE    false     true         ✓
15  pods_run                    core     CORE_WRITE    false     false ⚠️     ✓
16  pods_delete                 core     CORE_WRITE    false     true         ✓
17  resources_create_or_update  core     CORE_WRITE    false     true         ✓
18  resources_delete            core     CORE_WRITE    false     true         ✓
19  resources_scale             core     CORE_WRITE    false     true         ✓
20  configuration_view          config   CONFIG        true      false
21  helm_list                   helm     HELM          true      false
22  helm_install                helm     HELM          false     false ⚠️     ✓
23  helm_uninstall              helm     HELM          false     true         ✓
---------------------------------------------------------------------------
彙總：read_only_hint=true = 15 支（#1–13、20、21）；destructive_hint=true = 6 支
（#14、16、17、18、19、23）。⚠️ pods_run（pods.go:255）與 helm_install（helm.go:43）
雖屬 dangerous 紅標 8 支，但上游 DestructiveHint=false，故 `--disable-destructive`
**不會**隱藏這兩支——dangerous（UI 紅標）與 hint（上游 enforcement）刻意解耦。
===========================================================================
免設定工具集的 11 支（2026-10-25 以 v0.0.65 binary tools/list 實測抄錄）
---------------------------------------------------------------------------
 #  name                        toolset   category  ReadOnly  Destructive  dangerous
24  kcp_workspaces_list         kcp       KCP       true      false
25  kcp_workspace_describe      kcp       KCP       true      false
26  vm_clone                    kubevirt  KUBEVIRT  false     true         ✓
27  vm_create                   kubevirt  KUBEVIRT  false     true         ✓
28  vm_guest_info               kubevirt  KUBEVIRT  true      false
29  vm_lifecycle                kubevirt  KUBEVIRT  false     true         ✓
30  tekton_pipeline_start       tekton    TEKTON    false     false ⚠️     ✓
31  tekton_pipelinerun_restart  tekton    TEKTON    false     false ⚠️     ✓
32  tekton_task_start           tekton    TEKTON    false     false ⚠️     ✓
33  tekton_taskrun_logs         tekton    TEKTON    true      false
34  tekton_taskrun_restart      tekton    TEKTON    false     false ⚠️     ✓
---------------------------------------------------------------------------
34 支合計：read_only_hint=true 19 支；destructive_hint=true 9 支；dangerous 15 支。
tekton 的 start／restart 只新建 run（上游 DestructiveHint=false），`--disable-destructive` 不會隱藏。
---------------------------------------------------------------------------
多叢集（K8S_MCP_MULTI_CLUSTER 開、不帶 --disable-multi-cluster）且 kubeconfig 有多個 context 時再多 1 支：
35  configuration_contexts_list config    CONFIG    true      false
（上游 tool_filter.go：kubeconfig provider 只出 configuration_contexts_list，不出 targets_list；只有一個
context 時兩支都不出。其他 31 支此時多一個 ``context`` 參數。）
===========================================================================
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel


class ToolCategory(str, Enum):
    """K8s MCP 工具的 UI 分組分類。"""

    CORE_READ = "Core (Read)"
    CORE_WRITE = "Core (Write)"
    CONFIG = "Config"
    HELM = "Helm"
    KCP = "kcp"
    KUBEVIRT = "KubeVirt"
    TEKTON = "Tekton"


class ToolDefinition(BaseModel):
    """單一 MCP 工具定義。

    read_only_hint / destructive_hint 為 app-local 擴充欄位（core tools router 不讀、
    不受影響），供 mode-aware 對帳；operations 對齊正典 registry 介面（k8s 工具無
    per-op 概念，預設空 list）。
    """

    name: str
    description: str
    category: ToolCategory
    enabled_by_default: bool = True
    dangerous: bool = False
    read_only_hint: bool = False
    destructive_hint: bool = False
    operations: list[str] = []
    # 上游工具集（K8S_MCP_TOOLSETS 沒開的工具集，子行程不會曝露它的工具）
    toolset: str = "core"
    # 只在多叢集模式＋kubeconfig 有多個 context 時才曝露（configuration_contexts_list）
    multi_context_only: bool = False


class ToolState(BaseModel):
    """工具的執行期狀態（定義 + 當前 enabled）。"""

    name: str
    description: str
    category: ToolCategory
    enabled_by_default: bool
    dangerous: bool
    enabled: bool


class ToolUpdateRequest(BaseModel):
    """更新工具 enabled 狀態的請求 body。

    支援兩種格式：
    - dict：``{"tools": {"tool_name": true/false, ...}}``
    - list：``{"tools": [{"name": "...", "enabled": true/false, ...}, ...]}``
    """

    tools: Any  # dict[str, bool] or list[dict] from frontend


class ToolUpdateResponse(BaseModel):
    """更新工具狀態後的回應。"""

    updated: int
    tools: list[ToolState]


# ---------------------------------------------------------------------------
# 35 支工具正典 registry（照 §4.3 表 + 附錄 E annotations，加上免設定工具集的 11 支與多叢集的 1 支）
# ---------------------------------------------------------------------------

TOOL_REGISTRY: list[ToolDefinition] = [
    # ── Core (Read) 13 支 ─────────────────────────────────────────────────
    ToolDefinition(
        name="pods_list",
        description="List pods across all namespaces",
        category=ToolCategory.CORE_READ,
        read_only_hint=True,
    ),
    ToolDefinition(
        name="pods_list_in_namespace",
        description="List pods in a specific namespace",
        category=ToolCategory.CORE_READ,
        read_only_hint=True,
    ),
    ToolDefinition(
        name="pods_get",
        description="Get detailed information about a specific pod",
        category=ToolCategory.CORE_READ,
        read_only_hint=True,
    ),
    ToolDefinition(
        name="pods_log",
        description="Retrieve logs from a pod",
        category=ToolCategory.CORE_READ,
        read_only_hint=True,
    ),
    ToolDefinition(
        name="pods_top",
        description="Show resource usage (CPU/memory) for pods",
        category=ToolCategory.CORE_READ,
        read_only_hint=True,
    ),
    ToolDefinition(
        name="namespaces_list",
        description="List all namespaces in the cluster",
        category=ToolCategory.CORE_READ,
        read_only_hint=True,
    ),
    ToolDefinition(
        name="events_list",
        description="List cluster events",
        category=ToolCategory.CORE_READ,
        read_only_hint=True,
    ),
    ToolDefinition(
        name="nodes_log",
        description="Retrieve logs from a node",
        category=ToolCategory.CORE_READ,
        read_only_hint=True,
    ),
    ToolDefinition(
        name="nodes_top",
        description="Show resource usage for nodes",
        category=ToolCategory.CORE_READ,
        read_only_hint=True,
    ),
    ToolDefinition(
        name="nodes_stats_summary",
        description="Get node stats summary (CPU, memory, disk, network)",
        category=ToolCategory.CORE_READ,
        read_only_hint=True,
    ),
    ToolDefinition(
        name="resources_list",
        description="List Kubernetes resources of a given kind",
        category=ToolCategory.CORE_READ,
        read_only_hint=True,
    ),
    ToolDefinition(
        name="resources_get",
        description="Get a specific Kubernetes resource by name",
        category=ToolCategory.CORE_READ,
        read_only_hint=True,
    ),
    ToolDefinition(
        # 修正：上游屬 core toolset（來源 repo 誤寫成 Helm repositories）。
        # 於一般 K8s/K3s 叢集上通常為空或報錯（OpenShift 專屬概念）。
        name="projects_list",
        description=(
            "List all the OpenShift projects "
            "(通常僅 OpenShift 叢集有值；一般 K8s/K3s 上多為空/報錯)"
        ),
        category=ToolCategory.CORE_READ,
        read_only_hint=True,
    ),
    # ── Core (Write) 6 支 ─────────────────────────────────────────────────
    ToolDefinition(
        name="pods_exec",
        description="Execute a command inside a running pod",
        category=ToolCategory.CORE_WRITE,
        dangerous=True,
        destructive_hint=True,
    ),
    ToolDefinition(
        # ⚠️ dangerous 紅標但上游 destructiveHint=false（Run 只新建 pod、不摧毀既有資源）。
        name="pods_run",
        description="Run a new pod with a specified image",
        category=ToolCategory.CORE_WRITE,
        dangerous=True,
        destructive_hint=False,
    ),
    ToolDefinition(
        name="pods_delete",
        description="Delete a pod",
        category=ToolCategory.CORE_WRITE,
        dangerous=True,
        destructive_hint=True,
    ),
    ToolDefinition(
        name="resources_create_or_update",
        description="Create or update a Kubernetes resource from YAML/JSON",
        category=ToolCategory.CORE_WRITE,
        dangerous=True,
        destructive_hint=True,
    ),
    ToolDefinition(
        name="resources_delete",
        description="Delete a Kubernetes resource",
        category=ToolCategory.CORE_WRITE,
        dangerous=True,
        destructive_hint=True,
    ),
    ToolDefinition(
        name="resources_scale",
        description="Scale a deployment or statefulset replica count",
        category=ToolCategory.CORE_WRITE,
        dangerous=True,
        destructive_hint=True,
    ),
    # ── Config 1 支 ───────────────────────────────────────────────────────
    ToolDefinition(
        # 只 dump kubeconfig、不出網 → Test Connection 不可用它（見 Task 14）。
        name="configuration_view",
        description="View the MCP server configuration",
        category=ToolCategory.CONFIG,
        read_only_hint=True,
        toolset="config",
    ),
    ToolDefinition(
        # 多叢集模式且 kubeconfig 有多個 context 才出現（上游 TargetListProvider）。
        name="configuration_contexts_list",
        description="List the contexts available in the kubeconfig (multi-cluster mode)",
        category=ToolCategory.CONFIG,
        read_only_hint=True,
        toolset="config",
        multi_context_only=True,
    ),
    # ── Helm 3 支 ─────────────────────────────────────────────────────────
    ToolDefinition(
        name="helm_list",
        description="List Helm releases in the cluster",
        category=ToolCategory.HELM,
        read_only_hint=True,
        toolset="helm",
    ),
    ToolDefinition(
        # ⚠️ dangerous 紅標但上游 destructiveHint=false（install 帶 upgrade 語意、非破壞）。
        name="helm_install",
        description="Install or upgrade a Helm chart",
        category=ToolCategory.HELM,
        dangerous=True,
        destructive_hint=False,
        toolset="helm",
    ),
    ToolDefinition(
        name="helm_uninstall",
        description="Uninstall a Helm release",
        category=ToolCategory.HELM,
        dangerous=True,
        destructive_hint=True,
        toolset="helm",
    ),
    # ── kcp 2 支（只對 kcp 伺服器有意義；一般叢集呼叫會回錯誤）───────────────
    ToolDefinition(
        name="kcp_workspaces_list",
        description="List all available kcp workspaces in the current cluster",
        category=ToolCategory.KCP,
        read_only_hint=True,
        toolset="kcp",
    ),
    ToolDefinition(
        name="kcp_workspace_describe",
        description="Get detailed information about a specific kcp workspace",
        category=ToolCategory.KCP,
        read_only_hint=True,
        toolset="kcp",
    ),
    # ── KubeVirt 4 支（叢集要裝 KubeVirt）────────────────────────────────
    ToolDefinition(
        name="vm_clone",
        description="Clone a KubeVirt VirtualMachine (creates a VirtualMachineClone)",
        category=ToolCategory.KUBEVIRT,
        dangerous=True,
        destructive_hint=True,
        toolset="kubevirt",
    ),
    ToolDefinition(
        name="vm_create",
        description="Create a KubeVirt VirtualMachine (instance type, preference and disk image resolved automatically)",
        category=ToolCategory.KUBEVIRT,
        dangerous=True,
        destructive_hint=True,
        toolset="kubevirt",
    ),
    ToolDefinition(
        name="vm_guest_info",
        description="Get guest OS information from a VirtualMachine's QEMU guest agent",
        category=ToolCategory.KUBEVIRT,
        read_only_hint=True,
        toolset="kubevirt",
    ),
    ToolDefinition(
        name="vm_lifecycle",
        description="Start, stop or restart a KubeVirt VirtualMachine",
        category=ToolCategory.KUBEVIRT,
        dangerous=True,
        destructive_hint=True,
        toolset="kubevirt",
    ),
    # ── Tekton 5 支（叢集要裝 Tekton Pipelines）────────────────────────────
    ToolDefinition(
        # ⚠️ dangerous 紅標但上游 destructiveHint=false（只新建 PipelineRun）。
        name="tekton_pipeline_start",
        description="Start a Tekton Pipeline (creates a PipelineRun)",
        category=ToolCategory.TEKTON,
        dangerous=True,
        toolset="tekton",
    ),
    ToolDefinition(
        name="tekton_pipelinerun_restart",
        description="Restart a Tekton PipelineRun (creates a new PipelineRun with the same spec)",
        category=ToolCategory.TEKTON,
        dangerous=True,
        toolset="tekton",
    ),
    ToolDefinition(
        name="tekton_task_start",
        description="Start a Tekton Task (creates a TaskRun)",
        category=ToolCategory.TEKTON,
        dangerous=True,
        toolset="tekton",
    ),
    ToolDefinition(
        name="tekton_taskrun_logs",
        description="Get the logs of a Tekton TaskRun",
        category=ToolCategory.TEKTON,
        read_only_hint=True,
        toolset="tekton",
    ),
    ToolDefinition(
        name="tekton_taskrun_restart",
        description="Restart a Tekton TaskRun (creates a new TaskRun with the same spec)",
        category=ToolCategory.TEKTON,
        dangerous=True,
        toolset="tekton",
    ),
]

# registry 涵蓋的上游工具集（＝新安裝開啟的工具集，見 launcher.FRESH_TOOLSETS）
TOOLSETS: tuple[str, ...] = ("config", "core", "helm", "kcp", "kubevirt", "tekton")

# 依名稱快取查詢
TOOL_BY_NAME: dict[str, ToolDefinition] = {t.name: t for t in TOOL_REGISTRY}

# 依分類快取
TOOLS_BY_CATEGORY: dict[ToolCategory, list[ToolDefinition]] = {}
for _tool in TOOL_REGISTRY:
    TOOLS_BY_CATEGORY.setdefault(_tool.category, []).append(_tool)


def get_tool_states(enabled_overrides: dict[str, bool] | None = None) -> list[ToolState]:
    """建構完整工具狀態清單，套用 enabled 覆寫。"""
    overrides = enabled_overrides or {}
    return [
        ToolState(
            name=t.name,
            description=t.description,
            category=t.category,
            enabled_by_default=t.enabled_by_default,
            dangerous=t.dangerous,
            enabled=overrides.get(t.name, t.enabled_by_default),
        )
        for t in TOOL_REGISTRY
    ]


def get_category_summary() -> dict[str, dict[str, Any]]:
    """回傳每個分類的工具摘要 {count, tools}。"""
    return {
        cat.value: {
            "count": len(tools),
            "tools": [t.name for t in tools],
        }
        for cat, tools in TOOLS_BY_CATEGORY.items()
    }


def get_all_tool_names() -> list[str]:
    """回傳所有工具名的扁平清單。"""
    return [t.name for t in TOOL_REGISTRY]


def get_tool_by_name(name: str) -> ToolDefinition | None:
    """依名稱查詢工具。"""
    return TOOL_BY_NAME.get(name)


def expected_exposed_tools(
    read_only: bool,
    disable_destructive: bool,
    disabled: set[str] | None = None,
    toolsets: list[str] | tuple[str, ...] | set[str] | None = None,
    multiple_contexts: bool = False,
) -> set[str]:
    """推導在給定治理模式下、上游 tools/list 應曝露的工具名集合。

    對映上游 v0.0.65 行為（附錄 E 原始碼結論，2026-10-25 以 binary 實測六個工具集與多叢集）：
    - toolsets：只留這些工具集的工具；None＝registry 的全部工具集（＝新安裝）。
    - multiple_contexts：多叢集模式開著、而且 kubeconfig 有多個 context（才會多出 configuration_contexts_list）。
    - read_only=True：只留 readOnlyHint=true 的工具（其餘全隱藏）。
    - disable_destructive=True：剔除 destructiveHint=true 的工具。
    - disabled：per-tool 停用（config.json tools.disabled → TOML disabled_tools）。

    **一律只依 read_only_hint / destructive_hint 值運算**（不看 dangerous），
    以忠實對映上游 enforcement；供 live tools/list 對帳 smoke 與測試共用。
    """
    enabled_sets = set(TOOLSETS if toolsets is None else toolsets)
    tools = [t for t in TOOL_REGISTRY
             if t.toolset in enabled_sets and (multiple_contexts or not t.multi_context_only)]
    if read_only:
        exposed = {t.name for t in tools if t.read_only_hint}
    else:
        exposed = {t.name for t in tools}
    if disable_destructive:
        exposed -= {t.name for t in tools if t.destructive_hint}
    if disabled:
        exposed -= set(disabled)
    return exposed
