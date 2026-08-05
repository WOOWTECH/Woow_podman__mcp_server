import React, { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  Shield,
  Save,
  Loader2,
  CheckCircle2,
  XCircle,
  AlertCircle,
  AlertTriangle,
  RotateCcw,
} from 'lucide-react';
import { apiGet, apiPut } from '../api';

/**
 * Tool allow/deny policy.
 *
 * The backend only understands two keys here — `allowed_tools` (an ALLOWLIST)
 * and `denied_tools`. Anything else typed into this box used to be stored
 * verbatim under `tools.permissions`, a place the runtime env builder never
 * reads: writing {"readonly": true} produced a green banner, survived a
 * reload, and changed nothing. So the editor now validates the policy against
 * that contract before saving and points the operator at the Tools page for
 * the switches that live there.
 */
const POLICY_KEYS = new Set(['allowed_tools', 'denied_tools']);

/** Keys the operator plausibly means, but which are owned by the Tools page. */
const TOOLS_PAGE_KEYS = new Set([
  'readonly',
  'disabled_categories',
  'disabled_tools',
  'disabled_operations',
]);

const DEFAULT_POLICY = { allowed_tools: ['*'], denied_tools: [] };

function pretty(value) {
  return JSON.stringify(value, null, 2);
}

function policyText(permissions) {
  if (permissions == null) return pretty(DEFAULT_POLICY);
  // The stored value has been a JSON *string* on some older configs.
  if (typeof permissions === 'string') return permissions;
  return pretty(permissions);
}

/**
 * Parse + validate the editor contents against the policy contract.
 *
 * Returns {policy} or {error}. The old version only checked `JSON.parse`
 * succeeded, so an array or a bare string left Save enabled and the operator's
 * only feedback was FastAPI's 422 rendered as "[object Object]".
 */
function parsePolicy(text) {
  if (!text.trim()) return { error: 'Policy cannot be empty. Use {} for "no restrictions".' };

  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (err) {
    return { error: `JSON syntax error: ${err.message}` };
  }

  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return {
      error:
        'Policy must be a JSON object, e.g. {"allowed_tools": ["*"], "denied_tools": []} — ' +
        `got ${Array.isArray(parsed) ? 'an array' : typeof parsed}.`,
    };
  }

  const unknown = Object.keys(parsed).filter((key) => !POLICY_KEYS.has(key));
  if (unknown.length) {
    const misplaced = unknown.filter((key) => TOOLS_PAGE_KEYS.has(key));
    const hint = misplaced.length
      ? ` ${misplaced.join(', ')} ${misplaced.length === 1 ? 'is' : 'are'} set on the Tools page, not here — saving ${misplaced.length === 1 ? 'it' : 'them'} in this policy would have no effect.`
      : '';
    return {
      error: `Unsupported ${unknown.length === 1 ? 'key' : 'keys'}: ${unknown.join(', ')}. Only allowed_tools and denied_tools are honoured.${hint}`,
    };
  }

  for (const key of POLICY_KEYS) {
    if (!(key in parsed)) continue;
    const value = parsed[key];
    if (value === null && key === 'allowed_tools') continue; // null = no allowlist
    if (!Array.isArray(value)) {
      return { error: `${key} must be a list of tool names${key === 'allowed_tools' ? ' (or null for "no allowlist")' : ''}, got ${value === null ? 'null' : typeof value}.` };
    }
    const bad = value.find((item) => typeof item !== 'string');
    if (bad !== undefined) {
      return { error: `${key} must contain only strings; found ${JSON.stringify(bad)}.` };
    }
  }

  return { policy: parsed };
}

/**
 * Effective disabled set for a policy — the same rules the server applies.
 *
 * `allowed_tools` missing or null means "no allowlist". `["*"]` says that
 * explicitly. An EMPTY LIST means "allow nothing" and must fail CLOSED: the
 * most natural way to write a lockdown must never be read as the wildcard.
 */
function deriveDisabled(policy, allNames) {
  const denied = new Set((policy.denied_tools || []).map(String));
  const disabled = new Set(denied);

  const allowed = policy.allowed_tools;
  if (allowed === undefined || allowed === null) return disabled;
  const allowedSet = new Set(allowed.map(String));
  if (allowedSet.has('*')) return disabled;
  for (const name of allNames) {
    if (!allowedSet.has(name)) disabled.add(name);
  }
  return disabled;
}

export default function PermissionEditor() {
  const queryClient = useQueryClient();
  const [content, setContent] = useState('');
  const [parseError, setParseError] = useState(null);
  const [isDirty, setIsDirty] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const gutterRef = useRef(null);

  const { data: config, isLoading } = useQuery({
    queryKey: ['config'],
    queryFn: () => apiGet('/config'),
  });

  // Same cache entry as the Tools page, so this costs nothing on a warm SPA.
  // It is what lets the editor name unknown tools *before* the round trip and
  // show which tools the policy actually leaves live.
  const { data: toolsData } = useQuery({
    queryKey: ['tools'],
    queryFn: () => apiGet('/tools'),
  });

  useEffect(() => {
    if (config?.permissions != null) {
      setContent(policyText(config.permissions));
      setIsDirty(false);
      setParseError(null);
    }
  }, [config]);

  const saveMutation = useMutation({
    mutationFn: (permissions) => apiPut('/config/permissions', { permissions }),
    onSuccess: () => {
      // A permissions save rewrites tools.disabled_tools and restarts the MCP
      // child, so the Tools page and the health card are both stale now.
      queryClient.invalidateQueries({ queryKey: ['config'] });
      queryClient.invalidateQueries({ queryKey: ['tools'] });
      queryClient.invalidateQueries({ queryKey: ['health'] });
      queryClient.invalidateQueries({ queryKey: ['mcpStatus'] });
      setIsDirty(false);
      setConfirming(false);
    },
  });

  const allNames = useMemo(
    () => (toolsData?.tools || []).map((tool) => tool.name),
    [toolsData]
  );

  // Live preview of what the policy does, recomputed from the text on every
  // keystroke: the operator sees the lockdown before it restarts the server.
  const preview = useMemo(() => {
    const { policy, error } = parsePolicy(content);
    if (error || !allNames.length) return null;
    const disabled = deriveDisabled(policy, allNames);
    const known = new Set(allNames);
    const named = [...(policy.allowed_tools || []), ...(policy.denied_tools || [])].map(String);
    const unknown = [...new Set(named.filter((name) => name !== '*' && !known.has(name)))].sort();
    return {
      disabled: [...disabled].filter((name) => known.has(name)).sort(),
      enabled: allNames.filter((name) => !disabled.has(name)).sort(),
      unknown,
      lockdown: Array.isArray(policy.allowed_tools) && policy.allowed_tools.length === 0,
    };
  }, [content, allNames]);

  const handleChange = useCallback((e) => {
    const value = e.target.value;
    setContent(value);
    setIsDirty(true);
    setConfirming(false);
    setParseError(parsePolicy(value).error || null);
  }, []);

  function handleSave() {
    const { policy, error } = parsePolicy(content);
    if (error) {
      setParseError(error);
      setConfirming(false);
      return;
    }
    // Saving chains into apply_to_runtime -> manager.restart(), which SIGTERMs
    // the FastMCP child every connected client (including a claude.ai
    // connector) is streaming from. Never do that on a single click.
    if (!confirming) {
      setConfirming(true);
      return;
    }
    saveMutation.mutate(policy);
  }

  function handleReset() {
    if (config?.permissions != null) {
      setContent(policyText(config.permissions));
      setIsDirty(false);
      setParseError(null);
      setConfirming(false);
    }
  }

  function handleFormat() {
    const { policy, error } = parsePolicy(content);
    if (error) {
      setParseError(error);
      return;
    }
    setContent(pretty(policy));
    setParseError(null);
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="animate-spin text-gray-500" size={24} />
      </div>
    );
  }

  const lineCount = content.split('\n').length;
  const result = saveMutation.data;

  return (
    <div className="flex flex-col h-[calc(100vh-8rem)]">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h2 className="text-2xl font-bold text-gray-100">Permission Editor</h2>
          <p className="text-sm text-gray-500 mt-1">
            Allow / deny policy for MCP tools &middot; only{' '}
            <code className="text-gray-400">allowed_tools</code> and{' '}
            <code className="text-gray-400">denied_tools</code> are honoured
            {isDirty && <span className="text-amber-400 ml-2">(unsaved changes)</span>}
          </p>
        </div>

        <div className="flex items-center gap-2">
          <button
            onClick={handleFormat}
            className="px-3 py-2 bg-gray-800 hover:bg-gray-700 text-gray-400 hover:text-gray-200 rounded-lg text-sm font-medium transition-colors"
          >
            Format JSON
          </button>

          <button
            onClick={handleReset}
            disabled={!isDirty}
            className="flex items-center gap-1.5 px-3 py-2 bg-gray-800 hover:bg-gray-700 disabled:opacity-50 disabled:hover:bg-gray-800 text-gray-400 hover:text-gray-200 rounded-lg text-sm font-medium transition-colors"
          >
            <RotateCcw size={14} />
            <span>Reset</span>
          </button>

          <button
            onClick={handleSave}
            disabled={!!parseError || saveMutation.isPending || !isDirty}
            className={`flex items-center gap-1.5 px-4 py-2 ${
              confirming ? 'bg-amber-600 hover:bg-amber-500' : 'bg-brand-600 hover:bg-brand-500'
            } disabled:bg-gray-700 disabled:text-gray-500 text-white font-medium rounded-lg text-sm transition-colors`}
          >
            {saveMutation.isPending ? (
              <Loader2 size={14} className="animate-spin" />
            ) : (
              <Save size={14} />
            )}
            <span>{confirming ? 'Confirm save & restart' : 'Save'}</span>
          </button>
        </div>
      </div>

      {confirming && !saveMutation.isPending && (
        <div className="px-3 py-2.5 mb-3 bg-amber-500/10 border border-amber-500/20 rounded-lg text-sm text-amber-300">
          <div className="flex items-center gap-2 font-medium">
            <AlertTriangle size={16} className="shrink-0" />
            <span>Saving restarts the MCP server and drops every active client.</span>
          </div>
          <ul className="mt-2 ml-6 list-disc space-y-1 text-amber-200/80 text-xs">
            <li>In-flight connector sessions (including claude.ai) are disconnected.</li>
            {preview && preview.disabled.length > 0 && (
              <li>
                {preview.disabled.length} tool{preview.disabled.length === 1 ? '' : 's'} will be
                switched off: <span className="font-mono">{preview.disabled.join(', ')}</span>
              </li>
            )}
            {preview && preview.disabled.length === 0 && (
              <li>No tools are switched off by this policy.</li>
            )}
            {preview?.lockdown && (
              <li className="text-amber-100">
                <span className="font-medium">allowed_tools is empty</span> — this allows nothing.
                Every tool will be disabled. Use{' '}
                <code>{'{"allowed_tools": ["*"]}'}</code> if you meant "no restriction".
              </li>
            )}
          </ul>
          <button
            onClick={() => setConfirming(false)}
            className="mt-2 ml-6 text-xs text-amber-200/70 hover:text-amber-100 underline"
          >
            Cancel
          </button>
        </div>
      )}

      {parseError && (
        <div className="flex items-start gap-2 px-3 py-2.5 mb-3 bg-red-500/10 border border-red-500/20 rounded-lg text-sm text-red-400">
          <AlertCircle size={16} className="shrink-0 mt-0.5" />
          <span className="font-mono text-xs">{parseError}</span>
        </div>
      )}

      {/* The server's own verdict, not a hardcoded cheer: `partial` means the
          policy is on disk but the restart failed, so it is NOT live, and
          unknown_tools names every entry the registry does not have. */}
      {saveMutation.isSuccess && result && (
        <div
          className={`flex items-start gap-2 px-3 py-2.5 mb-3 rounded-lg text-sm border ${
            result.status === 'ok'
              ? 'bg-emerald-500/10 border-emerald-500/20 text-emerald-400'
              : 'bg-amber-500/10 border-amber-500/20 text-amber-300'
          }`}
        >
          {result.status === 'ok' ? (
            <CheckCircle2 size={16} className="shrink-0 mt-0.5" />
          ) : (
            <AlertTriangle size={16} className="shrink-0 mt-0.5" />
          )}
          <div>
            <div>
              {result.status === 'ok' &&
                `Policy saved and applied — MCP server restarted. ${result.disabled_count} tool${
                  result.disabled_count === 1 ? '' : 's'
                } disabled.`}
              {result.status === 'partial' &&
                `Policy saved to disk (${result.disabled_count} tool${
                  result.disabled_count === 1 ? '' : 's'
                } disabled) but the MCP server failed to restart — the OLD policy is still live. Restart it from Settings.`}
              {result.status === 'detached' &&
                `Policy saved (${result.disabled_count} tool${
                  result.disabled_count === 1 ? '' : 's'
                } disabled). The MCP server is not managed by this console, so it must be restarted manually before the policy takes effect.`}
            </div>
            {result.unknown_tools?.length > 0 && (
              <div className="mt-1 text-xs">
                Ignored — not in the tool registry (typo?):{' '}
                <span className="font-mono">{result.unknown_tools.join(', ')}</span>
              </div>
            )}
          </div>
        </div>
      )}

      {saveMutation.isError && (
        <div className="flex items-start gap-2 px-3 py-2.5 mb-3 bg-red-500/10 border border-red-500/20 rounded-lg text-sm text-red-400">
          <XCircle size={16} className="shrink-0 mt-0.5" />
          <span>{saveMutation.error.message}</span>
        </div>
      )}

      <div className="flex-1 flex gap-4 min-h-0">
        <div className="flex-1 relative">
          <div className="absolute inset-0 flex bg-gray-950 border border-gray-800 rounded-xl overflow-hidden">
            {/* The gutter has no scrollbar of its own — it is driven by the
                textarea's onScroll. Two independent scrollers drifted apart the
                moment a policy outgrew the viewport, so the line numbers lied
                exactly when a parse error quoted one. */}
            <div
              ref={gutterRef}
              className="py-3 px-2 bg-gray-900 text-right select-none border-r border-gray-800 overflow-hidden shrink-0"
            >
              {Array.from({ length: lineCount }, (_, i) => (
                <div key={i + 1} className="text-xs text-gray-600 leading-relaxed font-mono px-1">
                  {i + 1}
                </div>
              ))}
            </div>
            <textarea
              value={content}
              onChange={handleChange}
              onScroll={(e) => {
                if (gutterRef.current) gutterRef.current.scrollTop = e.currentTarget.scrollTop;
              }}
              spellCheck={false}
              className="flex-1 p-3 bg-transparent text-gray-200 font-mono text-sm leading-relaxed resize-none focus:outline-none placeholder-gray-600 overflow-y-auto"
              placeholder='{"allowed_tools": ["*"], "denied_tools": ["podman_container_exec"]}'
            />
          </div>
        </div>

        <div className="w-72 shrink-0 overflow-y-auto bg-gray-900/50 border border-gray-800 rounded-xl p-4">
          <h3 className="text-sm font-semibold text-gray-200 mb-2">Effective result</h3>
          {!preview && (
            <p className="text-xs text-gray-500">
              {parseError
                ? 'Fix the policy to see which tools it leaves enabled.'
                : 'Loading the tool registry…'}
            </p>
          )}
          {preview && (
            <div className="space-y-3 text-xs">
              {preview.lockdown && (
                <p className="text-amber-300">
                  <span className="font-medium">allowed_tools: []</span> allows nothing — every
                  tool is disabled.
                </p>
              )}
              <p className="text-gray-400">
                <span className="text-emerald-400 font-medium">{preview.enabled.length}</span>{' '}
                enabled &middot;{' '}
                <span className="text-red-400 font-medium">{preview.disabled.length}</span> disabled
              </p>
              {preview.unknown.length > 0 && (
                <p className="text-amber-300">
                  Not in the registry, will be ignored:{' '}
                  <span className="font-mono">{preview.unknown.join(', ')}</span>
                </p>
              )}
              <div>
                <p className="text-gray-500 uppercase tracking-wide mb-1">Enabled</p>
                <ul className="space-y-0.5 font-mono text-gray-300">
                  {preview.enabled.length === 0 && <li className="text-gray-600">none</li>}
                  {preview.enabled.map((name) => (
                    <li key={name} className="truncate" title={name}>
                      {name}
                    </li>
                  ))}
                </ul>
              </div>
              {preview.disabled.length > 0 && (
                <div>
                  <p className="text-gray-500 uppercase tracking-wide mb-1">Disabled</p>
                  <ul className="space-y-0.5 font-mono text-gray-500">
                    {preview.disabled.map((name) => (
                      <li key={name} className="truncate" title={name}>
                        {name}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              <p className="text-gray-600 pt-2 border-t border-gray-800">
                Read-only mode, category switches and per-operation gates live on the{' '}
                <span className="text-gray-400">Tools</span> page — they are ignored here.
              </p>
            </div>
          )}
        </div>
      </div>

      <div className="flex items-center justify-between mt-3 text-xs text-gray-600">
        <div className="flex items-center gap-1">
          <Shield size={12} />
          <span>Permission policy (JSON)</span>
        </div>
        <span>
          {lineCount} lines &middot; {content.length} chars
        </span>
      </div>
    </div>
  );
}
