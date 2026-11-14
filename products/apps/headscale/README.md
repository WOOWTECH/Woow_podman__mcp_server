# Headscale MCP

一個實例管理一個組織的 Headscale 控制伺服器。專用介面提供連線設定、公開註冊入口、裝置、路由與存取政策；原生 API adapter 位於 `headscale_mcp_server`，管理介面與 launcher 位於 `headscale_mcp_admin`。EMQX 僅是參考，沒有 EMQX 套件匯入或前端建置相依。

## 執行與連線契約

- 管理程式：`uvicorn headscale_mcp_admin.main:app --host 0.0.0.0 --port 8080`。
- 子行程：`headscale-mcp-server` → `headscale_mcp_admin.launcher:main`，固定只監聽 `127.0.0.1:8000`，Streamable HTTP `/mcp`。管理員不可用設定檔改成任意執行命令。`base_url` 與 `api_key` 是 `LaunchPolicy.required_connection`：兩個都存好之前，任何設定檔（含預設）都不啟動子行程（沒有金鑰時 launcher 會 exit 1；2026-11-13 之前預設設定檔會被看門狗每 2、5、15…秒重啟），存好連線時才啟動。
- 對外 GUI 為 `/`；MCP 用戶端使用管理介面發出的 `/private_<token>/mcp`。管理密碼、MCP token、Headscale API key、PaaS API token 是不同用途的憑證。
- 狀態位於 `MCP_ADMIN_CONFIG=/data/config.json`，PVC 掛載 `/data`；uid/gid `1000:1000`。升級保留既有設定，chart initContainer 僅在檔案不存在時 seed。
- 前端直接複製 `static/` 到 `/app/static`，不執行 npm、不使用共用 React 管理殼。Python wheel 安裝只涵蓋兩個 Python package；正式映像另行攜帶靜態資產。

管理登入／登出的 `mcp-admin-token` cookie 預設帶 `Secure`，外部入口必須使用 HTTPS。
`HEADSCALE_MCP_SECURE_COOKIE` 是部署環境旗標，預設 `true`；只接受 `true`／`false`
（大小寫不拘、可有首尾空白），其他值包含空字串會拒絕啟動。旗標不讀取 `Origin`
或 forwarded headers，Cloudflare 到容器的內部 HTTP 不會取消 Secure。
僅在本機 HTTP 開發時明確設定 `HEADSCALE_MCP_SECURE_COOKIE=false`；PaaS 保持預設。

### 0.1.1 管理入口與登入狀態

本版支援的管理契約為 **Headscale v0.29.4 的 29 個原生 API operations**。
連線測試只驗證健康與 API 認證；不能用它宣稱其它 Headscale 版本也完整相容。
`DebugCreateNode` 屬測試工具，正式預設停用；仍保留隔離驗證與明確啟用方式。

部署時必須明確指定允許的瀏覽器 Origin。使用既有 chart 的 `extraEnv`：

```yaml
extraEnv:
  - name: HEADSCALE_MCP_ALLOWED_ORIGINS
    value: "https://headscale-mcp.example.com"
```

多個來源用逗號分隔，每個都必須是完整 origin（scheme、hostname、可選 port），
不包含路徑、query 或萬用字元。沒有配置時，帶 Origin 的請求拒絕；不帶 Origin 的
原生 MCP client 仍須通過既有 token 驗證。代理轉送的 Host／forwarded headers
不會自動加入可信清單。公開網址變更時，部署者也要同步更新此設定。
Standalone 的本機 HTTP 範例另外加上
`-e HEADSCALE_MCP_ALLOWED_ORIGINS=http://127.0.0.1:8080`，並以相同網址開啟 GUI。

管理者登出會撤銷當次登入；其餘瀏覽器登入維持有效。撤銷狀態與 `config.json`
位於同一持久目錄的 `admin-sessions.sqlite3`：管理程式每次啟動時沒有就建立，內容只有隨機
epoch 與已登出登入的雜湊，所以設定卷的備份裡會有它（可能還有 `-journal` 等附屬檔）。
還原一律照[設定卷還原手冊](../../docs/paas-config-restore-runbook.md)用 `restore_inplace`，映像要含
「略過 `admin-sessions.sqlite3`」的 guard 修正（沒有的映像，例如 e0a80d6，會在備份檢查與 `prepare`
拒絕）。還原工具不讀、不複製、不還原這個檔與它的 `-journal`／`-wal`／`-shm`，live 卷上原有的也隨
其他舊資料丟棄；服務還原後第一次啟動時建立新的空資料庫（新 epoch）。所以還原前的管理登入，
包括備份之後才登出的，都不會再被接受（還原另外換掉了管理密碼與 `admin_session_generation`），
重設密碼後重新登入即可。不要把備份裡的這個檔手動放回 live 卷，也不要單獨回滾這個檔。
升級後舊版沒有獨立 session 識別的 GUI 登入需要重新登入一次；不更換 MCP token、
Headscale API key 或已配對裝置的身分。既有密碼變更機制仍會令所有管理登入失效。

**回復舊版：** 舊版不辨識 session 撤銷資料。回復前先安全更新持久設定中的
`admin_session_generation`，讓所有 GUI 管理者重新登入，避免已登出的舊 token 被接受。
不以還原 Headscale 資料庫處理 MCP 版本回復。還原較早的管理狀態備份時同樣要更新
generation，因為舊備份可能沒有後來的撤銷記錄。

### 寫入結果與發布驗收

有些寫入在上游已接受後才發生網路中斷。遇到 `outcome=unknown` 時，先查詢目標狀態，
不要自動重送建立、刪除、配對、金鑰或政策操作。錯誤回傳不包含完整上游回應或憑證。
政策 apply 會另行讀回確認；digest 比較與讀回仍不是上游原子的 compare-and-swap，
不能保證排除其它 Headplane／CLI writer 的競爭。

發布以同一映像 digest 的證據為準：原生操作與適用輸入邊界、GUI 實際提交及讀回、
Origin／session／權限拒絕、斷線與故障、負載、持久儲存及升級／回復。
各項需區分正式、隔離、合成故障與未驗證範圍。容量與持續運行時長依實際量測填寫，
不能從 `/healthz` 或 29 個工具映射推導 SLA。

`connection` 的約定欄位如下；seed 全部留空，不填入真實服務識別或憑證：

| 欄位 | 用途 |
| --- | --- |
| `base_url` | Headscale 管理 API 的基底網址；與公開註冊入口分開 |
| `api_key` | Headscale API bearer key |
| `enrollment_url` | 給 Tailscale 用戶端的公開 login-server 網址 |
| `headplane_url` | Headplane 管理介面連結 |
| `paas_base_url` | 選用的 PaaS API 基底網址 |
| `paas_api_token` | 有指定 workspace／服務授權的 PaaS API token |
| `workspace_id` | 選用的 PaaS workspace 綁定 |
| `headscale_service_id` | 選用的 PaaS Headscale 服務綁定 |

子行程連線環境變數使用固定前綴 `HEADSCALE_MCP_`。哪些欄位允許送出 API 請求、哪些僅作介面連結，由 Headscale 自有的設定與 client 驗證器決定；不能把所有 URL 都當成可代理的後端。

### Standalone 首次啟動

啟動前必須提供獨立的 `admin_password`，至少 12 字元；缺少、值為 `admin` 或過短時拒絕啟動。PaaS 由既有敏感設定通道產生管理密碼。Standalone 可在要掛載的目錄內生成隨機密碼，只寫入權限 0600 的檔案，不輸出其值：

```bash
export HEADSCALE_DATA="${XDG_STATE_HOME:-$HOME/.local/state}/headscale-mcp"
install -d -m 700 "$HEADSCALE_DATA"
python3 - <<'PY'
import json, os, secrets
fd = os.open(os.path.join(os.environ['HEADSCALE_DATA'], 'config.json'),
             os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as stream:
    json.dump({'admin_password': secrets.token_urlsafe(32)}, stream)
PY
# 由部署者以安全方式取得初始密碼；目錄與檔案必須可供容器 uid/gid 1000 使用。
docker run --rm -p 127.0.0.1:8080:8080 \
  -e HEADSCALE_MCP_SECURE_COOKIE=false \
  -e HEADSCALE_MCP_ALLOWED_ORIGINS=http://127.0.0.1:8080 \
  -v "$HEADSCALE_DATA:/data" local/headscale:candidate
```

`O_EXCL` 防止覆蓋既有設定。不要把檔案納入 Git、建置 context、日誌或研究報告；如果使用者不是 uid 1000，先由部署者調整這個專用目錄的擁有者。

## 私有後端與 chart

使用既有 `mcp-server` chart `0.1.9`：ClusterIP `8080`、Recreate、`1Gi` PVC。同 workspace 的 Headscale Service 有獨立 `8080` 控制 API port；`3000` 是 entrance，不應自動當成 Headscale API。必須由部署者確認實際 Service、port 與授權範圍。

私有位址例外只透過部署環境變數 `HEADSCALE_MCP_ALLOWED_PRIVATE_CIDRS` 指定，值為逗號分隔 CIDR。以實際目的地的 `/32`（IPv4）或 `/128`（IPv6）為優先；不要預設放行整段 pod、Service、RFC1918 或 CGNAT 網路。用 chart 既有 `extraEnv` 注入，不存入 GUI `connection` 或可編輯的 child env：

```yaml
extraEnv:
  - name: HEADSCALE_MCP_ALLOWED_PRIVATE_CIDRS
    value: "<已核准的後端 IPv4>/32"
```

此環境變數只調整 Headscale adapter 的目的地檢查，不停用共用 SSRF。chart 預設已允許同 namespace 的 pod 通訊，因此同 workspace 不需新增網路規則。跨 namespace/private backend 必須另有精確目的地及 port 的 NetworkPolicy；環境變數不會建立網路通道，也不代表跨 workspace 授權。

## 依賴與本機檢查

root `uv.lock` 是唯一 Python 解析結果；新增本 app 不更新既有套件記錄。目前解析的主要版本為 FastMCP `4.1.3`、MCP `2.3.0`、HTTPX `0.28.1`、httpcore `1.0.9`。SDK 版本不代表符合特定新版 MCP 規格；協定必須另做實際驗收。

在 repo 根目錄執行：

```bash
uvx --from 'uv==0.6.4' uv lock --check --python 3.12
uvx --from 'uv==0.6.4' uv sync --frozen --package woow-headscale-mcp-server --extra dev --python 3.12
uvx --from 'uv==0.6.4' uv run --no-sync --directory apps/headscale pytest -q
```

## 建置與發布

需要 Docker/BuildKit 的 builder；本機沒有 Docker 時，把同一個已整合的 source commit 送到既有 CI runner。Headscale 映像不需要 Node，但 monorepo 共用 CI 仍會測試其他 app 的前端。

```bash
# 從 monorepo 根目錄執行；正式發布來源須為已提交、乾淨的 checkout。
MCP_SHA=$(git rev-parse HEAD)
docker build --build-arg VCS_REF="$MCP_SHA" \
  -f apps/headscale/Dockerfile -t "local/headscale:$MCP_SHA" .
docker run --rm -i --entrypoint python "local/headscale:$MCP_SHA" \
  < packages/mcp-admin-core/scripts/check_certifi.py

# 已由既有 runner secret 機制登入 registry；不要把憑證放進命令、檔案或報告。
MCP_REG=artifactory-jcr-artifactory-nginx.jfrog.svc.cluster.local
MCP_IMAGE="$MCP_REG/woow-paas-docker-local/headscale-mcp-admin"
docker tag "local/headscale:$MCP_SHA" "$MCP_IMAGE:$MCP_SHA"
docker push "$MCP_IMAGE:$MCP_SHA"
docker buildx imagetools inspect "$MCP_IMAGE:$MCP_SHA"
```

Gitea `.gitea/workflows/ci.yml` 在 push/PR 跑測試及映像建置；Headscale 加入自身 paths filter、測試與 build matrix，沿用 certifi 與 `/healthz` smoke。Headscale 的 smoke 先在容器內生成獨立管理密碼，結束時清除該測試容器與匿名 volume。只有 `main` 自動推送 commit tag 與 `latest`。`/healthz` 只證明管理程式可啟動，不代表實際 Headscale 操作、註冊或 MCP 子行程已驗收。

部署拉取路徑為 `jcr-prod.woowtech.io/woow-paas-docker-local/headscale-mcp-admin:<commit>@sha256:<manifest-digest>`。平台 seed 必須使用已驗證的不可變 image pin，並由平台將服務公開 origin 注入 `HEADSCALE_MCP_ALLOWED_ORIGINS`。取得 digest、通過實際後端驗收後，才更新既有服務與平台範本。

發布證據需保留 source commit/tree、Dockerfile 與 `uv.lock` SHA256、CI run/job、builder 平台、基底映像解析 digest、最終 manifest/config digest、OCI revision label，以及實際映像測試結果。此 Dockerfile 沿用 `python:3.12-slim` 與 `uv:0.6.4` 基底 tag；基底 tag 可變，應在發布時記錄實際 digest，不能僅憑 frozen lock 宣稱完整可重現建置。未提交的候選建置也不可宣稱其 revision label 完整代表內容。

叢集變更與 live testing 由 Root 統一執行；本目錄不放 kubeconfig、registry 登入資訊或使用者憑證。
