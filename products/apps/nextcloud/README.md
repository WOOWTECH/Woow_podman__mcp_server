# apps/nextcloud — Nextcloud MCP Admin（MCP：Nextcloud）

讓 MCP client（Claude、Hermes、pi-agent…）以**一個指定的 Nextcloud 帳號**工作：瀏覽與讀取檔案、讀行事曆與待辦、
建立／改寫／上傳檔案、刪除單一檔案。共 9 支工具。

- 與 `apps/litellm` 同一套單一映像 MCP Admin 設計：管理介面在 `/`、設定存 `config.json`、MCP 端點
  `/private_<token>/mcp`（token 在 Tokens 頁產生）、工具政策在 Tools 頁、MCP 子行程由 `McpProcessManager` 啟動。
- MCP server 本體是 WOOWTECH 自有、MIT 授權的 `nextcloud_mcp_server`（GitHub `WOOWTECH/Woow_nextcloud_mcp_server`），
  **原封 vendor、與來源 byte-identical**，來源 commit 與更新方式見 [VENDORED.md](VENDORED.md)。
- 以 **app password**（HTTP Basic）連 Nextcloud；不需要 OAuth、資料庫、Context Agent 或 LLM。
- **預設全開**（負責人 2026-10-25「預設全開」）：9 支工具都開；要限制時在 Tools 頁逐支停用（停用 4 支寫入／刪除＝唯讀）。
  2026-10-08～10-24 開通、出廠唯讀（只開 5 支讀取工具）的服務，第一次以新映像啟動時由 core 的 `owner_defaults` 一次性改成全開；之後租戶在 Tools 頁的限制會保留。
- PaaS 映像名稱維持 `nextcloud-mcp-admin`。舊版 OAuth Gateway＋Context Agent 設計（`apps/nextcloud-oauth-gateway`）
  已於 2026-10-08 自正式環境拆除並移出 repo；要查舊程式碼請看 git 歷史（commit 9a0fafa 之前）。

## 架構

```
MCP client ──https://<host>/private_<token>/mcp──▶ admin（uvicorn :8080，mcp-admin-core）
                                                   │  驗 path token；Tools 頁停用的工具在 proxy 層擋下（#1384）
                                                   ▼
                                     nextcloud-mcp-server（launcher，只綁 127.0.0.1:8000）
                                                   │  依 Tools 頁政策產生 READONLY／ALLOW_DELETE／DISABLED_TOOLS
                                                   ▼  vendored FastMCP：只註冊政策開啟的工具
                                     Nextcloud（base_url；WebDAV／CalDAV／OCS，HTTP Basic＝帳號＋app password）
```

| 元件 | 位置 | 說明 |
|---|---|---|
| admin | `nextcloud_mcp_admin/main.py` | `create_app(extra_routers=[config, tools, health, logs])`；啟動時先補安全預設（`seed_defaults.py`）並登記 app password 供 log 遮罩，再由 core lifespan 拉起子行程 |
| 連線設定 | `nextcloud_mcp_admin/routers/config.py` | `base_url`、`username`＋secret `app_password`（只寫不讀）；Test 測存檔值；`base_url` 存檔前過 SSRF 檢查；換使用者或網址會撤銷舊 MCP 網址、換新 token（見下） |
| 健康探測 | `nextcloud_mcp_admin/health_probes.py` | healthy 只由**帶帳密的** `get_file_tree("", 1)`（vendored 同一段程式：OCS 查 user id＋PROPFIND 家目錄）決定；版本讀免認證的 `/status.php`（快取 10 分鐘）；連出去之前先過 SSRF 檢查，被擋的網址一個請求都不送（Test 與 `/api/health` 共用） |
| 工具清單 | `nextcloud_mcp_admin/tool_registry.py` | 9 支工具給 core Tools 頁（名稱取自 vendored `READ_TOOLS`／`WRITE_TOOLS`／`DELETE_TOOLS`） |
| 政策套用 | `nextcloud_mcp_admin/tool_policy.py` | Tools 頁打開原本停用的工具時自動重啟子行程（子行程啟動時才註冊工具） |
| launcher | `nextcloud_mcp_admin/launcher.py` | console script `nextcloud-mcp-server`：固定 Streamable HTTP、**只綁 127.0.0.1**、由政策算 gating env、關 FastMCP banner／PyPI 版本檢查、httpx log 壓到 WARNING |
| MCP server | `nextcloud_mcp_server/` | vendored（FastMCP 3.4，`uv.lock` 釘版） |
| log 衛生 | `nextcloud_mcp_admin/log_redaction.py` | `/private_<token>` → `/private_***`；app password（含 HTTP Basic base64）出現在任何 log 都改成 `***`；遮罩後 `record.args` 的形狀不變（uvicorn access log 的 5 個引數照舊，那一行不會消失） |

## 工具

| # | 工具 | 類別 | 分級 | 預設 | 說明 |
|---|---|---|---|:---:|---|
| 1 | `get_file_tree` | files | read | ✓ | 列出資料夾內容（1–3 層，有數量上限） |
| 2 | `get_file_content` | files | read | ✓ | 取回 UTF-8 文字檔的純文字 |
| 3 | `read_text_file` | files | read | ✓ | 讀文字檔，連同 etag／大小／類型 |
| 4 | `list_calendars` | calendar | read | ✓ | 列出行事曆 |
| 5 | `list_tasks` | calendar | read | ✓ | 讀待辦事項（VTODO） |
| 6 | `create_text_file` | files | write | ✓ | 建立新文字檔，絕不覆蓋 |
| 7 | `update_text_file` | files | write | ✓ | 改寫文字檔，需讀取時的 etag |
| 8 | `upload_file` | files | write | ✓ | base64 上傳：不帶 etag 只能新建，帶 etag 只取代該版本 |
| 9 | `delete_file_checked` | files | destructive ⚠ | ✓ | 刪除單一檔案（etag 必須相符），進 Nextcloud 垃圾桶 |

所有改寫與刪除都以 ETag 保護：檔案在讀取後被別人改過就拒絕，不會蓋掉別人的修改。資料夾的建立、搬移、複製不在範圍內。

`upload_file` 一個檔案最多 10 MiB（`NEXTCLOUD_MCP_MAX_UPLOAD_BYTES`，PaaS 不開放修改），base64 後約 13.3 MiB 的請求。MCP 子行程與閘道的請求內容上限是 16 MiB（`mcp_admin_core/request_body.py`；MCP SDK 預設只收 4 MiB，launcher 啟動前以 `request_body.install()` 放寬）：超過 10 MiB 的檔案由子行程回可讀的工具錯誤，請求超過 16 MiB 則由閘道回帶 id 的 JSON-RPC 錯誤。

## 設定（Connection 頁）

| 欄位 | 說明 |
|---|---|
| `base_url` | Nextcloud 根網址，例 `https://cloud.example.com` 或 `https://example.com/nextcloud`。存檔時協定與主機名轉小寫、去尾斜線與預設埠（`:443`／`:80`），誤貼的 `/index.php/…`、`/apps/…`、`/remote.php/…` 尾段會切掉；只接受 http(s)，不可含帳密、`?`、`#`。Nextcloud 會轉址時請填最終網址（不跟隨轉址）。**請填對外網址**：私網、本機（loopback）、雲端 metadata 位址，以及解析到這些位址的主機名會被 SSRF 檢查擋下（HTTP 400，`detail.code` 是 `ssrf_blocked`，未存檔），見下節 |
| `username` | 登入名稱（可為 e-mail；LDAP 等 user id 不同的帳號，子行程會自動用 OCS 查真正的 user id）。**換掉它（或 `base_url`）會撤銷舊的 MCP 網址**，見下節 |
| `app_password` | 該帳號的 app password。只寫不讀：GET 只回遮罩；留空或送回遮罩＝沿用既有值 |

連線鍵經 `mcp_server.env_prefix="NEXTCLOUD_MCP_"` 注入子行程：`NEXTCLOUD_MCP_BASE_URL`、`NEXTCLOUD_MCP_USERNAME`、
`NEXTCLOUD_MCP_APP_PASSWORD`（**只走環境變數，不會出現在 argv**）。

### 換使用者或網址會撤銷舊的 MCP 網址（身分綁定）

MCP 網址（`/private_<token>/mcp`）永遠綁一個 Nextcloud 帳號。**身分**是 `base_url`、`username`（`main.py` 的
`LaunchPolicy.identity_connection_keys`，`routers/config.py` 的 `IDENTITY_KEYS`）；`app_password` 不是身分。修正前（2026-11-14
實測 F-3）把使用者由 bob 改成 carol，bob 的同一個 MCP 網址立刻讀到 carol 的資料，token 歷史沒有紀錄，`restart:false` 時舊身分
還繼續跑到下次重啟。現在與 Odoo 同一套（核心 `save_bound_connection`）：

- 換掉 `base_url` 或 `username` 時，**同一次原子寫入**撤銷舊 token（之後 403、進行中的 session 被中止）、換成新 token、並寫入
  新連線；token 歷史多一筆 `backend_identity_change`（只有遮罩過的前一個 token，不含帳號、網址或密碼）。舊子行程在寫入之前先停；
  寫入失敗時設定檔、舊 token 都不變，舊子行程以舊身分重新拉起。
- 子行程一定用新身分重啟（`restart: false` 也一樣）；回應 `token_rotated: true`（不帶 token）。管理員到 **Tokens 頁**複製新的
  MCP 網址，再更新 Claude.ai、Claude Code 等用戶端。
- 只換 `app_password`（同一個帳號重新產生 app password）**不換** token。第一次設定（之前沒有完整的 `base_url`＋`username`）、
  token 已撤銷或沒有 token 時也不換。清空 `base_url` 或 `username` 算換身分（換 token）；清空之後再填，前一個身分已經不完整，不再換。
- `base_url` 用**正規化後**的值比（`normalize_base_url`）：協定與主機名大小寫、尾斜線、誤貼的 `/index.php/…` 尾段、預設埠不同都不算換身分，
  存檔值沿用原本的寫法；**路徑的大小寫算不同**（伺服器可能分大小寫，寧可多換一次 token，也不漏換）。`username` 去前後空白後逐字比，
  大小寫不同算換。
- Connection 頁（`PUT /api/config/connection`）、共用的 `PUT /api/settings/connection`、額外帳號（`X-MCP-Profile`）的同兩條路徑行為相同
  （共用路徑用 `LaunchPolicy.normalize_connection` 套同一套驗證與正規化）；額外帳號只換自己的 token，預設帳號與其他帳號不受影響。
- log 不印使用者、網址或任何憑證；只有一行 INFO `Nextcloud connection identity changed; MCP token rotated`（容器預設不輸出 INFO，
  持久的紀錄是 Tokens 頁的 token 歷史）。
- 共用前端只顯示回應的 `message`、不讀 `token_rotated`；核心在子行程探測尚未確認套用時會把 `message` 換成通用文字，所以
  「到 Tokens 頁複製新網址」那句提示不一定顯示（存檔後 Tokens 頁的資料會重新載入）。

### base_url 的 SSRF 檢查

`base_url` 存檔、Test connection、儀表板健康探測（`/api/health`）三條路都過核心的 SSRF 檢查（`mcp_admin_core.util.ssrf.check_url`，
n8n、EMQX 同一套；規則對齊上游 n8n-mcp，**這個 app 沒有另寫規則**）：

- **存檔**（Connection 頁 `PUT /api/config/connection`、共用的 `PUT /api/settings/connection`、額外帳號的同兩條）：被擋回 HTTP 400，
  `detail = {"code": "ssrf_blocked", "field": "base_url", "reason", "message"}`，**不存檔、不套用到子行程**；`reason` 只有原因
  （例如 `Private IP addresses not allowed`），不含目標網址、位址或目標的回應。空的 `base_url`（尚未設定、有意清空）不檢查。
  宣告在 `main.py` 的 `LAUNCH_POLICY.url_connection_keys`（`routers/config.py` 的 `URL_FIELDS`）與 router 的 `refuse_blocked_urls`。
- **Test connection、`/api/health`**：連出去之前先檢查（`health_probes.check_nextcloud`）；被擋的網址**一個請求都不送**，回
  `Blocked by SSRF protection: <原因>…`。「埠關閉」與「有 HTTP 服務」的回答不再有差異，管理 API 不能拿來探測叢集內網。
  也涵蓋這次修正之前存下的舊設定。
- **模式**讀容器環境變數 `WEBHOOK_SECURITY_MODE`（預設 `strict`；只有部署端 chart 能設，租戶從管理頁改不了）：`strict` 只收公開位址；
  `moderate` 另放行 localhost／loopback；`permissive` 只擋雲端 metadata（IPv4／IPv6／隧道內嵌）。所有模式都拒絕帶帳密的網址。
  本機 docker compose 連內網或本機的 Nextcloud 時，要在 compose 的 `environment` 加 `WEBHOOK_SECURITY_MODE: permissive`
  （`moderate` 只放行 localhost）；PaaS 用公開網址，維持 strict。
- **不在範圍**：子行程連線時沒有第二道檢查（映像沒有 `backend_policy`，vendored client 沒有 SSRF 規則）。DNS 在檢查之後改指私網
  （DNS rebinding）、這次修正之前存下的舊設定被子行程照用，要靠 PaaS 的 mcp-server egress NetworkPolicy（依核心 `util/ssrf.py` 的說明：已排除叢集與私有網段，woow-paas-charts mcp-server ≥ 0.1.6；本 repo 無法驗證叢集設定）。

### 建立專用帳號與 app password

建議為 AI 助理建立**專用 Nextcloud 帳號**，不要用個人帳號：app password 能存取該帳號看得到的一切，Nextcloud 也無法把
app password 限制在特定資料夾。

1. 以管理員到「帳號」（Accounts／Users）新增使用者，例如 `assistant`，可視需要設定配額。
2. 只把需要的資料夾與行事曆**分享**給這個帳號（不需寫入的就用唯讀分享）。
3. 以該帳號登入 → 右上角頭像 →「個人設定」→「安全性」（Settings → Security）→「裝置與工作階段」，輸入名稱（例如 `MCP`）後按
   「建立新應用程式密碼」。
4. 複製只顯示一次的密碼，貼到 Connection 頁的 App password。日後可在同一處撤銷；啟用兩步驟驗證的帳號也能使用 app password。

## 工具政策（Tools 頁）

- **單一來源是 Tools 頁的 `tools.disabled`。** launcher 每次啟動子行程都從 `config.json` 重新計算並強制設定：
  - `NEXTCLOUD_MCP_READONLY`：有開任何寫入或刪除工具才是 `false`，否則 `true`；
  - `NEXTCLOUD_MCP_ALLOW_DELETE`：有開 `delete_file_checked` 才是 `true`；
  - `NEXTCLOUD_MCP_DISABLED_TOOLS`：所有被停用的工具。

  因此子行程註冊的工具＝Tools 頁開啟的工具。Settings 頁 `mcp_server.env` 手動填的同名鍵會被覆寫，不要用它來解鎖。
  讀不到 `config.json` 時以唯讀、不准刪除啟動（fail closed）。
- **停用**：儲存後由 core MCP proxy 立即擋下（`tools/list` 隱藏、`tools/call` 拒絕，#1384），不重啟、不中斷現有連線。
- **開啟原本停用的工具**：儲存後 admin 自動重啟 MCP 子行程以註冊新工具；現有 MCP session 會中斷，client 需重新連線。
- 新安裝（admin 第一次啟動時，`seed_defaults`）寫上 `tools.nextcloud_policy_seeded` 標記，不停用任何工具（9 支全開）。
  2026-10-25 之前開通的服務（當時出廠會停用 4 支寫入／刪除工具）第一次以新映像啟動時，由 core 的 `owner_defaults`
  一次性清空停用清單並寫上 `tools.owner_defaults_20261025` 標記；之後租戶的選擇一律尊重。

## PaaS seed 契約（給平台範本）

`config.nonSensitive`（chart 是 seed-if-absent：只影響新開通）：

```json
{
  "connection": { "base_url": "https://<同工作區 Nextcloud 對外網址>", "username": "" },
  "mcp_server": {
    "command": "nextcloud-mcp-server",
    "args": ["--port", "8000"],
    "port": 8000,
    "env_prefix": "NEXTCLOUD_MCP_"
  }
}
```

- seed **不帶** `tools`：新安裝的工具政策由 admin 第一次啟動時的 `seed_defaults` 決定（9 支全開＋寫上標記）。

- `envPrefix`（chart 頂層）也設 `NEXTCLOUD_MCP_`。`app_password` **絕不自動供給**，租戶在 Connection 頁自己貼。
- `mcp_server.port` 必須等於 args 的 `--port`，不一致 proxy 一律 502。
- 不需要 `env` 預填任何 `NEXTCLOUD_MCP_READONLY` 類的鍵（launcher 依政策覆寫）。
- 保險：admin 啟動時若沒有 `nextcloud_policy_seeded` 標記會補上新安裝政策（全開）、`env_prefix` 為空會補 `NEXTCLOUD_MCP_`。

## MCP client 設定

1. MCP auth token 在第一次啟動時自動產生（撤銷過的不會自動復活）。Tokens 頁的 **MCP URL** 直接顯示／複製完整網址（揭露 60 秒或離開視窗就隱藏）；要換 token 用 Rotate Token。
2. URL：`https://<服務網址>/private_<token>/mcp` ——**不要加結尾斜線**。
3. Transport：Streamable HTTP。

### 存好連線之前（新開的服務）

Connection 頁的三個欄位沒填完之前子行程起不來（vendored server 缺任何一個就以 exit 2 結束）。這時 MCP 用戶端
（Claude.ai、Claude Code…）照樣連得上，拿到的是閘道的佔位 MCP 伺服器（`mcp_admin_core/setup_status.py`），不再是 503：

- `initialize` 成功，`serverInfo` 是「Nextcloud MCP（尚未設定 not configured）」，`instructions` 以中英文寫明：請管理員登入
  `https://<服務網址>/`，到 Connection 頁（`https://<服務網址>/config`）填入 Nextcloud 網址（base_url）、Nextcloud 帳號
  （username）、App password（app_password）並按 Save。只寫欄位名稱，不會帶出 token、密碼或任何設定值。
- `tools/list` 只有一支 `setup_status`；呼叫任何工具都回 `isError: true`＋同一段說明（Claude 會轉述給使用者）。直接用瀏覽器
  打開 MCP 網址（GET）回 405，內容也是這段說明。
- 存好之後子行程自動啟動，但**已經連上的用戶端不會自己換成完整的工具**：帶著佔位 session 的下一個請求才會拿到 404（session
  不存在），依 MCP 規格重新 initialize 就是真的 9 支工具——前提是用戶端真的送出了請求。實測（Claude Code 2.1.311，同一個行程、
  同一段對話）：
  - 模型不再呼叫這個伺服器的工具時，Claude Code 不送任何 MCP 請求，工具清單停在 `setup_status`，模型只能請使用者自己重新連線
    （舊的說明只寫「重新連線就會看到完整的工具」時就是這樣）。
  - 模型再呼叫一次 `setup_status` 時：這次呼叫拿到 404，Claude Code 重新連線，用真的子行程重試這次呼叫（真的子行程沒有
    `setup_status`，回 `Unknown tool: 'setup_status'`），接著載入完整工具，`get_file_tree` 成功。所以說明與 `setup_status` 的
    工具說明都請模型「存好之後再呼叫一次 setup_status」，也說明重試回 Unknown tool 代表已經換好。
  - 改成這樣的說明之後，同一個自然追問（「管理員說已經存好連線了，請再列一次根目錄」，`--model haiku`）跑 3 次，3 次都是模型
    自己再呼叫一次 `setup_status`、Claude Code 重新連線、看到 Unknown tool 後改用 `get_file_tree` 列出檔案。
  - 使用者也可以手動重新連線：Claude Code 用 `/mcp` → 選這個伺服器 → Reconnect。
  - MCP Python SDK（1.30、2.3）與 TS SDK（1.32.2）把 404 當錯誤丟給呼叫端（Session terminated／Session not found／HTTP 404），
    要由應用自己重新連線，重連後是 9 支工具。Claude.ai 還沒實測（要部署到 staging）。
- 三個欄位都有、子行程卻沒在跑（當掉、正在重啟）時，說明改成請管理員看 Logs 頁（不帶子行程輸出）。子行程當掉前就連上、
  拿著真的 session 的用戶端這時呼叫工具也拿到說明（`isError: true`）；它的工具清單是真的工具、沒有 `setup_status`，所以這種
  請求的說明改成「子行程恢復後再呼叫一次原本要用的工具」——子行程重啟過的話那次拿到子行程的 404，Claude Code 會重新連線並
  重試。實測（Claude Code 2.1.311、`--model haiku`，子行程本來在跑、已經有 9 支工具，再停掉子行程）：3 次模型都在停機期間
  重試原本的 `get_file_tree`、沒有去找 `setup_status`，請使用者 `/mcp` → Reconnect 或請管理員看 Logs 頁；子行程恢復後再問，那次拿到 404、
  Claude Code 重新連線並重試成功。說明寫「再呼叫一次 setup_status」時，模型會去找 `setup_status`，只得到「No matching
  deferred tools found」。
- `GET /healthz` 恆 200，`mcp` 欄位：`not_configured`（三個欄位沒填完）、`running`、`not_running`；平台服務頁用它提示「尚未設定連線」。沒有 `backend` 欄位：三個欄位都是必要欄位（`required_connection`，缺了子行程就起不來），不另外宣告後端連線欄位（`backend` 只給「子行程起得來、但沒接上後端」的產品，見根目錄 README）。

## 開發與測試

```bash
# repo root
uv sync --all-packages --all-extras
uv run --directory apps/nextcloud pytest -q          # 不連任何真的 Nextcloud（httpx.MockTransport）
npm ci && npm run build -w apps/nextcloud/frontend
docker build -t woow-nextcloud-mcp-server -f apps/nextcloud/Dockerfile .
```

| 測試 | 內容 |
|---|---|
| `test_launcher.py` | argv（只綁 127.0.0.1、固定 http）、政策 → gating env、fail closed、祕密不在 argv、vendored 結束碼、launcher 不提早載入 fastmcp |
| `test_tool_registry.py` | 9 支／三級與 vendored 一致、core Tools router |
| `test_config_router.py` | 遮罩、留空沿用、網址正規化、Test 只測存檔值、app password 不進 log |
| `test_mcp_surface.py` | in-process MCP client 對 mock Nextcloud：新安裝 9 支、唯讀政策 5 支、讀寫刪實際打到 WebDAV／CalDAV |
| `test_health_probes.py` | 唯讀探測、帳密錯誤、轉址、斷線、版本只補細節 |
| `test_main.py` | app 組裝、seed、Tools 政策放寬才重啟、`/healthz` 的 `mcp` 欄位 |
| `test_setup_status.py` | 必要欄位與 Connection 頁標籤一致；端到端：真的 admin（uvicorn＋lifespan）spawn launcher，缺連線時 MCP Python SDK 與 fastmcp client 都拿到佔位伺服器（設定步驟、只有 `setup_status`、isError），`/healthz` 是 not_configured；經管理 API 存好連線後子行程起來、佔位 session 回 404、重新 initialize 是真的 9 支工具並讀到假 Nextcloud（HTTP） |
| `test_child_process.py` | McpProcessManager 真的 spawn launcher，經 HTTP 列出政策允許的工具；經 core proxy（公開 Host＋瀏覽器 Origin）可用、直連帶公開 Host 被擋；MCP Python SDK 經真的 admin（uvicorn）上傳 6 MiB 與 10 MiB 檔案到假 Nextcloud（HTTP）內容一致、10 MiB＋1 byte 由子行程回工具錯誤、請求超過 16 MiB 由閘道回帶 id 的錯誤且 session 照常可用 |
| `test_log_redaction.py` | path token、app password（含 Basic base64、traceback）遮罩；以真的 uvicorn `AccessFormatter` 驗證 access log 的 5 個引數在遮罩後形狀不變（tuple／dict／None／空值、非字串元素、遮罩文字與整句遮罩逐字相同、冪等） |
| `test_identity_bound_token.py` | 身分綁定：真的 admin app＋ConfigStore＋McpProcessManager，子行程是記下 start／stop／設定寫入順序的假 `nextcloud-mcp-server`。換使用者／網址／兩者（`restart` 真假）→ 舊網址 403、新網址讀到新身分、舊 session 中止、token 歷史 `backend_identity_change` 且不含值、先停舊子行程再一次寫入；只換密碼、第一次設定、已撤銷或沒有 token、網址寫法不同（大小寫、尾斜線、`/index.php/…`、預設埠）不換；清空身分；寫入失敗時舊 token 與設定檔不變、舊子行程以舊身分拉回；額外帳號只換自己的；共用 `/api/settings/connection` 同樣換；過期／缺少 If-Match 與 SSRF 擋下時什麼都不動；log 不含使用者、網址、憑證；回應訊息（假 PM） |
| `test_base_url_ssrf.py` | `base_url` 的 SSRF 檢查：存檔（Connection 頁、共用 `/api/settings`、額外帳號）被擋回 400 且設定檔不變、公開網址（埠、路徑、https、IDN、IPv6）不被誤擋、空值不檢查、strict／moderate／permissive；Test connection 與 `/api/health` 對被擋的網址一個請求都不送（真的 loopback 假伺服器計數）、埠關閉與有服務的回答相同；主機名由 `fake_dns` 測試替身解析，不查真的 DNS |

## 安全注意事項

- **path token＝整個 MCP 端點的憑證**；外洩時到 Tokens 頁輪替。PaaS 前面的 ingress／tunnel 若記錄完整 URL，那些 log 也要視為持有憑證。
- app password 以明文存在 PVC 的 `/data/config.json`（0600）；在 Nextcloud 撤銷後需回 Connection 頁重貼新的。
- **MCP 網址綁一個 Nextcloud 帳號**：換 `base_url` 或 `username` 會撤銷舊網址、換新 token（見「換使用者或網址會撤銷舊的 MCP 網址」）；同一個帳號重新產生 app password 不換。
- 子行程只綁 127.0.0.1、本身沒有認證：同工作區其他 pod 只能經 admin（驗 token）進來。
- 子行程另有 DNS-rebinding 防護：`Host` 不是 loopback 回 421、瀏覽器 `Origin` 不是 loopback 回 403。core proxy 轉發時會把 `Host` 改寫成 `127.0.0.1:<port>` 並丟掉 `Origin`，所以經 `/private_<token>/mcp` 使用不受影響，也**不需要**設定 `NEXTCLOUD_MCP_ALLOWED_HOSTS`（`test_child_process.py` 守線）。
- 子行程不跟隨轉址、不讀系統 proxy 環境變數（`trust_env=False`）、以 `defusedxml` 解析伺服器 XML。
- `base_url` 存檔與 Test connection 過 SSRF 檢查（見上節「base_url 的 SSRF 檢查」）；子行程連線時沒有第二道，這一層靠 egress NetworkPolicy。
- 本 app 與 vendored server 皆以 clean-room 方式撰寫，不含任何 AGPL 程式碼（見 [VENDORED.md](VENDORED.md)）。
