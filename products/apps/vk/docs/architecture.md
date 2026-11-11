# VK MCP Admin — 架構

與其他 app 一致，本 app 是共用核心 `mcp-admin-core` 之上的薄殼（thin shell），包
supergateway + vibe-kanban-mcp。管理面（Login / Settings / Tokens / Proxy）全由共用 core
`create_app` 強掛，app 只注入 app-specific 的 connection config / tool registry / health probes /
前端 appConfig。

## 執行鏈

```
Claude / 客戶端
   │  POST /private_{TOKEN}/mcp
   ▼
vk admin（uvicorn :8080，mcp_admin_core.create_app）
   │  core proxy 透明轉發 → 127.0.0.1:8000/mcp   （proxy 目標 = mcp_server.port）
   ▼
supergateway（:8000，streamableHttp，/healthz）    ← McpProcessManager spawn 的子行程
   │  stateless：每個 POST /mcp spawn 一次 stdio 子行程
   ▼
vibe-kanban-mcp --mode global（Rust，musl）        ← 繼承 env VIBE_BACKEND_URL
   │  HTTP /api/*
   ▼
VK Host API（vibe_backend_url，例 http://vk-host:3000）
```

管理面路由（皆共用 core 現成）：

- `create_app` 強掛：Login / Settings / Tokens / MCP Proxy（`/private_{token}/mcp`）。
- app 以 factory 注入：`/api/config`（app-local 連線模型）、`/api/tools`（core
  `create_tools_router(registry=…)`）、`/api/health`（core `make_health_router(probe/fetch=…)`）、
  `/api/logs`（core `create_logs_router("vk_mcp_admin")`）。
- `/healthz` 永遠可用：零設定啟動時 `command=""` → `pm.start()` 印 warning 跳過，admin 照常聽
  8080，CI `/healthz` smoke 因此成立。

## 連線 → 子行程 env

`config.json` 的 `connection` 由 `process._build_subprocess_env` 大寫並加 `env_prefix`（本 app 為
`""`）後注入子行程，再經 supergateway env 繼承傳遞給 vibe-kanban-mcp：

| connection 鍵 | 注入 env | 消費者 |
|---|---|---|
| `vibe_backend_url` | `VIBE_BACKEND_URL`（`env_prefix=""` + key 大寫） | vibe-kanban-mcp 的 backend URL 來源（打 VK Host `/api/*`） |

鍵名 `vibe_backend_url` 一經定案即等同 env 名 `VIBE_BACKEND_URL`，**後端 router、前端 appConfig、
部署 seed 三處必須同名、不可改**。VK Host 現無認證（靠 Pod 網路邊界），故連線模型只有這一個非
secret 欄；未來若加認證欄，config router 已留 `drop_blank_secrets` 前瞻掛點。

## 與 k8s 解耦

app 只重啟自己 spawn 的 MCP 子行程（`McpProcessManager.restart()`，SIGTERM + 重起），
**不呼叫 k8s API、不引入 k8s client**。設定為 file-based（`config.json` 在 PVC 的 `/data`），
無 k8s Secret / ConfigMap 後援。service 級 rollout（整個 Deployment / Pod）交給 PaaS 平台。

來源 repo `entrypoint.sh` 的「背景雙行程（supergateway + uvicorn）」做法已廢除：supergateway 全交
McpProcessManager 依 `mcp_server.command` 於執行期 spawn，image CMD 只跑 uvicorn admin。這樣設定變更
（Save & Restart）才會第一次真正生效（來源做法因 `command=""` 導致所有 Save & Restart 皆為 no-op）。

## session 模式：stateless（預設）

supergateway 維持 **stateless**（不加 `--stateful`）：每個 `POST /mcp` 即時 spawn 一次 stdio 子行程
（vibe-kanban-mcp），請求結束即回收，**無長命 Rust 孫行程**。

- 好處：大幅降低 orphan 子行程風險（core `stop()` 只對主 PID 送訊號，SIGKILL 時無機會清孫行程）。
- 代價：每請求冷啟一次 binary + 重抓 context，高頻呼叫延遲放大。
- stateful（共用長命子行程、需 sessionTimeout 策略）會重新引入孫行程風險，屬未決 open question——
  待 WF2 / WF3 實測延遲後再與 user 定案。

執行期工具數註記：binary 以 `--mode global` 啟動、抓不到 workspace context 時會移除 `get_context`
（執行期 33 或 34 個）；tool registry 為靜態 UI 展示，一律採全集 **34**。
