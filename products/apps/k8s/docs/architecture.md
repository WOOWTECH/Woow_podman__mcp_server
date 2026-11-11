# K8s MCP Admin — 架構

比照 `apps/odoo-manage`，本 app 是共用核心之上的薄殼（thin shell），包上游 Go binary
`kubernetes-mcp-server`（v0.0.65）。**admin 後端自身不呼叫 k8s API、不引入 kubernetes python
client**；連叢集做認證操作的是子行程（內建 client-go）。admin 只管檔案（config.json）與行程。

## 元件

```
瀏覽器 ── HTTP :8080 ──▶ FastAPI（mcp_admin_core.create_app，title="K8s MCP Admin"）
                          ├─ AuthMiddleware（JWT / admin password）
                          ├─ /api/config           ← app-local（kubeconfig / in_cluster 兩欄）
                          ├─ /api/config/test       ← app-local（混合分流 Test）
                          ├─ /api/tools             ← core create_tools_router(registry=…, mcp_label="k8s-mcp")
                          ├─ /api/health            ← core make_health_router(app_type="k8s", probe/fetch=…)
                          ├─ /api/logs/stream       ← core create_logs_router("k8s_mcp_admin")
                          ├─ /api/tokens /settings /login  ← core（create_app 內建）
                          ├─ /private_<token>/mcp proxy    ← core proxy（唯一支援 transport）
                          └─ SPA（@woow/mcp-admin-ui，appConfig 驅動）
                                     │
                          McpProcessManager（core）
                                     │ spawn（config.mcp_server.command = "k8s-mcp-http"）
                                     ▼
                          launcher（k8s-mcp-http）── execvpe ──▶ kubernetes-mcp-server（Go，34–35 tools）
                                     │                                      │ client-go
                                     │ 派生檔                                ▼
                          /data/kubeconfig、/data/k8s-mcp.toml        Kubernetes API server
```

## config.json 單一權威與派生檔

`config.json`（env `MCP_ADMIN_CONFIG`，預設 `/data/config.json`、寫檔 0600）是唯一權威來源。
launcher 於每次子行程啟動（含 restart）時，從中派生兩個檔案——**皆為 spawn 派生物、每次 restart
重寫**、不是權威來源：

| 派生檔 | 來源 | 用途 | 生命週期 |
|---|---|---|---|
| `/data/kubeconfig`（0600） | `K8S_MCP_KUBECONFIG`（由 `connection.kubeconfig` 注入） | `--kubeconfig` 指向的路徑 | in-cluster 或 kubeconfig 空時**主動刪檔**；每次 restart 依現值重寫 |
| `/data/k8s-mcp.toml`（0600） | 三顆治理 env + `config.json` 的 `tools.disabled` | 上游 `--config` TOML | 每次 restart 重寫 |

launcher 於 `execvpe` 前 `env.pop` 已消化的 `K8S_MCP_*` 七鍵（`KUBECONFIG` / `IN_CLUSTER` /
`READ_ONLY` / `DISABLE_DESTRUCTIVE` / `ALLOW_SECRETS` / `TOOLSETS` / `MULTI_CLUSTER`），避免 kubeconfig 全文與治理
旗標隨環境落進 Go 子行程的 `/proc/<pid>/environ`。

## 連線 → 子行程 env

`config.json` 的 `connection` 由 `process._build_subprocess_env` 大寫並加 `env_prefix`
（本 app 為 `"K8S_MCP_"`）注入子行程：

| connection 鍵 | 注入 env | launcher 消化為 |
|---|---|---|
| `kubeconfig` | `K8S_MCP_KUBECONFIG`（YAML 全文） | `/data/kubeconfig`（路徑語意）→ `--kubeconfig` |
| `in_cluster` | `K8S_MCP_IN_CLUSTER`（`"true"`/`"false"`） | true 時刪 kubeconfig 派生檔、不帶 `--kubeconfig`（走 Pod SA） |

> ⚠️ `env_prefix="K8S_MCP_"` 是本 app 的命脈。core 的 `put_section` 對 `mcp_server` 走淺合併
> （`store.patch`）保留 `env_prefix`，故 Settings 頁 Save MCP Config 不會抹掉它。

## 單一通道規則（flag vs TOML，嚴禁重複）

每個設定鍵只走 flag 或 TOML **其一**：

| 設定 | 通道 | 值 |
|---|---|---|
| port | flag | `--port {mcp_server.port}`（預設 8000） |
| bind-address | flag | `--bind-address 127.0.0.1`（**硬編**安全邊界，不受 env/seed 影響） |
| disable-multi-cluster | flag | `--disable-multi-cluster`（`K8S_MCP_MULTI_CLUSTER` 關閉或缺鍵時才帶；預設多叢集開；關閉時上游忽略呼叫端帶的 `context`、一律用 current-context，見 README） |
| kubeconfig | flag | `--kubeconfig /data/kubeconfig`（in-cluster 時省略） |
| config（TOML 路徑） | flag | `--config /data/k8s-mcp.toml` |
| read_only | TOML | `read_only`（`K8S_MCP_READ_ONLY`） |
| disable_destructive | TOML | `disable_destructive`（`K8S_MCP_DISABLE_DESTRUCTIVE`） |
| toolsets | TOML | `toolsets`（`K8S_MCP_TOOLSETS`；新安裝 `config,core,helm,kcp,kubevirt,tekton`，缺鍵時 `["core","config","helm"]`） |
| disabled_tools | TOML | `disabled_tools`（`config.json` 的 `tools.disabled`） |
| denied_resources | TOML | `[[denied_resources]]`（Secret 封鎖；`K8S_MCP_ALLOW_SECRETS` 開＝移除，新安裝為開、缺鍵為關） |
| confirmation_fallback | TOML | 顯式 `"allow"` |

## Test Connection 兩路徑（混合分流）

`POST /api/config/test` 不吃 body、一律測「已存檔設定」，依子行程狀態分流，訊息分層誠實：

```
POST /api/config/test
   │
   ├─ pm.is_running == True  ──▶ 路徑一（真端到端）
   │      mcp_client.call_tool_via_mcp(port, "namespaces_list", {}, timeout=15s)
   │      Streamable HTTP：initialize → notifications/initialized → tools/call
   │      ├─ ok            → success，details.auth = "mcp_tools_call"
   │      ├─ isError+forbidden → success（警語：已連上、憑證有效但 RBAC 受限）
   │      ├─ McpTimeout    → fail「MCP server 有回應但叢集 API 未在時限內回應」
   │      └─ McpUnavailable → fail「子行程執行中但 MCP HTTP 尚未就緒（可能剛重啟）」
   │
   └─ pm.is_running == False ──▶ 路徑二（降級 reachability）
          cluster_probe.resolve_target → fetch_version（匿名 GET {server}/version）
          success=False（不宣稱認證通過），details.auth = "reachability_only"
          ├─ 200          → 附 gitVersion
          ├─ 401/403      → 「匿名 /version 被拒，叢集可達」
          └─ ConnectError → 網路不可達訊息
```

> 匿名 `GET /version` 可達性探測屬 **target-app 健康檢查慣例**（比照 `apps/odoo-manage` 打
> `/web/health`；k8s app 的 target 恰好是 API server），**不違反**「不引入 k8s client／不管理叢集
> 資源」鐵律——認證路徑一律交給子行程 client-go。health probe 與 Test 路徑二共用同一 helper。

## per-tool toggle 傳導鏈

Tools 頁停用某工具後，經下列鏈路 restart 生效：

```
Tools 頁 toggle ──▶ PUT /api/tools ──▶ config.json 的 tools.disabled ──▶ pm.restart()
   ──▶ launcher 重讀 config.json ──▶ 產 /data/k8s-mcp.toml 的 disabled_tools
   ──▶ 子行程以新 TOML 重啟 ──▶ tools/list 不再曝露該工具
```

## 與 k8s 解耦

app 只重啟自己 spawn 的 MCP 子行程（`McpProcessManager.restart()`，SIGTERM + 重起），**不呼叫
k8s API、不做 service 級 rollout**。整個 Deployment / Pod 的 rollout 交給 PaaS 平台。
連叢集的認證與資源操作，全由子行程 `kubernetes-mcp-server`（client-go）負責。
