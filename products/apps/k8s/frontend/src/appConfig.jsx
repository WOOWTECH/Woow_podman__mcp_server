import React from 'react';
import { LayoutDashboard, Wrench, Link, KeyRound, ScrollText, Shield, Settings, Server, Globe, Radio, Activity, Database } from 'lucide-react';
import { mcpServerCard } from '@woow/mcp-admin-ui';

function st(h) { return h === true ? 'green' : h === false ? 'red' : 'gray'; }

export const appConfig = {
  brandTitle: 'K8s MCP Admin',
  appName: 'k8s',
  nav: [
    { items: [
      { to: '/', label: 'Dashboard', icon: LayoutDashboard },
      { to: '/tools', label: 'Tools', icon: Wrench },
      { to: '/config', label: 'Connection', icon: Link },
      { to: '/tokens', label: 'Tokens', icon: KeyRound },
      { to: '/logs', label: 'Logs', icon: ScrollText },
      { to: '/permissions', label: 'Permissions', icon: Shield },
      { to: '/settings', label: 'Settings', icon: Settings },
    ] },
  ],
  healthBadges: [],
  connection: {
    description: 'Configure the connection to the Kubernetes cluster (containers/kubernetes-mcp-server)',
    saveEndpoint: '/config/connection',
    saveLabel: 'Save',
    sections: [
      {
        test: { endpoint: '/config/test' },
        fields: [
          // kubeconfig 含 cluster 憑證（client-key-data/token，常等於 cluster-admin），以 secret
          // 型 textarea 呈現：載入即空值、以 masked 摘要當 placeholder、留空即沿用已存檔值、不做 show/hide 眼睛。
          // 底層鍵名 kubeconfig，由 process._build_subprocess_env 加 K8S_MCP_ 前綴大寫成
          // K8S_MCP_KUBECONFIG 注入子行程，launcher 再派生成 /data/kubeconfig（路徑語意）。
          { name: 'kubeconfig', label: 'Kubeconfig', type: 'textarea', secret: true, rows: 10,
            maskedFrom: 'kubeconfig_masked',
            placeholder: '貼上完整 kubeconfig YAML；in-cluster 模式免填',
            help: '含 cluster 憑證，以 secret 處理：載入時不回顯、留空即沿用已存檔值。切換 In-Cluster 為 true 會清空已存檔的 kubeconfig。' },
          // in_cluster 以字串 "true"/"false" 存檔（連線值一律字串），後端 PUT 做別名正規化（1/yes→true）。
          // #1352：PaaS 部署 automountServiceAccountToken=false，in-cluster 必然失敗；後端偵測不到
          // SA token 時 PUT in_cluster=true 回 400、Test／Dashboard 顯示可讀原因。
          { name: 'in_cluster', label: 'In-Cluster Mode', type: 'text', placeholder: 'false',
            help: 'PaaS 上請維持 false，並在上方貼上「你自己叢集」的 kubeconfig。MCP 預設全開（寫入、破壞性操作、讀 Secret、多叢集都開），AI 實際能做什麼由這份 kubeconfig 的 RBAC 決定：要限制就給較小的權限（例如只綁唯讀的 view ClusterRole），或在 Settings 頁打開唯讀／擋破壞性、關閉 Secret 存取（步驟見 README「外部叢集授權 SOP」）。PaaS 部署不掛載叢集權杖，設為 true 會被拒絕。true 僅適用於自行部署且 Pod 有掛 ServiceAccount token 的情境（會清空已存檔的 kubeconfig）。' },
        ],
      },
    ],
  },
  dashboard: {
    subtitle: 'System health overview',
    buildSections: (h) => ([
      { cards: [
        mcpServerCard(h, Server),
        // target_app = 叢集 API server 可達性（帶 CA 的匿名 GET /version 探測）。
        { title: 'Cluster', icon: Globe, status: st(h.target_app?.healthy), value: h.target_app?.healthy ? 'Reachable' : 'Unreachable', subtitle: h.target_app?.error || h.target_app?.url || h.target_app?.message || 'No cluster configured' },
        { title: 'MCP Proxy', icon: Radio, status: st(h.proxy?.healthy), value: h.proxy?.healthy ? 'Active' : 'Inactive', subtitle: h.proxy?.process || 'built-in proxy' },
      ] },
      { heading: 'Details', cards: [
        // version = 叢集 gitVersion（自 /version）；db_name = current-context 名（in-cluster 模式顯示 in-cluster）。
        { title: 'Cluster Version', icon: Activity, status: 'gray', value: h.version || 'N/A', subtitle: 'Kubernetes gitVersion' },
        { title: 'Context', icon: Database, status: 'gray', value: h.db_name || 'N/A', subtitle: 'Active kube context' },
      ] },
    ]),
  },
  toolTabs: [
    { key: 'mcp', label: 'Tools', icon: Wrench, endpoint: '/tools', itemsKey: 'tools', grouped: true, labelText: '(k8s_mcp_admin)',
      note: '預設全開：config／core／helm／kcp／kubevirt／tekton 六個工具集共 34 支（kcp、KubeVirt、Tekton 的工具要叢集有裝對應元件才有作用）；多叢集模式下 kubeconfig 有多個 context 時再多 configuration_contexts_list，其他工具可指定 context。Read-only／Disable Destructive 開啟時，部分工具不會曝露給 MCP client。此處的 per-tool 停用儲存後立即由 MCP proxy 隱藏並拒絕呼叫（下次子行程重啟時也會寫入 TOML disabled_tools）。' },
  ],
  // Settings → MCP Server 的宣告式 env 開關；value 以字串 '1' / '0' 寫入 mcp_server.env，
  // 對齊 launcher 端 strip().lower() in ('1','true','yes') 的判定。三顆對映上游 governance flags。
  // 預設全開（負責人 2026-10-25「預設全開」）：唯讀關、擋破壞性關、Secret 存取開、多叢集開（launcher.FRESH_ENV；
  // 既有設定第一次以新映像啟動時一次性改成全開，之後管理員存的值保留）。
  settingsEnvToggles: [
    {
      env: 'K8S_MCP_READ_ONLY',
      label: 'Read-only Mode',
      description:
        '唯讀模式（預設關閉）：開啟後僅曝露唯讀工具（readOnlyHint=true），寫入類工具全數隱藏。變更後按 Save MCP Config 會自動重啟 MCP server 生效。',
    },
    {
      env: 'K8S_MCP_DISABLE_DESTRUCTIVE',
      label: 'Disable Destructive Tools',
      description:
        '擋破壞性工具（預設關閉）：開啟後隱藏 destructiveHint=true 的工具（delete／exec／uninstall、VM 建立／複製／啟停類）。變更後按 Save MCP Config 會自動重啟 MCP server 生效。',
    },
    {
      env: 'K8S_MCP_ALLOW_SECRETS',
      label: 'Allow Secrets Access',
      description:
        'Secret 存取（預設開啟）：AI client 可讀寫叢集的 Secret；關閉後以 denied_resources 封鎖 Secret。變更後按 Save MCP Config 會自動重啟 MCP server 生效。',
    },
    {
      env: 'K8S_MCP_MULTI_CLUSTER',
      label: 'Multi-cluster (kubeconfig contexts)',
      description:
        '多叢集（預設開啟）：kubeconfig 有多個 context 時，AI 可以列出 context（configuration_contexts_list）並在各工具指定 context 操作；關閉後一律只用 current-context。只有一個 context 時沒有差別。變更後按 Save MCP Config 會自動重啟 MCP server 生效。',
    },
  ],
};
