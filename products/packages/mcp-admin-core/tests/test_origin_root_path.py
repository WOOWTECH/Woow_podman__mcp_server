"""The same HTTP policy applies when Starlette strips an ASGI root or mount prefix."""
import copy
import json

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Mount

METHODS = ['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'TRACE', 'CONNECT', 'CUSTOM']
MODES = ['root_path', 'root_path_stripped', 'mount', 'nested_mount', 'root_path_mount']


def _transport(app, mode):
    if mode == 'root_path':
        return httpx.ASGITransport(app=app, root_path='/gateway'), '/gateway'
    if mode == 'root_path_stripped':
        return httpx.ASGITransport(app=app, root_path='/gateway'), ''
    if mode == 'mount':
        return httpx.ASGITransport(app=Starlette(routes=[Mount('/gateway', app=app)])), '/gateway'
    if mode == 'root_path_mount':
        parent = Starlette(routes=[Mount('/gateway', app=app)])
        return httpx.ASGITransport(app=parent, root_path='/outer'), '/outer/gateway'
    nested = Starlette(routes=[Mount('/outer', app=Starlette(routes=[Mount('/gateway', app=app)]))])
    return httpx.ASGITransport(app=nested), '/outer/gateway'


@pytest.mark.parametrize('mode', MODES)
@pytest.mark.parametrize('method', METHODS)
async def test_invalid_origin_denied_at_effective_route_for_all_methods(make_app, monkeypatch, mode, method):
    monkeypatch.setenv('MCP_ADMIN_ORIGIN_MODE', 'enforce')  # 預設 report-only 不擋；這裡驗 403
    monkeypatch.setenv('MCP_ADMIN_ALLOWED_ORIGINS', 'https://trusted.example')
    app = make_app({'mcp_auth_token': 'synthetic-root-token'}).app
    transport, prefix = _transport(app, mode)
    async with httpx.AsyncClient(transport=transport, base_url='http://testserver') as client:
        response = await client.request(method, prefix + '/private_synthetic-root-token/mcp',
                                        headers={'Origin': 'https://evil.example'})
    assert response.status_code == 403
    assert 'access-control-allow-origin' not in response.headers
    assert 'mcp-session-id' not in response.headers


@pytest.mark.parametrize('mode', MODES)
async def test_root_path_preflight_and_native_clients_use_same_policy(make_app, monkeypatch, mode):
    monkeypatch.setenv('MCP_ADMIN_ORIGIN_MODE', 'enforce')  # 預設 report-only 不擋；這裡驗 403
    monkeypatch.setenv('MCP_ADMIN_ALLOWED_ORIGINS', 'https://trusted.example')
    app = make_app({'mcp_auth_token': 'synthetic-root-token'}).app
    transport, prefix = _transport(app, mode)
    path = prefix + '/private_synthetic-root-token/mcp'
    preflight = {'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'content-type,mcp-session-id'}
    init = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
        'protocolVersion': '2025-03-26', 'capabilities': {}, 'clientInfo': {'name': 'root-path-test', 'version': '1'}}}
    async with httpx.AsyncClient(transport=transport, base_url='http://testserver') as client:
        bad = await client.options(path, headers={**preflight, 'Origin': 'https://evil.example'})
        assert bad.status_code == 403 and 'access-control-allow-origin' not in bad.headers
        good = await client.options(path, headers={**preflight, 'Origin': 'https://trusted.example'})
        assert good.status_code == 200 and good.headers['access-control-allow-origin'] == 'https://trusted.example'
        for origin in (None, 'https://trusted.example'):
            headers = {'Accept': 'application/json, text/event-stream'}
            if origin is not None:
                headers['Origin'] = origin
            response = await client.post(path, headers=headers, json=init)
            assert response.status_code == 200
        invalid = await client.post(prefix + '/private_invalid/mcp', json=init)
        assert invalid.status_code == 403


@pytest.mark.parametrize('mode', MODES)
async def test_root_path_never_bypasses_admin_authentication(make_app, mode):
    app = make_app({'mcp_auth_token': 'synthetic-root-token'}).app
    transport, prefix = _transport(app, mode)
    async with httpx.AsyncClient(transport=transport, base_url='http://testserver') as client:
        for method, suffix in [('GET', '/api/settings'), ('GET', '/api/tokens'),
                               ('POST', '/api/settings/mcp_auth_token/reveal')]:
            assert (await client.request(method, prefix + suffix)).status_code == 401


@pytest.mark.parametrize('mode', MODES)
async def test_root_path_selects_profile_by_token(make_app, tmp_config_path, monkeypatch, mode, synthetic_child):
    import mcp_admin_core.proxy as proxy
    from mcp_admin_core.config.store import _DEFAULT_CONFIG

    profile_id = '0123456789abcdef01234567'
    config = {'mcp_auth_token': 'synthetic-default', 'mcp_server': {'port': 17401},
              'profile_index': {profile_id: {'label': 'Second', 'port': 17402}}}
    app = make_app(config).app
    profile = copy.deepcopy(_DEFAULT_CONFIG)
    profile.update({'mcp_auth_token': 'synthetic-second', 'mcp_server': {'port': 17402}})
    dest = tmp_config_path.parent / 'profiles' / profile_id / 'config.json'
    dest.parent.mkdir(parents=True, mode=0o700)
    dest.write_text(json.dumps(profile)); dest.chmod(0o600)
    reached = []
    def upstream(request):
        reached.append(request.url.port)
        return httpx.Response(200, json={'ok': True})
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as backend:
        monkeypatch.setattr(proxy, '_client', backend)
        transport, prefix = _transport(app, mode)
        async with httpx.AsyncClient(transport=transport, base_url='http://testserver') as client:
            for token in ('synthetic-default', 'synthetic-second'):
                response = await client.get(prefix + '/private_' + token + '/health')
                assert response.status_code == 200
    assert reached == [17401, 17402]


@pytest.mark.parametrize('mode', MODES)
async def test_encoded_private_prefix_is_checked_using_the_routed_path(make_app, monkeypatch, mode):
    monkeypatch.setenv('MCP_ADMIN_ORIGIN_MODE', 'enforce')  # 預設 report-only 不擋；這裡驗 403
    monkeypatch.setenv('MCP_ADMIN_ALLOWED_ORIGINS', 'https://trusted.example')
    app = make_app({'mcp_auth_token': 'synthetic-root-token'}).app
    transport, prefix = _transport(app, mode)
    async with httpx.AsyncClient(transport=transport, base_url='http://testserver') as client:
        response = await client.get(prefix + '/%70rivate_synthetic-root-token/mcp',
                                    headers={'Origin': 'https://evil.example'})
    assert response.status_code == 403
