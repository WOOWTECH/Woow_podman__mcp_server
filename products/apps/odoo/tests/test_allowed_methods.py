"""Synthetic config + authenticated real app; no production services."""

import asyncio
import json

import mcp_admin_core.config.store as store_mod
import pytest
from fastapi.testclient import TestClient
from mcp_admin_core.config import ConfigStore
from mcp_admin_core.configuration import launch_revision
from mcp_admin_core.launch_policy import set_launch_policy
from mcp_admin_core.process import _build_subprocess_env
from odoo_mcp_admin import allowed_methods as methods
from odoo_mcp_admin.main import LAUNCH_POLICY, app
from pydantic import ValidationError

ROUTE = '/api/odoo/allowed-methods'
LIST = ['sale.order.action_confirm', 'res.partner.message_post']
INVALID = [None, '', {}, True, [1], [[]], LIST + LIST, ['sale.order.*'], ['*.action_confirm'],
           ['sale.order._private'], ['sale.order.__class__'], ['method'], ['sale..order.action_confirm'],
           [' sale.order.action_confirm'], ['sale.order.action_confirm '], ['sale.order.action_confirm\n'],
           ['sale.order.action_confirm,res.partner.write'], ['sale.order.方法'], ['sale.order.1method'],
           ['1model.action_confirm'], ['sale-order.action_confirm'], ['sale.order.action-confirm'],
           ['sale.order.action_confirm\x00'], ['x.' + 'a' * 255],
           [f'sale.order.action_{i}' for i in range(129)], [f'sale.order.{"a" * 240}{i}' for i in range(70)]]


@pytest.fixture
def editor(tmp_path, monkeypatch):
    path = tmp_path / 'config.json'
    monkeypatch.setenv('MCP_ADMIN_CONFIG', str(path))
    monkeypatch.setattr(store_mod, '_instance', None)
    monkeypatch.delenv(methods.ENV, raising=False)
    set_launch_policy(LAUNCH_POLICY)

    class Process:
        is_running = False
        calls = 0
        fail = False
        _attempted_revision = None
        _applied_revision = None
        _configuration_error = None
        async def start_or_restart(self):
            self.calls += 1
            store = store_mod.get_config_store()
            self._attempted_revision = launch_revision(store, await store.load())
            if self.fail:
                self._configuration_error = 'child_start_failed'
                raise RuntimeError('PRIVATE_FAKE_CREDENTIAL')
            self.is_running = True
            self._applied_revision = self._attempted_revision
            return True

    process = Process()
    monkeypatch.setattr(methods, 'get_process_manager', lambda: process)
    client = TestClient(app)  # No lifespan / real process starts.
    login = client.post('/api/auth/login', json={'password': 'admin'})
    assert login.status_code == 200
    headers = {'Authorization': 'Bearer ' + login.json()['token']}
    yield client, headers, path, process
    client.close()


@pytest.mark.parametrize('value', INVALID)
def test_validation(value):
    with pytest.raises(ValidationError):
        methods.MethodUpdate.model_validate({methods.FIELD: value})


def test_auth_revision_invalid_persistence_clear_and_no_secrets(editor):
    client, headers, path, process = editor
    client.cookies.clear()
    assert client.get(ROUTE).status_code == 401
    assert client.put(ROUTE, json={methods.FIELD: LIST}).status_code == 401
    boot = client.get(ROUTE, headers=headers)
    assert boot.status_code == 200 and boot.json()[methods.FIELD] == []
    assert boot.json()[methods.FIELD + '_source'] == 'legacy_empty'
    assert 'connection' not in boot.json() and 'mcp_auth_token' not in boot.json()
    original = path.read_bytes()
    assert methods.FIELD not in json.loads(original)['mcp_server']
    assert client.put(ROUTE, headers=headers, json={methods.FIELD: LIST}).status_code == 428
    conditional = {**headers, 'If-Match': boot.headers['etag']}
    for value in INVALID:
        assert client.put(ROUTE, headers=conditional, json={methods.FIELD: value}).status_code == 422
    for value in ({}, {methods.FIELD: LIST, 'writes_enabled': True}, {methods.FIELD: LIST, 'restart': False}):
        assert client.put(ROUTE, headers=conditional, json=value).status_code == 422
    assert process.calls == 0 and path.read_bytes() == original
    saved = client.put(ROUTE, headers=conditional, json={methods.FIELD: LIST})
    assert saved.status_code == 200 and saved.json()['saved'] is True
    assert saved.json()['configuration']['apply_state'] == 'applied'
    assert saved.json()['configuration']['saved_revision'] != boot.json()['configuration']['saved_revision']
    assert json.loads(path.read_bytes())['mcp_server'][methods.FIELD] == LIST and process.calls == 1
    stored = path.read_bytes()
    assert client.put(ROUTE, headers=conditional, json={methods.FIELD: []}).status_code == 409
    assert client.put('/api/settings/mcp_server', headers=conditional,
                      json={'env': {'ODOO_MCP_ENABLE_WRITES': '0'}}).status_code == 409
    assert path.read_bytes() == stored and process.calls == 1
    assert client.put('/api/settings/' + methods.FIELD, headers={**headers, 'If-Match': saved.headers['etag']}, json={'value': []}).status_code == 400
    fresh = client.get(ROUTE, headers=headers)
    assert fresh.json()[methods.FIELD] == LIST and fresh.json()[methods.FIELD + '_source'] == 'saved'
    assert fresh.headers['etag'] == saved.headers['etag']
    cleared = client.put(ROUTE, headers={**headers, 'If-Match': fresh.headers['etag']}, json={methods.FIELD: []})
    assert cleared.status_code == 200 and process.calls == 2
    assert json.loads(path.read_bytes())['mcp_server'][methods.FIELD] == []
    assert client.get(ROUTE, headers=headers).json()[methods.FIELD + '_source'] == 'saved'
    # A new store sees the same explicit empty list.
    assert asyncio.run(ConfigStore(path).load())['mcp_server'][methods.FIELD] == []


def test_failed_apply_keeps_saved_list_and_is_sanitized(editor):
    client, headers, path, process = editor
    boot = client.get(ROUTE, headers=headers)
    process.fail = True
    result = client.put(ROUTE, headers={**headers, 'If-Match': boot.headers['etag']}, json={methods.FIELD: LIST})
    assert result.status_code == 200 and result.json()['saved'] is True
    assert result.json()['apply_state'] == 'failed'
    assert result.json()['configuration']['apply_state'] == 'failed'
    assert result.json()['status'] == 'partial'
    assert 'PRIVATE_FAKE_CREDENTIAL' not in result.text
    assert json.loads(path.read_bytes())['mcp_server'][methods.FIELD] == LIST


def test_env_precedence_legacy_no_rewrite_and_switches_unchanged(editor, monkeypatch):
    client, headers, path, _process = editor
    monkeypatch.setenv(methods.ENV, ' inherited.model.method ,inherited.model.method,other.model.method')
    before = path.read_bytes()
    boot = client.get(ROUTE, headers=headers)
    assert boot.json()[methods.FIELD] == ['inherited.model.method', 'other.model.method']
    assert boot.json()[methods.FIELD + '_source'] == 'container_environment'
    assert path.read_bytes() == before
    snapshot = json.loads(before)
    env = _build_subprocess_env({}, snapshot['mcp_server'], LAUNCH_POLICY, snapshot)
    assert env[methods.ENV] == ' inherited.model.method ,inherited.model.method,other.model.method'
    assert methods.ENV not in methods.child_env(snapshot)
    switches = {key: env[key] for key in ('ODOO_MCP_ENABLE_WRITES', 'ODOO_MCP_ALLOW_DIRECT_WRITES',
                                         'ODOO_MCP_ALLOW_UNKNOWN_METHODS', 'MCP_CHATTER_DIRECT')}
    for saved in (LIST, []):
        current = {**snapshot, 'mcp_server': {**snapshot['mcp_server'], methods.FIELD: saved}}
        result = _build_subprocess_env({}, current['mcp_server'], LAUNCH_POLICY, current)
        assert result[methods.ENV] == ','.join(saved)
        assert {key: result[key] for key in switches} == switches
    # Editing only the methods does not grant a general env editor or a policy-file path.
    assert methods.ENV not in LAUNCH_POLICY.editable_env_keys
    assert 'ODOO_MCP_POLICY_FILE' not in LAUNCH_POLICY.editable_env_keys
