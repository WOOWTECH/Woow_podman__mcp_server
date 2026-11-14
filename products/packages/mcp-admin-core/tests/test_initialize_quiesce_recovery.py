"""Lifecycle steps under the configuration lock while an initialize dispatch is in flight (blocker 3, item 5).

A connection save that restarts the child, or the watchdog's restart after a crash, holds the transition lock
when it meets a still-registered initialize dispatch of the old generation. Synthetic gateway only: local ASGI,
``httpx.MockTransport`` upstream and fake process objects; no OS child process and no network.
"""
import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Response

from mcp_admin_core import configuration
from mcp_admin_core.config.store import get_config_store
from mcp_admin_core.routers import settings
from test_initialize_presend_readiness import INIT, TOKEN, fixture_gateway
from test_initialize_stop_recovery import FakeProcess

PORT = 59999  # fixture_gateway's configured child port (MockTransport: nothing listens there)


async def blocked_initialize(make_app, monkeypatch, *, stubborn):
    """An initialize whose upstream reply is back and whose outer header send is blocked by the client.

    ``calls`` records every upstream request as (JSON-RPC id, the manager's process object at that moment).
    """
    calls = []
    box = {}

    async def upstream(request):
        message = json.loads(request.content)
        calls.append((message.get('id'), box['pm']._process))
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': message.get('id'), 'result': {
            'protocolVersion': '2025-03-26', 'capabilities': {}, 'serverInfo': {'name': 'synthetic', 'version': '1'}}})

    app, pm, ready, probe = fixture_gateway(make_app, monkeypatch, upstream)
    box['pm'] = pm
    ready.set()
    await probe
    pm._process = FakeProcess()
    monkeypatch.setattr(settings, 'get_process_manager', lambda: pm)
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    delivered = []
    messages = asyncio.Queue()
    await messages.put({'type': 'http.request', 'body': json.dumps(INIT).encode(), 'more_body': False})

    async def receive():
        return await messages.get()

    async def send(event):
        if event['type'] == 'http.response.start':
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    if not stubborn:
                        raise
        delivered.append(event)

    path = '/private_' + TOKEN + '/mcp'
    scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
             'method': 'POST', 'scheme': 'http', 'path': path, 'raw_path': path.encode(),
             'query_string': b'', 'headers': [(b'host', b'test'), (b'content-type', b'application/json')],
             'client': ('127.0.0.1', 1234), 'server': ('test', 80), 'root_path': ''}
    task = asyncio.create_task(app(scope, receive, send))
    await asyncio.wait_for(entered.wait(), 1)
    return SimpleNamespace(app=app, pm=pm, task=task, cancelled=cancelled, release=release,
                           delivered=delivered, calls=calls)


def synthetic_starter(pm, started):
    """Replace ``_start_locked``: a new fake child and generation, marked as identity-verified for PORT."""
    async def start_locked():
        from mcp_admin_core.configuration import launch_revision
        store = get_config_store()
        proc = FakeProcess(5252 + len(started))
        started.append(proc)
        pm._process = proc
        pm._initialize_generation_changed = asyncio.Event()
        pm._running = True
        pm._intentional_stop = False
        pm._attempted_revision = pm._applied_revision = launch_revision(store, await store.load())
        # Synthetic stand-in for the probe's OS listener check (a FakeProcess owns no socket).
        pm._peer_verified = (pm._initialize_generation_changed, PORT)
        return True
    return start_locked


async def save_connection(pm):
    store = get_config_store()
    revision = configuration.configuration_status(store, await store.load(), pm)['revision']
    request = SimpleNamespace(state=SimpleNamespace(), headers={},
                              app=SimpleNamespace(state=SimpleNamespace(admin_auth_required=False)))
    return await settings.put_section('connection', {'synthetic_key': f'synthetic-{time.time_ns()}'}, request,
                                      Response(), '"' + revision + '"')


async def initialize(app, rpc_id):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        return await client.post('/private_' + TOKEN + '/mcp', json={**INIT, 'id': rpc_id})


async def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


async def finish(gate):
    gate.release.set()
    gate.task.cancel()
    await asyncio.gather(gate.task, return_exceptions=True)
    await gate.pm.stop()


@pytest.mark.asyncio
async def test_connection_save_with_cooperative_inflight_initialize_converges(make_app, monkeypatch):
    gate = await blocked_initialize(make_app, monkeypatch, stubborn=False)
    pm, old, started = gate.pm, gate.pm._process, []
    monkeypatch.setattr(pm, '_start_locked', synthetic_starter(pm, started))
    try:
        result = await asyncio.wait_for(save_connection(pm), 3)
        assert result['saved'] and result['status'] == 'ok' and result['apply_state'] == 'applied', result
        await asyncio.wait_for(asyncio.gather(gate.task, return_exceptions=True), 1)
        assert gate.cancelled.is_set() and gate.delivered == [] and old.signals
        assert len(started) == 1 and pm._process is started[0] and not pm._initialize_dispatches
        response = await initialize(gate.app, 2)
        assert response.status_code == 200
        assert gate.calls == [(1, old), (2, started[0])]
    finally:
        await finish(gate)


@pytest.mark.asyncio
async def test_connection_save_with_uncooperative_inflight_initialize_stays_fenced_then_converges(
        make_app, monkeypatch):
    gate = await blocked_initialize(make_app, monkeypatch, stubborn=True)
    pm, old, started = gate.pm, gate.pm._process, []
    monkeypatch.setattr(pm, '_start_locked', synthetic_starter(pm, started))
    try:
        result = await asyncio.wait_for(save_connection(pm), 3)
        # The restart cannot finish while the old generation's dispatch is registered: reported, not hidden.
        assert result['saved'] and result['apply_state'] == 'failed', result
        await asyncio.wait_for(gate.cancelled.wait(), 1)
        assert not old.signals and not started
        assert pm._process is old and pm.initialize_generation()[1].is_set()
        blocked = await initialize(gate.app, 3)
        assert blocked.status_code == 503 and gate.calls == [(1, old)]

        gate.release.set()
        await asyncio.wait_for(asyncio.gather(gate.task, return_exceptions=True), 1)
        # Converges by itself once the dispatch ends: the fenced child is stopped, then replaced.
        assert await wait_until(lambda: started and pm._process is started[0])
        assert old.signals and not pm._initialize_dispatches
        response = await initialize(gate.app, 4)
        assert response.status_code == 200
        # Only the old dispatch ever reached the old child; the replacement only saw the new initialize.
        assert gate.calls == [(1, old), (4, started[0])]
        # A sender that swallowed cancellation may still deliver its old headers (documented residual).
        assert any(event['type'] == 'http.response.start' for event in gate.delivered)
    finally:
        await finish(gate)


@pytest.mark.asyncio
async def test_watchdog_restart_waits_for_uncooperative_dispatch_then_converges(make_app, monkeypatch):
    from mcp_admin_core import process as process_mod
    gate = await blocked_initialize(make_app, monkeypatch, stubborn=True)
    pm, old = gate.pm, gate.pm._process
    spawned = []

    class Child:
        stdout = None
        def __init__(self):
            self.pid, self.returncode, self.signals = 6000 + len(spawned), None, []
        def send_signal(self, sig):
            self.signals.append(sig)
            self.returncode = 0
        def kill(self):
            self.send_signal(9)
        async def wait(self):
            return self.returncode

    async def spawn(*argv, **kwargs):
        spawned.append(Child())
        return spawned[-1]

    async def synthetic_probe(self, proc, snapshot, revision):
        self._applied_revision = revision
        self._peer_verified = (self._initialize_generation_changed, PORT)
        self._verifying_configuration = False

    monkeypatch.setattr(process_mod.asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(process_mod.McpProcessManager, '_verify_configuration', synthetic_probe)
    pm.auto_restart_delays = (0.05,)
    await get_config_store().patch('mcp_server', {'command': 'synthetic-child', 'args': []})
    try:
        # The old child crashes while its initialize dispatch is still blocked in the client's header send.
        old.returncode = 1
        pm._invalidate_initialize_generation()
        pm._running = False
        pm._schedule_auto_restart()
        await asyncio.wait_for(gate.cancelled.wait(), 1)
        await asyncio.sleep(0.8)  # the watchdog attempt has met the registered dispatch by now
        assert not spawned and not pm.is_running
        gate.release.set()
        await asyncio.wait_for(asyncio.gather(gate.task, return_exceptions=True), 1)
        assert await wait_until(lambda: spawned and pm.is_running)
        response = await initialize(gate.app, 5)
        assert response.status_code == 200 and gate.calls == [(1, old), (5, spawned[0])]
    finally:
        await finish(gate)
