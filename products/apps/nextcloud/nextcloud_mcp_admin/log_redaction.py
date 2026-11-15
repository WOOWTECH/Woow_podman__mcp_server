"""admin 行程的 log 衛生：path token 與 Nextcloud app password 都不進 log。

1. **path token**：MCP client 連 ``/private_{token}/mcp``，token 等於整個 MCP 端點的憑證。woow_paas_platform#1614
   起由共用層 ``mcp_admin_core.log_redaction`` 統一處理（``create_app()`` 安裝，所有 app 一致；也遮 Logs 頁
   EventSource 的 ``?token=<JWT>``），這裡的 ``install()`` 只是再確保一次並掛上額外 logger。

2. **app password**：正常路徑上 app password 不會出現在 log（vendored server 不記 header／body、錯誤訊息
   不帶憑證、pydantic 驗證錯誤隱藏輸入值；admin 只記 base_url）。這裡是第二道防線：admin 啟動與連線設定
   存檔時以 ``set_profile_secrets()`` 登記各身分的 app password（以及 HTTP Basic 的 base64 形式），之後**本行程任何
   logger** 產生的 record（含 McpProcessManager 轉錄的子行程輸出 ``[mcp-server] …``、Logs 頁、容器
   stdout）只要格式化後的訊息含有它，就整段改寫成 ``***``。做法是包一層 ``logging`` 的 record factory，
   所以不受 logger 階層、handler 安裝順序影響。

   **遮罩後 ``record.args`` 的形狀不變**（2026-11-14 實測 F-2）：uvicorn 的 ``AccessFormatter`` 不看 ``record.msg``，
   直接 ``client_addr, method, full_path, http_version, status_code = record.args`` 拆 5 個引數；舊版遮罩後把 args 設成
   ``None``，請求網址含已登記的 app password（``?api_token=<密碼>``、``?credential=<Basic base64>``）時那一行 access log
   整行消失、stderr 印出 ``--- Logging error ---`` 與 ``TypeError``。現在 tuple／dict 的 args 維持原本的容器、長度與
   key，字串元素逐一遮罩、數字等型別不動（``%d`` 要用），遮罩後的整句文字與舊版逐字相同；秘密剛好被拆在兩個引數之間、
   逐一遮罩遮不到時才退回整句遮罩（``uvicorn.access`` 的 5 元組仍維持形狀，字串引數全換成 ``***``）。

只處理本行程內的 log；PaaS 前面的 ingress／tunnel 若記錄完整 URL，仍要視為持有 path token。
"""
from __future__ import annotations

import base64
import logging
import threading
from collections.abc import Iterable, Mapping
from typing import Any

from mcp_admin_core import log_redaction as _core
from mcp_admin_core.log_redaction import PathTokenRedactionFilter, redact

__all__ = [
    "PathTokenRedactionFilter",
    "install",
    "redact",
    "redact_secrets",
    "registered_secret_count",
    "set_secrets",
    "set_profile_secrets",
    "remove_profile_secrets",
]

SECRET_MASK = "***"
MIN_SECRET_LEN = 6

_secrets: tuple[str, ...] = ()
_profile_secrets: dict[str, tuple[str, ...]] = {}
_lock = threading.Lock()
_factory_installed = False


def redact_secrets(text: str) -> str:
    for secret in _secrets:
        if secret in text:
            text = text.replace(secret, SECRET_MASK)
    return text


def set_secrets(app_password: str | None, username: str | None = None) -> None:
    """Compatibility entry point for the default profile's secrets."""
    set_profile_secrets("default", app_password, username)


def set_profile_secrets(profile_id: str, app_password: str | None, username: str | None = None) -> None:
    """Replace one profile's secrets while retaining every other profile's masks."""
    global _secrets
    values: list[str] = []
    pw = (app_password or "").strip()
    if len(pw) >= MIN_SECRET_LEN:
        values.append(pw)
        if username:
            basic = base64.b64encode(f"{username.strip()}:{pw}".encode()).decode()
            values.append(basic)
    with _lock:
        _profile_secrets[profile_id] = tuple(values)
        _secrets = tuple(sorted({secret for group in _profile_secrets.values() for secret in group}, key=len, reverse=True))
    if values:
        _install_record_factory()  # 有祕密要遮就確保 factory 已裝（不依賴 install() 的呼叫順序）


def remove_profile_secrets(profile_id: str) -> None:
    global _secrets
    with _lock:
        _profile_secrets.pop(profile_id, None)
        _secrets = tuple(sorted({secret for group in _profile_secrets.values() for secret in group}, key=len, reverse=True))


def registered_secret_count() -> int:
    return len(_secrets)


def _mask_value(value: Any) -> Any:
    """一個 args 元素的遮罩：字串直接遮；None／bool／int／float 不動（``%d``、``%f`` 需要原本的型別）；其他物件
    （例外、bytes…）只有 ``str()`` 真的含秘密時才換成遮好的字串，否則維持原物件。"""
    if isinstance(value, str):
        return redact_secrets(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    try:
        text = str(value)
    except Exception:  # noqa: BLE001 — 轉字串失敗就交給 logging 自己處理
        return value
    cleaned = redact_secrets(text)
    return value if cleaned == text else cleaned


def _mask_args(args: Any) -> Any:
    """tuple 維持 tuple、dict 維持 key；長度與各元素的位置不變。"""
    if isinstance(args, tuple):
        return tuple(_mask_value(item) for item in args)
    if isinstance(args, Mapping):
        return {key: _mask_value(item) for key, item in args.items()}
    return args


def _render(msg: Any, args: Any) -> str | None:
    """與 ``LogRecord.getMessage()`` 相同的格式化；失敗回 None。"""
    try:
        text = str(msg)
        return text % args if args else text
    except Exception:  # noqa: BLE001
        return None


def _is_access_shape(record: logging.LogRecord) -> bool:
    """uvicorn 的 AccessFormatter 只看 args（5 個引數），不看 msg：這種 record 的 args 形狀不可以改。"""
    return record.name == "uvicorn.access" and isinstance(record.args, tuple) and len(record.args) == 5


def _apply_masked_message(record: logging.LogRecord, cleaned: str) -> None:
    """把「整句遮罩後的文字」*cleaned* 套回 *record*，同時保留 ``record.args`` 的形狀。

    先逐一遮罩 msg 與 args 的元素，再確認格式化後的文字與整句遮罩（舊版的作法）逐字相同；多數情況（秘密完整地出現在
    某個引數或樣板裡）就是這樣。秘密被拆在相鄰引數之間、或是以非字串物件的形式出現而逐一遮罩遮不到時：
    ``uvicorn.access`` 的 5 元組把字串引數全換成 ``***``（形狀不變、不洩漏）；其他 record 沿用舊版的整句取代。
    """
    msg = redact_secrets(record.msg) if isinstance(record.msg, str) else redact_secrets(str(record.msg))
    args = _mask_args(record.args) if record.args else record.args
    if _render(msg, args) == cleaned:
        record.msg, record.args = msg, args
    elif _is_access_shape(record):
        record.msg = msg
        record.args = tuple(SECRET_MASK if isinstance(item, str) else item for item in record.args)
    else:
        record.msg, record.args = cleaned, ()


def _scrub_record(record: logging.LogRecord) -> None:
    if not _secrets:
        return
    try:
        message = record.getMessage()
    except Exception:  # noqa: BLE001 — 格式化失敗就交給 logging 自己處理
        return
    cleaned = redact_secrets(message)
    if cleaned != message:
        _apply_masked_message(record, cleaned)
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
