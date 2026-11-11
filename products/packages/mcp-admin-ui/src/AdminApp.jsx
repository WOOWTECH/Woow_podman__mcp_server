import React, { useState } from 'react';
import { Routes, Route, Navigate, useLocation } from 'react-router-dom';
import { Menu } from 'lucide-react';
import { getToken } from './api';
import { AppConfigProvider, useAppConfig } from './appConfigContext';
import Sidebar from './components/Sidebar';
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

export function AppLayout({ children }) {
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
        <div className="max-w-6xl mx-auto">{children}</div>
      </main>
    </div>
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
      <AppLayout>
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
      </AppLayout>
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
