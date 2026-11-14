// GET /api/profiles 每列的 disabled_reason：這個帳號的子程式為什麼沒有啟動（null＝照常）。帳號仍可選取、設定照常可改，
// 只是 MCP 網址拿到的是「沒有在跑」的說明。memory_capacity 另帶 required_memory_mib（記憶體上限至少要多少才會啟用）。
const SHORT = {
  memory_capacity: '記憶體不足，已停用',
  profile_limit: '超過帳號上限，已停用',
  port_conflict: 'port 衝突，已停用',
};

export function profileOptionLabel(profile) {
  const reason = profile?.disabled_reason;
  if (!reason) return profile?.label ?? '';
  return `${profile.label}（${SHORT[reason] || '已停用'}）`;
}

export function profileDisabledNotice(profile) {
  switch (profile?.disabled_reason) {
    case null:
    case undefined:
    case '':
      return '';
    case 'memory_capacity': {
      const need = Number.isSafeInteger(profile.required_memory_mib) ? profile.required_memory_mib : null;
      return `此帳號因服務的記憶體上限不足而停用，子程式不會啟動${need ? `；記憶體上限至少要 ${need} MiB 才能啟用` : ''}，或刪除其他帳號。`;
    }
    case 'profile_limit':
      return '此帳號超過這個產品的帳號上限而停用，子程式不會啟動；請刪除其他帳號。';
    case 'port_conflict':
      return '帳號索引把此帳號的 port 也指派給了其他帳號（手動修改或還原不一致），子程式不會啟動；請修正帳號索引，或刪除其中一個帳號。';
    default:
      return '此帳號目前停用，子程式不會啟動。';
  }
}
