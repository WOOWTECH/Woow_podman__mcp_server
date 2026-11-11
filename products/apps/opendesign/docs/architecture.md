# Open Design MCP Admin — 架構

與其他 app 一致，本 app 是共用核心 `mcp-admin-core` 之上的薄殼（thin shell），包 vendored
`od_mcp_server` ＋原生 HTTP launcher `opendesign-mcp-server`（#1369 起取代 supergateway 橋）。管理面（Login / Settings / Tokens / Proxy）全由共用 core `create_app`
強掛，app 只注入 app-specific 的 connection config / tool registry / health probes / MCP client /
前端 appConfig。

## 執行鏈

```
Claude / 客戶端
   │  POST /private_{TOKEN}/mcp
   ▼
opendesign admin（uvicorn :8080，mcp_admin_core.create_app）
   │  core proxy 透明轉發 → 127.0.0.1:8000/mcp   （proxy 目標 = mcp_server.port）
   ▼
opendesign-mcp-server（:8000，單一行程）          ← McpProcessManager spawn 的子行程
   │  core DualTransportApp：/mcp（Streamable HTTP、stateless）＋ /sse、/messages/（SSE）
   │  vendored od_mcp_server.py（FastMCP）；同步工具丟 worker thread   ← 繼承 env OD_API_BASE / OD_CHAT_TIMEOUT
   │  HTTP /api/*
   ▼
Open Design daemon（od_api_base，例 http://open-design-svc:7457）
```

管理面路由（皆共用 core 現成）：

- `create_app` 強掛：Login / Settings / Tokens / MCP Proxy（`/private_{token}/mcp`）。
- app 以 factory 注入：`/api/config`（app-local 連線模型）、`/api/tools`（core
  `create_tools_router(registry=…)`）、`/api/health`（core `make_health_router(probe/fetch=…)`）、
  `/api/logs`（core `create_logs_router("opendesign_mcp_admin")`）。
- `/healthz` 永遠可用：零設定啟動時 `command=""` → `pm.start()` 印 warning 跳過，admin 照常聽
  8080，CI `/healthz` smoke 因此成立。

## 連線 → 子行程 env

`config.json` 的 `connection` 由 `process._build_subprocess_env` 大寫並加 `env_prefix`（本 app 為
`""`）後注入子行程（launcher 與 vendored `od_mcp_server` 同一行程，直接讀 env）：

| connection 鍵 | 注入 env | 消費者 |
|---|---|---|
| `od_api_base` | `OD_API_BASE`（`env_prefix=""` + key 大寫） | vendored server 的 daemon base URL（打 Open Design `/api/*`） |
| `od_chat_timeout` | `OD_CHAT_TIMEOUT`（選配、字串化正數；清空＝移除鍵→回退 300） | 長時 chat 操作（`create_project` / `send_message`）的最長等待秒數 |

鍵名 `od_api_base` / `od_chat_timeout` 一經定案即等同 env 名 `OD_API_BASE` / `OD_CHAT_TIMEOUT`（vendored
`od_mcp_server.py` 模組頂層直讀的固定 env 名），**後端 router、前端 appConfig、部署 seed 三處必須同名、
不可改**。`od_chat_timeout` 為選配欄——空字串走 read-modify-write **真移除鍵**（不可存 `""`，否則子行程
`float("")` 會 crash）。連線模型只有這兩個非 secret 欄；未來若加認證欄，config router 已留
`drop_blank_secrets(secret_fields=[])` 前瞻掛點。

## 工具治理：停用由 core proxy 強制（#1384）

過去（v1）tool registry 的 `dangerous` 旗標與 `tools.disabled` 在此路徑上只是記錄性——core 不把 `tools.disabled`
注入子行程 env、vendored `FastMCP` 載入時無條件註冊全部 `@mcp.tool`、proxy 透明轉發不解析 JSON-RPC。

#1384 起改由所有 app 共用的 proxy（`packages/mcp-admin-core/mcp_admin_core/tool_filter.py`）強制：`tools/list`
回應移除停用工具、`tools/call` 停用工具不轉發（`/mcp` 回 `isError: true` 的工具結果，2026-10-26 前是 JSON-RPC `-32602`；legacy `/messages/` 改寫工具名讓子行程
回 unknown tool）。停用清單每個請求即時讀 config store，存檔立即生效、不重啟子行程。因此原 plan §8 的 v2 路徑
（vendored 檔加 `OD_DISABLED_TOOLS` env gate）**不再需要**；**請勿**在 vendored `od_mcp_server.py` 內加治理/停用
邏輯（保 byte-clean，見 plan §4.3）。

`dangerous` 仍只是 UI 紅標（`create_project` / `send_message` / `delete_project`），出廠全 15 支啟用；
`delete_project`（不可逆、無二次確認）不需要時建議在 Tools 頁停用。`tools.disabled_operations` 目前仍無強制
（opendesign 工具無 per-op 概念，影響為零）。

## Test Connection：k8s 式誠實分流

`POST /api/config/test` 讀 config store 存檔值（不吃 body），依 MCP 子行程狀態分流：running → 走本地
MCP `tools/call health` 端到端打通子行程→daemon 全鏈；未 running → 降級直打 daemon `/api/health`
（`health_probes.probe_target` 引用 `OD_HEALTH_PATH` 單一常數）並誠實標「未端到端驗證」。**嚴禁**改打
子行程本身的存活探測（只證行程活著，`OD_API_BASE` 錯仍回 ok＝假 GREEN）。

## `od_api_token` inert seam

現行 vendored wrapper 不送 `Authorization` header，只在 daemon `OD_DISABLE_API_AUTH=1` 免認證模式下運作，
故刻意**不設** `od_api_token` 死欄（避免誤導）。未來接 token 認證 daemon 須**同時** patch wrapper 送
`Authorization: Bearer`＋把 `od_api_token` 加進 `secret_fields`＋補前端欄位（三處一併補）。

## 與 k8s 解耦

app 只重啟自己 spawn 的 MCP 子行程（`McpProcessManager.restart()`，SIGTERM + 重起），**不呼叫 k8s API、
不引入 k8s client**。設定為 file-based（`config.json` 在 PVC 的 `/data`），無 k8s Secret / ConfigMap 後援。
service 級 rollout（整個 Deployment / Pod）交給 PaaS 平台。上游 `entrypoint.sh` 的「背景雙行程
（supergateway + uvicorn）」做法已廢除：MCP 子行程全交 McpProcessManager 依 `mcp_server.command` 於
執行期 spawn，image CMD 只跑 uvicorn admin，設定變更（Save & Restart）才第一次真正生效。

## session 模式：原生 stateless（#1369）

舊架構 supergateway 3.4.3 stateless 宣稱「每個 `POST /mcp` spawn 一次 stdio 子行程、請求結束即回收」，
**實測並不回收**：本機以 supergateway@3.4.3 連開 10 個 session（initialize／initialized／tools/list／
tools/call health），行程樹由 1 個行程 100MB 漲到 81 個行程 2687MB 且閒置後不降；正式站 limits 1Gi
第 7～8 個 session 即 OOMKilled。

現改為原生 launcher `opendesign-mcp-server`：

- 單一 python 行程，`StreamableHTTPSessionManager(stateless=True)`：不發 `Mcp-Session-Id`、每請求獨立
  transport、不留 per-session 狀態 → session 數不影響記憶體（同條件 10 個 session 維持 87MB、1 個行程；
  8 並行 40 個 session 後 93MB）。對外語意與舊 supergateway stateless 相同（client 不需 session）。
- 無孫行程：core `stop()` 對主 PID 送 SIGTERM 即完整停止，沒有 orphan 風險。
- vendored 工具都是同步函式，FastMCP 會直接在 event loop 上呼叫；launcher 啟動時以
  `offload_sync_tools` 把它們包成 `anyio.to_thread.run_sync` 的 async 版本（不改 vendored 檔），長時
  `send_message` 執行中其他 session 不受影響（實測慢工具 5s 期間另一個 session 0.04s 完成；不 offload
  時會被卡 4.5s）。
- 舊 seed（`supergateway` + `python -m opendesign_mcp_server`）由 `seed_migration.migrate_legacy_seed()`
  在 admin lifespan、子行程啟動前自動改寫（PaaS chart seed-if-absent，既有 PVC 不會自己更新）。
- 長時操作（`create_project` / `send_message` ≤300s）靠 core proxy `timeout=86400s` + `stream=True` +
  `x-accel-buffering:no` 不被代理層提前中斷；外部 client 也須容許對應長逾時。

## vendored 同步責任

只 vendor 單檔 `od_mcp_server.py`（@ `d6d157a`、byte-faithful），上游若改 daemon 事件 schema／新增 tool
需手動重 sync（整檔覆蓋並保留檔頭 provenance 註記）。已知上游程式碼債（`_register_project` 靜默吞錯、
`read_file`/`get_file_info` 路徑未 encode、零測試）vendor 後我方**不修**（保 byte-clean），僅此文件註記。

`input_policy.py` 與 `output_policy.py` 在載入 vendored 工具後安裝子程序 overlay，不修改上游單檔。
輸出 overlay 只處理 `list_connectors`、`list_runs`，按 HA OpenDesign 0.2.14 的有界欄位契約保留
connector 身分／狀態／工具名稱及 run 的 `id`、`projectId`、`status`；認證資料、內網 URL、
本機路徑、錯誤原文與工具描述不進 MCP 結果。HTTP 狀態、逾時、無效回應與連線失敗只回固定
公開代碼；管理頁仍依共用 proxy 處理工具權限；
上游若改這兩支工具的參數或回應 envelope，overlay 會拒絕不符結構，須重新審核契約。
