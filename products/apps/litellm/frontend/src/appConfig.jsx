import React from 'react';
import { LayoutDashboard, Wrench, Link, KeyRound, ScrollText, Settings, Server, Globe, Radio, Activity, Database, Boxes } from 'lucide-react';
import { mcpServerCard } from '@woow/mcp-admin-ui';

function st(h) { return h === true ? 'green' : h === false ? 'red' : 'gray'; }

export const appConfig = {
  brandTitle: 'LiteLLM MCP Admin',
  appName: 'litellm',
  nav: [
    { items: [
      { to: '/', label: 'Dashboard', icon: LayoutDashboard },
      { to: '/tools', label: 'Tools', icon: Wrench },
      { to: '/config', label: 'Connection', icon: Link },
      { to: '/tokens', label: 'Tokens', icon: KeyRound },
      { to: '/logs', label: 'Logs', icon: ScrollText },
      // 不放權限管理頁：core 沒有 /config/permissions；per-tool 停用走 Tools 頁、唯讀走 Settings 頁開關。
      { to: '/settings', label: 'Settings', icon: Settings },
    ] },
  ],
  // Sidebar 硬性依賴：healthBadges 缺鍵會讓受保護頁 TypeError 白屏（比照 vk / opendesign 給空陣列）。
  healthBadges: [],
  connection: {
    description: '設定要管理的 LiteLLM AI 模型閘道（PaaS 同工作區的 LiteLLM 會自動預填網址；金鑰需自行貼上）',
    saveEndpoint: '/config/connection',
    saveLabel: 'Save',
    sections: [
      {
        test: { endpoint: '/config/test' },
        fields: [
          // 鍵名 base_url／master_key 經 mcp_server.env_prefix=LITELLM_MCP_ 大寫注入子行程
          // （LITELLM_MCP_BASE_URL／LITELLM_MCP_MASTER_KEY）；後端 router、本檔、PaaS seed 三處必須同名。
          { name: 'base_url', label: 'LiteLLM 網址', type: 'url',
            placeholder: 'http://svc-xxxxxxxx-litellm:4000',
            help: 'LiteLLM 閘道根網址（例：http://svc-xxxxxxxx-litellm:4000）。會移除尾斜線及僅在根路徑的 /v1；反向代理前綴 /proxy/v1 會完整保留。Test 會呼叫 {根網址}/v1/models，請勿在前綴後再貼 OpenAI API 的 /v1。' },
          { name: 'master_key', label: 'LiteLLM 金鑰', type: 'password', maskedFrom: 'master_key_masked', clearSaved: true,
            help: 'MCP 預設全開（40 支工具含寫入與刪除），AI 實際能做什麼由這把金鑰的權限決定：要讓 AI 發 key、改模型就貼有寫入權限的 key；只想讓它查資料，就貼 proxy_admin_viewer（唯讀管理員）使用者的 key，或在 Settings 頁打開唯讀模式。不建議直接貼 master key。金鑰只寫不讀：存檔後只顯示遮罩，留空即沿用目前的值。' },
        ],
      },
    ],
  },
  dashboard: {
    subtitle: 'System health overview',
    buildSections: (h) => ([
      { cards: [
        mcpServerCard(h, Server),
        // target_app 由帶金鑰的 GET /v1/models 判定（/health/readiness 不驗金鑰，只補 DB；版本讀 /openapi.json）。
        { title: 'LiteLLM Gateway', icon: Globe, status: st(h.target_app?.healthy), value: h.target_app?.healthy ? 'Connected' : 'Disconnected', subtitle: h.target_app?.error || h.target_app?.url || 'No URL configured' },
        { title: 'MCP Proxy', icon: Radio, status: st(h.proxy?.healthy), value: h.proxy?.healthy ? 'Active' : 'Inactive', subtitle: h.proxy?.process || 'built-in proxy' },
      ] },
      { heading: 'Details', cards: [
        { title: 'LiteLLM Version', icon: Activity, status: 'gray', value: h.version || 'N/A', subtitle: 'LiteLLM gateway (openapi info.version)' },
        { title: 'Models', icon: Boxes, status: 'gray', value: h.item_count ?? 'N/A', subtitle: 'visible to the configured key' },
        { title: 'Database', icon: Database, status: 'gray', value: h.db_name || 'N/A', subtitle: 'LiteLLM DB status' },
      ] },
    ]),
  },
  toolTabs: [
    { key: 'mcp', label: 'Tools', icon: Wrench, endpoint: '/tools', itemsKey: 'tools', grouped: true, labelText: '(litellm_mcp_admin)',
      note: '預設全開：40 支工具都會曝露給 MCP client（15 支查詢、25 支寫入或主動操作，其中 8 支破壞性：delete_model／delete_key／block_key／regenerate_key／delete_team／team_member_delete／delete_user／delete_plugin）。Settings 頁打開 Read-only Mode 時只留 15 支查詢工具，25 支寫入或主動操作工具在子行程端就不註冊；此頁開關不反映唯讀模式（2026-10-25 之前開通、出廠唯讀的服務，第一次以新映像啟動時已一次性改成全開）。這裡的 per-tool 停用儲存後立即由 MCP proxy 隱藏並拒絕呼叫（不需重啟）。chat_completion 會實際花費模型額度；health 探測也可能花費額度並保存紀錄。token_counter／spend_calculate 不做推論，但可能下載圖片或 tokenizer，使用 create 主動操作權限。' },
  ],
  // Settings → MCP Server 的宣告式 env 開關：值以字串 '1'／'0' 寫入 mcp_server.env，
  // 子行程 pydantic-settings 讀 LITELLM_MCP_READONLY（'1'/'0' 皆可解析）。seed 寫 '0'（admin 啟動時若沒有這個鍵也會補
  // '0'；既有設定由 owner_defaults 一次性改成 '0'；負責人 2026-10-25「預設全開」），所以開關預設為關。
  settingsEnvToggles: [
    {
      env: 'LITELLM_MCP_READONLY',
      label: 'Read-only Mode（唯讀模式）',
      description:
        '預設關閉（40 支工具全開）。開啟後只曝露 15 支查詢工具，寫入及主動操作工具（含 health／token_counter／spend_calculate）一律不註冊。Health 可能呼叫付費 provider；計算工具不做推論，但可能下載圖片或 tokenizer。關閉時請確認 Connection 頁的金鑰權限符合你要讓 AI 做的事，並視需要在 Tools 頁停用不需要的破壞性工具。變更後按 Save MCP Config 會自動重啟 MCP server 生效。',
    },
  ],
};
