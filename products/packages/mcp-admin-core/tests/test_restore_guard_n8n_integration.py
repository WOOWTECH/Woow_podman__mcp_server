"""Real n8n MCP subprocesses, two synthetic accounts and a loopback API."""
import json
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

PROFILE = '1234567890abcdef12345678'
PACKAGE = os.environ.get('N8N_MCP_TEST_PACKAGE_DIR')
pytestmark = pytest.mark.skipif(not PACKAGE, reason='set N8N_MCP_TEST_PACKAGE_DIR to the pinned local n8n-mcp')


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def rpc_data(response, ident):
    if 'text/event-stream' in response.headers.get('content-type', ''):
        rows = [json.loads(row[5:].strip()) for row in response.text.splitlines() if row.startswith('data:')]
        return next(row for row in rows if row.get('id') == ident)
    return response.json()


def test_n8n_two_real_children_restore_and_profile_isolation(tmp_path):
    assert json.loads((Path(PACKAGE) / 'package.json').read_text())['version'] == '2.96.2'
    api_keys = [secrets.token_urlsafe(32), secrets.token_urlsafe(32)]
    external = [secrets.token_hex(32), secrets.token_hex(32)]
    internal = [secrets.token_urlsafe(32), secrets.token_urlsafe(32)]
    admin = secrets.token_urlsafe(32)
    signing_key = secrets.token_urlsafe(32)
    observed = []

    class Backend(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            key = self.headers.get('X-N8N-API-KEY')
            if urlsplit(self.path).path != '/api/v1/workflows' or key not in api_keys:
                self.send_response(403)
                self.end_headers()
                return
            i = api_keys.index(key)
            observed.append(i)
            data = {'data': [{'id': str(i + 1), 'name': ('SYNTH_A_ONLY', 'SYNTH_B_ONLY')[i],
                              'active': False, 'createdAt': '2020-01-01T00:00:00.000Z',
                              'updatedAt': '2020-01-01T00:00:00.000Z', 'nodes': [], 'tags': []}],
                    'nextCursor': None}
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    backend = ThreadingHTTPServer(('127.0.0.1', 0), Backend)
    thread = threading.Thread(target=backend.serve_forever, daemon=True)
    thread.start()
    gateway, port_a, port_b = free_port(), free_port(), free_port()
    while len({gateway, port_a, port_b, backend.server_address[1]}) != 4:
        gateway, port_a, port_b = free_port(), free_port(), free_port()
    base = f'http://127.0.0.1:{gateway}'
    data = tmp_path / 'original'
    data.mkdir(mode=0o700)
    extra = data / 'profiles' / PROFILE
    extra.mkdir(mode=0o700, parents=True)
    extra.parent.chmod(0o700)
    def config(i, port):
        return {'admin_password': admin, 'admin_session_generation': secrets.token_urlsafe(32),
                'mcp_auth_token': external[i], 'mcp_auth_revoked': False,
                'mcp_auth_generation': secrets.token_urlsafe(32), 'token_history': [],
                'connection': {'n8n_api_url': f'http://127.0.0.1:{backend.server_address[1]}',
                               'n8n_api_key': api_keys[i]},
                'mcp_server': {'port': port, 'env': {'AUTH_TOKEN': internal[i]}},
                'proxy': {'bearer_token': internal[i], 'timeout': 20},
                'tools': {'disabled': [], 'disabled_operations': {},
                          'owner_defaults_20261025': {'applied': 'fresh_install', 'source': 'new_config'}}}
    root, other = config(0, port_a), config(1, port_b)
    root['profile_index'] = {PROFILE: {'label': 'Synthetic B', 'port': port_b}}
    for path, value in ((data / 'config.json', root), (extra / 'config.json', other)):
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    # The launcher forces telemetry/version checks off. Only a loopback synthetic
    # API is configured; never inherit customer credentials or proxy settings.
    env = {'PATH': str(Path(sys.executable).parent) + ':' + os.environ['PATH'], 'HOME': str(tmp_path),
           'LANG': 'C.UTF-8', 'JWT_SECRET': signing_key, 'NO_PROXY': '*', 'PYTHONDONTWRITEBYTECODE': '1',
           'WEBHOOK_SECURITY_MODE': 'permissive', 'N8N_MCP_PACKAGE_DIR': PACKAGE}
    command = [sys.executable, '-m', 'uvicorn', 'n8n_mcp_admin.main:app', '--host', '127.0.0.1', '--port', str(gateway)]
    client = httpx.Client(timeout=20, trust_env=False)
    logs = tempfile.TemporaryFile()  # noqa: SIM115 -- closed together with subprocess
    secrets_seen = external + internal + api_keys + [admin]
    operation_ids = []
    proc = None

    def start(args, path=None):
        nonlocal proc
        process_env = env.copy()
        if path:
            process_env['MCP_ADMIN_CONFIG'] = str(path / 'config.json')
        proc = subprocess.Popen(args, env=process_env, stdout=logs, stderr=logs, start_new_session=True)
        for _ in range(200):
            if proc.poll() is not None:
                raise AssertionError('n8n admin exited before readiness')
            try:
                if client.get(base + '/healthz').status_code == 200:
                    return
            except httpx.TransportError:
                pass
            time.sleep(.1)
        raise AssertionError('n8n admin readiness timeout')

    def stop():
        nonlocal proc
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, 9)
                proc.wait(timeout=5)
            proc = None

    def login(password):
        response = client.post(base + '/api/auth/login', json={'password': password})
        assert response.status_code == 200
        token = response.json()['token']
        secrets_seen.append(token)
        return token

    def token_change(jwt, profile, operation):
        headers = {'Authorization': 'Bearer ' + jwt, 'X-MCP-Profile': profile}
        read = client.get(base + '/api/settings', headers=headers)
        assert read.status_code == 200
        operation_id = f'{int(time.time()*1000)}.{uuid.uuid4()}'
        operation_ids.append(operation_id)
        headers.update({'If-Match': read.headers['ETag'], 'Idempotency-Key': operation_id})
        response = client.post(base + '/api/tokens/' + operation, headers=headers, json={})
        assert response.status_code == 200
        token = response.json().get('token')
        if token:
            secrets_seen.append(token)
        return token

    def initialize(url, bearer=None, expected=200):
        headers = {'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json',
                   'MCP-Protocol-Version': '2025-03-26'}
        if bearer:
            headers['Authorization'] = 'Bearer ' + bearer
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-03-26', 'capabilities': {},
            'clientInfo': {'name': 'synthetic-n8n-restore', 'version': '1'}}}
        for _ in range(200):
            try:
                response = client.post(url, json=request, headers=headers)
            except httpx.TransportError:
                time.sleep(.15)
                continue
            if response.status_code in (200, 401, 403):
                break
            time.sleep(.15)
        else:
            raise AssertionError('synthetic child readiness timeout')
        assert response.status_code == expected
        if expected != 200:
            return None
        assert 'result' in rpc_data(response, 1)
        headers['Mcp-Session-Id'] = response.headers['mcp-session-id']
        response = client.post(url, headers=headers, json={'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        assert response.status_code in (200, 202, 204)
        return headers

    def call_profile(token, i, headers=None):
        url = base + '/private_' + token + '/mcp'
        headers = headers or initialize(url)
        response = client.post(url, headers=headers, json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
            'params': {'name': 'n8n_list_workflows', 'arguments': {'limit': 10}}})
        assert response.status_code == 200
        result = rpc_data(response, 2)['result']
        assert not result.get('isError')
        text = json.dumps(result)
        assert ('SYNTH_A_ONLY', 'SYNTH_B_ONLY')[i] in text
        assert ('SYNTH_B_ONLY', 'SYNTH_A_ONLY')[i] not in text
        return headers

    def search_catalog(token):
        url = base + '/private_' + token + '/mcp'
        headers = initialize(url)
        response = client.post(url, headers=headers, json={'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
            'params': {'name': 'search_nodes', 'arguments': {'query': 'webhook'}}})
        assert response.status_code == 200
        result = rpc_data(response, 3)['result']
        assert not result.get('isError')
        assert 'webhook' in json.dumps(result).lower()

    def make_snapshot(source, target):
        target.mkdir(mode=0o700)
        for relative in (Path('.'), Path('profiles') / PROFILE):
            src = source / relative
            dst = target / relative
            dst.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copy2(src / 'config.json', dst / 'config.json')
            journal = src / '.config.json.operations.json'
            if journal.exists():
                shutil.copy2(journal, dst / journal.name)
            (dst / 'n8n-mcp').mkdir(mode=0o700)
            # SQLite's backup API includes committed WAL content in one file;
            # source children have already stopped and only synthetic DBs exist.
            with sqlite3.connect(src / 'n8n-mcp' / 'nodes.db') as old, \
                    sqlite3.connect(dst / 'n8n-mcp' / 'nodes.db') as new:
                old.backup(new)
            (dst / 'n8n-mcp' / '.seed.lock').touch()

    try:
        start(command, data)
        old_jwt = login(admin)
        call_profile(external[0], 0)
        call_profile(external[1], 1)
        # Exercise the deployed child bearer, not merely the saved values.
        initialize(f'http://127.0.0.1:{port_a}/mcp', internal[0])
        initialize(f'http://127.0.0.1:{port_a}/mcp', internal[1], expected=401)
        # Seed two separate persistent history markers before the offline backup.
        stop()
        for i, directory in enumerate((data, extra)):
            with sqlite3.connect(directory / 'n8n-mcp' / 'nodes.db') as db:
                db.execute('CREATE TABLE synthetic_restore_history (marker TEXT)')
                db.execute('INSERT INTO synthetic_restore_history VALUES (?)', (f'HISTORY_{i}',))
        start(command, data)
        jwt = login(admin)
        old_b = token_change(jwt, PROFILE, 'rotate')
        old_b_operation = operation_ids[-1]
        external[1] = old_b
        call_profile(external[1], 1)
        stop()
        backup = tmp_path / 'backup'
        make_snapshot(data, backup)
        archived_before = (backup / 'profiles' / PROFILE / '.config.json.operations.json').read_bytes()
        start(command, data)
        jwt = login(admin)
        rotated = token_change(jwt, 'default', 'rotate')
        initialize(base + '/private_' + external[0] + '/mcp', expected=403)
        call_profile(rotated, 0)
        call_profile(external[1], 1)
        token_change(jwt, 'default', 'revoke')
        initialize(base + '/private_' + rotated + '/mcp', expected=403)
        call_profile(external[1], 1)
        # In this isolated test only, keep the original B online while a bad
        # separate restore is attempted. The actual n8n command must not run.
        bad_backup, bad_dest = tmp_path / 'bad-backup', tmp_path / 'bad-ready'
        shutil.copytree(backup, bad_backup)
        bad_path = bad_backup / 'profiles' / PROFILE / 'config.json'
        bad_config = json.loads(bad_path.read_text())
        bad_config['proxy']['bearer_token'] = secrets.token_urlsafe(32)
        secrets_seen.append(bad_config['proxy']['bearer_token'])
        bad_path.write_text(json.dumps(bad_config))
        bad_port = free_port()
        bad_command = [sys.executable, '-m', 'uvicorn', 'n8n_mcp_admin.main:app', '--host', '127.0.0.1',
                       '--port', str(bad_port)]
        failed = subprocess.run([sys.executable, '-m', 'mcp_admin_core.restore_guard', '--product', 'n8n',
                                 'serve', str(bad_backup), str(bad_dest), '--', *bad_command],
                                env=env, capture_output=True, text=True, timeout=30, check=False)
        assert failed.returncode != 0 and not bad_dest.exists()
        assert all(value not in failed.stdout + failed.stderr for value in secrets_seen)
        with socket.socket() as probe:
            probe.settimeout(1)
            assert probe.connect_ex(('127.0.0.1', bad_port)) != 0
        call_profile(external[1], 1)
        stop()
        ready = tmp_path / 'ready'
        restore_command = [sys.executable, '-m', 'mcp_admin_core.restore_guard', '--product', 'n8n',
                           'serve', str(backup), str(ready), '--', *command]
        start(restore_command)
        for token in (external[0], external[1], rotated):
            initialize(base + '/private_' + token + '/mcp', expected=403)
        assert client.get(base + '/api/profiles', headers={'Authorization': 'Bearer ' + old_jwt}).status_code == 401
        assert client.post(base + '/api/auth/login', json={'password': admin}).status_code == 401
        restored = [json.loads((p / 'config.json').read_text()) for p in (ready, ready / 'profiles' / PROFILE)]
        new_internal = [item['mcp_server']['env']['AUTH_TOKEN'] for item in restored]
        secrets_seen.extend(new_internal)
        unique_bearers = len(set(internal + new_internal)) == 4
        matched_pairs = all(item['proxy']['bearer_token'] == new_internal[i] for i, item in enumerate(restored))
        assert unique_bearers and matched_pairs
        for i, port in enumerate((port_a, port_b)):
            initialize(f'http://127.0.0.1:{port}/mcp', internal[i], expected=401)
            initialize(f'http://127.0.0.1:{port}/mcp', new_internal[1 - i], expected=401)
            initialize(f'http://127.0.0.1:{port}/mcp', new_internal[i])
            with sqlite3.connect((ready, ready / 'profiles' / PROFILE)[i] / 'n8n-mcp' / 'nodes.db') as db:
                assert db.execute('SELECT marker FROM synthetic_restore_history').fetchone() == (f'HISTORY_{i}',)
        new_password = restored[0]['admin_password']
        secrets_seen.append(new_password)
        new_jwt = login(new_password)
        archives = list((ready / 'profiles' / PROFILE / 'restore-operation-journals').iterdir())
        assert len(archives) == 1 and archives[0].read_bytes() == archived_before
        selected = {'Authorization': 'Bearer ' + new_jwt, 'X-MCP-Profile': PROFILE}
        etag = client.get(base + '/api/settings', headers=selected).headers['ETag']
        old_request = client.post(base + '/api/tokens/rotate', json={}, headers={**selected,
            'If-Match': etag, 'Idempotency-Key': old_b_operation})
        assert old_request.status_code == 410
        fresh_a = token_change(new_jwt, 'default', 'rotate')
        fresh_b = token_change(new_jwt, PROFILE, 'rotate')
        search_catalog(fresh_a)
        search_catalog(fresh_b)
        call_profile(fresh_a, 0)
        sibling_session = call_profile(fresh_b, 1)
        # Rotating and revoking A never changes B's session or bearer.
        newest_a = token_change(new_jwt, 'default', 'rotate')
        initialize(base + '/private_' + fresh_a + '/mcp', expected=403)
        call_profile(fresh_b, 1, sibling_session)
        token_change(new_jwt, 'default', 'revoke')
        initialize(base + '/private_' + newest_a + '/mcp', expected=403)
        call_profile(fresh_b, 1, sibling_session)
        fresh_again = token_change(new_jwt, 'default', 'rotate')
        call_profile(fresh_again, 0)
        call_profile(fresh_b, 1)
        assert set(observed) == {0, 1}
        stop()
        start(command, data)
        initialize(base + '/private_' + external[0] + '/mcp', expected=403)
        initialize(base + '/private_' + rotated + '/mcp', expected=403)
        call_profile(external[1], 1)
        stop()
        logs.seek(0)
        output = logs.read()
        assert all(token.encode() not in output for token in secrets_seen)
    finally:
        stop()
        logs.close()
        client.close()
        backend.shutdown()
        backend.server_close()
        thread.join(timeout=3)
