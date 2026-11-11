# Vendored：nextcloud_mcp_server

`apps/nextcloud/nextcloud_mcp_server/` 是 WOOWTECH 自有、MIT 授權的 Nextcloud MCP server，原封 vendor 進本 app，
由 `nextcloud-mcp-server` launcher（`nextcloud_mcp_admin/launcher.py`）當 MCP 子行程啟動。

## 來源

- 來源 repo：<https://github.com/WOOWTECH/Woow_nextcloud_mcp_server>
- commit：`7af7dbaa441e31f2b0afbacd752bee40d7921319`（來源 PR #5 的 merge commit）
- 版本：`0.1.6`（`nextcloud_mcp_server.__version__`）
- vendor 日期：2026-11-07（2026-11-01 取自 `28767c4`；這次只有 `paths.py` 隨來源 PR #5 變動，其餘檔案與 `28767c4` 逐位元相同）
- 取用範圍：`src/nextcloud_mcp_server/`（整個套件）＋`LICENSE`（放在 `apps/nextcloud/LICENSE`）
- 授權：MIT，Copyright (c) 2026 WOOWTECH。來源 repo 的 `PROVENANCE.md` 記錄它是依 `SPEC.md` 以 clean-room 方式撰寫，
  未參考任何 AGPL 的 Nextcloud MCP 實作；`THIRD_PARTY_NOTICES.md` 列出執行期依賴與授權（皆為寬鬆授權）。

## 規則

- **vendored 檔案與來源 byte-identical，不在本 repo 直接修改。** 部署期約束（只綁 127.0.0.1、固定 Streamable HTTP、
  依 Tools 頁政策產生 `NEXTCLOUD_MCP_READONLY`／`ALLOW_DELETE`／`DISABLED_TOOLS`、關 FastMCP banner／版本檢查、
  壓低 httpx log）全部集中在 `nextcloud_mcp_admin/launcher.py`。需要改行為就先改來源 repo，再重新 vendor。
- **clean-room**：本 app（含 admin 殼與測試）不得開啟、複製或改寫 舊版 `apps/nextcloud-oauth-gateway/context-agent-patch/**`（已移出 repo，仍在 git 歷史）、
  `nextcloud/context_agent`、`cbcoutinho/nextcloud-mcp-server` 等 AGPL 程式碼。
- 依賴版本由 root `uv.lock` 釘死（見下節），不使用來源 repo 的 `uv.lock`。

## 與來源的本地差異

目前有一處和上面記的 commit 不同；**整包同步（`sync_vendor.sh`）會把它蓋回去**，同步後要重新套上。

- `nextcloud_mcp_server/client.py`：`code_mode` 的預設改成「`auto` 也開」（負責人 2026-11-02 決定，commit `055291d`）；其餘與來源相同。

`nextcloud_mcp_server/paths.py` 的 etag 修正（`normalize_etag` 去掉 Apache 壓縮回應加在 etag 後面的 `-gzip`／`-br`／`-deflate`／`-zstd`；
沒有它，1 KB 以上的文字檔讀出來的 etag 拿去更新或刪除一律 `ETAG_MISMATCH`）是先改來源 repo（PR #5，2026-11-07 合併），再在這裡取用；
`paths.py` 與來源 `7af7dba` 逐位元相同，所以它不是本地差異。

## 依賴差異

來源 `pyproject.toml` 以 `==` 釘版；monorepo 是單一 uv workspace，同一套件只能有一個版本，因此
`apps/nextcloud/pyproject.toml` 改用相容下限，實際版本以 root `uv.lock` 為準：

| 套件 | 來源 repo 釘版 | 本 monorepo `uv.lock` | 說明 |
|---|---|---|---|
| fastmcp | 3.4.5 | 4.1.3 | 2026-10-24 起 monorepo 改用 MCP SDK 2.x（#78）；fastmcp 4 零修改可用（`tests/test_mcp_surface.py`、`test_child_process.py` 通過） |
| mcp | 1.28.1 | 2.3.0 | 2026-10-24 起 monorepo 用 MCP SDK 2.x（#78，見 docs/security/dependency-pins.md）；vendored 程式碼未改 |
| httpx | 0.28.1 | 0.28.1 | 相同 |
| pydantic-settings | 2.15.0 | 2.16.1 | 只用 `BaseSettings`／`SettingsConfigDict`（2.14 即具備） |
| typing-extensions | 4.16.0 | 4.16.0 | 相同（只用 `TypedDict`） |
| defusedxml | 0.7.1 | 0.7.1 | 本次新增 |
| tzdata | 2026.5 | 2026.5 | 原本只在 Windows 安裝；改為無條件依賴（slim 映像沒有系統時區資料庫） |

`tests/test_mcp_surface.py` 以 in-process MCP client 對 mock Nextcloud 驗證 9 支工具在上述版本下的實際行為；
另於 vendor 時以 monorepo venv 跑過來源 repo 的完整單元測試（見 commit 訊息）。fastmcp 4 下來源的
`tests/test_mcp.py::test_server_info_and_instructions` 會失敗（0.1.5＋159410a 與 0.1.6 都一樣），原因在測試端、
與 vendored 程式碼無關：fastmcp 4 的 in-memory `Client` 預設以 `server/discover`（2026-07-28 世代）協商，
`initialize_result` 是 `None`；改讀 `client.server_info`／`client.instructions`，或用 `mode="legacy"` 走
`initialize`，名稱、版本與 instructions 都與來源預期相同。

## 更新方式

```bash
apps/nextcloud/scripts/sync_vendor.sh /path/to/Woow_nextcloud_mcp_server
```

腳本以來源 repo 的 **已 commit HEAD**（`git archive`）取代 `nextcloud_mcp_server/` 與 `LICENSE`，並更新本檔的 commit／版本／日期。
之後手動比對來源 `pyproject.toml` 的依賴、必要時以 `uvx --from 'uv==0.6.4' uv lock` 重新 lock，再跑
`uv run --directory apps/nextcloud pytest -q`。
