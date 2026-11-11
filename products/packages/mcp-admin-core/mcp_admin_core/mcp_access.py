"""A request's shared-token lease, invalidated after a persisted auth change."""
from __future__ import annotations

import asyncio
import inspect
import secrets

from starlette.requests import ClientDisconnect
from starlette.responses import Response, StreamingResponse


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


class LeasedResponse(Response):
    """Keep buffered response delivery under the same credential lease."""
    def __init__(self, *args, lease, on_close=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.lease = lease
        self.on_close = on_close

    async def __call__(self, scope, receive, send):
        try:
            await self.lease.run(super().__call__(scope, receive, send))
        except AccessChanged:
            raise ClientDisconnect from None
        finally:
            _closed(self)


class LeasedStreamingResponse(StreamingResponse):
    """Cancel downstream sends too, including a client blocked by backpressure."""
    def __init__(self, *args, lease, upstream, on_close=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.lease = lease
        self.upstream = upstream
        self.on_close = on_close

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # stream_response may be cancelled before it starts (client gone at once).
            _closed(self)

    async def stream_response(self, send):
        try:
            await self.lease.run(super().stream_response(send))
        except AccessChanged:
            # Headers may already be sent. Abort the connection; no further
            # backend bytes or misleading replacement HTTP response can follow.
            raise ClientDisconnect from None
        finally:
            async def close():
                try:
                    await self.body_iterator.aclose()
                finally:
                    await self.upstream.aclose()
            try:
                await _finish_cleanup(close())
            finally:
                _closed(self)
