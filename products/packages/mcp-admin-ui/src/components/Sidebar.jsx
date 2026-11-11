import React from 'react';
import { NavLink, useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { LogOut, X } from 'lucide-react';
import { apiGet, clearToken } from '../api';
import { useAppConfig } from '../appConfigContext';
import { useCapabilities } from '../useCapabilities';
import { capabilityNav } from '../capabilities';

function HealthDot({ ok }) {
  return (
    <span
      className={`inline-block w-2 h-2 rounded-full ${
        ok === true ? 'bg-emerald-500 shadow-lg shadow-emerald-500/50' : ok === false ? 'bg-red-500 shadow-lg shadow-red-500/50' : 'bg-muted-2'
      }`}
    />
  );
}

export default function Sidebar({ open, onClose }) {
  const { brandTitle, nav, healthBadges, healthQuery, logout, closeIcon: CloseIcon = X, logoutIcon: LogoutIcon = LogOut } = useAppConfig();
  const navigate = useNavigate();
  const { data: capabilities } = useCapabilities();
  const { data: health } = useQuery({ queryKey: ['health'], queryFn: healthQuery || (() => apiGet('/health')), staleTime: 30_000 });

  async function handleLogout() {
    if (logout) {
      await logout();
      return;
    }
    clearToken();
    navigate('/login');
  }
  function handleNavClick() {
    if (onClose) onClose();
  }

  return (
    <aside
      className={`w-60 bg-bg border-r border-border flex flex-col h-screen fixed left-0 top-0 z-40 transition-transform duration-200 ease-in-out ${
        open ? 'translate-x-0' : '-translate-x-full'
      } lg:translate-x-0`}
    >
      <div className="p-5 border-b border-border">
        <div className="flex items-center justify-between">
          <h1 className="text-lg font-bold text-ink tracking-tight">{brandTitle}</h1>
          <button onClick={onClose} className="p-1 text-muted hover:text-ink-2 lg:hidden">
            <CloseIcon size={18} />
          </button>
        </div>
        {healthBadges.length > 0 && (
          <div className="flex items-center gap-3 mt-2">
            {healthBadges.map((b) => (
              <span key={b.key} className="flex items-center gap-1 text-xs text-muted">
                <span>{b.label}</span>
                <HealthDot ok={health?.[b.key]?.healthy} />
              </span>
            ))}
          </div>
        )}
      </div>

      <nav className="flex-1 py-3 px-3 space-y-1 overflow-y-auto">
        {capabilityNav(nav, capabilities).map((section, sIdx) => (
          <div key={sIdx}>
            {section.label && (
              <div className="px-3 pt-4 pb-1.5 text-[10px] font-semibold text-muted-2 uppercase tracking-widest">{section.label}</div>
            )}
            {section.items.map(({ to, label, icon: Icon }) => (
              <NavLink
                key={to}
                to={to}
                end={to === '/'}
                onClick={handleNavClick}
                className={({ isActive }) =>
                  `flex items-center gap-3 px-3 py-2 rounded-lg text-sm font-medium transition-colors ${
                    isActive ? 'bg-brand-600/20 text-brand-400 border border-brand-600/30' : 'text-muted hover:text-ink-2 hover:bg-panel'
                  }`
                }
              >
                <Icon size={16} />
                <span>{label}</span>
              </NavLink>
            ))}
          </div>
        ))}
      </nav>

      <div className="p-3 border-t border-border">
        {health?.version && <div className="px-3 py-1.5 mb-2 text-xs text-muted-2 font-mono">v{health.version}</div>}
        <button
          onClick={handleLogout}
          className="flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium text-muted hover:text-red-600 hover:bg-panel transition-colors w-full"
        >
          <LogoutIcon size={18} />
          <span>Logout</span>
        </button>
      </div>
    </aside>
  );
}
