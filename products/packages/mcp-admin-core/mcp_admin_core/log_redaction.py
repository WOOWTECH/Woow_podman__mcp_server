"""admin 行程的 log 衛生：URL 裡的憑證不進 log（woow_paas_platform#1614）。

兩種憑證會跟著請求路徑一起被印出來：

1. **MCP path token**：MCP client 連 ``/private_{token}/mcp``，token 等於整個 MCP 端點的憑證。
2. **管理頁 JWT**：Logs 頁用 EventSource 連 ``/api/logs/stream?token=<JWT>``（EventSource 不能帶 header，
   見 ``auth/middleware.py``），JWT 等於管理頁登入。

uvicorn access log 會印完整請求路徑（含 query string）；``routers/logs.py`` 又把 ``uvicorn.access``、
``httpx``、``mcp_admin_core`` 收進 Logs 頁 ring buffer，容器 stdout（``kubectl logs``）也看得到。這裡在
record 建立當下就把 ``/private_<token>`` 改寫成 ``/private_***``、``?token=<值>`` 改寫成 ``?token=***``，
之後不論哪個 handler（stdout、ring buffer）拿到的都是遮好的內容。

做法（兩層，皆 idempotent）：

- **record factory**（主要）：包一層 ``logging`` 的 record factory，本行程**任何 logger** 產生的 record
  都先遮——含 uvicorn.error 的例外 traceback、McpProcessManager 轉錄的子行程輸出 ``[mcp-server] …``、
  各 app 自己的 logger。只改字串型的 msg／args 元素（args 維持原本的 tuple 長度，uvicorn 的
  ``AccessFormatter`` 靠它拆出 ``client_addr, method, full_path, …``）。
- **logger filter**（第二道）：在 ``uvicorn.access``／``uvicorn.error``／``httpx`` 上掛
  :class:`PathTokenRedactionFilter`，萬一之後有人換掉 record factory 也還擋得住最主要的來源。

``create_app()`` 會呼叫 :func:`install`，所以每個 app 都自動套用；各 app 不需要（也不該）自己再寫一份。

只處理本行程內的 log；PaaS 前面的 ingress／tunnel 若記錄完整 URL，仍要視為持有憑證。
"""
from __future__ import annotations

import logging
import re
import threading
from collections.abc import Iterable, Mapping
from typing import Any

MASK = "***"

# /private_<token>：token 到下一個 / 、空白、? 、# 或引號為止（與 proxy 路由 /private_{token} 對齊）。
_PATH_TOKEN_RE = re.compile(r"(/private_)[^/\s?#\"']+")
# ?token=<值>／&token=<值>（管理頁 EventSource 的 JWT）；access_token 一併處理。
_QUERY_TOKEN_RE = re.compile(r"([?&](?:access_)?token=)[^&#\s\"']+", re.IGNORECASE)

# 會帶請求路徑的 logger：uvicorn access／error（例外訊息含 path）、httpx（admin 自己的探測與 proxy 上游）。
PATH_LOGGERS = ("uvicorn.access", "uvicorn.error", "httpx")

_lock = threading.Lock()
_factory_installed = False


def _has_candidate(text: str) -> bool:
    return "/private_" in text or "token=" in text.lower()


def redact(text: str) -> str:
    """把字串裡的 ``/private_<token>`` 與 ``?token=<值>`` 遮成 ``***``；沒有就原樣回傳。"""
    if not text or not _has_candidate(text):
        return text
    text = _PATH_TOKEN_RE.sub(r"\1" + MASK, text)
    return _QUERY_TOKEN_RE.sub(r"\1" + MASK, text)


def _redact_value(value: Any) -> Any:
    """str 直接遮；非基本型別（URL 物件、例外等）只有字串形式真的含憑證時才換成遮好的字串。"""
    if isinstance(value, str):
        return redact(value)
    if value is None or isinstance(value, (bool, int, float, bytes)):
        return value
    try:
        text = str(value)
    except Exception:  # noqa: BLE001 — 轉字串失敗就交給 logging 自己處理
        return value
    cleaned = redact(text)
    return value if cleaned == text else cleaned


def _redact_args(args: Any) -> Any:
    if isinstance(args, tuple):
        return tuple(_redact_value(a) for a in args)
    if isinstance(args, Mapping):
        return {k: _redact_value(v) for k, v in args.items()}
    return args


def scrub_record(record: logging.LogRecord) -> None:
    """就地遮 record 的 msg／args／例外文字／stack（不可讓 logging 爆掉）。"""
    try:
        record.msg = _redact_value(record.msg)
        if record.args:
            record.args = _redact_args(record.args)
        if record.exc_info and not record.exc_text:
            # Formatter 只在 exc_text 為空時才自己格式化 traceback；含憑證就先格式化並遮好，handler 會沿用。
            text = logging.Formatter().formatException(record.exc_info)
            cleaned = redact(text)
            if cleaned != text:
                record.exc_text = cleaned
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
    except Exception:  # noqa: BLE001
        pass


class PathTokenRedactionFilter(logging.Filter):
    """logger 層的第二道防線（record factory 被換掉時仍遮 uvicorn／httpx 的 log）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        scrub_record(record)
        return True


def _install_record_factory() -> None:
    global _factory_installed
    with _lock:
        if _factory_installed:
            return
        previous = logging.getLogRecordFactory()

        def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
            record = previous(*args, **kwargs)
            scrub_record(record)
            return record

        factory._mcp_admin_url_token_scrubber = True  # type: ignore[attr-defined]
        logging.setLogRecordFactory(factory)
        _factory_installed = True


def install(extra_loggers: Iterable[str] = ()) -> None:
    """安裝 record factory，並在會帶路徑的 logger 上掛 filter（皆 idempotent）。"""
    _install_record_factory()
    for name in (*PATH_LOGGERS, *extra_loggers):
        target = logging.getLogger(name)
        if not any(isinstance(f, PathTokenRedactionFilter) for f in target.filters):
            target.addFilter(PathTokenRedactionFilter())
