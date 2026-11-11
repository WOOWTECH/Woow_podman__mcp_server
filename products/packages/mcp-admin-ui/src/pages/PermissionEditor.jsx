import React, { useEffect, useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { apiGet, apiPut } from '../api';
import { applyFeedback } from '../capabilities';
import { isConfigurationConflict } from '../configuration';
import ConditionalWrites from '../components/ConditionalWrites';

// Only reviewed selector metadata grants editing controls. Schema enums and
// catalog operation labels by themselves do not establish an enforced policy.
export function editableOperationTools(data) {
  return (data?.tools || []).filter(tool => tool.operation_policy_supported === true
    && Array.isArray(tool.operations) && tool.operations.length > 0
    && tool.operations.every(operation => typeof operation === 'string'));
}

export function editableSavedOperations(data) {
  // The API uses the same reviewed normalization as enforcement. Raw saved
  // labels remain available separately for audit and older server fallback.
  if (data?.editable_disabled_operations) return { ...data.editable_disabled_operations };
  const result = { ...(data?.disabled_operations || {}) };
  for (const tool of editableOperationTools(data)) {
    if (!result[tool.name]) continue;
    result[tool.name] = [...new Set(result[tool.name].flatMap(operation =>
      tool.operation_legacy_denies?.[operation] || [operation]))].sort();
  }
  return result;
}

export function toggleOperation(saved, name, operation, allowed) {
  const denied = new Set(saved[name] || []);
  if (allowed) denied.delete(operation);
  else denied.add(operation);
  const result = { ...saved };
  if (denied.size) result[name] = [...denied].sort();
  else delete result[name];
  return result;
}

export default function PermissionEditor() {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState({});
  const [dirty, setDirty] = useState(false);
  const [revision, setRevision] = useState(null);
  const [conflict, setConflict] = useState(false);
  const [search, setSearch] = useState('');
  const query = useQuery({ queryKey: ['/tools'], queryFn: () => apiGet('/tools') });
  const tools = editableOperationTools(query.data);
  const saved = editableSavedOperations(query.data);

  useEffect(() => {
    if (query.data && !dirty && !conflict) {
      setDraft(editableSavedOperations(query.data));
      setRevision(query.data.configuration?.revision || null);
    }
  }, [query.data, dirty, conflict]);

  const mutation = useMutation({
    retry: false,
    mutationFn: disabled_operations => apiPut('/tools/operations', { disabled_operations }, { revision }),
    onSuccess: async () => {
      // Refresh saved state before clearing dirty, so a stale query cannot
      // briefly restore the previous rules after a successful save.
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['/tools'] }),
        queryClient.invalidateQueries({ queryKey: ['capabilities'] }),
        queryClient.invalidateQueries({ queryKey: ['config'] }),
      ]);
      setDirty(false);
    },
    onError: error => { if (isConfigurationConflict(error)) setConflict(true); },
  });

  if (query.isLoading) return <p role="status" className="text-muted">Loading operation permissions…</p>;
  if (query.isError) return <section className="capability-card">
    <h2 className="text-xl font-semibold">Operation permissions</h2>
    <p role="alert" className="text-red-600 mt-3">{query.error.message}</p>
    <button className="capability-action mt-3" onClick={() => query.refetch()}>Retry</button>
  </section>;

  const retained = Object.fromEntries(Object.entries(draft).flatMap(([name, denied]) => {
    const known = tools.find(tool => tool.name === name)?.operations || [];
    const unknown = denied.filter(operation => !known.includes(operation));
    return unknown.length ? [[name, unknown]] : [];
  }));
  const filtered = tools.filter(tool => `${tool.name} ${tool.operations.join(' ')}`.toLowerCase().includes(search.toLowerCase()));

  return <div className="space-y-6">
    <section className="capability-card">
      <h2 className="text-xl sm:text-2xl font-semibold">Operation permissions</h2>
      <p className="text-muted mt-3">Choose which operations each tool may use. Saved restrictions apply to new calls immediately. Child settings and backend permissions may further limit access.</p>
      {!tools.length && <p className="text-muted mt-3">This product has no reviewed operation controls. You can disable individual tools on the Tools page.</p>}
      {!!tools.length && <>
        <p className="text-muted mt-3">{tools.length} tools with supported operation controls. Existing rules are loaded below.</p>
        <div className="flex flex-wrap items-center gap-3 mt-4">
          <button className="capability-action" disabled={!dirty || mutation.isPending || query.isFetching || conflict}
            onClick={() => mutation.mutate(draft)}>{mutation.isPending ? 'Saving…' : 'Save permissions'}</button>
          <button className="capability-action" disabled={!dirty || mutation.isPending || conflict}
            onClick={() => { setDraft(saved); setDirty(false); mutation.reset(); }}>Reset</button>
          {dirty && <span role="status" className="text-muted">Unsaved changes</span>}
        </div>
      </>}
      {mutation.isError && <p role="alert" className="text-red-600 mt-3">{mutation.error.message}</p>}
      {conflict && <button className="capability-action mt-3" onClick={async () => {
        const loaded = await query.refetch();
        if (loaded.isSuccess) { setDirty(false); setConflict(false); mutation.reset(); }
      }}>重新載入權限（捨棄草稿）</button>}
      {mutation.isSuccess && <p role="status" className={`mt-3 ${applyFeedback(mutation.data) === 'error' ? 'text-amber-700' : 'text-muted'}`}>
        {mutation.data?.message || 'Permissions saved. Refresh availability to verify child access.'}
      </p>}
    </section>

    {!!Object.keys(retained).length && <section className="capability-card">
      <h3 className="font-semibold">Retained saved restrictions</h3>
      <p className="text-muted mt-3">These saved names have no current editable metadata. They remain enforced and are preserved when you save other operations.</p>
      <pre aria-label="Retained saved restrictions" className="mt-3 overflow-auto max-w-full text-sm">{JSON.stringify(retained, null, 2)}</pre>
    </section>}

    {!!tools.length && <input type="search" aria-label="Search operation controls" value={search}
      onChange={event => setSearch(event.target.value)} placeholder="Search tools or operations…"
      className="w-full px-4 py-3 bg-bg border border-border rounded-xl text-ink-2" />}

    {filtered.map(tool => <section key={tool.name} className="capability-card" aria-label={`${tool.name} operations`}>
      <h3 className="font-semibold break-all">{tool.name}</h3>
      <p className="text-muted mt-3 break-words">{tool.description}</p>
      {/* 產品覆核過的說明（/api/tools 的 operation_note），例如 Odoo：這裡的限制擋不住 execute_method 的直接寫入。 */}
      {tool.operation_note && <p role="note" aria-label={`${tool.name} 說明`}
        className="mt-3 text-amber-700 break-words">{tool.operation_note}</p>}
      <ConditionalWrites tool={tool} />
      {tool.enabled === false && <p className="text-muted mt-3">The whole tool is disabled. These operation settings will also apply when you enable it on the Tools page.</p>}
      <p className="text-muted mt-3 break-all">
        {tool.operation_selector === 'fixed' ? 'This tool has one fixed operation.'
          : tool.operation_selector === 'optional_string' ? `${tool.operation_parameter}: omitted or null creates a file; an etag replaces it.`
          : `Selector: ${tool.operation_parameter}${tool.operation_default ? ` · Default: ${tool.operation_default}` : ' · Required'}`}
      </p>
      <div className="mt-4 space-y-2">
        {tool.operations.map(operation => {
          const denied = draft[tool.name] || [];
          const dependent = (tool.operation_dependencies?.[operation] || []).filter(name => denied.includes(name));
          const flag = Object.entries(tool.operation_flags || {}).find(([, value]) => value === operation)?.[0];
          return <label key={operation} className="flex items-center gap-3 min-h-11 py-2 break-all">
            <input type="checkbox" checked={!denied.includes(operation)} disabled={mutation.isPending}
              aria-label={`Allow ${tool.name}: ${operation}`}
              onChange={event => {
                setDraft(current => toggleOperation(current, tool.name, operation, event.target.checked));
                setDirty(true);
                mutation.reset();
              }} />
            <span>{operation}{flag ? ` · 條件寫入（${flag}=true）` : ''}{dependent.length ? ` · Blocked by: ${dependent.join(', ')}` : ''}</span>
          </label>;
        })}
      </div>
    </section>)}
  </div>;
}
