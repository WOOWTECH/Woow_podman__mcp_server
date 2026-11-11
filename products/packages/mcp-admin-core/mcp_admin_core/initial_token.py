"""第一次啟動、從來沒有過 MCP token 的設定自動發一個 token（第三輪 G4）。

PaaS 新開的 MCP 服務 config.json 由 chart seed 寫好、平台只 seed 管理密碼，``mcp_auth_token`` 是空的：管理員不先到
Tokens 頁輪替，用戶端就連不上（正式站 266 開通 11 天沒人能用）。core lifespan 在 ``owner_defaults`` 之後、拉起子行程
之前呼叫 :func:`ensure_initial_mcp_token`：

- 只發給「從來沒有過 token」的設定（``config.store.never_had_mcp_token``：token 空、沒有撤銷、沒有 ``token_history``、
  沒有 ``mcp_auth_generation``）——本行程新建的 config.json、部署 seed 剛寫好的新服務、開通後從沒設過 token 的舊服務。
  撤銷過的（``mcp_auth_revoked``）與留有任何 token 紀錄的都不動，不會自動復活；之後要用 Tokens 頁的輪替重發。
- 產生器與記帳和管理頁輪替相同（``routers.tokens.new_token``、``ConfigStore`` 同一個 token 轉換：一筆
  ``token_history``（``action="auto_issue"``）、新的 generation），判斷與寫入在同一把鎖內。
- 寫一行 log（容器 stdout 與 Logs 頁），**不含 token**；完整 MCP 網址在 Tokens 頁（揭露 60 秒、離開視窗就隱藏）。
- 放在 ``owner_defaults`` 之後：標記的 ``source`` 仍依「第一次以新映像啟動時」的狀態判斷（PaaS 新服務＝``deploy_seed``）。
- **寫不進設定檔不擋啟動**（審查第三輪 G4）：PVC 滿、Longhorn 降成唯讀這類 ``OSError`` 時記一行 warning（不含 token）、
  這次不發，管理程序照常啟動——與自動發 token 之前一樣：/healthz 與管理頁照常回應，用戶端還連不上，寫得進去之後再到
  Tokens 頁 Rotate Token（或重啟時再自動發）。注意從沒登入過的設定第一次登入也要寫檔（``ConfigStore.get_admin_auth`` 補
  ``admin_session_generation``），寫不進去時那次登入回 500——這是自動發 token 之前就有的行為。寫入失敗時檔案與記憶體裡的
  設定都不變（``ConfigStore`` 寫完才換快取）。讀不到或壞掉的設定照舊拋 ``ConfigUnavailable``（fail safe）。
"""
from __future__ import annotations

import logging

from .owner_defaults import _StdoutHandler

logger = logging.getLogger(__name__)
if not any(isinstance(handler, _StdoutHandler) for handler in logger.handlers):
    logger.addHandler(_StdoutHandler())  # 同 owner_defaults：admin 沒有設定 root logger，kubectl logs 才看得到
if logger.level == logging.NOTSET or logger.level > logging.INFO:
    logger.setLevel(logging.INFO)

AUTO_ISSUE_ACTION = "auto_issue"


async def ensure_initial_mcp_token(store=None) -> bool:
    """設定從來沒有過 MCP token 時發一個（回傳有沒有發）。

    寫不進設定檔（``OSError``）時記 warning、回 ``False``，不擋啟動；讀不到設定照舊拋 ``ConfigUnavailable``。
    """
    from .config import get_config_store
    from .routers.tokens import new_token

    store = store or get_config_store()
    try:
        issued = await store.issue_initial_mcp_token(new_token, action=AUTO_ISSUE_ACTION)
    except OSError as exc:
        logger.warning("這份設定從來沒有過 MCP token，但設定檔寫不進去（%s），這次沒有自動產生；管理程序照常啟動，"
                       "用戶端要等設定檔寫得進去之後到管理頁 Tokens 頁 Rotate Token（或下次啟動再自動產生）",
                       type(exc).__name__)
        return False
    if issued:
        logger.info("這份設定從來沒有過 MCP token，第一次啟動已自動產生一個（token_history action=%s）；"
                    "完整 MCP 網址在管理頁 Tokens 頁", AUTO_ISSUE_ACTION)
    return issued
