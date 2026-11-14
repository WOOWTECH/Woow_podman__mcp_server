"""Real Odoo MCP children against a loopback-only, synthetic XML-RPC backend."""
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from xmlrpc.server import SimpleXMLRPCRequestHandler, SimpleXMLRPCServer

import httpx

PROFILE = '1234567890abcdef12345678'


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def rpc_body(response, ident):
    if response.headers.get('content-type', '').startswith('text/event-stream'):
        events = [json.loads(line[5:].strip()) for line in response.text.splitlines() if line.startswith('data:')]
        return next(x for x in events if x.get('id') == ident)
    return response.json()


def test_real_two_profile_restore_revokes_tokens_and_admin_session(tmp_path):
    passwords = [secrets.token_urlsafe(32), secrets.token_urlsafe(32)]
    tokens = [secrets.token_hex(32), secrets.token_hex(32)]
    admin = secrets.token_urlsafe(32)
    secret = secrets.token_urlsafe(32)
    users = {'synthetic-a': (101, passwords[0]), 'synthetic-b': (102, passwords[1])}
    calls = []

    class Handler(SimpleXMLRPCRequestHandler):
        rpc_paths = ('/xmlrpc/2/common', '/xmlrpc/2/object')
        def log_message(self, *args):
            pass

    backend = SimpleXMLRPCServer(('127.0.0.1', 0), requestHandler=Handler, logRequests=False, allow_none=True)
    def authenticate(db, user, password, _context):
        item = users.get(user)
        return item[0] if db == 'synthetic' and item and secrets.compare_digest(item[1], password) else False
    def execute_kw(db, uid, password, model, method, args, kwargs=None):
        if db != 'synthetic' or not any(uid == u and secrets.compare_digest(password, p) for u, p in users.values()):
            raise ValueError('invalid synthetic account')
        if model != 'res.users' or method != 'context_get':
            raise ValueError('unsupported synthetic call')
        calls.append(uid)
        return {'uid': uid, 'marker': 'SYNTH_A' if uid == 101 else 'SYNTH_B'}
    backend.register_function(authenticate, 'authenticate')
    backend.register_function(lambda: {'server_version': 'synthetic-18', 'server_version_info': [18, 0, 0, 'final', 0], 'protocol_version': 1}, 'version')
    backend.register_function(execute_kw, 'execute_kw')
    thread = threading.Thread(target=backend.serve_forever, daemon=True)
    thread.start()
    port, child_a, child_b = free_port(), free_port(), free_port()
    while len({port, child_a, child_b, backend.server_address[1]}) < 4:
        port, child_a, child_b = free_port(), free_port(), free_port()
    base = f'http://127.0.0.1:{port}'
    data = tmp_path / 'original'
    data.mkdir(mode=0o700)
    profile_dir = data / 'profiles' / PROFILE
    profile_dir.mkdir(mode=0o700, parents=True)
    profile_dir.parent.chmod(0o700)

    def config(i, child_port):
        return {'admin_password': admin, 'admin_session_generation': secrets.token_urlsafe(32),
                'mcp_auth_token': tokens[i], 'mcp_auth_revoked': False, 'mcp_auth_generation': secrets.token_urlsafe(32),
                'connection': {'odoo_url': f'http://127.0.0.1:{backend.server_address[1]}', 'odoo_db': 'synthetic',
                               'odoo_username': ('synthetic-a', 'synthetic-b')[i], 'odoo_password': passwords[i]},
                'mcp_server': {'port': child_port, 'env': {'ODOO_MCP_ENABLE_WRITES': '0', 'ODOO_MCP_ALLOW_DIRECT_WRITES': '0',
                               'ODOO_MCP_ALLOW_UNKNOWN_METHODS': '0', 'MCP_CHATTER_DIRECT': '0'}},
                'tools': {'disabled': [], 'disabled_operations': {},
                          'owner_defaults_20261025': {'applied': 'fresh_install', 'source': 'new_config'}},
                'proxy': {'timeout': 10}, 'token_history': []}
    root, second = config(0, child_a), config(1, child_b)
    root['profile_index'] = {PROFILE: {'label': 'Synthetic B', 'port': child_b}}
    for path, obj in ((data / 'config.json', root), (profile_dir / 'config.json', second)):
        path.write_text(json.dumps(obj))
        path.chmod(0o600)

    env = {'PATH': str(Path(sys.executable).parent) + ':/usr/bin:/bin', 'HOME': str(tmp_path),
           'LANG': 'C.UTF-8', 'JWT_SECRET': secret, 'NO_PROXY': '*', 'PYTHONDONTWRITEBYTECODE': '1'}
    uvicorn = [sys.executable, '-m', 'uvicorn', 'odoo_mcp_admin.main:app', '--host', '127.0.0.1',
               '--port', str(port)]
    client = httpx.Client(timeout=15, trust_env=False)
    log = tempfile.TemporaryFile()  # noqa: SIM115 -- closed with child processes in finally
    issued = list(tokens)
    operation_ids = []
    proc = None

    def start(command, path=None):
        nonlocal proc
        environ = env.copy()
        if path:
            environ['MCP_ADMIN_CONFIG'] = str(path / 'config.json')
        proc = subprocess.Popen(command, env=environ, stdout=log, stderr=log, start_new_session=True)
        for _ in range(100):
            if proc.poll() is not None:
                raise AssertionError('admin process exited')
            try:
                if client.get(base + '/healthz').status_code == 200:
                    return
            except httpx.TransportError:
                pass
            time.sleep(.1)
        raise AssertionError('admin readiness timeout')

    def stop():
        nonlocal proc
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, 9)
                proc.wait()
            proc = None

    def login(password=admin):
        r = client.post(base + '/api/auth/login', json={'password': password})
        assert r.status_code == 200
        return r.json()['token']

    def change_token(jwt, profile, operation):
        h = {'Authorization': 'Bearer ' + jwt, 'X-MCP-Profile': profile}
        r = client.get(base + '/api/settings', headers=h)
        assert r.status_code == 200
        operation_id = f'{int(time.time()*1000)}.{uuid.uuid4()}'
        operation_ids.append(operation_id)
        h.update({'If-Match': r.headers['ETag'], 'Idempotency-Key': operation_id})
        r = client.post(base + '/api/tokens/' + operation, headers=h, json={})
        assert r.status_code == 200
        return r.json().get('token')

    def invoke(token, marker, expected=200, restricted=False):
        url = base + '/private_' + token + '/mcp'
        headers = {'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json',
                   'MCP-Protocol-Version': '2025-03-26'}
        payload = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                   'params': {'protocolVersion': '2025-03-26', 'capabilities': {},
                              'clientInfo': {'name': 'synthetic-restore-test', 'version': '1'}}}
        for _ in range(80):
            response = client.post(url, json=payload, headers=headers)
            if response.status_code in (200, 403):
                break
            time.sleep(.1)
        assert response.status_code == expected
        if expected != 200:
            return
        assert 'result' in rpc_body(response, 1)
        headers['Mcp-Session-Id'] = response.headers['mcp-session-id']
        response = client.post(url, headers=headers, json={'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        assert response.status_code in (200, 202, 204)
        if restricted:
            response = client.post(url, headers=headers, json={'jsonrpc': '2.0', 'id': 3, 'method': 'tools/list'})
            assert response.status_code == 200
            listed = rpc_body(response, 3)['result']['tools']
            assert 'execute_method' not in {item['name'] for item in listed}
        response = client.post(url, headers=headers, json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
            'params': {'name': 'get_odoo_profile', 'arguments': {'include_modules': False}}})
        assert response.status_code == 200
        result = rpc_body(response, 2)['result']
        assert not result.get('isError')
        assert marker in json.dumps(result)
        assert ('SYNTH_B' if marker == 'SYNTH_A' else 'SYNTH_A') not in json.dumps(result)

    try:
        start(uvicorn, data)
        old_jwt = login()
        issued.append(old_jwt)
        invoke(tokens[0], 'SYNTH_A')
        invoke(tokens[1], 'SYNTH_B')
        tokens[1] = change_token(old_jwt, PROFILE, 'rotate')
        issued.append(tokens[1])
        previous_b_operation = operation_ids[-1]
        invoke(tokens[1], 'SYNTH_B')
        stop()
        backup = tmp_path / 'backup'
        shutil.copytree(data, backup)
        archived_before = (backup / 'profiles' / PROFILE / '.config.json.operations.json').read_bytes()
        # Model older, explicitly restricted backups for both profiles. Normal
        # startup must not expand either policy after the offline restore.
        for path in (backup / 'config.json', backup / 'profiles' / PROFILE / 'config.json'):
            saved = json.loads(path.read_text())
            saved['tools'].pop('owner_defaults_20261025', None)
            saved['tools']['disabled'] = ['execute_method']
            saved['tools']['disabled_operations'] = {'execute_method': ['unlink']}
            path.write_text(json.dumps(saved))
        start(uvicorn, data)
        jwt = login()
        changed = change_token(jwt, 'default', 'rotate')
        issued.extend((jwt, changed))
        invoke(tokens[0], 'SYNTH_A', expected=403)
        invoke(changed, 'SYNTH_A')
        invoke(tokens[1], 'SYNTH_B')
        change_token(jwt, 'default', 'revoke')
        invoke(changed, 'SYNTH_A', expected=403)
        invoke(tokens[1], 'SYNTH_B')
        stop()
        ready = tmp_path / 'ready'
        start([sys.executable, '-m', 'mcp_admin_core.restore_guard', 'serve', str(backup), str(ready), '--', *uvicorn])
        invoke(tokens[0], 'SYNTH_A', expected=403)
        invoke(tokens[1], 'SYNTH_B', expected=403)
        invoke(changed, 'SYNTH_A', expected=403)
        response = client.get(base + '/api/profiles', headers={'Authorization': 'Bearer ' + old_jwt})
        assert response.status_code == 401
        assert client.post(base + '/api/auth/login', json={'password': admin}).status_code == 401
        # Simulate a trusted recovery operator reading its own protected test
        # config; production must reset the password through a trusted channel.
        recovered_password = json.loads((ready / 'config.json').read_text())['admin_password']
        assert recovered_password != admin
        new_jwt = login(recovered_password)
        archives = list((ready / 'profiles' / PROFILE / 'restore-operation-journals').iterdir())
        assert len(archives) == 1 and archives[0].read_bytes() == archived_before
        selected = {'Authorization': 'Bearer ' + new_jwt, 'X-MCP-Profile': PROFILE}
        etag = client.get(base + '/api/settings', headers=selected).headers['ETag']
        old_request = client.post(base + '/api/tokens/rotate', json={}, headers={**selected,
            'If-Match': etag, 'Idempotency-Key': previous_b_operation})
        assert old_request.status_code == 410
        fresh_a, fresh_b = change_token(new_jwt, 'default', 'rotate'), change_token(new_jwt, PROFILE, 'rotate')
        issued.extend((new_jwt, fresh_a, fresh_b, admin, recovered_password))
        invoke(fresh_a, 'SYNTH_A', restricted=True)
        invoke(fresh_b, 'SYNTH_B', restricted=True)
        for path in (ready / 'config.json', ready / 'profiles' / PROFILE / 'config.json'):
            restored = json.loads(path.read_text())
            assert restored['tools']['disabled'] == ['execute_method']
            assert restored['tools']['disabled_operations'] == {'execute_method': ['unlink']}
            assert all(value == '0' for value in restored['mcp_server']['env'].values())
        assert set(calls) == {101, 102}
        stop()
        start(uvicorn, data)
        invoke(tokens[0], 'SYNTH_A', expected=403)
        invoke(changed, 'SYNTH_A', expected=403)
        invoke(tokens[1], 'SYNTH_B')
        stop()
        log.seek(0)
        captured = log.read()
        assert all(value.encode() not in captured for value in issued)
    finally:
        stop()
        log.close()
        client.close()
        backend.shutdown()
        backend.server_close()
        thread.join(timeout=3)
