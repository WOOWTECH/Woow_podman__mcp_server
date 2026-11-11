import React from 'react';
import {
  LayoutDashboard, Wrench, Link, KeyRound, ScrollText, Shield, Settings,
  Server, Globe, Activity, Brain, BookOpen, Database, MessageSquare,
} from 'lucide-react';

function st(h) { return h === true ? 'green' : h === false ? 'red' : 'gray'; }

export const appConfig = {
  brandTitle: 'Hermes MCP Admin',
  appName: 'hermes',
  nav: [
    { items: [
      { to: '/', label: 'Dashboard', icon: LayoutDashboard },
      { to: '/config', label: 'Connection', icon: Link },
    ] },
    { label: 'MCP Wrapper', items: [
      { to: '/tools', label: 'MCP Tools', icon: Wrench },
      { to: '/tokens', label: 'Tokens', icon: KeyRound },
    ] },
    { label: 'System', items: [
      { to: '/logs', label: 'Logs', icon: ScrollText },
      { to: '/permissions', label: 'Permissions', icon: Shield },
      { to: '/settings', label: 'Settings', icon: Settings },
    ] },
  ],
  healthBadges: [
    { key: 'gateway', label: 'Gateway' },
    { key: 'dashboard', label: 'Dashboard' },
  ],
  connection: {
    description: 'Configure the dual connection to the Hermes Agent gateway and dashboard',
    saveEndpoint: '/config/connection',
    saveLabel: 'Save & Restart MCP',
    sections: [
      // URL / username 欄一律不給 `default`（對齊其他六個 app 的 placeholder-only 慣例）。
      // 理由：這個 image 是通用產品，正確形狀依部署形態而異——直連 Service 是 host:port，
      // 反向代理在前（woow paas 的 hermes-agent authproxy 只開 8080，`/`→dashboard、
      // `/gateway/`→gateway）則是 <service-root> 與 <service-root>/gateway。預填任何一種
      // 都只是把錯誤翻到另一邊；留空才是顯性的「未設定」，配合後端守衛會回
      // 「... is not configured」並指回本頁。兩種形狀寫在 help 裡，不寫進值。
      {
        title: 'Gateway API Server', icon: Server, statusKey: 'gateway',
        test: { endpoint: '/config/test/gateway' },
        fields: [
          { name: 'gateway_url', label: 'Gateway URL', type: 'url', placeholder: 'http://hermes-agent-svc:8642',
            help: 'Direct to Service: http://<host>:8642 — behind a reverse proxy: https://<service-root>/gateway' },
          { name: 'gateway_api_key', label: 'API Key', type: 'password', maskedFrom: 'gateway_key_masked', help: 'The API key used to authenticate with the Hermes Gateway API' },
        ],
      },
      {
        title: 'Dashboard REST API', icon: Globe, statusKey: 'dashboard',
        test: { endpoint: '/config/test/dashboard' },
        fields: [
          { name: 'dashboard_url', label: 'Dashboard URL', type: 'url', placeholder: 'http://hermes-agent-svc:9119',
            help: 'Direct to Service: http://<host>:9119 — behind a reverse proxy: https://<service-root>' },
          { name: 'dashboard_username', label: 'Username', type: 'text', placeholder: 'admin' },
          { name: 'dashboard_password', label: 'Password', type: 'password', maskedFrom: 'dashboard_password_masked', help: 'Credentials for the Hermes Dashboard REST API' },
        ],
      },
    ],
  },
  dashboard: {
    subtitle: 'Hermes Agent system overview',
    buildSections: (h) => {
      const gateway = h.gateway || {}, dashboard = h.dashboard || {}, model = h.model || {};
      const tools = h.tools || {}, skills = h.skills || {}, mcpServers = h.mcp_servers || {}, sessions = h.sessions || {};
      return [
        { heading: 'Connection Status', cards: [
          { title: 'Gateway API', icon: Server, status: st(gateway.healthy), value: gateway.healthy ? 'Connected' : 'Disconnected', subtitle: gateway.url || 'Port 8642' },
          { title: 'Dashboard API', icon: Globe, status: st(dashboard.healthy), value: dashboard.healthy ? 'Connected' : 'Disconnected', subtitle: dashboard.url || 'Port 9119' },
          { title: 'Version', icon: Activity, status: 'gray', value: h.hermes_version || h.version || 'N/A', subtitle: 'Hermes Agent version' },
        ] },
        { heading: 'Agent Info', cards: [
          { title: 'Model', icon: Brain, status: model.provider ? 'green' : 'gray', value: model.name || 'N/A', subtitle: model.provider || 'No provider configured' },
          { title: 'Tools', icon: Wrench, status: tools.enabled != null ? 'green' : 'gray', value: tools.enabled != null ? `${tools.enabled} / ${tools.total || tools.enabled}` : 'N/A', subtitle: 'Enabled / total toolsets' },
          { title: 'Skills', icon: BookOpen, status: skills.enabled != null ? 'green' : 'gray', value: skills.enabled != null ? `${skills.enabled} / ${skills.total || skills.enabled}` : 'N/A', subtitle: 'Enabled / total skills' },
        ] },
        { heading: 'System', cards: [
          { title: 'MCP Servers', icon: Database, status: mcpServers.count != null ? 'green' : 'gray', value: mcpServers.count != null ? String(mcpServers.count) : 'N/A', subtitle: 'Configured MCP servers' },
          { title: 'Sessions', icon: MessageSquare, status: (sessions.active != null || sessions.total != null) ? 'green' : 'gray', value: sessions.active != null ? String(sessions.active) : 'N/A', subtitle: sessions.total != null ? `${sessions.total} total sessions` : 'Active agent sessions' },
        ] },
      ];
    },
    renderExtra: (h) => {
      const recent = h.sessions?.recent;
      if (!recent || recent.length === 0) return null;
      return (
        <>
          <h3 className="text-sm font-semibold text-muted uppercase tracking-wider mb-3 px-1">Recent Sessions</h3>
          <div className="bg-bg border border-border rounded-xl divide-y divide-border">
            {recent.map((s, idx) => (
              <div key={s.id || idx} className="flex items-center justify-between px-4 py-3">
                <div className="flex-1 min-w-0">
                  <span className="text-sm font-mono text-ink-2">{s.id ? s.id.substring(0, 12) + '...' : `Session ${idx + 1}`}</span>
                  {s.message_count != null && <span className="ml-3 text-xs text-muted">{s.message_count} messages</span>}
                </div>
                <span className="text-xs text-muted-2">{s.last_active ? new Date(s.last_active).toLocaleString() : s.created ? new Date(s.created).toLocaleString() : ''}</span>
              </div>
            ))}
          </div>
        </>
      );
    },
  },
  toolTabs: [
    { key: 'mcp', label: 'MCP Wrapper Tools', icon: Wrench, endpoint: '/tools', itemsKey: 'tools', grouped: true, labelText: '(hermes_mcp_admin)' },
    { key: 'hermes', label: 'Hermes Agent Toolsets', icon: Settings, endpoint: '/hermes-tools', itemsKey: 'toolsets', grouped: false, note: 'Changes require Gateway Restart to take effect' },
  ],
};
