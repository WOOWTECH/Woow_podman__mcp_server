import React, { useEffect, useRef, useState } from 'react';
import { Routes, Route, Navigate, useLocation } from 'react-router-dom';
import { Menu } from 'lucide-react';
import { QueryClient, QueryClientProvider, useQuery, useQueryClient } from '@tanstack/react-query';
import { apiDelete, apiGet, apiPatch, apiPost, DEFAULT_PROFILE_ID, getSelectedProfile, getToken, setSelectedProfile } from './api';
import { AppConfigProvider, useAppConfig } from './appConfigContext';
import Sidebar from './components/Sidebar';
import ProfileChooser from './components/ProfileChooser';
import Dashboard from './pages/Dashboard';
import ToolManager from './pages/ToolManager';
import ConnectionConfig from './pages/ConnectionConfig';
import TokenManager from './pages/TokenManager';
import LogViewer from './pages/LogViewer';
import SettingsPage from './pages/SettingsPage';
import LoginPage from './pages/LoginPage';
import PermissionEditor from './pages/PermissionEditor';

function ProtectedRoute({ children }) {
  if (!getToken()) return <Navigate to="/login" replace />;
  return children;
}

export function AppLayout({ children, profileChooser }) {
  const { brandTitle, menuIcon: MenuIcon = Menu } = useAppConfig();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  return (
    <div className="flex min-h-screen bg-bg">
      <Sidebar open={sidebarOpen} onClose={() => setSidebarOpen(false)} />
      {sidebarOpen && <div className="fixed inset-0 bg-black/50 z-30 lg:hidden" onClick={() => setSidebarOpen(false)} />}
      <main className="flex-1 min-w-0 lg:ml-60 p-4 sm:p-6 lg:p-8 overflow-y-auto min-h-screen">
        <div className="flex items-center gap-3 mb-4 lg:hidden">
          <button onClick={() => setSidebarOpen(true)} className="p-2 text-muted hover:text-ink-2 bg-panel rounded-lg shrink-0">
            <MenuIcon size={20} />
          </button>
          <h1 className="text-lg font-bold text-ink truncate">{brandTitle}</h1>
        </div>
        <div className="max-w-6xl mx-auto">
          {profileChooser}
          {children}
        </div>
      </main>
    </div>
  );
}

function ProfileWorkspace({ children }) {
  const outerClient = useQueryClient();
  const clients = useRef(new Map());
  const [selectedId, setSelectedId] = useState(getSelectedProfile);
  const { data, isPending, isError, error, refetch } = useQuery({
    queryKey: ['profiles'],
    queryFn: () => apiGet('/profiles'),
  });
  const profiles = data?.profiles || [];

  function select(id) {
    if (id === selectedId) return;
    setSelectedProfile(id);
    setSelectedId(id);
  }

  useEffect(() => {
    if (profiles.length && !profiles.some(profile => profile.id === selectedId)) {
      setSelectedProfile(DEFAULT_PROFILE_ID);
      setSelectedId(DEFAULT_PROFILE_ID);
    }
  }, [profiles, selectedId]);

  async function refreshProfiles() {
    await outerClient.invalidateQueries({ queryKey: ['profiles'] });
  }

  async function create(label) {
    const created = await apiPost('/profiles', { label });
    if (!created?.id) throw new Error('The server did not return a profile ID. Reload the profile list.');
    outerClient.setQueryData(['profiles'], current => ({
      ...current,
      profiles: [...(current?.profiles || []), created],
    }));
    select(created.id);
    await refreshProfiles();
  }

  async function rename(id, label) {
    const updated = await apiPatch(`/profiles/${encodeURIComponent(id)}`, { label });
    outerClient.setQueryData(['profiles'], current => ({
      ...current,
      profiles: current.profiles.map(profile => profile.id === id ? { ...profile, ...updated, label } : profile),
    }));
    await refreshProfiles();
  }

  async function remove(id) {
    await apiDelete(`/profiles/${encodeURIComponent(id)}`);
    clients.current.get(id)?.clear();
    clients.current.delete(id);
    outerClient.setQueryData(['profiles'], current => ({
      ...current,
      profiles: current.profiles.filter(profile => profile.id !== id),
    }));
    select(DEFAULT_PROFILE_ID);
    await refreshProfiles();
  }

  if (isPending) return <div role="status" className="p-6 text-muted">Loading profiles…</div>;
  if (isError && !data) return (
    <div role="alert" className="p-6 text-red-600">
      Could not load profiles: {error.message}
      <button type="button" onClick={() => refetch()} className="ml-3 text-brand-400">Retry</button>
    </div>
  );
  if (!profiles.some(profile => profile.id === DEFAULT_PROFILE_ID)) {
    return <div role="alert" className="p-6 text-red-600">The default profile is missing.</div>;
  }
  if (!profiles.some(profile => profile.id === selectedId)) return <div role="status" className="p-6 text-muted">Selecting default profile…</div>;

  if (!clients.current.has(selectedId)) {
    clients.current.set(selectedId, new QueryClient({ defaultOptions: outerClient.getDefaultOptions() }));
  }
  return (
    <QueryClientProvider key={selectedId} client={clients.current.get(selectedId)}>
      <AppLayout profileChooser={
        <>
          {isError && <p role="alert" className="mb-2 text-sm text-red-600">Profile list refresh failed: {error.message}
            <button type="button" onClick={() => refetch()} className="ml-2 text-brand-400">Retry</button>
          </p>}
          <ProfileChooser profiles={profiles} selectedId={selectedId} onSelect={select}
            onCreate={create} onRename={rename} onDelete={remove} />
        </>
      }>
        {children}
      </AppLayout>
    </QueryClientProvider>
  );
}

function Shell() {
  const location = useLocation();
  if (location.pathname === '/login') {
    return (
      <Routes>
        <Route path="/login" element={<LoginPage />} />
      </Routes>
    );
  }
  return (
    <ProtectedRoute>
      <ProfileWorkspace>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/config" element={<ConnectionConfig />} />
          <Route path="/tools" element={<ToolManager />} />
          <Route path="/tokens" element={<TokenManager />} />
          <Route path="/logs" element={<LogViewer />} />
          <Route path="/permissions" element={<PermissionEditor />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </ProfileWorkspace>
    </ProtectedRoute>
  );
}

export default function AdminApp({ config }) {
  const ShellComponent = config.shell || Shell;
  return (
    <AppConfigProvider config={config}>
      <ShellComponent />
    </AppConfigProvider>
  );
}
