"""設定版本、套用狀態與取消請求的真實管理入口回歸。"""
from management_client import revision_headers
import asyncio
import json
import importlib
import sys
import subprocess
import textwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import httpx

from mcp_admin_core.routers import settings

PRODUCTS = ['hermes', 'n8n', 'odoo', 'k8s', 'vk', 'opendesign', 'litellm', 'nextcloud']


def listening_child():
    """A real child that owns its loopback listener, so the manager's OS identity check passes (blocker 3).

    The MCP answers themselves come from the faked ``discover_tools``. Returns the ``mcp_server`` section.
    """
    import socket
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    code = ('import socket, time; s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); '
            f's.bind(("127.0.0.1", {port})); s.listen(); time.sleep(30)')
    return {'command': sys.executable, 'args': ['-c', code], 'port': port}


def product_payload(module):
    model = next(getattr(module, name) for name in ('ConnectionUpdateRequest', 'ConnectionUpdate', 'DualConnectionUpdate') if hasattr(module, name))
    values = {name: '' for name, field in model.model_fields.items() if field.is_required()}
    values.update(restart=True)
    if 'dashboard_username' in model.model_fields:
        values['dashboard_username'] = 'runtime-parity-editor'
    if 'in_cluster' in model.model_fields:
        values['in_cluster'] = 'false'
    return model(**values).model_dump()


@pytest.mark.parametrize('product', PRODUCTS)
def test_each_real_product_route_enforces_snapshot_revision(product, make_app, tmp_config_path, monkeypatch):
    module = importlib.import_module(product + '_mcp_admin.routers.config')
    action = AsyncMock(return_value=False)
    pm = SimpleNamespace(is_running=False, start=action, restart=action, start_or_restart=action)
    monkeypatch.setattr(module, 'get_process_manager', lambda: pm)
    if hasattr(getattr(module, 'tool_registry', None), 'refresh'):
        monkeypatch.setattr(module.tool_registry, 'refresh', AsyncMock())
    client = make_app({'connection': {'unchanged_secret': 'retained-fixture-secret'}})
    client.app.include_router(module.router)
    headers = auth(client)
    first = client.get('/api/config', headers=headers)
    assert first.status_code == 200
    state = first.json()['configuration']
    assert first.headers['etag'] == '"' + state['revision'] + '"'
    conditional = {**headers, 'If-Match': first.headers['etag']}
    saved = client.put('/api/config/connection', headers=conditional, json=product_payload(module))
    assert saved.status_code == 200
    assert saved.json()['configuration']['revision'] != state['revision']
    before = tmp_config_path.read_bytes()
    conflict = client.put('/api/config/connection', headers=conditional, json=product_payload(module))
    assert conflict.status_code == 409
    assert conflict.json()['detail']['code'] == 'config_conflict'
    assert 'retained-fixture-secret' not in conflict.text
    assert tmp_config_path.read_bytes() == before
    action.assert_awaited_once()


def test_revision_is_from_get_snapshot_even_if_config_changes_during_get(make_app, monkeypatch):
    import n8n_mcp_admin.routers.config as module
    from mcp_admin_core.config import get_config_store
    client = make_app({'connection': {'n8n_api_url': 'old-snapshot'}})
    client.app.include_router(module.router)
    async def refresh():
        await get_config_store().patch('connection', {'n8n_api_url': 'new-snapshot'})
    monkeypatch.setattr(module.tool_registry, 'refresh', refresh)
    headers = auth(client)
    old = client.get('/api/settings', headers=headers).headers['etag']
    result = client.get('/api/config', headers=headers)
    assert result.json()['n8n_api_url'] == 'old-snapshot'
    assert result.headers['etag'] == old
    assert client.get('/api/settings', headers=headers).headers['etag'] != old


async def test_late_probe_cannot_mark_newer_save_applied(store_env, monkeypatch):
    import mcp_admin_core.capabilities as capabilities
    from mcp_admin_core.configuration import configuration_status
    from mcp_admin_core.process import McpProcessManager
    from mcp_admin_core.launch_policy import set_launch_policy
    set_launch_policy(None)
    await store_env.put('mcp_server', listening_child())
    entered, release = asyncio.Event(), asyncio.Event()
    async def probe(snapshot):
        entered.set()
        await release.wait()
        return []
    monkeypatch.setattr(capabilities, 'discover_tools', probe)
    pm = McpProcessManager()
    try:
        assert await pm.start()
        await asyncio.wait_for(entered.wait(), 2)
        old_revision = pm._attempted_revision
        await store_env.patch('connection', {'url': 'new-config-not-applied'})
        release.set()
        task = pm._configuration_task
        if task:
            await asyncio.wait_for(task, 2)
        state = configuration_status(store_env, await store_env.load(), pm)
        assert state['applied_revision'] == old_revision
        assert state['saved_revision'] != old_revision
        assert state['apply_state'] == 'pending'
    finally:
        release.set()
        await pm.stop()


async def test_two_http_editors_are_serialized_and_only_first_replaces_child(make_app, monkeypatch, tmp_config_path):
    import hermes_mcp_admin.routers.config as module
    entered, release = asyncio.Event(), asyncio.Event()
    async def start():
        entered.set()
        await release.wait()
        return True
    action = AsyncMock(side_effect=start)
    monkeypatch.setattr(module, 'get_process_manager', lambda: SimpleNamespace(is_running=False, start=action))
    setup = make_app()
    setup.app.include_router(module.router)
    headers = auth(setup)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=setup.app), base_url='http://test', headers=headers) as client:
        initial = await client.get('/api/config')
        condition = {'If-Match': initial.headers['etag']}
        first = asyncio.create_task(client.put('/api/config/connection', headers=condition, json={'dashboard_username': 'first'}))
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(client.put('/api/config/connection', headers=condition, json={'dashboard_username': 'second'}))
        await asyncio.sleep(0)
        assert not second.done()
        release.set()
        a, b = await asyncio.wait_for(asyncio.gather(first, second), 3)
    assert a.status_code == 200 and b.status_code == 409
    assert json.loads(tmp_config_path.read_text())['connection']['dashboard_username'] == 'first'
    action.assert_awaited_once()


def test_commit_failure_has_no_apply_or_secret_error(make_app, monkeypatch, tmp_config_path):
    from mcp_admin_core.config import get_config_store
    action = AsyncMock(return_value=True)
    monkeypatch.setattr(settings, 'get_process_manager', lambda: SimpleNamespace(is_running=False, start_or_restart=action))
    client = make_app()
    headers = auth(client)
    initial = client.get('/api/settings', headers=headers)
    before = tmp_config_path.read_bytes()
    def fail(_data):
        raise OSError('private-fixture-upstream-credential')
    monkeypatch.setattr(get_config_store(), '_write_sync', fail)
    response = client.put('/api/settings/connection', headers={**headers, 'If-Match': initial.headers['etag']}, json={'url': 'new'})
    assert response.status_code == 500
    assert response.json()['detail']['code'] == 'config_save_failed'
    assert 'private-fixture' not in response.text
    assert tmp_config_path.read_bytes() == before
    assert client.get('/api/settings', headers=headers).headers['etag'] == initial.headers['etag']
    action.assert_not_awaited()


async def test_immediate_exit_is_never_applied_and_error_survives_reads(store_env, monkeypatch):
    import mcp_admin_core.capabilities as capabilities
    from mcp_admin_core.configuration import configuration_status
    from mcp_admin_core.process import McpProcessManager
    from mcp_admin_core.launch_policy import set_launch_policy
    set_launch_policy(None)
    await store_env.put('mcp_server', {'command': sys.executable, 'args': ['-c', 'raise SystemExit(1)']})
    async def unavailable(_snapshot):
        raise RuntimeError('private-child-detail')
    monkeypatch.setattr(capabilities, 'discover_tools', unavailable)
    pm = McpProcessManager()
    pm.auto_restart_delays = (30,)
    try:
        assert await pm.start()
        async with asyncio.timeout(3):
            while pm._configuration_error != 'child_exited':
                await asyncio.sleep(0.01)
        for _ in range(2):
            state = configuration_status(store_env, await store_env.load(), pm)
            assert state['apply_state'] == 'failed'
            assert state['applied_revision'] is None
            assert state['last_error_code'] == 'child_exited'
            assert 'private-child-detail' not in json.dumps(state)
    finally:
        await pm.stop()


async def test_readiness_failure_is_bounded_and_retry_of_applied_revision_is_noop(store_env, monkeypatch):
    import mcp_admin_core.capabilities as capabilities
    from mcp_admin_core.configuration import configuration_status
    from mcp_admin_core.process import McpProcessManager
    from mcp_admin_core.launch_policy import set_launch_policy
    set_launch_policy(None)
    await store_env.put('mcp_server', listening_child())
    async def unavailable(_snapshot):
        raise RuntimeError('private-probe-detail')
    monkeypatch.setattr(capabilities, 'discover_tools', unavailable)
    pm = McpProcessManager()
    pm.configuration_probe_timeout = 0.05
    monkeypatch.setattr(settings, 'get_process_manager', lambda: pm)
    try:
        assert await pm.start()
        await asyncio.wait_for(pm._configuration_task, 2)
        state = configuration_status(store_env, await store_env.load(), pm)
        assert pm.is_running and state['apply_state'] == 'failed'
        assert state['last_error_code'] == 'child_probe_failed'
        monkeypatch.setattr(capabilities, 'discover_tools', AsyncMock(return_value=[]))
        # The retried child must really be listening before its probe can pass (blocker 3): allow its startup.
        pm.configuration_probe_timeout = 10
        await settings.mcp_apply(None)
        if pm._configuration_task is not None:
            await asyncio.wait_for(pm._configuration_task, 2)
        pid = pm._process.pid
        state = configuration_status(store_env, await store_env.load(), pm)
        assert state['apply_state'] == 'applied'
        assert state['applied_revision'] == state['saved_revision']
        assert state['last_error_code'] is None
        result = await settings.mcp_apply(None, '"' + state['revision'] + '"')
        assert result['configuration']['apply_state'] == 'applied'
        assert pm._process.pid == pid
    finally:
        await pm.stop()


def test_auth_precedes_conditional_error_and_revision_is_invalidated_by_admin_restart(make_app, tmp_config_path, monkeypatch):
    from mcp_admin_core.config.store import ConfigStore
    import mcp_admin_core.config.store as store_module
    client = make_app()
    headers = auth(client)
    old = client.get('/api/settings', headers=headers).headers['etag']
    client.cookies.clear()
    assert client.put('/api/settings/connection', headers={'If-Match': old}, json={'url': 'unauthorized'}).status_code == 401
    before = tmp_config_path.read_bytes()
    monkeypatch.setattr(store_module, '_instance', ConfigStore(tmp_config_path))
    new = client.get('/api/settings', headers=headers).headers['etag']
    assert new != old
    response = client.put('/api/settings/connection', headers={**headers, 'If-Match': old}, json={'url': 'stale'})
    assert response.status_code == 409
    assert tmp_config_path.read_bytes() == before


def auth(client):
    token = client.post('/api/auth/login', json={'password': 'admin'}).json()['token']
    return {'Authorization': f'Bearer {token}'}


def test_settings_snapshot_has_opaque_revision_without_rewriting_state(make_app, tmp_config_path):
    client = make_app({'connection': {'password': 'private-fixture-credential'}})
    headers = auth(client)
    before = tmp_config_path.read_bytes()
    response = client.get('/api/settings', headers=headers)
    state = response.json()['configuration']
    assert response.headers['etag'] == '"' + state['revision'] + '"'
    assert state['saved_revision'] and state['applied_revision'] is None
    assert 'private-fixture-credential' not in json.dumps(state)
    assert tmp_config_path.read_bytes() == before


def test_stale_settings_save_rejected_before_write_or_restart(make_app, tmp_config_path, monkeypatch):
    pm = SimpleNamespace(is_running=False, start_or_restart=AsyncMock(return_value=False))
    monkeypatch.setattr(settings, 'get_process_manager', lambda: pm)
    client = make_app()
    headers = auth(client)
    first = client.get('/api/settings', headers=headers)
    conditional = {**headers, 'If-Match': first.headers.get('etag', '"missing-revision"')}
    assert client.put('/api/settings/connection', headers=conditional, json={'url': 'first'}).status_code == 200
    before = tmp_config_path.read_bytes()
    response = client.put('/api/settings/connection', headers=conditional, json={'url': 'stale'})
    assert response.status_code == 409
    assert response.json()['detail']['code'] == 'config_conflict'
    assert tmp_config_path.read_bytes() == before
    pm.start_or_restart.assert_awaited_once()


def test_spawn_failure_remains_visible_on_reload(make_app, monkeypatch):
    from mcp_admin_core.process import McpProcessManager
    pm = McpProcessManager()
    monkeypatch.setattr(settings, 'get_process_manager', lambda: pm)
    client = make_app({'mcp_server': {'command': '/missing-runtime-parity-child'}})
    headers = auth(client)
    result = client.put('/api/settings/connection', headers=revision_headers(client, headers), json={'url': 'saved'}).json()
    assert result['saved'] is True
    reloaded = client.get('/api/settings', headers=headers).json()['configuration']
    assert reloaded['apply_state'] == 'failed'
    assert reloaded['applied_revision'] is None
    assert reloaded['last_error_code'] == 'child_start_failed'


async def test_cancelled_caller_finishes_committed_transition_before_next_editor(store_env):
    from mcp_admin_core.configuration import run_configuration_transition
    started, release = asyncio.Event(), asyncio.Event()
    pm = SimpleNamespace(is_running=False)
    events = []

    async def first():
        await store_env.patch('connection', {'url': 'first'})
        started.set()
        await release.wait()
        events.append('applied-first')
        return {'saved': True}

    async def second():
        events.append('saved-second')
        await store_env.patch('connection', {'url': 'second'})
        return {'saved': True}

    task = asyncio.create_task(run_configuration_transition(store_env, pm, None, first))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    newer = asyncio.create_task(run_configuration_transition(store_env, pm, None, second))
    await asyncio.sleep(0)
    assert not newer.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    await asyncio.wait_for(newer, 2)
    assert events == ['applied-first', 'saved-second']
    assert (await store_env.get('connection'))['url'] == 'second'


async def test_simultaneous_lifecycle_calls_spawn_only_one_child(store_env, monkeypatch):
    import mcp_admin_core.capabilities as capabilities
    from mcp_admin_core.process import McpProcessManager
    from mcp_admin_core.launch_policy import set_launch_policy
    set_launch_policy(None)
    await store_env.put('mcp_server', {'command': sys.executable, 'args': ['-c', 'import time; time.sleep(30)']})
    monkeypatch.setattr(capabilities, 'discover_tools', AsyncMock(return_value=[]))
    original_spawn = asyncio.create_subprocess_exec
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0
    async def spawn(*args, **kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return await original_spawn(*args, **kwargs)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    pm = McpProcessManager()
    first = asyncio.create_task(pm.start())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(pm.start())
        await asyncio.sleep(0)
        assert calls == 1 and not second.done()
        release.set()
        assert await asyncio.wait_for(asyncio.gather(first, second), 3) == [True, True]
        assert calls == 1
    finally:
        release.set()
        await first
        await pm.stop()


def test_anyio_scope_cancellation_finishes_accepted_transition_without_spin(tmp_path):
    # A faulty cancellation join can starve its own event loop. Run the real
    # helper in a subprocess so this regression also has an external deadline.
    code = textwrap.dedent('''
        import asyncio, anyio, sys
        from types import SimpleNamespace
        from mcp_admin_core.config.store import ConfigStore
        from mcp_admin_core.configuration import run_configuration_transition
        async def main():
            store = ConfigStore(sys.argv[1])
            saved, release = asyncio.Event(), asyncio.Event()
            scopes, completed = [], []
            original_shield, joins = asyncio.shield, []
            def counted_shield(task):
                joins.append(None)
                return original_shield(task)
            asyncio.shield = counted_shield
            async def operation():
                await store.patch('connection', {'url': 'accepted'})
                saved.set()
                await release.wait()
                completed.append('applied')
                return {'saved': True}
            async def request():
                with anyio.CancelScope() as scope:
                    scopes.append(scope)
                    await run_configuration_transition(store, SimpleNamespace(is_running=False), None, operation)
            task = asyncio.create_task(request())
            await asyncio.wait_for(saved.wait(), 2)
            scopes[0].cancel()
            await asyncio.sleep(0.05)
            assert not task.done()
            release.set()
            try:
                await asyncio.wait_for(task, 2)
            except asyncio.CancelledError:
                pass
            assert completed == ['applied']
            assert len(joins) < 10, ('cancelled scope repeatedly re-entered join', len(joins))
            assert not store._transition_lock.locked()
            assert (await store.get('connection'))['url'] == 'accepted'
        asyncio.run(main())
    ''')
    result = subprocess.run([sys.executable, '-c', code, str(tmp_path / 'scope.json')],
                            capture_output=True, text=True, timeout=8)
    assert result.returncode == 0, result.stderr


async def test_cancelled_queued_transition_never_writes(store_env):
    from mcp_admin_core.configuration import run_configuration_transition
    before = store_env._path.read_bytes()
    condition = '"' + store_env.revision(await store_env.load()) + '"'
    operation = AsyncMock(return_value={'saved': True})
    async with store_env.transition():
        task = asyncio.create_task(run_configuration_transition(
            store_env, SimpleNamespace(is_running=False), condition, operation))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    operation.assert_not_awaited()
    assert store_env._path.read_bytes() == before


@pytest.mark.parametrize('path,payload', [
    ('/api/config/connection', {'dashboard_username': 'revoked-queued-editor'}),
    ('/api/settings/connection', {'url': 'revoked-queued-editor'}),
])
@pytest.mark.parametrize('change', ['revoked', 'expired'])
async def test_queued_authenticated_mutation_rechecks_session_before_write(path, payload, change, make_app, monkeypatch, tmp_config_path):
    from contextlib import asynccontextmanager
    from mcp_admin_core.config import get_config_store
    import hermes_mcp_admin.routers.config as module
    setup = make_app()
    setup.app.include_router(module.router)
    headers = auth(setup)
    store = get_config_store()
    action = AsyncMock(return_value=True)
    pm = SimpleNamespace(is_running=False, start=action, start_or_restart=action)
    monkeypatch.setattr(module, 'get_process_manager', lambda: pm)
    monkeypatch.setattr(settings, 'get_process_manager', lambda: pm)
    queued = asyncio.Event()
    original = store.transition
    @asynccontextmanager
    async def observed_transition():
        queued.set()
        async with original():
            yield
    monkeypatch.setattr(store, 'transition', observed_transition)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=setup.app), base_url='http://test', headers=headers) as client:
        etag = (await client.get('/api/settings')).headers['etag']
        async with original():
            request = asyncio.create_task(client.put(path, json=payload, headers={'If-Match': etag}))
            await asyncio.wait_for(queued.wait(), 2)
            # A preceding password update revokes the already authenticated
            # request while it waits for the transition lock. Revision does not
            # include auth state, so If-Match alone cannot protect this case.
            if change == 'revoked':
                await store.put('admin_password', 'new-fixture-password')
            else:
                import mcp_admin_core.configuration as configuration
                monkeypatch.setattr(configuration, 'time', SimpleNamespace(time=lambda: 10**12))
            before = tmp_config_path.read_bytes()
        response = await asyncio.wait_for(request, 2)
    assert response.status_code == 401
    assert 'configuration' not in response.text
    assert tmp_config_path.read_bytes() == before
    action.assert_not_awaited()
