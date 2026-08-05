import React, { useState, useEffect, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  Save,
  Loader2,
  CheckCircle2,
  XCircle,
  AlertTriangle,
  RotateCw,
  Plus,
  Trash2,
  Eye,
  EyeOff,
  Server,
  Shield,
  Network,
} from 'lucide-react';
import { apiGet, apiPut, apiPost, clearToken } from '../api';

function SectionCard({ title, icon: Icon, children }) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
      <div className="flex items-center gap-2 mb-4">
        <Icon size={18} className="text-brand-400" />
        <h3 className="text-lg font-semibold text-gray-100">{title}</h3>
      </div>
      {children}
    </div>
  );
}

function StatusBadge({ status }) {
  // `running` is process liveness; `ready` is "accepting connections on the
  // port". They differ for the 10-14s the FastMCP child needs to bind after a
  // restart, and during that window the connector answers 502 — so a single
  // green "Running" pill was actively misleading right after every save.
  const running = !!status?.running;
  const ready = !!status?.ready;
  const label = !running ? 'Stopped' : ready ? 'Running' : 'Starting…';
  const tone = !running
    ? 'bg-gray-700/50 text-gray-400 border-gray-600'
    : ready
      ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20'
      : 'bg-amber-500/10 text-amber-400 border-amber-500/20';
  const dot = !running ? 'bg-gray-500' : ready ? 'bg-emerald-400' : 'bg-amber-400';
  return (
    <span
      className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium border ${tone}`}
    >
      <span className={`w-1.5 h-1.5 rounded-full ${dot}`} />
      {label}
    </span>
  );
}

function Alert({ type, message }) {
  const styles = {
    success: 'bg-emerald-500/10 border-emerald-500/20 text-emerald-400',
    warning: 'bg-amber-500/10 border-amber-500/20 text-amber-300',
    error: 'bg-red-500/10 border-red-500/20 text-red-400',
  };
  const Icon = type === 'success' ? CheckCircle2 : type === 'warning' ? AlertTriangle : XCircle;
  return (
    <div
      className={`flex items-start gap-2 px-3 py-2.5 rounded-lg text-sm border ${styles[type] || styles.error}`}
    >
      <Icon size={16} className="shrink-0 mt-0.5" />
      <span>{message}</span>
    </div>
  );
}

/** Inline "are you sure" strip — the destructive buttons all restart the child. */
function ConfirmBar({ message, onConfirm, onCancel, pending }) {
  return (
    <div className="flex items-start gap-2 px-3 py-2.5 rounded-lg text-sm border bg-amber-500/10 border-amber-500/20 text-amber-300">
      <AlertTriangle size={16} className="shrink-0 mt-0.5" />
      <div className="flex-1">
        <p>{message}</p>
        <div className="flex gap-2 mt-2">
          <button
            onClick={onConfirm}
            disabled={pending}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-amber-600 hover:bg-amber-500 disabled:bg-gray-700 disabled:text-gray-500 text-white text-xs font-medium rounded-lg transition-colors"
          >
            {pending && <Loader2 size={12} className="animate-spin" />}
            <span>Yes, restart it</span>
          </button>
          <button
            onClick={onCancel}
            className="px-3 py-1.5 bg-gray-800 hover:bg-gray-700 text-gray-400 text-xs font-medium rounded-lg transition-colors"
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}

const inputClass =
  'w-full px-3 py-2.5 bg-gray-800 border border-gray-700 rounded-lg text-gray-100 placeholder-gray-600 focus:outline-none focus:border-brand-500 focus:ring-1 focus:ring-brand-500 transition-colors font-mono text-sm';

// The backend rejects anything shorter; keep the two ends in step so the
// operator is told before the round trip, not by a 400.
const MIN_PASSWORD_LEN = 8;

/** The --port value sitting in argv, if any. */
function argPort(args) {
  const list = Array.isArray(args) ? args.map(String) : [];
  for (let i = 0; i < list.length; i += 1) {
    if (list[i] === '--port' && i + 1 < list.length) return list[i + 1];
    if (list[i].startsWith('--port=')) return list[i].slice('--port='.length);
  }
  return null;
}

export default function SettingsPage() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [mcpForm, setMcpForm] = useState({ command: '', args: [], port: 3000, env: {} });
  const [proxyForm, setProxyForm] = useState({ timeout: 86400, bearer_token: '' });
  const [bearerTouched, setBearerTouched] = useState(false);
  const [passwordForm, setPasswordForm] = useState({ current: '', new_password: '' });
  const [newEnvKey, setNewEnvKey] = useState('');
  const [newEnvVal, setNewEnvVal] = useState('');
  const [newArg, setNewArg] = useState('');
  const [showBearerToken, setShowBearerToken] = useState(false);
  const [feedback, setFeedback] = useState(null);
  const [confirming, setConfirming] = useState(null); // 'save' | 'restart' | null

  const { data: settings, isLoading } = useQuery({
    queryKey: ['settings'],
    queryFn: () => apiGet('/settings'),
  });

  const { data: mcpStatus } = useQuery({
    queryKey: ['mcpStatus'],
    queryFn: () => apiGet('/settings/mcp/status'),
    refetchInterval: 5000,
  });

  useEffect(() => {
    if (settings) {
      const mcp = settings.mcp_server || {};
      setMcpForm({
        command: mcp.command || '',
        args: mcp.args || [],
        port: mcp.port || 3000,
        env: mcp.env || {},
      });
      const proxy = settings.proxy || {};
      setProxyForm({
        timeout: proxy.timeout || 86400,
        // Arrives MASKED. It is only ever sent back when the operator retypes
        // it (see bearerTouched) so the mask cannot overwrite the real token.
        bearer_token: proxy.bearer_token || '',
      });
      setBearerTouched(false);
    }
  }, [settings]);

  const saveMcpMutation = useMutation({
    mutationFn: (data) => apiPut('/settings/mcp_server', data),
    onSuccess: (res) => {
      queryClient.invalidateQueries({ queryKey: ['settings'] });
      queryClient.invalidateQueries({ queryKey: ['mcpStatus'] });
      queryClient.invalidateQueries({ queryKey: ['health'] });
      setConfirming(null);
      // "partial" means saved but the child is not accepting connections yet —
      // the connector is answering 502 right now, so do not call that success.
      setFeedback({
        type: res.status === 'ok' ? 'success' : 'warning',
        message: res.message || 'MCP server config saved',
      });
    },
    onError: (err) => {
      setConfirming(null);
      setFeedback({ type: 'error', message: err.message });
    },
  });

  const saveProxyMutation = useMutation({
    mutationFn: (data) => apiPut('/settings/proxy', data),
    onSuccess: (res) => {
      queryClient.invalidateQueries({ queryKey: ['settings'] });
      setFeedback({
        type: res.status === 'ok' || !res.status ? 'success' : 'warning',
        message: res.message || 'Proxy config saved',
      });
    },
    onError: (err) => setFeedback({ type: 'error', message: err.message }),
  });

  const restartMutation = useMutation({
    mutationFn: () => apiPost('/settings/mcp/restart'),
    onSuccess: (res) => {
      queryClient.invalidateQueries({ queryKey: ['mcpStatus'] });
      queryClient.invalidateQueries({ queryKey: ['health'] });
      setConfirming(null);
      setFeedback({
        type: res.status === 'ok' ? 'success' : res.status === 'error' ? 'error' : 'warning',
        message: res.message || 'MCP server restarted',
      });
    },
    onError: (err) => {
      setConfirming(null);
      setFeedback({ type: 'error', message: err.message });
    },
  });

  const passwordMutation = useMutation({
    mutationFn: (data) => apiPut('/settings/admin_password', data),
    onSuccess: () => {
      setPasswordForm({ current: '', new_password: '' });
      setFeedback({
        type: 'success',
        message: 'Admin password updated. All sessions were revoked — signing you out…',
      });
      // The backend revokes every issued JWT (and the httpOnly cookie), so the
      // token still in localStorage is dead. Staying on the page would 401 on
      // the next poll; go to /login deliberately instead.
      clearToken();
      setTimeout(() => navigate('/login'), 1200);
    },
    onError: (err) => setFeedback({ type: 'error', message: err.message }),
  });

  function addEnvVar() {
    if (newEnvKey.trim()) {
      setMcpForm((prev) => ({
        ...prev,
        env: { ...prev.env, [newEnvKey.trim()]: newEnvVal },
      }));
      setNewEnvKey('');
      setNewEnvVal('');
    }
  }

  function removeEnvVar(key) {
    setMcpForm((prev) => {
      const env = { ...prev.env };
      delete env[key];
      return { ...prev, env };
    });
  }

  function addArg() {
    if (newArg.trim()) {
      setMcpForm((prev) => ({ ...prev, args: [...prev.args, newArg.trim()] }));
      setNewArg('');
    }
  }

  function removeArg(index) {
    setMcpForm((prev) => ({
      ...prev,
      args: prev.args.filter((_, i) => i !== index),
    }));
  }

  // config.json carries the port twice: as `mcp_server.port` (which the proxy
  // dials, and which the launcher now injects into argv) and as a literal
  // --port in `args`. Surface the disagreement rather than letting the
  // operator believe the argv value is what the child will bind.
  const argvPort = useMemo(() => argPort(mcpForm.args), [mcpForm.args]);
  const portMismatch = argvPort != null && String(mcpForm.port) !== argvPort;

  const proxyPayload = bearerTouched
    ? { timeout: proxyForm.timeout, bearer_token: proxyForm.bearer_token }
    : { timeout: proxyForm.timeout };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="animate-spin text-gray-500" size={24} />
      </div>
    );
  }

  return (
    <div>
      <div className="mb-6">
        <h2 className="text-2xl font-bold text-gray-100">Settings</h2>
        <p className="text-sm text-gray-500 mt-1">
          MCP server process, proxy, and admin configuration
        </p>
      </div>

      {feedback && (
        <div className="mb-4">
          <Alert type={feedback.type} message={feedback.message} />
        </div>
      )}

      <div className="space-y-6 max-w-2xl">
        <SectionCard title="MCP Server Process" icon={Server}>
          <div className="flex items-center justify-between mb-4">
            <StatusBadge status={mcpStatus} />
            <div className="flex gap-2">
              <button
                onClick={() => setConfirming('restart')}
                disabled={restartMutation.isPending || confirming === 'restart'}
                className="flex items-center gap-1.5 px-3 py-1.5 bg-gray-800 hover:bg-gray-700 disabled:text-gray-600 text-gray-300 text-sm rounded-lg transition-colors"
              >
                {restartMutation.isPending ? (
                  <Loader2 size={14} className="animate-spin" />
                ) : (
                  <RotateCw size={14} />
                )}
                Restart
              </button>
            </div>
          </div>

          {confirming === 'restart' && (
            <div className="mb-4">
              <ConfirmBar
                message="Restarting the MCP server drops every in-flight connector session (including claude.ai) and the child needs ~10-15s to accept connections again."
                pending={restartMutation.isPending}
                onConfirm={() => restartMutation.mutate()}
                onCancel={() => setConfirming(null)}
              />
            </div>
          )}

          {mcpStatus?.running && (
            <div className="text-xs text-gray-500 mb-4 font-mono">
              PID: {mcpStatus.pid} | Restarts: {mcpStatus.restart_count}
              {mcpStatus.state ? ` | ${mcpStatus.state}` : ''}
            </div>
          )}

          <div className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-gray-400 mb-1.5">Command</label>
              <input
                type="text"
                value={mcpForm.command}
                onChange={(e) => setMcpForm((p) => ({ ...p, command: e.target.value }))}
                placeholder="python"
                className={inputClass}
              />
              <p className="text-xs text-gray-600 mt-1">
                e.g. <code className="text-gray-500">python</code> with args{' '}
                <code className="text-gray-500">-m woow_podman_mcp_server.server --transport streamable-http</code>
              </p>
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-400 mb-1.5">Port</label>
              <input
                type="number"
                value={mcpForm.port}
                onChange={(e) => setMcpForm((p) => ({ ...p, port: parseInt(e.target.value) || 3000 }))}
                className={inputClass + ' max-w-[120px]'}
              />
              <p className="text-xs text-gray-600 mt-1">
                Loopback port (127.0.0.1) the child binds and the proxy forwards to. This value
                is authoritative: it overrides any <code className="text-gray-500">--port</code>{' '}
                in Arguments when the process is launched.
              </p>
              {portMismatch && (
                <p className="text-xs text-amber-400 mt-1">
                  Arguments still say <code>--port {argvPort}</code>. Saving launches the child on{' '}
                  {mcpForm.port} regardless — remove the stale argument to avoid confusion.
                </p>
              )}
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-400 mb-1.5">Arguments</label>
              <div className="space-y-1.5">
                {mcpForm.args.map((arg, i) => (
                  <div key={i} className="flex items-center gap-2">
                    <code className="flex-1 px-3 py-1.5 bg-gray-800 border border-gray-700 rounded text-sm text-gray-300">
                      {arg}
                    </code>
                    <button
                      onClick={() => removeArg(i)}
                      className="p-1 text-gray-500 hover:text-red-400 transition-colors"
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                ))}
                <div className="flex gap-2">
                  <input
                    type="text"
                    value={newArg}
                    onChange={(e) => setNewArg(e.target.value)}
                    onKeyDown={(e) => e.key === 'Enter' && (e.preventDefault(), addArg())}
                    placeholder="--flag value"
                    className={inputClass + ' flex-1'}
                  />
                  <button
                    onClick={addArg}
                    disabled={!newArg.trim()}
                    className="p-2.5 bg-gray-800 hover:bg-gray-700 disabled:text-gray-600 text-gray-300 rounded-lg transition-colors"
                  >
                    <Plus size={16} />
                  </button>
                </div>
              </div>
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-400 mb-1.5">
                Environment Variables
              </label>
              <div className="space-y-1.5">
                {Object.entries(mcpForm.env).map(([key, val]) => (
                  <div key={key} className="flex items-center gap-2">
                    <code className="px-2 py-1.5 bg-gray-800 border border-gray-700 rounded text-xs text-brand-400 min-w-[140px]">
                      {key}
                    </code>
                    <input
                      type="text"
                      value={val}
                      onChange={(e) =>
                        setMcpForm((p) => ({
                          ...p,
                          env: { ...p.env, [key]: e.target.value },
                        }))
                      }
                      className={inputClass + ' flex-1'}
                    />
                    <button
                      onClick={() => removeEnvVar(key)}
                      className="p-1 text-gray-500 hover:text-red-400 transition-colors"
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                ))}
                <div className="flex gap-2">
                  <input
                    type="text"
                    value={newEnvKey}
                    onChange={(e) => setNewEnvKey(e.target.value)}
                    placeholder="PODMAN_MCP_PROFILE"
                    className={inputClass + ' w-[220px]'}
                  />
                  <input
                    type="text"
                    value={newEnvVal}
                    onChange={(e) => setNewEnvVal(e.target.value)}
                    onKeyDown={(e) => e.key === 'Enter' && (e.preventDefault(), addEnvVar())}
                    placeholder="value"
                    className={inputClass + ' flex-1'}
                  />
                  <button
                    onClick={addEnvVar}
                    disabled={!newEnvKey.trim()}
                    className="p-2.5 bg-gray-800 hover:bg-gray-700 disabled:text-gray-600 text-gray-300 rounded-lg transition-colors"
                  >
                    <Plus size={16} />
                  </button>
                </div>
              </div>
              {/* Secret-looking variables (…KEY/…TOKEN/…PASSWORD/…SECRET) are
                  returned masked. Leaving one untouched saves it unchanged;
                  editing it replaces the real value. */}
              <p className="text-xs text-gray-600 mt-1.5">
                Values of key/token/password/secret variables are shown masked. Leave one as-is to
                keep the stored value; type over it to replace it.
              </p>
            </div>

            {confirming === 'save' && (
              <ConfirmBar
                message="Saving restarts the MCP server: in-flight connector sessions are dropped and the child needs ~10-15s to accept connections again."
                pending={saveMcpMutation.isPending}
                onConfirm={() => saveMcpMutation.mutate(mcpForm)}
                onCancel={() => setConfirming(null)}
              />
            )}

            <button
              onClick={() => setConfirming('save')}
              disabled={saveMcpMutation.isPending || confirming === 'save'}
              className="flex items-center gap-2 px-4 py-2.5 bg-brand-600 hover:bg-brand-500 disabled:bg-gray-700 text-white font-medium rounded-lg transition-colors"
            >
              {saveMcpMutation.isPending ? <Loader2 size={16} className="animate-spin" /> : <Save size={16} />}
              Save MCP Config
            </button>
            <p className="text-xs text-gray-600">
              Saving this section restarts the MCP server process.
            </p>
          </div>
        </SectionCard>

        <SectionCard title="MCP Proxy" icon={Network}>
          <div className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-gray-400 mb-1.5">
                Proxy Timeout (seconds)
              </label>
              <input
                type="number"
                value={proxyForm.timeout}
                onChange={(e) =>
                  setProxyForm((p) => ({ ...p, timeout: parseInt(e.target.value) || 86400 }))
                }
                className={inputClass + ' max-w-[160px]'}
              />
              <p className="text-xs text-gray-600 mt-1">
                Default 86400s (24h) for long-running streamed MCP calls
              </p>
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-400 mb-1.5">
                Upstream Bearer Token (optional)
              </label>
              <div className="relative">
                <input
                  type={showBearerToken ? 'text' : 'password'}
                  value={proxyForm.bearer_token}
                  onChange={(e) => {
                    setBearerTouched(true);
                    setProxyForm((p) => ({ ...p, bearer_token: e.target.value }));
                  }}
                  placeholder="Leave empty unless the child enforces StaticTokenVerifier"
                  className={inputClass + ' pr-10'}
                />
                <button
                  type="button"
                  onClick={() => setShowBearerToken(!showBearerToken)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-500 hover:text-gray-300"
                >
                  {showBearerToken ? <EyeOff size={16} /> : <Eye size={16} />}
                </button>
              </div>
              <p className="text-xs text-gray-600 mt-1">
                Injected as <code className="text-gray-500">Authorization: Bearer</code> when the proxy
                forwards to the loopback MCP child. Shown masked; it is only written back when you
                edit it.
              </p>
            </div>

            <button
              onClick={() => saveProxyMutation.mutate(proxyPayload)}
              disabled={saveProxyMutation.isPending}
              className="flex items-center gap-2 px-4 py-2.5 bg-brand-600 hover:bg-brand-500 disabled:bg-gray-700 text-white font-medium rounded-lg transition-colors"
            >
              {saveProxyMutation.isPending ? <Loader2 size={16} className="animate-spin" /> : <Save size={16} />}
              Save Proxy Config
            </button>
          </div>
        </SectionCard>

        <SectionCard title="Admin Password" icon={Shield}>
          <div className="space-y-4">
            <p className="text-sm text-gray-500">
              Current:{' '}
              <code className="text-gray-400">
                {settings?.admin_password_configured
                  ? settings?.admin_password_masked
                  : '(not set)'}
              </code>
            </p>
            {/* The backend requires proof of the current password: without it,
                anything holding the 12h admin JWT (an XSS reading localStorage,
                a leaked HAR) could lock the real operator out. */}
            {settings?.admin_password_configured && (
              <div>
                <label className="block text-sm font-medium text-gray-400 mb-1.5">
                  Current Password
                </label>
                <input
                  type="password"
                  value={passwordForm.current}
                  onChange={(e) => setPasswordForm((p) => ({ ...p, current: e.target.value }))}
                  placeholder="Required"
                  autoComplete="current-password"
                  className={inputClass + ' max-w-xs'}
                />
              </div>
            )}
            <div>
              <label className="block text-sm font-medium text-gray-400 mb-1.5">New Password</label>
              <input
                type="password"
                value={passwordForm.new_password}
                onChange={(e) =>
                  setPasswordForm((p) => ({ ...p, new_password: e.target.value }))
                }
                placeholder={`Minimum ${MIN_PASSWORD_LEN} characters`}
                autoComplete="new-password"
                className={inputClass + ' max-w-xs'}
              />
            </div>
            <p className="text-xs text-amber-400/80">
              Changing the password revokes every active session, including this one — you will be
              signed out and must log in again.
            </p>
            <button
              onClick={() =>
                passwordMutation.mutate({
                  current: passwordForm.current,
                  value: passwordForm.new_password,
                })
              }
              disabled={
                passwordMutation.isPending ||
                passwordForm.new_password.length < MIN_PASSWORD_LEN ||
                (settings?.admin_password_configured && !passwordForm.current)
              }
              className="flex items-center gap-2 px-4 py-2.5 bg-gray-800 hover:bg-gray-700 disabled:bg-gray-800 disabled:text-gray-600 text-gray-300 font-medium rounded-lg transition-colors"
            >
              {passwordMutation.isPending ? <Loader2 size={16} className="animate-spin" /> : <Save size={16} />}
              Update Password
            </button>
          </div>
        </SectionCard>
      </div>
    </div>
  );
}
