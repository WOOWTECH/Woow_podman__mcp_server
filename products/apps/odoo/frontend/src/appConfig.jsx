import React from 'react';
import { LayoutDashboard, Wrench, Link, KeyRound, ScrollText, Shield, Settings, Server, Globe, Radio, Activity, Database, Package } from 'lucide-react';
import { mcpServerCard } from '@woow/mcp-admin-ui';
import AllowedMethods from './AllowedMethods';

function st(h) { return h === true ? 'green' : h === false ? 'red' : 'gray'; }

export const appConfig = {
  brandTitle: 'Odoo MCP Admin',
  appName: 'odoo',
  settingsExtra: AllowedMethods,
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
    description: 'Configure the Odoo account for this Profile. Changing its URL, database or username revokes the old MCP URL; copy the new one from Tokens. A password or API Key refresh for the same account keeps the URL.',
    saveEndpoint: '/config/connection',
    saveLabel: 'Save',
    sections: [
      {
        test: { endpoint: '/config/test' },
        fields: [
          { name: 'odoo_url', label: 'Odoo URL', type: 'url', placeholder: 'http://localhost:8069' },
          { name: 'odoo_db', label: 'Database', type: 'text', placeholder: 'odoo' },
          { name: 'odoo_username', label: 'Username', type: 'text', placeholder: 'admin' },
          // 單一 credential 欄位：Odoo XML-RPC 的 authenticate 只有一個密碼位子，
          // 套件（odoo_client.py:35）是 `api_key or password`，因此 API Key 直接放這裡即可；
          // 底層仍以 odoo_password 儲存 / 注入 ODOO_PASSWORD，免 schema 遷移。
          { name: 'odoo_password', label: 'API Key', type: 'password', maskedFrom: 'odoo_password_masked', clearSaved: true,
            help: 'Odoo API Key（Settings → Account Security → New API Key）。走 XML-RPC，開了 2FA 的帳號必須用 API Key；未開 2FA 的帳號也可改填登入密碼。' },
        ],
      },
    ],
  },
  dashboard: {
    subtitle: 'System health overview',
    buildSections: (h) => ([
      { cards: [
        mcpServerCard(h, Server),
        { title: 'Odoo Instance', icon: Globe, status: st(h.target_app?.healthy), value: h.target_app?.healthy ? 'Connected' : 'Disconnected', subtitle: h.target_app?.url || 'No URL configured' },
        { title: 'MCP Proxy', icon: Radio, status: st(h.proxy?.healthy), value: h.proxy?.healthy ? 'Active' : 'Inactive', subtitle: h.proxy?.process || 'built-in proxy' },
      ] },
      { heading: 'Details', cards: [
        { title: 'Version', icon: Activity, status: 'gray', value: h.version || 'N/A', subtitle: 'MCP Admin version' },
        { title: 'Database', icon: Database, status: 'gray', value: h.db_name || 'N/A', subtitle: 'Odoo database' },
        { title: 'Modules', icon: Package, status: 'gray', value: h.item_count != null ? String(h.item_count) : 'N/A', subtitle: 'Installed modules' },
      ] },
    ]),
  },
  toolTabs: [
    { key: 'mcp', label: 'Tools', icon: Wrench, endpoint: '/tools', itemsKey: 'tools', grouped: true, labelText: '(odoo_mcp_admin)' },
  ],
  // Settings → MCP Server 的宣告式 env 開關；value 以字串 '1' / '0' 寫入 mcp_server.env，
  // 對齊子行程的判定（strip().lower() in ('1','true','yes')）。四顆預設全開（負責人 2026-10-25「預設全開」，
  // 見 odoo_mcp_admin/seed_defaults.py；既有設定第一次以新映像啟動時一次性改成全開），之後管理員存的值一律保留。
  settingsEnvToggles: [
    {
      env: 'ODOO_MCP_ENABLE_WRITES',
      label: 'Enable Approved Writes',
      description:
        '寫入總閘門（預設開啟）。關閉時任何工具都不寫入：execute_method 只跑審過的讀取方法（search、read、read_group 之類；名字是 get_ 開頭但不在清單上的方法也拒絕，例如會產生分享 token 的 get_portal_url），chatter_post 與三階段審核的 execute_approved_write 一律拒絕，其他三個開關都不生效。開啟時仍受工具權限與 Odoo 帳號自己的權限限制。變更後按 Save MCP Config 會自動重啟 MCP server 生效。',
    },
    {
      env: 'ODOO_MCP_ALLOW_DIRECT_WRITES',
      label: 'Allow Direct Writes',
      description:
        '預設開啟：execute_method 直接 create／write／unlink，不必走三階段寫入審批（preview → validate → execute；三支審批工具仍列出，可選用）。關閉後 create／write／unlink 只能走三階段審批，execute_method 也不直接執行未審查的方法（action_*、copy、不在讀取清單上的 get_* 之類；只有白名單裡的 model.method 照跑，Allow Unreviewed Methods 此時沒有作用）。變更後按 Save MCP Config 會自動重啟 MCP server 生效。',
    },
    {
      env: 'ODOO_MCP_ALLOW_UNKNOWN_METHODS',
      label: 'Allow Unreviewed Methods',
      description:
        '預設開啟：execute_method 可呼叫未審查的副作用方法（例如 action_confirm、action_post、button_validate）；只在 Allow Direct Writes 開啟時有作用。關閉後這類方法會被擋下（不在讀取清單上的 get_* 也算；審過的讀取方法不受影響；create／write／unlink 由 Allow Direct Writes 決定）。變更後按 Save MCP Config 會自動重啟 MCP server 生效。',
    },
    {
      env: 'MCP_CHATTER_DIRECT',
      label: 'Direct Chatter Post',
      description:
        '預設開啟：chatter_post 第一次呼叫就張貼訊息，不先回預覽與核准權杖。關閉後改成先預覽、帶 approval 與 confirm=true 再張貼，execute_method 也不能直接執行在 chatter 張貼訊息的呼叫（message_post 等 message_* 方法、mail.message 的 create、mail.compose.message 送信、完成活動的 action_feedback／activity_feedback、rating_apply 之類，白名單也不例外；改用 chatter_post）。其他呼叫改資料時 Odoo 自己在 chatter 留的紀錄（欄位追蹤、action_confirm 這類業務方法）不歸這個開關管，由 Allow Direct Writes／Allow Unreviewed Methods 管。變更後按 Save MCP Config 會自動重啟 MCP server 生效。',
    },
  ],
};
