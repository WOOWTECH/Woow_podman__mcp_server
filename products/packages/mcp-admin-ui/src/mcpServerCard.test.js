// node --test packages/mcp-admin-ui/src/mcpServerCard.test.js
// Dashboard「MCP Server」卡片要看得到看門狗狀態（商用驗收 DATA-NMCP-004-1）。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { describeMcpServer, mcpServerCard, healthRefetchInterval } from './mcpServerCard.js';

test('running: Online + pid + 自動重啟次數', () => {
  const d = describeMcpServer({ healthy: true, process: 'pid=88', restart_count: 2, auto_restart_count: 4 });
  assert.equal(d.status, 'green');
  assert.equal(d.value, 'Online');
  assert.match(d.subtitle, /pid=88/);
  assert.match(d.subtitle, /Auto-restarts: 4/);
});

test('crashed, watchdog pending: Restarting + exit code + 倒數', () => {
  const d = describeMcpServer({
    healthy: false, process: 'stopped', auto_restart_count: 1,
    auto_restart_pending: true, next_auto_restart_in: 4.2, exit_code: -9,
  });
  assert.equal(d.status, 'yellow');
  assert.equal(d.value, 'Restarting');
  assert.match(d.subtitle, /exit code -9/);
  assert.match(d.subtitle, /auto-restart in 5s/);
  assert.match(d.subtitle, /Auto-restarts: 1/);
});

test('stopped, nothing scheduled: Offline + exit code', () => {
  const d = describeMcpServer({ healthy: false, process: 'stopped', auto_restart_count: 0, auto_restart_pending: false, exit_code: 1 });
  assert.equal(d.status, 'red');
  assert.equal(d.value, 'Offline');
  assert.match(d.subtitle, /exit code 1/);
});

test('舊後端（沒有看門狗欄位）照舊顯示', () => {
  const d = describeMcpServer({ healthy: true, process: 'pid=7', restart_count: 0 });
  assert.equal(d.value, 'Online');
  assert.match(d.subtitle, /pid=7/);
  assert.match(d.subtitle, /Auto-restarts: 0/);
});

test('沒有 mcp_server 區塊：灰色、process unknown', () => {
  const d = describeMcpServer(undefined);
  assert.equal(d.status, 'gray');
  assert.equal(d.value, 'Offline');
  assert.equal(d.subtitle, 'process unknown');
});

test('mcpServerCard 帶 title 與 icon', () => {
  const icon = () => null;
  const c = mcpServerCard({ mcp_server: { healthy: true, process: 'pid=1' } }, icon);
  assert.equal(c.title, 'MCP Server');
  assert.equal(c.icon, icon);
  assert.equal(c.value, 'Online');
});

test('看門狗排程中時 Dashboard 加快刷新', () => {
  assert.equal(healthRefetchInterval({ mcp_server: { auto_restart_pending: true } }), 3000);
  assert.equal(healthRefetchInterval({ mcp_server: { auto_restart_pending: false } }), 30000);
  assert.equal(healthRefetchInterval(undefined), 30000);
});
