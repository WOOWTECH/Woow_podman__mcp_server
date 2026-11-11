import test from 'node:test';
import assert from 'node:assert/strict';
import { newOperationId, operationStorageKey } from './useDurableOperation.js';
import { apiPost } from './api.js';

test('operation IDs use server snapshot plus monotonic elapsed time, not client wall time', () => {
  const original = Date.now;
  Date.now = () => 1999999999999;
  try {
    assert.match(newOperationId(1793328000000, 120.75), /^1793328000120\.[A-Za-z0-9_-]{16,64}$/);
    assert.match(newOperationId(1793328000000, -10), /^1793328000000\./);
    assert.throws(() => newOperationId(undefined), /伺服器時間/);
  } finally { Date.now = original; }
});

test('durable operation receipts are isolated by profile and retain the default legacy key', () => {
  const path = '/tokens/rotate';
  assert.equal(operationStorageKey(path, 'default'), 'mcp-admin-operation:/tokens/rotate');
  assert.equal(operationStorageKey(path, 'secondary'), 'mcp-admin-operation:secondary:/tokens/rotate');
  assert.notEqual(operationStorageKey(path, 'secondary'), operationStorageKey(path, 'third'));
});

test('explicit operation key/revision are preserved and unknown outcome remains structured', async () => {
  globalThis.localStorage = { getItem: () => 'synthetic-admin' };
  const id = '1793328000000.1234567890abcdef';
  let sent;
  globalThis.fetch = async (_, request) => {
    sent = request;
    return new Response(JSON.stringify({ detail: { code: 'operation_outcome_unknown',
      operation: { id, kind: 'mcp.restart', state: 'unknown' } } }), { status: 409 });
  };
  await assert.rejects(apiPost('/settings/mcp/restart', undefined, { revision: 'original', operationId: id }), error => {
    assert.equal(error.code, 'operation_outcome_unknown');
    assert.equal(error.operation.id, id);
    assert.equal(error.operation.state, 'unknown');
    return true;
  });
  assert.equal(sent.headers['If-Match'], '"original"');
  assert.equal(sent.headers['Idempotency-Key'], id);
  assert.equal(sent.body, undefined);
});
