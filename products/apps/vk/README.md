# VK MCP Admin

Admin bundle（Web GUI + built-in MCP reverse proxy + process manager），打包
**supergateway**（npm、stdio→streamableHttp bridge）＋ **vibe-kanban-mcp**（Rust 靜態
binary、34 工具）成單一容器，產出 image `vk-mcp-admin`。

上游來源：BloopAI/vibe-kanban `v0.1.44`（tag `v0.1.44-20260424091429`），其 MCP crate 產出
獨立 binary `vibe-kanban-mcp`（musl 靜態連結）。vibe-kanban-mcp 靠 env `VIBE_BACKEND_URL`
打外部 **VK Host API**（Vibe Kanban 後端，`/api/*`）。

## 與 apps/odoo-manage 的差異

| | apps/odoo-manage（ivnvxd Manage 版） | apps/vk（Vibe Kanban 版） |
|---|---|---|
| 底層 MCP server | Python 套件 `mcp-server-odoo`（venv console script） | npm `supergateway` 包 Rust binary `vibe-kanban-mcp`（`--mode global`） |
| 子行程啟動 | 直接 spawn Python console script | McpProcessManager spawn npm 全域 bin `supergateway`（本 repo 首例） |
| 連線欄位 | `odoo_user` / `odoo_api_key`（含 secret，需遮罩） | 單欄 `vibe_backend_url`（非 secret、不遮罩；VK Host 現無認證，靠 Pod 網路邊界） |
| 工具數 | 9 tools / 2 類 | 34 tools / 4 類（A - Execution 10、B - Repo 5、C - Issue / Kanban 18、D - Context 1） |
| 對外 transport | `/mcp` + `/sse` | 僅 `/mcp`（streamableHttp）；`/sse` 回 404 |
| 健康探測目標 | Odoo `/web/health` + XML-RPC 版本／模組數 | VK Host `/api/health`（無 db／模組概念，version best-effort） |

## 分層（與其他 app 一致）

- 共用 `packages/mcp-admin-core`（app factory、auth、config store、process manager、proxy、
  hoisted routers：tools / health / logs / tokens）與 `packages/mcp-admin-ui`（`@woow/mcp-admin-ui`）。
- 本 app 只保留 app-specific 四件：
  - `vk_mcp_admin/routers/config.py`：VK Host 連線設定（單欄 `vibe_backend_url`）。Test 測「已存檔
    設定」；Save 對 secret 欄 `drop_blank_secrets`（目前無 secret 欄、為前瞻掛點）＋ strip 防呆。
  - `vk_mcp_admin/tool_registry.py`：vibe-kanban-mcp 34 tools（4 類、3 dangerous UI 紅標；停用由 core proxy 強制）。
  - `vk_mcp_admin/health_probes.py`：VK Host `/api/health` 可達性 + best-effort version 探測。
  - `frontend/src/appConfig.jsx`：宣告式 nav / connection / dashboard / toolTabs。

## 本機

```bash
# build（build context = repo root，Dockerfile 依賴 root 的 npm / uv workspace）
docker build -t woow-vk-mcp-server -f apps/vk/Dockerfile .
docker run --rm -p 8080:8080 woow-vk-mcp-server
# → http://localhost:8080/healthz
```

### vibe-kanban-mcp binary 來源（Dockerfile Stage A，ARG 驅動 + sha256 pin）

binary 不進 git（供應鏈斷點），由 Dockerfile Stage A 以 ARG 驅動的 URL 下載並 `sha256sum -c` 驗證：

| 選項 | 用途 | 判定 |
|---|---|---|
| (1) 上游 R2 CDN | `https://npm-cdn.vibekanban.com/binaries/v0.1.44-20260424091429/linux-x64/vibe-kanban-mcp.zip`（sha256 `ce1df1be…6299`） | **預設**（Gate 2 定案；CI build step 不帶 `--build-arg`，故預設 URL 必須 runner 可達） |
| (2) 內部 Artifactory generic mirror | 把 (1) 的 zip + sha256 搬進內網後，覆寫 ARG 預設值 | **建議最終落地形態**（符合繞 CF 走內網慣例） |
| (3) cargo 自建 | `cargo zigbuild --release --target x86_64-unknown-linux-musl -p mcp --bin vibe-kanban-mcp` | fallback（重、需 crates.io 出網或 vendor） |

日後切內網 mirror 只改三個 ARG 預設值（`VK_MCP_TAG` / `VK_MCP_URL` / `VK_MCP_SHA256`）、不動結構。
本機覆寫範例：

```bash
docker build -t woow-vk-mcp-server -f apps/vk/Dockerfile . \
  --build-arg VK_MCP_URL=https://<內網 mirror>/vibe-kanban-mcp.zip \
  --build-arg VK_MCP_SHA256=<對應 sha256>
```

## 部署時 seed（不在 repo；設定 file-based，寫在 /data/config.json）

正式部署由 paas-charts 的 mcp-server chart 提供 `config.json` seed。來源 `entrypoint.sh` 雙行程做法
已廢除：supergateway 全交 McpProcessManager 於執行期依 `mcp_server.command` spawn，設定變更
（Save & Restart）才會真正生效。

```jsonc
{
  "connection": {
    "vibe_backend_url": "http://<vk-host>:3000"   // 必填；不論 binary 是否內建 fallback 一律顯式 seed
  },
  "mcp_server": {
    "command": "supergateway",                     // node 全域 bin，image 已預裝 supergateway@3.4.3
    "args": ["--stdio", "vibe-kanban-mcp --mode global",
             "--port", "8000",
             "--outputTransport", "streamableHttp", // 不給則不開 /mcp（--stdio 預設 outputTransport=sse）
             "--healthEndpoint", "/healthz"],
    "port": 8000,                                  // ⚠️ 必須與 args 的 --port 完全一致，否則 proxy 一律 502
    "env": {},
    "env_prefix": ""                               // connection.vibe_backend_url → 大寫 VIBE_BACKEND_URL 注入
  }
}
```

> ⚠️ **port 一致性紅字**：`mcp_server.port`（core proxy 轉發目標）與 args 的 `--port` 是兩個獨立欄位，
> core 不做交叉驗證。兩者不一致（例如一個 8000、一個 8001）時 proxy 一律回 502。本例統一 8000。

未 seed `command` 時，admin 仍正常提供 GUI 與 `/healthz`，只是不啟動 MCP 子行程。

## 工具開關（#1384 起由 core proxy 強制）

Tools 頁的 per-tool 開關寫入 `config.json` 的 `tools.disabled`，由 core MCP proxy 在每個請求即時套用：

- `tools/list` 回應移除被停用的工具（JSON 與 SSE 回應皆處理）。
- `tools/call` 被停用的工具：Streamable HTTP（`/mcp`）直接回 `isError: true` 的工具結果（說明是管理員停用；
  2026-10-26 前是 JSON-RPC error `-32602`）、不轉發給 binary；legacy SSE（`/messages/`）改寫工具名讓 binary 回 unknown tool。
- 儲存後立即生效，不重啟子行程、不中斷現有 session。

vibe-kanban-mcp binary 本身仍無工具過濾 flag；`dangerous=True`（`start_workspace` / `delete_workspace`
/ `delete_issue`）僅為 UI 紅標，不會自動停用——需要時請在 Tools 頁關掉。來源 repo 的 `deny-list.yaml`
是純文件性警告（無任何程式讀取），已不遷移。

## Transport

對外只有一條 MCP 路徑：`POST /private_{token}/mcp`（core proxy 透明轉發到
`127.0.0.1:8000/mcp` 的 supergateway streamableHttp）。單一 supergateway 只能有一個
outputTransport，故 **無 `/sse`**（`/sse` 回 404），比照 n8n 單 `/mcp` 先例。

## Test Connection 語意

Connection 頁的「Test」讀 **config store 存檔值**、不吃 request body，httpx `GET
{vibe_backend_url}/api/health` 打「vibe-kanban-mcp 子行程實際依賴的 VK Host 層」（HTTP 200 判定可達）。
**嚴禁**改打 supergateway 的 bridge `:8000/healthz`——那只證 node bridge 活著，`VIBE_BACKEND_URL`
填錯／VK Host 不可達時仍回 ok，會假 GREEN。

不過度保證：Test 綠只代表 VK Host `/api/health` 可達，**≠ MCP 工具鏈成功**（不驗 MCP 協定與 34 工具實跑）。

## 還沒填 VK Host 網址時（第五輪 2026-10-27）

VK Host 一律在容器外。沒有 `VIBE_BACKEND_URL` 時 vibe-kanban-mcp（v0.1.44 `resolve_base_url`）改找本機的 port 檔，容器裡沒有就結束；admin 管的子行程是 supergateway，它照樣在跑（`/healthz` 的 `mcp` 是 `running`），每個 MCP 請求卻都失敗。所以 `vibe_backend_url` 是後端連線欄位：沒填時公開的 `/healthz` 回 `"backend": "missing"`（填了是 `"configured"`），平台服務頁據此顯示「尚未設定後端連線」的提示。`configured` 只代表欄位填了，VK Host 連不連得上請按 Test。
