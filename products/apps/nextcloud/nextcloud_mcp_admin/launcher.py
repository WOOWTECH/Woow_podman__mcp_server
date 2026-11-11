"""Nextcloud MCP 子行程 launcher（console script ``nextcloud-mcp-server``）。

McpProcessManager 依 config.json 的 ``mcp_server.command`` 於執行期 spawn 本 launcher，launcher 再交給
vendored ``nextcloud_mcp_server.server.main()``。vendored 套件與來源 repo byte-identical，所有部署期約束
集中在這裡（比照 apps/litellm launcher）：

- **只綁 127.0.0.1**（安全邊界，seed 不可覆寫）：core proxy 是唯一入口（``/private_{token}/`` 驗 token 後
  轉 ``127.0.0.1:{mcp_server.port}``）。vendored server 本身沒有任何認證，綁 0.0.0.0 等於讓同工作區任何 pod
  繞過 token 直接用這個 Nextcloud 帳號。
- **transport 固定 Streamable HTTP**，path 預設 ``/mcp``（client URL ``https://<host>/private_<token>/mcp``）。
- ``--port`` 預設 8000，**必須等於** config.json 的 ``mcp_server.port``（core proxy 轉發目標）。
- **不對外連線、不印 banner**：``FASTMCP_SHOW_SERVER_BANNER=false``、``FASTMCP_CHECK_FOR_UPDATES=off``
  （setdefault，部署端仍可用 env 覆寫）。
- **log 衛生**：httpx／httpcore logger 壓到 WARNING（INFO 會印完整 WebDAV URL，也就是使用者的檔名路徑）。
- **請求內容上限 16 MiB**（``mcp_admin_core.request_body``，與其他 Python 子行程一致）：``upload_file`` 以
  base64 收檔案（上限 10 MiB，約 13.3 MiB 的 JSON），MCP SDK（1.30.0 起，2.x 相同）的 transport 預設 4 MiB、
  超過就在處理前回 413；fastmcp 4.x 建 transport 時不傳這個參數，故啟動前 ``request_body.install()`` 改 SDK 的預設值。
- **連線**：``base_url``／``username``／``app_password`` 由 core 依 ``mcp_server.env_prefix="NEXTCLOUD_MCP_"``
  注入成 ``NEXTCLOUD_MCP_BASE_URL``／``USERNAME``／``APP_PASSWORD`` 環境變數（見 ``seed_defaults``）；
  **app password 只走環境變數，絕不出現在 argv**（argv 會出現在 ``ps``、Settings 頁與 log）。

工具政策 → 子行程 env（單一來源＝Tools 頁的 ``tools.disabled``）
------------------------------------------------------------------
vendored server 用三個 env 決定註冊哪些工具；launcher 每次啟動都從 config.json（``MCP_ADMIN_CONFIG``）
讀 ``tools.disabled`` 重新算出來並**強制覆寫**（``mcp_server.env`` 裡手動填的同名鍵不算數）：

- ``NEXTCLOUD_MCP_READONLY``：政策有開任何 write／destructive 工具才給 ``false``，否則 ``true``。
- ``NEXTCLOUD_MCP_ALLOW_DELETE``：政策有開 ``delete_file_checked`` 才給 ``true``，否則 ``false``。
- ``NEXTCLOUD_MCP_DISABLED_TOOLS``：政策停用的每一支（只列已知工具名，逗號分隔、固定順序）。

所以子行程註冊的工具＝政策開啟的工具，一支不多。讀不到 config.json 時 fail closed：唯讀、不准刪除。
Tools 頁停用工具時 core proxy 立即擋下（#1384）；開放原本沒註冊的工具時由 ``tool_policy`` 自動重啟子行程。

用法（seed 契約）::

    "mcp_server": {"command": "nextcloud-mcp-server", "args": ["--port", "8000"],
                   "port": 8000, "env_prefix": "NEXTCLOUD_MCP_"}
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Iterable
from pathlib import Path

from mcp_admin_core import request_body
from mcp_admin_core.tool_filter import normalize_disabled

# ⚠️ 本模組頂層不可 import fastmcp（含 vendored nextcloud_mcp_server.tools、本 app 的 tool_registry）：
#    FastMCP 在 import 時就讀 FASTMCP_* 設定，必須先 apply_fastmcp_env_defaults() 才 import，
#    否則 banner／PyPI 版本檢查照樣執行（tests/test_launcher.py 守線）。

LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_PATH = "/mcp"
DEFAULT_CONFIG_PATH = "/data/config.json"

ENV_READONLY = "NEXTCLOUD_MCP_READONLY"
ENV_ALLOW_DELETE = "NEXTCLOUD_MCP_ALLOW_DELETE"
ENV_DISABLED_TOOLS = "NEXTCLOUD_MCP_DISABLED_TOOLS"
GATING_ENV_KEYS = (ENV_READONLY, ENV_ALLOW_DELETE, ENV_DISABLED_TOOLS)

FASTMCP_ENV_DEFAULTS = {
    "FASTMCP_SHOW_SERVER_BANNER": "false",
    "FASTMCP_CHECK_FOR_UPDATES": "off",
}

_URL_LOGGING_LOGGERS = ("httpx", "httpcore")

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# argv
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nextcloud-mcp-server",
        description="Nextcloud MCP server launcher (Streamable HTTP, loopback only).",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Loopback port (== mcp_server.port).")
    parser.add_argument("--path", default=DEFAULT_PATH, help="Streamable HTTP endpoint path (default /mcp).")
    # 以下參數只為相容來源 repo 的 gateway 呼叫法（--transport http --host 127.0.0.1 …）；值一律忽略。
    parser.add_argument("--host", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--transport", default=None, help=argparse.SUPPRESS)
    return parser


def build_server_argv(argv: list[str] | None = None) -> list[str]:
    """把 launcher 參數轉成 vendored ``server.main()`` 的 argv（不含 argv[0]）。"""
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
    ]


# ---------------------------------------------------------------------------
# tool policy → env
# ---------------------------------------------------------------------------


def gating_env(disabled: Iterable[str]) -> dict[str, str]:
    """由「被停用的工具」算出子行程的三個 gating env（未知工具名略過）。"""
    from . import tool_registry  # 延後 import（會載入 fastmcp），見模組頂端說明

    known = set(tool_registry.get_all_tool_names())
    off = {name for name in disabled if name in known}
    enabled = known - off
    writes = set(tool_registry.write_tool_names()) | set(tool_registry.dangerous_tool_names())
    deletes = set(tool_registry.dangerous_tool_names())
    ordered_off = [name for name in tool_registry.get_all_tool_names() if name in off]
    return {
        ENV_READONLY: "false" if enabled & writes else "true",
        ENV_ALLOW_DELETE: "true" if enabled & deletes else "false",
        ENV_DISABLED_TOOLS: ",".join(ordered_off),
    }


def config_path() -> Path:
    return Path(os.environ.get("MCP_ADMIN_CONFIG") or DEFAULT_CONFIG_PATH)


def read_disabled_tools(path: Path | None = None) -> frozenset[str] | None:
    """讀 config.json 的 ``tools.disabled``；檔案讀不到或格式不對回 None（呼叫端 fail closed）。"""
    path = path or config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    tools_cfg = data.get("tools", {})
    if not isinstance(tools_cfg, dict):
        return None
    return normalize_disabled(tools_cfg.get("disabled", []))


def policy_env(path: Path | None = None) -> dict[str, str]:
    """目前政策對應的 gating env；讀不到政策時唯讀、不准刪除（fail closed）。"""
    disabled = read_disabled_tools(path)
    if disabled is None:
        logger.warning("Cannot read the tool policy from %s; starting read-only", path or config_path())
        return {ENV_READONLY: "true", ENV_ALLOW_DELETE: "false", ENV_DISABLED_TOOLS: ""}
    return gating_env(disabled)


def apply_policy_env(path: Path | None = None) -> dict[str, str]:
    """把政策 env 強制寫進 os.environ（覆寫 mcp_server.env 裡的同名鍵），回傳寫入的值。"""
    env = policy_env(path)
    os.environ.update(env)
    return env


# ---------------------------------------------------------------------------
# misc
# ---------------------------------------------------------------------------


def quiet_url_loggers() -> None:
    for name in _URL_LOGGING_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def apply_fastmcp_env_defaults() -> None:
    for key, value in FASTMCP_ENV_DEFAULTS.items():
        os.environ.setdefault(key, value)


def main(argv: list[str] | None = None) -> None:
    server_argv = build_server_argv(sys.argv[1:] if argv is None else argv)
    quiet_url_loggers()
    apply_fastmcp_env_defaults()  # 必須在任何 fastmcp import 之前
    request_body.install()  # 必須在 server 建立 transport 之前
    env = apply_policy_env()
    # 只印政策，不印連線設定（app password 在 env 裡，絕不輸出）。
    print(
        "nextcloud-mcp-server: tool policy "
        + " ".join(f"{key}={env[key] or '-'}" for key in GATING_ENV_KEYS),
        file=sys.stderr,
        flush=True,
    )
    # import 放在這裡：vendored server 在 main() 內才讀 NEXTCLOUD_MCP_* env 建立 Settings。
    from nextcloud_mcp_server import server

    sys.exit(server.main(server_argv))


if __name__ == "__main__":  # pragma: no cover
    main()
