"""Real product gateways and child processes, with two independent profiles each."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import time

import httpx
import pytest

from mcp_admin_core.config.store import _DEFAULT_CONFIG

PRODUCTS = {
    'emqx': {'base_url': 'https://emqx.invalid', 'api_key': 'synthetic-key', 'api_secret': 'synthetic-secret'},
    'nextcloud': {'base_url': 'https://nextcloud.invalid', 'username': 'synthetic-user', 'app_password': 'synthetic-password'},
    'litellm': {},
    'hermes': {},
    'odoo': {},
    'n8n': {},
    'headscale': {'base_url': 'https://headscale.invalid', 'api_key': 'synthetic-key'},
}


def _free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def _initialize(client, path, headers):
    return client.post(path, headers=headers, json={
        'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-03-26', 'capabilities': {},
            'clientInfo': {'name': 'origin-integration-test', 'version': '1'}}})


def _response_object(response):
    text = response.text
    if text.startswith('event:') or text.startswith('data:'):
        text = next(line[5:].strip() for line in text.splitlines() if line.startswith('data:'))
    return json.loads(text)


@pytest.mark.parametrize('mode', ['enforce', 'report-only'])
@pytest.mark.parametrize('product', list(PRODUCTS))
@pytest.mark.parametrize('root_path', ['', '/gateway'])
def test_origin_boundary_with_real_product_and_two_profiles(tmp_path, product, root_path, mode):
    """MCP_ADMIN_ORIGIN_MODE 明確設定（預設是 report-only）：enforce 擋沒通過的 Origin；report-only 時 Headscale 以外的產品
    照常服務（#136 之前的 CORS：原樣回 Origin、允許帶憑證），Headscale 一律照樣 403。"""
    enforced = mode == 'enforce' or product == 'headscale'
    if product == 'n8n':
        upstream = os.environ.get('N8N_MCP_PACKAGE_DIR') or str(
            Path(__file__).resolve().parents[3] / 'apps/n8n/upstream-dependencies/node_modules/n8n-mcp')
        if not Path(upstream, 'dist/mcp/index.js').is_file():
            pytest.skip('n8n-mcp 2.96.2 runtime not installed')
    profile_id = '0123456789abcdef01234567'
    tokens = [secrets.token_urlsafe(32), secrets.token_urlsafe(32)]
    ports = set()
    while len(ports) < 3:
        ports.add(_free_port())
    gateway_port, first_port, second_port = sorted(ports)
    cfgs = []
    for token, port in zip(tokens, (first_port, second_port)):
        config = copy.deepcopy(_DEFAULT_CONFIG)
        config['admin_password'] = 'synthetic-admin-password-only'
        config['mcp_auth_token'] = token
        config['connection'] = PRODUCTS[product]
        config['mcp_server']['port'] = port
        if product == 'n8n':
            child_token = secrets.token_urlsafe(32)
            config['mcp_server']['env'] = {'AUTH_TOKEN': child_token}
            config['proxy']['bearer_token'] = child_token
        cfgs.append(config)
    cfgs[0]['profile_index'] = {profile_id: {'label': 'Second', 'port': second_port}}
    root = tmp_path / 'config.json'
    child = tmp_path / 'profiles' / profile_id / 'config.json'
    child.parent.mkdir(parents=True, mode=0o700)
    for path, config in ((root, cfgs[0]), (child, cfgs[1])):
        path.write_text(json.dumps(config))
        path.chmod(0o600)
    env = os.environ.copy()
    env.update({'MCP_ADMIN_CONFIG': str(root), 'MCP_ADMIN_ALLOWED_ORIGINS': 'https://service.example',
                'HEADSCALE_MCP_ALLOWED_ORIGINS': 'https://service.example', 'MCP_ADMIN_ORIGIN_MODE': mode,
                'FASTMCP_CHECK_FOR_UPDATES': 'off', 'FASTMCP_SHOW_SERVER_BANNER': 'false',
                'PATH': str(Path(sys.executable).parent) + os.pathsep + os.environ.get('PATH', '')})
    if product == 'n8n':
        env['N8N_MCP_PACKAGE_DIR'] = upstream
    command = [sys.executable, '-m', 'uvicorn', product + '_mcp_admin.main:app',
               '--host', '127.0.0.1', '--port', str(gateway_port), '--no-access-log']
    if root_path:
        command.extend(['--root-path', root_path])
    proc = subprocess.Popen(command,
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{gateway_port}', timeout=3, trust_env=False) as client:
            headers = {'Accept': 'application/json, text/event-stream'}
            for token in tokens:
                path = '/private_' + token + '/mcp'
                deadline = time.monotonic() + 40
                while True:
                    assert proc.poll() is None, f'{product} gateway exited before initialize'
                    try:
                        response = _initialize(client, path, headers)
                        session = response.headers.get('mcp-session-id', '')
                        if response.status_code == 200 and session and not session.startswith('woow-mcp-setup-'):
                            break
                        if session:
                            client.delete(path, headers={**headers, 'Mcp-Session-Id': session})
                    except httpx.HTTPError:
                        pass
                    assert time.monotonic() < deadline, f'{product} child did not initialize'
                    time.sleep(.2)
                assert _response_object(response)['result']['serverInfo']['name']
                assert client.delete(path, headers={**headers, 'Mcp-Session-Id': session}).status_code in (200, 202, 204)
                for origin in ('https://evil.example', 'null', 'not-an-origin'):
                    denied = _initialize(client, path, {**headers, 'Origin': origin,
                                         'Host': 'evil.example', 'X-Forwarded-Host': 'evil.example',
                                         'X-Forwarded-Proto': 'https'})
                    if enforced:
                        assert denied.status_code == 403
                        assert 'mcp-session-id' not in denied.headers
                        assert 'access-control-allow-origin' not in denied.headers
                        continue
                    # report-only：真的子行程照常回答（不是佔位 session），帶 #136 之前的 CORS 標頭。
                    assert denied.status_code == 200
                    assert denied.headers['access-control-allow-origin'] == origin
                    assert denied.headers['access-control-allow-credentials'] == 'true'
                    served = denied.headers['mcp-session-id']
                    assert not served.startswith('woow-mcp-setup-')
                    assert _response_object(denied)['result']['serverInfo']['name']
                    assert client.delete(path, headers={**headers, 'Mcp-Session-Id': served}).status_code in (200, 202, 204)
                # Simulate TLS termination at an ingress: Origin and Host agree,
                # while the gateway itself is reached over loopback HTTP.
                browser = {'Origin': 'https://service.example', 'Host': 'service.example',
                           'X-Forwarded-Proto': 'https'}
                good = _initialize(client, path, {**headers, **browser})
                assert good.status_code == 200
                assert good.headers['access-control-allow-origin'] == 'https://service.example'
                session = good.headers['mcp-session-id']
                assert not session.startswith('woow-mcp-setup-')
                assert client.delete(path, headers={**headers, 'Mcp-Session-Id': session}).status_code in (200, 202, 204)
                preflight = {'Access-Control-Request-Method': 'POST',
                             'Access-Control-Request-Headers': 'content-type,mcp-session-id,mcp-protocol-version'}
                bad = client.options(path, headers={**preflight, 'Origin': 'https://evil.example'})
                if enforced:
                    assert bad.status_code == 403 and 'access-control-allow-origin' not in bad.headers
                else:  # report-only：#136 之前的 preflight 回答
                    assert bad.status_code == 200 and bad.headers['access-control-allow-origin'] == 'https://evil.example'
                    assert 'POST' in bad.headers['access-control-allow-methods']
                good = client.options(path, headers={**preflight, **browser})
                assert good.status_code == 200 and good.headers['access-control-allow-origin'] == 'https://service.example'
                assert 'POST' in good.headers['access-control-allow-methods']
            assert _initialize(client, '/private_wrong/mcp', headers).status_code == 403
    finally:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
