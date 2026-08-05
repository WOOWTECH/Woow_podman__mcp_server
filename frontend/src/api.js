const TOKEN_KEY = 'mcp-admin-token';

/**
 * Decode the `exp` claim of a JWT without verifying it.
 *
 * Presence of *any* string in localStorage used to be enough to render the
 * whole app shell, so a returning user with a 12h-expired token got the full
 * console and was only bounced to /login once the first API call 401'd (and a
 * stored literal "undefined" produced an endless /login -> / -> /login loop).
 * Returns null when the value is not a decodable JWT.
 */
function jwtExpiry(token) {
  if (typeof token !== 'string') return null;
  const parts = token.split('.');
  if (parts.length !== 3) return null;
  try {
    const base64 = parts[1].replace(/-/g, '+').replace(/_/g, '/');
    const payload = JSON.parse(atob(base64 + '==='.slice((base64.length + 3) % 4)));
    return typeof payload.exp === 'number' ? payload.exp * 1000 : null;
  } catch {
    return null;
  }
}

export function getToken() {
  const token = localStorage.getItem(TOKEN_KEY);
  // "undefined"/"null" are what a bad login response used to store; they are
  // truthy strings and would keep ProtectedRoute admitting the user forever.
  if (!token || token === 'undefined' || token === 'null') return null;
  const expiresAt = jwtExpiry(token);
  if (expiresAt !== null && expiresAt <= Date.now()) {
    localStorage.removeItem(TOKEN_KEY);
    return null;
  }
  return token;
}

export function setToken(token) {
  // Guard against `data.token || data.access_token` both being undefined: the
  // literal string "undefined" would otherwise be persisted as a valid session.
  if (typeof token !== 'string' || !token) {
    throw new Error('Login response did not contain a token');
  }
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}

/**
 * End the session on BOTH ends.
 *
 * `clearToken()` alone only drops the copy in localStorage. The login endpoint
 * also sets an httpOnly `mcp-admin-token` cookie, which JS cannot delete and
 * which AuthMiddleware accepts on its own — so a "logout" that only touched
 * localStorage showed a fresh login screen while the browser still held a live
 * 24h admin session (good for /api/tokens, /api/settings, the log stream…).
 * `POST /api/auth/logout` is the half only the server can do: it expires the
 * cookie and bumps the session epoch, which also kills any JWT already copied
 * out of localStorage.
 *
 * Deliberately never rejects: a logout must succeed locally even if the
 * network call does not, or a user faced with a dead backend can never sign
 * out at all.
 */
export async function logout() {
  try {
    await fetch('/api/auth/logout', {
      method: 'POST',
      headers: authHeaders(),
      credentials: 'same-origin',
    });
  } catch {
    // Offline or backend down — still clear what we can reach.
  } finally {
    clearToken();
  }
}

/** Authorization header for the current token, or {} when there is none. */
function authHeaders() {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/**
 * Turn a FastAPI error body into one readable line.
 *
 * FastAPI returns `detail` as an ARRAY of {loc, msg, type} objects for 422
 * responses. `new Error(body.detail)` stringified that array to the literal
 * "[object Object]", which is all an operator ever saw for a malformed
 * payload. Every page renders `error.message` verbatim, so the flattening has
 * to happen here, once.
 */
export function formatApiError(body, fallback) {
  const detail = body && body.detail;

  if (Array.isArray(detail)) {
    const lines = detail
      .map((item) => {
        if (typeof item === 'string') return item;
        if (!item || typeof item !== 'object') return String(item);
        // Drop the leading "body"/"query" segment — the operator cares about
        // the field name, not FastAPI's request-part prefix.
        const loc = Array.isArray(item.loc) ? item.loc.filter((p) => p !== 'body') : [];
        const where = loc.join('.');
        const msg = item.msg || item.type || 'invalid value';
        return where ? `${where}: ${msg}` : String(msg);
      })
      .filter(Boolean);
    if (lines.length) return lines.join('; ');
  }

  if (typeof detail === 'string' && detail) return detail;
  if (detail && typeof detail === 'object') {
    // Never hand a raw object to a component that renders it as a React child.
    try {
      return JSON.stringify(detail);
    } catch {
      return fallback;
    }
  }
  if (body && typeof body.message === 'string' && body.message) return body.message;
  return fallback;
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
    // Send the httpOnly `mcp-admin-token` cookie the login endpoint sets; it is
    // what the SSE stream authenticates with, so it must stay warm.
    credentials: 'same-origin',
  });

  if (response.status === 401) {
    clearToken();
    window.location.href = '/login';
    throw new Error('Unauthorized');
  }

  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(formatApiError(body, `Request failed: ${response.status}`));
  }

  if (response.status === 204) {
    return null;
  }

  return response.json();
}

export function apiGet(path) {
  return apiFetch(path);
}

export function apiPut(path, data) {
  return apiFetch(path, {
    method: 'PUT',
    body: JSON.stringify(data),
  });
}

export function apiPost(path, data) {
  // `JSON.stringify(undefined)` is `undefined`, i.e. no body at all. Keeping
  // that explicit stops a caller that means "no payload" from being confused
  // with one that forgot to pass the form.
  return apiFetch(path, {
    method: 'POST',
    ...(data === undefined ? {} : { body: JSON.stringify(data) }),
  });
}

export function apiDelete(path) {
  return apiFetch(path, { method: 'DELETE' });
}

/** Build `?a=1&b=2`, skipping empty values. */
export function queryString(params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value === undefined || value === null || value === '') continue;
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : '';
}

/**
 * Open an SSE stream against the admin API.
 *
 * The JWT is deliberately NOT put in the query string: uvicorn's access logger
 * records the full request line, so every Log page visit used to write a
 * still-valid 12h admin token into container stdout (and the browser history).
 * `/api/auth/login` sets an httpOnly `mcp-admin-token` cookie and
 * AuthMiddleware accepts it, and the stream is same-origin, so the cookie
 * authenticates the connection with nothing to leak.
 */
export function createEventSource(path) {
  return new EventSource(`/api${path}`, { withCredentials: true });
}
