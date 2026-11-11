import React, { useCallback, useEffect, useRef, useState } from 'react';
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
  Terminal,
  Link2,
} from 'lucide-react';
import { apiGet, apiPost } from '../api';
import useDurableOperation from '../useDurableOperation';
import OperationRecovery from '../components/OperationRecovery';
import { useAppConfig } from '../appConfigContext';
import { claudeCodeAddCommand, mcpUrl } from '../mcpUrl';

export default function TokenManager() {
  const queryClient = useQueryClient();
  const { appName } = useAppConfig();
  const [showToken, setShowToken] = useState(false);
  const [currentToken, setCurrentToken] = useState(null);
  const [copied, setCopied] = useState(false);
  const [copiedUrl, setCopiedUrl] = useState(false);
  const [copiedCmd, setCopiedCmd] = useState(false);
  const [confirmRevision, setConfirmRevision] = useState(null);
  const [conflict, setConflict] = useState(false);
  const [confirmRotate, setConfirmRotate] = useState(false);
  const [confirmRevoke, setConfirmRevoke] = useState(false);
  const { data: info } = useQuery({ queryKey: ['tokens'], queryFn: () => apiGet('/tokens') });
  const rotateOperation = useDurableOperation('/tokens/rotate', info?.configuration);
  const revokeOperation = useDurableOperation('/tokens/revoke', info?.configuration);
  function refreshAccess() {
    return Promise.all(['settings', 'tokens', 'capabilities'].map(key => queryClient.invalidateQueries({ queryKey: [key] })));
  }
  const [revealError, setRevealError] = useState('');
  const [revealing, setRevealing] = useState(false);
  const revealGeneration = useRef(0);
  const revealInvalidationReason = useRef(null);
  const revealTimer = useRef(null);
  const hideRevealedToken = useCallback((reason) => {
    revealGeneration.current += 1;
    revealInvalidationReason.current = reason === 'metadata' ? 'metadata' : null;
    if (revealTimer.current !== null) clearTimeout(revealTimer.current);
    revealTimer.current = null;
    setCurrentToken(null);
    setShowToken(false);
  }, []);

  const { data: settings, isLoading } = useQuery({
    queryKey: ['settings'],
    queryFn: () => apiGet('/settings'),
  });

  const rotateMutation = useMutation({
    retry: false,
    mutationFn: () => rotateOperation.run(confirmRevision),
    onError: (error) => { if (error.code === 'config_conflict') setConflict(true); },
    onSuccess: (result) => {
      hideRevealedToken();
      setRevealError('');
      refreshAccess();
      setConfirmRotate(false);
    },
  });

  const revokeMutation = useMutation({
    retry: false,
    mutationFn: () => revokeOperation.run(confirmRevision),
    onError: (error) => { if (error.code === 'config_conflict') setConflict(true); },
    onSuccess: () => {
      hideRevealedToken(); setRevealError('');
      setConfirmRevoke(false); setConfirmRotate(false);
      refreshAccess();
    },
  });
  const busy = rotateMutation.isPending || revokeMutation.isPending || revealing;
  const revoked = info?.state === 'revoked';
  const maskedToken = settings?.mcp_auth_token_masked || 'No token configured';
  const tokenConfigured = !revoked && Boolean(info?.token_set ?? settings?.mcp_auth_token_set);

  // Another administrator may rotate/revoke while this page stays mounted.
  // A metadata refresh must never leave a previously revealed credential visible.
  useEffect(() => {
    hideRevealedToken('metadata');
  }, [hideRevealedToken, info?.state, info?.last_rotated, info?.token_masked, settings?.mcp_auth_token_masked]);

  useEffect(() => {
    const onVisibilityChange = () => { if (document.hidden) hideRevealedToken(); };
    window.addEventListener('blur', hideRevealedToken);
    document.addEventListener('visibilitychange', onVisibilityChange);
    return () => {
      window.removeEventListener('blur', hideRevealedToken);
      document.removeEventListener('visibilitychange', onVisibilityChange);
      revealGeneration.current += 1;
      if (revealTimer.current !== null) clearTimeout(revealTimer.current);
      revealTimer.current = null;
    };
  }, [hideRevealedToken]);

  // 完整 MCP 網址與 Claude Code 連線範例（各 app 共用）：my-<app>-mcp 由 appConfig.appName 組出，host 取自目前瀏覽的
  // origin。未顯示 token 時以 <TOKEN> 佔位；按「顯示」之後才帶入真實 token——與 token 本身同一個揭露狀態，60 秒或離開
  // 視窗就一起隱藏（#85 的 hideRevealedToken）。
  const serverName = `my-${appName}-mcp`;
  const origin = typeof window !== 'undefined' ? window.location.origin : '';
  const effectiveToken = revoked ? '' : currentToken || '';
  const displayUrl = mcpUrl(origin, showToken && effectiveToken ? effectiveToken : null);
  const addCommand = claudeCodeAddCommand(serverName, displayUrl);

  async function copyToClipboard(value) {
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      // Fallback：clipboard API 不可用時（如非 https 環境）改用 textarea + execCommand
      const ta = document.createElement('textarea');
      ta.value = value;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
    }
  }

  async function revealCurrentToken() {
    if (revoked || busy) return '';
    setRevealing(true);
    setRevealError('');
    const generation = revealGeneration.current;
    try {
      const result = await apiPost('/settings/mcp_auth_token/reveal');
      if (generation !== revealGeneration.current) {
        if (revealInvalidationReason.current === 'metadata' && !document.hidden) {
          setRevealError('Token changed while loading. Please try again.');
        }
        return '';
      }
      if (document.hidden) return '';
      const value = result.value || '';
      if (value) {
        if (revealTimer.current !== null) clearTimeout(revealTimer.current);
        revealTimer.current = setTimeout(hideRevealedToken, 60000);
        setCurrentToken(value);
      }
      return value;
    } finally {
      setRevealing(false);
    }
  }

  async function toggleTokenVisibility() {
    if (revoked || busy) return;
    if (showToken) {
      hideRevealedToken();
      return;
    }
    try {
      if (await revealCurrentToken()) setShowToken(true);
    } catch (err) {
      setRevealError(err.message);
    }
  }

  async function handleCopy() {
    if (revoked || busy) return;
    try {
      const value = tokenConfigured ? await revealCurrentToken() : '';
      if (!value) return;
      await copyToClipboard(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      setRevealError(err.message);
    }
  }

  async function handleCopyUrl() {
    if (revoked || busy) return;
    try {
      const tokenForUrl = tokenConfigured ? await revealCurrentToken() : '';
      if (!tokenForUrl) return;
      await copyToClipboard(mcpUrl(origin, tokenForUrl));
      setCopiedUrl(true);
      setTimeout(() => setCopiedUrl(false), 2000);
    } catch (err) {
      setRevealError(err.message);
    }
  }

  async function handleCopyCommand() {
    if (revoked || busy) return;
    try {
      const tokenForCommand = tokenConfigured ? await revealCurrentToken() : '';
      if (!tokenForCommand) return;
      const command = claudeCodeAddCommand(serverName, mcpUrl(origin, tokenForCommand));
      await copyToClipboard(command);
      setCopiedCmd(true);
      setTimeout(() => setCopiedCmd(false), 2000);
    } catch (err) {
      setRevealError(err.message);
    }
  }

  const history = info?.history || [];

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="animate-spin text-muted" size={24} />
      </div>
    );
  }

  return (
    <div>
      <div className="mb-6">
        <h2 className="text-xl sm:text-2xl font-bold text-ink">Token Manager</h2>
        <p className="text-sm text-muted mt-1">This profile has one token shared by its MCP clients. Rotation or revocation affects this profile.</p>
      </div>

      <div className="bg-bg border border-border rounded-xl p-6 max-w-xl mb-6">
        <div className="flex items-center gap-2 mb-4">
          <KeyRound size={18} className="text-muted" />
          <h3 className="text-sm font-semibold text-ink-2 uppercase tracking-wide">
            Profile MCP Token
          </h3>
        </div>

        {revealError && <p className="mb-3 text-sm text-red-600">{revealError}</p>}

        <div className="flex items-center gap-2 mb-4">
          <div className="flex-1 px-3 py-2.5 bg-panel border border-border rounded-lg font-mono text-sm text-ink-2 overflow-hidden">
            {revoked ? "Revoked — this profile's clients blocked" : showToken ? (effectiveToken || 'No token') : maskedToken}
          </div>
          <button
            onClick={toggleTokenVisibility}
            disabled={revoked || busy || !tokenConfigured}
            className="p-2.5 bg-panel hover:bg-field text-muted hover:text-ink-2 border border-border rounded-lg transition-colors"
            title={showToken ? 'Hide token' : 'Show token'}
          >
            {showToken ? <EyeOff size={16} /> : <Eye size={16} />}
          </button>
          <button
            onClick={handleCopy}
            disabled={revoked || busy || !tokenConfigured}
            className="p-2.5 bg-panel hover:bg-field disabled:opacity-50 text-muted hover:text-ink-2 border border-border rounded-lg transition-colors"
            title="Copy token"
          >
            {copied ? <CheckCircle2 size={16} className="text-brand-400" /> : <Copy size={16} />}
          </button>
        </div>

        {rotateMutation.isSuccess && !revoked && (
          <div className="mb-4 flex items-center gap-2 px-3 py-2.5 rounded-lg text-sm bg-emerald-500/10 border border-emerald-500/20 text-emerald-600">
            <CheckCircle2 size={16} />
            <span>New token generated for this profile. Copy it and update its MCP clients.</span>
          </div>
        )}

        <div className="flex items-center gap-2 mb-2">
          <Link2 size={16} className="text-muted" />
          <h4 className="text-xs font-semibold text-ink-2 uppercase tracking-wide">MCP URL (Streamable HTTP)</h4>
        </div>
        <div className="flex items-center gap-2 mb-4">
          <div className="flex-1 px-3 py-2.5 bg-panel border border-border rounded-lg font-mono text-xs text-ink-2 overflow-x-auto whitespace-nowrap" data-testid="mcp-url">
            {revoked ? 'No MCP URL while access is revoked' : tokenConfigured ? displayUrl : 'No token configured — Rotate Token issues one'}
          </div>
          <button
            onClick={toggleTokenVisibility}
            disabled={revoked || busy || !tokenConfigured}
            className="p-2.5 bg-panel hover:bg-field text-muted hover:text-ink-2 border border-border rounded-lg transition-colors"
            title={showToken ? 'Hide MCP URL' : 'Show MCP URL'}
          >
            {showToken ? <EyeOff size={16} /> : <Eye size={16} />}
          </button>
          <button
            onClick={handleCopyUrl}
            disabled={revoked || busy || !tokenConfigured}
            className="p-2.5 bg-panel hover:bg-field disabled:opacity-50 text-muted hover:text-ink-2 border border-border rounded-lg transition-colors"
            title="Copy MCP URL"
          >
            {copiedUrl ? <CheckCircle2 size={16} className="text-brand-400" /> : <Copy size={16} />}
          </button>
        </div>

        <p className="text-xs text-muted-2 mb-4">
          MCP clients for this profile connect to this URL; the token in it is the credential. A new profile gets its own token automatically. Revealed tokens and URLs are hidden after 60 seconds or when you leave this window; Copy always uses the real token.
        </p>

        {!confirmRotate ? (
          <button
            onClick={() => { setConfirmRevision(info?.configuration?.revision); setConfirmRotate(true); setConfirmRevoke(false); }}
            disabled={busy}
            className="flex items-center gap-2 px-4 py-2.5 bg-amber-600/20 hover:bg-amber-600/30 text-amber-600 border border-amber-600/30 font-medium rounded-lg transition-colors"
          >
            <RefreshCw size={16} />
            <span>Rotate Token</span>
          </button>
        ) : (
          <div className="border border-amber-600/30 bg-amber-600/10 rounded-lg p-4">
            <div className="flex items-start gap-2 mb-3">
              <AlertTriangle size={18} className="text-amber-600 mt-0.5 shrink-0" />
              <div>
                <p className="text-sm font-medium text-amber-300">Confirm Token Rotation</p>
                <p className="text-xs text-amber-600/70 mt-0.5">
                  This will generate a new token. Clients of this profile using the old token will lose access, including existing proxy streams. Already dispatched backend work may continue.
                </p>
              </div>
            </div>
            <div className="flex gap-2">
              <button
                onClick={() => rotateMutation.mutate()}
                disabled={busy || conflict}
                className="flex items-center gap-2 px-4 py-2 bg-amber-600 hover:bg-amber-500 disabled:bg-field text-white font-medium rounded-lg text-sm transition-colors"
              >
                {rotateMutation.isPending ? (
                  <Loader2 size={14} className="animate-spin" />
                ) : (
                  <RefreshCw size={14} />
                )}
                <span>Confirm Rotate</span>
              </button>
              <button
                onClick={() => setConfirmRotate(false)}
                className="px-4 py-2 bg-panel hover:bg-field text-muted font-medium rounded-lg text-sm transition-colors"
              >
                Cancel
              </button>
            </div>

            {rotateMutation.isError && (
              <div className="mt-3 text-sm text-red-600">{rotateMutation.error.message}</div>
            )}
          </div>
        )}
      </div>

      <div className="bg-bg border border-border rounded-xl p-6 max-w-xl mb-6">
        <h3 className="font-semibold mb-2">Revoke this profile's access</h3>
        <p className="text-sm text-muted mb-4">Revocation blocks this profile's new requests and closes its proxy streams. It survives restarts. Already dispatched backend work may continue; completed effects are not undone. Issue a new token to restore access.</p>
        {!confirmRevoke ? <button onClick={() => { setConfirmRevision(info?.configuration?.revision); setConfirmRevoke(true); setConfirmRotate(false); }} disabled={busy || revoked || !tokenConfigured}
          className="px-4 py-2 border border-red-600/30 text-red-600 rounded-lg disabled:opacity-50">Revoke Token</button>
          : <div role="alert"><p className="text-sm mb-3">Revoke access for every client using this profile's token?</p>
            <button onClick={() => revokeMutation.mutate()} disabled={busy || conflict} className="px-4 py-2 bg-red-600 text-white rounded-lg mr-2">Confirm Revoke</button>
            <button onClick={() => setConfirmRevoke(false)} disabled={busy} className="px-4 py-2 border border-border rounded-lg">Cancel</button>
          </div>}
        {revokeMutation.isError && <p className="mt-3 text-red-600">{revokeMutation.error.message}</p>}
        {revoked && <p className="mt-3 text-sm" role="status">This profile's access is revoked. Rotate Token issues a new credential.</p>}
      </div>
      {conflict && <div role="alert">設定版本已變更，請重新載入再確認。
        <button onClick={async () => { await refreshAccess(); if (rotateMutation.error?.code === 'config_conflict') rotateOperation.clear(); if (revokeMutation.error?.code === 'config_conflict') revokeOperation.clear(); setConfirmRotate(false); setConfirmRevoke(false); setConflict(false); }}>重新載入設定</button>
      </div>}
      <OperationRecovery operation={rotateOperation} onReload={refreshAccess} />
      <OperationRecovery operation={revokeOperation} onReload={refreshAccess} />
      <div className="bg-bg border border-border rounded-xl p-6 max-w-xl mb-6">
        <h3 className="font-semibold mb-3">Recent token changes</h3>
        {history.length ? <ul className="text-sm text-muted space-y-2">{history.map((item, index) => <li key={index}>
          {item.action || 'rotate'} · {item.rotated_at || item.created_at || 'Time unavailable'} · {item.previous_token_masked || item.token_masked || item.token_prefix || ''}
        </li>)}</ul> : <p className="text-sm text-muted">No recorded changes.</p>}
      </div>

      <div className="bg-bg border border-border rounded-xl p-6 max-w-xl mb-6">
        <div className="flex items-center gap-2 mb-4">
          <Terminal size={18} className="text-muted" />
          <h3 className="text-sm font-semibold text-ink-2 uppercase tracking-wide">
            Add to Claude Code
          </h3>
        </div>

        <p className="text-xs text-muted-2 mb-3">
          Register this MCP server with Claude Code over the streamable HTTP transport.
          The same command shape works for every app — run:
        </p>

        <div className="flex items-stretch gap-2">
          <div className="flex-1 px-3 py-2.5 bg-panel border border-border rounded-lg font-mono text-xs text-ink-2 overflow-x-auto whitespace-pre">
            {addCommand}
          </div>
          <button
            onClick={handleCopyCommand}
            className="shrink-0 p-2.5 bg-panel hover:bg-field text-muted hover:text-ink-2 border border-border rounded-lg transition-colors"
            title="Copy command"
            disabled={revoked || busy || !tokenConfigured}
          >
            {copiedCmd ? <CheckCircle2 size={16} className="text-brand-400" /> : <Copy size={16} />}
          </button>
        </div>

        <p className="text-xs text-muted-2 mt-3">
          {showToken
            ? 'The command above includes your real token — treat it as a secret.'
            : 'Reveal the token above to see it inline. Copy always uses your real token.'}
        </p>
      </div>
    </div>
  );
}
