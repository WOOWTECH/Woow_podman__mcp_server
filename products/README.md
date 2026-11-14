# Woow MCP 產品工作區（Podman / Quadlet）

這個工作區提供與 PaaS 同一份 `mcp-admin-core`、產品 API adapter、MCP 工具政策與管理前端。各產品使用獨立的 rootless Podman 容器、網路、資料 volume、金鑰與 Quadlet；共用管理入口是 [`deploy/manage.sh`](deploy/manage.sh)。原本在 repo 根目錄的 Podman 主機 MCP 已於 2026-11-14 退役。此版同步 PaaS 的多設定檔連線：管理 GUI 可為不同後端建立獨立的連線、權杖與工具授權，切換時不共用設定檔狀態。

| 產品 | 預設本機 GUI port | 來源與發布狀態 |
| --- | ---: | --- |
| Headscale | 18081 | PaaS；獨立安裝腳本與專用 GUI |
| EMQX | 18501 | PaaS、HA 已發布 |
| Hermes | 18502 | PaaS、HA 已發布 |
| n8n | 18503 | PaaS、HA 已發布 |
| Odoo | 18504 | PaaS、HA 已發布 |
| LiteLLM | 18505 | PaaS、HA 已發布 |
| Nextcloud | 18506 | PaaS、HA 已發布 |
| OpenDesign | 18507 | PaaS；HA 程式碼尚未啟用發布 |
| K8s | 18508 | PaaS；HA 尚無產品 |

`odoo-manage` 已封存，不安裝其服務；保留它的 frontend 目錄只是為了符合 PaaS npm workspace lock。
`vk`（Vibe Kanban）自 2026-11-14 起不再部署：`apps/vk` 仍和其他 app 一樣收編（PaaS 的 uv 與 npm workspace lock 包含它，共用核心測試也逐一檢查每個 app），但不建置也不安裝映像。

## 安裝與連線

主機需求：Podman 4.9 以上、使用者 systemd、Quadlet、linger、Python 3.12、已審查映像與[發行證明](../docs/release-provenance-guard.md)。請以持有容器的普通使用者執行：

```bash
products/deploy/manage.sh list
products/deploy/manage.sh emqx install --dry-run \
  --release-manifest /path/to/reviewed-release.json \
  --source-checkout /path/to/merged-canonical-checkout
products/deploy/manage.sh emqx install \
  --release-manifest /path/to/reviewed-release.json \
  --source-checkout /path/to/merged-canonical-checkout
products/deploy/manage.sh emqx smoke
```

2026-11-14 起擁有者核准不簽章的發布：先 `git fetch origin` 並切到已合併的 `origin/main`，以 `products/deploy/build-release-images.sh [產品...]` 建映像，再用 `--skip-release-signature` 取代 `--release-manifest` 與 `--source-checkout`，例如 `products/deploy/manage.sh emqx upgrade --skip-release-signature`。這樣會略過簽章，但仍要求乾淨的 checkout、HEAD 已在 `origin/main`、映像標籤對得上這個 commit 與 PaaS 版本，並把映像 ID 固定寫進 Quadlet。少了哪些保護，見[發行證明](../docs/release-provenance-guard.md)。

首次安裝產生獨立的 Podman secrets，從 `ADMIN_PASSWORD` 只在空 volume 寫入管理密碼；應用首次啟動另外產生 MCP token。可在本機查看初次管理密碼：

```bash
podman secret inspect --showsecret --format '{{.SecretData}}' emqx-mcp-admin-password
```

管理 GUI 預設只發佈在 `127.0.0.1`，可用 SSH tunnel 或同機 HTTPS 反向代理開啟。登入後在 Connection 頁設定該產品的後端 API 憑證，於 Tokens 頁取得 `https://<host>/private_<token>/mcp`。這段路徑包含連線憑證。各應用的連線欄位與工具操作詳見 `apps/<產品>/README.md`。

若後端也是同一個 rootless Podman 使用者的容器，可以讓 MCP 同時加入它的現有網路：

```bash
products/deploy/manage.sh hermes install --backend-network hermes --ssrf-mode permissive \
  --release-manifest /path/to/reviewed-release.json --source-checkout /path/to/merged-canonical-checkout
products/deploy/manage.sh odoo install --backend-network odoo18-network \
  --release-manifest /path/to/reviewed-release.json --source-checkout /path/to/merged-canonical-checkout
```

`--backend-network` 僅接受已存在的 Podman network 名稱。連線頁可填該網路內的容器 DNS 名稱與內部 port；各產品仍保有自己的獨立網路。Hermes、n8n、EMQX 等對私網 URL 有 SSRF 檢查；本機私網服務需由部署者明確指定 `--ssrf-mode permissive`，這會放行私有位址但仍封鎖雲端 metadata 位址。預設維持 `strict`。需要對外提供 GUI 時可用 `--bind <單一 LAN IPv4> --port <port>`，不接受 `0.0.0.0`。

每個產品容器都有記憶體上限（`--memory`，預設 `1g`，至少 `512m`，寫在 env 檔的 `MCP_MEMORY_LIMIT`）。共用核心依這個上限計算還能新增幾個帳號（設定檔）：放不下另一個子行程時，新增帳號會回 409，並說明需要的記憶體。請在建立額外帳號之前就設好上限；之後調低，排在後面的帳號可能因容量不足而無法啟動。`--origins`（`MCP_ADMIN_ALLOWED_ORIGINS`，逗號分隔的精確 http(s) 來源）與 `--origin-mode report-only|enforce`（`MCP_ADMIN_ORIGIN_MODE`，預設 report-only）控制 MCP 請求的 Origin 檢查；`--gateway-connections`、`--gateway-streams`、`--gateway-timeout` 調整每個帳號的閘道連線名額。安裝與升級時會先驗證這些值，不合法就不寫入，避免核心啟動失敗。

Headscale 沿用其專用安裝、Origin 與 cookie 檢查。若本機 18081 已被其他服務使用，指定另一個 port 與相同 Origin：

```bash
products/deploy/manage.sh headscale install --port 18500 --origin http://127.0.0.1:18500 \
  --release-manifest /path/to/reviewed-release.json --source-checkout /path/to/merged-canonical-checkout
```

K8s 需要在 GUI 提供可用的 kubeconfig 或掛載叢集內 ServiceAccount，空白初次安裝只驗證管理頁。其他產品未設定後端時，安裝 smoke 會區分 MCP 協定已啟動、工具可觀測及後端尚未設定的狀態。

## 維護

```bash
products/deploy/manage.sh emqx status
products/deploy/manage.sh emqx backup
products/deploy/manage.sh emqx restore /path/to/backup --yes
products/deploy/manage.sh emqx upgrade \
  --release-manifest /path/to/reviewed-release.json \
  --source-checkout /path/to/merged-canonical-checkout
products/deploy/manage.sh emqx uninstall --dry-run
```

備份先停止服務再匯出 volume 與 SHA256，並保存 0600 的 env 檔。還原會檢查 SHA256，先在暫存 volume 由已安裝的映像以共用核心的 `restore_guard` 產生「全部撤銷」的副本（這一步失敗時服務與資料都不動；k8s 的 kubeconfig 與 TOML 派生檔不還原，啟動時依設定重新產生），成功後才停止服務、換上這份副本：帳號（設定檔）、連線與工具開關都會回來，但每個帳號的 MCP token 一律撤銷、舊的操作紀錄過期、管理 session 失效，管理密碼改為 podman secret `<產品>-mcp-admin-password` 的值。還原後請登入 GUI，逐一確認各帳號的後端連線，再到 Tokens 頁為需要的帳號發新 token，並更新 MCP 用戶端的網址。升級失敗時的自動回復是用同一次升級剛做的備份，不撤銷 token。`upgrade` 先留存 Quadlet 設定與資料備份，安裝失敗時回復。一般 `uninstall` 保留資料及 secrets；明確指定 `--purge --yes` 才會先備份再刪除。備份含服務憑證，請使用私有儲存空間。

## 來源與驗證

PaaS 來源固定在 [`deploy/catalog.json`](deploy/catalog.json) 的 commit。9 個新增產品的功能程式與管理前端直接取自該版本；差異限於部署識別、容器啟動、Podman 4.9 的 K8s 下載後同 SHA256 驗證，以及部署相關測試。詳細差異見 [`PROVENANCE.md`](PROVENANCE.md)。

```bash
python3 products/scripts/check-all-parity.py --source-checkout /path/to/woow-mcp-server
products/deploy/shared/tests/quadlet-dryrun.sh
cd products
uv sync --all-packages --all-extras --frozen
.venv/bin/pytest -q deploy/shared/tests
```

GitHub Actions 的 `all-mcp-products` 工作流程會執行共用核心與應用測試、前端測試，以及 9 個產品的獨立映像建置與一次性容器 smoke。Headscale 保留既有獨立 CI。
