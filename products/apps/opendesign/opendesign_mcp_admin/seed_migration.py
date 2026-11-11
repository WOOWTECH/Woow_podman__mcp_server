"""舊 supergateway seed → 原生 launcher 的啟動時遷移（#1369）。

PaaS chart 的 initContainer 是 **seed-if-absent**（config.json 已存在就不動），所以既有
mcp-opendesign 服務的 PVC 會一直保留舊 seed::

    "command": "supergateway",
    "args": ["--stdio", "python -m opendesign_mcp_server", "--outputTransport", "streamableHttp", ...]

這條鏈每個 MCP session 都 spawn 一個 python 子行程且不回收（~115MB／session → OOM）。
admin 啟動、MCP 子行程拉起之前，若偵測到**完全是**這個舊形狀，就改寫成原生 launcher：

    "command": "opendesign-mcp-server",
    "args": ["--host", "127.0.0.1", "--port", "<mcp_server.port>"]

只認 command == "supergateway" 且 args 內含 ``opendesign_mcp_server`` 的 seed；其他自訂
command 一律不動。``port``／``env``／``env_prefix`` 等其餘鍵保留（store.patch 淺合併）。
"""
from __future__ import annotations

import logging
from typing import Any

from mcp_admin_core.config import get_config_store

logger = logging.getLogger(__name__)

NATIVE_COMMAND = "opendesign-mcp-server"
_LEGACY_COMMAND = "supergateway"
_LEGACY_MARKER = "opendesign_mcp_server"


def is_legacy_supergateway_seed(mcp_cfg: dict[str, Any]) -> bool:
    if mcp_cfg.get("command", "") != _LEGACY_COMMAND:
        return False
    return any(_LEGACY_MARKER in str(a) for a in mcp_cfg.get("args", []) or [])


def native_args(port: int) -> list[str]:
    return ["--host", "127.0.0.1", "--port", str(port)]


async def migrate_legacy_seed() -> bool:
    """偵測到舊 supergateway seed 就改寫成原生 launcher；回傳是否有改寫。"""
    store = get_config_store()
    mcp_cfg = await store.get("mcp_server", {}) or {}
    if not is_legacy_supergateway_seed(mcp_cfg):
        return False
    try:
        port = int(mcp_cfg.get("port", 8000))
    except (TypeError, ValueError):
        port = 8000
    await store.patch(
        "mcp_server",
        {"command": NATIVE_COMMAND, "args": native_args(port), "port": port},
    )
    logger.warning(
        "Migrated legacy supergateway seed to native '%s' (port %s) — "
        "supergateway spawned one python process per MCP session (#1369)",
        NATIVE_COMMAND,
        port,
    )
    return True
