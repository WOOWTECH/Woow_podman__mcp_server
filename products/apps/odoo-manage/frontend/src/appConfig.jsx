import React from 'react';
import { LayoutDashboard, Wrench, Link, KeyRound, ScrollText, Shield, Settings, Server, Globe, Radio, Activity, Database, Package } from 'lucide-react';
import { mcpServerCard } from '@woow/mcp-admin-ui';

function st(h) { return h === true ? 'green' : h === false ? 'red' : 'gray'; }

export const appConfig = {
  brandTitle: 'Odoo Manage MCP Admin',
  appName: 'odoo-manage',
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
    description: 'Configure the connection to the Odoo instance (ivnvxd/mcp-server-odoo)',
    saveEndpoint: '/config/connection',
    saveLabel: 'Save',
    sections: [
      {
        test: { endpoint: '/config/test' },
        fields: [
          { name: 'odoo_url', label: 'Odoo URL', type: 'url', placeholder: 'http://localhost:8069' },
          { name: 'odoo_db', label: 'Database', type: 'text', placeholder: 'odoo' },
          { name: 'odoo_user', label: 'Username', type: 'text', placeholder: 'admin' },
          // ivnvxd 標準模式（Phase 2）走 API Key：注入 ODOO_API_KEY。底層鍵名 odoo_api_key，
          // 由 process._build_subprocess_env 大寫成 ODOO_API_KEY 注入子行程。
          { name: 'odoo_api_key', label: 'API Key', type: 'password', maskedFrom: 'odoo_api_key_masked',
            help: 'Odoo API Key（Settings → Account Security → New API Key）。走 XML-RPC，開了 2FA 的帳號必須用 API Key。' },
          // YOLO 模式：off / read / true（#1351：改下拉，避免自由文字打錯；後端仍接受別名並正規化）。
          // 標準模式需目標 Odoo 裝 Odoo 端 MCP 模組；PaaS 自家 Odoo 範本沒有該模組，要用 read / true。
          { name: 'odoo_yolo', label: 'YOLO Mode', type: 'select', default: 'off',
            options: [
              { value: 'off', label: 'off — 標準模式（需目標 Odoo 安裝 MCP 模組）' },
              { value: 'read', label: 'read — 唯讀（純 XML-RPC，封鎖寫入）' },
              { value: 'true', label: 'true — 全開（純 XML-RPC，可讀寫）' },
            ],
            help: '標準模式（off）透過 Odoo 端 MCP 模組（mcp_server，/mcp/* 端點）連線並套用模組的存取控制，目標 Odoo 必須先安裝該模組。\nPaaS 自家的 Odoo 服務沒有安裝這個模組，請選 read（唯讀）或 true（可讀寫），以上方 API Key（Odoo 帳號的 API Key）走一般 XML-RPC。' },
        ],
      },
    ],
  },
  dashboard: {
    subtitle: 'System health overview',
    buildSections: (h) => ([
      { cards: [
        mcpServerCard(h, Server),
        // #1351：標準模式但目標 Odoo 缺 MCP 模組 → 黃燈＋提示改用 YOLO 模式（health 的 target_app.warning）
        { title: 'Odoo Instance', icon: Globe, status: h.target_app?.warning ? 'yellow' : st(h.target_app?.healthy), value: h.target_app?.warning ? 'MCP 模組未安裝' : h.target_app?.healthy ? 'Connected' : 'Disconnected', subtitle: h.target_app?.warning || h.target_app?.url || 'No URL configured' },
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
    { key: 'mcp', label: 'Tools', icon: Wrench, endpoint: '/tools', itemsKey: 'tools', grouped: true, labelText: '(odoo_manage_mcp_admin)' },
  ],
};
