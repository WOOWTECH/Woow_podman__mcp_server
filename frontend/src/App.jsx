import React from 'react';
import { Routes, Route, Navigate, useLocation } from 'react-router-dom';
import { getToken } from './api';
import Sidebar from './components/Sidebar';
import ErrorBoundary from './components/ErrorBoundary';
import Dashboard from './pages/Dashboard';
import ToolManager from './pages/ToolManager';
import ConnectionConfig from './pages/ConnectionConfig';
import TokenManager from './pages/TokenManager';
import LogViewer from './pages/LogViewer';
import PermissionEditor from './pages/PermissionEditor';
import SettingsPage from './pages/SettingsPage';
import LoginPage from './pages/LoginPage';

function ProtectedRoute({ children }) {
  const token = getToken();
  if (!token) {
    return <Navigate to="/login" replace />;
  }
  return children;
}

function AppLayout({ children }) {
  return (
    <div className="flex min-h-screen bg-gray-950">
      <Sidebar />
      <main className="flex-1 ml-60 p-8 overflow-y-auto">
        <div className="max-w-6xl mx-auto">{children}</div>
      </main>
    </div>
  );
}

export default function App() {
  const location = useLocation();
  const isLoginPage = location.pathname === '/login';

  if (isLoginPage) {
    return (
      <ErrorBoundary resetKey={location.pathname}>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
        </Routes>
      </ErrorBoundary>
    );
  }

  return (
    <ProtectedRoute>
      <AppLayout>
        {/* Load-bearing: without a boundary here a render error in ANY page
            unmounts the React root and blanks the entire console. Keyed on the
            path so navigating away clears a stuck page. */}
        <ErrorBoundary resetKey={location.pathname}>
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/tools" element={<ToolManager />} />
            <Route path="/config" element={<ConnectionConfig />} />
            <Route path="/tokens" element={<TokenManager />} />
            <Route path="/logs" element={<LogViewer />} />
            <Route path="/permissions" element={<PermissionEditor />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </ErrorBoundary>
      </AppLayout>
    </ProtectedRoute>
  );
}
