import React, { useState, useEffect, useRef } from 'react';
import { isConfigurationConflict } from '../configuration';
import ConfigurationStatus from '../components/ConfigurationStatus';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  Settings,
  Save,
  Loader2,
  CheckCircle2,
  XCircle,
  RotateCw,
  Plus,
  Trash2,
  Eye,
  EyeOff,
  Play,
  Square,
  Server,
  Shield,
  Network,
} from 'lucide-react';
import { apiGet, apiPut, apiPost, DEFAULT_PROFILE_ID, getSelectedProfile } from '../api';
import useDurableOperation from '../useDurableOperation';
import OperationRecovery from '../components/OperationRecovery';
import { useAppConfig } from '../appConfigContext';
import { envUpdate, applyFeedback } from '../capabilities';
import { ownerDefaultsMessage } from '../ownerDefaults';

// 開啟判定對齊子行程端（例如 odoo 的 fix_direct_writes）的 strip().lower() in ('1','true','yes')。
const isEnvOn = (v) => ['1', 'true', 'yes'].includes(String(v ?? '').trim().toLowerCase());

function SectionCard({ title, icon: Icon, children }) {
  return (
    <div className="bg-bg border border-border rounded-xl p-6">
      <div className="flex items-center gap-2 mb-4">
        <Icon size={18} className="text-brand-400" />
        <h3 className="text-lg font-semibold text-ink">{title}</h3>
      </div>
      {children}
    </div>
  );
}

function StatusBadge({ running }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium ${
        running
          ? 'bg-emerald-500/10 text-emerald-600 border border-emerald-500/20'
          : 'bg-field text-muted border border-border-soft'
      }`}
    >
      <span className={`w-1.5 h-1.5 rounded-full ${running ? 'bg-emerald-400' : 'bg-muted'}`} />
      {running ? 'Running' : 'Stopped'}
    </span>
  );
}

function Alert({ type, message }) {
  const styles = {
    success: 'bg-emerald-500/10 border-emerald-500/20 text-emerald-600',
    error: 'bg-red-500/10 border-red-500/20 text-red-600',
  };
  return (
    <div className={`flex items-center gap-2 px-3 py-2.5 rounded-lg text-sm border ${styles[type]}`}>
      {type === 'success' ? <CheckCircle2 size={16} /> : <XCircle size={16} />}
      <span>{message}</span>
    </div>
  );
}

const inputClass =
  'w-full px-3 py-2.5 bg-panel border border-border rounded-lg text-ink placeholder-muted-2 focus:outline-none focus:border-brand-500 focus:ring-1 focus:ring-brand-500 transition-colors font-mono text-sm';

export default function SettingsPage() {
  const queryClient = useQueryClient();
  const isDefaultProfile = getSelectedProfile() === DEFAULT_PROFILE_ID;
  // 各 app 於 appConfig 宣告的 env 開關（如 odoo 的 Allow Direct Writes）；未宣告者為空陣列 → 不顯示、不影響行為。
  const { settingsEnvToggles = [], settingsExtra: SettingsExtra } = useAppConfig();
  const toggleKeys = new Set(settingsEnvToggles.map((t) => t.env));
  const [mcpForm, setMcpFormState] = useState({ command: '', args: [], port: 8000, env: {} });
  const [proxyForm, setProxyFormState] = useState({ timeout: 86400, bearer_token: '' });
  const [mcpDirty, setMcpDirty] = useState(false);
  const [proxyDirty, setProxyDirty] = useState(false);
  const [mcpRevision, setMcpRevision] = useState(null);
  const [proxyRevision, setProxyRevision] = useState(null);
  const [conflict, setConflict] = useState(false);
  const mcpEdits = useRef(0), proxyEdits = useRef(0);
  function setMcpForm(value) { mcpEdits.current += 1; setMcpDirty(true); setMcpFormState(value); }
  function setProxyForm(value) { proxyEdits.current += 1; setProxyDirty(true); setProxyFormState(value); }
  const [passwordRevision, setPasswordRevision] = useState(null);
  const [passwordForm, setPasswordForm] = useState({ current: '', new_password: '' });
  const [newEnvKey, setNewEnvKey] = useState('');
  const [newEnvVal, setNewEnvVal] = useState('');
  const [showBearerToken, setShowBearerToken] = useState(false);
  const [bearerTokenEdited, setBearerTokenEdited] = useState(false);
  const [feedback, setFeedback] = useState(null);

  const { data: settings, isLoading, refetch } = useQuery({
    queryKey: ['settings'],
    queryFn: () => apiGet('/settings'),
  });

  // #1530：子行程 command／args／port 由部署與 app 決定，只顯示不給改；env 只能改後端宣告的鍵
  // （editable_env_keys；null＝app 未宣告政策，沿用自由輸入但後端仍擋危險變數）。
  const editableEnvKeys = settings?.editable_env_keys ?? null;
  const freeFormEnv = editableEnvKeys === null;

  const { data: mcpStatus } = useQuery({
    queryKey: ['mcpStatus'],
    queryFn: () => apiGet('/settings/mcp/status'),
    refetchInterval: 5000,
  });

  const restartOperation = useDurableOperation('/settings/mcp/restart', mcpStatus?.configuration);

  useEffect(() => {
    if (settings && !conflict) {
      if (!mcpDirty) {
        const mcp = settings.mcp_server || {};
        setMcpFormState({
          command: mcp.command || '',
          args: mcp.args || [],
          port: mcp.port || 8000,
          env: mcp.env || {},
        });
        setMcpRevision(settings.configuration?.revision || null);
      }
      if (!proxyDirty) {
        const proxy = settings.proxy || {};
        setProxyFormState({
          timeout: proxy.timeout || 86400,
          // Never copy the masked API value into an editable input. A blank secret
          // is omitted by the backend on save, preserving the stored credential.
          bearer_token: '',
        });
        setProxyRevision(settings.configuration?.revision || null);
        setShowBearerToken(false);
        setBearerTokenEdited(false);
      }
    }
  }, [settings, mcpDirty, proxyDirty, conflict]);

  // --- Mutations ---

  const saveMcpMutation = useMutation({
    retry: false,
    mutationFn: ({ data, revision }) => apiPut('/settings/mcp_server', data, { revision }),
    onSuccess: async (res, submitted) => {
      await Promise.all(['settings', 'mcpStatus', 'capabilities'].map(key => queryClient.invalidateQueries({ queryKey: [key] })));
      if (mcpEdits.current === submitted.edits) setMcpDirty(false);
      setFeedback({ type: applyFeedback(res), message: res.message || 'MCP server config saved' });
    },
    onError: (err) => { if (isConfigurationConflict(err)) setConflict(true); setFeedback({ type: 'error', message: err.message }); },
  });

  const saveProxyMutation = useMutation({
    retry: false,
    mutationFn: ({ data, revision }) => apiPut('/settings/proxy', data, { revision }),
    onSuccess: async (res, submitted) => {
      await queryClient.invalidateQueries({ queryKey: ['settings'] });
      if (proxyEdits.current === submitted.edits) { setProxyDirty(false); setShowBearerToken(false); setBearerTokenEdited(false); }
      setFeedback({ type: 'success', message: res.message || 'Proxy config saved' });
    },
    onError: (err) => { if (isConfigurationConflict(err)) setConflict(true); setFeedback({ type: 'error', message: err.message }); },
  });

  async function toggleBearerToken() {
    if (showBearerToken) {
      setShowBearerToken(false);
      if (!bearerTokenEdited) setProxyFormState((p) => ({ ...p, bearer_token: '' }));
      return;
    }
    if (bearerTokenEdited) {
      setShowBearerToken(true);
      return;
    }
    try {
      const result = await apiPost('/settings/proxy/bearer_token/reveal');
      setProxyFormState((p) => ({ ...p, bearer_token: result.value || '' }));
      setShowBearerToken(true);
    } catch (err) {
      setFeedback({ type: 'error', message: err.message });
    }
  }

  const restartMutation = useMutation({
    retry: false,
    mutationFn: () => restartOperation.run(mcpStatus?.configuration?.revision),
    onSuccess: (res) => {
      queryClient.invalidateQueries({ queryKey: ['mcpStatus'] });
      queryClient.invalidateQueries({ queryKey: ['capabilities'] });
      // A completed receipt records the outcome; it does not imply success.
      const restarted = res?.status === 'ok' && applyFeedback(res) === 'success';
      setFeedback({ type: restarted ? 'success' : 'error', message: res?.message || (restarted
        ? 'MCP server restarted' : 'Failed to restart MCP server — check server status and logs') });
    },
    onError: (err) => { if (isConfigurationConflict(err)) setConflict(true); setFeedback({ type: 'error', message: err.message }); },
  });

  const passwordMutation = useMutation({
    retry: false,
    mutationFn: (data) => apiPut('/settings/admin_password', data, { revision: passwordRevision }),
    onSuccess: () => {
      setPasswordForm({ current: '', new_password: '' }); setPasswordRevision(null);
      setFeedback({ type: 'success', message: 'Admin password updated' });
    },
    onError: (err) => { if (isConfigurationConflict(err)) setConflict(true); setFeedback({ type: 'error', message: err.message }); },
  });

  // --- Env var helpers ---

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

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="animate-spin text-muted" size={24} />
      </div>
    );
  }

  return (
    <div>
      <div className="mb-6">
        <h2 className="text-xl sm:text-2xl font-bold text-ink">Settings</h2>
        <p className="text-sm text-muted mt-1">
          MCP server process, proxy, and admin configuration
        </p>
      </div>

      {feedback && (
        <div className="mb-4">
          <Alert type={feedback.type} message={feedback.message} />
        </div>
      )}

      <div className="space-y-6 max-w-2xl">
        <ConfigurationStatus configuration={mcpStatus?.configuration || settings?.configuration} />
        {SettingsExtra && <SettingsExtra />}
        {conflict && <div role="alert" className="p-4 bg-amber-50 text-amber-800 rounded-xl">
          <p>設定已由其他操作更新。草稿仍保留，請重新載入並檢查後再儲存。</p>
          <button className="capability-action mt-3" disabled={saveMcpMutation.isPending || saveProxyMutation.isPending} onClick={async () => {
            const loaded = await refetch();
            if (loaded.isSuccess) { mcpEdits.current += 1; proxyEdits.current += 1; setMcpDirty(false); setProxyDirty(false); setPasswordForm({ current: '', new_password: '' }); setPasswordRevision(null); setConflict(false); setFeedback(null); }
          }}>重新載入設定（捨棄草稿）</button>
        </div>}
        {(mcpDirty || proxyDirty) && <p role="status" className="text-sm text-muted">有尚未儲存的變更</p>}
        <OperationRecovery operation={restartOperation} onReload={async () => { await refetch(); await queryClient.invalidateQueries({ queryKey: ['mcpStatus'] }); }} />
        {/* MCP Server Process */}
        <SectionCard title="MCP Server Process" icon={Server}>
          <div className="flex items-center justify-between mb-4">
            <StatusBadge running={mcpStatus?.running} />
            <div className="flex gap-2">
              <button
                onClick={() => restartMutation.mutate()}
                disabled={restartMutation.isPending}
                className="flex items-center gap-1.5 px-3 py-1.5 bg-panel hover:bg-field text-ink-2 text-sm rounded-lg transition-colors"
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

          {mcpStatus?.running && (
            <div className="text-xs text-muted mb-4 font-mono">
              PID: {mcpStatus.pid} | Restarts: {mcpStatus.restart_count} | Auto-restarts: {mcpStatus.auto_restart_count ?? 0}
            </div>
          )}
          {/* #1350：子行程已 exit 時顯示 exit code 與最後幾行輸出（例如缺連線設定的錯誤），
              不讓租戶只看到「Stopped」。 */}
          {mcpStatus && !mcpStatus.running && (mcpStatus.exit_code != null || mcpStatus.last_output?.length > 0) && (
            <div className="mb-4">
              {mcpStatus.exit_code != null && (
                <div className="text-xs text-muted font-mono mb-1">Exit code: {mcpStatus.exit_code}</div>
              )}
              {mcpStatus.auto_restart_pending && (
                <div className="text-xs text-muted font-mono mb-1">
                  Watchdog: auto-restart in {Math.ceil(mcpStatus.next_auto_restart_in ?? 0)}s (auto-restarts so far: {mcpStatus.auto_restart_count ?? 0})
                </div>
              )}
              {mcpStatus.last_output?.length > 0 && (
                <pre className="text-xs font-mono bg-panel border border-border-soft rounded-lg p-3 overflow-x-auto whitespace-pre-wrap break-all text-ink-2 max-h-48">
                  {mcpStatus.last_output.join('\n')}
                </pre>
              )}
            </div>
          )}

          <div className="space-y-4">
            {/* #1530：唯讀顯示。子行程指令由部署與 app 決定，管理頁改不了（改了等於在容器裡執行任意程式）。 */}
            <div>
              <label className="block text-sm font-medium text-muted mb-1.5">Command (read-only)</label>
              <code className="block px-3 py-2.5 bg-panel border border-border rounded-lg text-sm text-ink-2 break-all">
                {[mcpForm.command, ...mcpForm.args].join(' ') || '—'}
              </code>
              <p className="text-xs text-muted-2 mt-1">
                Listening on 127.0.0.1:{mcpForm.port}. The command is fixed by the deployment and cannot be changed here.
              </p>
            </div>

            {/* 負責人 2026-10-25「預設全開」：後端 owner_defaults 標記（新安裝、部署 seed（PaaS 新服務）、既有設定
                一次性遷移，或既有設定本來就全開只寫標記；文字見 ownerDefaults.js）。之後在這裡或 Tools 頁設的限制會保留。 */}
            {settings?.owner_defaults && (
              <p className="text-xs text-muted bg-panel border border-border-soft rounded-lg px-3 py-2">
                {ownerDefaultsMessage(settings.owner_defaults)}
              </p>
            )}

            {/* App 宣告的 env 開關（appConfig.settingsEnvToggles）；語法糖：寫入 mcp_server.env 後
                按 Save MCP Config 會自動重啟子行程生效。被接管的 key 會從下方泛用清單隱藏，避免兩處重複編輯。 */}
            {settingsEnvToggles.map((t) => {
              const on = isEnvOn(mcpForm.env?.[t.env]);
              return (
                <div
                  key={t.env}
                  className="flex items-center justify-between gap-4 p-4 bg-panel border border-border rounded-lg"
                >
                  <div>
                    <label className="block text-sm font-medium text-ink">{t.label}</label>
                    {t.description && <p className="text-xs text-muted mt-0.5">{t.description}</p>}
                    <code className="text-[11px] text-muted-2">{t.env}</code>
                  </div>
                  <button
                    type="button"
                    role="switch"
                    aria-checked={on}
                    onClick={() =>
                      setMcpForm((p) => ({ ...p, env: { ...p.env, [t.env]: on ? '0' : '1' } }))
                    }
                    className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors ${
                      on ? 'bg-brand-500' : 'bg-field'
                    }`}
                  >
                    <span
                      className={`pointer-events-none inline-block h-5 w-5 rounded-full bg-white shadow transform transition-transform ${
                        on ? 'translate-x-5' : 'translate-x-0'
                      }`}
                    />
                  </button>
                </div>
              );
            })}

            {/* Env vars：只列可編輯的鍵（後端宣告），部署 seed 的鍵不顯示、不能新增任意變數 */}
            {(freeFormEnv || editableEnvKeys.some((k) => !toggleKeys.has(k))) && (
            <div>
              <label className="block text-sm font-medium text-muted mb-1.5">
                Environment Variables
              </label>
              <div className="space-y-1.5">
                {(freeFormEnv
                  ? Object.entries(mcpForm.env)
                  : editableEnvKeys.map((k) => [k, mcpForm.env?.[k] ?? '']))
                  .filter(([key]) => !toggleKeys.has(key))
                  .map(([key, val]) => (
                  <div key={key} className="flex items-center gap-2">
                    <code className="px-2 py-1.5 bg-panel border border-border rounded text-xs text-brand-400 min-w-[140px]">
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
                    {freeFormEnv && (
                    <button
                      onClick={() => removeEnvVar(key)}
                      className="p-1 text-muted hover:text-red-600 transition-colors"
                    >
                      <Trash2 size={14} />
                    </button>
                    )}
                  </div>
                ))}
                {freeFormEnv && (
                <div className="flex gap-2">
                  <input
                    type="text"
                    value={newEnvKey}
                    onChange={(e) => setNewEnvKey(e.target.value)}
                    placeholder="KEY"
                    className={inputClass + ' w-[140px]'}
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
                    className="p-2.5 bg-panel hover:bg-field disabled:text-muted-2 text-ink-2 rounded-lg transition-colors"
                  >
                    <Plus size={16} />
                  </button>
                </div>
                )}
              </div>
            </div>
            )}

            {(freeFormEnv || editableEnvKeys.length > 0) && (
              <button
                onClick={() => saveMcpMutation.mutate({ data: envUpdate(settings?.mcp_server?.env, mcpForm.env), revision: mcpRevision, edits: mcpEdits.current })}
                disabled={saveMcpMutation.isPending || conflict}
                className="flex items-center gap-2 px-4 py-2.5 bg-brand-600 hover:bg-brand-500 disabled:bg-field text-white font-medium rounded-lg transition-colors"
              >
                {saveMcpMutation.isPending ? <Loader2 size={16} className="animate-spin" /> : <Save size={16} />}
                Save MCP Config
              </button>
            )}
          </div>
        </SectionCard>

        {/* Proxy Config */}
        <SectionCard title="MCP Proxy" icon={Network}>
          <div className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-muted mb-1.5">
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
              <p className="text-xs text-muted-2 mt-1">
                Default 86400s (24h) for long-running MCP calls
              </p>
            </div>

            <div>
              <label className="block text-sm font-medium text-muted mb-1.5">
                Bearer Token (optional, for n8n proxy)
              </label>
              <div className="relative">
                <input
                  type={showBearerToken ? 'text' : 'password'}
                  value={proxyForm.bearer_token}
                  onChange={(e) => {
                    setBearerTokenEdited(true);
                    setProxyForm((p) => ({ ...p, bearer_token: e.target.value }));
                  }}
                  placeholder={settings?.proxy?.bearer_token_masked ? 'Stored token; leave blank to keep' : 'Leave empty if not needed'}
                  className={inputClass + ' pr-10'}
                />
                <button
                  type="button"
                  onClick={toggleBearerToken}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-muted hover:text-ink-2"
                >
                  {showBearerToken ? <EyeOff size={16} /> : <Eye size={16} />}
                </button>
              </div>
            </div>

            <button
              onClick={() => saveProxyMutation.mutate({ data: proxyForm, revision: proxyRevision, edits: proxyEdits.current })}
              disabled={saveProxyMutation.isPending || conflict}
              className="flex items-center gap-2 px-4 py-2.5 bg-brand-600 hover:bg-brand-500 disabled:bg-field text-white font-medium rounded-lg transition-colors"
            >
              {saveProxyMutation.isPending ? <Loader2 size={16} className="animate-spin" /> : <Save size={16} />}
              Save Proxy Config
            </button>
          </div>
        </SectionCard>

        {/* The management password authenticates all profiles. */}
        {isDefaultProfile && <SectionCard title="Global Admin Password" icon={Shield}>
          <div className="space-y-4">
            <p className="text-sm text-muted">
              This management password is shared across all profiles.
            </p>
            <p className="text-sm text-muted">
              Current: <code className="text-muted">{settings?.admin_password_masked}</code>
            </p>
            <div>
              <label className="block text-sm font-medium text-muted mb-1.5">New Password</label>
              <input
                type="password"
                value={passwordForm.new_password}
                onChange={(e) =>
                  { if (passwordRevision === null) setPasswordRevision(settings?.configuration?.revision); setPasswordForm((p) => ({ ...p, new_password: e.target.value })); }
                }
                placeholder="Minimum 4 characters"
                className={inputClass + ' max-w-xs'}
              />
            </div>
            <button
              onClick={() => passwordMutation.mutate({ value: passwordForm.new_password })}
              disabled={conflict || passwordMutation.isPending || !passwordRevision || passwordForm.new_password.length < 4}
              className="flex items-center gap-2 px-4 py-2.5 bg-panel hover:bg-field disabled:bg-panel disabled:text-muted-2 text-ink-2 font-medium rounded-lg transition-colors"
            >
              {passwordMutation.isPending ? <Loader2 size={16} className="animate-spin" /> : <Save size={16} />}
              Update Password
            </button>
          </div>
        </SectionCard>}
      </div>
    </div>
  );
}
