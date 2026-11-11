// Pure view adapters: observed availability must never be inferred from saved policy.
export function capabilityNav(nav, snapshot) {
  return nav.map((section) => ({
    ...section,
    items: section.items.filter((item) => item.to !== '/permissions' || snapshot?.capabilities?.operation_policy !== false),
  })).filter((section) => section.items.length);
}

export function mergeToolItems(legacy, snapshot, disabledTools) {
  const items = new Map(legacy.map((item) => [item.name, item]));
  const disabled = Array.isArray(disabledTools) ? new Set(disabledTools) : null;
  for (const tool of snapshot?.tools?.items || []) {
    items.set(tool.name, { ...items.get(tool.name), ...tool,
      enabled: disabled ? !disabled.has(tool.name) : tool.policy_enabled });
  }
  return [...items.values()];
}

export function disabledAfterToggle(items, savedDisabled, name, enabled) {
  const disabled = new Set([...(savedDisabled || []), ...items.filter((t) => !t.enabled).map((t) => t.name)]);
  if (enabled) disabled.add(name);
  else disabled.delete(name);
  return [...disabled].sort();
}

export function matchesTool(tool, search) {
  const q = search.trim().toLowerCase();
  return !q || [tool.name, tool.description, tool.category, JSON.stringify(tool.inputSchema || {}), JSON.stringify(tool.conditional_writes || []),
    ...(tool.operations || [])].join(' ').toLowerCase().includes(q);
}

export function effectiveLabel(value) {
  return Number.isInteger(value) ? String(value) : 'Unknown';
}

export function toolAvailability(tool) {
  if (tool.effective === true) return 'Available';
  const reasons = {
    policy_disabled: 'Disabled by saved policy',
    child_stopped: 'Child is stopped',
    child_probe_failed: 'Child tool discovery failed',
    state_changed_during_probe: 'State changed during discovery; refresh to verify',
    not_advertised_by_child: 'Not advertised by the child; check settings and restart status',
  };
  return reasons[tool.reason] || (tool.effective === false ? 'Unavailable' : 'Availability unknown');
}

export function envUpdate(saved, edited) {
  return { env: edited, env_delete: Object.keys(saved || {}).filter((key) => !(key in edited)) };
}

export function applyFeedback(result) {
  if (result?.apply_state === 'unchanged') return 'success';
  return (result?.success === true && result?.restarted === false && result?.apply_state === undefined) || (result?.saved !== undefined && result.saved !== true) || ['partial', 'error'].includes(result?.status) || ['failed', 'pending'].includes(result?.apply_state)
    ? 'error' : 'success';
}
