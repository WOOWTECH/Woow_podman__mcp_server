import { useRef, useState } from 'react';
import { apiGet, apiPost, DEFAULT_PROFILE_ID, getSelectedProfile } from './api.js';

export function newOperationId(serverTime, elapsed = 0) {
  if (!Number.isSafeInteger(serverTime) || serverTime <= 0) throw new Error('請重新載入以取得伺服器時間。');
  return `${Math.floor(serverTime + Math.max(0, elapsed))}.${crypto.randomUUID()}`;
}

export function operationStorageKey(path, profileId = getSelectedProfile()) {
  // Preserve outstanding receipts from before profiles were introduced.
  return profileId === DEFAULT_PROFILE_ID
    ? `mcp-admin-operation:${path}`
    : `mcp-admin-operation:${encodeURIComponent(profileId)}:${path}`;
}

// Only public intent metadata lives in sessionStorage. No request body or secret.
// A retry keeps the original revision/key, even after a page or admin restart.
export default function useDurableOperation(path, configuration) {
  const clock = useRef(null);
  if (Number.isSafeInteger(configuration?.server_time_ms) && clock.current?.configuration !== configuration) {
    clock.current = { configuration, observed: performance.now() };
  }
  const storageKey = operationStorageKey(path);
  const intent = useRef(undefined);
  const [receipt, setReceipt] = useState(null);
  if (intent.current === undefined) {
    try {
      const saved = JSON.parse(sessionStorage.getItem(storageKey));
      intent.current = saved && typeof saved.operationId === 'string' && typeof saved.revision === 'string' ? saved : null;
    } catch { intent.current = null; }
  }
  function clear() {
    intent.current = null;
    sessionStorage.removeItem(storageKey);
    setReceipt(null);
  }
  async function run(revision) {
    if (!intent.current) {
      if (!revision) throw new Error('請先重新載入管理設定。');
      intent.current = { operationId: newOperationId(clock.current?.configuration.server_time_ms, performance.now() - (clock.current?.observed ?? performance.now())), revision };
      sessionStorage.setItem(storageKey, JSON.stringify(intent.current));
    }
    try {
      const result = await apiPost(path, undefined, intent.current);
      clear();
      const { token, ...metadata } = result;
      return metadata;
    } catch (error) {
      setReceipt(error.operation || { id: intent.current.operationId, state: 'unconfirmed' });
      throw error;
    }
  }
  async function query() {
    if (!intent.current) return null;
    try {
      const result = await apiGet(`/operations/${encodeURIComponent(intent.current.operationId)}`);
      setReceipt(result.operation);
      return result;
    } catch (error) {
      setReceipt({ id: intent.current.operationId, state: error.code || 'unconfirmed' });
      return null;
    }
  }
  return { run, query, clear, receipt, hasIntent: Boolean(intent.current) };
}
