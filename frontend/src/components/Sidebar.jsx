import React, { useState } from 'react';
import { NavLink, useNavigate } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  LayoutDashboard,
  Wrench,
  Link,
  KeyRound,
  ScrollText,
  Shield,
  Settings,
  LogOut,
  Loader2,
} from 'lucide-react';
import { apiGet, logout } from '../api';

const navItems = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard },
  { to: '/tools', label: 'Tools', icon: Wrench },
  { to: '/config', label: 'Connection', icon: Link },
  { to: '/tokens', label: 'Tokens', icon: KeyRound },
  { to: '/logs', label: 'Logs', icon: ScrollText },
  { to: '/permissions', label: 'Permissions', icon: Shield },
  { to: '/settings', label: 'Settings', icon: Settings },
];

// This is the Podman build of the shared console, so the correct branding
// lives here in the SOURCE. The reference carried every product's title and
// relied on a build-time `sed` in the Dockerfile to rewrite them, so any local
// `npm run build` shipped the wrong product name. Keep this map Podman-only.
const APP_TITLES = {
  podman: 'Podman MCP Admin',
};
const DEFAULT_APP_TITLE = 'Podman MCP Admin';

export default function Sidebar() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [loggingOut, setLoggingOut] = useState(false);

  const { data: health } = useQuery({
    queryKey: ['health'],
    queryFn: () => apiGet('/health'),
    staleTime: 30_000,
  });

  const appType = health?.app_type || 'podman';
  const appTitle = APP_TITLES[appType] || DEFAULT_APP_TITLE;

  // Dropping the localStorage token is only half a logout: the login endpoint
  // also set an httpOnly cookie that AuthMiddleware accepts on its own, and JS
  // cannot delete it. POST /api/auth/logout expires that cookie and revokes
  // every issued JWT; without it the console showed the login form while the
  // browser still held a live admin session. `logout()` never rejects, so a
  // dead backend cannot trap the operator in a signed-in shell.
  async function handleLogout() {
    if (loggingOut) return;
    setLoggingOut(true);
    try {
      await logout();
      // Wipe the cached health/settings/tokens views so the next login does not
      // flash the previous session's data before refetching.
      queryClient.clear();
      navigate('/login', { replace: true });
    } finally {
      setLoggingOut(false);
    }
  }

  return (
    <aside className="w-60 bg-gray-900 border-r border-gray-800 flex flex-col h-screen fixed left-0 top-0">
      <div className="p-5 border-b border-gray-800">
        <h1 className="text-lg font-bold text-gray-100 tracking-tight">{appTitle}</h1>
        {health?.namespace && (
          <p className="text-xs text-gray-500 mt-1 font-mono">{health.namespace}</p>
        )}
      </div>

      <nav className="flex-1 py-3 px-3 space-y-1 overflow-y-auto">
        {navItems.map(({ to, label, icon: Icon }) => (
          <NavLink
            key={to}
            to={to}
            end={to === '/'}
            className={({ isActive }) =>
              `flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors ${
                isActive
                  ? 'bg-brand-600/20 text-brand-400 border border-brand-600/30'
                  : 'text-gray-400 hover:text-gray-200 hover:bg-gray-800'
              }`
            }
          >
            <Icon size={18} />
            <span>{label}</span>
          </NavLink>
        ))}
      </nav>

      <div className="p-3 border-t border-gray-800">
        {health?.version && (
          <div className="px-3 py-1.5 mb-2 text-xs text-gray-600 font-mono">
            Podman v{health.version}
          </div>
        )}
        <button
          onClick={handleLogout}
          disabled={loggingOut}
          className="flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium text-gray-500 hover:text-red-400 hover:bg-gray-800 disabled:opacity-50 transition-colors w-full"
        >
          {loggingOut ? <Loader2 size={18} className="animate-spin" /> : <LogOut size={18} />}
          <span>{loggingOut ? 'Signing out…' : 'Logout'}</span>
        </button>
      </div>
    </aside>
  );
}
