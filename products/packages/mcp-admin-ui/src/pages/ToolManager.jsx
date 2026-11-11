import React, { useState } from 'react';
import { useAppConfig } from '../appConfigContext';
import ToolToggleList from '../components/ToolToggleList';
import { Link } from 'react-router-dom';
import { useCapabilities } from '../useCapabilities';

function TabButton({ active, onClick, children }) {
  return (
    <button
      onClick={onClick}
      className={`px-4 py-2 text-sm font-medium rounded-lg transition-colors ${
        active
          ? 'bg-brand-600/20 text-brand-400 border border-brand-600/30'
          : 'text-muted hover:text-ink-2 hover:bg-panel'
      }`}
    >
      {children}
    </button>
  );
}

export default function ToolManager() {
  const { toolTabs } = useAppConfig();
  const { data: capabilities } = useCapabilities();
  const [active, setActive] = useState(toolTabs[0].key);
  const tab = toolTabs.find((t) => t.key === active) || toolTabs[0];

  return (
    <div>
      <div className="mb-6">
        <h2 className="text-xl sm:text-2xl font-bold text-ink">Tool Manager</h2>
        <p className="text-sm text-muted mt-1">Manage MCP tools</p>
      </div>

      {toolTabs.length > 1 && (
        <div className="flex items-center gap-2 mb-6 flex-wrap">
          {toolTabs.map((t) => {
            const Icon = t.icon;
            return (
              <TabButton key={t.key} active={active === t.key} onClick={() => setActive(t.key)}>
                <span className="flex items-center gap-2">{Icon && <Icon size={14} />}{t.label}</span>
              </TabButton>
            );
          })}
        </div>
      )}

      {(capabilities?.capabilities?.operation_policy === true || Object.keys(capabilities?.tools?.disabled_operations || {}).length > 0) && (
        <div className="capability-card mb-6">
          <p className="text-muted">Review operation restrictions alongside whole-tool settings.</p>
          <Link className="capability-action inline-flex mt-3" to="/permissions">Edit operation permissions</Link>
        </div>
      )}

      <ToolToggleList
        key={tab.key}
        endpoint={tab.endpoint}
        itemsKey={tab.itemsKey}
        grouped={tab.grouped}
        note={tab.note}
        labelText={tab.labelText}
      />
    </div>
  );
}
