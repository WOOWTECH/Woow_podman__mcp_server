# apps/litellm — LiteLLM MCP Admin（MCP：AI 模型閘道）

讓 MCP client（Claude、Hermes、pi-agent…）透過 40 支工具管理一台 **LiteLLM AI 模型閘道**：查模型、發虛擬金鑰、管團隊與使用者、看用量與費用、檢查健康狀態、管理 Claude Code skill-hub 外掛。

- 來源：GitHub `WOOWTECH/Woow_litellm_mcp_server` main `4d41903`。MCP server 本體（`woow_litellm_mcp_server/`，40 支工具＋三層 gating）由該來源 vendor；本地 URL 正規化、registry 與主動操作權限修正見 [PARITY.md](PARITY.md)，目前不宣稱 byte-identical；admin 改接 monorepo 共用的 `packages/mcp-admin-core`／`packages/mcp-admin-ui`，丟掉來源 repo 自己 fork 的 core。
- PaaS 上架名稱「MCP：AI 模型閘道」，chart＝平台共用 `mcp-server`，image＝`woow-paas-docker-local/litellm-mcp-admin:<git-sha>`。
- **預設全開**（負責人 2026-10-25「預設全開」）：40 支工具都曝露；要限制時在 Settings 頁打開 Read-only Mode（只留 15 支查詢工具），或在 Tools 頁逐支停用。2026-10-25 之前開通、出廠唯讀的服務，第一次以新映像啟動時由 core 的 `owner_defaults` 一次性改成全開；之後租戶的限制會保留。

## 架構

```
MCP client ──https://<host>/private_<token>/mcp──▶ admin（uvicorn :8080，mcp-admin-core）
                                                   │  驗 path token、Tools 頁停用的工具在 proxy 層擋下（#1384）
                                                   ▼
                                     litellm-mcp-server（launcher，只綁 127.0.0.1:8000）
                                                   │  vendored FastMCP：gate 決定註冊哪些工具
                                                   ▼
                                     LiteLLM gateway（base_url，Bearer <金鑰>）
```

| 元件 | 位置 | 說明 |
|---|---|---|
| admin | `litellm_mcp_admin/main.py` | `create_app(extra_routers=[config, tools, health, logs])`；啟動時先補安全預設（`seed_defaults.py`），再由 core lifespan 拉起子行程 |
| 連線設定 | `litellm_mcp_admin/routers/config.py` | `base_url`＋secret `master_key`（只寫不讀）；Test 測存檔值 |
| 健康探測 | `litellm_mcp_admin/health_probes.py` | healthy 只由**帶金鑰**的 `GET /v1/models` 決定；`/health/readiness`（免認證）只補 DB 狀態；版本讀 `/openapi.json` 的 `info.version`（LiteLLM 1.103 的 readiness 已不回版本；每個網址快取 10 分鐘） |
| 工具清單 | `litellm_mcp_admin/tool_registry.py` | re-export vendored registry 給 core Tools 頁 |
| launcher | `litellm_mcp_admin/launcher.py` | console script `litellm-mcp-server`：固定 Streamable HTTP、**只綁 127.0.0.1**、關 FastMCP banner／PyPI 版本檢查、httpx log 壓到 WARNING |
| MCP server | `woow_litellm_mcp_server/` | vendored（FastMCP 3.x，`uv.lock` 釘版） |
| log 衛生 | `litellm_mcp_admin/log_redaction.py` | uvicorn access log 的 `/private_<token>` 改寫成 `/private_***` |

## PaaS seed 契約（給平台範本）

`config.nonSensitive`（chart 是 seed-if-absent：只影響新開通）：

```json
{
  "connection": { "base_url": "http://svc-xxxxxxxx-litellm:4000" },
  "mcp_server": {
    "command": "litellm-mcp-server",
    "args": ["--port", "8000"],
    "port": 8000,
    "env_prefix": "LITELLM_MCP_",
    "env": { "LITELLM_MCP_READONLY": "0" }
  }
}
```

（`"0"`＝全開，負責人 2026-10-25「預設全開」；2026-10-25 之前的範本寫 `"1"`，那些服務第一次以新映像啟動時由 `owner_defaults` 一次性改成 `"0"`。）

- `envPrefix`（chart 頂層）也設 `LITELLM_MCP_`。connection 鍵經前綴大寫注入子行程：`base_url` → `LITELLM_MCP_BASE_URL`、`master_key` → `LITELLM_MCP_MASTER_KEY`。
- `connection.base_url`：平台同工作區自動串接預填（**不含 /v1**）；`master_key` **絕不自動供給**，租戶在 Connection 頁自己貼。
- `mcp_server.port` 必須等於 args 的 `--port`，不一致 proxy 一律 502。
- `requiredFields` 只列 `admin_password`。
- 保險：admin 啟動時若 `mcp_server.env` 沒有 `LITELLM_MCP_READONLY` 會補 `"0"`（全開）、`env_prefix` 為空會補 `LITELLM_MCP_`。2026-10-25 之前開通、存著 `"1"` 的服務，第一次以新映像啟動時由 core 的 `owner_defaults` 一次性改成 `"0"` 並寫上 `tools.owner_defaults_20261025` 標記；之後租戶自己切回 `"1"` 會保留。

## 連線設定與金鑰

| 欄位 | 說明 |
|---|---|
| `base_url` | LiteLLM 網址，不含 `/v1`（例 `http://svc-xxxxxxxx-litellm:4000`）。存檔時去尾斜線、只去掉 origin 根路徑 `/v1`；保留 `/proxy/v1` 等前綴，只接受 http(s)，不可含帳密／query／fragment |
| `master_key` | 送成 `Authorization: Bearer`。只寫不讀：GET 只回遮罩；留空或送回遮罩＝沿用既有值 |

**AI 實際能做什麼由這把 key 的權限決定**（MCP 預設全開），不建議直接貼 master key。只想讓 AI 查資料時，在 LiteLLM 建一個 `proxy_admin_viewer`（唯讀管理員）使用者並替他發 key：

```bash
# 以 master key 呼叫 LiteLLM（只做一次）
curl -s -X POST "$LITELLM/user/new" -H "Authorization: Bearer $MASTER" -H 'content-type: application/json' \
  -d '{"user_id": "mcp-viewer", "user_role": "proxy_admin_viewer"}'
curl -s -X POST "$LITELLM/key/generate" -H "Authorization: Bearer $MASTER" -H 'content-type: application/json' \
  -d '{"user_id": "mcp-viewer", "key_alias": "mcp-viewer"}'
```

viewer key 可查所有模型、金鑰、團隊、使用者與用量，但不能新增／修改／刪除（LiteLLM 會回 401／403）。要讓 MCP 發 key、改模型，就貼有寫入權限的 key（唯讀模式若打開過，也要關閉）。

**還沒存好連線時**（第五輪 2026-10-27）：子行程照樣啟動、列出 40 支工具，但沒有 `base_url` 時連 `http://localhost:4000`（容器裡沒有 LiteLLM）、沒有 `master_key` 時不帶 Authorization（LiteLLM 管理 API 回 401），工具呼叫都會失敗。兩欄有一個沒填，公開的 `/healthz` 就回 `"backend": "missing"`（兩欄都填了是 `"configured"`；`mcp` 照舊是 `running`／`not_running`），平台服務頁據此顯示「尚未設定後端連線」的提示。平台預填了同工作區網址、金鑰還沒貼的新服務也是 `missing`。`configured` 只代表欄位都填了，網址或金鑰對不對請按 Test。

## 治理（gating）

| 層 | 設定位置 | 生效方式 |
|---|---|---|
| 唯讀模式 `LITELLM_MCP_READONLY` | Settings 頁「Read-only Mode」開關（寫入 `mcp_server.env`，`1`／`0`） | 子行程 gate 在註冊時移除所有非 `read` 工具；Save MCP Config 會自動重啟子行程 |
| per-tool 停用 | Tools 頁開關（`tools.disabled`） | core MCP proxy 即時強制：`tools/list` 隱藏、`tools/call` 回 `isError: true` 的工具結果（2026-10-26 前是 JSON-RPC error），不需重啟（#1384） |
| 分類／工具／操作停用 | Settings 頁 Environment Variables：`LITELLM_MCP_DISABLED_CATEGORIES`、`LITELLM_MCP_DISABLED_TOOLS`、`LITELLM_MCP_DISABLED_OPERATIONS`（JSON 或逗號分隔） | 子行程 gate 在註冊時移除；需重啟子行程 |

- 被 gate 移除的工具若被呼叫（client 快取了舊 tools/list），子行程回可讀原因（「唯讀模式」「被管理員停用」），不是「Unknown tool」。
- 寫入工具在送出前會再檢查一次 gate（第二道防線）。
- Tools 頁的開關**不反映**唯讀模式（唯讀時 25 支寫入或主動操作工具在子行程端就不存在）。

## 工具清單

| # | 工具 | 類別 | 方法 | LiteLLM 端點 | 操作 | 唯讀模式 | 破壞性 |
|---|---|---|---|---|---|:---:|:---:|
| 1 | `litellm_list_models` | models | GET | `/v1/models` | read | ✓ |  |
| 2 | `litellm_model_info` | models | GET | `/model/info` | read | ✓ |  |
| 3 | `litellm_model_group_info` | models | GET | `/model_group/info` | read | ✓ |  |
| 4 | `litellm_add_model` | models | POST | `/model/new` | create |  |  |
| 5 | `litellm_update_model` | models | PATCH | `/model/{model_id}/update` | update |  |  |
| 6 | `litellm_delete_model` | models | POST | `/model/delete` | delete |  | ⚠ |
| 7 | `litellm_chat_completion` | chat | POST | `/v1/chat/completions` | create |  |  |
| 8 | `litellm_token_counter` | chat | POST | `/utils/token_counter` | create |  |  |
| 9 | `litellm_generate_key` | keys | POST | `/key/generate` | create |  |  |
| 10 | `litellm_list_keys` | keys | GET | `/key/list` | read | ✓ |  |
| 11 | `litellm_key_info` | keys | GET | `/key/info` | read | ✓ |  |
| 12 | `litellm_update_key` | keys | POST | `/key/update` | update |  |  |
| 13 | `litellm_delete_key` | keys | POST | `/key/delete` | delete |  | ⚠ |
| 14 | `litellm_block_key` | keys | POST | `/key/block` | update |  | ⚠ |
| 15 | `litellm_unblock_key` | keys | POST | `/key/unblock` | update |  |  |
| 16 | `litellm_regenerate_key` | keys | POST | `/key/{key}/regenerate` | update |  | ⚠ |
| 17 | `litellm_create_team` | teams | POST | `/team/new` | create |  |  |
| 18 | `litellm_list_teams` | teams | GET | `/v2/team/list` | read | ✓ |  |
| 19 | `litellm_team_info` | teams | GET | `/team/info` | read | ✓ |  |
| 20 | `litellm_update_team` | teams | POST | `/team/update` | update |  |  |
| 21 | `litellm_delete_team` | teams | POST | `/team/delete` | delete |  | ⚠ |
| 22 | `litellm_team_member_add` | teams | POST | `/team/member_add` | create |  |  |
| 23 | `litellm_team_member_delete` | teams | POST | `/team/member_delete` | delete |  | ⚠ |
| 24 | `litellm_create_user` | users | POST | `/user/new` | create |  |  |
| 25 | `litellm_list_users` | users | GET | `/user/list` | read | ✓ |  |
| 26 | `litellm_user_info` | users | GET | `/user/info` | read | ✓ |  |
| 27 | `litellm_update_user` | users | POST | `/user/update` | update |  |  |
| 28 | `litellm_delete_user` | users | POST | `/user/delete` | delete |  | ⚠ |
| 29 | `litellm_spend_logs` | spend | GET | `/spend/logs` | read | ✓ |  |
| 30 | `litellm_global_spend_report` | spend | GET | `/spend/logs` | read | ✓ |  |
| 31 | `litellm_spend_calculate` | spend | POST | `/spend/calculate` | create |  |  |
| 32 | `litellm_health` | health | GET | `/health` | create |  |  |
| 33 | `litellm_health_readiness` | health | GET | `/health/readiness` | read | ✓ |  |
| 34 | `litellm_list_plugins` | plugins | GET | `/claude-code/plugins` | read | ✓ |  |
| 35 | `litellm_plugin_info` | plugins | GET | `/claude-code/plugins/{plugin_name}` | read | ✓ |  |
| 36 | `litellm_register_plugin` | plugins | POST | `/claude-code/plugins` | create |  |  |
| 37 | `litellm_delete_plugin` | plugins | DELETE | `/claude-code/plugins/{plugin_name}` | delete |  | ⚠ |
| 38 | `litellm_enable_plugin` | plugins | POST | `/claude-code/plugins/{plugin_name}/enable` | update |  |  |
| 39 | `litellm_disable_plugin` | plugins | POST | `/claude-code/plugins/{plugin_name}/disable` | update |  |  |
| 40 | `litellm_skill_hub` | plugins | GET | `/public/skill_hub` | read | ✓ |  |

合計 40 支、8 類；唯讀模式可用 15 支；破壞性 8 支。

`health`、`token_counter`、`spend_calculate` 使用 `create` 作為主動操作權限。Health 可能呼叫付費 provider 並保存健康紀錄；計算工具不做推論或計費，但可能下載圖片／tokenizer。舊 `read` 禁用仍有效。詳見 [產品契約、遷移與 rollback](PARITY.md)。

注意：
- `litellm_chat_completion` 會實際呼叫模型、花費額度（屬寫入類，唯讀模式下隱藏）。
- `litellm_regenerate_key` 在 LiteLLM 開源版回「Enterprise feature」（工具會說明改用刪除再建立）。
- `litellm_global_spend_report` 以 `/spend/logs` 在 client 端彙總（開源版沒有 `/global/spend/report`）。

## MCP client 設定

1. MCP auth token 在第一次啟動時自動產生（平台不 seed token；撤銷過的不會自動復活）。Tokens 頁的 **MCP URL** 直接顯示／複製完整網址（揭露 60 秒或離開視窗就隱藏）；要換 token 用 Rotate Token。
2. URL：`https://<服務網址>/private_<token>/mcp` ——**不要加結尾斜線**（`/mcp/` 會被子行程 307 轉到內部位址）。
3. Transport：Streamable HTTP。

## 安全注意事項

- **path token＝整個 MCP 端點的憑證**。本 app 的 log 會遮掉（uvicorn access log → `/private_***`），但 PaaS 前面的 ingress／tunnel 若記錄完整 URL，那些 log 也要視為持有憑證（來源 repo FINDING-006）。外洩時到 Tokens 頁輪替。
- 金鑰以明文存在 PVC 的 `/data/config.json`（0600）；LiteLLM 換 key 不會同步，需回 Connection 頁重貼。
- 子行程只綁 127.0.0.1：同工作區其他 pod 只能經 admin（驗 token）進來。
- `mcp-server` chart 的 egress 不排除私網（平台層議題，見平台筆記）。

## 與來源 repo 的差異

| 項目 | 來源 `Woow_litellm_mcp_server@4d41903` | 本 app |
|---|---|---|
| core | 自己 fork 的 `mcp_admin_core`（缺 security headers #1372、proxy 工具過濾 #1384；留著 `k8s/client.py`） | monorepo `packages/mcp-admin-core`（不 import k8s） |
| 前端 | 自己的 SPA，`package-lock.json` 未提交（OPEN-2） | 共用 `@woow/mcp-admin-ui`＋`appConfig.jsx`，root `package-lock.json` 釘版 |
| image | Dockerfile 把 SPA 放 `litellm_mcp_admin/static/`，core 找 `$CWD/static` → 沒有 GUI；pip 不釘版；無 image CI | SPA 在 `/app/static`（建置期自檢）；依賴由 `uv.lock` 釘版；CI docker build＋`/healthz` smoke，main 推 JCR |
| 部署 | 自帶 chart：每次起 pod `git clone`＋`npm install`＋`pip install`（無法釘 commit） | 平台共用 `mcp-server` chart＋釘 digest 的 image |
| 子行程 bind | seed 決定（預設 `0.0.0.0`） | launcher 強制 `127.0.0.1` |
| 預設權限 | 全開（40 支） | 全開（40 支，2026-10-25 起；之前開通、出廠唯讀 18 支的服務第一次以新映像啟動時一次性改成全開）；admin 啟動時缺鍵補 `LITELLM_MCP_READONLY="0"` |
| 連線鍵 | `litellm_mcp_base_url`／`litellm_mcp_master_key`（`env_prefix=""`） | `base_url`／`master_key`（`env_prefix="LITELLM_MCP_"`，配合平台自動串接） |
| Test／Dashboard | 帶 key 的 `/v1/models`＋readiness | 相同判定（沿用） |
| Tools／Permissions 頁 | 自己的 tools router（寫 `LITELLM_MCP_DISABLED_*` env 並重啟）、Permissions 頁 | core Tools 頁（proxy 即時強制）＋Settings 頁唯讀開關；不提供 Permissions 頁 |
| log | path token 會出現在 edge log；httpx INFO 會印 `key_info` 的金鑰 query | admin access log 遮 token；子行程 httpx 壓 WARNING |
| `.bak` 備份 | PVC 上留含憑證的 `config.json.*.bak`（OPEN-3） | 不產生備份；`.gitignore` 排除 `config.json*`、`*.bak` |
| MCP path | `/mcp/` | `/mcp`（monorepo 慣例） |

## 開發與測試

```bash
uvx --from 'uv==0.6.4' uv sync --all-packages --all-extras --frozen   # lock 用 uv 0.6.x
uv run --directory apps/litellm pytest -q                              # 工具／gating（移植）＋admin 測試
npm ci && npm run build -w apps/litellm/frontend
docker build -t woow-litellm-mcp-server -f apps/litellm/Dockerfile .    # build context = repo root
```

真 LiteLLM 探測測試（`tests/test_live_litellm.py`）預設不跑，`pytest -m live` 才跑。
