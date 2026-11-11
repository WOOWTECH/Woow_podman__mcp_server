import React from 'react';
import { useCapabilities } from '../useCapabilities';
import { effectiveLabel } from '../capabilities';

export default function CapabilitySummary() {
  const { data, error, isFetching, refetch } = useCapabilities();
  return (
    <section className="capability-card mb-6" aria-label="MCP availability">
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div>
          <h3 className="text-lg font-semibold">MCP availability</h3>
          <p className="text-sm text-muted mt-2">
            {data?.tools?.catalog_count ?? '—'} catalog tools · {effectiveLabel(error ? null : data?.tools?.effective_count)} effective
          </p>
        </div>
        <button className="capability-action" onClick={() => refetch()} disabled={isFetching}>
          {isFetching ? 'Checking…' : 'Refresh availability'}
        </button>
      </div>
      <p className="text-sm text-muted mt-3" role="status">
        {error ? 'Availability check failed. Saved policy is unchanged.'
          : `Child: ${data?.health?.child || 'unknown'}. Backend: ${data?.health?.backend || 'unknown'}. Public endpoint: ${data?.health?.public_endpoint || 'unverified'}.`}
      </p>
      <p className="text-sm text-muted mt-2">Effective means advertised by the child and allowed by saved tool policy. Backend access and individual operations require separate checks.</p>
    </section>
  );
}
