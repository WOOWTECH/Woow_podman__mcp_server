// Settings 頁「負責人 2026-10-25 預設全開」那一行（後端 /api/settings 的 owner_defaults，見
// mcp_admin_core/owner_defaults.py owner_defaults_view）。純 JS、不含 JSX，方便 node --test 直接測。
//
// applied：migrated＝這次有改（原本有限制）；fresh_install＝不需要改。source（2026-10-26 起新寫入的標記才有，
// 判斷見 owner_defaults.config_source）：new_config＝admin 自己建立的設定（全新安裝）；deploy_seed＝部署 seed
// 寫好、還沒有 MCP token／連線／登入紀錄的設定（PaaS 新開的服務；從沒有人設定過的舊服務也是）；existing_config
// ＝第一次以這一版啟動時已經有使用痕跡的設定。舊標記沒有 source，fresh_install 可能是新安裝、也可能是本來就
// 沒有限制的舊服務，所以用不下斷言的說法。
//
// token 那一句看後端給的事實，不看 source（審查第三輪 G4）：token_state（active／revoked／unconfigured，與 Tokens 頁
// 同一個判斷）與 token_auto_issued（現在這個 token 是第一次啟動時自動發的，見 mcp_admin_core/initial_token.py）。
// 撤銷過、管理員自己輪替過，或第二輪映像開出後自己 Rotate 的服務，都不能說成「第一次啟動時自動產生」。後端沒給這兩個
// 欄位（舊版 admin）就不提 token。

const SCOPE = '所有工具、寫入、破壞性操作與 Secret 存取';
const KEEP = '之後在這裡或 Tools 頁設定的限制會保留。';
const TOKEN_AUTO = 'MCP token 已在第一次啟動時自動產生，完整網址見 Tokens 頁。';
const TOKEN_REVOKED = 'MCP token 已撤銷，用戶端都連不上；要恢復請到 Tokens 頁 Rotate Token 重發。';
const TOKEN_NONE = '目前還沒有 MCP token，用戶端連不上；請到 Tokens 頁 Rotate Token 產生，完整網址也在那裡。';

export function tokenSentence(view) {
  if (!view) return '';
  if (view.token_state === 'revoked') return TOKEN_REVOKED;
  if (view.token_state === 'unconfigured') return TOKEN_NONE;
  if (view.token_state === 'active' && view.token_auto_issued === true) return TOKEN_AUTO;
  return '';
}

export function ownerDefaultsMessage(view) {
  if (!view) return null;
  const decision = view.decision || '負責人 2026-10-25「預設全開」';
  const at = view.at ? `${view.at} ` : '';
  const token = tokenSentence(view);
  if (view.applied === 'migrated') {
    return `已套用${decision}：既有設定已於 ${view.at || '升級時'} 一次性改成全開（${SCOPE}）。${token}${KEEP}`;
  }
  if (view.source === 'existing_config') {
    return `已套用${decision}：既有設定本來就是全開（${SCOPE}）；這份設定已經有 MCP token、連線設定或登入紀錄，${at}第一次以這一版啟動時只寫上標記、沒有改動。${token}${KEEP}`;
  }
  if (view.source === 'deploy_seed') {
    return `已套用${decision}：部署時寫入的設定本來就是全開（${SCOPE}）；${at}第一次啟動時寫上標記（當時還沒有 MCP token、連線設定或登入紀錄，新開通的服務就是這樣）。${token}${KEEP}`;
  }
  if (view.source === 'new_config') {
    return `已套用${decision}：新安裝即全開（${SCOPE}）。${token}${KEEP}`;
  }
  return `已套用${decision}：設定為全開（${SCOPE}；新安裝，或既有設定本來就沒有限制）。${token}${KEEP}`;
}
