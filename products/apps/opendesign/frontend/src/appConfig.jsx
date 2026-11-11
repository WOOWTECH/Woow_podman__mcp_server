import React from 'react';
import { LayoutDashboard, Wrench, Link, KeyRound, ScrollText, Settings, Server, Globe, Radio, Activity } from 'lucide-react';
import { mcpServerCard } from '@woow/mcp-admin-ui';

function st(h) { return h === true ? 'green' : h === false ? 'red' : 'gray'; }

export const appConfig = {
  brandTitle: 'Open Design MCP Admin',
  appName: 'opendesign',
  nav: [
    { items: [
      { to: '/', label: 'Dashboard', icon: LayoutDashboard },
      { to: '/tools', label: 'Tools', icon: Wrench },
      { to: '/config', label: 'Connection', icon: Link },
      { to: '/tokens', label: 'Tokens', icon: KeyRound },
      { to: '/logs', label: 'Logs', icon: ScrollText },
      // 刻意不放權限管理頁：opendesign 15 支工具 operations=[]，該頁無內容可管（比照 vk）。
      { to: '/settings', label: 'Settings', icon: Settings },
    ] },
  ],
  // Sidebar 硬性依賴：Sidebar.jsx 無條件解構 healthBadges 並呼叫 healthBadges.length，
  // 缺此鍵登入後所有受保護頁會 TypeError 白屏。opendesign 無 badge 需求故給空陣列（比照 vk / odoo-manage）。
  healthBadges: [],
  connection: {
    description: 'Configure the connection to the Open Design daemon (od MCP server)',
    saveEndpoint: '/config/connection',
    saveLabel: 'Save',
    sections: [
      {
        test: { endpoint: '/config/test' },
        fields: [
          // 非 secret URL 欄：由 process._build_subprocess_env（env_prefix=""）大寫成
          // OD_API_BASE 注入子行程，1:1 對齊 vendored od_mcp_server.py 直讀的固定 env 名。
          { name: 'od_api_base', label: 'Open Design Daemon URL', type: 'url',
            placeholder: 'http://open-design-svc:7457',
            help: 'Open Design daemon 位址（例：叢集內 http://open-design-svc:7457，或 PaaS 服務的 https://… 網址）；注入子行程 OD_API_BASE。帳密請填下方 Basic Auth 欄位。Test 檢查存檔值的 GET {url}/api/health。' },
          // 選配非 secret「數字文字欄」：刻意用 type:'text' 而非 'number'——後端合約為 str|None、
          // 空字串＝清除回退上游預設 300，而 ConnectionConfig 對 type:'number' 會 parseInt 強轉數字
          // （空值變 0），會同時破壞字串型別與「空＝清除」語意。
          { name: 'od_chat_timeout', label: 'Chat Timeout (秒)', type: 'text',
            placeholder: '300',
            help: 'AI 生成最長等待秒數，預設 300；選配，注入子行程 OD_CHAT_TIMEOUT。留空＝清除回退上游預設。' },
          // #1354：Open Design 前置 basic auth（PaaS 的 open-design 服務）。password 為 secret：
          // 載入即空值、以遮罩摘要當 placeholder、留空沿用既有值。帳密不要再寫進 URL。
          { name: 'od_basic_user', label: 'Basic Auth 帳號', type: 'text',
            placeholder: 'admin',
            help: '接 PaaS 上的 Open Design（前面有帳號密碼保護）時填：帳號通常是 admin。自架且無認證的 daemon 請留空；清空即不帶認證。' },
          { name: 'od_basic_password', label: 'Basic Auth 密碼', type: 'password', maskedFrom: 'od_basic_password_masked',
            help: 'PaaS Open Design 服務的登入密碼。請填在這裡，不要寫進上方 URL（URL 不會遮罩）；舊的 https://帳號:密碼@主機 寫法存檔時會自動移到這兩欄。' },
          // 不設 od_api_token（G2-B 已決）：現行 wrapper 不送 Authorization，接死欄會誤導管理者
          // 以為填了有保護；未來 daemon 開認證時連同 wrapper patch 一併補欄位。
        ],
      },
    ],
  },
  dashboard: {
    subtitle: 'System health overview',
    buildSections: (h) => ([
      { cards: [
        mcpServerCard(h, Server),
        { title: 'Open Design Daemon', icon: Globe, status: st(h.target_app?.healthy), value: h.target_app?.healthy ? 'Connected' : 'Disconnected', subtitle: h.target_app?.url || 'No URL configured' },
        { title: 'MCP Proxy', icon: Radio, status: st(h.proxy?.healthy), value: h.proxy?.healthy ? 'Active' : 'Inactive', subtitle: h.proxy?.process || 'built-in proxy' },
      ] },
      // Details 只留 Version：opendesign daemon 無 db/模組概念，不放 Database/Modules 卡避免恆 N/A 噪音。
      { heading: 'Details', cards: [
        { title: 'Version', icon: Activity, status: 'gray', value: h.version || 'N/A', subtitle: 'MCP Admin version' },
      ] },
    ]),
  },
  toolTabs: [
    // note 為提示文字：共用 ToolManager→ToolToggleList 以 amber 橫幅渲染於工具清單頂部。
    // 工具開關僅寫入 config.json、無強制力（core 不注入子行程 env、FastMCP 無過濾 flag、proxy 不解析 JSON-RPC）。
    { key: 'mcp', label: 'Tools', icon: Wrench, endpoint: '/tools', itemsKey: 'tools', grouped: true, labelText: '(opendesign_mcp_admin)',
      note: '長時間操作（create/send 最長 300s）；delete_project 不可逆，不需要時建議停用。停用後立即由 MCP proxy 隱藏並拒絕呼叫（不需重啟）。' },
  ],
};
