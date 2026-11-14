"""Fence initialize headers at the outer application ASGI boundary."""
from .mcp_access import AccessChanged


class _Rejected(Exception):
    pass


class InitializeFenceMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)

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
                body = (b'{"detail":"MCP access changed; reconnect"}' if status == 403 else
                        b'{"detail":"MCP child is not ready; reconnect after checking its status"}')
                # Preserve boundary CORS/security headers only; the child's
                # arbitrary headers and session ID do not belong on a rejection.
                safe_headers = {b'access-control-allow-origin', b'access-control-allow-credentials', b'vary',
                                b'strict-transport-security', b'content-security-policy', b'x-content-type-options',
                                b'x-frame-options', b'referrer-policy', b'permissions-policy'}
                headers = [(key, value) for key, value in message.get('headers', [])
                           if key.lower() in safe_headers]
                headers.extend([(b'content-type', b'application/json'), (b'content-length', str(len(body)).encode()),
                                (b'cache-control', b'no-store')])
                await send({'type': 'http.response.start', 'status': status, 'headers': headers})
                await send({'type': 'http.response.body', 'body': body, 'more_body': False})
                raise _Rejected
            try:
                # A revoked token cancels even a backpressured outer send. A
                # header already accepted by the transport cannot be retracted.
                await lease.run(send(message))
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
