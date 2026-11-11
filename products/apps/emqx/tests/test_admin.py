"""Real shared factory/router/store with isolated config; no shared service writes."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import pytest
from fastapi.testclient import TestClient
from emqx_mcp_admin.tool_registry import SQL_TOOL


@pytest.fixture
def admin_session(tmp_path, monkeypatch):
    import mcp_admin_core.config.store as store
    import mcp_admin_core.process as process
    from emqx_mcp_admin.main import app
    path = tmp_path / 'config.json'
    monkeypatch.setenv('MCP_ADMIN_CONFIG', str(path))
    # Exercise the installed launcher both under pytest and `uv run` in CI.
    monkeypatch.setenv('PATH', str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH'])

    @contextmanager
    def session():
        # A new admin generation must reload disk state and own its process loop.
        monkeypatch.setattr(store, '_instance', None)
        monkeypatch.setattr(process, '_instance', None)
        with TestClient(app) as client:
            login = client.post('/api/auth/login', json={'password': 'admin'})
            assert login.status_code == 200, login.text
            client.headers['Authorization'] = 'Bearer ' + login.json()['token']
            yield client, path

    return session


@pytest.fixture
def admin(admin_session):
    with admin_session() as state:
        yield state


def save(client, **updates):
    revision = client.get('/api/config').headers['etag']
    return client.put('/api/config/connection', headers={'If-Match': revision}, json={**updates, 'restart': False})


def test_actual_factory_all_on_and_revocations_persist_restart(admin_session):
    with admin_session() as (client, path):
        result = client.get('/api/tools')
        assert result.status_code == 200, result.text
        body = result.json()
        assert body['total'] == 39 and len(body['categories']) == 7
        assert sum(t['dangerous'] for t in body['tools']) == 14  # HA13 + explicit SQL grant
        assert body['disabled_tools'] == [] and body['enabled'] == 39
        sql = next(t for t in body['tools'] if t['name'] == SQL_TOOL)
        assert sql['operations'] == ['execute'] and sql['policy_enabled']
        assert body['operation_policy_supported']
        assert path.stat().st_mode & 0o777 == 0o600
        data = json.loads(path.read_text())
        assert 'emqx_policy_seeded' not in data['tools']
        # Standard Tools route revokes a grant; the app never reseeds it on restart.
        changed = client.put('/api/tools', json={'disabled_tools': ['emqx_publish', SQL_TOOL]},
                             headers={'If-Match': result.headers['etag']})
        assert changed.status_code == 200, changed.text
    # Exit the old lifespan before creating the new one; no cross-loop child reuse.
    with admin_session() as (restarted, _):
        assert restarted.get('/api/tools').json()['disabled_tools'] == ['emqx_publish', SQL_TOOL]


def test_saved_connection_masking_blank_preservation_and_real_probe(admin, broker):
    client, path = admin
    result = save(client, base_url=broker.url + '/api/v5/', api_key='synthetic-api-key', api_secret='synthetic-api-secret')
    assert result.status_code == 200, result.text
    assert result.json()['saved'] and result.json()['apply_state'] == 'not_requested'
    masked = client.get('/api/config')
    assert 'synthetic-api-' not in masked.text
    assert masked.json()['api_key_set'] and masked.json()['api_secret_set']
    assert save(client, base_url=broker.url, api_key='', api_secret='********').status_code == 200
    assert json.loads(path.read_text())['connection']['api_secret'] == 'synthetic-api-secret'
    # Posted values are ignored; only stored authenticated destination is tested.
    check = client.post('/api/config/test', json={'base_url': 'http://169.254.169.254'})
    assert check.json()['success'], check.text
    assert check.json()['details']['version'] == '5.8.9'
    assert len(broker.calls) == 1


def test_revision_conflict_and_invalid_input_have_no_effect(admin, broker):
    client, path = admin
    before = path.read_bytes()
    stale = client.put('/api/config/connection', headers={'If-Match': '"stale"'},
                       json={'base_url': broker.url, 'api_key': 'owned', 'api_secret': 'owned'})
    assert stale.status_code == 409
    assert path.read_bytes() == before
    for payload in [{'base_url': 'https://owned:credential@example.com'},
                    {'api_key': {'credential': 'must-not-echo'}},
                    {'api_secret': 'must-not-echo', 'unknown': 'must-not-echo'},
                    {'api_key': 'invalid:must-not-echo'}]:
        result = save(client, **payload)
        assert result.status_code in (400, 422), result.text
        assert 'must-not-echo' not in result.text and 'credential@' not in result.text
        assert path.read_bytes() == before
    assert broker.calls == []


def test_admin_requires_auth(admin):
    client, _ = admin
    client.headers.pop('Authorization')
    client.cookies.clear()
    assert client.get('/api/config').status_code == 401
    assert client.get('/api/tools').status_code == 401
    assert client.post('/api/config/test').status_code == 401


@pytest.mark.parametrize('response', [{}, {'version': '5.8.9'}, [], [{'node': 'owned', 'version': '5.8.9'}]])
def test_health_rejects_arbitrary_http_200(response, admin, broker):
    client, _ = admin
    assert save(client, base_url=broker.url, api_key='synthetic-api-key', api_secret='synthetic-api-secret').status_code == 200
    broker.response = response
    assert client.post('/api/config/test').json()['success'] is False


def test_n13_mandatory_revision_from_shared_factory(admin):
    client, path = admin
    before = path.read_bytes()
    result = client.put('/api/config/connection', json={'base_url': '', 'restart': False})
    assert result.status_code == 428, result.text
    assert path.read_bytes() == before


def test_n15_shared_restart_receipt_replay(admin):
    client, path = admin
    settings = client.get('/api/settings')
    operation_id = str(settings.json()['configuration']['server_time_ms']) + '.emqx-owned-restart-test'
    headers = {'If-Match': settings.headers['etag'], 'Idempotency-Key': operation_id}
    first = client.post('/api/settings/mcp/restart', headers=headers)
    second = client.post('/api/settings/mcp/restart', headers=headers)
    assert first.status_code == second.status_code == 200, (first.text, second.text)
    assert first.json()['operation']['replayed'] is False
    assert second.json()['operation']['replayed'] is True
    receipt = client.get('/api/operations/' + operation_id)
    assert receipt.status_code == 200 and receipt.json()['operation']['state'] == 'completed'
