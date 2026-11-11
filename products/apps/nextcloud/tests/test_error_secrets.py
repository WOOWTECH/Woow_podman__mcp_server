"""真 launcher／子行程／PaaS gateway 的錯誤遮罩；只連自有 HTTP 假後端。"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import shutil
from urllib.parse import quote
from xml.sax.saxutils import escape

import httpx
import pytest

from .conftest import AuthCheckingNextcloud
from .fake_nextcloud import FakeNextcloud, LOGIN
from .test_child_process import INITIALIZE, MCP_HEADERS, _FakeNextcloudHTTP, _free_port, _wait_tools

PASSWORD = 'FAKE-paas-pass/+"quoted-4917'
# 週期性密碼（overlap）：回聲多一個週期時，兩個出現位置重疊。
PERIOD = 'FAKE-period-'
DIAGNOSTIC = 'FAKE quota exhausted on isolated backend'
TOKEN = 'FAKE-paas-mcp-token-20261029'
UNKNOWN_TOKEN = 'FAKE-unknown-backend-token-20261101'
INTERNAL_HOST = 'nextcloud-db.internal.svc.cluster.local'


def _result(response):
    assert response.status_code == 200
    if response.headers['content-type'].startswith('application/json'):
        return response.json()['result']
    return next(json.loads(line[6:])['result'] for line in response.text.splitlines()
                if line.startswith('data: ') and '"result"' in line)


@pytest.mark.parametrize('json_response', [False, True], ids=['sse', 'json'])
@pytest.mark.parametrize('form', ['raw', 'basic', 'url', 'json', 'base64', 'folded', 'cut',
                                  'json-solidus', 'overlap'])
async def test_error_diagnostic_never_reflects_credentials_or_internal_host(
    tmp_path, monkeypatch, caplog, capfd, json_response, form,
):
    import mcp_admin_core.config.store as store_mod
    import mcp_admin_core.process as process_mod
    import mcp_admin_core.proxy as proxy_mod
    from mcp_admin_core.launch_policy import get_launch_policy, set_launch_policy
    from nextcloud_mcp_admin import main as admin_main

    # 核心回歸不可因缺少 console script 被靜默略過。
    assert shutil.which('nextcloud-mcp-server'), '請先 uv sync 安裝真 launcher'
    password = {'folded': 'FAKE-paas-pass  spaced-4917', 'overlap': PERIOD * 2}.get(form, PASSWORD)
    exposed = {
        'raw': password,
        'basic': base64.b64encode(f'{LOGIN}:{password}'.encode()).decode(),
        'url': quote(password, safe=''),
        'json': json.dumps(password)[1:-1],
        'base64': base64.b64encode(password.encode()).decode(),
        'folded': ' '.join(password.split()),
        'cut': password[:16],
        # 以下兩種自上游 v0.1.6 起遮罩。Nextcloud 是 PHP，json_encode 預設把 / 寫成 \/。
        'json-solidus': json.dumps(password)[1:-1].replace('/', '\\/'),
        # 整段回聲（含重疊的兩次出現）只剩一個 [REDACTED]，不留後半段。
        'overlap': PERIOD * 3,
    }[form]
    detail = (f'{DIAGNOSTIC}; credential echo {exposed}; token={UNKNOWN_TOKEN}; '
              f'host={INTERNAL_HOST}; end diagnostic')
    if form == 'folded':
        detail = f'{DIAGNOSTIC}; credential echo {password}; token={UNKNOWN_TOKEN}; host={INTERNAL_HOST}'
    elif form == 'cut':
        detail = (DIAGNOSTIC + 'x' * (184 - len(DIAGNOSTIC)) + password +
                  f'; token={UNKNOWN_TOKEN}; host={INTERNAL_HOST}')
    mode = {'status': 422, 'detail': detail}
    fake = FakeNextcloud()

    def fail(request):
        assert request.method == 'PROPFIND'
        body = ('<d:error xmlns:d="DAV:" xmlns:s="http://sabredav.org/ns">'
                f'<s:message>{escape(mode["detail"])}</s:message></d:error>')
        return httpx.Response(mode['status'], content=body.encode(),
                              headers={'Content-Type': 'application/xml'})

    fake.override = fail
    authed = AuthCheckingNextcloud(fake, password=password)
    caplog.set_level(logging.INFO, logger='mcp_admin_core.process')
    monkeypatch.setenv('FASTMCP_JSON_RESPONSE', str(json_response).lower())
    failures = []
    with _FakeNextcloudHTTP(authed) as backend:
        port = _free_port()
        cfg = json.loads(json.dumps(store_mod._DEFAULT_CONFIG))
        cfg['mcp_auth_token'] = TOKEN
        cfg['connection'] = {'base_url': backend.base_url, 'username': LOGIN, 'app_password': password}
        cfg['tools'] = {'disabled': [], 'nextcloud_policy_seeded': True}
        cfg['mcp_server'] = {'command': 'nextcloud-mcp-server', 'args': ['--port', str(port)],
                             'port': port, 'env': {}, 'env_prefix': 'NEXTCLOUD_MCP_'}
        path = tmp_path / 'config.json'
        path.write_text(json.dumps(cfg))
        monkeypatch.setenv('MCP_ADMIN_CONFIG', str(path))
        monkeypatch.setattr(store_mod, '_instance', None)
        monkeypatch.setattr(process_mod, '_instance', None)
        monkeypatch.setattr(proxy_mod, '_client', None)
        previous_policy = get_launch_policy()
        set_launch_policy(admin_main.LAUNCH_POLICY)
        try:
            async with admin_main.app.router.lifespan_context(admin_main.app):
                pm = process_mod.get_process_manager()
                await _wait_tools(f'http://127.0.0.1:{port}/mcp')
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=admin_main.app),
                                             base_url='https://mcp.example.test', timeout=30) as client:
                    url = f'/private_{TOKEN}/mcp'
                    init = await client.post(url, json=INITIALIZE, headers=MCP_HEADERS)
                    assert 'Woow Nextcloud' in init.text
                    headers = {**MCP_HEADERS, 'Mcp-Session-Id': init.headers['mcp-session-id']}
                    await client.post(url, headers=headers,
                                      json={'jsonrpc': '2.0', 'method': 'notifications/initialized'})

                    async def call(index):
                        response = await client.post(url, headers=headers, json={
                            'jsonrpc': '2.0', 'id': index, 'method': 'tools/call',
                            'params': {'name': 'get_file_tree', 'arguments': {'path': 'x'}},
                        })
                        assert response.headers['content-type'].startswith(
                            'application/json' if json_response else 'text/event-stream')
                        result = _result(response)
                        assert result['isError'] is True
                        return '\n'.join(item.get('text', '') for item in result['content'])

                    text = await call(2)
                    assert text == (
                        'BACKEND_HTTP_ERROR status=422: '
                        'Nextcloud refused the request for "x" (422): storage quota exhausted.')
                    if any(value in text for value in
                           (password, exposed, UNKNOWN_TOKEN, INTERNAL_HOST, DIAGNOSTIC)):
                        failures.append('422 tool response')
                    # 5xx only carries the same fixed diagnostic category.
                    mode['status'] = 500
                    assert await call(3) == (
                        'BACKEND_HTTP_ERROR status=500: '
                        'Nextcloud returned an error (500); try again later. '
                        'Backend diagnostic: storage quota exhausted.')
                    # Unknown free-form backend text is never relayed.
                    mode.update(status=422, detail='SAFE ' * 60)
                    assert await call(4) == (
                        'BACKEND_HTTP_ERROR status=422: Nextcloud refused the request for "x" (422).')
                await pm.stop()
                # _drain_logs 是真子行程 reader；等本次 reader 完成再查完整 log 與 UI tail。
                readers = [task for task in asyncio.all_tasks()
                           if task.get_coro().__qualname__ == 'McpProcessManager._drain_logs']
                if readers:
                    await asyncio.wait_for(asyncio.gather(*readers), timeout=5)
                captured = capfd.readouterr()
                output = '\n'.join(record.getMessage() for record in caplog.records)
                assert 'tool policy' in output
                logs = output + captured.out + captured.err + '\n'.join(pm._last_output)
                if any(value in logs for value in (password, exposed, UNKNOWN_TOKEN, INTERNAL_HOST)):
                    failures.append('child/gateway logs')
                assert TOKEN not in logs
        finally:
            set_launch_policy(previous_policy)
            if proxy_mod._client is not None:
                await proxy_mod._client.aclose()
                proxy_mod._client = None
    assert authed.auth_headers and all(header == authed.expected for header in authed.auth_headers)
    assert all(request.method in ('GET', 'PROPFIND') for request in fake.requests)
    assert not failures, {'form': form, 'transport': 'json' if json_response else 'sse', 'leaks': failures}


def test_public_error_code_prefix_is_on_by_default_and_opt_out_only_by_false():
    """2026-11-02 owner decision: the HA add-on's BACKEND_* prefix is the PaaS public text too."""
    from nextcloud_mcp_server.client import NextcloudClient
    from nextcloud_mcp_server.errors import NextcloudHTTPError, coded, render_error
    from nextcloud_mcp_server.settings import Settings

    def mode(value):
        settings = Settings(base_url='https://nc.example.test', username='u',
                            app_password='p', error_codes=value)
        return NextcloudClient(settings, policy=None).code_mode

    assert mode('auto') is True and mode('true') is True and mode('false') is False
    http_error = NextcloudHTTPError('Permission denied for "x".', 403)
    assert str(render_error(http_error, True)) == 'BACKEND_HTTP_ERROR status=403: Permission denied for "x".'
    assert str(render_error(coded('Could not reach Nextcloud (timed out).', 'BACKEND_TIMEOUT'), True)) == (
        'BACKEND_TIMEOUT: Could not reach Nextcloud (timed out).')
