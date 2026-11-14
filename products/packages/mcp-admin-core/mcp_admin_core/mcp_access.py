"""A request's shared-token lease, invalidated after a persisted auth change."""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import secrets
from typing import Callable

from starlette.requests import ClientDisconnect
from starlette.responses import Response, StreamingResponse

logger = logging.getLogger(__name__)

#: 閘道自己回答「token 已換掉」時的內容（與 proxy 在轉發之前發現時的 403 相同）。
ACCESS_CHANGED_DETAIL = "MCP access changed; reconnect using the current shared token"
#: token 在回應送完之前換掉：閘道最多花這麼久把自己的結尾（403、JSON-RPC 錯誤）交給用戶端；用戶端不讀（背壓）就放棄。
INTERRUPTED_FINISH_TIMEOUT = 2.0


async def _finish_cleanup(awaitable):
    """Join cleanup even when a disconnected caller is cancelled repeatedly."""
    cleaner = asyncio.create_task(awaitable)
    cancelled = False
    while True:
        try:
            await asyncio.shield(cleaner)
            break
        except asyncio.CancelledError:
            if cleaner.cancelled():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError


class AccessChanged(Exception):
    """Authentication changed while a request or stream was in flight."""


class AccessLease:
    def __init__(self, changed: asyncio.Event):
        self.changed = changed

    @classmethod
    async def acquire(cls, store, supplied: str):
        (expected, revoked, _generation), changed = await store.get_mcp_access()
        if revoked or not expected or not secrets.compare_digest(expected.encode(), supplied.encode()):
            raise AccessChanged
        return cls(changed)

    def check(self):
        if self.changed.is_set():
            raise AccessChanged

    async def run(self, awaitable):
        """Cancel only this request's pending I/O; never claim backend rollback."""
        if self.changed.is_set():
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            raise AccessChanged
        operation = asyncio.ensure_future(awaitable)
        revoked = asyncio.create_task(self.changed.wait())
        async def stop_tasks():
            for task in (operation, revoked):
                if not task.done():
                    task.cancel()
            await asyncio.gather(operation, revoked, return_exceptions=True)

        try:
            await asyncio.wait((operation, revoked), return_when=asyncio.FIRST_COMPLETED)
            self.check()
            result = operation.result()
            # Returning is the ownership transfer. Finish every awaited cleanup
            # first, so cancellation here cannot strand an unclaimed response.
            await _finish_cleanup(stop_tasks())
            self.check()
        except BaseException:
            async def cleanup():
                await stop_tasks()
                # Revocation/caller cancellation can race with response headers.
                # An unclaimed response still owns a connection that must close.
                if not operation.cancelled() and operation.exception() is None:
                    close = getattr(operation.result(), 'aclose', None)
                    if close is not None:
                        await close()
            await _finish_cleanup(cleanup())
            raise
        return result


def _closed(response) -> None:
    """Run the response's on_close callback once (request bookkeeping, e.g. session_reclaim)."""
    callback, response.on_close = getattr(response, "on_close", None), None
    if callback is not None:
        callback()


class _Delivery:
    """回應已經送到哪裡：標頭交出去了沒、整個回應結束了沒（``more_body`` 為 False 的本體）。"""

    __slots__ = ("started", "completed")

    def __init__(self) -> None:
        self.started = False
        self.completed = False

    def wrap(self, send):
        async def tracked(message):
            await send(message)
            if message["type"] == "http.response.start":
                self.started = True
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                self.completed = True
        return tracked


async def _finish_interrupted(send, delivery: _Delivery, tail: Callable[[], bytes] | None) -> None:
    """token 在回應送完之前被輪替或撤銷：子行程的 I/O 已經停掉，這裡只送閘道自己的結尾，不再送任何子行程的位元組。

    - 標頭還沒送：乾淨的 403（與轉發之前就發現時的內容相同）。
    - 標頭已送：``tail()`` 給的結尾（SSE 是每個還沒得到回應的請求一則 JSON-RPC 錯誤事件；JSON 本體一個位元組都還沒送時
      是 JSON-RPC 錯誤本體；其他情況沒有），然後正常結束回應。
    - 用戶端不讀（背壓）或已斷線：最多等 :data:`INTERRUPTED_FINISH_TIMEOUT` 秒就放棄、直接返回。

    一律不拋例外：以前拋 ``ClientDisconnect`` 到 uvicorn，每筆記成「Exception in ASGI application」加約 89 行
    traceback，標頭還沒送時還變成 HTTP 500（2026-11-13 多帳號實測 iso S12／S12b，P3-a）。
    """
    if delivery.completed:
        return
    logger.info("MCP token 在回應送完之前被輪替或撤銷：已停止轉發子行程的回應（%s）",
                "標頭已送出，補上閘道的結尾後結束" if delivery.started else "回 403")
    try:
        async with asyncio.timeout(INTERRUPTED_FINISH_TIMEOUT):
            if not delivery.started:
                body = json.dumps({"detail": ACCESS_CHANGED_DETAIL}).encode()
                await send({"type": "http.response.start", "status": 403, "headers": [
                    (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                    (b"cache-control", b"no-store")]})
                await send({"type": "http.response.body", "body": body, "more_body": False})
                return
            extra = tail() if tail is not None else b""
            if extra:
                await send({"type": "http.response.body", "body": extra, "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
    except (TimeoutError, OSError, ClientDisconnect):
        pass


class LeasedResponse(Response):
    """Keep buffered response delivery under the same credential lease.

    The body is sent without ``Content-Length`` (chunked), so a revocation that lands after the headers can still
    end the response with the gateway's JSON-RPC error instead of a body shorter than its declared length.
    """
    def __init__(self, *args, lease, on_close=None, interrupted: Callable[[], bytes] | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.lease = lease
        self.on_close = on_close
        self.interrupted = interrupted
        self.raw_headers = [(key, value) for key, value in self.raw_headers if key != b"content-length"]

    async def __call__(self, scope, receive, send):
        delivery = _Delivery()
        try:
            try:
                await self.lease.run(super().__call__(scope, receive, delivery.wrap(send)))
                return
            except AccessChanged:
                pass
            await _finish_interrupted(send, delivery, self.interrupted)
        finally:
            _closed(self)


class LeasedStreamingResponse(StreamingResponse):
    """Cancel downstream sends too, including a client blocked by backpressure.

    On a revocation the upstream response is closed first; then the client gets the gateway's own ending
    (:func:`_finish_interrupted`), never another byte from the child.
    """
    def __init__(self, *args, lease, upstream, on_close=None, interrupted: Callable[[], bytes] | None = None,
                 **kwargs):
        super().__init__(*args, **kwargs)
        self.lease = lease
        self.upstream = upstream
        self.on_close = on_close
        self.interrupted = interrupted

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # stream_response may be cancelled before it starts (client gone at once).
            _closed(self)

    async def _close_upstream(self):
        try:
            await self.body_iterator.aclose()
        finally:
            await self.upstream.aclose()

    async def stream_response(self, send):
        delivery = _Delivery()
        try:
            try:
                await self.lease.run(super().stream_response(delivery.wrap(send)))
                return
            except AccessChanged:
                pass
            # Stop the child's I/O before writing anything else (the stream may be parked behind backpressure).
            await _finish_cleanup(self._close_upstream())
            await _finish_interrupted(send, delivery, self.interrupted)
        finally:
            try:
                await _finish_cleanup(self._close_upstream())
            finally:
                _closed(self)
