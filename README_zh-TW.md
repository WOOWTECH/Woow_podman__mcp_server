# Woow Podman MCP Server

[English](README.md) · **繁體中文**

一個透過 libpod REST API 把 **Podman 主機** 以 MCP 工具形式公開的 FastMCP 伺服器，外加一個網頁管理
主控台：負責監督它、限制它能做的事，並把它發布在一個經過驗證的 URL 上，讓 Claude（或任何 MCP
用戶端）可以直接連線。

它以 **一個借用主機 Podman socket 的 rootless Podman 容器** 執行。容器透過綁定掛載的 socket 目錄連到
主機的 daemon 來管理主機上的容器，容器內從不執行自己的 Podman。這一個容器裡有三個元件：

| # | 元件 | 說明 |
|---|------|------|
| 1 | `woow_podman_mcp_server` | MCP 伺服器。以 libpod API 提供 23 個工具，受安全設定檔（profile）限制。只綁定 loopback。 |
| 2 | `podman_mcp_admin` | 管理主控台：React SPA + FastAPI，容器內監聽 `:8080`。以子行程方式監督元件 1。 |
| 3 | `mcp_admin_core` | 與其他 Woow MCP 主控台共用、與產品無關的底層：app factory、JWT 驗證、設定儲存、行程管理、反向代理。 |

連線器（connector）URL 是 `https://<host>/private_<mcp_auth_token>/mcp/`。路徑中的那一段 **就是**
憑證，請見 [驗證](#驗證)。

---

## 安裝（rootless Podman + Quadlet + systemd）

支援的部署方式是一組由使用者 systemd 管理的 [Quadlet](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html)
單元。已在 Ubuntu 24.04、podman 4.9.3（本 repo 接受的最低版本為 4.9）、systemd 255、rootless 並啟用
linger 的環境測試。

```bash
git clone https://github.com/WOOWTECH/Woow_podman__mcp_server.git
cd Woow_podman__mcp_server
scripts/install.sh                 # 或：scripts/install.sh --port 18080
```

`scripts/install.sh` 可重複執行（idempotent），它會：

1. 檢查主機（不是 root、podman >= 4.9、有 Quadlet 產生器、`systemctl --user` 可用），若 linger 與
   `podman.socket` 未啟用則啟用；
2. 第一次執行時，從 [`config/podman-mcp-admin.env.example`](config/podman-mcp-admin.env.example)
   建立 `~/.config/podman-mcp-admin/podman-mcp-admin.env`（權限 0600），並寫入 daemon 的 API 版本。
   `--port N`、`--bind ADDR`、`--set KEY=VALUE` 會修改設定並存回該檔；
3. 若已有一個不受 Quadlet 管理、名為 `podman-mcp-admin` 的容器，或舊的手寫單元仍在啟用，便拒絕繼續
   （見 [遷移既有部署](#遷移既有部署)），因為 Quadlet 用 `podman run --replace` 啟動容器；
4. 用 env 檔渲染 [`quadlet/`](quadlet/) 內的單元（`@@VAR@@` 標記，白名單在 `quadlet/render-vars`），
   並在安裝任何東西之前，先用 podman 4.9.3 的產生器與 `systemd-analyze --user verify` 檢查；
5. 若 `localhost/woow-podman-mcp-admin:<VERSION>` 這個 tag 還不存在，就從 [`Dockerfile`](Dockerfile)
   建置（`--rebuild` 強制重建，`--no-build` 禁止建置）；
6. 若三個 podman secret 不存在就建立（隨機產生，從不印出）：JWT 簽章金鑰、首次開機的管理員密碼、
   首次開機的連線器 token；
7. 只安裝有變更的檔案，只重啟檔案有變更的單元。沒有任何變更時重新執行，不會重啟任何東西；
8. 等待容器變成 healthy，然後執行 [`tests/smoke.sh`](tests/smoke.sh)。

`scripts/install.sh --dry-run` 會渲染並驗證所有內容、回報將會變更什麼，但不做任何變更。

安裝的內容：

| 路徑 | 內容 |
|---|---|
| `~/.config/containers/systemd/podman-mcp-admin.container` | 主控台；預設發布在 `127.0.0.1:8080` |
| `~/.config/containers/systemd/podman-mcp.volume` | volume `podman_mcp_data`（`/data/config.json`） |
| `~/.config/containers/systemd/podman-mcp.network` | network `podman-mcp` |
| `~/.config/podman-mcp-admin/podman-mcp-admin.env` | 每台主機的設定（0600） |
| podman secret `podman-mcp-admin-{jwt-secret,password,mcp-token}` | `JWT_SECRET`、`ADMIN_PASSWORD`、`MCP_AUTH_TOKEN` |

### 首次登入

```bash
podman secret inspect --showsecret --format '{{.SecretData}}' podman-mcp-admin-password
```

這是主控台首次開機時寫入的密碼。透過你的 tunnel 開啟主控台（或 `ssh -L 8080:127.0.0.1:8080 <host>`
後開 <http://localhost:8080>），登入後用下列指令印出連線器 URL：

```bash
scripts/show-connector.sh --base https://podman-mcp.example.com
```

它從 `/data/config.json` 讀取目前生效的 token，所以在 Tokens 頁輪替 token 之後仍然正確。

### 設定

編輯 `~/.config/podman-mcp-admin/podman-mcp-admin.env` 後重新執行 `scripts/install.sh`。這些值會在
安裝時渲染進單元檔，所以值有變更就重啟容器，沒變更就不重啟。

| 鍵 | 預設 | 說明 |
|----|------|------|
| `MCP_ADMIN_BIND` | `127.0.0.1` | 發布位址。只有 tunnel 在另一台機器上時才改成區網 IP。拒絕 `0.0.0.0`。 |
| `MCP_ADMIN_PORT` | `8080` | 主機埠號。 |
| `PODMAN_MCP_PROFILE` | `safe` | `readonly`（13 個工具）/ `safe`（18）/ `full`（23） |
| `PODMAN_MCP_NAME_ALLOW` | *（空）* | 工具可以操作的容器/pod 名稱正規表示式（不可含空白或引號）。 |
| `PODMAN_API_VERSION` | daemon 的版本 | 由首次安裝寫入。比 daemon 新的版本會讓每個呼叫都回 404。 |
| `PODMAN_MCP_MAX_CHARS` | `20000` | 每次呼叫的回應上限；工具會以列為單位截斷，並說明丟掉幾列。 |
| `JWT_EXPIRY_HOURS` | `12` | 主控台登入工作階段的有效時間。 |

**首次開機之後，以 `/data/config.json` 為準。** 主控台在空的 volume 上會用 `PODMAN_*`、
`ADMIN_PASSWORD`、`MCP_AUTH_TOKEN` 自動產生 `config.json`（見 `podman_mcp_admin/bootstrap.py`），
之後該檔中的值會蓋過容器環境變數。因此若 `config.json` 已經存了 `PODMAN_MCP_PROFILE` 或
`PODMAN_API_VERSION`，之後在 env 檔改這些值不會影響該部署（首次開機時為空的鍵不會被寫入，例如沒設定
的 `PODMAN_MCP_NAME_ALLOW`，它仍然跟著 env 檔）。已存下的值請在設定頁修改，或編輯 volume 中的
`config.json` 後重啟單元。

### 本機開發（不使用容器）

```bash
pip install -e ".[dev]"
python3 scripts/seed.py --config /tmp/pm/config.json \
    --podman-uri unix:///run/user/$(id -u)/podman/podman.sock
MCP_ADMIN_CONFIG=/tmp/pm/config.json JWT_SECRET=dev \
    uvicorn podman_mcp_admin.main:app --port 8080
```

或只跑 MCP 伺服器（stdio），完全不用主控台：

```bash
PODMAN_MCP_PROFILE=readonly python3 -m woow_podman_mcp_server.server
```

---

## 驗證

1. **網路。** 主控台只發布在 `127.0.0.1`，只有同一台主機上的行程能連到：例如使用 host 網路的
   cloudflared，或 Nginx Proxy Manager。只有 tunnel 在另一台機器上時才放寬 `MCP_ADMIN_BIND`，而且只
   放寬到一個區網 IP。
2. **管理主控台。** 一組管理員密碼，存在 `/data/config.json`（權限 0600）。登入工作階段是以
   `JWT_SECRET` 簽章的 HS256 JWT，放在 `HttpOnly`、`SameSite=Strict` 的 cookie 中；請求以 HTTPS 進來時
   （`X-Forwarded-Proto: https`，可用 `ADMIN_COOKIE_SECURE` 覆寫）加上 `Secure`。有效期為
   `JWT_EXPIRY_HOURS`；登出或變更密碼會撤銷所有已發出的 token。300 秒內失敗 5 次，該用戶端會被鎖
   30 秒，之後加倍，最長 900 秒。用戶端以 `X-Forwarded-For` 的 *第一個* 位址識別，除非代理伺服器覆寫
   它，否則這個值由用戶端自己決定，所以此節流只有在同主機的 tunnel 後面才有意義。
3. **MCP 連線器。** URL `https://<host>/private_<token>/mcp/` 是 **bearer 憑證**。以常數時間比對，不符
   時回 403。在 Tokens 頁輪替它會重啟子行程。沒有 OAuth：`/.well-known/*` 與 `/register` 故意回 JSON
   404。這個 URL 會出現在 tunnel 的存取紀錄與 claude.ai 的連線器設定中，請把它當成密碼。
4. **持有 token 的人能做什麼。** 能做這個使用者的 podman socket 能做的一切，再由
   `PODMAN_MCP_PROFILE` 與 `PODMAN_MCP_NAME_ALLOW` 縮小範圍。預設的 `safe` 包含 `exec`，可以讀取任何
   容器的檔案與環境變數。即使是 `readonly` 也包含 container inspect，而它 **會傳回每個容器的
   `Config.Env`，包括以 `type=env` 傳入的 podman secret**（podman 4.9.3）。因此連線器 token 同時也是
   該使用者所有堆疊 env secret 的讀取憑證，包括本服務自己的 `JWT_SECRET`。只做監看的代理程式請用
   `readonly` 加名稱白名單。
5. **建議的對外方式。** 在主控台的主機名稱上設定 Cloudflare Access 政策，涵蓋 **除了** `/private_*`
   以外的所有路徑：主控台因此需要 SSO 加密碼，而連線器路徑仍可讓 claude.ai 連入（它無法完成 Access
   登入）。若不想用 Access 的路徑規則，可以為連線器另用一個主機名稱。

這些 secret 以 `type=env` 的 podman secret 進入容器，所以不會出現在單元檔、`systemctl --user cat`、
容器的建立指令或 journal 中。但它們會出現在執行中容器的 `podman inspect` 裡（見第 4 點）。要改成以
檔案掛載，需要先在 `mcp_admin_core` 加上 `*_FILE` 支援。

---

## 放在 Cloudflare tunnel 後面

若 cloudflared 跑在 **同一台主機**（例如使用 host 網路），把 ingress 指向 `http://localhost:8080`
（或你設定的 `MCP_ADMIN_PORT`）。這是預設情境，不需要其他設定。

若 cloudflared 跑在 **別的地方**，例如另一台機器上的叢集內 cloudflared pod，`localhost` 是那個 pod 自己
的 loopback，會回 502。請發布在 Podman 主機的區網位址，並把 ingress 指過去：

```bash
scripts/install.sh --bind 192.168.1.20        # 本機的區網 IP；拒絕 0.0.0.0
```

```yaml
# cloudflared config.yaml 的 ingress 項目
- hostname: podman-mcp.example.io
  service: http://192.168.1.20:8080
```

請先接受兩個後果：主控台與連線器路徑會被 **該區網上的任何東西** 連到，只靠一組管理員密碼保護；
而且主機的區網 IP 必須固定（靜態租約），否則 tunnel 會無聲地回 502。若這樣暴露面太大，改在 Podman
主機上跑第二個 cloudflared 並指向 localhost。

---

## 升級

```bash
git pull
scripts/upgrade.sh
```

`upgrade.sh` 會先為已安裝的單元做快照、用 `scripts/backup.sh` 匯出 `podman_mcp_data`，再執行
`scripts/install.sh`（建置新的 `VERSION` tag 並重啟有變更的部分）與 `tests/smoke.sh`。任何一步失敗，
就放回先前的單元、以先前的映像 tag 重啟（install 從不刪除映像 tag），並以 1 結束。版本號同時存在於
[`VERSION`](VERSION)、單元的 `Image=` tag 與 `pyproject.toml`；三者不一致時 CI 會失敗。

## 備份與還原

```bash
scripts/backup.sh                       # -> ~/backups/podman-mcp-admin/<時間戳>/
scripts/backup.sh --include-secrets     # 另外把三個 podman secret 存到 secrets.env（0600）
scripts/restore.sh ~/backups/podman-mcp-admin/<時間戳>     # 會先詢問；--yes 略過
```

備份是 `podman_mcp_data` 的 `podman volume export`，其中的 `config.json` 含管理員密碼與目前的連線器
token，請妥善保管備份。`restore.sh` 會停止單元、用匯出檔取代 volume、啟動並執行 smoke 測試。

## 解除安裝

```bash
scripts/uninstall.sh                    # 停止並移除單元；保留 volume、secret、映像與 env 檔
scripts/uninstall.sh --purge            # 另外刪除 volume（先做最後一次備份）、network 與 secret
scripts/uninstall.sh --purge-images     # 另外移除 localhost/woow-podman-mcp-admin:* 映像
```

`--purge` 是這些腳本刪除資料的唯一方式；它會要求輸入應用名稱確認（`--yes` 可略過）。一般解除安裝後
重新執行 `install.sh` 會沿用同一個 volume，所以連線器 URL 與管理員密碼不變。`podman.socket` 永遠不會被
動到，因為其他堆疊也在用。env 檔會留在 `~/.config/podman-mcp-admin/`，請自行刪除。

## 遷移既有部署

適用於以舊 README 方式執行容器的主機（`podman run … podman-mcp-admin`、`podman generate systemd`
產生的單元，或手寫的 `podman-podman-mcp-admin.service`）：

1. 檢查 tunnel：主控台主機名稱的 ingress 必須指向本機的 `localhost:<port>` 或 `127.0.0.1:<port>`。
   若指向主機的區網 IP，請規劃使用 `--bind <區網 IP>`。
2. 備份：`podman volume export podman_mcp_data -o ~/podman_mcp_data-pre-quadlet.tar`（0600），並保留
   一份舊的單元檔。
3. 停止並停用舊單元；若它的名稱是 `podman-mcp-admin.service`（會遮蔽 Quadlet 單元），把它移開：
   `systemctl --user disable --now podman-podman-mcp-admin.service`。
4. 保留舊容器以便回復，停止後改名：
   `podman stop podman-mcp-admin && podman rename podman-mcp-admin podman-mcp-admin-legacy-$(date +%Y%m%d)`。
5. 執行 `scripts/install.sh`。它會沿用 `podman_mcp_data`，所以連線器 token 與管理員密碼不變；只有主控台
   登入（新的 `JWT_SECRET`）需要重新登入。
6. `config.json` 保留首次開機時的 API 版本。若 `tests/smoke.sh` 通過但工具呼叫回 404，請在設定頁把連線
   的 API 版本改成 daemon 的版本（`podman version --format '{{.Server.APIVersion}}'`）。
7. 輪替管理員密碼（它曾存在舊容器的環境變數中，也可能在 shell 歷史紀錄裡），並考慮輪替連線器 token。

回復：`scripts/uninstall.sh`，把舊容器改回原名，重新啟用舊單元。

## Docker 與 compose

本 repo 已不再包含 Docker Compose。最後一個含 `docker-compose.yml` 的 commit 標記為
[`compose-final`](https://github.com/WOOWTECH/Woow_podman__mcp_server/tree/compose-final)，不再維護。
Kubernetes 請用 `Woow_k3s_mcp_server`。映像在本機建置（`Pull=never`）；發布到 GHCR 是之後的工作。

---

## 安全設定檔

工具在 **註冊** 時就被限制，而不是在列出時。不在目前設定檔內的工具在協定上根本不存在：無法以名稱
呼叫、快取了舊 `tools/list` 的用戶端也碰不到，schema 裡也看不到。只過濾 *清單* 的限制，會被任何已經
知道工具名稱的用戶端繞過。

| 設定檔 | 工具數 | 包含 |
|--------|-------|------|
| `readonly` | 13 | `ps`、`images`、`logs`、`inspect`、`stats`、`top`、`events`、`system_df`、`info`、`pods`、`networks`、`volumes`、`healthcheck` |
| `safe` *（預設）* | 18 | + `start`、`stop`、`restart`、`exec`、`image_pull` |
| `full` | 23 | + `container_remove`、`image_remove`、`volume_remove`、`network_remove`、`system_prune` |

**socket 一旦掛載，這就是唯一有意義的邊界：** 能碰到 socket 的東西就擁有該 uid 完整的 Podman，設定檔
是縮小範圍的唯一手段。

---

## 安全性

**Podman socket 就是全部的邊界。** libpod 沒有 API 金鑰：能碰到 socket 的東西就能建立特權容器並綁定
掛載主機根目錄，等同該 uid 的 root。Quadlet 單元掛載的是 *rootless* socket 目錄（`%t/podman`），並以容器
root 執行，也就是 rootless user namespace 中的主機使用者。它同時丟棄所有 capability、設定
`no-new-privileges` 並使用唯讀根檔案系統（`/data` 是唯一可寫的 volume）。

**沒有 OAuth。** 伺服器對所有 `/.well-known/*` 探測與 `/register` 都回 JSON `404`。SPA 的萬用路由以前會
以 `200 text/html` 回應這些探測，用戶端會解讀成「我有授權伺服器」，接著嘗試 Dynamic Client
Registration，拿到 HTML 後失敗，出現 *"Couldn't register with … 's sign-in service"* 的重導迴圈。乾脆的
404 讓探索快速失敗，用戶端便退回匿名存取，直接送出 `initialize`。

**以 `tcp://` 連遠端主機完全沒有驗證。** 用戶端支援 `PODMAN_URI=tcp://host:2376`（可選用
`PODMAN_TLS_*` 的 mTLS），但 `podman system service` 本身沒有 TLS 也沒有驗證。只在受信任、隔離的網段內
使用 `tcp://`，並自行在前面終結 mTLS。需要經過驗證的遠端傳輸時，建議用 SSH tunnel 連到 socket。

### 實務筆記

* **`podman stats` 遇到不存在的名稱。** libpod 回 `HTTP 200` 與 `{"Error": {}, "Stats": null}`，而
  `{}` 在 Python 中為假，所以直覺的 `if payload.get("Error")` 永遠不會觸發，工具會無聲地什麼也不回。
  只要 **任何一個** 名稱不存在，它就完全不回 stats，所以錯誤訊息會列出整批名稱。
* **`podman top` 使用一般 `ps` 旗標。** 對 `aux` 這類旗標式參數，libpod 回傳的欄位比標題 *少*，無法製表。
  工具偵測到不一致時會印出原始輸出，並提示改用描述子格式（`ps_args="-eo pid,user,comm"`）。
* **串流框架。** libpod *一律* 使用 8 位元組多工，即使有 TTY 也一樣；只有 Docker 相容的 `/v1.x` 端點是
  原始串流。`tty` 旗標會明確往下傳，而不是從內容猜測，否則剛好以 `\x01\x00\x00\x00` 開頭的輸出會被吃掉。

---

## 測試

```bash
pytest               # 33 個測試，不需網路，不需 Podman
tests/dryrun.sh      # 渲染單元並用 podman 4.9.3 產生器 + systemd-analyze 檢查
tests/smoke.sh       # 在已安裝的主機上：健康狀態、只聽 loopback、登入、MCP initialize、錯誤 token 回 403
```

CI：[`quadlet-ci.yml`](.github/workflows/quadlet-ci.yml)（vendored lib 校驗和、dry-run、shellcheck）與
[`tests.yml`](.github/workflows/tests.yml)（pytest、憑證掃描、映像建置）。

## 目錄結構

```
Dockerfile                  兩階段建置（node 22.23.2 建 SPA、python 3.12.14 執行），版本固定
VERSION                     映像 tag；必須與 quadlet/podman-mcp-admin.container 及 pyproject.toml 一致
quadlet/                    podman-mcp-admin.container、podman-mcp.volume、podman-mcp.network、render-vars
config/                     podman-mcp-admin.env.example
scripts/                    install、upgrade、uninstall、backup、restore、show-connector；lib/quadlet-lib.sh（vendored）
tests/                      pytest 測試、dryrun.sh（+ dryrun.local.sh、fixtures/）、smoke.sh
verification/               針對執行中部署做手動檢查的容器內用戶端
```

---

## 路線圖

第 1 階段（**本版本**）的目標是「主控台能啟動、連線器能用」。MCP 伺服器是一個自給自足的
`server.py`；主控台負責監督、代理、串流紀錄與輪替 token。

| 階段 | 範圍 |
|------|------|
| 1 ✅ | 主控台啟動、自動產生設定、驗證、行程監督、加密代理、23 個工具中 18 個可用 |
| 2 | 設定檔資料模型：`registry.py`、`gating.py`、拆分 `tools/`、GUI 設定檔選擇與逐一工具開關 |
| 3 | 連線與健康：真正的 Podman 探測、依失敗類型給出不同錯誤的 Test Connection、完整儀表板 |
| 4 | Podman 操作頁面（容器、映像、volume、network、pod） |

在第 2/3 階段完成前，**Connection** 與 **Tools** 頁面會從 API fallback 拿到 JSON `404` 而顯示空白。
這是刻意的，比假裝能用的空殼更容易除錯。

---

## 授權

MIT，見 [LICENSE](LICENSE)。
