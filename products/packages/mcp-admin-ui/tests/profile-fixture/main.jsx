import React from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { LayoutDashboard, Link, Wrench, KeyRound, Settings, ScrollText } from 'lucide-react';
import AdminApp from '../../src/AdminApp.jsx';

const config = {
  appName: 'fixture',
  brandTitle: 'Profile fixture',
  nav: [{ items: [
    { to: '/', label: 'Dashboard', icon: LayoutDashboard },
    { to: '/config', label: 'Connection', icon: Link },
    { to: '/tools', label: 'Tools', icon: Wrench },
    { to: '/tokens', label: 'Tokens', icon: KeyRound },
    { to: '/settings', label: 'Settings', icon: Settings },
    { to: '/logs', label: 'Logs', icon: ScrollText },
  ] }],
  healthBadges: [],
  dashboard: { subtitle: 'Fixture', buildSections: () => [] },
  connection: {
    description: 'Fixture connection',
    saveEndpoint: '/config/connection',
    sections: [{ fields: [{ name: 'endpoint', label: 'Endpoint', type: 'text' }] }],
  },
  toolTabs: [{ key: 'tools', label: 'Tools', endpoint: '/tools', itemsKey: 'tools', grouped: false }],
};

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false, staleTime: 10_000 } },
});
createRoot(document.getElementById('root')).render(
  <QueryClientProvider client={queryClient}>
    <BrowserRouter><AdminApp config={config} /></BrowserRouter>
  </QueryClientProvider>,
);
