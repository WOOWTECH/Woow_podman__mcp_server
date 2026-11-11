"""管理設定 HTTP 寫入不可省略版本，也不可從 token 相容路徑繞過。"""
from management_client import operation_id
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest


def login(client):
    result = client.post('/api/auth/login', json={'password': 'admin'})
    assert result.status_code == 200
    return {'Authorization': 'Bearer ' + result.json()['token']}


MUTATIONS = [
    ('put', '/api/settings/proxy', {'timeout': 12}),
    ('put', '/api/settings/tools', {'disabled': []}),
    ('put', '/api/settings/admin_password', {'value': 'a-new-password'}),
    ('post', '/api/settings/mcp/restart', None),
    ('post', '/api/settings/mcp/apply', None),
    ('post', '/api/settings/mcp_auth_token/rotate', None),
    ('put', '/api/tokens', {'token': 'explicit-token-for-contract-tests'}),
    ('post', '/api/tokens/rotate', {'length': 64}),
    ('post', '/api/tokens/revoke', None),
]


@pytest.mark.parametrize('method,path,payload', MUTATIONS)
@pytest.mark.parametrize('condition,expected', [(None, 428), ('"old-editor"', 409)])
def test_every_management_mutation_rejects_missing_or_stale_revision_without_effect(
        method, path, payload, condition, expected, make_app, tmp_config_path, monkeypatch):
    from mcp_admin_core.routers import settings, tokens
    action = AsyncMock(return_value=True)
    manager = SimpleNamespace(is_running=False, restart=action, start_or_restart=action)
    monkeypatch.setattr(settings, 'get_process_manager', lambda: manager)
    monkeypatch.setattr(tokens, 'get_process_manager', lambda: manager)
    client = make_app({'mcp_auth_token': 'previous-token-remains'})
    headers = login(client)
    if condition is not None:
        headers['If-Match'] = condition
    before = tmp_config_path.read_bytes()
    reply = getattr(client, method)(path, json=payload, headers=headers)
    assert reply.status_code == expected
    assert tmp_config_path.read_bytes() == before
    assert 'previous-token-remains' not in reply.text
    action.assert_not_awaited()


@pytest.mark.parametrize('path', ['/api/tokens/rotate', '/api/settings/mcp_auth_token/rotate'])
def test_token_rotation_invalidates_all_open_editors_and_can_be_reloaded(path, make_app, tmp_config_path):
    client = make_app({'mcp_auth_token': 'previous-token-remains'})
    headers = login(client)
    initial = client.get('/api/tokens', headers=headers)
    revision = initial.headers['etag']
    rotated = client.post(path, headers={**headers, 'If-Match': revision, 'Idempotency-Key': operation_id()})
    assert rotated.status_code == 200
    assert rotated.headers['etag'] != revision
    before = tmp_config_path.read_bytes()
    rejected = client.post('/api/tokens/revoke', headers={**headers, 'If-Match': revision})
    assert rejected.status_code == 409
    assert tmp_config_path.read_bytes() == before
    latest = client.get('/api/tokens', headers=headers)
    revoked = client.post('/api/tokens/revoke', headers={**headers, 'If-Match': latest.headers['etag'], 'Idempotency-Key': operation_id()})
    assert revoked.status_code == 200
    assert client.get('/api/tokens', headers=headers).json()['state'] == 'revoked'


def test_anonymous_requests_do_not_receive_revision(make_app):
    client = make_app()
    response = client.post('/api/tokens/rotate')
    assert response.status_code == 401
    assert 'configuration' not in response.text
