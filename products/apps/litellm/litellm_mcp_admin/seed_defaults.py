"""admin 啟動時補齊 LiteLLM 子行程的預設（seed 漏寫時的保險）。

PaaS 範本會 seed ``mcp_server.env.LITELLM_MCP_READONLY`` 與 ``env_prefix="LITELLM_MCP_"``；
但 chart 是 seed-if-absent、本機 ``docker run`` 也沒有 seed。core lifespan 拉起子行程之前：

- ``mcp_server.env`` **沒有** ``LITELLM_MCP_READONLY`` 鍵 → 補 ``"0"``（全開：40 支工具都註冊，負責人
  2026-10-25「預設全開」）。鍵已存在一律不動；Settings 頁的開關把這個鍵從泛用 env 清單隱藏，租戶只能切 1／0、
  刪不掉。2026-10-25 之前開通、seed 寫 ``"1"`` 的既有服務，由 core 的 ``owner_defaults`` 在第一次以本映像
  啟動時一次性改成 ``"0"``（之後租戶再切回 ``"1"`` 會保留）。
- ``mcp_server.env_prefix`` 為空或沒有 → 補 ``"LITELLM_MCP_"``。connection 鍵是
  ``base_url``／``master_key``，子行程只讀 ``LITELLM_MCP_*``；前綴空著＝子行程永遠連
  ``http://localhost:4000``、沒有金鑰。已設成其他非空值則尊重（自訂部署）。

只用 ``store.patch``（淺合併），不動 command／args／port 等其他鍵。
"""
from __future__ import annotations

import logging

from mcp_admin_core.config import get_config_store

logger = logging.getLogger(__name__)

READONLY_ENV = "LITELLM_MCP_READONLY"
# 新安裝的唯讀開關值："0"＝關閉唯讀（全開）。
FRESH_READONLY = "0"
ENV_PREFIX = "LITELLM_MCP_"


async def apply_seed_defaults() -> list[str]:
    """補齊預設，回傳有補的項目名稱（供測試與 log）。"""
    store = get_config_store()
    mcp_cfg = await store.get("mcp_server", {}) or {}
    patch: dict = {}
    changed: list[str] = []

    env = mcp_cfg.get("env")
    env = dict(env) if isinstance(env, dict) else {}
    if READONLY_ENV not in env:
        env[READONLY_ENV] = FRESH_READONLY
        patch["env"] = env
        changed.append(READONLY_ENV)

    if not mcp_cfg.get("env_prefix"):
        patch["env_prefix"] = ENV_PREFIX
        changed.append("env_prefix")

    if patch:
        await store.patch("mcp_server", patch)
        logger.warning("Applied LiteLLM MCP defaults: %s", ", ".join(changed))
    return changed
