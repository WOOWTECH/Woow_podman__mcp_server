import React, { useState, useMemo } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  Search, Loader2, Wrench, ToggleLeft, ToggleRight, RefreshCw, AlertTriangle, Settings,
} from 'lucide-react';
import { apiGet, apiPut } from '../api';
import { useCapabilities } from '../useCapabilities';
import ConditionalWrites from './ConditionalWrites';
import { mergeToolItems, disabledAfterToggle, matchesTool, toolAvailability, effectiveLabel, applyFeedback } from '../capabilities';

// 泛用啟用/停用清單：driven by props。取代原 McpWrapperTools / HermesToolsTab。
export default function ToolToggleList({ endpoint, itemsKey, grouped, note, labelText }) {
  const queryClient = useQueryClient();
  const [search, setSearch] = useState('');
  const [pendingToggles, setPendingToggles] = useState({});
  const isMcp = endpoint === '/tools';
  const capabilityQuery = useCapabilities(isMcp);
  const snapshot = capabilityQuery.isError ? null : capabilityQuery.data;

  const { data, isLoading, isFetching, error, refetch } = useQuery({
    queryKey: [endpoint],
    queryFn: () => apiGet(endpoint),
  });

  const mutation = useMutation({
    retry: false,
    mutationFn: ({ payload, revision }) => apiPut(endpoint, payload, { revision }),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: [endpoint] }),
        queryClient.invalidateQueries({ queryKey: ['capabilities'] }),
      ]);
      setPendingToggles({});
    },
    onError: () => setPendingToggles({}),
  });

  const items = useMemo(() => isMcp
    ? mergeToolItems(data?.[itemsKey] || [], snapshot, data?.disabled_tools)
    : data?.[itemsKey] || [], [data, itemsKey, isMcp, snapshot]);

  const filtered = useMemo(() => {
    return items.filter((t) => matchesTool(t, search));
  }, [items, search]);

  const groups = useMemo(() => {
    if (!grouped) return [['', filtered]];
    const map = {};
    for (const item of filtered) {
      const cat = item.category || 'Uncategorized';
      (map[cat] ||= []).push(item);
    }
    return Object.entries(map).sort(([a], [b]) => a.localeCompare(b));
  }, [filtered, grouped]);

  function handleToggle(name, currentEnabled) {
    setPendingToggles((prev) => ({ ...prev, [name]: true }));
    const payload = isMcp
      ? { disabled_tools: disabledAfterToggle(items, data?.disabled_tools, name, currentEnabled) }
      : { [itemsKey]: items.map((t) => (t.name === name ? { ...t, enabled: !currentEnabled } : t)) };
    mutation.mutate({ payload, revision: data?.configuration?.revision });
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <RefreshCw className="animate-spin text-muted" size={24} />
      </div>
    );
  }
  if (error) {
    return (
      <div className="bg-red-500/10 border border-red-500/20 rounded-xl p-6 text-center">
        <p className="text-red-600 font-medium">Failed to load</p>
        <p className="text-red-600/70 text-sm mt-1">{error.message}</p>
        <button onClick={() => refetch()} className="mt-4 px-4 py-2 bg-panel hover:bg-field text-ink-2 rounded-lg text-sm transition-colors">
          Retry
        </button>
      </div>
    );
  }

  const enabledCount = items.filter((t) => t.enabled).length;

  return (
    <div>
      {note && (
        <div className="flex items-center gap-2 px-3 py-2.5 mb-4 bg-amber-600/10 border border-amber-600/20 rounded-lg text-sm text-amber-600">
          <AlertTriangle size={16} className="shrink-0" />
          <span>{note}</span>
        </div>
      )}

      <p className="text-sm text-muted mb-4">
        {enabledCount} of {items.length} enabled by saved policy {labelText || ''}
        {isMcp && <> · {effectiveLabel(snapshot?.tools?.effective_count)} effective</>}
      </p>
      {isMcp && <div className="capability-card mb-4">
        <p className="text-sm text-muted">Tool availability comes from the running child. Enabling a tool does not prove backend access or override other settings.</p>
        <p className="text-sm text-muted mt-2" role="status">{snapshot?.tools?.status === 'observed'
          ? 'Child tool list observed.' : 'Effective count is unknown until child discovery succeeds.'}</p>
        <button className="capability-action mt-3" disabled={capabilityQuery.isFetching} onClick={() => { refetch(); capabilityQuery.refetch(); }}>
          {capabilityQuery.isFetching ? 'Checking…' : 'Refresh availability'}
        </button>
      </div>}
      {mutation.isError && <p role="alert" className="text-red-600 mb-4">{mutation.error.message}</p>}
      {mutation.isSuccess && <p role="status" className={`mb-4 text-sm ${applyFeedback(mutation.data) === 'error' ? 'text-amber-700' : 'text-muted'}`}>
        {mutation.data?.message || 'Saved. Refresh availability to verify the child state.'}
      </p>}

      {mutation.isPending && (
        <div className="flex items-center gap-2 px-3 py-2 mb-4 bg-brand-600/20 border border-brand-600/30 rounded-lg text-sm text-brand-400">
          <Loader2 size={14} className="animate-spin" />
          <span>Applying...</span>
        </div>
      )}

      <div className="relative mb-4">
        <Search size={18} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted" />
        <input
          type="text"
          aria-label="Search tools and input schemas"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search tools, descriptions, or input fields…"
          className="w-full pl-10 pr-4 py-2.5 bg-bg border border-border rounded-lg text-ink-2 placeholder-muted-2 focus:outline-none focus:border-brand-500 focus:ring-1 focus:ring-brand-500 transition-colors"
        />
      </div>

      {filtered.length === 0 ? (
        <div className="text-center py-12 text-muted">
          <Wrench size={32} className="mx-auto mb-3 opacity-50" />
          <p>No items match your search</p>
        </div>
      ) : (
        <div className="space-y-6">
          {groups.map(([category, groupItems]) => (
            <div key={category || '_'}>
              {grouped && category && (
                <h3 className="text-sm font-semibold text-muted uppercase tracking-wider mb-3 px-1">
                  {category}
                  <span className="ml-2 text-muted-2 font-normal">({groupItems.length})</span>
                </h3>
              )}
              <div className="capability-tools bg-bg border border-border rounded-xl divide-y divide-border">
                {groupItems.map((item) => {
                  const isPending = pendingToggles[item.name];
                  return (
                    <div key={item.name} className="flex items-center justify-between px-4 py-3 hover:bg-field/50 transition-colors">
                      <div className="flex-1 min-w-0 mr-4">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="text-sm font-medium text-ink-2 font-mono break-all">{item.name}</span>
                          {!grouped && item.category && (
                            <span className="px-1.5 py-0.5 text-[10px] rounded bg-field text-muted">{item.category}</span>
                          )}
                        </div>
                        {item.description && <p className="text-sm text-muted mt-2 break-words">{item.description}</p>}
                        {isMcp && <>
                          <p className="text-sm text-muted mt-2">{toolAvailability(item)}{item.runtime_only ? ' · Runtime-only tool' : ''}</p>
                          <ConditionalWrites tool={item} />
                          {item.operations?.length > 0 && <p className="text-sm text-muted mt-2">Operations: {item.operations.map(operation => `${operation.name}${operation.policy_enabled ? '' : ' (disabled)'}`).join(', ')}
                            {item.disabled_operations?.length > 0 && ` · Saved denies: ${item.disabled_operations.join(', ')}`}</p>}
                          <details className="capability-schema mt-3">
                            <summary>Input schema</summary>
                            {item.inputSchema ? <pre tabIndex={0} aria-label={`${item.name} input schema`}>{JSON.stringify(item.inputSchema, null, 2)}</pre>
                              : <p className="text-sm text-muted py-3">No schema observed from the child.</p>}
                          </details>
                        </>}
                      </div>
                      <button
                        onClick={() => handleToggle(item.name, item.enabled)}
                        disabled={isPending || mutation.isPending || isFetching}
                        className="shrink-0 disabled:opacity-50 transition-opacity"
                        title={item.enabled ? 'Disable' : 'Enable'}
                        aria-label={`${item.enabled ? 'Disable' : 'Enable'} ${item.name}`}
                        aria-pressed={!!item.enabled}
                      >
                        {isPending ? (
                          <Loader2 size={24} className="animate-spin text-muted" />
                        ) : item.enabled ? (
                          <ToggleRight size={28} className="text-brand-500" />
                        ) : (
                          <ToggleLeft size={28} className="text-muted-2" />
                        )}
                      </button>
                    </div>
                  );
                })}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
