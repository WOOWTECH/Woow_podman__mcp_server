import React from 'react';

export default function OperationRecovery({ operation, onReload }) {
  if (!operation.hasIntent) return null;
  return <div role="status" className="my-3 p-3 border border-amber-500 rounded-lg text-sm">
    <p>保留原操作識別碼；重試會使用相同請求，不會自動重送。</p>
    <p>操作狀態：{operation.receipt?.state || 'unconfirmed'}。unknown 表示可能已執行，不能推定成功或失敗。</p>
    <button className="capability-action mr-3" onClick={async () => { await operation.query(); await onReload(); }}>查詢操作狀態</button>
    <button className="capability-action" onClick={async () => {
      if (!window.confirm('已檢查目前狀態，並確認要結束此操作追蹤？新的操作可能再次產生效果。')) return;
      await onReload(); operation.clear();
    }}>核對後重新載入</button>
  </div>;
}
