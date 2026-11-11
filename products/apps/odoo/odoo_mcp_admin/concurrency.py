"""讓唯讀工具不再互相卡住，寫入／核准類維持序列（商用驗收 DATA-ODOO-011-1；MCP SDK 2.x 見 #78）。

## 現象
MCP 240 在 10 個 session 並發時延遲隨並發線性上升：tools/list＋search_records p95 約 3.8 s、
p99 8–11 s（單一 session 的 search_records p50 0.94 s；Odoo 與 MCP pod 的 CPU 都沒滿）。

## 根因
上游 ``odoo-mcp`` 的工具都是**同步** ``def``。mcp 1.x 的 FastMCP 對同步工具直接在事件迴圈上呼叫
（``FuncMetadata.call_fn_with_arg_validation``：``fn(**args)``）⇒ 每一次 Odoo XML-RPC 都把整個
行程的事件迴圈卡住，所有 session 排成一條隊。單一請求越慢（冷啟動 2–3 s）隊伍越長。

## mcp 2.x 的變化（#78）
mcp 2.x 的 ``MCPServer`` 反過來：**所有**同步工具都丟進 anyio 預設的工作緒池（上限 40 條）並行跑。
不處理的話有兩個靜默後果（評估報告 §0.2-C 實測）：

- 寫入類（``execute_method``、``chatter_post``）不再序列：20 個並發 execute_method，假 Odoo 端的最大
  並行 RPC 由 1 變 20，而且都在工作緒上用共用的 XML-RPC proxy（見下方 2.）；
- ``ToolAnnotations`` 的欄位改成 snake_case（``read_only_hint``），舊的 ``readOnlyHint`` 讀到 None，
  一個工具都不包 ⇒ ``ODOO_MCP_READ_THREADS`` 失效、唯讀工具退回 anyio 預設的 40 條。
  注意：同一行程 import 過 fastmcp 4 時，它會替 SDK 型別補回 camelCase 屬性（並發警告），所以只在
  「整個 workspace 一個 venv」的測試裡看得到 ``readOnlyHint``，正式映像（只裝 odoo app 的相依）看不到；
  ``tests/test_odoo_concurrency_sdk2.py`` 以不 import fastmcp 的子行程守住這一點。

## 修法
依工具的唯讀標註（上游自己標的 ``read_only_hint``）把同步工具分兩類：

1. 唯讀且同步：把 ``tool.fn`` 包成 async，內部丟進工作緒（``anyio.to_thread``，容量
   ``ODOO_MCP_READ_THREADS``，預設 8）。
2. 非唯讀且同步（寫入類，目前 ``execute_method``、``chatter_post``）：包成 async、**直接在事件迴圈上
   呼叫**，與 mcp 1.x 完全相同——寫入類、核准流程彼此序列（``execute_approved_write`` 上游本來就是 async，
   也在事件迴圈上），核准 token 的消耗、replay 防護等共用狀態不受並行影響。
3. ``OdooClient`` 的 ``_common`` 與 ``_models`` 是共用的 ``xmlrpc.client.ServerProxy``，同一個
   ``Transport`` 只有一條持久連線，不能被多個執行緒同時用。改成 descriptor：事件迴圈上的呼叫
   （寫入類）照舊用共用的；工作緒第一次用時建自己的 proxy（同樣的 timeout／SSL 設定），之後重用。
   JSON-2 transport 每次呼叫自己開連線，本來就安全。

已是 async 的工具（上游自己寫成 async 的，以及已經包過的）一律不動。沒有改變任何工具的回傳內容、錯誤處理
或權限。``install()`` 要在上游 ``odoo_mcp.server`` 已 import（工具已註冊）之後呼叫，見 ``launcher.py``。
"""

from __future__ import annotations

import functools
import logging
import os
import threading
import weakref
from typing import Any

logger = logging.getLogger(__name__)

_tls = threading.local()
_limiter: Any = None

#: 包過的工具函式上的標記：值為 ``"thread"``（唯讀，工作緒）或 ``"event_loop"``（寫入類，事件迴圈）。
LANE_ATTR = "_woow_odoo_lane"


def _worker_threads() -> int:
    try:
        return max(1, min(64, int(os.environ.get("ODOO_MCP_READ_THREADS", "8"))))
    except ValueError:
        return 8


def _get_limiter():
    """anyio 4 的 CapacityLimiter 要在事件迴圈裡建，所以第一次使用時才建。"""
    global _limiter  # noqa: PLW0603
    if _limiter is None:
        import anyio

        _limiter = anyio.CapacityLimiter(_worker_threads())
    return _limiter


def is_read_only(annotations: Any) -> bool:
    """工具是否標了唯讀（mcp 2.x 的 ``ToolAnnotations.read_only_hint``）。

    不要讀 camelCase 的 ``readOnlyHint``：那是 fastmcp 4 被 import 過的行程才有的相容橋接，正式映像裡會靜默落空
    （見模組說明）。
    """
    return annotations is not None and getattr(annotations, "read_only_hint", None) is True


class _ThreadAwareProxy:
    """``OdooClient._common`` / ``_models`` 的替身：工作緒拿自己的 ServerProxy，其餘照舊。"""

    def __init__(self, name: str, endpoint: str) -> None:
        # 指派到既有 class 時 __set_name__ 不會被呼叫，所以名字由呼叫端明確給。
        self.name = name
        self.key = f"_shared_{name}"
        self.endpoint = endpoint

    def __get__(self, obj: Any, objtype: type | None = None) -> Any:
        if obj is None:
            return self
        shared = obj.__dict__.get(self.key)
        if shared is None or not getattr(_tls, "worker", False):
            return shared
        cache = getattr(_tls, "proxies", None)
        if cache is None:
            cache = _tls.proxies = weakref.WeakKeyDictionary()
        per_client = cache.setdefault(obj, {})
        proxy = per_client.get(self.name)
        if proxy is None:
            import xmlrpc.client

            from odoo_mcp.odoo_client import RedirectTransport

            transport = RedirectTransport(
                timeout=obj.timeout,
                use_https=obj.url.startswith("https://"),
                verify_ssl=obj.verify_ssl,
            )
            proxy = per_client[self.name] = xmlrpc.client.ServerProxy(
                f"{obj.url}/xmlrpc/2/{self.endpoint}", transport=transport, allow_none=True
            )
        return proxy

    def __set__(self, obj: Any, value: Any) -> None:
        obj.__dict__[self.key] = value


def _in_thread(fn):
    @functools.wraps(fn)
    async def run(*args, **kwargs):
        import anyio

        def call():
            _tls.worker = True
            try:
                return fn(*args, **kwargs)
            finally:
                _tls.worker = False

        return await anyio.to_thread.run_sync(call, limiter=_get_limiter())

    setattr(run, LANE_ATTR, "thread")
    return run


def _on_event_loop(fn):
    """同步的寫入類工具：在事件迴圈上直接呼叫（mcp 1.x 的語意），不讓 mcp 2.x 丟進工作緒並行。"""

    @functools.wraps(fn)
    async def run(*args, **kwargs):
        return fn(*args, **kwargs)

    setattr(run, LANE_ATTR, "event_loop")
    return run


def tools_in_lane(mcp: Any, lane: str) -> list[str]:
    """``install()`` 之後，被放到 *lane*（``"thread"``／``"event_loop"``）的工具名（排序）。"""
    return sorted(t.name for t in mcp._tool_manager.list_tools() if getattr(t.fn, LANE_ATTR, None) == lane)


def install(mcp: Any, client_cls: Any) -> list[str]:
    """唯讀同步工具改在工作緒執行、寫入類同步工具釘回事件迴圈；回傳改到工作緒的工具名（排序）。

    Idempotent：已經是 async 的（含已包過的）不會再包。
    """
    client_cls._common = _ThreadAwareProxy("_common", "common")
    client_cls._models = _ThreadAwareProxy("_models", "object")
    patched = []
    serialized = []
    for tool in mcp._tool_manager.list_tools():
        if tool.is_async:
            continue
        if is_read_only(tool.annotations):
            tool.fn = _in_thread(tool.fn)
            patched.append(tool.name)
        else:
            tool.fn = _on_event_loop(tool.fn)
            serialized.append(tool.name)
        tool.is_async = True
    logger.info(
        "odoo-mcp tools: %d read-only tools run in worker threads (ODOO_MCP_READ_THREADS=%d); "
        "%d write tools stay serialized on the event loop: %s",
        len(patched), _worker_threads(), len(serialized), ", ".join(sorted(serialized)) or "-",
    )
    return sorted(patched)
