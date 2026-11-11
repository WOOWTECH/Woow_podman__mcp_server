import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { RefreshCw } from 'lucide-react';
import { apiGet } from '../api';
import StatusCard from '../components/StatusCard';
import { useAppConfig } from '../appConfigContext';
import { healthRefetchInterval } from '../mcpServerCard';
import CapabilitySummary from '../components/CapabilitySummary';

export default function Dashboard() {
  const { dashboard } = useAppConfig();
  const { data: health, isLoading, error, refetch, dataUpdatedAt } = useQuery({
    queryKey: ['health'],
    queryFn: () => apiGet('/health'),
    // 看門狗排程重試時 3 秒刷新一次（看得到倒數與恢復），平常 30 秒（DATA-NMCP-004-1）
    refetchInterval: (query) => healthRefetchInterval(query.state.data),
  });

  const lastRefresh = dataUpdatedAt ? new Date(dataUpdatedAt).toLocaleTimeString() : null;

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
        <p className="text-red-600 font-medium">Failed to load health status</p>
        <p className="text-red-600/70 text-sm mt-1">{error.message}</p>
        <button onClick={() => refetch()} className="mt-4 px-4 py-2 bg-panel hover:bg-field text-ink-2 rounded-lg text-sm transition-colors">
          Retry
        </button>
      </div>
    );
  }

  const sections = dashboard.buildSections(health || {});
  const extra = dashboard.renderExtra ? dashboard.renderExtra(health || {}) : null;

  return (
    <div>
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-6">
        <div>
          <h2 className="text-xl sm:text-2xl font-bold text-ink">Dashboard</h2>
          <p className="text-sm text-muted mt-1">
            {dashboard.subtitle}
            {lastRefresh && <span> &middot; Updated {lastRefresh}</span>}
          </p>
        </div>
        <button onClick={() => refetch()} className="flex items-center gap-2 px-3 py-2 bg-panel hover:bg-field text-muted hover:text-ink-2 rounded-lg text-sm transition-colors">
          <RefreshCw size={14} />
          <span>Refresh</span>
        </button>
      </div>

      <CapabilitySummary />
      {sections.map((section, i) => (
        <div key={i}>
          {section.heading && (
            <h3 className="text-sm font-semibold text-muted uppercase tracking-wider mb-3 px-1">{section.heading}</h3>
          )}
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 mb-8">
            {section.cards.map((c, j) => (
              <StatusCard key={j} title={c.title} status={c.status} value={c.value} subtitle={c.subtitle} icon={c.icon} />
            ))}
          </div>
        </div>
      ))}

      {extra}
    </div>
  );
}
