const TOKEN_KEY = 'mcp-admin-token';
const PROFILE_KEY = 'mcp-admin-selected-profile';
export const DEFAULT_PROFILE_ID = 'default';

let selectedProfile = DEFAULT_PROFILE_ID;
try {
  selectedProfile = sessionStorage.getItem(PROFILE_KEY) || DEFAULT_PROFILE_ID;
} catch { /* Storage can be unavailable in private browsing or tests. */ }

export function getSelectedProfile() {
  return selectedProfile;
}

export function setSelectedProfile(id) {
  selectedProfile = id || DEFAULT_PROFILE_ID;
  try { sessionStorage.setItem(PROFILE_KEY, selectedProfile); } catch { /* Keep the in-memory selection. */ }
}

export function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token) {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}

function requestHeaders(path, extra = {}) {
  const token = getToken();
  const headers = { ...extra };
  const globalRoute = /^\/(auth|profiles)(\/|\?|$)/.test(path);
  if (globalRoute || !token) {
    for (const key of Object.keys(headers)) {
      if (key.toLowerCase() === 'x-mcp-profile') delete headers[key];
    }
  }
  if (token) {
    headers.Authorization = `Bearer ${token}`;
    if (!globalRoute) {
      headers['X-MCP-Profile'] = selectedProfile;
    }
  }
  return headers;
}

export async function apiFetch(path, options = {}) {
  const headers = requestHeaders(path, {
    'Content-Type': 'application/json',
    ...options.headers,
  });

  const response = await fetch(`/api${path}`, {
    ...options,
    headers,
  });

  if (response.status === 401) {
    clearToken();
    window.location.href = '/login';
    throw new Error('Unauthorized');
  }

  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const detail = body.detail && typeof body.detail === 'object' ? body.detail : body;
    const error = new Error((typeof body.detail === 'string' ? body.detail : detail.message) || `Request failed: ${response.status}`);
    error.status = response.status;
    error.code = detail.code;
    error.configuration = detail.configuration;
    error.operation = detail.operation;
    throw error;
  }

  if (response.status === 204) {
    return null;
  }

  return response.json();
}

export function apiGet(path) {
  return apiFetch(path);
}

export function apiPut(path, data, { revision, operationId } = {}) {
  return apiFetch(path, {
    method: 'PUT',
    headers: { ...(revision ? { 'If-Match': `"${revision}"` } : {}),
      ...(operationId ? { 'Idempotency-Key': operationId } : {}) },
    body: JSON.stringify(data),
  });
}

export function apiPost(path, data, { revision, operationId } = {}) {
  return apiFetch(path, {
    method: 'POST',
    headers: { ...(revision ? { 'If-Match': `"${revision}"` } : {}),
      ...(operationId ? { 'Idempotency-Key': operationId } : {}) },
    body: JSON.stringify(data),
  });
}

export function apiPatch(path, data) {
  return apiFetch(path, { method: 'PATCH', body: JSON.stringify(data) });
}

export function apiDelete(path) {
  return apiFetch(path, { method: 'DELETE' });
}

export function createEventSource(path) {
  // Native EventSource cannot set X-MCP-Profile. Fetch the SSE stream so logs
  // follow the same authenticated profile as the rest of the admin UI.
  const controller = new AbortController();
  const listeners = new Map();
  let closed = false;
  let retryTimer;
  const source = {
    onopen: null,
    onmessage: null,
    onerror: null,
    addEventListener(type, listener) {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(listener);
    },
    close() {
      closed = true;
      controller.abort();
      clearTimeout(retryTimer);
    },
  };

  function dispatch(type, data) {
    const event = { type, data };
    if (type === 'message') source.onmessage?.(event);
    for (const listener of listeners.get(type) || []) listener(event);
  }

  async function connect() {
    if (closed) return;
    try {
      const response = await fetch(`/api${path}`, {
        headers: requestHeaders(path, { Accept: 'text/event-stream' }),
        signal: controller.signal,
        cache: 'no-store',
      });
      if (closed) {
        await response.body?.cancel();
        return;
      }
      if (response.status === 401) {
        closed = true;
        clearToken();
        window.location.href = '/login';
        return;
      }
      if (!response.ok || !response.body) throw new Error(`Log stream failed: ${response.status}`);
      source.onopen?.({ type: 'open' });
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let pending = '';
      let eventType = 'message';
      let data = [];
      function lineReceived(line) {
        if (line.endsWith('\r')) line = line.slice(0, -1);
        if (!line) {
          if (data.length) dispatch(eventType, data.join('\n'));
          eventType = 'message';
          data = [];
          return;
        }
        if (line.startsWith(':')) return;
        const colon = line.indexOf(':');
        const field = colon < 0 ? line : line.slice(0, colon);
        let value = colon < 0 ? '' : line.slice(colon + 1);
        if (value.startsWith(' ')) value = value.slice(1);
        if (field === 'event') eventType = value;
        if (field === 'data') data.push(value);
      }
      while (!closed) {
        const { value, done } = await reader.read();
        if (done) break;
        pending += decoder.decode(value, { stream: true });
        const lines = pending.split('\n');
        pending = lines.pop();
        lines.forEach(lineReceived);
      }
      if (!closed) throw new Error('Log stream disconnected');
    } catch (error) {
      if (closed) return;
      source.onerror?.(error);
      retryTimer = setTimeout(connect, 3000);
    }
  }

  connect();
  return source;
}
