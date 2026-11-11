# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased] - 2026-11-02

### Changed（與 HA add-on 的 Hermes 錯誤文字對齊）
- 所有 MCP 可見的 Hermes 錯誤統一為 `CODE: text`。沿用 HA 既有代碼（`BACKEND_HTTP_ERROR status=N`、
  `BACKEND_AUTH_REJECTED`、`BACKEND_REDIRECT_REJECTED`、`BACKEND_UNAVAILABLE`、`BACKEND_TIMEOUT`、
  `BACKEND_UNEXPECTED_RESPONSE`），新增 `INVALID_INPUT`、`NOT_FOUND`、`CONFIRMATION_REQUIRED`、
  `CONFIG_NOT_APPLIED`、`CHAT_TURN_FAILED`；`SESSION_DELETE_OUTCOME_UNKNOWN`、`CRON_CREATE_OUTCOME_UNKNOWN` 不變。
- Gateway 失敗（`hermes_chat`、`hermes_inspect target=capabilities`）改用與 Dashboard 相同的固定安全文字，
  不再轉述後端網址或上游本文。
- `hermes_mcp add` 的 loopback 網址（localhost、127.0.0.0/8、::1）改為單一說法
  `INVALID_INPUT: loopback MCP servers (localhost, 127.0.0.0/8, ::1) cannot be added`（其他私網仍為 SSRF 說法）。

## [Unreleased] - 2026-10-18

### Security（商用驗收 AI-HMCP-001-1／-001-3／-002-1／-002-2）
- 拼進 dashboard 網址的 id／name（session_id、job_id、webhook／toolset／MCP server 名稱）改用白名單＋`quote(safe="")`，
  拒絕 `..` 與 `/`；`hermes_session get session_id="../config/raw"` 不再讀到 raw config（MCP 工具與後台直通都是）。
- `hermes_config set` 的 deny-list 改為正規化後比對根鍵、子鍵與祖先；新增 model、mcp_servers、providers、
  custom_providers、security；任何一段是憑證鍵或以 `_` 開頭也拒絕。
- 後台 gateway_url／dashboard_url 的儲存與「測試連線」套用 SSRF 檢查（`WEBHOOK_SECURITY_MODE`，預設 strict）。
- `hermes_mcp add` 與後台 `POST /api/mcp-servers/live` 只收 http／https＋SSRF，拒絕 stdio transport 與 command 類欄位。

## [1.1.0] - 2026-06-27

### Added
- Dashboard proxy router (`dashboard_proxy.py`) — proxies 8 endpoint groups to Hermes Dashboard API
- Full health endpoint with model, tools, skills, sessions, MCP servers summary data
- Cookie caching (10-minute TTL) to prevent Dashboard 429 rate limiting
- Mobile responsive layout with hamburger menu sidebar
- 15 screenshots captured from live deployment

### Fixed
- Health response keys (`gateway_health` → `gateway`) to match frontend expectations
- ConfigEditor stale closure bug (treeData → rawContent sync)
- SessionManager bulk delete count closure bug
- Model provider fallback to `"auto"` when providers config is empty
- Correct Dashboard API paths (`/api/config` not `/api/settings`)

### Changed
- All 17 frontend files updated for responsive web design (RWD)
- Page headers use responsive text sizing and flex-wrap
- Viewport height calculations account for mobile header

## [1.0.0] - 2026-06-27

### Added
- Initial release
- 3-package Python architecture: `mcp_admin_core`, `hermes_mcp_admin`, `hermes_mcp_server`
- 9 FastMCP tools (Read/Write/Agent categories)
- 15 React admin pages with dark theme
- Dual connection support (Gateway API + Dashboard API)
- Cookie-based auth for Hermes Dashboard v0.17.0
- JWT authentication for admin GUI
- K8s deployment manifest with RBAC
- Docker multi-stage build (Node 20 + Python 3.12)
- Cloudflare Tunnel integration
