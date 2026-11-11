#!/usr/bin/env python3
"""Loopback child protocol smoke; synthetic HTTP fixture, no real-broker claim.

Run with the image's Python. Uses only owned temp files/sockets, no cluster API.
"""
import asyncio
import base64
import json
import os
from pathlib import Path
import socket
import signal
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ['FASTMCP_CHECK_FOR_UPDATES'] = 'off'
os.environ['FASTMCP_SHOW_SERVER_BANNER'] = 'false'
from fastmcp import Client


async def smoke():
    effects = []
    sql_requests = []
    auth = 'Basic ' + base64.b64encode(b'ci-owned-key:ci-owned-secret').decode()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            self.respond()
        def do_POST(self):
            self.respond()
        def respond(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            if self.headers.get('Authorization') != auth:
                status, value = 401, {}
            elif self.path == '/api/v5/stats':
                status, value = 200, [{'connections.count': 3}]
            elif self.path == '/api/v5/publish' and self.command == 'POST':
                effects.append(json.loads(body))
                status, value = 200, {'id': 'owned-id', 'reason_code': 0}
            elif self.path == '/api/v5/rule_test' and self.command == 'POST':
                sql_requests.append(json.loads(body))
                status, value = 200, [{'owned': True}]
            else:
                status, value = 404, {}
            raw = json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
    thread.start()
    proc = None
    try:
        with tempfile.TemporaryDirectory(prefix='emqx-child-smoke-', dir=os.environ.get('TMPDIR', '/tmp')) as directory:
            root = Path(directory)
            policy = root / 'config.json'
            # Explicit fixture restriction, independent of the app's all-on seed.
            policy.write_text('{"tools":{"disabled":["emqx_publish"]}}')
            policy.chmod(0o600)
            with socket.socket() as port_socket:
                port_socket.bind(('127.0.0.1', 0))
                port = port_socket.getsockname()[1]
            env = {**os.environ, 'MCP_ADMIN_CONFIG': str(policy), 'WEBHOOK_SECURITY_MODE': 'moderate',
                   'EMQX_MCP_BASE_URL': f'http://127.0.0.1:{server.server_port}',
                   'EMQX_MCP_API_KEY': 'ci-owned-key', 'EMQX_MCP_API_SECRET': 'ci-owned-secret',
                   'PYTHONDONTWRITEBYTECODE': '1'}
            with (root / 'child.log').open('w+') as log:
                for generation in range(2):
                    proc = subprocess.Popen([sys.executable, '-m', 'emqx_mcp_admin.launcher', '--port', str(port)],
                                            env=env, stdout=log, stderr=log)
                    for attempt in range(150):
                        if proc.poll() is not None:
                            raise RuntimeError('EMQX child exited; inspect owned smoke log before cleanup')
                        try:
                            with socket.create_connection(('127.0.0.1', port), timeout=.1):
                                break
                        except OSError:
                            await asyncio.sleep(.1)
                    else:
                        raise RuntimeError('EMQX child startup timeout')
                    async with Client(f'http://127.0.0.1:{port}/mcp') as client:
                        names = {t.name for t in await client.list_tools()}
                        assert len(names) == 38
                        assert ('emqx_test_rule_sql' in names) == (generation == 0)
                        assert not (await client.call_tool('emqx_broker_stats')).is_error
                        sql = {'sql': 'SELECT * FROM "owned/#"'}
                        if generation == 0:
                            assert not (await client.call_tool('emqx_test_rule_sql', sql)).is_error
                            assert len(sql_requests) == 1
                            policy.write_text('{"tools":{"disabled":["emqx_publish","emqx_test_rule_sql"]}}')
                            args = {'topic': 'owned/smoke', 'payload': 'owned-message'}
                            assert (await client.call_tool('emqx_publish', args, raise_on_error=False)).is_error
                            assert effects == []
                            policy.write_text('{"tools":{"disabled":["emqx_test_rule_sql"]}}')
                            assert not (await client.call_tool('emqx_publish', args)).is_error
                            assert len(effects) == 1 and effects[0]['payload'] == 'owned-message'
                        assert (await client.call_tool('emqx_test_rule_sql', sql, raise_on_error=False)).is_error
                        assert len(sql_requests) == 1  # Denied now and after restart.
                    proc.terminate()
                    proc.wait(timeout=10)
                    # Uvicorn re-raises SIGTERM after completing graceful shutdown.
                    assert proc.returncode in (0, -signal.SIGTERM), proc.returncode
                    proc = None
                log.seek(0)
                output = log.read()
                assert 'ci-owned-secret' not in output and 'ci-owned-key' not in output and auth not in output
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    return {'status': 'PASS', 'fixture': 'synthetic-local-http', 'child_generations': 2,
            'permitted_write_effects': len(effects), 'denied_write_effects': 0,
            'sql_allowed_requests': len(sql_requests), 'sql_revoked_requests': 0,
            'saved_policy_survived_child_restart': True, 'cleanup': 'owned-processes-sockets-tempfiles-closed'}


if __name__ == '__main__':
    print(json.dumps(asyncio.run(smoke()), sort_keys=True))
