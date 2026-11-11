import test from 'node:test';
import assert from 'node:assert/strict';
import {
  apiDelete, apiFetch, apiGet, apiPatch, apiPost, createEventSource,
  DEFAULT_PROFILE_ID, getSelectedProfile, setSelectedProfile,
} from './api.js';

function storage(entries = {}) {
  const values = new Map(Object.entries(entries));
  return {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  };
}

test('authenticated management requests carry the selected profile; global routes do not', async () => {
  globalThis.localStorage = storage({ 'mcp-admin-token': 'admin-jwt' });
  globalThis.sessionStorage = storage();
  const requests = [];
  globalThis.fetch = async (url, options) => {
    requests.push({ url, ...options });
    return new Response('{}');
  };
  setSelectedProfile(DEFAULT_PROFILE_ID);
  await apiGet('/health');
  setSelectedProfile('secondary');
  await apiFetch('/settings', { headers: { 'X-MCP-Profile': 'wrong' } });
  await apiFetch('/profiles', { headers: { 'x-mcp-profile': 'wrong' } });
  await apiGet('/profiles/secondary');
  await apiGet('/auth/session');
  assert.equal(requests[0].headers['X-MCP-Profile'], 'default');
  assert.equal(requests[1].headers['X-MCP-Profile'], 'secondary');
  assert.equal(requests[1].headers.Authorization, 'Bearer admin-jwt');
  for (const request of requests.slice(2)) assert.equal(request.headers['X-MCP-Profile'], undefined);
  assert.equal(sessionStorage.getItem('mcp-admin-selected-profile'), 'secondary');
  assert.equal(getSelectedProfile(), 'secondary');
  localStorage.removeItem('mcp-admin-token');
  await apiGet('/tools');
  assert.equal(requests.at(-1).headers.Authorization, undefined);
  assert.equal(requests.at(-1).headers['X-MCP-Profile'], undefined);
  setSelectedProfile(DEFAULT_PROFILE_ID);
});

test('profile create, rename, and delete use the profile routes and expected bodies', async () => {
  globalThis.localStorage = storage({ 'mcp-admin-token': 'admin-jwt' });
  const requests = [];
  globalThis.fetch = async (url, options) => {
    requests.push({ url, ...options });
    return options.method === 'DELETE' ? new Response(null, { status: 204 }) : Response.json({ id: 'p one', label: 'Worker' });
  };
  setSelectedProfile('p one');
  const created = await apiPost('/profiles', { label: 'Worker' });
  await apiPatch('/profiles/p%20one', { label: 'Renamed' });
  await apiDelete('/profiles/p%20one');
  assert.equal(created.id, 'p one');
  assert.deepEqual(requests.map(request => [request.method, request.url]), [
    ['POST', '/api/profiles'], ['PATCH', '/api/profiles/p%20one'], ['DELETE', '/api/profiles/p%20one'],
  ]);
  assert.deepEqual(JSON.parse(requests[0].body), { label: 'Worker' });
  assert.deepEqual(JSON.parse(requests[1].body), { label: 'Renamed' });
  assert.ok(requests.every(request => request.headers['X-MCP-Profile'] === undefined));
  setSelectedProfile(DEFAULT_PROFILE_ID);
});

test('log stream uses the profile header and parses split SSE frames', async () => {
  globalThis.localStorage = storage({ 'mcp-admin-token': 'admin-jwt' });
  setSelectedProfile('worker');
  let request;
  const encoder = new TextEncoder();
  globalThis.fetch = async (url, options) => {
    request = { url, options };
    return new Response(new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode('event: connected\ndata: yes\n\nda'));
        controller.enqueue(encoder.encode('ta: {"message":"worker log"}\n\n'));
      },
    }), { headers: { 'Content-Type': 'text/event-stream' } });
  };
  const received = [];
  let resolveMessage;
  const message = new Promise(resolve => { resolveMessage = resolve; });
  const stream = createEventSource('/logs/stream');
  stream.addEventListener('connected', event => received.push(event.data));
  stream.onmessage = event => { received.push(event.data); stream.close(); resolveMessage(); };
  await message;
  assert.deepEqual(received, ['yes', '{"message":"worker log"}']);
  assert.equal(request.url, '/api/logs/stream');
  assert.equal(request.options.headers['X-MCP-Profile'], 'worker');
  assert.equal(request.options.headers.Authorization, 'Bearer admin-jwt');
  assert.equal(request.options.signal.aborted, true);
  setSelectedProfile(DEFAULT_PROFILE_ID);
});
