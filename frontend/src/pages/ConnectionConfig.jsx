import React, { useState, useEffect } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  Link,
  Save,
  TestTube,
  Loader2,
  CheckCircle2,
  XCircle,
  AlertTriangle,
  FileKey,
} from 'lucide-react';
import { apiGet, apiPut, apiPost } from '../api';

const inputClass =
  'w-full px-3 py-2.5 bg-gray-800 border border-gray-700 rounded-lg text-gray-100 placeholder-gray-600 focus:outline-none focus:border-brand-500 focus:ring-1 focus:ring-brand-500 transition-colors';

/**
 * Podman connection settings.
 *
 * Podman has no bearer credential: a local host is reached over a *unix socket*
 * and a remote one over TCP, optionally with mutual TLS whose three fields are
 * FILE PATHS on the container's filesystem, not secrets. That is why this page
 * has no password input and no mask/unmask dance — there is nothing to mask,
 * and the backend's `_NON_SECRET_KEYS` exempts the TLS paths from the
 * secret-looking-key heuristic so they can be cleared again.
 *
 * The field names match the `connection` section the backend upper-cases into
 * the MCP subprocess environment, so `podman_uri` arrives as PODMAN_URI and
 * `podman_api_version` as PODMAN_API_VERSION.
 *
 * NOTE (Phase 1): `podman_mcp_admin` does not register `routers/config.py` yet,
 * so GET /api/config answers a JSON 404 and this page renders its error banner.
 * That is expected until Phase 3; the form below is the target shape.
 */

/**
 * Reject a URI the MCP child could never use.
 *
 * The input is a plain text field and both buttons are type="button" with
 * preventDefault(), so the browser's constraint validation never runs. A
 * scheme-less "/run/podman/podman.sock" used to be saved happily and, because
 * PUT defaults to restart=true, immediately restarted the MCP child against a
 * URI httpx cannot parse.
 */
export function uriError(value) {
  const text = (value || '').trim();
  if (!text) return 'A Podman URI is required.';

  // unix:///run/podman/podman.sock — three slashes: empty authority, then an
  // absolute path. "unix://run/..." parses as host="run" and would silently
  // resolve to the wrong (or no) socket.
  if (text.startsWith('unix:')) {
    if (!text.startsWith('unix:///')) {
      return 'A unix socket URI needs three slashes and an absolute path, e.g. unix:///run/podman/podman.sock';
    }
    if (text.length <= 'unix:///'.length) return 'The URI has no socket path.';
    return null;
  }

  if (text.startsWith('tcp:') || text.startsWith('http:') || text.startsWith('https:')) {
    let parsed;
    try {
      parsed = new URL(text);
    } catch {
      return 'Not a valid URI — e.g. tcp://podman-host:2376';
    }
    if (!parsed.hostname) return 'The URI has no host.';
    return null;
  }

  return 'Unsupported scheme — use unix:// for a local socket or tcp:// for a remote host.';
}

const TLS_FIELDS = [
  { key: 'podman_tls_ca', label: 'TLS CA certificate', placeholder: '/certs/ca.pem' },
  { key: 'podman_tls_cert', label: 'TLS client certificate', placeholder: '/certs/cert.pem' },
  { key: 'podman_tls_key', label: 'TLS client key', placeholder: '/certs/key.pem' },
];

export default function ConnectionConfig() {
  const queryClient = useQueryClient();
  const [form, setForm] = useState({});
  const [testResult, setTestResult] = useState(null);

  const { data: config, isLoading, error } = useQuery({
    queryKey: ['config'],
    queryFn: () => apiGet('/config'),
  });

  useEffect(() => {
    if (!config) return;
    setForm({
      podman_uri: config.podman_uri || '',
      podman_api_version: config.podman_api_version || '',
      podman_tls_ca: config.podman_tls_ca || '',
      podman_tls_cert: config.podman_tls_cert || '',
      podman_tls_key: config.podman_tls_key || '',
    });
  }, [config]);

  const saveMutation = useMutation({
    mutationFn: (data) => apiPut('/config/connection', data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['config'] });
      queryClient.invalidateQueries({ queryKey: ['health'] });
      queryClient.invalidateQueries({ queryKey: ['mcpStatus'] });
    },
  });

  const testMutation = useMutation({
    // Probe the values on screen, not the ones on disk. Testing the saved
    // config after the operator retyped the URI produced a green banner about
    // a socket they were in the middle of replacing.
    mutationFn: (payload) => apiPost('/config/test', payload),
    onSuccess: (result) => {
      setTestResult({ success: result.success, message: result.message || 'Connected' });
    },
    onError: (err) => setTestResult({ success: false, message: err.message }),
  });

  function handleChange(field, value) {
    setForm((prev) => ({ ...prev, [field]: value }));
    setTestResult(null);
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="animate-spin text-gray-500" size={24} />
      </div>
    );
  }

  const uri = form.podman_uri;
  const uriValidationError = uriError(uri);
  const isTcp = (uri || '').trim().startsWith('tcp:');
  const payload = {
    podman_uri: (uri || '').trim(),
    podman_api_version: (form.podman_api_version || '').trim(),
    // Sent even when blank: an empty string is how the operator CLEARS a TLS
    // path. The backend exempts these keys from mask-preservation precisely so
    // that this works.
    podman_tls_ca: (form.podman_tls_ca || '').trim(),
    podman_tls_cert: (form.podman_tls_cert || '').trim(),
    podman_tls_key: (form.podman_tls_key || '').trim(),
  };
  const saveStatus = saveMutation.data?.status;

  return (
    <div>
      <div className="mb-6">
        <h2 className="text-2xl font-bold text-gray-100">Podman Connection</h2>
        <p className="text-sm text-gray-500 mt-1">
          Point the MCP server at your Podman host&apos;s libpod REST API
        </p>
      </div>

      {error && (
        <div className="mb-4 flex items-center gap-2 px-3 py-2.5 rounded-lg text-sm bg-red-500/10 border border-red-500/20 text-red-400 max-w-xl">
          <XCircle size={16} />
          <span>Could not load configuration: {error.message}</span>
        </div>
      )}

      <form className="bg-gray-900 border border-gray-800 rounded-xl p-6 max-w-xl">
        <div className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-gray-400 mb-1.5">Podman URI</label>
            <div className="relative">
              <Link size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500" />
              <input
                type="text"
                value={uri || ''}
                onChange={(e) => handleChange('podman_uri', e.target.value)}
                placeholder="unix:///run/podman/podman.sock"
                className={
                  inputClass +
                  ' pl-10 font-mono' +
                  (uri && uriValidationError ? ' border-red-500/60 focus:border-red-500' : '')
                }
              />
            </div>
            {uri && uriValidationError ? (
              <p className="text-xs text-red-400 mt-1.5">{uriValidationError}</p>
            ) : (
              <p className="text-xs text-gray-600 mt-1.5">
                <code className="text-gray-500">unix:///run/podman/podman.sock</code> for a mounted
                local socket, or <code className="text-gray-500">tcp://host:2376</code> for a remote
                host. Paths like <code className="text-gray-500">/v5.0.0/libpod/…</code> are appended
                automatically.
              </p>
            )}
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-400 mb-1.5">API version</label>
            <input
              type="text"
              value={form.podman_api_version || ''}
              onChange={(e) => handleChange('podman_api_version', e.target.value)}
              placeholder="v5.0.0"
              className={inputClass + ' font-mono'}
            />
            <p className="text-xs text-gray-600 mt-1.5">
              The libpod API version prefix. Podman rejects a version newer than the daemon with a
              404 that looks like a missing endpoint — leave blank to use the default{' '}
              <code className="text-gray-500">v5.0.0</code>.
            </p>
          </div>

          {isTcp && (
            <div className="pt-2 border-t border-gray-800 space-y-4">
              <p className="text-xs text-gray-500 pt-2">
                Mutual TLS for the remote host. These are <strong>file paths inside this
                container</strong>, not secrets — mount the certificates in and point at them here.
              </p>
              {TLS_FIELDS.map((field) => (
                <div key={field.key}>
                  <label className="block text-sm font-medium text-gray-400 mb-1.5">
                    {field.label}
                  </label>
                  <div className="relative">
                    <FileKey
                      size={16}
                      className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500"
                    />
                    <input
                      type="text"
                      value={form[field.key] || ''}
                      onChange={(e) => handleChange(field.key, e.target.value)}
                      placeholder={field.placeholder}
                      className={inputClass + ' pl-10 font-mono'}
                      autoComplete="off"
                    />
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {testResult && (
          <div
            className={`mt-4 flex items-start gap-2 px-3 py-2.5 rounded-lg text-sm ${
              testResult.success
                ? 'bg-emerald-500/10 border border-emerald-500/20 text-emerald-400'
                : 'bg-red-500/10 border border-red-500/20 text-red-400'
            }`}
          >
            {testResult.success ? (
              <CheckCircle2 size={16} className="shrink-0 mt-0.5" />
            ) : (
              <XCircle size={16} className="shrink-0 mt-0.5" />
            )}
            <span>{testResult.message}</span>
          </div>
        )}

        {/* "partial" is the backend saying: written to disk, but the MCP child
            did NOT come back up, so the new connection is not live. Rendering
            that as the same green banner as a clean save hid a dead server. */}
        {saveMutation.isSuccess && (
          <div
            className={`mt-4 flex items-start gap-2 px-3 py-2.5 rounded-lg text-sm ${
              saveStatus === 'ok'
                ? 'bg-emerald-500/10 border border-emerald-500/20 text-emerald-400'
                : 'bg-amber-500/10 border border-amber-500/20 text-amber-300'
            }`}
          >
            {saveStatus === 'ok' ? (
              <CheckCircle2 size={16} className="shrink-0 mt-0.5" />
            ) : (
              <AlertTriangle size={16} className="shrink-0 mt-0.5" />
            )}
            <span>
              {saveStatus === 'ok'
                ? 'Saved. The MCP server restarted against the new host.'
                : 'Saved to disk, but the MCP server did not restart — the new connection is NOT live yet. Restart it from Settings.'}
            </span>
          </div>
        )}

        {saveMutation.isError && (
          <div className="mt-4 flex items-start gap-2 px-3 py-2.5 rounded-lg text-sm bg-red-500/10 border border-red-500/20 text-red-400">
            <XCircle size={16} className="shrink-0 mt-0.5" />
            <span>{saveMutation.error.message}</span>
          </div>
        )}

        <div className="flex gap-3 mt-6">
          <button
            type="button"
            onClick={(e) => {
              e.preventDefault();
              testMutation.mutate(payload);
            }}
            disabled={testMutation.isPending || !!uriValidationError}
            className="flex items-center gap-2 px-4 py-2.5 bg-gray-800 hover:bg-gray-700 disabled:bg-gray-800 disabled:text-gray-600 text-gray-300 font-medium rounded-lg transition-colors"
          >
            {testMutation.isPending ? (
              <Loader2 size={16} className="animate-spin" />
            ) : (
              <TestTube size={16} />
            )}
            <span>Test Connection</span>
          </button>

          <button
            type="button"
            onClick={(e) => {
              e.preventDefault();
              saveMutation.mutate(payload);
            }}
            disabled={saveMutation.isPending || !!uriValidationError}
            className="flex items-center gap-2 px-4 py-2.5 bg-brand-600 hover:bg-brand-500 disabled:bg-gray-700 disabled:text-gray-500 text-white font-medium rounded-lg transition-colors"
          >
            {saveMutation.isPending ? (
              <Loader2 size={16} className="animate-spin" />
            ) : (
              <Save size={16} />
            )}
            <span>Save</span>
          </button>
        </div>
        <p className="text-xs text-gray-600 mt-3">
          Saving restarts the MCP server; active connector sessions will drop.
        </p>
      </form>
    </div>
  );
}
