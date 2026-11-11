// Dashboard「MCP Server」卡片（各 app 共用）。
// 除了 Online／Offline 與 pid，也顯示看門狗狀態（商用驗收 DATA-NMCP-004-1）：
// 自動重啟次數、排程中的重試倒數、子行程 exit code。欄位來自 /api/health 的 mcp_server
// （mcp_admin_core.routers.health.mcp_server_health）；舊後端沒有這些欄位時照舊顯示。
// 純 JS、不含 JSX，方便 node --test 直接測。

const FAST_REFETCH_MS = 3_000;
const NORMAL_REFETCH_MS = 30_000;

export function describeMcpServer(m) {
  if (!m) return { status: 'gray', value: 'Offline', subtitle: 'process unknown' };
  const restarts = `Auto-restarts: ${m.auto_restart_count ?? 0}`;
  const exit = m.exit_code != null ? `exit code ${m.exit_code}` : null;
  if (m.healthy) {
    return { status: 'green', value: 'Online', subtitle: [m.process || 'running', restarts].join(' · ') };
  }
  if (m.auto_restart_pending) {
    const wait = m.next_auto_restart_in != null
      ? `auto-restart in ${Math.ceil(m.next_auto_restart_in)}s`
      : 'auto-restart scheduled';
    return { status: 'yellow', value: 'Restarting', subtitle: [exit || 'exited', wait, restarts].join(' · ') };
  }
  return {
    status: 'red',
    value: 'Offline',
    subtitle: [m.process || 'process unknown', exit, restarts].filter(Boolean).join(' · '),
  };
}

export function mcpServerCard(health, icon) {
  return { title: 'MCP Server', icon, ...describeMcpServer(health?.mcp_server) };
}

// 看門狗排程中（子行程剛崩潰、等待重試）時 Dashboard 每 3 秒刷新，讓倒數與恢復看得到；平常 30 秒。
export function healthRefetchInterval(health) {
  return health?.mcp_server?.auto_restart_pending ? FAST_REFETCH_MS : NORMAL_REFETCH_MS;
}
