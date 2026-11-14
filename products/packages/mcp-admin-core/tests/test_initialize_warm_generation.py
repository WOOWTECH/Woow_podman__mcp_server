"""Synthetic warm-child generation races through the actual gateway."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from mcp_admin_core.session_reclaim import IDLE_FLOOR_SECONDS, SessionReclaimer
from n8n_mcp_admin.child_env import is_session_limit_reply
from managed_proxy_fixture import trust_listener
from test_initialize_presend_readiness import INIT, TOKEN, fixture_gateway


class StoppableSyntheticProcess:
    pid = 4242
    returncode = None
    def send_signal(self, _signal):
        self.returncode = 0
    async def wait(self):
        return self.returncode


def replace_generation(pm):
    pm._invalidate_initialize_generation()
    pm._process = SimpleNamespace(pid=99999, returncode=None)
    pm._initialize_generation_changed = asyncio.Event()


@pytest.mark.asyncio
async def test_warm_initialize_does_not_succeed_after_generation_changes(make_app, monkeypatch):
    calls = []
    pm = None
    async def upstream(request):
        calls.append((request.method, pm.child_pid))
        replace_generation(pm)
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': 1, 'result': {}})
    app, pm, ready, probe = fixture_gateway(make_app, monkeypatch, upstream)
    ready.set()
    await probe
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/private_' + TOKEN + '/mcp', json=INIT)
    assert response.status_code == 503 and calls == [('POST', 4242)]


@pytest.mark.asyncio
@pytest.mark.parametrize('delete_status', [204, 404])
async def test_warm_reclaim_cannot_delete_or_retry_into_replacement(make_app, monkeypatch, delete_status):
    calls = []
    clock = [1000.0]
    pm = None
    async def upstream(request):
        calls.append((request.method, pm.child_pid))
        if request.method == 'DELETE':
            return httpx.Response(delete_status)
        if len(calls) == 1:
            replace_generation(pm)
            return httpx.Response(429, json={'jsonrpc': '2.0', 'id': 1,
                'error': {'code': -32000, 'message': 'Session limit reached (1). Please wait for existing sessions to expire.'}})
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': 1, 'result': {}})
    app, pm, ready, probe = fixture_gateway(make_app, monkeypatch, upstream)
    ready.set()
    await probe
    sessions = SessionReclaimer(is_session_limit=is_session_limit_reply, clock=lambda: clock[0])
    sessions.created('synthetic-old-session', 4242)
    clock[0] += IDLE_FLOOR_SECONDS + 1
    app.state.session_reclaimer = sessions
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/private_' + TOKEN + '/mcp', json=INIT)
    assert response.status_code == 503 and calls == [('POST', 4242)]


@pytest.mark.asyncio
async def test_initialize_cannot_send_to_child_started_after_empty_capture(make_app, monkeypatch):
    from mcp_admin_core import proxy
    calls = []
    async def upstream(request):
        calls.append(request.method)
        return httpx.Response(200)
    app, pm, ready, probe = fixture_gateway(make_app, monkeypatch, upstream)
    ready.set()
    await probe
    pm._process = None
    async def start_at_wait(*args):
        pm._process = SimpleNamespace(pid=99999, returncode=None)
        return None
    monkeypatch.setattr(proxy, '_wait_for_initialize_ready', start_at_wait)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/private_' + TOKEN + '/mcp', json=INIT)
    assert response.status_code == 503 and calls == []


@pytest.mark.asyncio
async def test_stop_during_warm_reclaim_delete_fences_replacement(make_app, monkeypatch):
    from mcp_admin_core import proxy
    monkeypatch.setattr(proxy, '_AUTOMATIC_RECLAIM_EXPERIMENTAL', True)
    calls = []
    entered = asyncio.Event()
    clock = [1000.0]
    async def upstream(request):
        calls.append(request.method)
        if request.method == 'DELETE':
            entered.set()
            await asyncio.sleep(20)
            return httpx.Response(204)
        return httpx.Response(429, json={'jsonrpc': '2.0', 'id': 1,
            'error': {'code': -32000, 'message': 'Session limit reached (1). Please wait for existing sessions to expire.'}})
    app, pm, ready, probe = fixture_gateway(make_app, monkeypatch, upstream)
    ready.set()
    await probe
    pm._process = StoppableSyntheticProcess()
    sessions = SessionReclaimer(is_session_limit=is_session_limit_reply, clock=lambda: clock[0])
    sessions.created('synthetic-old-session', pm.initialize_generation())
    clock[0] += IDLE_FLOOR_SECONDS + 1
    app.state.session_reclaimer = sessions
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        task = asyncio.create_task(client.post('/private_' + TOKEN + '/mcp', json=INIT))
        await asyncio.wait_for(entered.wait(), 1)
        await pm.stop()
        assert pm._process.returncode == 0
        response = await asyncio.wait_for(task, 1)
    assert response.status_code == 503 and calls == ['POST', 'DELETE']
    assert not pm._initialize_dispatches


@pytest.mark.asyncio
async def test_reclaim_does_not_forget_reused_id_from_new_generation():
    clock = [1000.0]
    sessions = SessionReclaimer(is_session_limit=lambda *args: True, clock=lambda: clock[0])
    sessions.created('synthetic-reused-id', 'old-generation')
    clock[0] += IDLE_FLOOR_SECONDS + 1
    async def delete(_sid):
        sessions.created('synthetic-reused-id', 'new-generation')
        return 204
    assert not await sessions.reclaim(delete, 'old-generation')
    assert sessions.tracked()['synthetic-reused-id']['generation'] == 'new-generation'


@pytest.mark.asyncio
@pytest.mark.parametrize('delete_status', [204, 404])
async def test_generation_change_during_delete_never_retries(make_app, monkeypatch, delete_status):
    from mcp_admin_core import proxy
    monkeypatch.setattr(proxy, '_AUTOMATIC_RECLAIM_EXPERIMENTAL', True)
    calls, clock = [], [1000.0]
    pm = None
    async def upstream(request):
        calls.append((request.method, pm.child_pid))
        if request.method == 'DELETE':
            replace_generation(pm)
            return httpx.Response(delete_status)
        return httpx.Response(429, json={'jsonrpc': '2.0', 'id': 1,
            'error': {'code': -32000, 'message': 'Session limit reached (1). Please wait for existing sessions to expire.'}})
    app, pm, ready, probe = fixture_gateway(make_app, monkeypatch, upstream)
    ready.set()
    await probe
    sessions = SessionReclaimer(is_session_limit=is_session_limit_reply, clock=lambda: clock[0])
    sessions.created('synthetic-old-session', pm.initialize_generation())
    clock[0] += IDLE_FLOOR_SECONDS + 1
    app.state.session_reclaimer = sessions
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/private_' + TOKEN + '/mcp', json=INIT)
    assert response.status_code == 503 and calls == [('POST', 4242), ('DELETE', 4242)]


@pytest.mark.asyncio
async def test_same_pid_new_generation_does_not_reclaim_old_session(make_app, monkeypatch):
    from mcp_admin_core import proxy
    monkeypatch.setattr(proxy, '_AUTOMATIC_RECLAIM_EXPERIMENTAL', True)
    calls, clock = [], [1000.0]
    async def upstream(request):
        calls.append(request.method)
        return httpx.Response(429, json={'jsonrpc': '2.0', 'id': 1,
            'error': {'code': -32000, 'message': 'Session limit reached (1). Please wait for existing sessions to expire.'}})
    app, pm, ready, probe = fixture_gateway(make_app, monkeypatch, upstream)
    ready.set()
    await probe
    sessions = SessionReclaimer(is_session_limit=is_session_limit_reply, clock=lambda: clock[0])
    sessions.created('synthetic-old-session', pm.initialize_generation())
    pm._invalidate_initialize_generation()
    pm._process = SimpleNamespace(pid=4242, returncode=None)
    pm._initialize_generation_changed = asyncio.Event()
    trust_listener(pm, 59999)  # the replacement passed its (synthetic) identity check
    clock[0] += IDLE_FLOOR_SECONDS + 1
    app.state.session_reclaimer = sessions
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/private_' + TOKEN + '/mcp', json=INIT)
    assert response.status_code == 429 and calls == ['POST'] and sessions.tracked() == {}


@pytest.mark.asyncio
async def test_default_session_limit_returns_429_without_delete_or_retry(make_app, monkeypatch):
    calls, clock = [], [1000.0]
    async def upstream(request):
        calls.append(request.method)
        return httpx.Response(429, json={'jsonrpc': '2.0', 'id': 1,
            'error': {'code': -32000, 'message': 'Session limit reached (1). Please wait for existing sessions to expire.'}})
    app, pm, ready, probe = fixture_gateway(make_app, monkeypatch, upstream)
    ready.set()
    await probe
    sessions = SessionReclaimer(is_session_limit=is_session_limit_reply, clock=lambda: clock[0])
    sessions.created('synthetic-old-session', pm.initialize_generation())
    clock[0] += IDLE_FLOOR_SECONDS + 1
    app.state.session_reclaimer = sessions
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/private_' + TOKEN + '/mcp', json=INIT)
    assert response.status_code == 429 and calls == ['POST']
    assert 'synthetic-old-session' in sessions.tracked()


@pytest.mark.asyncio
async def test_token_revoked_during_capacity_response_never_retries(make_app, monkeypatch):
    from mcp_admin_core.config.store import get_config_store
    calls = []
    async def upstream(request):
        calls.append(request.method)
        await get_config_store().change_mcp_token(None, action='revoke')
        return httpx.Response(429, json={'error': {'code': -32000, 'message': 'Session limit reached (1).'}})
    app, pm, ready, probe = fixture_gateway(make_app, monkeypatch, upstream)
    ready.set()
    await probe
    app.state.session_reclaimer = SessionReclaimer(is_session_limit=is_session_limit_reply)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/private_' + TOKEN + '/mcp', json=INIT)
    assert response.status_code == 403 and calls == ['POST']


@pytest.mark.asyncio
async def test_missing_generation_api_fails_closed(make_app, monkeypatch):
    from mcp_admin_core import proxy
    calls = []
    async def upstream(request):
        calls.append(request.method)
        return httpx.Response(200)
    app, pm, ready, probe = fixture_gateway(make_app, monkeypatch, upstream)
    ready.set()
    await probe
    monkeypatch.setattr(proxy, 'get_process_manager', lambda: SimpleNamespace(is_running=True, child_pid=4242))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/private_' + TOKEN + '/mcp', json=INIT)
    assert response.status_code == 503 and calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('origin', [None, 'https://trusted.example'])
async def test_replacement_after_relay_before_response_headers_fails_closed(make_app, monkeypatch, origin):
    from mcp_admin_core import proxy
    monkeypatch.setenv('MCP_ADMIN_ALLOWED_ORIGINS', 'https://trusted.example')
    async def upstream(request):
        return httpx.Response(200, headers={'mcp-session-id': 'synthetic-session', 'x-child-private': 'synthetic-only'},
                              json={'jsonrpc': '2.0', 'id': 1, 'result': {}})
    app, pm, ready, probe = fixture_gateway(make_app, monkeypatch, upstream)
    ready.set()
    await probe
    app.state.session_reclaimer = SessionReclaimer(is_session_limit=is_session_limit_reply)
    observe = proxy._observe_session
    def replace_after_observe(*args):
        observe(*args)
        asyncio.get_running_loop().call_soon(replace_generation, pm)
    monkeypatch.setattr(proxy, '_observe_session', replace_after_observe)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/private_' + TOKEN + '/mcp', json=INIT,
                                     headers={'Origin': origin} if origin else {})
    assert response.status_code == 503
    assert 'mcp-session-id' not in response.headers and 'x-child-private' not in response.headers
    assert 'result' not in response.json()
    if origin:
        assert response.headers['access-control-allow-origin'] == origin


@pytest.mark.asyncio
async def test_managed_stop_cancels_blocked_outer_header_send(make_app, monkeypatch):
    entered = asyncio.Event()
    delivered = []
    async def upstream(request):
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': 1, 'result': {}})
    app, pm, ready, probe = fixture_gateway(make_app, monkeypatch, upstream)
    ready.set()
    await probe
    pm._process = StoppableSyntheticProcess()
    messages = asyncio.Queue()
    await messages.put({'type': 'http.request', 'body': json.dumps(INIT).encode(), 'more_body': False})
    async def receive():
        return await messages.get()
    async def send(event):
        if event['type'] == 'http.response.start':
            entered.set()
            await asyncio.sleep(20)
        delivered.append(event)
    path = '/private_' + TOKEN + '/mcp'
    scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
             'method': 'POST', 'scheme': 'http', 'path': path, 'raw_path': path.encode(),
             'query_string': b'', 'headers': [(b'host', b'test'), (b'content-type', b'application/json')],
             'client': ('127.0.0.1', 1234), 'server': ('test', 80), 'root_path': ''}
    task = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert pm._initialize_dispatches
        await pm.stop()
        assert pm._process.returncode == 0
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
        assert delivered == [] and not pm._initialize_dispatches
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_revoke_cancels_blocked_outer_header_send(make_app, monkeypatch):
    from mcp_admin_core.config.store import get_config_store
    entered = asyncio.Event()
    delivered = []
    async def upstream(request):
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': 1, 'result': {}})
    app, pm, ready, probe = fixture_gateway(make_app, monkeypatch, upstream)
    ready.set()
    await probe
    messages = asyncio.Queue()
    await messages.put({'type': 'http.request', 'body': json.dumps(INIT).encode(), 'more_body': False})
    async def receive():
        return await messages.get()
    async def send(event):
        if event['type'] == 'http.response.start':
            entered.set()
            await asyncio.sleep(20)
        delivered.append(event)
    path = '/private_' + TOKEN + '/mcp'
    scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
             'method': 'POST', 'scheme': 'http', 'path': path, 'raw_path': path.encode(),
             'query_string': b'', 'headers': [(b'host', b'test'), (b'content-type', b'application/json')],
             'client': ('127.0.0.1', 1234), 'server': ('test', 80), 'root_path': ''}
    task = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(get_config_store().change_mcp_token(None, action='revoke'), .5)
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
        assert delivered == [] and not pm._initialize_dispatches
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_missing_required_fields_during_child_exit_returns_setup_without_dispatch(make_app, monkeypatch):
    from mcp_admin_core import proxy
    calls = []
    async def upstream(request):
        calls.append(request.method)
        return httpx.Response(200)
    app, pm, ready, probe = fixture_gateway(make_app, monkeypatch, upstream)
    async def missing(_store):
        return [('base_url', 'Nextcloud URL')]
    monkeypatch.setattr(proxy.setup_status, 'missing_fields', missing)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            task = asyncio.create_task(client.post('/private_' + TOKEN + '/mcp', json=INIT))
            for _ in range(100):
                if pm._initialize_waiters: break
                await asyncio.sleep(.005)
            assert pm._initialize_waiters == 1
            pm._process.returncode = 2
            pm._invalidate_initialize_generation()
            response = await asyncio.wait_for(task, 1)
        assert response.status_code == 200 and response.headers['mcp-session-id'].startswith('woow-')
        assert 'Nextcloud URL' in response.json()['result']['instructions'] and calls == []
    finally:
        ready.set()
        await probe


@pytest.mark.asyncio
async def test_missing_fields_placeholder_rechecks_revoked_token(make_app, monkeypatch):
    from mcp_admin_core import proxy
    from mcp_admin_core.config.store import get_config_store
    async def upstream(_request):
        pytest.fail('must not dispatch after child exit')
    app, pm, ready, probe = fixture_gateway(make_app, monkeypatch, upstream)
    async def missing(_store):
        await get_config_store().change_mcp_token(None, action='revoke')
        return [('base_url', 'Nextcloud URL')]
    monkeypatch.setattr(proxy.setup_status, 'missing_fields', missing)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            task = asyncio.create_task(client.post('/private_' + TOKEN + '/mcp', json=INIT))
            for _ in range(100):
                if pm._initialize_waiters: break
                await asyncio.sleep(.005)
            assert pm._initialize_waiters == 1
            pm._process.returncode = 2
            pm._invalidate_initialize_generation()
            response = await asyncio.wait_for(task, 1)
        assert response.status_code == 403
    finally:
        ready.set()
        await probe


@pytest.mark.asyncio
async def test_missing_fields_lookup_respects_initialize_deadline(make_app, monkeypatch):
    from mcp_admin_core import proxy
    async def upstream(_request):
        pytest.fail('must not dispatch after child exit')
    app, pm, ready, probe = fixture_gateway(make_app, monkeypatch, upstream)
    async def slow_missing(_store):
        await asyncio.sleep(20)
        return [('base_url', 'Nextcloud URL')]
    monkeypatch.setattr(proxy, 'INITIALIZE_READY_TIMEOUT', .08)
    monkeypatch.setattr(proxy.setup_status, 'missing_fields', slow_missing)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            task = asyncio.create_task(client.post('/private_' + TOKEN + '/mcp', json=INIT))
            for _ in range(100):
                if pm._initialize_waiters: break
                await asyncio.sleep(.002)
            assert pm._initialize_waiters == 1
            pm._process.returncode = 2
            pm._invalidate_initialize_generation()
            response = await asyncio.wait_for(task, .5)
        assert response.status_code == 503
    finally:
        ready.set()
        await probe

@pytest.mark.asyncio
async def test_setup_lookup_stubborn_cancellation_is_bounded_and_single(make_app, monkeypatch):
    from mcp_admin_core import proxy
    from mcp_admin_core.config.store import get_config_store
    from mcp_admin_core.mcp_access import AccessLease
    app, pm, ready, probe = fixture_gateway(make_app, monkeypatch, lambda req: httpx.Response(200))
    store = get_config_store()
    lease = await AccessLease.acquire(store, TOKEN)
    release = asyncio.Event()
    cancelled = asyncio.Event()
    calls = []
    async def stubborn(_store):
        calls.append(1)
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
        return [('base_url', 'Nextcloud URL')]
    monkeypatch.setattr(proxy.setup_status, 'missing_fields', stubborn)
    try:
        loop = asyncio.get_running_loop()
        assert await asyncio.wait_for(proxy._bounded_setup_fields(store, lease, loop.time() + .04), .3) is None
        await asyncio.wait_for(cancelled.wait(), .3)
        assert await asyncio.wait_for(proxy._bounded_setup_fields(store, lease, loop.time() + .04), .3) is None
        assert len(calls) == 1
    finally:
        release.set()
        pending = getattr(store, '_initialize_setup_lookup', None)
        if pending is not None:
            await asyncio.wait_for(pending, .3)
        ready.set()
        await probe
