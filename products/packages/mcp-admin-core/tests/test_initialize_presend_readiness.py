"""Initialize should wait before its first HTTP dispatch, under the token lease."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from mcp_admin_core.process import McpProcessManager

TOKEN = 'synthetic-presend-only'
INIT = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
    'protocolVersion': '2025-03-26', 'capabilities': {},
    'clientInfo': {'name': 'presend-test', 'version': '1'},
}}


@pytest.mark.asyncio
async def test_initialize_waits_before_first_dispatch(make_app, monkeypatch):
    from mcp_admin_core import proxy

    ready = asyncio.Event()
    sent = []
    pm = McpProcessManager()
    pm._process = SimpleNamespace(pid=4242, returncode=None)
    pm._running = True
    pm._attempted_revision = 'synthetic-revision'
    pm._verifying_configuration = True
    # Synthetic stand-in for the probe's first OS listener check (MockTransport: no socket to inspect).
    pm._peer_verified = (pm._initialize_generation_changed, 59999)

    async def verification():
        await ready.wait()
        pm._applied_revision = pm._attempted_revision
        pm._verifying_configuration = False

    pm._configuration_task = asyncio.create_task(verification())

    async def upstream(request):
        sent.append(request.method)
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': 1, 'result': {}})

    monkeypatch.setattr(proxy, 'get_process_manager', lambda: pm)
    monkeypatch.setattr(proxy, '_get_client', lambda *args: httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    app = make_app({'mcp_auth_token': TOKEN, 'mcp_server': {'command': '', 'args': [], 'port': 59999, 'env': {}}}).app
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
            request = asyncio.create_task(c.post('/private_' + TOKEN + '/mcp', json=INIT))
            await asyncio.sleep(.05)
            assert not request.done() and sent == []
            ready.set()
            result = await asyncio.wait_for(request, 2)
            assert result.status_code == 200 and sent == ['POST']
    finally:
        ready.set()
        await pm._configuration_task


def fixture_gateway(make_app, monkeypatch, handler):
    from mcp_admin_core import proxy
    ready = asyncio.Event()
    pm = McpProcessManager()
    pm._process = SimpleNamespace(pid=4242, returncode=None)
    pm._running = True
    pm._attempted_revision = 'synthetic-revision'
    pm._verifying_configuration = True
    # Synthetic stand-in for the probe's first OS listener check (MockTransport: no socket to inspect).
    pm._peer_verified = (pm._initialize_generation_changed, 59999)

    async def verify():
        await ready.wait()
        pm._applied_revision = pm._attempted_revision
        pm._verifying_configuration = False

    probe = pm._configuration_task = asyncio.create_task(verify())
    monkeypatch.setattr(proxy, 'get_process_manager', lambda: pm)
    monkeypatch.setattr(proxy, '_get_client', lambda *args: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    app = make_app({'mcp_auth_token': TOKEN, 'mcp_server': {'command': '', 'args': [], 'port': 59999, 'env': {}}}).app
    return app, pm, ready, probe


@pytest.mark.asyncio
async def test_revocation_while_waiting_stops_request_without_cancelling_shared_probe(make_app, monkeypatch):
    from mcp_admin_core.config.store import get_config_store
    sent = []
    async def upstream(request):
        sent.append(1)
        return httpx.Response(200)
    app, pm, ready, probe = fixture_gateway(make_app, monkeypatch, upstream)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
            request = asyncio.create_task(c.post('/private_' + TOKEN + '/mcp', json=INIT))
            await asyncio.sleep(.05)
            await asyncio.wait_for(get_config_store().change_mcp_token(None, action='revoke'), .5)
            response = await asyncio.wait_for(request, 1)
            assert response.status_code == 403 and not sent and not probe.done()
    finally:
        ready.set()
        await probe


@pytest.mark.asyncio
async def test_client_cancel_does_not_cancel_shared_verification(make_app, monkeypatch):
    sent = []
    async def upstream(request):
        sent.append(1)
        return httpx.Response(200)
    app, pm, ready, probe = fixture_gateway(make_app, monkeypatch, upstream)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
            request = asyncio.create_task(c.post('/private_' + TOKEN + '/mcp', json=INIT))
            await asyncio.sleep(.05)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert not probe.done() and not sent
    finally:
        ready.set()
        await probe


@pytest.mark.asyncio
async def test_readiness_timeout_is_bounded_and_does_not_dispatch(make_app, monkeypatch):
    from mcp_admin_core import proxy
    sent = []
    async def upstream(request):
        sent.append(1)
        return httpx.Response(200)
    app, pm, ready, probe = fixture_gateway(make_app, monkeypatch, upstream)
    monkeypatch.setattr(proxy, 'INITIALIZE_READY_TIMEOUT', .04)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
            response = await asyncio.wait_for(c.post('/private_' + TOKEN + '/mcp', json=INIT), .5)
            assert response.status_code == 503 and not sent and not probe.done()
    finally:
        ready.set()
        await probe


@pytest.mark.asyncio
async def test_generation_change_while_waiting_fails_closed_even_if_pid_reused(make_app, monkeypatch):
    sent = []
    async def upstream(request):
        sent.append(1)
        return httpx.Response(200)
    app, pm, ready, probe = fixture_gateway(make_app, monkeypatch, upstream)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
            request = asyncio.create_task(c.post('/private_' + TOKEN + '/mcp', json=INIT))
            await asyncio.sleep(.05)
            pm._invalidate_initialize_generation()
            pm._process = SimpleNamespace(pid=4242, returncode=None)
            ready.set()
            response = await asyncio.wait_for(request, 1)
            assert response.status_code == 503 and not sent
    finally:
        ready.set()
        await probe


@pytest.mark.asyncio
async def test_generation_changes_at_dispatch_boundary_without_send(make_app, monkeypatch):
    sent = []
    async def upstream(request):
        sent.append(1)
        return httpx.Response(200)
    app, pm, ready, probe = fixture_gateway(make_app, monkeypatch, upstream)
    original = pm.begin_initialize_dispatch
    def replace_before_reservation(generation):
        pm._invalidate_initialize_generation()
        pm._process = SimpleNamespace(pid=4242, returncode=None)
        return original(generation)
    monkeypatch.setattr(pm, 'begin_initialize_dispatch', replace_before_reservation)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
            request = asyncio.create_task(c.post('/private_' + TOKEN + '/mcp', json=INIT))
            await asyncio.sleep(.05)
            ready.set()
            response = await asyncio.wait_for(request, 1)
            assert response.status_code == 503 and not sent
    finally:
        ready.set()
        await probe


@pytest.mark.asyncio
async def test_generation_invalidation_cancels_inflight_dispatch(make_app, monkeypatch):
    entered = asyncio.Event()
    sent = []
    async def upstream(request):
        sent.append(1)
        entered.set()
        await asyncio.sleep(20)
        return httpx.Response(200)
    app, pm, ready, probe = fixture_gateway(make_app, monkeypatch, upstream)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
            request = asyncio.create_task(c.post('/private_' + TOKEN + '/mcp', json=INIT))
            await asyncio.sleep(.05)
            ready.set()
            await asyncio.wait_for(entered.wait(), 1)
            pm._invalidate_initialize_generation()
            # Cancellation is delivered at the dispatch's next await: the bounded
            # quiesce (blocker 3) lets this cooperative dispatch finish instead of
            # failing the lifecycle step; the generation stays fenced.
            await pm._quiesce_initialize_dispatches()
            assert not pm._initialize_dispatches
            response = await asyncio.wait_for(request, 1)
            assert response.status_code == 503 and sent == [1]
            await pm._quiesce_initialize_dispatches()
    finally:
        ready.set()
        await probe


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [
    {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'noop'}},
    [INIT, {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'noop'}}],
])
async def test_non_initialize_does_not_wait_for_readiness(make_app, monkeypatch, body):
    sent = []
    async def upstream(request):
        sent.append(1)
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': 2, 'result': {}})
    app, pm, ready, probe = fixture_gateway(make_app, monkeypatch, upstream)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
            response = await asyncio.wait_for(c.post('/private_' + TOKEN + '/mcp', json=body), 1)
            assert response.status_code == 200 and sent == [1] and not probe.done()
    finally:
        ready.set()
        await probe


@pytest.mark.asyncio
async def test_non_initialize_waits_for_a_pending_identity_check_then_forwards(make_app, monkeypatch):
    """Blocker 3: right after a (re)start the listener check may still be pending; wait for it, not 502."""
    sent = []
    async def upstream(request):
        sent.append(1)
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': 2, 'result': {}})
    app, pm, ready, probe = fixture_gateway(make_app, monkeypatch, upstream)
    pm._peer_verified = None  # the probe has not seen an owned listener yet
    body = {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'noop'}}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
            request = asyncio.create_task(c.post('/private_' + TOKEN + '/mcp', json=body))
            await asyncio.sleep(.1)
            assert not request.done() and sent == []
            pm._peer_verified = (pm._initialize_generation_changed, 59999)  # synthetic: the check passed
            response = await asyncio.wait_for(request, 1)
            assert response.status_code == 200 and sent == [1] and not probe.done()
    finally:
        ready.set()
        await probe


@pytest.mark.asyncio
async def test_non_initialize_is_never_forwarded_when_the_identity_check_does_not_pass(make_app, monkeypatch):
    sent = []
    async def upstream(request):
        sent.append(1)
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': 2, 'result': {'from': 'upstream'}})
    app, pm, ready, probe = fixture_gateway(make_app, monkeypatch, upstream)
    pm._peer_verified = None
    body = {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'noop'}}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
            request = asyncio.create_task(c.post('/private_' + TOKEN + '/mcp', json=body))
            await asyncio.sleep(.1)
            assert not request.done()
            ready.set()  # the probe ends without ever verifying the listener
            await probe
            response = await asyncio.wait_for(request, 1)
            assert sent == [] and 'upstream' not in response.text and response.status_code in (200, 502)
    finally:
        ready.set()
        await probe


@pytest.mark.asyncio
async def test_second_waiter_survives_first_caller_cancellation(make_app, monkeypatch):
    sent = []
    async def upstream(request):
        sent.append(1)
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': 1, 'result': {}})
    app, pm, ready, probe = fixture_gateway(make_app, monkeypatch, upstream)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
            first = asyncio.create_task(c.post('/private_' + TOKEN + '/mcp', json=INIT))
            second = asyncio.create_task(c.post('/private_' + TOKEN + '/mcp', json=INIT))
            await asyncio.sleep(.05)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert not probe.done() and not sent
            ready.set()
            response = await asyncio.wait_for(second, 1)
            assert response.status_code == 200 and sent == [1]
    finally:
        ready.set()
        await probe
    assert pm._initialize_waiters == 0


@pytest.mark.asyncio
async def test_waiter_limit_fails_closed_without_blocking_existing_waiter(make_app, monkeypatch):
    from mcp_admin_core import proxy
    sent = []
    async def upstream(request):
        sent.append(1)
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': 1, 'result': {}})
    app, pm, ready, probe = fixture_gateway(make_app, monkeypatch, upstream)
    monkeypatch.setattr(proxy, 'INITIALIZE_READY_WAITERS', 1)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
            first = asyncio.create_task(c.post('/private_' + TOKEN + '/mcp', json=INIT))
            await asyncio.sleep(.05)
            second = await asyncio.wait_for(c.post('/private_' + TOKEN + '/mcp', json=INIT), 1)
            assert second.status_code == 503 and not sent
            ready.set()
            assert (await asyncio.wait_for(first, 1)).status_code == 200 and sent == [1]
    finally:
        ready.set()
        await probe
    assert pm._initialize_waiters == 0


@pytest.mark.asyncio
async def test_asgi_disconnect_while_waiting_has_no_dispatch(make_app, monkeypatch):
    sent = []
    async def upstream(request):
        sent.append(1)
        return httpx.Response(200)
    app, pm, ready, probe = fixture_gateway(make_app, monkeypatch, upstream)
    messages = asyncio.Queue()
    await messages.put({'type': 'http.request', 'body': json.dumps(INIT).encode(), 'more_body': False})
    outgoing = []
    async def receive():
        return await messages.get()
    async def send(event):
        outgoing.append(event['type'])
    path = '/private_' + TOKEN + '/mcp'
    scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
             'method': 'POST', 'scheme': 'http', 'path': path, 'raw_path': path.encode(),
             'query_string': b'', 'headers': [(b'host', b'test'), (b'content-type', b'application/json')],
             'client': ('127.0.0.1', 1234), 'server': ('test', 80), 'root_path': ''}
    try:
        request = asyncio.create_task(app(scope, receive, send))
        await asyncio.sleep(.05)
        await messages.put({'type': 'http.disconnect'})
        await asyncio.wait_for(request, 1)
        assert not sent and not probe.done()
    finally:
        ready.set()
        await probe
    assert pm._initialize_waiters == 0


@pytest.mark.asyncio
async def test_asgi_disconnect_during_dispatch_cancels_transport(make_app, monkeypatch):
    entered, cancelled = asyncio.Event(), asyncio.Event()
    sent = []

    async def upstream(request):
        sent.append(1)
        entered.set()
        try:
            await asyncio.sleep(20)
        finally:
            cancelled.set()
        return httpx.Response(200)

    app, pm, ready, probe = fixture_gateway(make_app, monkeypatch, upstream)
    messages = asyncio.Queue()
    await messages.put({'type': 'http.request', 'body': json.dumps(INIT).encode(), 'more_body': False})
    outgoing = []
    async def receive():
        return await messages.get()
    async def send(event):
        outgoing.append(event)
    path = '/private_' + TOKEN + '/mcp'
    scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
             'method': 'POST', 'scheme': 'http', 'path': path, 'raw_path': path.encode(),
             'query_string': b'', 'headers': [(b'host', b'test'), (b'content-type', b'application/json')],
             'client': ('127.0.0.1', 1234), 'server': ('test', 80), 'root_path': ''}
    request = asyncio.create_task(app(scope, receive, send))
    try:
        for _ in range(100):
            if pm._initialize_waiters:
                break
            await asyncio.sleep(.005)
        assert pm._initialize_waiters == 1
        ready.set()
        await asyncio.wait_for(entered.wait(), 1)
        await messages.put({'type': 'http.disconnect'})
        await asyncio.wait_for(cancelled.wait(), .5)
        await asyncio.wait_for(request, .5)
        assert sent == [1]
        assert not any(event.get('status') == 200 for event in outgoing)
    finally:
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        ready.set()
        await probe


@pytest.mark.asyncio
async def test_failed_verification_does_not_dispatch(make_app, monkeypatch):
    sent = []
    async def upstream(request):
        sent.append(1)
        return httpx.Response(200)
    app, pm, ready, probe = fixture_gateway(make_app, monkeypatch, upstream)
    probe.cancel()
    await asyncio.gather(probe, return_exceptions=True)
    pm._configuration_task = None
    pm._verifying_configuration = False
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
        response = await c.post('/private_' + TOKEN + '/mcp', json=INIT)
    assert response.status_code == 503 and not sent


@pytest.mark.asyncio
async def test_429_after_wait_does_not_reclaim_or_replay(make_app, monkeypatch):
    from mcp_admin_core import proxy
    sent = []
    async def upstream(request):
        sent.append(request.method)
        return httpx.Response(429, text='synthetic capacity')
    app, pm, ready, probe = fixture_gateway(make_app, monkeypatch, upstream)
    class Sessions:
        def begin(self, *args): return None
    app.state.session_reclaimer = Sessions()
    async def forbidden_reclaim(*args, **kwargs):
        pytest.fail('reclaim would replay initialize')
    monkeypatch.setattr(proxy, '_reclaim_and_retry', forbidden_reclaim)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
            request = asyncio.create_task(c.post('/private_' + TOKEN + '/mcp', json=INIT))
            await asyncio.sleep(.05)
            ready.set()
            result = await asyncio.wait_for(request, 1)
            assert result.status_code == 429 and sent == ['POST']
    finally:
        ready.set()
        await probe


@pytest.mark.asyncio
async def test_stubborn_dispatch_prevents_managed_stop_and_replacement():
    pm = McpProcessManager()
    release = asyncio.Event()
    started = asyncio.Event()
    signals = []
    class Process:
        pid = 4242
        returncode = None
        def send_signal(self, sig): signals.append(sig)
    pm._process = Process()
    pm._running = True
    pm._applied_revision = 'synthetic-revision'
    async def stubborn():
        assert pm.begin_initialize_dispatch(pm.initialize_generation())
        started.set()
        try:
            # Uncooperative: swallows every cancellation (each lifecycle attempt cancels again).
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass
        finally:
            pm.end_initialize_dispatch()
    task = asyncio.create_task(stubborn())
    await started.wait()
    try:
        # Bounded (INITIALIZE_QUIESCE_TIMEOUT) wait under the lock, then fail closed.
        with pytest.raises(RuntimeError, match='did not stop'):
            await asyncio.wait_for(pm._stop_locked(), 2)
        assert not signals and pm._process.returncode is None and pm._applied_revision is None
        # Replacing the fenced generation is refused the same way while the dispatch is registered.
        with pytest.raises(RuntimeError, match='did not stop'):
            await asyncio.wait_for(pm._start_locked(), 2)
        assert not signals and pm._process.returncode is None
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_invalidated_warm_generation_cannot_be_reused(make_app, monkeypatch):
    sent = []
    async def upstream(request):
        sent.append(1)
        return httpx.Response(200)
    app, pm, ready, probe = fixture_gateway(make_app, monkeypatch, upstream)
    ready.set()
    await probe
    pm._invalidate_initialize_generation()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
        response = await c.post('/private_' + TOKEN + '/mcp', json=INIT)
    assert response.status_code == 503 and not sent


@pytest.mark.asyncio
async def test_readiness_and_first_dispatch_share_bounded_deadline(make_app, monkeypatch):
    from mcp_admin_core import proxy
    entered = asyncio.Event()
    sent = []
    async def upstream(request):
        sent.append(1)
        entered.set()
        await asyncio.sleep(20)
        return httpx.Response(200)
    app, pm, ready, probe = fixture_gateway(make_app, monkeypatch, upstream)
    monkeypatch.setattr(proxy, 'INITIALIZE_READY_TIMEOUT', .15)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
            task = asyncio.create_task(c.post('/private_' + TOKEN + '/mcp', json=INIT))
            await asyncio.sleep(.05)
            ready.set()
            await asyncio.wait_for(entered.wait(), 1)
            response = await asyncio.wait_for(task, .5)
            assert response.status_code == 503 and sent == [1]
    finally:
        ready.set()
        await probe


@pytest.mark.asyncio
async def test_managed_stop_cancels_dispatch_before_stopping_child(make_app, monkeypatch):
    entered = asyncio.Event()
    events = []
    async def upstream(request):
        events.append('dispatch-start')
        entered.set()
        try:
            await asyncio.sleep(20)
        finally:
            events.append('dispatch-finished')
        return httpx.Response(200)
    app, pm, ready, probe = fixture_gateway(make_app, monkeypatch, upstream)
    class Process:
        pid = 4242
        returncode = None
        def send_signal(self, sig):
            events.append('signal')
            self.returncode = 0
        async def wait(self): return self.returncode
    pm._process = Process()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
            request = asyncio.create_task(c.post('/private_' + TOKEN + '/mcp', json=INIT))
            await asyncio.sleep(.05)
            ready.set()
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(pm.stop(), 1)
            response = await asyncio.wait_for(request, 1)
            assert response.status_code == 503
            assert events == ['dispatch-start', 'dispatch-finished', 'signal']
    finally:
        ready.set()
        await asyncio.gather(probe, return_exceptions=True)


@pytest.mark.asyncio
async def test_deadline_returns_while_stubborn_transport_remains_fenced(make_app, monkeypatch):
    from mcp_admin_core import proxy
    entered = asyncio.Event()
    release = asyncio.Event()
    closed = asyncio.Event()
    sent = []
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{}'
        async def aclose(self):
            closed.set()
    async def upstream(request):
        sent.append(1)
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return httpx.Response(200, stream=Stream())
    app, pm, ready, probe = fixture_gateway(make_app, monkeypatch, upstream)
    monkeypatch.setattr(proxy, 'INITIALIZE_READY_TIMEOUT', .12)
    request = None
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
            request = asyncio.create_task(c.post('/private_' + TOKEN + '/mcp', json=INIT))
            for _ in range(100):
                if pm._initialize_waiters: break
                await asyncio.sleep(.005)
            assert pm._initialize_waiters == 1
            ready.set()
            await asyncio.wait_for(entered.wait(), 1)
            response = await asyncio.wait_for(asyncio.shield(request), .4)
            assert response.status_code == 503 and sent == [1]
            assert len(pm._initialize_dispatches) == 1
            with pytest.raises(RuntimeError, match='did not stop'):
                await pm._quiesce_initialize_dispatches()
    finally:
        release.set()
        if request is not None:
            await asyncio.gather(request, return_exceptions=True)
        await asyncio.gather(probe, return_exceptions=True)
        for _ in range(100):
            if not pm._initialize_dispatches: break
            await asyncio.sleep(.005)
    assert not pm._initialize_dispatches
    await asyncio.wait_for(closed.wait(), 1)
