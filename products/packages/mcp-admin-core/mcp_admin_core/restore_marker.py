"""MCP 設定還原進行中標記：``<設定目錄>/.mcp-restore-in-progress`` 存在時，管理程序拒絕啟動（fail closed）。

設定目錄＝``MCP_ADMIN_CONFIG``（預設 ``/data/config.json``）的上一層，也就是 PaaS 的 PVC 根目錄。標記由
``python -m mcp_admin_core.restore_inplace`` 在動到任何檔案之前寫下、確認還原結果之後最後才移除（見該模組與
``docs/security/mcp-config-restore-candidate-20261112.md``）。標記還在＝還原沒有完成：設定目錄可能是舊的有效 token、
部分還原的結果，甚至沒有 ``config.json``（``ConfigStore`` 會以出廠預設值補一份）。這時任何 MCP 流量或設定寫入都可能讓
舊 token 復活，所以：

- **拒絕啟動**（:class:`RestoreInProgressGuard`）：最外層的 ASGI lifespan 檢查，在 app 自己的 seed／遷移
  （odoo／litellm／nextcloud 的 ``seed_defaults``、opendesign 的 ``seed_migration``、headscale／emqx 讀設定）、core 的
  ``owner_defaults``、自動發 token 與任何子行程之前。標記存在就回 ``lifespan.startup.failed``（再拋例外，與 Starlette
  router 相同）：uvicorn 記錄原因、``Application startup failed. Exiting.``、在建立監聽 socket 之前以 exit code 3 結束，
  所以沒有任何 HTTP 流量；Kubernetes 會一直重啟容器（CrashLoopBackOff），readiness 不會成功。**一定要先送
  ``startup.failed``**：uvicorn 預設 ``--lifespan auto`` 下，lifespan 在送出任何訊息前拋出的例外只會被當成「app 不支援
  lifespan」，照常開始服務。
- **不建立出廠設定**：``ConfigStore`` 要以預設值新建 ``config.json`` 時先檢查標記（:func:`restore_in_progress`），存在就
  ``ConfigUnavailable``——就算有程式在 lifespan 之外建立 store，也不會在還原到一半的目錄裡寫出 ``admin``／``admin``。
- 標記判斷 fail closed：任何種類的目錄項目（檔案、目錄、懸空 symlink）都算存在；``lstat`` 失敗但不是「不存在」
  （權限、I/O 錯誤）也當作存在。

只在啟動時檢查：服務執行中才出現的標記不會讓它停下來——還原必須先把服務縮到 0（見文件）。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from .owner_defaults import _StdoutHandler

MARKER_NAME = ".mcp-restore-in-progress"
#: 與 ``config.store._DEFAULT_CONFIG_PATH`` 相同（這裡不 import store：store 反過來會用本模組）。
DEFAULT_CONFIG_PATH = "/data/config.json"

logger = logging.getLogger(__name__)
# 拒絕啟動時行程馬上結束，Logs 頁看不到：一定要寫到容器 stdout（kubectl logs）。
if not any(isinstance(handler, _StdoutHandler) for handler in logger.handlers):
    logger.addHandler(_StdoutHandler())

#: 送給 ASGI server（uvicorn 以 ERROR 記錄）的固定訊息；不含路徑以外的任何資料。
REFUSAL = ("MCP 設定還原進行中（偵測到 .mcp-restore-in-progress 標記）：拒絕啟動——不提供 MCP、不讀寫設定、不啟動子行程。"
           "請保持服務縮到 0，完成或重新執行 python -m mcp_admin_core.restore_inplace；標記在還原確認完成後才會移除。")


class RestoreInProgress(RuntimeError):
    """設定目錄還有還原進行中標記：拒絕啟動。"""


def marker_path(config_file: str | os.PathLike[str] | None = None) -> Path:
    """標記的位置：設定檔（預設依 ``MCP_ADMIN_CONFIG``，與 ``ConfigStore`` 相同的解析）所在目錄。"""
    path = Path(config_file or os.environ.get("MCP_ADMIN_CONFIG", DEFAULT_CONFIG_PATH))
    return path.parent / MARKER_NAME


def restore_in_progress(config_file: str | os.PathLike[str] | None = None) -> bool:
    """標記存在、或無法確認不存在 → True（fail closed）。"""
    try:
        os.lstat(marker_path(config_file))
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


class RestoreInProgressGuard:
    """最外層的 ASGI lifespan 守門：標記存在就讓啟動失敗，內層的 lifespan（任何設定讀寫、子行程）都不執行。

    HTTP／WebSocket 請求原樣交給內層（標記存在時根本不會有：server 不會開始監聽）。
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "lifespan" or not restore_in_progress():
            await self.app(scope, receive, send)
            return
        message = await receive()
        if message.get("type") != "lifespan.startup":
            return
        logger.error("%s 標記：%s", REFUSAL, marker_path())
        # 先送 startup.failed（uvicorn 才會判定啟動失敗並結束），再拋例外（與 Starlette router 相同；測試用的
        # TestClient 也才會看到失敗）。內層 app 完全沒有收到 lifespan。
        await send({"type": "lifespan.startup.failed", "message": REFUSAL})
        raise RestoreInProgress(REFUSAL)
