"""LiteLLM MCP 子行程 launcher（console script ``litellm-mcp-server``）。

McpProcessManager 依 config.json 的 ``mcp_server.command`` 於執行期 spawn 本 launcher，
launcher 再交給 vendored ``woow_litellm_mcp_server.server.main()``（Streamable HTTP）。
vendored 套件維持與來源 repo byte-identical，所有部署期約束集中在這裡：

- **只綁 127.0.0.1**（安全邊界，seed 不可覆寫）：core proxy 是唯一入口（``/private_{token}/``
  驗 token 後轉 ``127.0.0.1:{mcp_server.port}``）。PaaS 的 baseline NetworkPolicy 允許同工作區
  pod 互連，子行程若綁 0.0.0.0，同工作區任何 pod 都能繞過 token 直接打 40 支工具。比照
  apps/k8s launcher 硬編 bind-address。
- **transport 固定 Streamable HTTP**，path 預設 ``/mcp``（對齊 monorepo 其他 app：client URL
  ``https://<host>/private_<token>/mcp``）。
- ``--port`` 預設 8000，**必須等於** config.json 的 ``mcp_server.port``（core proxy 轉發目標）。
- **不對外連線、不印 banner**：FastMCP 3 預設印 ASCII banner 並連 PyPI 檢查新版；PaaS 子行程
  一律關掉（``FASTMCP_SHOW_SERVER_BANNER=false``、``FASTMCP_CHECK_FOR_UPDATES=off``，可被 env 覆寫）。
- **log 衛生**：子行程的 httpx logger 壓到 WARNING。``litellm_key_info`` 把被查詢的虛擬金鑰放
  在 query string（``/key/info?key=sk-…``）、``litellm_regenerate_key`` 放在路徑，httpx 的
  INFO log 會把完整 URL（含金鑰）印出，再經 McpProcessManager 進到 Logs 頁與容器 stdout。
- **請求內容上限 16 MiB**（``mcp_admin_core.request_body``，與其他 Python 子行程一致）：MCP SDK（1.30.0 起，
  2.x 相同）的 transport 預設 4 MiB、超過就在處理前回 413，fastmcp 4.x 建 transport 時不傳這個參數，故啟動前
  ``request_body.install()`` 改 SDK 的預設值。

用法（seed 契約）::

    "mcp_server": {"command": "litellm-mcp-server", "args": ["--port", "8000"],
                   "port": 8000, "env_prefix": "LITELLM_MCP_",
                   "env": {"LITELLM_MCP_READONLY": "0"}}   # 新安裝全開；"1"＝唯讀（Settings 頁開關）
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from mcp_admin_core import request_body

LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_PATH = "/mcp"

# FastMCP 3 的 banner／PyPI 版本檢查（見模組說明）；setdefault，部署端仍可用 env 覆寫。
FASTMCP_ENV_DEFAULTS = {
    "FASTMCP_SHOW_SERVER_BANNER": "false",
    "FASTMCP_CHECK_FOR_UPDATES": "off",
}

# 會把完整請求 URL 印在 INFO 的 HTTP client logger（見模組說明）。
_URL_LOGGING_LOGGERS = ("httpx", "httpcore")

logger = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="litellm-mcp-server",
        description="LiteLLM MCP server launcher (Streamable HTTP, loopback only).",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Loopback port (== mcp_server.port).")
    parser.add_argument("--path", default=DEFAULT_PATH, help="Streamable HTTP endpoint path (default /mcp).")
    parser.add_argument("--log-level", default="INFO", help="DEBUG/INFO/WARNING/ERROR.")
    # 以下兩個參數只為相容舊 seed（來源 repo 的 chart 會帶）；值一律忽略。
    parser.add_argument("--host", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--transport", default=None, help=argparse.SUPPRESS)
    return parser


def build_server_argv(argv: list[str] | None = None) -> list[str]:
    """把 launcher 參數轉成 vendored server.main() 的 argv（不含 argv[0]）。"""
    args = _parser().parse_args(argv)
    if args.host not in (None, LOOPBACK_HOST, "localhost"):
        logger.warning("Ignoring --host %s: the MCP child always binds %s", args.host, LOOPBACK_HOST)
    if args.transport not in (None, "http", "streamable-http"):
        logger.warning("Ignoring --transport %s: the MCP child always serves Streamable HTTP", args.transport)
    path = args.path if args.path.startswith("/") else f"/{args.path}"
    return [
        "--transport", "http",
        "--host", LOOPBACK_HOST,
        "--port", str(args.port),
        "--path", path,
        "--log-level", args.log_level,
    ]


def quiet_url_loggers() -> None:
    for name in _URL_LOGGING_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def apply_fastmcp_env_defaults() -> None:
    for key, value in FASTMCP_ENV_DEFAULTS.items():
        os.environ.setdefault(key, value)


def main(argv: list[str] | None = None) -> None:
    server_argv = build_server_argv(sys.argv[1:] if argv is None else argv)
    quiet_url_loggers()
    apply_fastmcp_env_defaults()
    request_body.install()  # 必須在 server 建立 transport 之前
    # import 放在這裡：vendored server 模組在 import 時就以 env 建好 FastMCP 與 gate。
    from woow_litellm_mcp_server import server

    sys.argv = ["litellm-mcp-server", *server_argv]
    server.main()


if __name__ == "__main__":  # pragma: no cover
    main()
