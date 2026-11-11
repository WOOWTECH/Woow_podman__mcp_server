"""把連線層例外翻成維運看得懂的一句話（DATA-OMCP-007-2）。

httpx 的 ``ConnectError`` 涵蓋 DNS 查不到、對方拒絕連線、路由不通、TLS 失敗……若一律報成
``"Connection refused"``，網址打錯字（DNS）也會被當成「Odoo 沒起來／防火牆擋」去查。
httpx／httpcore 會以 ``raise ... from exc`` 保留原始 ``OSError``，沿 ``__cause__``／``__context__``
往下找即可分辨。
"""

from __future__ import annotations

import socket
import ssl


def _chain(exc: BaseException):
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen and len(seen) < 16:
        seen.add(id(cur))
        yield cur
        cur = cur.__cause__ or cur.__context__


def describe_connect_error(exc: BaseException, host: str = "") -> str:
    """回傳固定開頭的短句；拒絕連線維持原本的 ``"Connection refused"`` 字樣（既有告警／測試依賴）。"""
    causes = list(_chain(exc))
    target = f" ({host})" if host else ""
    for cause in causes:
        if isinstance(cause, socket.gaierror):
            return f"DNS lookup failed{target}: {cause.strerror or cause}"
    for cause in causes:
        if isinstance(cause, ConnectionRefusedError):
            return "Connection refused"
    for cause in causes:
        if isinstance(cause, (TimeoutError, socket.timeout)):
            return f"Connection timed out{target}"
    for cause in causes:
        if isinstance(cause, ssl.SSLError):
            return f"TLS handshake failed{target}: {cause}"
    last = causes[-1] if causes else exc
    detail = getattr(last, "strerror", None) or str(last) or type(last).__name__
    return f"Connection failed{target}: {detail}"
