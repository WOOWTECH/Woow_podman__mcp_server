import React from 'react';

// 相依政策只讀取伺服器描述；草稿不會提前變成已儲存的授權。
export default function ConditionalWrites({ tool }) {
  return (tool.conditional_writes || []).map(rule => <div key={rule.parameter}
    className="mt-3 text-base text-muted break-words" aria-label={`${tool.name} 條件寫入`}>
    <h4 className="font-semibold text-ink-2">條件寫入</h4>
    <p className="mt-2">{rule.parameter} 正規化後為 true 時，表示要將工作流程開放給 MCP 存取；即使所選操作是讀取，也須有額外寫入權限。false 或省略不觸發這項額外檢查，原工具與操作權限仍須允許。</p>
    <p className="mt-2 break-all">需要 {rule.requires_tool}；相依權限：已儲存政策{rule.policy_enabled === true ? '允許' : rule.policy_enabled === false ? '禁止' : '未知'}。</p>
    <p className="mt-2">請在工具管理確認相依工具已啟用，且沒有禁止它的操作。勾選 expose 只解除本工具的條件限制，不會自動啟用相依工具。儲存後對新呼叫立即生效；子行程設定與後端權限仍可能限制存取。</p>
  </div>);
}
