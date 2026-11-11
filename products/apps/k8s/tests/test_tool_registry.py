"""tool_registry 契約測試（錨定上游 v0.0.65／附錄 E）。

鎖住三組獨立事實：
1. 形狀與固定數字（35 支＝六個免設定工具集 34 支＋多叢集的 configuration_contexts_list、7 類、operations 全空；
   core,config,helm 仍是 23 支）。
2. dangerous 集合（app 自有的 UI 紅標語意）與 destructive_hint 集合
   （上游 --disable-destructive enforcement 語意）**獨立斷言、不相等**。
3. expected_exposed_tools() mode-aware 推導（toolsets / read_only / disable_destructive / disabled），
   數字與 2026-10-25 對 v0.0.65 binary 實際 tools/list 的結果一致（六個工具集：全開 34、唯讀 19、擋破壞性 25）。
"""

from __future__ import annotations

from k8s_mcp_admin import tool_registry as reg


# §4.3 / 附錄 E 固定集合
DANGEROUS_8 = {
    "pods_exec",
    "pods_run",
    "pods_delete",
    "resources_create_or_update",
    "resources_delete",
    "resources_scale",
    "helm_install",
    "helm_uninstall",
}

# 附錄 E 實測：上游 destructiveHint=true 僅 6 支（pods_run / helm_install 標 false）
DESTRUCTIVE_HINT_6 = {
    "pods_exec",
    "pods_delete",
    "resources_create_or_update",
    "resources_delete",
    "resources_scale",
    "helm_uninstall",
}

# 免設定工具集（kcp／kubevirt／tekton）的 11 支，2026-10-25 以 v0.0.65 binary tools/list 實測
NEW_TOOLSETS = {
    "kcp": {"kcp_workspaces_list", "kcp_workspace_describe"},
    "kubevirt": {"vm_clone", "vm_create", "vm_guest_info", "vm_lifecycle"},
    "tekton": {"tekton_pipeline_start", "tekton_pipelinerun_restart", "tekton_task_start",
               "tekton_taskrun_logs", "tekton_taskrun_restart"},
}
NEW_READ_ONLY = {"kcp_workspaces_list", "kcp_workspace_describe", "vm_guest_info", "tekton_taskrun_logs"}
# 多叢集模式＋kubeconfig 有多個 context 才出現（2026-10-25 以 v0.0.65 binary 實測）
MULTI_CONTEXT = {"configuration_contexts_list"}
NEW_DESTRUCTIVE = {"vm_clone", "vm_create", "vm_lifecycle"}
NEW_DANGEROUS = {"vm_clone", "vm_create", "vm_lifecycle", "tekton_pipeline_start", "tekton_pipelinerun_restart",
                 "tekton_task_start", "tekton_taskrun_restart"}

# 附錄 E：readOnlyHint=true 15 支（#1–13、20、21）
READ_ONLY_HINT_15 = {
    "pods_list",
    "pods_list_in_namespace",
    "pods_get",
    "pods_log",
    "pods_top",
    "namespaces_list",
    "events_list",
    "nodes_log",
    "nodes_top",
    "nodes_stats_summary",
    "resources_list",
    "resources_get",
    "projects_list",
    "configuration_view",
    "helm_list",
}


def test_registry_has_35_tools():
    names = [t.name for t in reg.TOOL_REGISTRY]
    assert len(reg.TOOL_REGISTRY) == 35
    assert len(names) == len(set(names)), "工具名不得重複"
    assert {t.name for t in reg.TOOL_REGISTRY if t.multi_context_only} == MULTI_CONTEXT


def test_toolsets_match_fresh_install():
    from k8s_mcp_admin.launcher import FRESH_ENV, FRESH_TOOLSETS

    assert set(reg.TOOLSETS) == set(FRESH_TOOLSETS) == {"config", "core", "helm", "kcp", "kubevirt", "tekton"}
    assert FRESH_ENV["K8S_MCP_TOOLSETS"].split(",") == FRESH_TOOLSETS
    assert FRESH_ENV["K8S_MCP_MULTI_CLUSTER"] == "1"
    assert {t.toolset for t in reg.TOOL_REGISTRY} == set(reg.TOOLSETS)
    legacy = {t.name for t in reg.TOOL_REGISTRY
              if t.toolset in ("core", "config", "helm") and not t.multi_context_only}
    assert len(legacy) == 23
    for toolset, names in NEW_TOOLSETS.items():
        assert {t.name for t in reg.TOOL_REGISTRY if t.toolset == toolset} == names


def test_dangerous_set():
    dangerous = {t.name for t in reg.TOOL_REGISTRY if t.dangerous}
    assert dangerous == DANGEROUS_8 | NEW_DANGEROUS
    assert len(dangerous) == 15


def test_destructive_hint_set():
    # 上游 enforcement 語意，與 dangerous 解耦（pods_run / helm_install / tekton start・restart 為 hint=false）
    destructive = {t.name for t in reg.TOOL_REGISTRY if t.destructive_hint}
    assert destructive == DESTRUCTIVE_HINT_6 | NEW_DESTRUCTIVE
    # 明確驗證兩集合「不相等」，防未來誤把 hint 綁死 dangerous
    assert destructive != DANGEROUS_8 | NEW_DANGEROUS


def test_read_only_hint_set():
    read_only = {t.name for t in reg.TOOL_REGISTRY if t.read_only_hint}
    assert read_only == READ_ONLY_HINT_15 | NEW_READ_ONLY | MULTI_CONTEXT
    assert len(read_only) == 20


def test_categories():
    assert issubclass(reg.ToolCategory, str)
    counts: dict[str, int] = {}
    for t in reg.TOOL_REGISTRY:
        counts[t.category.value] = counts.get(t.category.value, 0) + 1
    assert counts[reg.ToolCategory.CORE_READ.value] == 13
    assert counts[reg.ToolCategory.CORE_WRITE.value] == 6
    assert counts[reg.ToolCategory.CONFIG.value] == 2
    assert counts[reg.ToolCategory.HELM.value] == 3
    assert counts[reg.ToolCategory.KCP.value] == 2
    assert counts[reg.ToolCategory.KUBEVIRT.value] == 4
    assert counts[reg.ToolCategory.TEKTON.value] == 5


def test_operations_all_empty():
    assert all(t.operations == [] for t in reg.TOOL_REGISTRY)


def test_lookup_helpers():
    names = reg.get_all_tool_names()
    assert len(names) == 35
    assert reg.get_tool_by_name("pods_exec").dangerous is True
    assert reg.get_tool_by_name("does_not_exist") is None


def test_expected_exposed_tools_mode_aware():
    all_names = set(reg.get_all_tool_names()) - MULTI_CONTEXT

    # 全開（六個工具集、單一 context）→ 34；kubeconfig 有多個 context（多叢集）→ 35
    assert reg.expected_exposed_tools(read_only=False, disable_destructive=False) == all_names
    assert reg.expected_exposed_tools(False, False, multiple_contexts=True) == all_names | MULTI_CONTEXT
    assert reg.expected_exposed_tools(True, False, multiple_contexts=True) == READ_ONLY_HINT_15 | NEW_READ_ONLY | MULTI_CONTEXT

    # read_only → 僅 readOnlyHint=true 的 19 支
    assert reg.expected_exposed_tools(read_only=True, disable_destructive=False) == READ_ONLY_HINT_15 | NEW_READ_ONLY

    # disable_destructive → 全量減 destructiveHint=true 的 9 支 → 25 支
    exposed = reg.expected_exposed_tools(read_only=False, disable_destructive=True)
    assert exposed == all_names - DESTRUCTIVE_HINT_6 - NEW_DESTRUCTIVE
    assert len(exposed) == 25

    # disabled 再減
    exposed_disabled = reg.expected_exposed_tools(
        read_only=False, disable_destructive=False, disabled={"helm_list"}
    )
    assert exposed_disabled == all_names - {"helm_list"}
    assert len(exposed_disabled) == 33


def test_expected_exposed_tools_legacy_toolsets():
    """2026-10-25 之前開通的服務（toolsets core,config,helm）：數字與當時相同。"""
    legacy = ["core", "config", "helm"]
    assert len(reg.expected_exposed_tools(False, False, toolsets=legacy)) == 23
    assert reg.expected_exposed_tools(True, False, toolsets=legacy) == READ_ONLY_HINT_15
    assert len(reg.expected_exposed_tools(False, True, toolsets=legacy)) == 17
    # 當時的全鎖 seed（唯讀＋擋破壞性）→ 15 支
    assert reg.expected_exposed_tools(True, True, toolsets=legacy) == READ_ONLY_HINT_15
