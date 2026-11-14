"""Local ASGI controls for cancellation and operation-journal recovery."""
import asyncio
import json
import time
from types import SimpleNamespace
import httpx
import pytest
from fastapi import HTTPException, Response
from mcp_admin_core import configuration
from mcp_admin_core.config.store import get_config_store
from mcp_admin_core.routers import settings
from test_initialize_presend_readiness import INIT, TOKEN, fixture_gateway

class FakeProcess:
    def __init__(self, pid=4242):
        self.pid, self.returncode, self.signals = pid, None, []
    def send_signal(self, sig):
        self.signals.append(sig)
        self.returncode = 0
    async def wait(self):
        return self.returncode

async def blocked_header(make_app, monkeypatch, *, stubborn=False):
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    delivered = []
    async def upstream(request):
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': 1, 'result': {}})
    app, pm, ready, probe = fixture_gateway(make_app, monkeypatch, upstream)
    ready.set()
    await probe
    pm._process = FakeProcess()
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
    return pm, task, cancelled, release, delivered

@pytest.mark.asyncio
async def test_cooperative_header_cancel_allows_stop(make_app, monkeypatch):
    pm, task, cancelled, release, delivered = await blocked_header(make_app, monkeypatch)
    proc = pm._process
    try:
        await pm.stop()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
        assert cancelled.is_set() and proc.signals and not pm.is_running
        assert not pm._initialize_dispatches and delivered == []
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

@pytest.mark.asyncio
async def test_stubborn_header_fences_then_new_operation_recovers(make_app, monkeypatch):
    pm, task, cancelled, release, delivered = await blocked_header(make_app, monkeypatch, stubborn=True)
    proc = pm._process
    monkeypatch.setattr(settings, 'get_process_manager', lambda: pm)
    async def fake_start():
        pm._process = FakeProcess(5252)
        pm._initialize_generation_changed = asyncio.Event()
        pm._running = True
        return True
    monkeypatch.setattr(pm, '_start_locked', fake_start)
    store = get_config_store()
    async def restart(operation_id):
        revision = configuration.configuration_status(store, await store.load(), pm)['revision']
        request = SimpleNamespace(state=SimpleNamespace(),
            app=SimpleNamespace(state=SimpleNamespace(admin_auth_required=False)),
            headers={'Idempotency-Key': operation_id})
        return await settings.mcp_restart(response=Response(), if_match='"' + revision + '"', request=request)
    prefix = str(int(time.time() * 1000))
    old_id, blocked_id, new_id = (prefix + '.synthetic-operation-a',
                                  prefix + '.synthetic-operation-b', prefix + '.synthetic-operation-c')
    try:
        with pytest.raises(HTTPException) as exc:
            await restart(old_id)
        assert exc.value.status_code == 409 and exc.value.detail['code'] == 'operation_outcome_unknown'
        await asyncio.wait_for(cancelled.wait(), 1)
        assert not proc.signals and pm.is_running and pm.initialize_generation()[1].is_set()
        with pytest.raises(HTTPException) as exc:
            await restart(old_id)
        assert exc.value.status_code == 409 and exc.value.detail['code'] == 'operation_outcome_unknown'
        assert pm._process is proc and not proc.signals
        with pytest.raises(HTTPException) as exc:
            await restart(blocked_id)
        assert exc.value.status_code == 409 and exc.value.detail['code'] == 'operation_outcome_unknown'
        assert pm._process is proc and not proc.signals
        release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
        assert not pm._initialize_dispatches
        # A sender that swallows cancellation can still deliver old headers.
        assert any(event['type'] == 'http.response.start' for event in delivered)
        result = await restart(new_id)
        assert result['status'] == 'ok' and proc.signals and pm._process is not proc
        with pytest.raises(HTTPException) as exc:
            await restart(old_id)
        assert exc.value.status_code == 409 and exc.value.detail['code'] == 'operation_outcome_unknown'
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

@pytest.mark.asyncio
async def test_cooperative_header_allows_journalled_restart(make_app, monkeypatch):
    pm, task, cancelled, release, delivered = await blocked_header(make_app, monkeypatch)
    proc = pm._process
    monkeypatch.setattr(settings, 'get_process_manager', lambda: pm)
    async def fake_start():
        pm._process = FakeProcess(5252)
        pm._initialize_generation_changed = asyncio.Event()
        pm._running = True
        return True
    monkeypatch.setattr(pm, '_start_locked', fake_start)
    store = get_config_store()
    revision = configuration.configuration_status(store, await store.load(), pm)['revision']
    request = SimpleNamespace(state=SimpleNamespace(), app=SimpleNamespace(state=SimpleNamespace(admin_auth_required=False)),
                              headers={'Idempotency-Key': str(int(time.time() * 1000)) + '.synthetic-restart-a'})
    try:
        result = await settings.mcp_restart(response=Response(), if_match='"' + revision + '"', request=request)
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
        assert result['status'] == 'ok' and result['operation']['state'] == 'completed'
        assert cancelled.is_set() and proc.signals and pm._process is not proc and delivered == []
        replay = await settings.mcp_restart(response=Response(), if_match='"' + revision + '"', request=request)
        assert replay['operation']['replayed'] and replay['operation']['state'] == 'completed'
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

@pytest.mark.asyncio
async def test_restart_rechecks_revision_after_unlocked_drain(make_app, monkeypatch):
    pm, task, cancelled, release, delivered = await blocked_header(make_app, monkeypatch, stubborn=True)
    proc = pm._process
    monkeypatch.setattr(settings, 'get_process_manager', lambda: pm)
    store = get_config_store()
    revision = configuration.configuration_status(store, await store.load(), pm)['revision']
    request = SimpleNamespace(state=SimpleNamespace(), app=SimpleNamespace(state=SimpleNamespace(admin_auth_required=False)),
                              headers={'Idempotency-Key': str(int(time.time() * 1000)) + '.synthetic-cas-test'})
    operation = asyncio.create_task(settings.mcp_restart(response=Response(), if_match='"' + revision + '"', request=request))
    try:
        await asyncio.wait_for(cancelled.wait(), 1)
        # Acquiring transition here proves drain is not holding it.
        async with asyncio.timeout(.15):
            async with store.transition():
                await store.patch('connection', {'synthetic': 'changed'})
        release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
        with pytest.raises(HTTPException) as exc:
            await operation
        assert exc.value.status_code == 409 and exc.value.detail['code'] == 'operation_outcome_unknown'
        assert pm._process is proc and not proc.signals and pm.initialize_generation()[1].is_set()
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, operation, return_exceptions=True)

@pytest.mark.asyncio
async def test_restart_rereads_journal_after_unlocked_drain(make_app, monkeypatch):
    from mcp_admin_core.operations import OperationJournal
    pm, task, cancelled, release, delivered = await blocked_header(make_app, monkeypatch, stubborn=True)
    monkeypatch.setattr(settings, 'get_process_manager', lambda: pm)
    async def fake_start():
        pm._process = FakeProcess(5252)
        pm._initialize_generation_changed = asyncio.Event()
        pm._running = True
        return True
    monkeypatch.setattr(pm, '_start_locked', fake_start)
    store = get_config_store()
    revision = configuration.configuration_status(store, await store.load(), pm)['revision']
    prefix = str(int(time.time() * 1000))
    first, second = prefix + '.synthetic-first-op', prefix + '.synthetic-second-op'
    request = SimpleNamespace(state=SimpleNamespace(), app=SimpleNamespace(state=SimpleNamespace(admin_auth_required=False)),
                              headers={'Idempotency-Key': first})
    operation = asyncio.create_task(settings.mcp_restart(response=Response(), if_match='"' + revision + '"', request=request))
    try:
        await asyncio.wait_for(cancelled.wait(), 1)
        async with store.transition():
            journal = OperationJournal(store.path)
            data, _, fingerprint = journal.inspect(second, 'token.set', {})
            journal.begin(second, data, 'token.set', fingerprint)
            journal.complete(second, data, {'status': 'ok'})
        release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
        result = await operation
        assert result['operation']['state'] == 'completed'
        async with store.transition():
            assert journal.query(first)['operation']['state'] == 'completed'
            assert journal.query(second)['operation']['state'] == 'completed'
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, operation, return_exceptions=True)

@pytest.mark.asyncio
async def test_restart_cancelled_caller_finishes_accepted_operation(make_app, monkeypatch):
    from mcp_admin_core.operations import OperationJournal
    pm, task, cancelled, release, delivered = await blocked_header(make_app, monkeypatch, stubborn=True)
    monkeypatch.setattr(settings, 'get_process_manager', lambda: pm)
    async def fake_start():
        pm._process = FakeProcess(5252)
        pm._initialize_generation_changed = asyncio.Event()
        pm._running = True
        return True
    monkeypatch.setattr(pm, '_start_locked', fake_start)
    store = get_config_store()
    revision = configuration.configuration_status(store, await store.load(), pm)['revision']
    op_id = str(int(time.time() * 1000)) + '.synthetic-cancel-op'
    request = SimpleNamespace(state=SimpleNamespace(), app=SimpleNamespace(state=SimpleNamespace(admin_auth_required=False)),
                              headers={'Idempotency-Key': op_id})
    operation = asyncio.create_task(settings.mcp_restart(response=Response(), if_match='"' + revision + '"', request=request))
    try:
        await asyncio.wait_for(cancelled.wait(), 1)
        operation.cancel()
        release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(operation, 1)
        async with store.transition():
            assert OperationJournal(store.path).query(op_id)['operation']['state'] == 'completed'
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, operation, return_exceptions=True)

@pytest.mark.asyncio
async def test_restart_rejects_replacement_during_unlocked_drain(make_app, monkeypatch):
    pm, task, cancelled, release, delivered = await blocked_header(make_app, monkeypatch, stubborn=True)
    old = pm._process
    monkeypatch.setattr(settings, 'get_process_manager', lambda: pm)
    store = get_config_store()
    revision = configuration.configuration_status(store, await store.load(), pm)['revision']
    request = SimpleNamespace(state=SimpleNamespace(), app=SimpleNamespace(state=SimpleNamespace(admin_auth_required=False)),
                              headers={'Idempotency-Key': str(int(time.time() * 1000)) + '.synthetic-generation'})
    operation = asyncio.create_task(settings.mcp_restart(response=Response(), if_match='"' + revision + '"', request=request))
    try:
        await asyncio.wait_for(cancelled.wait(), 1)
        replacement = FakeProcess(5252)
        async with store.transition():
            pm._process = replacement
            pm._initialize_generation_changed = asyncio.Event()
        release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
        with pytest.raises(HTTPException) as exc:
            await operation
        assert exc.value.detail['code'] == 'operation_outcome_unknown'
        assert not old.signals and not replacement.signals and pm._process is replacement
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, operation, return_exceptions=True)

@pytest.mark.asyncio
async def test_interrupted_drain_keeps_receipt_unknown_and_new_id_can_recover(make_app, monkeypatch):
    from mcp_admin_core.operations import OperationJournal
    pm, task, cancelled, release, delivered = await blocked_header(make_app, monkeypatch)
    monkeypatch.setattr(settings, 'get_process_manager', lambda: pm)
    real_drain = pm.drain_initializes
    async def interrupted(ticket):
        raise asyncio.CancelledError()
    monkeypatch.setattr(pm, 'drain_initializes', interrupted)
    async def fake_start():
        pm._process = FakeProcess(5252)
        pm._initialize_generation_changed = asyncio.Event()
        pm._running = True
        return True
    monkeypatch.setattr(pm, '_start_locked', fake_start)
    store = get_config_store()
    prefix = str(int(time.time() * 1000))
    old_id, new_id = prefix + '.synthetic-crash-a', prefix + '.synthetic-crash-b'
    async def restart(op_id):
        revision = configuration.configuration_status(store, await store.load(), pm)['revision']
        request = SimpleNamespace(state=SimpleNamespace(), app=SimpleNamespace(state=SimpleNamespace(admin_auth_required=False)),
                                  headers={'Idempotency-Key': op_id})
        return await settings.mcp_restart(response=Response(), if_match='"' + revision + '"', request=request)
    try:
        with pytest.raises(asyncio.CancelledError):
            await restart(old_id)
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
        async with store.transition():
            assert OperationJournal(store.path).query(old_id)['operation']['state'] == 'unknown'
        with pytest.raises(HTTPException) as exc:
            await restart(old_id)
        assert exc.value.detail['code'] == 'operation_outcome_unknown'
        monkeypatch.setattr(pm, 'drain_initializes', real_drain)
        result = await restart(new_id)
        assert result['operation']['state'] == 'completed'
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

@pytest.mark.asyncio
async def test_profile_b_restart_completes_while_profile_a_drains(make_app, monkeypatch):
    from mcp_admin_core import process
    from mcp_admin_core.profiles import get_profile_registry
    make_app({'mcp_auth_token': TOKEN, 'mcp_server': {'command': '', 'args': [], 'port': 59999, 'env': {}}})
    registry = get_profile_registry()
    a = (await registry.create('Synthetic A'))['id']
    b = (await registry.create('Synthetic B'))['id']
    pm_a, pm_b = registry.manager_for(a), registry.manager_for(b)
    pm_a._process, pm_b._process = FakeProcess(4242), FakeProcess(4343)
    old_a, old_b = pm_a._process, pm_b._process
    pm_a._running = pm_b._running = True
    async def start_a():
        pm_a._process = FakeProcess(5252)
        pm_a._initialize_generation_changed = asyncio.Event()
        pm_a._running = True
        return True
    async def start_b():
        pm_b._process = FakeProcess(5353)
        pm_b._initialize_generation_changed = asyncio.Event()
        pm_b._running = True
        return True
    monkeypatch.setattr(pm_a, '_start_locked', start_a)
    monkeypatch.setattr(pm_b, '_start_locked', start_b)
    monkeypatch.setattr(process, 'INITIALIZE_DRAIN_TIMEOUT', 1.5)
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def dispatch_a():
        assert pm_a.begin_initialize_dispatch(pm_a.initialize_generation())
        entered.set()
        try:
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
        finally:
            pm_a.end_initialize_dispatch()
    dispatch = asyncio.create_task(dispatch_a())
    await entered.wait()
    prefix = str(int(time.time() * 1000))
    async def restart(profile, op_id):
        with registry.activate(profile):
            store = registry.store_for(profile)
            pm = registry.manager_for(profile)
            revision = configuration.configuration_status(store, await store.load(), pm)['revision']
            request = SimpleNamespace(state=SimpleNamespace(), app=SimpleNamespace(state=SimpleNamespace(admin_auth_required=False)),
                                      headers={'Idempotency-Key': op_id})
            return await settings.mcp_restart(response=Response(), if_match='"' + revision + '"', request=request)
    op_a = asyncio.create_task(restart(a, prefix + '.synthetic-profile-a'))
    try:
        await asyncio.wait_for(cancelled.wait(), 1)
        result_b = await asyncio.wait_for(restart(b, prefix + '.synthetic-profile-b'), .7)
        assert result_b['operation']['state'] == 'completed' and old_b.signals
        assert not op_a.done() and not old_a.signals
        release.set()
        await asyncio.wait_for(dispatch, 1)
        result_a = await asyncio.wait_for(op_a, 1)
        assert result_a['operation']['state'] == 'completed' and old_a.signals
    finally:
        release.set()
        dispatch.cancel()
        await asyncio.gather(dispatch, op_a, return_exceptions=True)

@pytest.mark.asyncio
async def test_same_and_new_id_during_drain_preserve_receipts(make_app, monkeypatch):
    from mcp_admin_core import process
    from mcp_admin_core.operations import OperationJournal
    pm, task, cancelled, release, delivered = await blocked_header(make_app, monkeypatch, stubborn=True)
    monkeypatch.setattr(settings, 'get_process_manager', lambda: pm)
    monkeypatch.setattr(process, 'INITIALIZE_DRAIN_TIMEOUT', 1.5)
    async def fake_start():
        pm._process = FakeProcess(5252)
        pm._initialize_generation_changed = asyncio.Event()
        pm._running = True
        return True
    monkeypatch.setattr(pm, '_start_locked', fake_start)
    store = get_config_store()
    revision = configuration.configuration_status(store, await store.load(), pm)['revision']
    prefix = str(int(time.time() * 1000))
    first, second = prefix + '.synthetic-same-id', prefix + '.synthetic-new-id-a'
    async def restart(op_id):
        request = SimpleNamespace(state=SimpleNamespace(), app=SimpleNamespace(state=SimpleNamespace(admin_auth_required=False)),
                                  headers={'Idempotency-Key': op_id})
        return await settings.mcp_restart(response=Response(), if_match='"' + revision + '"', request=request)
    operation = asyncio.create_task(restart(first))
    try:
        await asyncio.wait_for(cancelled.wait(), 1)
        for op_id in (first, second):
            with pytest.raises(HTTPException) as exc:
                await restart(op_id)
            assert exc.value.detail['code'] == 'operation_outcome_unknown'
        release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
        result = await operation
        assert result['operation']['state'] == 'completed'
        async with store.transition():
            journal = OperationJournal(store.path)
            assert journal.query(first)['operation']['state'] == 'completed'
            assert journal.query(second)['operation']['state'] == 'unknown'
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, operation, return_exceptions=True)
