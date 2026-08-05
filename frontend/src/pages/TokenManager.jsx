import React, { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  KeyRound,
  Copy,
  RefreshCw,
  Loader2,
  CheckCircle2,
  AlertTriangle,
  Eye,
  EyeOff,
  Clock,
  Sparkles,
  Link2,
} from 'lucide-react';
import { apiGet, apiPost } from '../api';

/**
 * Masked label for one rotation-history row.
 *
 * The history contract is {"masked": …, "rotated_at": …}; older builds wrote
 * {"token_masked": …} and older still a bare string. The previous renderer was
 * `{entry.masked || entry}`, which handed React a raw OBJECT for every entry
 * written by the other rotate endpoint — "Objects are not valid as a React
 * child" unmounted the whole console. This helper can only ever return a
 * string, so that failure cannot come back whatever shape the API returns.
 */
function maskedOf(entry) {
  if (entry == null) return '—';
  if (typeof entry === 'string') return entry;
  if (typeof entry === 'object') {
    const masked = entry.token_masked ?? entry.masked;
    if (typeof masked === 'string' && masked) return masked;
    try {
      return JSON.stringify(entry);
    } catch {
      return '(unreadable entry)';
    }
  }
  return String(entry);
}

function rotatedAtOf(entry) {
  const raw = entry && typeof entry === 'object' ? entry.rotated_at : null;
  if (!raw) return 'retired';
  const when = new Date(raw);
  return Number.isNaN(when.getTime()) ? String(raw) : when.toLocaleString();
}

export default function TokenManager() {
  const queryClient = useQueryClient();
  const [showToken, setShowToken] = useState(false);
  const [newToken, setNewToken] = useState(null);
  const [preview, setPreview] = useState(null);
  const [copied, setCopied] = useState('');
  const [confirmRotate, setConfirmRotate] = useState(false);
  const [acknowledged, setAcknowledged] = useState(false);

  // GET /api/tokens is the only read this page needs: it reports
  // {masked, configured, history}. It deliberately does NOT carry the
  // plaintext token — GET /api/settings no longer returns `mcp_auth_token` at
  // all, so reading it from there would render `undefined` into a connector
  // URL an operator would then paste into claude.ai.
  const { data: tokenInfo, isLoading } = useQuery({
    queryKey: ['tokenInfo'],
    queryFn: () => apiGet('/tokens'),
  });

  // POST /api/tokens/rotate is the canonical endpoint: it mints
  // secrets.token_urlsafe(32) and writes the {masked, rotated_at} history this
  // page renders. The page used to call the core's
  // /settings/mcp_auth_token/rotate, which minted a different character set and
  // wrote a different history shape — the root cause of the render crash.
  const rotateMutation = useMutation({
    mutationFn: () => apiPost('/tokens/rotate'),
    onSuccess: (result) => {
      setNewToken(result?.token || null);
      setPreview(null);
      setShowToken(true);
      queryClient.invalidateQueries({ queryKey: ['settings'] });
      queryClient.invalidateQueries({ queryKey: ['tokenInfo'] });
      queryClient.invalidateQueries({ queryKey: ['mcpStatus'] });
      setConfirmRotate(false);
      setAcknowledged(false);
    },
  });

  // Preview mints a candidate without persisting anything, so an operator can
  // stage the new value (Worker secret, connector URL) before the switchover.
  const previewMutation = useMutation({
    mutationFn: () => apiPost('/tokens/generate'),
    onSuccess: (result) => setPreview(result?.token || null),
  });

  // The full value exists in the browser only on the response to the rotation
  // that minted it; every later read is masked. Anything else shown or copied
  // here would be a mask masquerading as a credential.
  const plaintext = newToken;
  const maskedToken = tokenInfo?.configured
    ? tokenInfo.masked || '(configured)'
    : 'No token configured';
  const connectorUrl = plaintext
    ? `${window.location.origin}/private_${plaintext}/mcp/`
    : null;

  async function copyValue(value, tag) {
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      // clipboard API needs a secure context; fall back to the old selection
      // trick so copying still works over plain http on a LAN address.
      const ta = document.createElement('textarea');
      ta.value = value;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
    }
    setCopied(tag);
    setTimeout(() => setCopied(''), 2000);
  }

  const history = Array.isArray(tokenInfo?.history) ? tokenInfo.history : [];
  const rotateStatus = rotateMutation.data?.status;

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="animate-spin text-gray-500" size={24} />
      </div>
    );
  }

  return (
    <div>
      <div className="mb-6">
        <h2 className="text-2xl font-bold text-gray-100">Token Manager</h2>
        <p className="text-sm text-gray-500 mt-1">Manage the MCP proxy authentication token</p>
      </div>

      <div className="bg-gray-900 border border-gray-800 rounded-xl p-6 max-w-xl mb-6">
        <div className="flex items-center gap-2 mb-4">
          <KeyRound size={18} className="text-gray-400" />
          <h3 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">
            Current Token
          </h3>
        </div>

        <div className="flex items-center gap-2 mb-4">
          <div className="flex-1 px-3 py-2.5 bg-gray-800 border border-gray-700 rounded-lg font-mono text-sm text-gray-300 overflow-hidden break-all">
            {showToken && plaintext ? plaintext : maskedToken}
          </div>
          <button
            onClick={() => setShowToken(!showToken)}
            disabled={!plaintext}
            className="p-2.5 bg-gray-800 hover:bg-gray-700 disabled:opacity-40 text-gray-400 hover:text-gray-200 border border-gray-700 rounded-lg transition-colors"
            title={
              plaintext
                ? showToken
                  ? 'Hide token'
                  : 'Show token'
                : 'The stored token is masked — rotate to see a full value'
            }
          >
            {showToken ? <EyeOff size={16} /> : <Eye size={16} />}
          </button>
          <button
            onClick={() => copyValue(plaintext, 'token')}
            disabled={!plaintext}
            className="p-2.5 bg-gray-800 hover:bg-gray-700 disabled:opacity-40 text-gray-400 hover:text-gray-200 border border-gray-700 rounded-lg transition-colors"
            title={plaintext ? 'Copy token' : 'Only a freshly rotated token can be copied'}
          >
            {copied === 'token' ? (
              <CheckCircle2 size={16} className="text-brand-400" />
            ) : (
              <Copy size={16} />
            )}
          </button>
        </div>

        {newToken && (
          <div className="mb-4 flex items-start gap-2 px-3 py-2.5 rounded-lg text-sm bg-emerald-500/10 border border-emerald-500/20 text-emerald-400">
            <CheckCircle2 size={16} className="mt-0.5 shrink-0" />
            <span>
              New token active. Copy it now — every later read is masked, so this is the
              only time the full value is shown.
            </span>
          </div>
        )}

        {rotateStatus === 'partial' && (
          <div className="mb-4 flex items-start gap-2 px-3 py-2.5 rounded-lg text-sm bg-amber-500/10 border border-amber-500/20 text-amber-400">
            <AlertTriangle size={16} className="mt-0.5 shrink-0" />
            <span>
              The token was saved but the MCP server did not restart — restart it from
              Settings before re-pointing your clients.
            </span>
          </div>
        )}

        <div className="mb-4">
          <p className="text-xs text-gray-500 mb-1.5 flex items-center gap-1.5">
            <Link2 size={12} />
            MCP connector URL <span className="text-gray-600">(the trailing slash is required)</span>
          </p>
          <div className="flex items-center gap-2">
            <code className="flex-1 px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg font-mono text-xs text-gray-300 break-all">
              {connectorUrl || `${window.location.origin}/private_<token>/mcp/`}
            </code>
            <button
              onClick={() => copyValue(connectorUrl, 'url')}
              disabled={!connectorUrl}
              className="p-2 bg-gray-800 hover:bg-gray-700 disabled:opacity-40 text-gray-400 hover:text-gray-200 border border-gray-700 rounded-lg transition-colors"
              title={
                connectorUrl
                  ? 'Copy connector URL'
                  : 'Rotate the token to get a copyable connector URL'
              }
            >
              {copied === 'url' ? (
                <CheckCircle2 size={16} className="text-brand-400" />
              ) : (
                <Copy size={16} />
              )}
            </button>
          </div>
          {!connectorUrl && (
            <p className="text-xs text-gray-600 mt-1.5">
              The stored token is masked, so the full URL cannot be rebuilt here. It is
              shown once, immediately after a rotation.
            </p>
          )}
        </div>

        <div className="flex flex-wrap gap-2">
          <button
            onClick={() => previewMutation.mutate()}
            disabled={previewMutation.isPending}
            className="flex items-center gap-2 px-4 py-2.5 bg-gray-800 hover:bg-gray-700 text-gray-300 border border-gray-700 font-medium rounded-lg transition-colors"
            title="Mint a candidate token without changing anything"
          >
            {previewMutation.isPending ? (
              <Loader2 size={16} className="animate-spin" />
            ) : (
              <Sparkles size={16} />
            )}
            <span>Preview new token</span>
          </button>

          {!confirmRotate && (
            <button
              onClick={() => {
                setConfirmRotate(true);
                setAcknowledged(false);
              }}
              className="flex items-center gap-2 px-4 py-2.5 bg-amber-600/20 hover:bg-amber-600/30 text-amber-400 border border-amber-600/30 font-medium rounded-lg transition-colors"
            >
              <RefreshCw size={16} />
              <span>Rotate Token</span>
            </button>
          )}
        </div>

        {preview && (
          <div className="mt-4 px-3 py-2.5 rounded-lg bg-gray-800 border border-gray-700">
            <p className="text-xs text-gray-500 mb-1.5">
              Candidate token — nothing has changed yet. Rotating is what makes it live.
            </p>
            <div className="flex items-center gap-2">
              <code className="flex-1 font-mono text-xs text-gray-300 break-all">{preview}</code>
              <button
                onClick={() => copyValue(preview, 'preview')}
                className="p-1.5 text-gray-400 hover:text-gray-200"
                title="Copy candidate"
              >
                {copied === 'preview' ? (
                  <CheckCircle2 size={14} className="text-brand-400" />
                ) : (
                  <Copy size={14} />
                )}
              </button>
            </div>
          </div>
        )}

        {previewMutation.isError && (
          <div className="mt-3 text-sm text-red-400">{previewMutation.error.message}</div>
        )}

        {confirmRotate && (
          <div className="mt-4 border border-amber-600/30 bg-amber-600/10 rounded-lg p-4">
            <div className="flex items-start gap-2 mb-3">
              <AlertTriangle size={18} className="text-amber-400 mt-0.5 shrink-0" />
              <div>
                <p className="text-sm font-medium text-amber-300">
                  Rotating breaks every connected MCP client immediately
                </p>
                <ul className="text-xs text-amber-400/80 mt-1.5 space-y-1 list-disc pl-4">
                  <li>
                    The old <code>/private_&lt;token&gt;/mcp/</code> URL stops working the
                    instant you confirm — there is no grace period.
                  </li>
                  <li>
                    Every claude.ai connector must be re-pointed at the NEW URL by hand,
                    and any Cloudflare Worker <code>UPSTREAM_TOKEN</code> secret must be
                    updated in lockstep.
                  </li>
                  <li>The MCP server restarts, so in-flight sessions are dropped.</li>
                  <li>The full value is shown once. It cannot be recovered afterwards.</li>
                </ul>
              </div>
            </div>

            <label className="flex items-start gap-2 mb-3 text-xs text-amber-300 cursor-pointer">
              <input
                type="checkbox"
                checked={acknowledged}
                onChange={(e) => setAcknowledged(e.target.checked)}
                className="mt-0.5"
              />
              <span>
                I understand that connected clients will break until I re-point them at the
                new URL.
              </span>
            </label>

            <div className="flex gap-2">
              <button
                onClick={() => rotateMutation.mutate()}
                disabled={rotateMutation.isPending || !acknowledged}
                className="flex items-center gap-2 px-4 py-2 bg-amber-600 hover:bg-amber-500 disabled:bg-gray-700 disabled:text-gray-500 text-white font-medium rounded-lg text-sm transition-colors"
              >
                {rotateMutation.isPending ? (
                  <Loader2 size={14} className="animate-spin" />
                ) : (
                  <RefreshCw size={14} />
                )}
                <span>Rotate now</span>
              </button>
              <button
                onClick={() => {
                  setConfirmRotate(false);
                  setAcknowledged(false);
                }}
                className="px-4 py-2 bg-gray-800 hover:bg-gray-700 text-gray-400 font-medium rounded-lg text-sm transition-colors"
              >
                Cancel
              </button>
            </div>

            {rotateMutation.isError && (
              <div className="mt-3 text-sm text-red-400">{rotateMutation.error.message}</div>
            )}
          </div>
        )}
      </div>

      {history.length > 0 && (
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-6 max-w-xl">
          <div className="flex items-center gap-2 mb-4">
            <Clock size={18} className="text-gray-400" />
            <h3 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">
              Rotation History
            </h3>
          </div>
          <ul className="space-y-2">
            {history.map((entry, i) => (
              <li
                key={i}
                className="flex items-center justify-between gap-3 px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg font-mono text-xs text-gray-400"
              >
                <span className="break-all">{maskedOf(entry)}</span>
                <span className="text-gray-600 shrink-0">{rotatedAtOf(entry)}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
