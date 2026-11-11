"""Authenticated app proxy -> actual child process -> owned HTTP fixture."""
import copy
import json
import os
import socket
import sys
import time
from fastapi.testclient import TestClient


def test_proxy_auth_and_live_mixed_policy(tmp_path, monkeypatch, broker):
    import mcp_admin_core.config.store as store
    import mcp_admin_core.process as process
    from emqx_mcp_admin.main import app
    path = tmp_path / 'config.json'
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    cfg = copy.deepcopy(store._DEFAULT_CONFIG)
    cfg['mcp_server']['port'] = port
    cfg['connection'] = {'base_url': broker.url, 'api_key': 'synthetic-api-key', 'api_secret': 'synthetic-api-secret'}
    path.write_text(json.dumps(cfg))
    path.chmod(0o600)
    # Installed-console-script equivalent, confined to this test's PATH.
    launcher = tmp_path / 'emqx-mcp-server'
    launcher.write_text(f'#!{sys.executable}\nfrom emqx_mcp_admin.launcher import main\nmain()\n')
    launcher.chmod(0o700)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('MCP_ADMIN_CONFIG', str(path))
    monkeypatch.setattr(store, '_instance', None)
    monkeypatch.setattr(process, '_instance', None)
    pm = process.get_process_manager()
    with TestClient(app) as client:
        for _ in range(150):
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=.1):
                    break
            except OSError:
                time.sleep(.1)
        else:
            raise AssertionError('Owned child did not start')
        token = json.loads(path.read_text())['mcp_auth_token']
        endpoint = f'/private_{token}/mcp'
        headers = {'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-03-26'}
        def rpc(method, params, number=1):
            response = client.post(endpoint, headers=headers,
                json={'jsonrpc': '2.0', 'id': number, 'method': method, 'params': params})
            assert response.status_code == 200, response.text
            if response.headers.get('mcp-session-id'):
                headers['Mcp-Session-Id'] = response.headers['mcp-session-id']
            raw = response.text
            body = json.loads(next(line[6:] for line in raw.splitlines() if line.startswith('data: '))) if raw.startswith('event:') else response.json()
            return body
        assert client.post('/private_wrong/mcp', headers=headers,
            json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}).status_code in (401, 403, 404)
        rpc('initialize', {'protocolVersion': '2025-03-26', 'capabilities': {}, 'clientInfo': {'name': 'owned-fixture', 'version': '1'}})
        client.post(endpoint, headers=headers, json={'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        listed = rpc('tools/list', {})['result']['tools']
        assert len(listed) == 39 and 'emqx_test_rule_sql' in {t['name'] for t in listed}
        allowed = rpc('tools/call', {'name': 'emqx_broker_stats', 'arguments': {}})
        assert not allowed['result'].get('isError')
        broker.calls.clear()
        sql = {'name': 'emqx_test_rule_sql', 'arguments': {'sql': 'SELECT * FROM "owned/#"'}}
        assert not rpc('tools/call', sql)['result'].get('isError')
        assert [c[:2] for c in broker.calls] == [('POST', '/api/v5/rule_test')]
        broker.calls.clear()
        # Admin writes go through the actual shared auth/revision route.
        login = client.post('/api/auth/login', json={'password': 'admin'}).json()
        admin_headers = {'Authorization': 'Bearer ' + login['token']}
        tools = client.get('/api/tools', headers=admin_headers)
        updated = client.put('/api/tools/operations', headers={**admin_headers, 'If-Match': tools.headers['etag']},
            json={'disabled_operations': {'emqx_manage_authn_users': ['create', 'delete'],
                                          'emqx_test_rule_sql': ['execute']}})
        assert updated.status_code == 200, updated.text
        sql_denied = rpc('tools/call', sql)
        assert sql_denied.get('error') or sql_denied.get('result', {}).get('isError')
        for operation in ['create', 'READ', ' read ', None]:
            denied = rpc('tools/call', {'name': 'emqx_manage_authn_users', 'arguments': {
                'operation': operation, 'user_id': 'owned', 'password': 'owned-password'}})
            assert denied.get('error') or denied.get('result', {}).get('isError'), denied
        assert broker.calls == []
        read = rpc('tools/call', {'name': 'emqx_manage_authn_users', 'arguments': {'operation': 'read'}})
        assert not read['result'].get('isError')
        assert [c[0] for c in broker.calls] == ['GET']
        tools = client.get('/api/tools', headers=admin_headers)
        enabled = client.put('/api/tools/operations', headers={**admin_headers, 'If-Match': tools.headers['etag']},
            json={'disabled_operations': {'emqx_manage_authn_users': ['create', 'delete']}})
        assert enabled.status_code == 200
        assert not rpc('tools/call', sql)['result'].get('isError')
        assert [c[0] for c in broker.calls] == ['GET', 'POST']
        assert pm.is_running and pm._restart_count == 0
    assert not pm.is_running
