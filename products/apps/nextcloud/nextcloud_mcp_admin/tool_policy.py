"""Tools 頁政策變更 → 必要時重啟 MCP 子行程。

core 的 ``PUT /api/tools`` 只把 ``tools.disabled`` 存檔、**不重啟**子行程（#1384：停用由 proxy 即時強制，
重啟會讓所有現有 MCP session 失效）。這對「停用」足夠，但 Nextcloud 子行程是依政策 env
（``NEXTCLOUD_MCP_READONLY``／``ALLOW_DELETE``／``DISABLED_TOOLS``，見 ``launcher.policy_env``）在啟動時
就決定註冊哪些工具：Tools 頁**打開**一支原本被停用的工具（例如 2026-10-25 之前開通、出廠關閉的 create_text_file）時，
子行程裡根本沒有這支工具，必須重啟才會註冊。

``ToolPolicyMiddleware`` 只攔 ``PUT /api/tools``：記下存檔前的停用清單，回應 200 之後若「開啟的工具」
比之前多（政策放寬），且子行程正在執行 → ``restart()``。只是停用更多工具時不重啟（proxy 已擋下，
下次重啟時 launcher 也會把它們列進 ``DISABLED_TOOLS``）。純 ASGI middleware，不碰其他路徑
（MCP proxy 的串流回應完全不經過這裡的邏輯）。
"""
from __future__ import annotations

import logging

from starlette._utils import get_route_path

from mcp_admin_core.config import get_config_store
from mcp_admin_core.process import get_process_manager
from mcp_admin_core.tool_filter import read_disabled_tools

from . import tool_registry

logger = logging.getLogger(__name__)

TOOLS_PATH = "/api/tools"


def enabled_tools(disabled: frozenset[str]) -> frozenset[str]:
    return frozenset(tool_registry.get_all_tool_names()) - disabled


async def current_disabled() -> frozenset[str]:
    return await read_disabled_tools(get_config_store())


async def apply_after_update(before: frozenset[str]) -> bool:
    """存檔後呼叫：政策放寬且子行程在跑就重啟。回傳是否有重啟。"""
    after = await current_disabled()
    widened = enabled_tools(after) - enabled_tools(before)
    if not widened:
        return False
    pm = get_process_manager()
    if not pm.is_running:
        # 沒在跑：下次 start（存連線設定、自動重啟或 admin 重開）時 launcher 就會讀到新政策。
        return False
    logger.warning("Tool policy widened (%s); restarting the MCP server", ", ".join(sorted(widened)))
    try:
        return bool(await pm.restart())
    except Exception as exc:  # noqa: BLE001 — 政策已存檔，重啟失敗交給 Settings 頁顯示
        logger.warning("MCP restart after tool policy change failed: %s", exc)
        return False


class ToolPolicyMiddleware:
    """純 ASGI：只對 ``PUT /api/tools`` 在成功回應後套用 ``apply_after_update``。"""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if (
            scope.get("type") != "http"
            or scope.get("method") != "PUT"
            or get_route_path(scope).rstrip("/") != TOOLS_PATH
        ):
            await self.app(scope, receive, send)
            return

        before = await current_disabled()
        status: dict[str, int] = {}

        async def _send(message) -> None:
            if message.get("type") == "http.response.start":
                status["code"] = int(message.get("status", 0))
            await send(message)

        await self.app(scope, receive, _send)
        if status.get("code") == 200:
            await apply_after_update(before)
