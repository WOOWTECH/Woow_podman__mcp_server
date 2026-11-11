export function isConfigurationConflict(error) {
  return error?.status === 409 && error?.code === 'config_conflict';
}

export function configurationMessage(configuration) {
  return {
    applied: '已儲存，且此版本已由目前的 MCP 子程序確認套用。',
    pending: '已儲存；目前子程序尚未確認套用此版本。',
    failed: '已儲存，但子程序啟動或設定驗證失敗。',
    unconfigured: '尚未設定可啟動的 MCP 子程序。',
    unknown: '尚未確認已儲存設定的套用狀態。',
  }[configuration?.apply_state] || '目前服務未提供設定套用狀態。';
}

export function canRetryConfiguration(configuration) {
  return Boolean(configuration?.revision && ['pending', 'failed', 'unknown'].includes(configuration.apply_state));
}
