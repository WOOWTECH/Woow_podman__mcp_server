import React from 'react';
import { mcpServerCard } from '@woow/mcp-admin-ui';

const icon = name => ({ size = 20, className = '' }) =>
  <span aria-hidden="true" className={`mdi mdi-${name} ${className}`} style={{ fontSize: size, lineHeight: 1 }} />;
const LayoutDashboard = icon('view-dashboard-outline'), Wrench = icon('wrench-outline'),
  Link = icon('link-variant'), KeyRound = icon('key-outline'), ScrollText = icon('text-box-outline'),
  Settings = icon('cog-outline'), Server = icon('server'), Radio = icon('access-point'), Activity = icon('pulse');

export const appConfig = {
  brandTitle: 'EMQX MCP Admin', appName: 'emqx', healthBadges: [],
  nav: [{ items: [
    { to: '/', label: 'Dashboard', icon: LayoutDashboard },
    { to: '/tools', label: 'Tools', icon: Wrench },
    { to: '/config', label: 'Connection', icon: Link },
    { to: '/tokens', label: 'Tokens', icon: KeyRound },
    { to: '/logs', label: 'Logs', icon: ScrollText },
    { to: '/settings', label: 'Settings', icon: Settings },
  ] }],
  connection: {
    description: '使用專用 EMQX API key 連接 broker。工具會以此 key 的權限執行。',
    saveEndpoint: '/config/connection', saveLabel: 'Save',
    sections: [{ test: { endpoint: '/config/test' }, fields: [
      { name: 'base_url', label: 'EMQX URL', type: 'url', placeholder: 'https://broker.example.com',
        help: '填寫 EMQX Dashboard API 根網址；可含 /api/v5。請使用最終網址，連線不會跟隨轉址。' },
      { name: 'api_key', label: 'API key', type: 'password', maskedFrom: 'api_key_masked',
        help: '使用 EMQX 的 API key。留空保留目前值。' },
      { name: 'api_secret', label: 'API secret', type: 'password', maskedFrom: 'api_secret_masked',
        help: '填寫 API secret，非 Dashboard 登入密碼。存檔後只顯示遮罩；留空保留目前值。' },
    ] }],
  },
  dashboard: { subtitle: 'Broker and MCP health', buildSections: h => [{ cards: [
    mcpServerCard(h, Server),
    { title: 'EMQX', icon: Radio, status: h.target_app?.healthy ? 'green' : 'red',
      value: h.target_app?.healthy ? 'Connected' : 'Disconnected', subtitle: h.target_app?.error || 'Authenticated broker identity' },
    { title: 'Broker version', icon: Activity, status: 'gray', value: h.version || 'N/A', subtitle: `${h.item_count ?? 0} nodes` },
  ] }] },
  toolTabs: [{ key: 'mcp', label: 'Tools', icon: Wrench, endpoint: '/tools', itemsKey: 'tools', grouped: true,
    labelText: '(emqx_mcp_admin)',
    note: '39 支工具、7 個群組，依管理預設全部啟用。SQL 測試會在 broker 執行運算，列為寫入執行權限，可單獨撤銷。13 支具破壞性或寫入風險的工具另加上 SQL 測試，以警示標記顯示。工具與操作限制即時生效，並保留至下次啟動。' }],
};
