"""n8n MCP server 的 HTTP launcher（console script ``n8n-mcp-http``）。

n8n-mcp（npm，全域安裝）的 bin（``dist/mcp/stdio-wrapper.js``）硬寫 ``MCP_MODE='stdio'``，
所以 launcher 直接以 node 執行上游建議的 HTTP 入口 ``dist/mcp/index.js``：``MCP_MODE=http``、
**不設** ``USE_FIXED_HTTP`` → ``SingleSessionHTTPServer``（Streamable HTTP＋SSE、有 session）。

woow_paas_platform#1531：舊版 exec 的 ``dist/http-server.js`` 是上游 v2.31.8 起標為 deprecated
的固定 HTTP 模式——無狀態、``initialize`` 宣告 resources 但 ``resources/*``／``prompts/list``／
``ping`` 全回 -32601、GET /mcp 拿不到 SSE 串流，也不讀 session 上限與逾時設定。

- 套件位置以 ``npm root -g`` 動態定位（``upstream.package_dir``），不硬編 node 全域路徑。
- 只綁 loopback ``HOST:{port}``（預設 ``127.0.0.1:3000``，對齊 config ``mcp_server.port``）；
  mcp-admin 的反向代理（``/private_{token}/mcp`` → ``127.0.0.1:{port}/mcp``）是唯一入口。
- n8n-mcp http 強制 ``AUTH_TOKEN``：由 config.json ``mcp_server.env.AUTH_TOKEN`` 注入，並與
  ``proxy.bearer_token`` 同值（proxy 轉發時帶 ``Authorization: Bearer``，client 只需 path token）。
- 上游的認證失敗限流（``AUTH_RATE_LIMIT_MAX``，預設 15 分鐘 20 次、以來源 IP 計）在這裡所有 client
  都經 proxy 從 127.0.0.1 進來、共用一個額度；真正的認證是 proxy 的 path token，故預設放寬。
- 以 ``execvpe`` 取代本行程，讓 admin 的 ``McpProcessManager`` 直接管到 node 子行程（SIGTERM 可達）。
- woow_paas_platform#1615：上游預設開啟匿名使用統計（送到 telemetry.n8n-mcp.com，含工具使用、去識別化的
  workflow 結構與 intent 文字）。PaaS 一律關閉：寫死上游的關閉開關 ``N8N_MCP_TELEMETRY_DISABLED=true``
  （``dist/telemetry/config-manager.js`` 的 ``isDisabledByEnvironment``，優先於 ``~/.n8n-mcp/telemetry.json``，
  也擋住 early error logger 與 startup checkpoints）。這裡是最後一步、蓋掉繼承來的同名變數；LaunchPolicy 的
  env 白名單（只有 ``AUTH_TOKEN``）也不讓管理頁或 config.json 帶進來，租戶沒有辦法打開。另外拿掉上游的
  ``N8N_MCP_TELEMETRY_URL``／``N8N_MCP_TELEMETRY_KEY``，避免被導去別的收集端。
- 帶帳號密碼（userinfo）的 ``N8N_API_URL``：管理頁存檔與測試連線都以 SSRF 規則拒絕這種網址
  （``mcp_admin_core.util.ssrf.has_userinfo``），新的設定存不進來。舊設定裡還留著的，launcher **拿掉帳密後**交給上游
  （:func:`sanitize_api_url`），印一行提醒（不含帳密）到輸出（管理頁 Logs／Settings 看得到）。理由：
  （1）第二輪審查指出若整個拿掉 ``N8N_API_URL``，既有服務的管理工具會不聲不響地消失；上游 env 路徑
  （``dist/services/n8n-api-client.js`` 的建構子）本來就把帳密拿掉後照連，API Key 才是認證。（2）但不能原樣交給上游：
  上游的 ``n8n_health_check``、錯誤訊息、建立 workflow 的網址與 log 都直接帶出原始的 ``N8N_API_URL``，帳密會出現在
  工具結果裡（本機以真的 n8n-mcp 2.96.2 實測）。主機部分含反斜線的網址（Python 與 WHATWG URL 解析不同，改寫可能換掉
  主機）不交給上游、照樣印出原因。判斷與 ``has_userinfo`` 逐字相同（測試比對）；不 import mcp_admin_core，免得 child 的
  輸出多出 admin 的啟動警告。
- npm 版本檢查一律關：n8n-mcp 的 ``n8n_health_check`` 與診斷會連 ``registry.npmjs.org`` 查最新版。映像釘版、
  租戶無法自行升級，「有新版」的提示只會誤導，也多一次對外連線。2.96 起上游提供開關
  ``N8N_MCP_VERSION_CHECK_DISABLED``（``dist/utils/npm-version-checker.js`` 的 ``isVersionCheckDisabled``，認
  "true"／"1"），比照 telemetry 在最後寫死 "true"，繼承來的 "false"／"0" 也蓋掉；上游因此回
  ``checkStatus: "disabled"``、不發請求。映像建置時 ``telemetry_check`` 一併驗證。

部署 seed（command／args 由 app 的 LaunchPolicy 固定，見 main.py）::

    "mcp_server": {
      "command": "n8n-mcp-http",
      "args": ["--port", "3000"],
      "port": 3000,
      "env": {"AUTH_TOKEN": "<from Secret>"},
      "env_prefix": ""
    }
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

from urllib.parse import urlsplit, urlunsplit

from . import upstream
from .database import prepare_database

# proxy 已做 path token 認證；上游以來源 IP 計的認證失敗限流在這裡只會讓所有 client 一起被鎖。
AUTH_RATE_LIMIT_MAX_DEFAULT = "1000"

# #1615：上游 telemetry 一律關閉（寫死，不可由設定打開）。上游認 "true"／"1"，三個變數任一為真就關。
TELEMETRY_OFF_ENV = {"N8N_MCP_TELEMETRY_DISABLED": "true"}
# 上游可改寫收集端的變數；關閉後用不到，仍一併移除。
TELEMETRY_ENDPOINT_ENV = ("N8N_MCP_TELEMETRY_URL", "N8N_MCP_TELEMETRY_KEY")
# n8n-mcp 2.96+ 對 registry.npmjs.org 的版本檢查一律關閉（寫死）。上游只認 "true"／"1"。
VERSION_CHECK_OFF_ENV = {"N8N_MCP_VERSION_CHECK_DISABLED": "true"}


USERINFO_STRIPPED = (
    "n8n-mcp-http: n8n API URL 含帳號或密碼（userinfo），已拿掉帳密再交給 n8n-mcp（n8n 以 API Key 認證）；管理頁的存檔與"
    "測試連線都會拒絕這種網址，請到 Connection 改成不含帳密的對外網址。Removed the username/password from the n8n API "
    "URL before starting n8n-mcp (the API key authenticates); please replace it with a URL without credentials."
)
USERINFO_AMBIGUOUS = (
    "n8n-mcp-http: n8n API URL 的主機部分含反斜線，無法確定是哪一台主機，不交給 n8n-mcp（n8n 管理工具暫時不能用）：請到"
    " Connection 改成正確的對外網址。The n8n API URL has a backslash in its authority and cannot be parsed "
    "unambiguously; n8n management tools stay unavailable until the URL is fixed."
)


def has_userinfo(url: str) -> bool:
    """與 ``mcp_admin_core.util.ssrf.has_userinfo`` 相同的判斷（URL 是否帶帳號或密碼）。"""
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    return bool(parts.username or parts.password)


def sanitize_api_url(env: dict[str, str]) -> str | None:
    """``N8N_API_URL`` 帶帳號密碼時就地拿掉帳密（主機部分含反斜線、無法確定主機時整個拿掉）；回傳要印的提醒，沒事回 None。

    只動 authority 裡最後一個 ``@`` 之前的部分（Python 與 WHATWG URL 都以最後一個 ``@`` 分開帳密與主機），scheme、
    主機、port、路徑、query 原樣保留。
    """
    url = env.get("N8N_API_URL", "")
    if not has_userinfo(url):
        return None
    parts = urlsplit(url.strip())
    if "\\" in parts.netloc:
        env.pop("N8N_API_URL", None)
        return USERINFO_AMBIGUOUS
    env["N8N_API_URL"] = urlunsplit((parts.scheme, parts.netloc.rpartition("@")[2], parts.path, parts.query,
                                     parts.fragment))
    return USERINFO_STRIPPED


def resolve_entrypoint() -> str:
    """回傳上游 HTTP 入口 ``dist/mcp/index.js`` 的路徑；不存在就結束（訊息進管理頁的 last_output）。"""
    path = upstream.entrypoint()
    if not os.path.exists(path):
        sys.exit(f"n8n-mcp 入口不存在：{path}（n8n-mcp 是否已全域安裝？）")
    return path


def build_env(host: str, port: int, base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    env["MCP_MODE"] = "http"
    env["HOST"] = host
    env["PORT"] = str(port)
    env.pop("USE_FIXED_HTTP", None)
    env.setdefault("AUTH_RATE_LIMIT_MAX", AUTH_RATE_LIMIT_MAX_DEFAULT)
    for key in TELEMETRY_ENDPOINT_ENV:
        env.pop(key, None)
    # 最後覆寫：繼承來的 "false"／"0" 也蓋掉
    env.update(TELEMETRY_OFF_ENV)
    env.update(VERSION_CHECK_OFF_ENV)
    return env


def main(argv: list[str] | None = None) -> None:
    """Console script 進入點（``n8n-mcp-http``）。"""
    parser = argparse.ArgumentParser(
        description="Run the n8n-mcp server over Streamable HTTP (fronted by the mcp-admin proxy)."
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind host；僅供 in-container proxy 反代用，務必維持 loopback（預設 127.0.0.1）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=3000,
        help="bind port；必須等於 config mcp_server.port（預設 3000）",
    )
    args = parser.parse_args(argv)

    entry = resolve_entrypoint()
    try:
        database_path = prepare_database(
            upstream.package_dir(), os.environ.get("MCP_ADMIN_CONFIG", "/data/config.json"))
    except (OSError, ValueError, sqlite3.Error):
        # 不輸出 DB 內容／connection／credentials，也不能退回包內唯讀 DB。
        sys.exit("n8n-mcp 資料庫初始化失敗；請檢查持久目錄與資料庫權限/完整性")
    env = build_env(args.host, args.port)
    notice = sanitize_api_url(env)
    if notice:
        print(notice, file=sys.stderr, flush=True)
    env["NODE_DB_PATH"] = database_path
    os.execvpe("node", ["node", entry], env)


if __name__ == "__main__":
    main()
