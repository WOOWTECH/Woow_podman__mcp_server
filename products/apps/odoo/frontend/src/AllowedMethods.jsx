import React, { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { apiGet, apiPut } from '../../../../packages/mcp-admin-ui/src/api';

const endpoint = '/odoo/allowed-methods';
const exactMethod = /^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\.[A-Za-z][A-Za-z0-9_]*$/;

export default function AllowedMethods() {
  const cache = useQueryClient();
  const { data, error, refetch } = useQuery({ queryKey: ['odooAllowedMethods'], queryFn: () => apiGet(endpoint) });
  const [draft, setDraft] = useState('');
  const [revision, setRevision] = useState(null);
  const [dirty, setDirty] = useState(false);
  const [pending, setPending] = useState(false);
  const [conflict, setConflict] = useState(false);
  const [feedback, setFeedback] = useState('');
  useEffect(() => {
    if (data && !dirty && !pending && !conflict) {
      setDraft(data.odoo_allowed_methods.join('\n'));
      setRevision(data.configuration?.revision || null);
    }
  }, [data, dirty, pending, conflict]);
  const methods = draft === '' ? [] : draft.split('\n');
  const valid = methods.length <= 128 && new Set(methods).size === methods.length
    && methods.every(item => item.length <= 256 && exactMethod.exec(item)?.[0] === item)
    && JSON.stringify(methods).length <= 16384;

  async function save(event) {
    event.preventDefault();
    if (!valid || !revision || pending || conflict) return;
    setPending(true);
    setFeedback('');
    try {
      const result = await apiPut(endpoint, { odoo_allowed_methods: methods }, { revision });
      setFeedback(result.message);
      await Promise.all(['odooAllowedMethods', 'settings', 'mcpStatus', 'capabilities'].map(key =>
        cache.invalidateQueries({ queryKey: [key] })));
      setDirty(false);
    } catch (err) {
      setFeedback(err.message);
      if (err.status === 409 || err.status === 428) setConflict(true);
    } finally {
      setPending(false);
    }
  }

  async function reload() {
    const loaded = await refetch();
    if (loaded.isSuccess) { setDirty(false); setConflict(false); setFeedback(''); }
  }

  return <section className="bg-bg border border-border rounded-[20px] p-6 space-y-4" aria-labelledby="odoo-methods-heading">
    <h3 id="odoo-methods-heading" className="text-lg font-semibold text-ink">Reviewed Odoo methods</h3>
    <p className="text-muted leading-relaxed">你審查過的完整 model.method，每行一項。例如 sale.order.action_confirm。
      最多 128 項、每項 256 字元、總計 16 KiB；不能重複、使用萬用字元、空白或底線開頭的私有方法。清空後儲存會移除這份清單。</p>
    <p className="text-muted leading-relaxed">Allow Unreviewed Methods 關閉時，這份清單允許審過的精確方法；
      該開關與 Allow Direct Writes 都開啟時，清單不會限制其他方法。
      清單不授予工具權限，也不繞過寫入總閘門、chatter 限制或 create／write／unlink 的直接寫入限制，Odoo 帳號權限仍適用。</p>
    <p className="text-muted leading-relaxed">儲存後會重新啟動 MCP 子程序。部署另設的政策檔項目仍會加入允許清單，此表單不編輯政策檔。</p>
    {data?.odoo_allowed_methods_source === 'container_environment' && <p className="text-muted">
      目前顯示容器環境提供的清單。儲存後使用你在這裡設定的清單；儲存空清單也會覆寫容器環境值。</p>}
    {error && <p role="alert">無法載入方法清單：{error.message}</p>}
    <form onSubmit={save} className="space-y-4">
      <label htmlFor="odoo-methods" className="block text-ink">Allowed model.method entries</label>
      <textarea id="odoo-methods" rows={6} value={draft} disabled={!data || pending}
        aria-invalid={!valid} aria-describedby={!valid ? 'odoo-methods-invalid' : undefined}
        onChange={event => { setDraft(event.target.value); setDirty(true); }}
        className="w-full min-w-0 p-4 bg-panel border border-border rounded-[20px] text-ink font-mono focus:outline-none focus:ring-2 focus:ring-brand-500" />
      {!valid && <p id="odoo-methods-invalid" role="alert">請檢查方法名稱、重複項目與上限；每行必須是完整的公開 model.method。</p>}
      {conflict && <p role="alert">設定已由其他操作更新。草稿仍保留，請重新載入並檢查後再儲存。</p>}
      {feedback && <p role="status">{feedback}</p>}
      <div className="flex flex-wrap gap-3">
        <button type="submit" disabled={!data || !revision || !dirty || !valid || pending || conflict}
          className="min-h-11 px-5 py-2 rounded-full bg-brand-500 text-white disabled:opacity-50 focus:ring-2 focus:ring-brand-500">
          {pending ? '儲存中…' : 'Save reviewed methods'}</button>
        <button type="button" disabled={pending} onClick={reload}
          className="min-h-11 px-5 py-2 rounded-full bg-panel border border-border text-ink focus:ring-2 focus:ring-brand-500">
          重新載入（捨棄草稿）</button>
      </div>
    </form>
  </section>;
}
