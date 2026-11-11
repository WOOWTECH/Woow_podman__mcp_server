import React from 'react';
import { LayoutDashboard, Wrench, Link, KeyRound, ScrollText, Settings, Server, Cloud, Radio, Activity, User, FolderTree } from 'lucide-react';
import { mcpServerCard } from '@woow/mcp-admin-ui';

function st(h) { return h === true ? 'green' : h === false ? 'red' : 'gray'; }

export const appConfig = {
  brandTitle: 'Nextcloud MCP Admin',
  appName: 'nextcloud',
  nav: [
    { items: [
      { to: '/', label: 'Dashboard', icon: LayoutDashboard },
      { to: '/tools', label: 'Tools', icon: Wrench },
      { to: '/config', label: 'Connection', icon: Link },
      { to: '/tokens', label: 'Tokens', icon: KeyRound },
      { to: '/logs', label: 'Logs', icon: ScrollText },
      // 工具政策（含寫入／刪除）一律在 Tools 頁開關；沒有另外的唯讀開關。
      { to: '/settings', label: 'Settings', icon: Settings },
    ] },
  ],
  // Sidebar 硬性依賴：healthBadges 缺鍵會讓受保護頁 TypeError 白屏（比照 litellm 給空陣列）。
  healthBadges: [],
  connection: {
    description: '設定 MCP 要以哪一個 Nextcloud 帳號工作。請使用專用帳號與 app password，不要用個人帳號或登入密碼。',
    saveEndpoint: '/config/connection',
    saveLabel: 'Save',
    sections: [
      {
        test: { endpoint: '/config/test' },
        fields: [
          // 鍵名 base_url／username／app_password 經 mcp_server.env_prefix=NEXTCLOUD_MCP_ 大寫注入子行程
          // （NEXTCLOUD_MCP_BASE_URL／USERNAME／APP_PASSWORD）；後端 router、本檔、PaaS seed 三處必須同名。
          { name: 'base_url', label: 'Nextcloud 網址', type: 'url',
            placeholder: 'https://cloud.example.com',
            help: 'Nextcloud 的根網址（例：https://cloud.example.com，或裝在子路徑時 https://example.com/nextcloud）。直接從瀏覽器網址列貼上也可以，存檔時會自動去掉 /index.php、/apps/… 等尾段。若 Nextcloud 會轉址到別的網址，請填最終網址（MCP 不跟隨轉址）。' },
          { name: 'username', label: 'Nextcloud 帳號', type: 'text',
            placeholder: 'assistant',
            help: '建議為 AI 助理建一個專用帳號（例如 assistant），只把需要的資料夾與行事曆分享給它；app password 能存取該帳號看得到的一切，無法只限定某些資料夾。填登入名稱即可，用 e-mail 或 LDAP 登入的帳號也可以。' },
          { name: 'app_password', label: 'App password', type: 'password', maskedFrom: 'app_password_masked',
            help: '以上面的帳號登入 Nextcloud → 右上角頭像 →「個人設定」→「安全性」（Settings → Security）→「裝置與工作階段」，輸入名稱（例如 MCP）後按「建立新應用程式密碼」，複製只顯示一次的密碼貼到這裡。不要填登入密碼；啟用兩步驟驗證的帳號也可用 app password。日後可在同一處撤銷。密碼只寫不讀：存檔後只顯示遮罩，留空即沿用目前的值，也不會出現在任何 log。' },
        ],
      },
    ],
  },
  dashboard: {
    subtitle: 'System health overview',
    buildSections: (h) => ([
      { cards: [
        mcpServerCard(h, Server),
        // target_app 由帶帳密的 get_file_tree("", 1)（OCS 查 user id＋WebDAV 讀家目錄）判定；版本讀免認證的 /status.php。
        { title: 'Nextcloud', icon: Cloud, status: st(h.target_app?.healthy), value: h.target_app?.healthy ? 'Connected' : 'Disconnected', subtitle: h.target_app?.error || h.target_app?.url || 'No URL configured' },
        { title: 'MCP Proxy', icon: Radio, status: st(h.proxy?.healthy), value: h.proxy?.healthy ? 'Active' : 'Inactive', subtitle: h.proxy?.process || 'built-in proxy' },
      ] },
      { heading: 'Details', cards: [
        { title: 'Nextcloud Version', icon: Activity, status: 'gray', value: h.version || 'N/A', subtitle: '/status.php versionstring' },
        { title: 'Account', icon: User, status: 'gray', value: h.db_name || 'N/A', subtitle: 'MCP 以此帳號存取 Nextcloud' },
        { title: 'Home Folder', icon: FolderTree, status: 'gray', value: h.item_count ?? 'N/A', subtitle: '家目錄第一層項目數' },
      ] },
    ]),
  },
  toolTabs: [
    { key: 'mcp', label: 'Tools', icon: Wrench, endpoint: '/tools', itemsKey: 'tools', grouped: true, labelText: '(nextcloud_mcp_admin)',
      note: '預設 9 支全開：5 支讀取工具（get_file_tree、get_file_content、read_text_file、list_calendars、list_tasks）、寫入工具（create_text_file、update_text_file、upload_file）與刪除工具 delete_file_checked（紅標，檔案進 Nextcloud 垃圾桶）。2026-10-25 之前開通、出廠只開讀取工具的服務，第一次以新映像啟動時已一次性改成全開；之後在這裡停用的工具會保留。停用工具會立即由 MCP proxy 隱藏並拒絕呼叫，不需重啟；打開原本停用的寫入／刪除工具並儲存後，MCP server 會自動重啟以註冊這些工具（現有 MCP 連線需重新連線）。所有改寫與刪除都要帶讀取時的 etag，檔案被別人改過就會拒絕。' },
  ],
};
