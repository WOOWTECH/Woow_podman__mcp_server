"""Owned loopback HTTP fixture, not acceptance against an EMQX image."""
import base64
import logging
import socket
import ssl
import subprocess
import pytest
from fastmcp import Client
from emqx_mcp_server.server import build_server
from emqx_mcp_server.http_policy import BrokerClient, Destination, DestinationDenied, MAX_RESPONSE_BYTES
from emqx_mcp_server.errors import emqx_request, json_body, EmqxApiError


@pytest.fixture
def policy(tmp_path):
    path = tmp_path / 'policy.json'
    path.write_text('{"tools":{"disabled":[]}}')
    path.chmod(0o600)
    return path


@pytest.fixture
async def http(broker):
    client = BrokerClient(broker.url, 'synthetic-api-key', 'synthetic-api-secret', .1)
    yield client
    await client.aclose()
    assert client.is_closed


@pytest.mark.parametrize('status', [401, 403, 404, 409, 429, 500, 503])
async def test_http_errors_are_errors_without_body_credentials(status, broker, http, caplog):
    broker.status = status
    broker.response = {'message': 'synthetic-api-secret synthetic-api-key'}
    with pytest.raises(EmqxApiError) as result:
        await emqx_request(http, 'POST', '/publish', json={'topic': 'owned', 'payload': 'x'})
    assert str(status) in str(result.value)
    assert 'synthetic-api-' not in str(result.value) + caplog.text
    assert len(broker.calls) == 1


@pytest.mark.parametrize('tool', ['emqx_get_retained', 'emqx_delete_retained'])
async def test_retained_error_does_not_trigger_another_operation(tool, broker, policy):
    broker.status = 503
    async with Client(build_server(policy_path=policy)) as client:
        result = await client.call_tool(tool, {'topic': 'owned/topic'}, raise_on_error=False)
        assert result.is_error
    assert len(broker.calls) == 1, 'A failed/ambiguous operation must not cause another backend request'


async def test_redirect_is_not_followed(broker, http):
    broker.status = 302
    broker.headers = {'Location': broker.url + '/stolen'}
    with pytest.raises(EmqxApiError, match='REDIRECT_DENIED'):
        await emqx_request(http, 'GET', '/nodes')
    assert len(broker.calls) == 1


@pytest.mark.parametrize('body', [b'<html>login</html>', b'null', b'42', b'"secret"', b''])
async def test_invalid_json_never_claims_success(body, broker, http):
    broker.body = body
    response = await emqx_request(http, 'GET', '/nodes')
    with pytest.raises(EmqxApiError, match='INVALID_RESPONSE'):
        json_body(response)


async def test_large_and_encoded_response_fail_closed(broker, http):
    broker.body = b'x' * (MAX_RESPONSE_BYTES + 1)
    with pytest.raises(EmqxApiError, match='RESPONSE_TOO_LARGE'):
        await emqx_request(http, 'GET', '/nodes')
    broker.body = b'compressed'
    broker.headers = {'Content-Encoding': 'gzip'}
    with pytest.raises(EmqxApiError):
        await emqx_request(http, 'GET', '/nodes')


@pytest.mark.parametrize('url', [
    'http://a:b@example.com', 'http://example.com:0', 'http://example.com:99999',
    'http://example.com/path?secret=x', 'http://example.com/#x', 'file:///tmp/x',
    'http://example.com/../private', 'http://example.com/%2e%2e',
    'http://example.com/api/v5/api/v5', 'http://169.254.169.254',
])
def test_bad_destinations_never_construct_transport(url):
    with pytest.raises(DestinationDenied):
        BrokerClient(url, 'synthetic-key', 'synthetic-secret')


@pytest.mark.parametrize('path', ['/../nodes', '/clients/..', '/clients/%2e%2e',
                                 '/clients/%252e%252e', '/clients/a%5cb', '//evil.test/nodes',
                                 'http://evil.test/api/v5/nodes'])
async def test_path_escape_is_denied_before_io(path, broker, http):
    with pytest.raises(DestinationDenied):
        await http.request('GET', path)
    assert not broker.calls


def test_strict_private_and_mixed_dns_answers_are_denied(monkeypatch):
    monkeypatch.setenv('WEBHOOK_SECURITY_MODE', 'strict')
    with pytest.raises(DestinationDenied):
        Destination('http://127.0.0.1')
    def rows(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (ip, 443))
                for ip in ['8.8.8.8', '127.0.0.1']]
    monkeypatch.setattr(socket, 'getaddrinfo', rows)
    with pytest.raises(DestinationDenied):
        Destination('https://broker.example.test').answers()


async def test_dns_resolves_once_and_socket_uses_pinned_address(broker, monkeypatch):
    original = socket.getaddrinfo
    resolutions = []
    def resolve(host, port, *args, **kwargs):
        if host == 'broker.example.test':
            resolutions.append(host)
            if len(resolutions) > 1:
                raise AssertionError('Hostname was resolved again after validation')
            return original('127.0.0.1', port, *args, **kwargs)
        return original(host, port, *args, **kwargs)
    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    monkeypatch.setenv('HTTP_PROXY', 'http://127.0.0.1:1')
    monkeypatch.setenv('ALL_PROXY', 'http://127.0.0.1:1')
    client = BrokerClient(broker.url.replace('127.0.0.1', 'broker.example.test'),
                          'synthetic-api-key', 'synthetic-api-secret')
    try:
        for _ in range(2):
            assert (await emqx_request(client, 'GET', '/nodes')).status_code == 200
    finally:
        await client.aclose()
    assert len(resolutions) == 1 and len(broker.calls) == 2


async def test_mcp_lifespan_owns_one_pool_and_closes_on_error(policy, broker, monkeypatch):
    import emqx_mcp_server.lifespan as module
    original = module.make_client
    pools = []
    def make():
        pool = original()
        pools.append(pool)
        return pool
    monkeypatch.setattr(module, 'make_client', make)
    async with Client(build_server(policy_path=policy)) as client:
        await client.call_tool('emqx_broker_stats')
        broker.status = 401
        assert (await client.call_tool('emqx_cluster_status', raise_on_error=False)).is_error
        assert len(pools) == 1 and not pools[0].is_closed
    assert pools[0].is_closed


def test_credentials_masked_with_access_log_format_and_exception():
    from emqx_mcp_admin.log_redaction import set_secrets
    from uvicorn.logging import AccessFormatter
    set_secrets('owned-key', 'owned-secret/123')
    logger = logging.getLogger('uvicorn.access')
    record = logger.makeRecord('uvicorn.access', logging.INFO, '', 1,
        '%s - "%s %s HTTP/%s" %d', ('127.0.0.1:1', 'GET', '/owned-secret/123', '1.1', 200), None)
    text = AccessFormatter('%(message)s %(request_line)s', use_colors=False).format(record)
    assert 'owned-secret' not in text and '***' in text
    secret = base64.b64encode(b'owned-key:owned-secret/123').decode()
    try:
        raise ValueError(secret)
    except ValueError:
        import sys
        record = logger.makeRecord('x', logging.ERROR, '', 1, 'bad %s', ('owned-key',), sys.exc_info())
    text = logging.Formatter().format(record)
    assert secret not in text and 'owned-key' not in text


async def test_write_timeout_has_no_transport_retry(broker, http):
    broker.delay = .25
    with pytest.raises(EmqxApiError, match='EMQX_TIMEOUT'):
        await emqx_request(http, 'POST', '/publish', json={'topic': 'owned/topic', 'payload': 'owned'})
    assert len(broker.calls) == 1


async def test_tls_verification_cannot_be_disabled_by_environment(tmp_path, monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    cert, key = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                    '-subj', '/CN=localhost', '-addext', 'subjectAltName=IP:127.0.0.1',
                    '-keyout', str(key), '-out', str(cert)], check=True, capture_output=True)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
    thread.start()
    monkeypatch.setenv('SSL_CERT_FILE', str(cert))
    client = BrokerClient(f'https://127.0.0.1:{server.server_port}', 'synthetic-key', 'synthetic-secret')
    try:
        with pytest.raises(EmqxApiError, match='UNAVAILABLE'):
            await emqx_request(client, 'GET', '/nodes')
        assert calls == []
    finally:
        await client.aclose()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
