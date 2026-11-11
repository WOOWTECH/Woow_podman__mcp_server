import React, { useState, useEffect, useRef } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Link, Save, TestTube, Loader2, CheckCircle2, XCircle, Eye, EyeOff, KeyRound } from 'lucide-react';
import { apiGet, apiPut, apiPost } from '../api';
import { useAppConfig } from '../appConfigContext';
import { useCapabilities } from '../useCapabilities';
import { applyFeedback } from '../capabilities';
import { isConfigurationConflict } from '../configuration';
import ConfigurationStatus from '../components/ConfigurationStatus';

function StatusPill({ status }) {
  if (status === undefined) return null;
  return (
    <span
      className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium ${
        status === true
          ? 'bg-emerald-500/10 text-emerald-600 border border-emerald-500/20'
          : status === false
          ? 'bg-red-500/10 text-red-600 border border-red-500/20'
          : 'bg-field text-muted border border-border-soft'
      }`}
    >
      <span className={`w-1.5 h-1.5 rounded-full ${status === true ? 'bg-emerald-400' : status === false ? 'bg-red-400' : 'bg-muted'}`} />
      {status === true ? 'Connected' : status === false ? 'Disconnected' : 'Unknown'}
    </span>
  );
}

const inputClass =
  'w-full px-3 py-2.5 bg-panel border border-border rounded-lg text-ink placeholder-muted-2 focus:outline-none focus:border-brand-500 focus:ring-1 focus:ring-brand-500 transition-colors';

function ClearConfirmation({ field, onCancel, onConfirm }) {
  const dialog = useRef(null);
  useEffect(() => { dialog.current.showModal(); }, []);
  return <dialog ref={dialog} aria-labelledby="clear-secret-title" onCancel={onCancel}
    className="m-auto w-[calc(100%-2rem)] max-w-md rounded-[20px] border border-border bg-bg p-6 text-ink backdrop:bg-black/30">
    <h3 id="clear-secret-title" className="text-lg font-semibold">清除已儲存的{field.label}？</h3>
    <p className="mt-3 text-sm">只清除此金鑰，其他連線設定保留。服務會套用變更；需要此金鑰的後端操作將無法使用，直到你重新設定。</p>
    <div className="mt-5 flex flex-wrap gap-3">
      <button type="button" className="capability-action min-h-11" autoFocus onClick={onCancel}>取消</button>
      <button type="button" className="capability-action min-h-11" onClick={onConfirm}>確認清除</button>
    </div>
  </dialog>;
}

function Field({ field, value, masked, onChange, clearDisabled, onClear }) {
  const [show, setShow] = useState(false);
  // isSecret 泛化：type==='password' 或顯式 field.secret===true（供 textarea secret，如 kubeconfig）。
  const isSecret = field.type === 'password' || field.secret === true;
  const isPassword = field.type === 'password';
  const isTextarea = field.type === 'textarea';
  const isSelect = field.type === 'select';
  return (
    <div>
      <label className="block text-sm font-medium text-muted mb-1.5">{field.label}</label>
      <div className="relative">
        {field.type === 'url' && <Link size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted" />}
        {/* KeyRound 僅裝飾單行 secret 輸入；textarea 為多行故不加左側圖示（避免圖示浮在中央覆蓋文字）。 */}
        {isSecret && !isTextarea && field.maskedFrom && <KeyRound size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted" />}
        {isSelect ? (
          // 下拉（#1351）：field.options = [{ value, label }]；存檔值不在選項內時仍保留顯示該值。
          <select
            value={value ?? field.default ?? ''}
            onChange={(e) => onChange(e.target.value)}
            className={inputClass}
          >
            {value && !field.options.some((o) => o.value === value) && <option value={value}>{value}</option>}
            {field.options.map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
        ) : isTextarea ? (
          // textarea secret 不做 show/hide 眼睛；載入即空值、以 masked 摘要當 placeholder、留空沿用。
          <textarea
            rows={field.rows ?? 8}
            spellCheck={false}
            value={value ?? ''}
            onChange={(e) => onChange(e.target.value)}
            placeholder={masked || field.placeholder || ''}
            className={inputClass + ' font-mono text-xs resize-y'}
          />
        ) : (
          <input
            type={isPassword ? (show ? 'text' : 'password') : field.type === 'number' ? 'number' : field.type === 'url' ? 'url' : 'text'}
            value={value ?? ''}
            onChange={(e) => onChange(field.type === 'number' ? (parseInt(e.target.value) || field.default || 0) : e.target.value)}
            placeholder={masked || field.placeholder || ''}
            className={
              inputClass +
              (field.type === 'url' ? ' pl-10' : '') +
              (isSecret && field.maskedFrom ? ' pl-10' : '') +
              (isPassword ? ' pr-10' : '')
            }
          />
        )}
        {/* 眼睛僅限 type==='password'：textarea secret 不做 show/hide。 */}
        {isPassword && (
          <button type="button" onClick={() => setShow(!show)} className="absolute right-3 top-1/2 -translate-y-1/2 text-muted hover:text-ink-2">
            {show ? <EyeOff size={16} /> : <Eye size={16} />}
          </button>
        )}
      </div>
      {field.help && <p className="text-xs text-muted-2 mt-1 whitespace-pre-line">{field.help}</p>}
      {isSecret && field.maskedFrom && masked && masked !== '(not set)' && (
        <p className="text-xs text-muted-2 mt-1">Leave blank to keep the current value.</p>
      )}
      {onClear && <div className="mt-3 space-y-2">
        <p className="text-sm text-muted">保留：留白後儲存；替換：輸入新金鑰後儲存；清除：使用下方按鈕並確認。</p>
        <button type="button" className="capability-action min-h-11 disabled:opacity-50"
          disabled={clearDisabled || !masked || masked === '(not set)'} onClick={onClear}>清除已儲存的{field.label}</button>
        <p className="text-xs text-muted-2">有未儲存的變更時，請先儲存或重新載入頁面，再清除已儲存的金鑰。</p>
      </div>}
    </div>
  );
}

export default function ConnectionConfig() {
  const { connection } = useAppConfig();
  const { data: capabilities } = useCapabilities();
  const queryClient = useQueryClient();
  const [form, setForm] = useState({});
  const [testResults, setTestResults] = useState({});
  const [formRevision, setFormRevision] = useState(null);
  const [dirty, setDirty] = useState(false);
  const [conflict, setConflict] = useState(false);
  const [clearTarget, setClearTarget] = useState(null);
  const [clearNeedsReadback, setClearNeedsReadback] = useState(false);
  const clearInFlight = useRef(false);
  const edits = useRef(0);

  const { data: health } = useQuery({ queryKey: ['health'], queryFn: () => apiGet('/health'), staleTime: 60_000 });
  const { data: config, isLoading, refetch } = useQuery({ queryKey: ['config'], queryFn: () => apiGet('/config') });
  const { data: runtime } = useQuery({ queryKey: ['mcpStatus'], queryFn: () => apiGet('/settings/mcp/status'), refetchInterval: 5000 });

  useEffect(() => {
    if (!config || dirty || conflict) return;
    const init = {};
    for (const section of connection.sections) {
      for (const f of section.fields) {
        // secret 欄（password 或 field.secret===true，如 kubeconfig textarea）一律初始化為空值，留空即沿用既有值。
        init[f.name] = (f.type === 'password' || f.secret === true) ? '' : (config[f.name] ?? f.default ?? (f.type === 'number' ? 0 : ''));
      }
    }
    setForm(init);
    setFormRevision(config.configuration?.revision || null);
  }, [config, connection, dirty, conflict]);

  const saveMutation = useMutation({
    retry: false,
    mutationFn: ({ data, revision }) => apiPut(connection.saveEndpoint, data, { revision }),
    onSuccess: async (_result, submitted) => {
      await Promise.all(['config', 'health', 'capabilities', 'mcpStatus', 'tokens'].map(key =>
        queryClient.invalidateQueries({ queryKey: [key] })));
      if (edits.current === submitted.edits) { setDirty(false); setConflict(false); }
    },
    onError: error => { if (isConfigurationConflict(error)) setConflict(true); },
  });

  // Only opted-in Odoo/LiteLLM secrets use the existing shared null-patch API.
  // Ordinary saves retain product validation/normalization. No two-write
  // save/clear sequence and no automatic retry after an uncertain reply.
  const clearMutation = useMutation({
    retry: false,
    mutationFn: ({ field, revision }) => apiPut('/settings/connection', { [field.name]: null }, { revision }),
    onSuccess: async (result, submitted) => {
      const loaded = await refetch({ throwOnError: true });
      if (result.saved !== true || loaded.data?.[submitted.field.maskedFrom] !== '(not set)') {
        throw new Error('清除結果尚待確認，請重新載入已儲存設定。');
      }
      setTestResults({});
      setClearNeedsReadback(false);
      await Promise.all(['health', 'capabilities', 'mcpStatus'].map(key =>
        queryClient.invalidateQueries({ queryKey: [key] })));
    },
    onError: error => { setClearNeedsReadback(true); if (isConfigurationConflict(error)) setConflict(true); },
    onSettled: () => { clearInFlight.current = false; },
  });

  function confirmClear() {
    if (!clearTarget || clearInFlight.current || dirty || conflict || saveMutation.isPending || clearNeedsReadback) return;
    clearInFlight.current = true;
    setClearNeedsReadback(true);
    saveMutation.reset();
    clearMutation.mutate(clearTarget);
    setClearTarget(null);
  }

  function setField(name, value) {
    clearMutation.reset();
    edits.current += 1;
    setDirty(true);
    setForm((prev) => ({ ...prev, [name]: value }));
    setTestResults({});
  }

  function runTest(section) {
    // Test 一律測「已存檔的設定」（後端讀 config store，忽略 request body），
    // 故不送表單值——避免 UI 讓人誤以為測的是當前未儲存的表單。
    apiPost(section.test.endpoint)
      .then((r) => setTestResults((prev) => ({ ...prev, [section.title || '_']: { success: r.success, message: r.message || r.n8n_version || 'OK' } })))
      .catch((e) => setTestResults((prev) => ({ ...prev, [section.title || '_']: { success: false, message: e.message } })));
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="animate-spin text-muted" size={24} />
      </div>
    );
  }

  const single = connection.sections.length === 1 && !connection.sections[0].title;

  if (capabilities?.backend?.can_edit === false) return (
    <section className="capability-card">
      <h2 className="text-xl font-semibold">Fixed backend</h2>
      <p className="text-muted mt-3">The backend is managed by this deployment. Connection fields cannot be edited here.</p>
      <p className="text-muted mt-2">Backend status: {capabilities.health?.backend || 'unknown'}</p>
    </section>
  );

  return (
    <div>
      <div className="mb-6">
        <h2 className="text-xl sm:text-2xl font-bold text-ink">Connection Configuration</h2>
        <p className="text-sm text-muted mt-1">{connection.description}</p>
      </div>

      <div className="space-y-6 max-w-xl">
        <fieldset className="min-w-0" disabled={clearMutation.isPending || !!clearTarget || clearNeedsReadback}>
          <ConfigurationStatus configuration={runtime?.configuration || config?.configuration} />
        </fieldset>
        {(conflict || clearNeedsReadback && !clearMutation.isPending) && <div role="alert" className="p-4 bg-amber-50 text-amber-800 rounded-xl">
          <p>{conflict ? '其他操作已更新設定。您的草稿仍保留；請重新載入並檢查後再儲存。' : '清除結果尚待確認。請重新載入已儲存設定；不會自動重送清除。'}</p>
          <button className="capability-action mt-3" disabled={saveMutation.isPending || clearMutation.isPending} onClick={async () => {
            const loaded = await refetch();
            if (loaded.isSuccess) { edits.current += 1; setDirty(false); setConflict(false); setClearNeedsReadback(false); setTestResults({}); saveMutation.reset(); clearMutation.reset(); }
          }}>重新載入（捨棄草稿）</button>
        </div>}
        {dirty && <p role="status" className="text-sm text-muted">有尚未儲存的變更</p>}
        {connection.sections.map((section) => {
          const SectionIcon = section.icon;
          const status = section.statusKey ? health?.[section.statusKey]?.healthy : undefined;
          const tr = testResults[section.title || '_'];
          const body = (
            <fieldset className="space-y-4 min-w-0" disabled={clearMutation.isPending || !!clearTarget || clearNeedsReadback}>
              {section.fields.map((f) => (
                <Field key={f.name} field={f} value={form[f.name]} masked={f.maskedFrom ? config?.[f.maskedFrom] : undefined} onChange={(v) => setField(f.name, v)}
                  clearDisabled={dirty || conflict || saveMutation.isPending || !formRevision || capabilities?.backend?.can_edit !== true}
                  onClear={f.clearSaved === true && f.type === 'password' && f.maskedFrom ? () => setClearTarget({ field: f, revision: formRevision }) : undefined} />
              ))}
              {tr && (
                <div className={`flex items-center gap-2 px-3 py-2.5 rounded-lg text-sm ${tr.success ? 'bg-emerald-500/10 border border-emerald-500/20 text-emerald-600' : 'bg-red-500/10 border border-red-500/20 text-red-600'}`}>
                  {tr.success ? <CheckCircle2 size={16} /> : <XCircle size={16} />}
                  <span>{tr.message}</span>
                </div>
              )}
              {section.test && capabilities?.backend?.can_test !== false && (
                <div className="space-y-1.5">
                  <button
                    type="button"
                    onClick={() => runTest(section)}
                    className="flex items-center gap-2 px-4 py-2.5 bg-panel hover:bg-field text-ink-2 font-medium rounded-lg transition-colors"
                  >
                    <TestTube size={16} />
                    <span>Test Connection</span>
                  </button>
                  <p className="text-xs text-muted-2">
                    Tests the saved configuration, not unsaved form changes.
                  </p>
                </div>
              )}
            </fieldset>
          );
          return single ? (
            <div key="single" className="bg-bg border border-border rounded-xl p-6">{body}</div>
          ) : (
            <div key={section.title} className="bg-bg border border-border rounded-xl p-6">
              <div className="flex items-center justify-between mb-4">
                <div className="flex items-center gap-2">
                  {SectionIcon && <SectionIcon size={18} className="text-brand-400" />}
                  <h3 className="text-lg font-semibold text-ink">{section.title}</h3>
                </div>
                <StatusPill status={status} />
              </div>
              {body}
            </div>
          );
        })}

        {clearMutation.isSuccess && <div role="status" className="text-sm text-muted">
          已清除儲存的金鑰。{applyFeedback(clearMutation.data) === 'error' ? '子程序尚未確認套用，請查看上方設定套用狀態。' : '後端連線是否可用需另行驗證。'}
        </div>}
        {clearMutation.isError && <p role="alert" className="text-sm text-amber-800">{clearMutation.error.message}</p>}
        {saveMutation.isSuccess && (
          <div role="status" className={`flex items-center gap-2 px-3 py-2.5 rounded-lg text-sm ${applyFeedback(saveMutation.data) === 'error' ? 'text-amber-700 bg-amber-50' : 'text-muted bg-panel'}`}>
            <CheckCircle2 size={16} />
            {/* 顯示後端實際結果（restart／start／啟動失敗），#1350 */}
            <span>{saveMutation.data?.message || 'Configuration saved. MCP server will restart.'}</span>
          </div>
        )}
        {saveMutation.isError && (
          <div className="flex items-center gap-2 px-3 py-2.5 rounded-lg text-sm bg-red-500/10 border border-red-500/20 text-red-600">
            <XCircle size={16} />
            <span>{saveMutation.error.message}</span>
          </div>
        )}

        <button
          type="button"
          onClick={() => saveMutation.mutate({ data: form, revision: formRevision, edits: edits.current })}
          disabled={saveMutation.isPending || conflict || clearMutation.isPending || clearNeedsReadback || !!clearTarget}
          className="flex items-center gap-2 px-5 py-2.5 bg-brand-600 hover:bg-brand-500 disabled:bg-field disabled:text-muted text-white font-medium rounded-lg transition-colors"
        >
          {saveMutation.isPending ? <Loader2 size={16} className="animate-spin" /> : <Save size={16} />}
          <span>{connection.saveLabel || 'Save'}</span>
        </button>
      </div>
      {clearTarget && <ClearConfirmation field={clearTarget.field} onCancel={() => setClearTarget(null)} onConfirm={confirmClear} />}
    </div>
  );
}
