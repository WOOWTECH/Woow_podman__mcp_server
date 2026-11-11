import React, { createContext, useContext } from 'react';

const AppConfigContext = createContext(null);

export function AppConfigProvider({ config, children }) {
  return <AppConfigContext.Provider value={config}>{children}</AppConfigContext.Provider>;
}

export function useAppConfig() {
  const ctx = useContext(AppConfigContext);
  if (!ctx) throw new Error('useAppConfig 必須在 AppConfigProvider 內使用');
  return ctx;
}
