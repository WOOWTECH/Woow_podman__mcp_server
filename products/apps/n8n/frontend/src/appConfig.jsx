import React from 'react';
import { LayoutDashboard, Wrench, Link, KeyRound, ScrollText, Shield, Settings, Server, Globe, Radio, Activity, Database, Package } from 'lucide-react';
import { mcpServerCard } from '@woow/mcp-admin-ui';

function st(h) { return h === true ? 'green' : h === false ? 'red' : 'gray'; }

export const appConfig = {
  brandTitle: 'n8n MCP Admin',
  appName: 'n8n',
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
    description: 'Configure the connection to the n8n instance',
    saveEndpoint: '/config/connection',
    saveLabel: 'Save',
    sections: [
      {
        test: { endpoint: '/config/test' },
        fields: [
          { name: 'n8n_api_url', label: 'n8n API URL', type: 'url', placeholder: 'https://n8n.example.com', help: '填 n8n 的對外網址（例如 https://n8n.example.com）。MCP 子行程不連私網、本機（localhost／127.0.0.1）、叢集內名稱（例如 http://n8n:5678）與雲端 metadata，也不接受帶帳號密碼的網址：這些網址存檔時就會被拒（400）。' },
          { name: 'n8n_api_key', label: 'n8n API Key', type: 'password', maskedFrom: 'n8n_api_key_masked', help: 'Generate in n8n: Settings → API → Create API Key' },
          { name: 'n8n_mcp_access_token', label: 'Native MCP Access Token (optional)', type: 'password', maskedFrom: 'n8n_mcp_access_token_masked', help: 'For direct / prepare / pinned: enable Settings → Instance-level MCP in n8n and copy its separate API key. Connection Test verifies the REST key only.' },
          { name: 'clear_n8n_mcp_access_token', label: 'Saved Native MCP Token', type: 'select', default: 'false', options: [{ value: 'false', label: 'Keep or replace with the token above' }, { value: 'true', label: 'Clear the saved native MCP token on save' }] },
          { name: 'mcp_session_timeout', label: 'Session Timeout (s)', type: 'number', default: 900, help: 'MCP session 閒置多久後結束並釋放名額（60–86400 秒，預設 900）。實際以分鐘生效（無條件進位）；上游 n8n-mcp 每 5 分鐘才清掃一次過期 session，所以會在設定值之後 0–5 分鐘內才真正結束，最小有效粒度約 5 分鐘（例：設 60 秒，約 1–6 分鐘後釋放）。名額滿時不必等逾時：閘道會先結束 5 分鐘沒有呼叫的 session（見 Max Sessions）。開著 GET 串流不算使用中——上游的逾時也只看呼叫。' },
          { name: 'mcp_max_sessions', label: 'Max Sessions', type: 'number', default: 20, help: '同時存在的 MCP session 上限（1–100，預設 20）。名額滿時，閘道先結束最久沒活動、5 分鐘沒有呼叫的 session（用戶端重連或結束時沒送 DELETE 留下的；Claude Code 結束時不送 DELETE；開著 GET 串流不算使用中，還有呼叫在跑的才不結束），再讓新的 initialize 進來，每分鐘最多結束 10 個。被結束的用戶端下次呼叫會收到 404，依 MCP 規範重新 initialize（Claude Code 2.1.310 實測會自動重連並重試那次呼叫；Claude.ai 尚未實測）。所有 session 都在 5 分鐘內有呼叫（或還有呼叫在跑）時，新的 initialize 才回 429。要立即清空所有 session，到 Settings 按 MCP Server Process 的 Restart（連線中的用戶端會斷線，需重新 initialize）。' },
        ],
      },
    ],
  },
  dashboard: {
    subtitle: 'System health overview',
    buildSections: (h) => ([
      { cards: [
        mcpServerCard(h, Server),
        { title: 'n8n Instance', icon: Globe, status: st(h.target_app?.healthy), value: h.target_app?.healthy ? 'Connected' : 'Disconnected', subtitle: h.target_app?.url || 'No URL configured' },
        { title: 'MCP Proxy', icon: Radio, status: st(h.proxy?.healthy), value: h.proxy?.healthy ? 'Active' : 'Inactive', subtitle: h.proxy?.process || 'built-in proxy' },
      ] },
      { heading: 'Details', cards: [
        { title: 'Version', icon: Activity, status: 'gray', value: h.version || 'N/A', subtitle: 'MCP Admin version' },
        { title: 'Database', icon: Database, status: 'gray', value: h.db_name || 'N/A', subtitle: 'n8n database' },
        { title: 'Workflows', icon: Package, status: 'gray', value: h.item_count != null ? String(h.item_count) : 'N/A', subtitle: 'Active workflows' },
      ] },
    ]),
  },
  toolTabs: [
    { key: 'mcp', label: 'Tools', icon: Wrench, endpoint: '/tools', itemsKey: 'tools', grouped: true, labelText: '(n8n_mcp_admin)' },
  ],
};
