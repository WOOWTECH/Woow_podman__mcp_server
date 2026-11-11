import test from 'node:test';
import assert from 'node:assert/strict';
import { apiPut, apiPost } from './api.js';
import { configurationMessage, isConfigurationConflict, canRetryConfiguration } from './configuration.js';

test('conditional save carries the supplied editor snapshot and preserves structured conflict', async () => {
  globalThis.localStorage = { getItem: () => 'fixture-admin' };
  let request;
  globalThis.fetch = async (url, options) => {
    request = { url, options };
    return new Response(JSON.stringify({ detail: { code: 'config_conflict', message: '請重新載入', configuration: { revision: 'newer' } } }), { status: 409 });
  };
  await assert.rejects(apiPut('/config/connection', { url: 'draft' }, { revision: 'editor-snapshot' }), error => {
    assert.equal(error.message, '請重新載入');
    assert.equal(error.configuration.revision, 'newer');
    assert.equal(isConfigurationConflict(error), true);
    return true;
  });
  assert.equal(request.options.headers['If-Match'], '"editor-snapshot"');
  assert.deepEqual(JSON.parse(request.options.body), { url: 'draft' });
});

test('retry applies a saved revision without replaying form data', async () => {
  let request;
  globalThis.fetch = async (url, options) => { request = { url, options }; return new Response('{}'); };
  await apiPost('/settings/mcp/apply', undefined, { revision: 'saved-snapshot' });
  assert.equal(request.options.body, undefined);
  assert.equal(request.options.headers['If-Match'], '"saved-snapshot"');
  assert.equal(canRetryConfiguration({ revision: 'saved-snapshot', apply_state: 'failed' }), true);
  assert.equal(canRetryConfiguration({ revision: 'saved-snapshot', apply_state: 'applied' }), false);
});

test('persistent labels distinguish child state without promising backend access', () => {
  assert.match(configurationMessage({ apply_state: 'failed' }), /已儲存.*失敗/);
  assert.match(configurationMessage({ apply_state: 'pending' }), /尚未確認/);
  assert.match(configurationMessage({ apply_state: 'applied' }), /目前的 MCP 子程序/);
  assert.match(configurationMessage({ apply_state: 'unknown' }), /尚未確認/);
});

test('connection save refused by the SSRF guard surfaces the readable message (n8n F2)', async () => {
  globalThis.localStorage = { getItem: () => 'fixture-admin' };
  const message = 'n8n_api_url blocked by SSRF protection: Private IP addresses not allowed. (n8n_api_url 被 SSRF 防護擋下，未存檔)';
  globalThis.fetch = async () => new Response(JSON.stringify({
    detail: { code: 'ssrf_blocked', field: 'n8n_api_url', reason: 'Private IP addresses not allowed', message },
  }), { status: 400 });
  // ConnectionConfig.jsx 的存檔錯誤區塊顯示 saveMutation.error.message
  await assert.rejects(apiPut('/config/connection', { n8n_api_url: 'http://10.0.0.5:5678' }), error => {
    assert.equal(error.status, 400);
    assert.equal(error.code, 'ssrf_blocked');
    assert.equal(error.message, message);
    assert.equal(isConfigurationConflict(error), false);
    return true;
  });
});
