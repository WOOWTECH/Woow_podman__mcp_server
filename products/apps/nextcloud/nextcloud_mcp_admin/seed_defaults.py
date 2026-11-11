"""admin 啟動時補齊 Nextcloud 子行程的預設（seed 漏寫時的保險）。

PaaS 範本會 seed ``mcp_server``（command／args／port／env_prefix）；但 chart 是 seed-if-absent、本機
``docker run`` 也沒有 seed。core lifespan 拉起子行程之前：

- ``tools`` 沒有 ``nextcloud_policy_seeded`` 標記 → 把 ``tool_registry.default_disabled_tools()``（新安裝為
  空：9 支全開，負責人 2026-10-25「預設全開」）**併入** ``tools.disabled``，再寫上標記。既有停用清單只增不減。
  本檔從第一版（2026-10-08）起就在第一次啟動時寫上標記，所以沒有標記＝從未啟動過的新安裝。已有標記的既有服務
  （當時出廠會停用 create_text_file、update_text_file、upload_file、delete_file_checked 四支）由 core 的
  ``owner_defaults`` 在第一次以本映像啟動時一次性清空停用清單（9 支全開，並寫上它自己的標記）；之後租戶在
  Tools 頁關掉的工具會保留。
- ``mcp_server.env_prefix`` 為空或沒有 → 補 ``"NEXTCLOUD_MCP_"``。connection 鍵 ``base_url``／
  ``username``／``app_password`` 要靠這個前綴才會變成子行程讀的 ``NEXTCLOUD_MCP_*``；前綴空著子行程會因
  缺設定直接 exit 2。已設成其他非空值則尊重（自訂部署）。

只用 ``store.patch``（淺合併），不動 command／args／port 等其他鍵。
"""
from __future__ import annotations

import logging

from mcp_admin_core.config import get_config_store
from mcp_admin_core.tool_filter import normalize_disabled

from . import tool_registry

logger = logging.getLogger(__name__)

ENV_PREFIX = "NEXTCLOUD_MCP_"
POLICY_SEEDED_KEY = "nextcloud_policy_seeded"


async def apply_seed_defaults() -> list[str]:
    """補齊安全預設，回傳有補的項目名稱（供測試與 log）。"""
    store = get_config_store()
    changed: list[str] = []

    tools_cfg = await store.get("tools", {}) or {}
    if not isinstance(tools_cfg, dict):
        tools_cfg = {}
    if not tools_cfg.get(POLICY_SEEDED_KEY):
        current = normalize_disabled(tools_cfg.get("disabled", []))
        extra = [name for name in tool_registry.default_disabled_tools() if name not in current]
        # 保留原本的順序與內容（含未知名稱），只在後面補上預設要關的工具。
        existing = tools_cfg.get("disabled", [])
        disabled = list(existing) if isinstance(existing, list) else sorted(current)
        await store.patch("tools", {"disabled": disabled + extra, POLICY_SEEDED_KEY: True})
        changed.append("tools.disabled")

    mcp_cfg = await store.get("mcp_server", {}) or {}
    if not mcp_cfg.get("env_prefix"):
        await store.patch("mcp_server", {"env_prefix": ENV_PREFIX})
        changed.append("env_prefix")

    if changed:
        logger.warning("Applied Nextcloud MCP safe defaults: %s", ", ".join(changed))
    return changed
