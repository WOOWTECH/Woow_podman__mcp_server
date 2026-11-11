"""Odoo MCP 寫入開關：全開預設（負責人 2026-10-25「預設全開」，與 HA add-on 0.2.0 起相同）。

四個開關都交給子行程（``LaunchPolicy.env_keys``），也都是 Settings 頁的開關：

- ``ODOO_MCP_ENABLE_WRITES``：寫入總閘門——關掉時任何工具都不寫（execute_method 只跑審過的讀取方法
  ``write_gates.REVIEWED_READ_METHODS``，上游依名字當唯讀的其他 get_* 不算；chatter_post 與
  preview → validate → execute_approved_write 的最後一步都拒絕）。
- ``ODOO_MCP_ALLOW_DIRECT_WRITES``：execute_method 直接 create／write／unlink（``patches/fix_direct_writes.py``；
  ``health_check`` 的 ``runtime.direct_writes_enabled`` 以同一個判斷式回報）；關掉時未審查的方法也不直接執行；
  三支審批工具照樣列出（41 支，同 HA 0.4.0），只是不再必要。
- ``ODOO_MCP_ALLOW_UNKNOWN_METHODS``：execute_method 可呼叫未審查的副作用方法（action_confirm、action_post…）。
- ``MCP_CHATTER_DIRECT``：chatter_post 第一次呼叫就張貼，不先回預覽與核准權杖；關掉時 execute_method 也不跑在 chatter
  張貼的呼叫（message_*、mail.message／mail.compose.message／mail.scheduled.message、完成活動與評分的張貼方法）。

四個開關的完整語意與判斷順序在 ``write_gates``（``docs/write-switches.md``）。

``FRESH_ENV``（四個都開）是 ``LaunchPolicy.fresh_env``：本行程建立的 config.json 直接帶這組值；既有 config.json
在第一次以本映像啟動時由 core 的 ``owner_defaults`` 一次性改成這組值並寫上標記（PaaS 範本 seed 也明寫同一組值）。

``LEGACY_DEFAULTS`` 只在「已套用標記、卻缺鍵」的設定補值（目前不會發生，留給之後新增的開關）：值＝改版前的
行為（審核寫入開，其餘關）。管理員自己存的 0／1 一律不動，不改連線、憑證或其他設定。
"""
from mcp_admin_core.config import get_config_store

FRESH_ENV = {
    "ODOO_MCP_ENABLE_WRITES": "1",
    "ODOO_MCP_ALLOW_DIRECT_WRITES": "1",
    "ODOO_MCP_ALLOW_UNKNOWN_METHODS": "1",
    "MCP_CHATTER_DIRECT": "1",
}

LEGACY_DEFAULTS = {
    "ODOO_MCP_ENABLE_WRITES": "1",
    "ODOO_MCP_ALLOW_DIRECT_WRITES": "0",
    "ODOO_MCP_ALLOW_UNKNOWN_METHODS": "0",
    "MCP_CHATTER_DIRECT": "0",
}

POLICY_KEYS = tuple(FRESH_ENV)


def write_defaults(cfg):
    """子行程要拿到的四個開關：已存的值，缺鍵時用既有服務原本的值（``LEGACY_DEFAULTS``）。"""
    stored = (cfg.get("mcp_server") or {}).get("env") or {}
    return {key: str(stored.get(key, LEGACY_DEFAULTS[key])) for key in POLICY_KEYS}


async def apply_seed_defaults():
    """把缺的鍵以 ``LEGACY_DEFAULTS`` 寫進 config.json（只補不改），回傳補上的鍵。"""
    store = get_config_store()
    mcp_cfg = await store.get("mcp_server", {}) or {}
    env = dict(mcp_cfg.get("env") or {})
    changed = [key for key in POLICY_KEYS if key not in env]
    if changed:
        for key in changed:
            env[key] = LEGACY_DEFAULTS[key]
        await store.patch("mcp_server", {"env": env})
    return changed
