# Hermes MCP Admin — Cloud Service 契約（O-1）

> 給 paas-charts（建 Secret / ConfigMap / initContainer）與 paas-platform 的硬依賴。
> pilot=hermes；odoo/n8n 對外結構相同，差異僅容器內（internal port / transport / `env_prefix`）。
> 來源：`packages/mcp-admin-core/mcp_admin_core/config/store.py:35-54`、`.gitea/workflows/ci.yml`、
> `apps/hermes/hermes_mcp_server/launcher.py`。

## Image 座標

| 項目 | 值 |
|------|----|
| Pull 座標 | `jcr-prod.woowtech.io/woow-paas-docker-local/hermes-mcp-admin:<tag>` |
| Tag 來源 | `.gitea/workflows/ci.yml`：每次 main push 推 `:<git-sha>`（不可變）+ `:latest`（移動式）。**建議 chart pin `:<git-sha>`** |
| CI push registry | 叢集內網 `artifactory-jcr-artifactory-nginx.jfrog.svc.cluster.local/...`（繞 Cloudflare 上傳上限）；對外 pull 走 `jcr-prod.woowtech.io` |
| 對外 port | `8080`（`EXPOSE 8080` + `CMD uvicorn --port 8080`，硬編、不讀 PORT env） |
| Volume | `VOLUME /data`（存唯一設定檔 `config.json`） |

## 對外路由（同一 process / 同一 port 8080）

| Path | 用途 |
|------|------|
| `/` | admin GUI（SPA，須掛 host root，**不可跑 subpath**） |
| `/api/*` | admin REST（含 hermes 的 `/api` dashboard 反代、SSE `/api/logs/stream`） |
| `/healthz` | health endpoint，`GET → {"status":"ok"}`（**probe 用 httpGet `:8080/healthz`**） |
| `/private_{token}/mcp` | MCP client 端點（Streamable HTTP，建議）。**ingress/tunnel 不可改寫/strip 此前綴**，否則 token 驗證失敗回 403 |
| `/private_{token}/sse` | legacy SSE 端點（舊 client 相容）；`endpoint` 事件由 proxy 補上前綴，回傳通道為 `/private_{token}/messages/` |

MCP 子行程在容器內 `127.0.0.1:8000`（loopback、不對外、不需 Service），由 admin reverse proxy 反代。

> **proxy 對 FastMCP DNS-rebinding 保護的處理（TransportSecurityMiddleware 驗 Host+Origin）**：
> 反代轉發給子行程時，把 `Host` 正規化為 `127.0.0.1:{mcp_server.port}`（裸 host 會回 **421**），
> 並 **drop 外部 `Origin`**（瀏覽器型 web MCP client 送的 `Origin: https://<host>` 屬非 loopback，
> 會被回 **403**；FastMCP 對「缺 Origin」短路放行）。**MCP endpoint 不得透傳外部 Origin。**
> ⇒ ingress/tunnel **不需**（也不應）改寫或 strip `Origin`，proxy 內部已處理；瀏覽器型 MCP client 可正常連線。

## config.json 完整 schema + 敏感切分

頂層預設來自 `_DEFAULT_CONFIG`（`store.py:35-54`）；`connection` 子鍵為 hermes 專屬
（`apps/hermes/hermes_mcp_admin/routers/config.py:46-84` 的 `DualConnectionUpdate`；該 PUT 是
**partial patch**——未送出的欄位保留存檔值，只補其中幾格不會清掉其餘的）。

| key path | 型別 | 預設 | 敏感 | seed 來源 |
|----------|------|------|------|-----------|
| `admin_password` | str | `"admin"` | 🔴 SECRET | Secret |
| `mcp_auth_token` | str | `""` | 🔴 SECRET | Secret（或啟動產生） |
| `connection.gateway_url` | str | — | ⚪ 設定 | ConfigMap（形狀見下方「兩種部署形態」） |
| `connection.gateway_api_key` | str | — | 🔴 SECRET | Secret |
| `connection.dashboard_url` | str | — | ⚪ 設定 | ConfigMap（形狀見下方「兩種部署形態」） |
| `connection.dashboard_username` | str | — | 🟡 憑證 | Secret（建議） |
| `connection.dashboard_password` | str | — | 🔴 SECRET | Secret |
| `tools.disabled` / `disabled_operations` | list / dict | `[]` / `{}` | ⚪ 設定 | ConfigMap/預設 |
| `mcp_server.command` | str | `""` | ⚪ 設定 | ConfigMap（`"hermes-mcp-server"`） |
| `mcp_server.args` | list | `[]` | ⚪ 設定 | ConfigMap（`["--host","127.0.0.1","--port","8000"]`） |
| `mcp_server.port` | int | `8000` | ⚪ 設定 | ConfigMap（內部 loopback 埠） |
| `mcp_server.env` | dict | `{}` | 🟡 視內容 | 若放密鑰走 Secret |
| `mcp_server.env_prefix` | str | `""` → **`"HERMES_"`** | ⚪ 設定 | ConfigMap |
| `proxy.timeout` | int | `86400` | ⚪ 設定 | 預設 |
| `proxy.bearer_token` | str | （未設, optional） | 🔴 SECRET（若啟用） | Secret |
| `token_history` | list | `[]` | ⚪（僅遮罩 token） | 執行期產生，勿 seed |

**另一個不在 config.json、但屬同一份 Secret 的密鑰**：`JWT_SECRET`（**env var**）— 🔴 SECRET，
未設則每次重啟隨機產生致登入 token 失效，正式環境必須以 env 從 Secret 固定。

`env_prefix="HERMES_"` 使 `connection` 各鍵大寫加前綴注入子行程（`gateway_api_key` →
`HERMES_GATEWAY_API_KEY`），對齊 `hermes_mcp_server/server.py:65-73` 的讀取。

## `gateway_url` / `dashboard_url` 的兩種部署形態（**照抄前先確認自己是哪一種**）

這兩個值指向的是 **Hermes Agent**（被管理的對象），不是本 admin 容器。正確形狀取決於
Hermes Agent 那端「對外開的是哪個 port」，**沒有一體適用的預設值**（GUI 因此不預填，
只給 placeholder 與 help）：

| 部署形態 | Hermes Agent 對外 | `gateway_url` | `dashboard_url` |
|---|---|---|---|
| **A. 直連 Service / docker-compose**（agent 兩個 port 都對外） | `8642`（gateway）+ `9119`（dashboard） | `http://<agent-host>:8642` | `http://<agent-host>:9119` |
| **B. 反向代理在前**（woow paas 目前形態） | 只有 `8080`（nginx authproxy），由它分流 `/`→dashboard(9119)、`/gateway/`→api(8642) | `https://<service-root>/gateway` | `https://<service-root>` |

> ⚠ **形態 B 沒有 `:8642` / `:9119` 這種直連埠**——那兩個 port 不在 Service 上，照抄形態 A
> 的形狀會連不上。反過來，形態 A 也不能套 `/gateway` 子路徑。
>
> 兩種形態都**必須帶 `http://` / `https://` scheme**：少了 scheme 或整格留空，MCP 工具與
> Test Connection 會回 `... is not configured` / `must start with http:// or https://`
> （守衛在 `hermes_mcp_server/server.py` 的 `_require_url()`），不會再出現誤導人的
> `Request URL is missing an 'http://' or 'https://' protocol.`。

## seed 範例（initContainer 產生的 `/data/config.json`）

以下為 **形態 B（woow paas，反向代理在前）**；形態 A 請把 `connection` 兩個 URL 換成上表
A 列的形狀，其餘不變。

```json
{
  "admin_password": "<from Secret>",
  "mcp_auth_token": "<from Secret>",
  "connection": {
    "gateway_url": "https://<service-root>/gateway",
    "gateway_api_key": "<from Secret>",
    "dashboard_url": "https://<service-root>",
    "dashboard_username": "<from Secret>",
    "dashboard_password": "<from Secret>"
  },
  "mcp_server": {
    "command": "hermes-mcp-server",
    "args": ["--host", "127.0.0.1", "--port", "8000"],
    "port": 8000,
    "env_prefix": "HERMES_"
  }
}
```

## 生命週期：GUI-owned / seed-if-absent（spec 拍板）

- initContainer **只在 `/data/config.json` 不存在時** seed 初始值；之後 GUI runtime 變更
  持久化於 PVC、pod 重啟保留，**不全量覆寫**。
- 代價：Secret 輪替不自動生效（pilot 接受；後續可加 reconcile）。
- 映像 / 啟動腳本不需改（Option A）：seed 邏輯全在 chart（Secret + ConfigMap + initContainer）。
