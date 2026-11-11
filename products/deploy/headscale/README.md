# Headscale MCP for Podman

此產品使用與 PaaS Headscale MCP 相同的專用管理 GUI、29 項 Headscale v0.29.4 原生 API
操作與共用核心。`products/PROVENANCE.md` 記錄固定來源版本及唯一的應用層部署識別差異。
現有 Podman 主機 MCP 與此產品分開安裝，兩者使用不同映像、網路、容器、volume、port
和 Python 環境；Headscale 容器沒有 Podman socket 權限。

## 安裝

需求：Ubuntu 24.04、rootless Podman 4.9 以上、使用者 systemd、Quadlet 和 linger。
由預計持有容器的普通使用者執行：

```bash
products/deploy/headscale/scripts/install.sh --dry-run
products/deploy/headscale/scripts/install.sh
```

初次安裝會在 `~/.config/headscale-mcp-admin/headscale-mcp-admin.env` 建立 0600 的公開
設定檔，在 Podman secrets 中產生獨立的首次管理密碼及 JWT 金鑰，建置固定版本映像，
驗證 Quadlet，安裝並啟動 `headscale-mcp-admin.service`。GUI 預設僅監聽
`127.0.0.1:18081`。首次密碼可在安裝主機查閱：

```bash
podman secret inspect --showsecret --format '{{.SecretData}}' headscale-mcp-admin-password
```

透過 SSH tunnel 或同主機的代理開啟 GUI。登入後到「連線設定」填入 Headscale 管理 API
網址和 API key；Tailscale 客戶端使用的公開註冊網址是另一個欄位。MCP token 會在首次啟動時
產生；在 Tokens 頁可輪替，或由主機管理員輸出目前的連線網址：

```bash
products/deploy/headscale/scripts/show-connector.sh --base https://mcp.example.com
```

該網址含 bearer token，請當成密碼保存。GUI 管理密碼、MCP token 與 Headscale API key
用途不同，不能互換。

## 網路與 Origin

預設本機 HTTP 模式將 `HEADSCALE_MCP_ALLOWED_ORIGINS` 設為
`http://127.0.0.1:18081`，並明確將 Secure cookie 設為 `false`。正式對外請使用可信任的
HTTPS 反向代理，在安裝時指定實際瀏覽器 Origin，並啟用 Secure cookie：

```bash
products/deploy/headscale/scripts/install.sh \
  --origin https://mcp.example.com \
  --set HEADSCALE_MCP_SECURE_COOKIE=true
```

可編輯 env 檔再重跑 install；Origin 只接受完整且精確的 HTTP(S) origin，不接受路徑或
萬用字元。非本機 Origin 必須使用 HTTPS。若 Headscale 管理 API 使用私有 IP，可在
`HEADSCALE_MCP_ALLOWED_PRIVATE_CIDRS` 列出確切目的地，建議 `/32` 或 `/128`，例如
`10.42.3.4/32`。此設定只放寬目的地驗證，不建立網路通道，也不授予 Headscale API 權限。

`--bind` 只接受單一 IPv4，拒絕 `0.0.0.0`。若代理在同主機，保留預設 loopback
即可。對外代理需要覆寫用戶可控制的 forwarded headers；GUI 應額外受存取控制保護。

## 資料與更新

持久資料放在獨立 Podman volume `headscale_mcp_data`，包括 `config.json`、
管理 session 撤銷資料和操作紀錄。首次密碼 secret 只在空 volume 時 seed；重啟或再次
安裝均不覆蓋已儲存的密碼、API key 或 MCP token。更新程式碼時請先提高 `VERSION`
版本，再執行 `upgrade.sh`；它會先備份 volume 並保留舊映像及 Quadlet 設定供回復。
卸載 Quadlet 時保留 volume；要清除資料須明確使用 `--purge`。

使用產品腳本備份、升級、還原與卸載：

```bash
products/deploy/headscale/scripts/backup.sh
products/deploy/headscale/scripts/upgrade.sh
products/deploy/headscale/scripts/restore.sh /path/to/backup --yes
products/deploy/headscale/scripts/uninstall.sh --dry-run
```

備份會暫停服務以保證 SQLite session 資料與設定檔一致；備份檔含管理密碼、API key
與 MCP token，須以私有權限儲存。還原腳本會驗證校驗碼、重建同名 volume，並更新
session epoch，使還原前的 GUI cookie 全部失效。卸載預設保留 volume 與 secrets；
`--purge` 會先做最後一次備份，再清除資料。

## 驗證

```bash
products/deploy/headscale/scripts/smoke.sh
python3 products/scripts/check-headscale-parity.py --source-checkout /path/to/woow-mcp-server
cd products
.venv/bin/pytest -q apps/headscale/tests deploy/headscale/tests
```

CI 另行建置映像，在一次性容器中測試健康檢查、管理登入、MCP 初始化、錯誤 token、
不可信 Origin 及重啟後的資料保留。原有 Podman 主機產品使用 repo 根目錄的另一組測試。
