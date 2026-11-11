"""Existing null-patch contract used by Odoo/LiteLLM explicit-clear UI.

Real app authentication/CAS/config files; process manager is a recording stub.
No backend requests, tokens, or subprocesses from outside this fixture.
"""
import copy
import importlib
import json
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from mcp_admin_core import launch_policy, process
from mcp_admin_core.config import store as stores


@pytest.fixture(autouse=True)
def controlled_environment(monkeypatch):
    # Never place the runner's inherited environment in assertion diagnostics.
    # These tests stub all child/backend work and need only fixture-owned values.
    monkeypatch.setattr(os, 'environ', {
        'PATH': os.defpath,
        'JWT_SECRET': 'SYNTHETIC_G11_JWT_SECRET_FOR_LOCAL_TESTS_ONLY',
        'JWT_EXPIRY_HOURS': '24',
    })


@pytest.fixture(params=[
    ('odoo', 'odoo_password'), ('litellm', 'master_key'),
    ('emqx', 'api_key'), ('emqx', 'api_secret'),
], ids=['odoo', 'litellm', 'emqx-key', 'emqx-secret'])
def subject(request, tmp_path, monkeypatch):
    product, key = request.param
    public = dict(odoo_url='https://odoo.example.test', odoo_db='localdb', odoo_username='fixture') if product == 'odoo' else dict(base_url='https://llm.example.test')
    if product == 'emqx':
        # A literal public address needs no DNS for the real SSRF validator.
        # No connection test/lifespan is entered; the process manager is stubbed.
        peer_key = 'api_secret' if key == 'api_key' else 'api_key'
        public = {'base_url': 'https://8.8.8.8', peer_key: 'SYNTHETIC_PEER_SECRET'}
    config = copy.deepcopy(stores._DEFAULT_CONFIG)
    config['connection'] = {**public, key: 'SYNTHETIC_SECRET_OLD'}
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(config)); path.chmod(0o600)
    monkeypatch.setenv('MCP_ADMIN_CONFIG', str(path))
    monkeypatch.setattr(stores, '_instance', None)
    pm = SimpleNamespace(is_running=False, calls=0, fail=False)
    async def apply():
        pm.calls += 1
        if pm.fail: raise RuntimeError('synthetic apply failure')
        return True
    pm.start = pm.restart = pm.start_or_restart = apply
    monkeypatch.setattr(process, '_instance', pm)
    module = importlib.import_module(product + '_mcp_admin.main')
    old_policy = launch_policy.get_launch_policy()
    launch_policy.set_launch_policy(module.LAUNCH_POLICY)
    client = TestClient(module.app)
    login = client.post('/api/auth/login', json={'password': 'admin'})
    assert login.status_code == 200
    client.headers['Authorization'] = 'Bearer ' + login.json()['token']
    state = SimpleNamespace(client=client, path=path, pm=pm, key=key, public=public, policy=module.LAUNCH_POLICY)
    try: yield state
    finally:
        client.close()
        launch_policy.set_launch_policy(old_policy)


def read(subject):
    response = subject.client.get('/api/config')
    assert response.status_code == 200
    assert 'SYNTHETIC_SECRET' not in response.text
    return response


def put(subject, path, data, revision=None):
    return subject.client.put(path, json=data, headers={'If-Match': revision or read(subject).headers['etag']})


def test_keep_replace_clear_readback_preserves_other_fields(subject):
    s = subject
    result = put(s, '/api/config/connection', {**s.public, s.key: '', 'restart': False})
    assert result.status_code == 200
    assert json.loads(s.path.read_text())['connection'][s.key] == 'SYNTHETIC_SECRET_OLD'
    result = put(s, '/api/config/connection', {**s.public, s.key: 'SYNTHETIC_SECRET_NEW', 'restart': False})
    assert result.status_code == 200
    assert json.loads(s.path.read_text())['connection'][s.key] == 'SYNTHETIC_SECRET_NEW'
    before = json.loads(s.path.read_text())
    revision = read(s).headers['etag']
    result = put(s, '/api/settings/connection', {s.key: None}, revision)
    assert result.status_code == 200 and result.json()['saved'] is True
    after = json.loads(s.path.read_text())
    assert after['connection'] == {**s.public, s.key: None}
    assert {k:v for k,v in after.items() if k != 'connection'} == {k:v for k,v in before.items() if k != 'connection'}
    assert read(s).json()[s.key + '_masked'] == '(not set)'
    env = process._build_subprocess_env(after['connection'], after['mcp_server'], s.policy, after)
    assert 'SYNTHETIC_SECRET_NEW' not in env.values()
    assert s.pm.calls == 1
    assert put(s, '/api/settings/connection', {s.key: None}, revision).status_code == 409
    assert s.pm.calls == 1


def test_auth_revision_and_invalid_input_fail_before_mutation(subject):
    s = subject
    revision = read(s).headers['etag']
    before = s.path.read_bytes()
    assert s.client.put('/api/settings/connection', json={s.key: None}).status_code == 428
    assert put(s, '/api/settings/connection', {s.key: None}, '"stale"').status_code == 409
    assert put(s, '/api/config/connection', {**s.public, s.key: None, 'restart': False}).status_code == 422
    assert put(s, '/api/settings/connection', {'NOT_AN_ALLOWED_CONNECTION_FIELD': None}).status_code == 400
    headers = dict(s.client.headers)
    s.client.headers.pop('Authorization'); s.client.cookies.clear()
    assert s.client.put('/api/settings/connection', json={s.key: None}, headers={'If-Match': revision}).status_code == 401
    s.client.headers.update(headers)
    assert s.path.read_bytes() == before and s.pm.calls == 0


@pytest.mark.parametrize('secret_update', [{}, {'value': ''}, {'value': '   '}], ids=['omitted', 'blank', 'whitespace'])
def test_shared_route_still_preserves_existing_secret(subject, secret_update):
    s = subject
    body = {s.key: secret_update['value']} if 'value' in secret_update else {}
    result = put(s, '/api/settings/connection', body)
    assert result.status_code == 200
    stored = json.loads(s.path.read_text())['connection']
    assert stored == {**s.public, s.key: 'SYNTHETIC_SECRET_OLD'}
    assert s.pm.calls == 1


def test_saved_clear_apply_failure_is_not_reported_as_unsaved_or_retried(subject):
    s = subject; s.pm.fail = True
    revision = read(s).headers['etag']
    result = put(s, '/api/settings/connection', {s.key: None}, revision)
    assert result.status_code == 200
    assert result.json()['saved'] is True and result.json()['apply_state'] == 'failed'
    assert read(s).json()[s.key + '_masked'] == '(not set)'
    assert json.loads(s.path.read_text())['connection'] == {**s.public, s.key: None}
    assert s.pm.calls == 1
    assert put(s, '/api/settings/connection', {s.key: None}, revision).status_code == 409
    assert s.pm.calls == 1


def test_cleared_secret_is_not_a_nonempty_child_credential(subject, monkeypatch):
    """3c93 emitted 'None'; clear must also override a parent credential."""
    s = subject
    env_name = s.policy.env_name_for_connection_key(s.key)
    monkeypatch.setenv(env_name, 'SYNTHETIC_PARENT_SECRET')
    assert put(s, '/api/settings/connection', {s.key: None}).status_code == 200
    after = json.loads(s.path.read_text())
    assert after['connection'][s.key] is None
    assert read(s).json()[s.key + '_masked'] == '(not set)'
    env = process._build_subprocess_env(after['connection'], after['mcp_server'], s.policy, after)
    cleared_value = env.get(env_name)
    assert cleared_value == '', 'clear must override the parent with an empty child credential'


@pytest.mark.parametrize('policy', [None, launch_policy.LaunchPolicy(
    command='synthetic-child', env_prefix='TEST_', connection_keys=frozenset({'key'}))], ids=['legacy', 'allowlisted'])
@pytest.mark.parametrize('value,expected', [(None, ''), ('', ''), (False, 'False'), (0, '0'), ('replacement', 'replacement')])
def test_connection_env_value_semantics(policy, value, expected, monkeypatch):
    monkeypatch.setenv('TEST_KEY', 'SYNTHETIC_PARENT_SECRET')
    env = process._build_subprocess_env({'key': value}, {'env_prefix': 'TEST_'}, policy)
    actual = env.get('TEST_KEY')
    assert actual == expected


@pytest.mark.parametrize('policy', [None, launch_policy.LaunchPolicy(
    command='synthetic-child', env_prefix='TEST_', connection_keys=frozenset({'key'}))], ids=['legacy', 'allowlisted'])
def test_omitted_connection_key_retains_parent_semantics(policy, monkeypatch):
    monkeypatch.setenv('TEST_KEY', 'SYNTHETIC_PARENT_SECRET')
    env = process._build_subprocess_env({}, {'env_prefix': 'TEST_'}, policy)
    actual = env.get('TEST_KEY')
    assert actual == 'SYNTHETIC_PARENT_SECRET'


@pytest.mark.parametrize('product,key', [
    ('odoo', 'odoo_password'), ('litellm', 'master_key'),
    ('n8n', 'n8n_api_key'), ('nextcloud', 'app_password'),
    ('hermes', 'gateway_api_key'), ('k8s', 'kubeconfig'),
    ('opendesign', 'od_basic_password'), ('vk', 'vibe_backend_url'),
    ('emqx', 'api_key'), ('emqx', 'api_secret'),
])
def test_current_product_policy_null_overrides_inherited_value(product, key, monkeypatch):
    """Mapping only: actual product policies, no child or backend execution."""
    old_policy = launch_policy.get_launch_policy()
    try:
        policy = importlib.import_module(product + '_mcp_admin.main').LAUNCH_POLICY
        assert key in policy.connection_keys and key not in policy.non_env_connection_keys
        name = policy.env_name_for_connection_key(key)
        monkeypatch.setenv(name, 'SYNTHETIC_PARENT_VALUE')
        env = process._build_subprocess_env({key: None}, {}, policy)
        actual = env.get(name)
        assert actual == ''
    finally:
        launch_policy.set_launch_policy(old_policy)
