import React from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { apiPost } from '../api';
import { configurationMessage, canRetryConfiguration, isConfigurationConflict } from '../configuration';

export default function ConfigurationStatus({ configuration }) {
  const queryClient = useQueryClient();
  const refresh = () => Promise.all(['config', 'settings', 'mcpStatus', 'capabilities'].map(key =>
    queryClient.invalidateQueries({ queryKey: [key] })));
  const apply = useMutation({
    retry: false,
    mutationFn: revision => apiPost('/settings/mcp/apply', undefined, { revision }),
    onSuccess: refresh,
  });
  if (!configuration) return null;
  return <section aria-label="設定套用狀態" className="mb-5 p-4 bg-panel border border-border rounded-[20px] break-words">
    <p role="status" className={configuration.apply_state === 'failed' ? 'text-amber-700' : 'text-ink-2'}>
      {configurationMessage(configuration)}
    </p>
    <p className="mt-2 text-xs text-muted">此狀態不代表後端連線或各項工具操作已通過驗證。重試只使用已儲存設定，不會提交未儲存草稿。</p>
    {configuration.last_error_code && <p className="mt-2 text-xs text-muted">錯誤代碼：{configuration.last_error_code}</p>}
    <div className="flex flex-wrap gap-3 mt-3">
      {canRetryConfiguration(configuration) && <button className="capability-action" disabled={apply.isPending}
        onClick={() => apply.mutate(configuration.revision)}>{apply.isPending ? '套用中…' : '重試套用已儲存設定'}</button>}
      <button className="capability-action" disabled={apply.isPending} onClick={() => { apply.reset(); refresh(); }}>重新讀取狀態</button>
    </div>
    {apply.isError && <p role="alert" className="mt-3 text-red-600">{isConfigurationConflict(apply.error)
      ? '已儲存設定已更新。請先重新讀取狀態，確認後再重試。'
      : '無法確認套用結果。請先重新讀取狀態，勿重複提交表單。'}</p>}
  </section>;
}
