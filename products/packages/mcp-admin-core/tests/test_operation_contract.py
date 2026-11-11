"""Durable management operations through real authenticated HTTP routes."""
import asyncio
import json
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from mcp_admin_core.operations import OperationJournal, TTL_MS


def operation_id(offset=0):
    return f"{int(time.time() * 1000) + offset}.{uuid.uuid4()}"


def login(client):
    response = client.post('/api/auth/login', json={'password': 'admin'})
    assert response.status_code == 200
    return {'Authorization': 'Bearer ' + response.json()['token']}


def editor(client, auth, key=None):
    response = client.get('/api/tokens', headers=auth)
    assert response.status_code == 200
    return {**auth, 'If-Match': response.headers['etag'], 'Idempotency-Key': key or operation_id()}


def test_rotate_replay_alias_no_old_secret_and_restart_store(make_app, tmp_config_path, monkeypatch):
    client = make_app()
    auth = login(client)
    headers = editor(client, auth)
    first = client.post('/api/tokens/rotate', headers=headers, json={'length': 64})
    assert first.status_code == 200
    token = first.json()['token']
    before = tmp_config_path.read_bytes()
    journal = OperationJournal(tmp_config_path)
    assert token not in journal.path.read_text()
    assert 'admin' not in journal.path.read_text()
    assert journal.path.stat().st_mode & 0o777 == 0o600
    # A new store has a new revision key, like admin restart. Replay precedes CAS.
    import mcp_admin_core.config.store as store_mod
    monkeypatch.setattr(store_mod, '_instance', store_mod.ConfigStore(tmp_config_path))
    replay = client.post('/api/settings/mcp_auth_token/rotate', headers=headers)
    assert replay.status_code == 200
    assert replay.json()['operation']['replayed'] is True
    assert 'token' not in replay.json()
    assert token not in replay.text
    assert tmp_config_path.read_bytes() == before
    query = client.get('/api/operations/' + headers['Idempotency-Key'], headers=auth)
    assert query.status_code == 200
    assert query.json()['operation']['state'] == 'completed'
    assert token not in query.text and 'fingerprint' not in query.text


def test_payload_mismatch_and_current_auth_before_receipt(make_app, tmp_config_path):
    client = make_app()
    auth = login(client)
    headers = editor(client, auth)
    first = client.post('/api/tokens/rotate', headers=headers, json={'length': 32})
    assert first.status_code == 200
    before = tmp_config_path.read_bytes()
    mismatch = client.post('/api/tokens/rotate', headers=headers, json={'length': 64})
    assert mismatch.status_code == 409
    assert mismatch.json()['detail']['code'] == 'operation_id_conflict'
    assert tmp_config_path.read_bytes() == before
    key = headers['Idempotency-Key']
    client.cookies.clear()
    assert client.get('/api/operations/' + key).status_code == 401
    changed = client.put('/api/settings/admin_password', json={'value': 'new-password'}, headers=editor(client, auth))
    assert changed.status_code == 200
    assert client.get('/api/operations/' + key, headers=auth).status_code == 401
    assert client.post('/api/tokens/rotate', headers=headers).status_code == 401


def test_missing_id_does_not_write_and_old_revision_still_rejected(make_app, tmp_config_path):
    client = make_app()
    auth = login(client)
    headers = editor(client, auth)
    del headers['Idempotency-Key']
    before = tmp_config_path.read_bytes()
    response = client.post('/api/tokens/rotate', headers=headers)
    assert response.status_code == 428
    assert response.json()['detail']['code'] == 'operation_id_required'
    assert tmp_config_path.read_bytes() == before
    assert not OperationJournal(tmp_config_path).path.exists()


def test_restart_once_and_ambiguous_failure_never_rerun(make_app, monkeypatch, tmp_config_path):
    from mcp_admin_core.routers import settings
    action = AsyncMock(return_value=True)
    monkeypatch.setattr(settings, 'get_process_manager', lambda: SimpleNamespace(is_running=False, restart=action))
    client = make_app()
    auth = login(client)
    headers = editor(client, auth)
    assert client.post('/api/settings/mcp/restart', headers=headers).status_code == 200
    assert client.post('/api/settings/mcp/restart', headers=headers).status_code == 200
    assert action.await_count == 1
    action.side_effect = RuntimeError('synthetic secret must not leak')
    uncertain = editor(client, auth)
    result = client.post('/api/settings/mcp/restart', headers=uncertain)
    assert result.status_code == 409
    assert result.json()['detail']['code'] == 'operation_outcome_unknown'
    assert 'synthetic secret' not in result.text
    assert client.post('/api/settings/mcp/restart', headers=uncertain).status_code == 409
    assert action.await_count == 2
    query = client.get('/api/operations/' + uncertain['Idempotency-Key'], headers=auth)
    assert query.json()['operation']['state'] == 'unknown'


@pytest.mark.parametrize('phase', ['intent', 'completion'])
def test_durable_write_failure_boundary(phase, make_app, monkeypatch, tmp_config_path):
    original = OperationJournal._write
    def failing(self, data):
        state = next(iter(data['receipts'].values()))['state']
        if (state == 'pending') == (phase == 'intent'):
            raise OSError('private storage detail')
        return original(self, data)
    monkeypatch.setattr(OperationJournal, '_write', failing)
    client = make_app()
    headers = editor(client, login(client))
    before = tmp_config_path.read_bytes()
    result = client.post('/api/tokens/rotate', headers=headers)
    assert result.status_code == (503 if phase == 'intent' else 409)
    assert 'private storage' not in result.text
    assert (tmp_config_path.read_bytes() == before) == (phase == 'intent')
    if phase == 'completion':
        after = tmp_config_path.read_bytes()
        monkeypatch.setattr(OperationJournal, '_write', original)
        assert client.post('/api/tokens/rotate', headers=headers).status_code == 409
        assert tmp_config_path.read_bytes() == after


def test_capacity_expiry_and_clock_rollback_never_reintroduce_id(tmp_path, monkeypatch):
    import mcp_admin_core.operations as module
    monkeypatch.setattr(module, 'MAX_RECEIPTS', 2)
    now = int(time.time() * 1000)
    monkeypatch.setattr(module.time, 'time', lambda: now / 1000)
    journal = OperationJournal(tmp_path / 'config.json')
    keys = [f'{now}.{uuid.uuid4()}' for _ in range(3)]
    for key in keys[:2]:
        data, _, fingerprint = journal.inspect(key, 'mcp.restart', {})
        journal.begin(key, data, 'mcp.restart', fingerprint)
    data, _, fingerprint = journal.inspect(keys[2], 'mcp.restart', {})
    with pytest.raises(HTTPException) as full:
        journal.begin(keys[2], data, 'mcp.restart', fingerprint)
    assert full.value.detail['code'] == 'operation_journal_full'
    now += TTL_MS + 1
    new_key = f'{now}.{uuid.uuid4()}'
    data, _, fingerprint = journal.inspect(new_key, 'mcp.restart', {})
    journal.begin(new_key, data, 'mcp.restart', fingerprint)
    now -= TTL_MS
    with pytest.raises(HTTPException) as expired:
        journal.inspect(keys[0], 'mcp.restart', {})
    assert expired.value.status_code == 410


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'corrupt'])
def test_unsafe_journal_fails_closed(kind, tmp_path):
    journal = OperationJournal(tmp_path / 'config.json')
    target = tmp_path / 'other'
    target.write_text('do not change')
    if kind == 'symlink':
        journal.path.symlink_to(target)
    elif kind == 'hardlink':
        journal.path.hardlink_to(target)
    else:
        journal.path.write_text('{}')
    with pytest.raises(HTTPException) as unavailable:
        journal.inspect(operation_id(), 'token.rotate', {'length': 64})
    assert unavailable.value.status_code == 503
    assert target.read_text() == 'do not change'


def test_pending_survives_new_python_process_and_cannot_reexecute(tmp_path):
    import os
    import subprocess
    import sys
    journal = OperationJournal(tmp_path / 'config.json')
    key = operation_id()
    data, _, fingerprint = journal.inspect(key, 'mcp.restart', {})
    journal.begin(key, data, 'mcp.restart', fingerprint)
    code = 'from pathlib import Path; from mcp_admin_core.operations import OperationJournal; import sys; print(OperationJournal(Path(sys.argv[1])).query(sys.argv[2])["operation"]["state"])'
    output = subprocess.check_output([sys.executable, '-c', code, str(tmp_path / 'config.json'), key], env=os.environ)
    assert output.strip() == b'unknown'


@pytest.mark.parametrize('broken', [[], None, 'invalid', 1, True,
    {'version': 1}, {'version': 1, 'key': [], 'expired_before_ms': 0, 'receipts': {}}])
def test_malformed_top_level_is_unavailable_not_500(broken, tmp_path):
    journal = OperationJournal(tmp_path / 'config.json')
    journal.path.write_text(json.dumps(broken))
    with pytest.raises(HTTPException) as error:
        journal.query(operation_id())
    assert error.value.status_code == 503
    assert error.value.detail == {'code': 'operation_journal_unavailable'}


@pytest.mark.parametrize('broken', [[], None, 'invalid', 1, {},
    {'state': []}, {'state': 'completed', 'kind': 'mcp.restart', 'fingerprint': 'a' * 64},
    {'state': 'completed', 'kind': 'mcp.restart', 'fingerprint': 'a' * 64, 'result': []},
    {'state': 'completed', 'kind': 'mcp.restart', 'fingerprint': 'a' * 64, 'result': {'status': {'token': 'fake-secret'}}}])
def test_malformed_receipt_is_unavailable_not_500(broken, tmp_path):
    journal = OperationJournal(tmp_path / 'config.json')
    key = operation_id()
    journal.path.write_text(json.dumps({'version': 1, 'key': 'a' * 64,
        'expired_before_ms': 0, 'receipts': {key: broken}}))
    with pytest.raises(HTTPException) as error:
        journal.query(key)
    assert error.value.status_code == 503


@pytest.mark.parametrize('offset,status', [(60000, 400), (-TTL_MS - 1000, 410)])
def test_future_and_expired_ids_never_write(offset, status, make_app, tmp_config_path):
    client = make_app()
    headers = editor(client, login(client), operation_id(offset))
    before = tmp_config_path.read_bytes()
    response = client.post('/api/tokens/rotate', headers=headers)
    assert response.status_code == status
    assert tmp_config_path.read_bytes() == before
    assert not OperationJournal(tmp_config_path).path.exists()


async def test_http_cancel_after_intent_finishes_once_then_replays(make_app, monkeypatch, tmp_config_path):
    import httpx
    from mcp_admin_core.routers import settings
    entered, release = asyncio.Event(), asyncio.Event()
    count = 0
    async def restart():
        nonlocal count
        count += 1
        entered.set()
        await release.wait()
        return True
    monkeypatch.setattr(settings, 'get_process_manager', lambda: SimpleNamespace(is_running=False, restart=restart))
    setup = make_app()
    headers = editor(setup, login(setup))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=setup.app), base_url='http://test') as client:
        request = asyncio.create_task(client.post('/api/settings/mcp/restart', headers=headers))
        await asyncio.wait_for(entered.wait(), 2)
        assert OperationJournal(tmp_config_path).query(headers['Idempotency-Key'])['operation']['state'] == 'unknown'
        request.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await request
        replay = await client.post('/api/settings/mcp/restart', headers=headers)
        assert replay.status_code == 200
        assert replay.json()['operation']['replayed'] is True
        assert count == 1


def test_completed_rotate_replays_in_new_process_without_old_token(make_app, tmp_config_path):
    import os
    import subprocess
    import sys
    client = make_app()
    headers = editor(client, login(client))
    response = client.post('/api/tokens/rotate', headers=headers)
    assert response.status_code == 200
    before = tmp_config_path.read_bytes()
    script = '''
import sys
from fastapi.testclient import TestClient
from mcp_admin_core import create_app
client = TestClient(create_app(title="Synthetic restart"))
auth = client.post('/api/auth/login', json={'password':'admin'}).json()['token']
response = client.post('/api/tokens/rotate', headers={'Authorization':'Bearer '+auth,
    'Idempotency-Key':sys.argv[1], 'If-Match':'"old-process-revision"'})
assert response.status_code == 200
assert 'token' not in response.json()
assert response.json()['operation']['replayed'] is True
print('replayed-without-secret')
'''
    output = subprocess.check_output([sys.executable, '-c', script, headers['Idempotency-Key']],
        env={**os.environ, 'MCP_ADMIN_CONFIG': str(tmp_config_path)})
    assert output.strip() == b'replayed-without-secret'
    assert tmp_config_path.read_bytes() == before


def test_set_replay_journal_contains_no_payload_token(make_app, tmp_config_path):
    client = make_app()
    headers = editor(client, login(client))
    payload = {'token': 'synthetic-custom-secret-00001'}
    assert client.put('/api/tokens', headers=headers, json=payload).status_code == 200
    journal = OperationJournal(tmp_config_path)
    assert payload['token'] not in journal.path.read_text()
    before = tmp_config_path.read_bytes()
    assert client.put('/api/tokens', headers=headers, json=payload).json()['operation']['replayed'] is True
    mismatch = client.put('/api/tokens', headers=headers, json={'token': 'synthetic-other-secret-00002'})
    assert mismatch.json()['detail']['code'] == 'operation_id_conflict'
    assert tmp_config_path.read_bytes() == before
