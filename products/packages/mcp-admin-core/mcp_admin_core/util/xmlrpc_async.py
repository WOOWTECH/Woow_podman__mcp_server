"""在 asyncio 事件迴圈裡安全地呼叫同步 XML-RPC（DATA-OMCP-007-1）。

``xmlrpc.client.ServerProxy`` 是同步 I/O 且預設沒有逾時：Odoo URL 指到黑洞位址
（例如 ``http://192.0.2.1:8069``）時，``/api/health`` 的 ``common.version()`` 會在事件迴圈上
卡到 TCP 逾時（約 2 分鐘），期間 ``/healthz`` 也回不出去 ⇒ kubelet liveness 連續失敗而殺掉
容器（服務中斷約 2.5 分鐘）。修法：

1. 每個 proxy 都帶 socket 逾時（``make_proxy``），所以工作緒不會無限期卡住；
2. 同步呼叫一律丟進工作緒（``run_blocking``），外層再加 ``asyncio.wait_for`` 的總期限，
   事件迴圈絕不被目標系統的網路狀況拖住。
"""

from __future__ import annotations

import asyncio
import xmlrpc.client
from typing import Any, Callable, TypeVar

T = TypeVar("T")

#: 單一 socket 操作（connect／讀）的逾時秒數。
DEFAULT_SOCKET_TIMEOUT = 8.0
#: 一次背景呼叫（可能含多個 RPC）的總期限秒數。
DEFAULT_DEADLINE = 15.0


class _TimeoutTransport(xmlrpc.client.Transport):
    def __init__(self, timeout: float, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._timeout = timeout

    def make_connection(self, host):  # type: ignore[override]
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn


class _TimeoutSafeTransport(xmlrpc.client.SafeTransport):
    def __init__(self, timeout: float, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._timeout = timeout

    def make_connection(self, host):  # type: ignore[override]
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn


def make_proxy(url: str, *, timeout: float = DEFAULT_SOCKET_TIMEOUT) -> xmlrpc.client.ServerProxy:
    """帶 socket 逾時的 ``ServerProxy``（http／https 皆適用）。"""
    transport: xmlrpc.client.Transport = (
        _TimeoutSafeTransport(timeout) if url.lower().startswith("https:") else _TimeoutTransport(timeout)
    )
    return xmlrpc.client.ServerProxy(url, transport=transport, allow_none=True)


async def run_blocking(fn: Callable[..., T], *args: Any, deadline: float = DEFAULT_DEADLINE) -> T:
    """把同步函式丟進工作緒執行，超過 ``deadline`` 秒就放棄（拋 ``asyncio.TimeoutError``）。

    放棄只是不再等結果；工作緒會在 socket 逾時後自行結束，不會無限累積。
    """
    return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout=deadline)
