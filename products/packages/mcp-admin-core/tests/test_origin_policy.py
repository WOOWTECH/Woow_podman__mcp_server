"""Origin enforcement at the MCP gateway's public HTTP boundary.

預設模式自 2026-11-12 起是 report-only（不擋）：驗 Origin 403 的測試都明確設 MCP_ADMIN_ORIGIN_MODE=enforce；其他測試
（清單不合法、可信來源、非 MCP 路徑）與模式無關。report-only 見 test_origin_report_only.py。
"""

import pytest


def test_untrusted_origin_with_valid_token_cannot_initialize(make_app, monkeypatch):
    monkeypatch.setenv('MCP_ADMIN_ORIGIN_MODE', 'enforce')
    client = make_app({'mcp_auth_token': 'synthetic-origin-token'})
    body = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
        'protocolVersion': '2025-03-26', 'capabilities': {},
        'clientInfo': {'name': 'origin-test', 'version': '1'}}}
    headers = {'Accept': 'application/json, text/event-stream'}
    assert client.post('/private_synthetic-origin-token/mcp', headers=headers, json=body).status_code == 200
    response = client.post('/private_synthetic-origin-token/mcp',
        headers={**headers, 'Origin': 'https://untrusted.example'}, json=body)
    assert response.status_code == 403


@pytest.mark.parametrize('headers', [
    [('Origin', '')], [('Origin', 'null')], [('Origin', 'not-an-origin')],
    [('Origin', 'https://service.example/')], [('Origin', 'https://user@service.example')],
    [('Origin', 'https://service.example:65536')], [('Origin', 'https://service.example.evil')],
    [('Origin', 'https://service.example'), ('Origin', 'https://service.example')],
    [('Origin', 'https://evil.example'), ('Host', 'evil.example'),
     ('X-Forwarded-Host', 'evil.example'), ('X-Forwarded-Proto', 'https'),
     ('Forwarded', 'host=evil.example;proto=https')],
])
def test_rejects_bad_origins_even_if_host_or_proxy_headers_are_spoofed(make_app, monkeypatch, headers):
    monkeypatch.setenv('MCP_ADMIN_ORIGIN_MODE', 'enforce')
    monkeypatch.setenv('MCP_ADMIN_ALLOWED_ORIGINS', 'https://service.example')
    client = make_app({'mcp_auth_token': 'synthetic-origin-token'})
    assert client.get('/private_synthetic-origin-token/mcp', headers=headers).status_code == 403


def test_preflight_is_limited_to_explicitly_trusted_origins(make_app, monkeypatch):
    monkeypatch.setenv('MCP_ADMIN_ORIGIN_MODE', 'enforce')
    monkeypatch.setenv('MCP_ADMIN_ALLOWED_ORIGINS', 'https://service.example')
    client = make_app({'mcp_auth_token': 'synthetic-origin-token'})
    endpoint = '/private_synthetic-origin-token/mcp'
    preflight = {'Access-Control-Request-Method': 'POST',
                 'Access-Control-Request-Headers': 'content-type,mcp-session-id,mcp-protocol-version'}
    bad = client.options(endpoint, headers={**preflight, 'Origin': 'https://evil.example',
                                           'X-Forwarded-Host': 'evil.example'})
    assert bad.status_code == 403
    assert 'access-control-allow-origin' not in bad.headers
    good = client.options(endpoint, headers={**preflight, 'Origin': 'https://service.example'})
    assert good.status_code == 200
    assert good.headers['access-control-allow-origin'] == 'https://service.example'
    assert 'POST' in good.headers['access-control-allow-methods']
    assert 'mcp-session-id' in good.headers['access-control-allow-headers']
    assert good.headers['access-control-allow-credentials'] == 'true'
    assert 'origin' in good.headers['vary'].lower()
    denied = client.options(endpoint, headers={**preflight, 'Origin': 'https://service.example',
                                              'Access-Control-Request-Headers': 'x-unapproved'})
    assert denied.status_code == 403
    denied_method = client.options(endpoint, headers={**preflight, 'Origin': 'https://service.example',
                                                     'Access-Control-Request-Method': 'PATCH'})
    assert denied_method.status_code == 403


def test_trusted_origin_still_requires_token_and_exposes_session_header(make_app, monkeypatch):
    monkeypatch.setenv('MCP_ADMIN_ALLOWED_ORIGINS', 'https://service.example')
    client = make_app({'mcp_auth_token': 'synthetic-origin-token'})
    headers = {'Origin': 'https://service.example', 'Accept': 'application/json, text/event-stream'}
    body = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
        'protocolVersion': '2025-03-26', 'capabilities': {},
        'clientInfo': {'name': 'origin-test', 'version': '1'}}}
    denied = client.post('/private_wrong/mcp', headers=headers, json=body)
    assert denied.status_code == 403
    accepted = client.post('/private_synthetic-origin-token/mcp', headers=headers, json=body)
    assert accepted.status_code == 200
    assert accepted.headers['access-control-allow-origin'] == 'https://service.example'
    assert 'mcp-session-id' in accepted.headers['access-control-expose-headers'].lower()


@pytest.mark.parametrize('value', ['*', 'null', 'https://service.example/path', 'https://service.example,'])
def test_misconfigured_allowlist_fails_closed_even_without_origin(make_app, monkeypatch, value):
    monkeypatch.setenv('MCP_ADMIN_ALLOWED_ORIGINS', value)
    client = make_app({'mcp_auth_token': 'synthetic-origin-token'})
    assert client.get('/private_synthetic-origin-token/mcp').status_code == 503


def test_misconfigured_allowlist_prevents_service_start(make_app, monkeypatch):
    monkeypatch.setenv('MCP_ADMIN_ALLOWED_ORIGINS', '*')
    client = make_app({'mcp_auth_token': 'synthetic-origin-token'})
    with pytest.raises(RuntimeError, match='Invalid MCP Origin allowlist'):
        with client:
            pass


def test_default_cors_does_not_reflect_untrusted_origins_on_other_routes(make_app):
    client = make_app()
    response = client.options('/api/auth/login', headers={
        'Origin': 'https://evil.example', 'Access-Control-Request-Method': 'POST'})
    assert response.status_code == 400
    assert 'access-control-allow-origin' not in response.headers


def test_non_headscale_rejects_trailing_dot_allowlist(make_app, monkeypatch):
    monkeypatch.setenv('MCP_ADMIN_ALLOWED_ORIGINS', 'https://service.example.')
    client = make_app({'mcp_auth_token': 'synthetic-origin-token'})
    assert client.get('/private_synthetic-origin-token/mcp').status_code == 503
