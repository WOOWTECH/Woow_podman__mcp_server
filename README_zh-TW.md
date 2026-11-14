# Woow Podman MCP Server

[English](README.md) · **繁體中文**

此 repo 在 [`products/`](products/README.md) 提供 9 個產品：Headscale、EMQX、Hermes、
n8n、Odoo、LiteLLM、Nextcloud、OpenDesign 和 K8s。它們共用 PaaS 的應用、核心和前端
原始碼。每個產品使用獨立的 rootless Podman Quadlet、port、volume 與憑證，且不掛載
Podman socket。

```bash
products/deploy/manage.sh list
products/deploy/manage.sh emqx install --dry-run \
  --release-manifest /path/to/reviewed-release.json \
  --source-checkout /path/to/merged-canonical-checkout
products/deploy/manage.sh emqx install \
  --release-manifest /path/to/reviewed-release.json \
  --source-checkout /path/to/merged-canonical-checkout
products/deploy/manage.sh emqx upgrade \
  --release-manifest /path/to/reviewed-release.json \
  --source-checkout /path/to/merged-canonical-checkout
```

這 9 個產品的安裝與升級需要以下其中一種：

- 獨立審查過的簽章發行清單、乾淨的來源 checkout，以及已審查映像的精確 ID；
- 擁有者核准不簽章發布時，改用 `--skip-release-signature`，映像須由
  `products/deploy/build-release-images.sh` 從已合併的 `main` 建置。

完整步驟見[發行證明與信任限制](docs/release-provenance-guard.md)。上方路徑是佔位範例。
產品安裝程式會拒絕 `--rebuild`，只接受事先建置及審查的映像。

Headscale 的專用安裝方式見[產品說明](products/deploy/headscale/README.md)，包括 GUI
預設網址 `http://127.0.0.1:18081/`、首次登入、Origin、Headscale API 連線和備份。

## 已退役：Podman 主機 MCP

在 2026-11-14 以前，repo 根目錄還有一套 Podman 主機 MCP，由 MCP 伺服器
`woow_podman_mcp_server` 和管理主控台 `podman_mcp_admin` 組成，透過 libpod API 公開
這台主機的 Podman。擁有者已將它退役，改由 Pi Web 處理這類工作。它最後的原始碼保存在
git tag `archive/podman-host-mcp-20261114`。

`scripts/lib/quadlet-lib.sh` 仍留在根目錄，因為產品的安裝腳本要用它。它是從
Woow_quadlet_migration_plan 同步過來的，由 `.github/workflows/tests.yml` 檢查。

## 目錄

```
products/                   9 個產品：vendored PaaS 原始碼、部署腳本、Quadlet
docs/                       計畫文件與發行證明說明
scripts/lib/                vendored quadlet-lib（products/deploy 會載入）
tests/                      quadlet-lib 檢查（lib-version.sh、lock-release.sh）
.github/workflows/          產品 CI、Headscale CI、quadlet-lib 與憑證檢查
```
