# Open Design MCP Admin

Admin bundle（Web GUI + built-in MCP reverse proxy + process manager），打包
**vendored `od_mcp_server`**（Python FastMCP、Open Design daemon 的 15 工具）＋原生 HTTP launcher
`opendesign-mcp-server`（單一行程跑 Streamable HTTP〔stateless〕＋SSE）成單一容器，產出 image
`opendesign-mcp-admin`。

> **#1369 起不再用 supergateway 橋**：舊架構 supergateway（stdio→streamableHttp）每個 MCP 請求
> spawn 一個 `python -m opendesign_mcp_server` 且不回收，每個 session 多佔 ~115MB、第 7～8 個 session
> 即 OOM。現改為原生 launcher 單一行程，多 session 記憶體持平；admin 啟動時會把 PVC 裡的舊
> supergateway seed 自動遷移（見下方「部署時 seed」）。

上游來源：`WOOWTECH/Woow_opendesign_mcp_server` @ `d6d157a`（vendored 單檔 copy、非 submodule；
`opendesign_mcp_server/od_mcp_server.py` 與上游逐行一致 byte-faithful，唯一新增＝檔頭 provenance
註記，run guard 另置薄 `__main__.py`）。vendored server 以 stdio 跑 `FastMCP`，靠 env `OD_API_BASE`
打外部 **Open Design daemon**（`/api/*`），`OD_CHAT_TIMEOUT` 控 AI 生成長時操作上限（預設 300s）。

### 專屬輸入界線

`opendesign_mcp_server/input_policy.py` 在 vendored 工具註冊後、HTTP 或 stdio 服務啟動前安裝
OpenDesign 專屬 guard。六支帶 `project_id` 的工具採用 HA 0.2.13 所釘 daemon core 的安全 ID 語法
（1–128 字、英數及 `._-`，拒絕純點號），保留大寫 UUID 及匯入專案的非 UUID ID 原樣；
`read_file`／`get_file_info` 的 `file_path` 只接受最多 256 字的相對安全路徑，包括中文漢字，
拒絕 traversal、反斜線、百分號與控制／bidi 字元。`read_file` 另拒絕隱藏及 secret-like 路徑段；
`get_file_info` 只查檔案清單的 metadata，沿用 HA 0.2.13 對它的相對路徑規則，沒有新增
secret-like 限制。拒絕發生在子行程送出 daemon HTTP 請求前。

共用 PaaS gateway 仍先驗 token／停用政策／body 上限，再原樣轉發；沒有新增 OpenDesign gateway
參數正規化，也沒有改 15 支工具的宣告 `inputSchema`。拒絕會作為子工具錯誤回傳，與 HA gateway
的 `-32602` 錯誤形狀不同。HA gateway 對六支工具只接受 UUID，PaaS 保留可安全使用的
匯入專案 ID，因此兩側的 ID 語法並非完全相同。上游 vendored 檔仍保持 byte-faithful；若工具名稱或參數變更，
guard 在啟動時 fail closed，須重新審查對應輸入規則。

## 與 apps/vk 的差異

| | apps/vk（Vibe Kanban 版） | apps/opendesign（Open Design 版） |
|---|---|---|
| 底層 MCP server | npm `supergateway` 包 Rust binary `vibe-kanban-mcp`（binary Dockerfile 下載＋sha256 驗證） | 原生 launcher `opendesign-mcp-server`（core `DualTransportApp` 跑 vendored FastMCP，單一行程；無 supergateway） |
| 連線欄位 | 單欄 `vibe_backend_url`（非 secret） | `od_api_base`（非 secret URL）＋ `od_chat_timeout`（非 secret、選配、字串秒數） |
| 工具數 | 34 tools / 4 類 | 15 tools / 3 類（System & Discovery 7、Projects & Files – Read 5、Projects – Write 3） |
| dangerous | 3 支 | 3 支（`create_project` / `send_message` / `delete_project`），皆寫入或長時（≤300s） |
| 對外 transport | 僅 `/mcp`（streamableHttp） | `/mcp`（streamableHttp、stateless）＋ `/sse`（legacy SSE） |
| 健康探測目標 | VK Host `/api/health` | Open Design daemon `/api/health`（`OD_HEALTH_PATH` 單一常數；無 db／模組概念，version best-effort） |

## 分層（與其他 app 一致）

- 共用 `packages/mcp-admin-core`（app factory、auth、config store、process manager、proxy、
  hoisted routers：tools / health / logs / tokens）與 `packages/mcp-admin-ui`（`@woow/mcp-admin-ui`）。
- 本 app 只保留 app-specific 五件：
  - `opendesign_mcp_admin/routers/config.py`：Open Design 連線設定（`od_api_base` + `od_chat_timeout`）。
    Test 測「已存檔設定」且**誠實分流**（見下）；Save 一律 strip 防呆＋對 secret 欄 `drop_blank_secrets`
    （目前 `secret_fields=[]`、為前瞻掛點）。
  - `opendesign_mcp_admin/tool_registry.py`：vendored server 15 tools（3 類、3 dangerous UI 紅標；停用由 core proxy 強制）。
  - `opendesign_mcp_admin/health_probes.py`：Open Design daemon `/api/health` 可達性 + best-effort version。
  - `opendesign_mcp_admin/mcp_client.py`：Test 走 MCP `tools/call health` 端到端驗證時用的本地 MCP client。
  - `frontend/src/appConfig.jsx`：宣告式 nav / connection / dashboard / toolTabs。

## 本機

```bash
# build（build context = repo root，Dockerfile 依賴 root 的 npm / uv workspace）
docker build -t woow-opendesign-mcp-server -f apps/opendesign/Dockerfile .
docker run --rm -p 8080:8080 woow-opendesign-mcp-server
# → http://localhost:8080/healthz
```

MCP 子行程**不**在 image 啟動時拉起（上游 `entrypoint.sh` 雙行程做法已廢除），CMD 只跑 uvicorn
admin，子行程（`opendesign-mcp-server`）全交 `McpProcessManager` 依 `config.json` 的 `mcp_server`
於執行期 spawn。image 目前仍預裝 node20 + `supergateway@3.4.3`（為縮小本次變更面暫不動 Dockerfile；
原生 launcher 不需要它，確認各環境都已遷移後可移除以縮小 image）。

本機不經 docker 直接跑子行程：

```bash
OD_API_BASE=http://127.0.0.1:7457 uv run --package woow-opendesign-mcp-server \
  opendesign-mcp-server --host 127.0.0.1 --port 8000
# → POST http://127.0.0.1:8000/mcp（Streamable HTTP）、GET /sse（SSE）
```

## 部署時 seed（不在 repo；設定 file-based，寫在 PVC 的 `/data/config.json`）

正式部署由 paas-charts 的 mcp-server chart 提供 `config.json` seed（連線值一律字串；鍵經 `env_prefix=""`
大寫後注入子行程 env）：

```jsonc
{
  "connection": {
    "od_api_base": "http://open-design-svc:7457",   // 必填；指向 Open Design daemon 的 /api/*
    "od_chat_timeout": "300"                          // 選配、UI 可調；注入 OD_CHAT_TIMEOUT（清空＝回退上游預設 300）
  },
  "mcp_server": {
    "command": "opendesign-mcp-server",               // venv console script（PATH 前置 /app/.venv/bin）
    "args": ["--host", "127.0.0.1", "--port", "8000"],// 只綁 loopback，由 core proxy 反代
    "port": 8000,                                     // ⚠️ 必須與 args 的 --port 完全一致，否則 proxy 一律 502
    "env": {},                                        // od_chat_timeout 走 connection 欄，此處不放
    "env_prefix": ""                                  // connection.od_api_base → OD_API_BASE 注入
  },
  "proxy": { "timeout": 86400 }
}
```

> ⚠️ **port 一致性紅字**：`mcp_server.port`（core proxy 轉發目標）與 args 的 `--port` 是兩個獨立欄位、
> core 不做交叉驗證，不一致時 proxy 一律回 502。本例統一 8000。
>
> **舊 seed 自動遷移（#1369）**：PaaS chart 的 initContainer 是 seed-if-absent，既有服務的 PVC 會保留舊的
> `"command": "supergateway", "args": ["--stdio", "python -m opendesign_mcp_server", ...]`。admin 每次啟動、
> 拉起子行程**之前**（`opendesign_mcp_admin/seed_migration.py`）偵測到這個舊形狀就改寫成上面的原生
> launcher（`port`／`env`／`env_prefix` 保留，args 的 port 取 `mcp_server.port`）；其他自訂 command 不動。
>
> ⚠️ **絕不**在 seed 抄入上游明文 `JWT_SECRET` / `ADMIN_PASSWORD`（上游 manifest 曾明文塞入、屬安全
> 事件）；admin 認證 secret 由部署期供給，非 repo 檔案。

未 seed `command` 時，admin 仍正常提供 GUI 與 `/healthz`，只是不啟動 MCP 子行程。

## 工具治理：停用由 core proxy 強制（#1384，含 `delete_project`）

Tools 頁的工具開關**有實際封鎖力**：共用核心的 MCP proxy（`packages/mcp-admin-core/mcp_admin_core/tool_filter.py`）
每個請求即時讀 config store 的 `tools.disabled`——

- `tools/list` 回應移除被停用的工具（JSON 與 SSE 兩種回應格式）。
- `tools/call` 呼叫被停用的工具：Streamable HTTP（`/mcp`）直接回 `isError: true` 的工具結果（2026-10-26 前是 JSON-RPC error `-32602`）、不轉發給子行程；
  legacy SSE（`/messages/`）改寫工具名轉發，由子行程回「unknown tool」。
- Tool Manager 存檔後立即生效，不重啟子行程、不中斷現有 session。

vendored `od_mcp_server.py` 本身仍無過濾 flag（也**不要**在 vendored 檔內加治理邏輯，保 byte-clean）——封鎖完全
發生在 proxy 層。`dangerous` 旗標（`create_project` / `send_message` / `delete_project`）只是 UI 紅標；
`delete_project` 為**不可逆、無二次確認**的刪除，不需要時建議在 Tools 頁停用。出廠全 15 支啟用（不 seed
`tools.disabled`）。

註：單一工具內「停用某些操作」（`tools.disabled_operations`）目前仍無任何層強制，opendesign 工具也沒有 per-op 概念。

## Transport

- `POST /private_{token}/mcp`：Streamable HTTP，core proxy 透明轉發到 `127.0.0.1:8000/mcp`。
  **stateless**——不發 `Mcp-Session-Id`、每個請求獨立 transport、server 端不留 per-session 狀態
  （與舊 supergateway stateless 對外語意相同；15 支工具本身都是無狀態 REST 呼叫）。
- `GET /private_{token}/sse` ＋ `POST /private_{token}/messages/`：legacy SSE（core `DualTransportApp`
  附帶，連線結束即釋放）。

**記憶體行為**：單一 python 行程（實測 ~80–95MB RSS），session 數不影響記憶體；舊 supergateway 每個
session 會多出數個常駐 python 子行程（正式站實測 ~115MB／session，不釋放）。

**同步工具不卡 event loop**：vendored 工具都是同步函式（httpx 同步 client；`send_message`／`create_project`
可阻塞到 `OD_CHAT_TIMEOUT`）。launcher 啟動時把同步工具包成丟 worker thread 執行的 async 版本
（`offload_sync_tools`，不改 vendored 檔），長時工具執行中其他 session 照常回應。

## Test Connection 語意（誠實分流）

Connection 頁的「Test」讀 **config store 存檔值**、不吃 request body，並依 MCP 子行程狀態分流：

- **子行程 running** → 走本地 MCP `tools/call health`，經 MCP 子行程打通到 Open Design daemon
  的**完整協定鏈**才算 GREEN（證「子行程活著＋整鏈通」，杜絕 daemon 活但子行程死的假 GREEN）。
- **子行程未 running** → 降級直打 daemon `/api/health`（`health_probes.probe_target`，內部引用
  `OD_HEALTH_PATH` 單一常數），可達即 success，但**誠實標示**「MCP 子行程未執行、未端到端驗證」。

**嚴禁**改打子行程本身的存活探測——那只證行程活著，`OD_API_BASE` 填錯／daemon 不可達時仍回 ok，
會假 GREEN。Test 綠只代表可達，**≠ 15 工具實跑成功**。

**還沒填 daemon 網址時**（第五輪 2026-10-27）：子行程照樣啟動、列出 15 支工具，但連不到 daemon（存成空字串時網址是空的；從沒存過時是上游預設的 `http://open-design-svc:7457`，PaaS 的 Open Design 服務名稱是 `svc-xxxxxxxx`，不會是這個），每支工具都失敗，而且錯誤只有 `Error executing tool list_projects: [Errno -2] Name or service not known`（本機實測），看不出是沒設定。`od_api_base` 沒填時公開的 `/healthz` 回 `"backend": "missing"`（填了是 `"configured"`；`mcp` 照舊是 `running`／`not_running`），平台服務頁據此顯示「尚未設定後端連線」的提示。Basic Auth 兩欄是選填（自架、沒有認證的 daemon 留空）、Chat Timeout 有預設值，都不算；接 PaaS 的 Open Design 卻沒填帳密時是 `configured`，工具會回 401——`configured` 只代表必填的欄位填了，請按 Test。

## 長時操作（≥300s）

`create_project` / `send_message` 走 daemon `POST /api/chat`（SSE），AI 生成可耗數分鐘，上游硬編 timeout
300s（可由 `od_chat_timeout` 連線欄調整、注入 `OD_CHAT_TIMEOUT`）。core proxy 預設 `timeout=86400s`＋
`stream=True`＋`x-accel-buffering:no`，**不會**在 300s 前把長時操作提前中斷；但外部 MCP client（如 Claude
Code）也須容許對應的長逾時，否則會是 client 端先斷。

## 接 PaaS 上的 Open Design（前置 basic auth，#1354）

連線另有兩個選配欄位 `od_basic_user`（非 secret）／`od_basic_password`（secret、遮罩）。
PaaS 的 open-design 服務前面有 basic auth（帳號通常是 `admin`、密碼為開通時設定的服務密碼）。Connection 頁填：

| 欄位 | 值 |
|---|---|
| Open Design Daemon URL | 服務對外網址，例 `https://<open-design 服務網域>`（**不要**把帳密寫進 URL） |
| Basic Auth 帳號 | `admin` |
| Basic Auth 密碼 | 服務密碼（secret：存檔後只顯示遮罩摘要，留空＝沿用既有值） |

- 帳密注入子行程 `OD_BASIC_USER`／`OD_BASIC_PASSWORD`；`opendesign_mcp_server` 套件在 vendored server
  import 前把 `OD_API_BASE` 的 userinfo 去掉，並讓 vendored 模組的 `httpx.Client` 預設帶
  `httpx.BasicAuth`（不改 vendored 檔）。Health／Test 的 daemon 探測同樣帶 BasicAuth，401 時會明說是帳密問題。
- **舊寫法相容**：`https://admin:<密碼>@<host>` 仍可用；存檔時 userinfo 會被拆進上面兩欄、URL 欄只留
  `https://<host>`。尚未重存的舊設定在 GET／Dashboard 顯示時密碼以 `****` 遮掉，子行程也不會把帳密留在
  URL（避免 httpx 錯誤訊息把 `user:password@` 帶回給 MCP client）。
- 帳號清空＝不帶認證（自架、無認證的 daemon 留空即可）。

## `od_api_token` inert seam（未來認證掛點）

目前刻意**不設** `od_api_token` secret 欄（YAGNI＋誠實介面：死欄會誤導管理者以為「填了就有保護」）。
現行 vendored wrapper 根本不送 `Authorization` header，只在 daemon 以 `OD_DISABLE_API_AUTH=1` 免認證模式
下運作。未來接 token 認證的 daemon 時，須**同時**：patch wrapper 送 `Authorization: Bearer`＋把
`od_api_token` 加進 config router 的 `secret_fields`＋補前端欄位（三處一併補，不預留死欄）。

## Claude Code client 設定範例

從 admin 的 Tokens 頁核發一枚 proxy token，填入 `url` 的 `private_{token}` 段：

```jsonc
{
  "mcpServers": {
    "opendesign": {
      "type": "http",
      "url": "https://<admin-host>/private_<PROXY_TOKEN>/mcp"
    }
  }
}
```

`<admin-host>` = 部署後 admin GUI 的對外網域；`<PROXY_TOKEN>` = Tokens 頁核發值。連上後 `tools/list`
得 15 支；長時工具（`create_project` / `send_message`）請確認 client 逾時設定 ≥300s。
