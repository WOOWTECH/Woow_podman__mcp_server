const TOKEN_KEY = 'mcp-admin-token';

export function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token) {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}

export async function apiFetch(path, options = {}) {
  const token = getToken();
  const headers = {
    'Content-Type': 'application/json',
    ...options.headers,
  };

  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }

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

export function apiDelete(path) {
  return apiFetch(path, { method: 'DELETE' });
}

export function createEventSource(path) {
  const token = getToken();
  const url = token ? `/api${path}?token=${encodeURIComponent(token)}` : `/api${path}`;
  return new EventSource(url);
}
