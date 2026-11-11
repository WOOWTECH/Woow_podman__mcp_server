"""Shared credentials: atomic persistence and real proxy I/O cut-off."""

from management_client import revision_headers
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import ClientDisconnect

from mcp_admin_core.config.store import ConfigStore
from mcp_admin_core.mcp_access import AccessChanged, AccessLease
from mcp_admin_core import proxy


def auth(client):
    token = client.post('/api/auth/login', json={'password': 'admin'}).json()['token']
    return {'Authorization': 'Bearer ' + token}


def test_revoke_persists_and_requires_explicit_reissue(make_app, tmp_config_path):
    c = make_app({'mcp_auth_token': 'initial-shared-token'})
    h = auth(c)
    assert c.post('/api/tokens/revoke', headers=revision_headers(c, h, read_path='/api/settings')).json()['token_state'] == 'revoked'
    assert c.get('/private_initial-shared-token/mcp').status_code == 403
    info = c.get('/api/tokens', headers=h).json()
    assert info['shared'] and info['state'] == 'revoked' and not info['token_set']
    persisted = json.loads(tmp_config_path.read_text())
    assert persisted['mcp_auth_revoked'] and persisted['mcp_auth_token'] == ''
    assert 'initial-shared-token' not in json.dumps(persisted['token_history'])
    # New process/store loads the persisted revocation, even if the old seed is supplied.
    c2 = make_app({**persisted, 'mcp_auth_token': 'initial-shared-token'})
    assert c2.get('/private_initial-shared-token/mcp').status_code == 403
    fresh = c2.post('/api/settings/mcp_auth_token/rotate', headers=revision_headers(c2, auth(c2), read_path='/api/settings')).json()['token']
    assert fresh != 'initial-shared-token'
    assert c2.get('/private_initial-shared-token/mcp').status_code == 403
    assert c2.get('/api/tokens').json()['state'] == 'active'


async def test_failed_persist_keeps_old_lease_and_history(fresh_store, monkeypatch):
    await fresh_store.put('mcp_auth_token', 'prior-credential')
    lease = await AccessLease.acquire(fresh_store, 'prior-credential')
    old = await fresh_store.load()
    def fail(_):
        raise OSError('simulated disk failure')
    monkeypatch.setattr(fresh_store, '_write_sync', fail)
    with pytest.raises(OSError):
        await fresh_store.change_mcp_token(None, action='revoke')
    assert await fresh_store.load() == old
    lease.check()


async def test_concurrent_rotation_and_revoke_have_one_atomic_history(fresh_store):
    await fresh_store.put('mcp_auth_token', 'prior-credential')
    await asyncio.gather(*(fresh_store.change_mcp_token('next-'+str(i), action='rotate') for i in range(8)))
    cfg = await fresh_store.load()
    assert cfg['mcp_auth_token'] == 'next-7' and len(cfg['token_history']) == 5
    assert cfg['token_history'][0]['previous_token_masked']
    assert json.loads(fresh_store.path.read_text()) == cfg


async def test_old_whole_config_save_cannot_undo_revocation(fresh_store):
    await fresh_store.put('mcp_auth_token', 'prior-credential')
    old = await fresh_store.load()
    await fresh_store.change_mcp_token(None, action='revoke')
    await fresh_store.save(old)
    with pytest.raises(AccessChanged):
        await AccessLease.acquire(fresh_store, 'prior-credential')
    fresh = ConfigStore(fresh_store.path)
    with pytest.raises(AccessChanged):
        await AccessLease.acquire(fresh, 'prior-credential')


async def test_old_snapshot_cannot_restore_token_or_history_after_reissue(fresh_store):
    await fresh_store.change_mcp_token('prior-credential', action='set')
    old = await fresh_store.load()
    await fresh_store.change_mcp_token(None, action='revoke')
    await fresh_store.change_mcp_token('current-credential', action='rotate')
    current = await fresh_store.load()
    lease = await AccessLease.acquire(fresh_store, 'current-credential')
    old['connection'] = {'fixture': 'updated'}
    await fresh_store.save(old)
    lease.check()
    updated = await fresh_store.load()
    for key in ('mcp_auth_token', 'mcp_auth_revoked', 'mcp_auth_generation', 'token_history'):
        assert updated[key] == current[key]
    assert updated['connection'] == old['connection']
    with pytest.raises(AccessChanged):
        await AccessLease.acquire(ConfigStore(fresh_store.path), 'prior-credential')
    await AccessLease.acquire(ConfigStore(fresh_store.path), 'current-credential')


class SlowStream(httpx.AsyncByteStream):
    def __init__(self):
        self.waiting = asyncio.Event()
        self.closed = False
    async def __aiter__(self):
        yield b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n\n'
        self.waiting.set()
        await asyncio.Event().wait()
        yield b'NEVER_AFTER_REVOCATION'
    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize('path', ['sse', 'mcp'])
async def test_two_active_streams_close_on_revocation(fresh_store, monkeypatch, path):
    await fresh_store.put('mcp_auth_token', 'shared')
    streams = []
    async def upstream(_):
        stream = SlowStream(); streams.append(stream)
        return httpx.Response(200, stream=stream, headers={'content-type':'text/event-stream'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as upstream_client:
        monkeypatch.setattr(proxy, 'get_config_store', lambda: fresh_store)
        monkeypatch.setattr(proxy, '_get_client', lambda *args: upstream_client)
        app = FastAPI(); app.include_router(proxy.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as c:
            tasks = [asyncio.create_task(c.get('/private_shared/'+path)) for _ in range(2)]
            async with asyncio.timeout(3):
                while len(streams) < 2:
                    await asyncio.sleep(0)
                await asyncio.gather(*(s.waiting.wait() for s in streams))
                await fresh_store.change_mcp_token(None, action='revoke')
                responses = await asyncio.gather(*tasks, return_exceptions=True)
            assert all(s.closed for s in streams)
            assert all(isinstance(r, ClientDisconnect) for r in responses)
            assert (await c.get('/private_shared/'+path)).status_code == 403


@pytest.mark.parametrize('bounded', [False, True])
async def test_revocation_during_slow_request_body_prevents_dispatch(fresh_store, monkeypatch, bounded):
    from mcp_admin_core.request_limits import McpRequestLimits

    await fresh_store.put('mcp_auth_token', 'shared')
    started = asyncio.Event()
    async def body():
        started.set()
        await asyncio.Event().wait()
    async def stream():
        yield await body()
    limits = McpRequestLimits(4 * 1024 * 1024, 10_000) if bounded else None
    request = SimpleNamespace(url=SimpleNamespace(query=''), headers={}, body=body, stream=stream,
                              app=SimpleNamespace(state=SimpleNamespace(mcp_request_limits=limits)))
    monkeypatch.setattr(proxy, 'get_config_store', lambda: fresh_store)
    def no_dispatch(*args):
        raise AssertionError('revoked request dispatched upstream')
    monkeypatch.setattr(proxy, '_get_client', no_dispatch)
    task = asyncio.create_task(proxy.mcp_proxy('shared', 'mcp', request))
    await asyncio.wait_for(started.wait(), 2)
    await fresh_store.change_mcp_token(None, action='revoke')
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        await asyncio.wait_for(task, 2)
    assert e.value.status_code == 403


async def test_revocation_before_upstream_headers_discards_response(fresh_store, monkeypatch):
    await fresh_store.put('mcp_auth_token', 'shared')
    started = asyncio.Event(); closed = asyncio.Event()
    async def upstream(_):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as upstream_client:
        monkeypatch.setattr(proxy, 'get_config_store', lambda: fresh_store)
        monkeypatch.setattr(proxy, '_get_client', lambda *args: upstream_client)
        app = FastAPI(); app.include_router(proxy.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as c:
            task = asyncio.create_task(c.get('/private_shared/mcp'))
            await started.wait()
            await fresh_store.change_mcp_token(None, action='revoke')
            response = await asyncio.wait_for(task, 2)
            assert response.status_code == 403 and closed.is_set()


async def test_revoke_and_headers_same_tick_close_unclaimed_response():
    changed = asyncio.Event(); lease = AccessLease(changed); closed = asyncio.Event()
    async def close(): closed.set()
    async def headers():
        changed.set()
        return SimpleNamespace(aclose=close)
    with pytest.raises(AccessChanged):
        await lease.run(headers())
    assert closed.is_set()


async def test_repeated_caller_cancellation_finishes_io_cleanup():
    entered = asyncio.Event(); closing = asyncio.Event(); finish = asyncio.Event(); closed = asyncio.Event()
    lease = AccessLease(asyncio.Event())
    async def io():
        entered.set()
        try: await asyncio.Event().wait()
        finally:
            closing.set()
            await finish.wait()
            closed.set()
    task = asyncio.create_task(lease.run(io()))
    await entered.wait(); task.cancel(); await closing.wait()
    for _ in range(3):
        task.cancel(); await asyncio.sleep(0)
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError): await task
    assert closed.is_set()


async def test_cancellation_during_successful_header_cleanup_closes_unclaimed_response():
    closing = asyncio.Event(); release = asyncio.Event()
    class ControlledWaiter(asyncio.Event):
        async def wait(self):
            try:
                await asyncio.Event().wait()
            finally:
                closing.set()
                await release.wait()
    stream = SlowStream()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=stream))) as client:
        operation = client.send(client.build_request('GET', 'http://fixture/mcp'), stream=True)
        task = asyncio.create_task(AccessLease(ControlledWaiter()).run(operation))
        await asyncio.wait_for(closing.wait(), 2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert stream.closed


@pytest.mark.parametrize('buffered', [False, True])
async def test_revocation_cancels_blocked_downstream_delivery(fresh_store, monkeypatch, buffered):
    await fresh_store.change_mcp_token('shared', action='set')
    if buffered:
        await fresh_store.put('tools', {'disabled': ['hidden']})
    stream = SlowStream()
    async def upstream(_):
        if buffered:
            return httpx.Response(200, json={'jsonrpc':'2.0', 'id':1, 'result':{'tools':[{'name':'allowed'}]}})
        return httpx.Response(200, stream=stream, headers={'content-type':'text/event-stream'})
    async def body():
        return b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
    async def body_stream():  # 非 n8n 產品也是有上限地串流讀取（request_body.read_body）
        yield await body()
    request = SimpleNamespace(url=SimpleNamespace(query=''), headers={}, body=body, stream=body_stream, method='POST',
        app=SimpleNamespace(state=SimpleNamespace(tool_registry=None)), scope={'root_path': ''})
    blocked = asyncio.Event(); release = asyncio.Event(); delivered = []
    async def send(message):
        if message['type'] == 'http.response.body' and message.get('body'):
            blocked.set()
            await release.wait()
            delivered.append(message['body'])
    async def receive():
        await asyncio.Event().wait()
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        monkeypatch.setattr(proxy, 'get_config_store', lambda: fresh_store)
        monkeypatch.setattr(proxy, '_get_client', lambda *args: client)
        response = await proxy.mcp_proxy('shared', 'mcp', request)
        task = asyncio.create_task(response({'type':'http', 'asgi':{'spec_version':'2.4'}}, receive, send))
        await asyncio.wait_for(blocked.wait(), 2)
        await fresh_store.change_mcp_token(None, action='revoke')
        with pytest.raises(ClientDisconnect):
            await asyncio.wait_for(task, 2)
        assert buffered or stream.closed
        release.set()
        await asyncio.sleep(0)
        assert delivered == []
