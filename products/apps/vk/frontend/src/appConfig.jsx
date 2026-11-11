import React from 'react';
import { LayoutDashboard, Wrench, Link, KeyRound, ScrollText, Settings, Server, Globe, Radio, Activity } from 'lucide-react';
import { mcpServerCard } from '@woow/mcp-admin-ui';

function st(h) { return h === true ? 'green' : h === false ? 'red' : 'gray'; }

export const appConfig = {
  brandTitle: 'VK MCP Admin',
  appName: 'vk',
  nav: [
    { items: [
      { to: '/', label: 'Dashboard', icon: LayoutDashboard },
      { to: '/tools', label: 'Tools', icon: Wrench },
      { to: '/config', label: 'Connection', icon: Link },
      { to: '/tokens', label: 'Tokens', icon: KeyRound },
      { to: '/logs', label: 'Logs', icon: ScrollText },
      // 刻意不放權限管理頁：vk 工具 operations=[]，該頁無內容可管
      { to: '/settings', label: 'Settings', icon: Settings },
    ] },
  ],
  // Sidebar 硬性依賴：Sidebar.jsx 無條件解構 healthBadges 並呼叫 healthBadges.length，
  // 缺此鍵登入後所有受保護頁會 TypeError 白屏。vk 無 badge 需求故給空陣列（比照 odoo-manage）。
  healthBadges: [],
  connection: {
    description: 'Configure the connection to the Vibe Kanban Host API (supergateway + vibe-kanban-mcp)',
    saveEndpoint: '/config/connection',
    saveLabel: 'Save',
    sections: [
      {
        test: { endpoint: '/config/test' },
        // 單一非 secret 欄 vibe_backend_url：由 process._build_subprocess_env 大寫成
        // VIBE_BACKEND_URL 注入子行程；無 secret 欄故不需遮罩設定。
        fields: [
          { name: 'vibe_backend_url', label: 'VK Host API URL', type: 'url',
            placeholder: 'http://localhost:3000',
            help: 'Injected into the MCP subprocess as VIBE_BACKEND_URL. Test checks GET {url}/api/health of the saved value.' },
        ],
      },
    ],
  },
  dashboard: {
    subtitle: 'System health overview',
    buildSections: (h) => ([
      { cards: [
        mcpServerCard(h, Server),
        { title: 'VK Host', icon: Globe, status: st(h.target_app?.healthy), value: h.target_app?.healthy ? 'Connected' : 'Disconnected', subtitle: h.target_app?.url || 'No URL configured' },
        { title: 'MCP Proxy', icon: Radio, status: st(h.proxy?.healthy), value: h.proxy?.healthy ? 'Active' : 'Inactive', subtitle: h.proxy?.process || 'built-in proxy' },
      ] },
      // Details 只留 Version：vk 無 db/模組概念，不放 Database/Modules 卡避免恆 N/A 噪音
      { heading: 'Details', cards: [
        { title: 'Version', icon: Activity, status: 'gray', value: h.version || 'N/A', subtitle: 'MCP Admin version' },
      ] },
    ]),
  },
  toolTabs: [
    // note 為提示文字：共用 ToolManager→ToolToggleList 以 amber 橫幅渲染於工具清單頂部。
    // 工具開關僅寫入 config.json、無強制力（core 不注入子行程 env、binary 無過濾 flag、proxy 不解析 JSON-RPC）。
    { key: 'mcp', label: 'Tools', icon: Wrench, endpoint: '/tools', itemsKey: 'tools', grouped: true, labelText: '(vk_mcp_admin)',
      note: '停用後立即生效：MCP proxy 會從 tools/list 隱藏並拒絕 tools/call（不需重啟、不中斷現有 session）。' },
  ],
};
