"""admin 行程的 log 衛生：path token 與 Nextcloud app password 都不進 log。

1. **path token**：MCP client 連 ``/private_{token}/mcp``，token 等於整個 MCP 端點的憑證。woow_paas_platform#1614
   起由共用層 ``mcp_admin_core.log_redaction`` 統一處理（``create_app()`` 安裝，所有 app 一致；也遮 Logs 頁
   EventSource 的 ``?token=<JWT>``），這裡的 ``install()`` 只是再確保一次並掛上額外 logger。

2. **app password**：正常路徑上 app password 不會出現在 log（vendored server 不記 header／body、錯誤訊息
   不帶憑證、pydantic 驗證錯誤隱藏輸入值；admin 只記 base_url）。這裡是第二道防線：admin 啟動與連線設定
   存檔時以 ``set_secrets()`` 登記目前的 app password（以及 HTTP Basic 的 base64 形式），之後**本行程任何
   logger** 產生的 record（含 McpProcessManager 轉錄的子行程輸出 ``[mcp-server] …``、Logs 頁、容器
   stdout）只要格式化後的訊息含有它，就整段改寫成 ``***``。做法是包一層 ``logging`` 的 record factory，
   所以不受 logger 階層、handler 安裝順序影響。

只處理本行程內的 log；PaaS 前面的 ingress／tunnel 若記錄完整 URL，仍要視為持有 path token。
"""
from __future__ import annotations

import base64
import logging
import threading
from collections.abc import Iterable

from mcp_admin_core import log_redaction as _core
from mcp_admin_core.log_redaction import PathTokenRedactionFilter, redact

__all__ = [
    "PathTokenRedactionFilter",
    "install",
    "redact",
    "redact_secrets",
    "registered_secret_count",
    "set_secrets",
]

SECRET_MASK = "***"
MIN_SECRET_LEN = 6

_secrets: tuple[str, ...] = ()
_lock = threading.Lock()
_factory_installed = False


def redact_secrets(text: str) -> str:
    for secret in _secrets:
        if secret in text:
            text = text.replace(secret, SECRET_MASK)
    return text


def set_secrets(app_password: str | None, username: str | None = None) -> None:
    """登記要遮的祕密（每次覆蓋前一次登記）：app password 本身，有 username 時再加 HTTP Basic 的
    base64(``username:password``)。空值＝清空；太短（< 6 字元）的值不登記，避免把一般字串也遮掉。"""
    global _secrets
    values: list[str] = []
    pw = (app_password or "").strip()
    if len(pw) >= MIN_SECRET_LEN:
        values.append(pw)
        if username:
            basic = base64.b64encode(f"{username.strip()}:{pw}".encode()).decode()
            values.append(basic)
    with _lock:
        _secrets = tuple(values)
    if values:
        _install_record_factory()  # 有祕密要遮就確保 factory 已裝（不依賴 install() 的呼叫順序）


def registered_secret_count() -> int:
    return len(_secrets)


def _scrub_record(record: logging.LogRecord) -> None:
    if not _secrets:
        return
    try:
        message = record.getMessage()
    except Exception:  # noqa: BLE001 — 格式化失敗就交給 logging 自己處理
        return
    cleaned = redact_secrets(message)
    if cleaned != message:
        record.msg = cleaned
        record.args = None
    if record.exc_info and not record.exc_text:
        # Formatter 只在 exc_text 為空時才自己格式化 traceback；先格式化並遮好，handler 就會沿用。
        try:
            text = logging.Formatter().formatException(record.exc_info)
        except Exception:  # noqa: BLE001
            text = ""
        if text and redact_secrets(text) != text:
            record.exc_text = redact_secrets(text)
    elif record.exc_text:
        record.exc_text = redact_secrets(record.exc_text)
    if record.stack_info:
        record.stack_info = redact_secrets(record.stack_info)


def _install_record_factory() -> None:
    global _factory_installed
    with _lock:
        if _factory_installed:
            return
        previous = logging.getLogRecordFactory()

        def factory(*args, **kwargs) -> logging.LogRecord:
            record = previous(*args, **kwargs)
            _scrub_record(record)
            return record

        factory._nextcloud_secret_scrubber = True  # type: ignore[attr-defined]
        logging.setLogRecordFactory(factory)
        _factory_installed = True


def install(extra_loggers: Iterable[str] = ()) -> None:
    """裝共用層的 URL 憑證遮罩（path token／?token=，idempotent）並安裝 app password 遮罩的 record factory。"""
    _core.install(extra_loggers)
    _install_record_factory()
