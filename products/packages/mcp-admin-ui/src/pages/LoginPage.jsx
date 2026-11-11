import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { KeyRound, AlertCircle, Loader2 } from 'lucide-react';
import { setToken } from '../api';

export default function LoginPage() {
  const navigate = useNavigate();
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    setError('');
    setLoading(true);

    try {
      const response = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password }),
      });

      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || 'Invalid password');
      }

      const data = await response.json();
      setToken(data.token || data.access_token);
      navigate('/', { replace: true });
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="min-h-screen bg-bg flex items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <div className="text-center mb-8">
          <div className="inline-flex items-center justify-center w-16 h-16 rounded-2xl bg-brand-600/20 border border-brand-600/30 mb-4">
            <KeyRound className="text-brand-400" size={28} />
          </div>
          <h1 className="text-2xl font-bold text-ink">MCP Admin</h1>
          <p className="text-sm text-muted mt-1">Enter your admin password to continue</p>
        </div>

        <form onSubmit={handleSubmit} className="bg-bg border border-border rounded-xl p-6 space-y-4">
          {error && (
            <div className="flex items-center gap-2 px-3 py-2.5 bg-red-500/10 border border-red-500/20 rounded-lg text-sm text-red-600">
              <AlertCircle size={16} className="shrink-0" />
              <span>{error}</span>
            </div>
          )}

          <div>
            <label htmlFor="password" className="block text-sm font-medium text-muted mb-1.5">
              Password
            </label>
            <input
              id="password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="Admin password"
              autoFocus
              required
              className="w-full px-3 py-2.5 bg-panel border border-border rounded-lg text-ink placeholder-muted-2 focus:outline-none focus:border-brand-500 focus:ring-1 focus:ring-brand-500 transition-colors"
            />
          </div>

          <button
            type="submit"
            disabled={loading || !password}
            className="w-full flex items-center justify-center gap-2 px-4 py-2.5 bg-brand-600 hover:bg-brand-500 disabled:bg-field disabled:text-muted text-white font-medium rounded-lg transition-colors"
          >
            {loading ? (
              <>
                <Loader2 size={18} className="animate-spin" />
                <span>Signing in...</span>
              </>
            ) : (
              <span>Sign in</span>
            )}
          </button>
        </form>
      </div>
    </div>
  );
}
