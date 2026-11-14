"""Fence initialize headers at the outer application ASGI boundary."""
import asyncio

from .mcp_access import AccessChanged

#: The outer ``send`` was blocked (backpressure) when the token changed and the headers were cancelled; the gateway
#: then tries to deliver its own 403 for at most this long. A client that reads nothing cannot take the 403 either:
#: give up quietly (the connection is closed) instead of waiting behind it.
REJECTION_SEND_TIMEOUT = 0.25


class _Rejected(Exception):
    pass


#: Headers a rejection keeps from the child's response: boundary CORS/security headers only; the child's arbitrary
#: headers and session ID do not belong on a rejection.
_SAFE_HEADERS = {b'access-control-allow-origin', b'access-control-allow-credentials', b'vary',
                 b'strict-transport-security', b'content-security-policy', b'x-content-type-options',
                 b'x-frame-options', b'referrer-policy', b'permissions-policy'}


class InitializeFenceMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)

        async def reject(message, status):
            body = (b'{"detail":"MCP access changed; reconnect"}' if status == 403 else
                    b'{"detail":"MCP child is not ready; reconnect after checking its status"}')
            headers = [(key, value) for key, value in message.get('headers', [])
                       if key.lower() in _SAFE_HEADERS]
            headers.extend([(b'content-type', b'application/json'), (b'content-length', str(len(body)).encode()),
                            (b'cache-control', b'no-store')])
            await send({'type': 'http.response.start', 'status': status, 'headers': headers})
            await send({'type': 'http.response.body', 'body': body, 'more_body': False})

        async def guarded_send(message):
            guard = scope.get('state', {}).get('_mcp_initialize_guard')
            if message['type'] != 'http.response.start' or guard is None:
                return await send(message)
            pm, generation, lease = guard
            try:
                lease.check()
                status = 503
            except AccessChanged:
                status = 403
            reserved = status == 503 and pm.begin_initialize_dispatch(generation)
            if not reserved:
                await reject(message, status)
                raise _Rejected
            accepted = False

            async def forward():
                nonlocal accepted
                await send(message)
                accepted = True

            try:
                # A revoked token cancels even a backpressured outer send. A
                # header already accepted by the transport cannot be retracted.
                await lease.run(forward())
            except AccessChanged:
                if accepted:
                    # The token changed in the very instant the transport took the headers: the status line is out
                    # and cannot be replaced. Not an error: the response's own lease handling (mcp_access) ends the
                    # body with the gateway's JSON-RPC error (-32005) and none of the child's bytes.
                    return
                # The send was cancelled before the transport took anything (the client was not reading: backpressure):
                # the same clean 403 as a token that had already changed before the headers. Raising it to the server
                # would be an "Exception in ASGI application" with a traceback and a 500.
                try:
                    async with asyncio.timeout(REJECTION_SEND_TIMEOUT):
                        await reject(message, 403)
                except TimeoutError:
                    pass  # still backpressured: nothing more can be delivered to this client
                raise _Rejected
            finally:
                pm.end_initialize_dispatch()

        try:
            await self.app(scope, receive, guarded_send)
        except _Rejected:
            pass
        except BaseExceptionGroup as exc:
            rejected, remainder = exc.split(_Rejected)
            if remainder is not None:
                raise remainder
            if rejected is None:
                raise
