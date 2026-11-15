"""F-02：emqx_prometheus_stats 說明寫「Raw Prometheus exposition text」，所以只有這支工具的這個請求送 Accept: text/plain。

2026-11-14 EMQX 5.8.9 實測：BrokerClient 固定送 `Accept: application/json`，`GET /prometheus/stats` 就回 JSON 統計，
`max_bytes` 把 JSON 截在半途；直接用 `Accept: text/plain` 打 broker 才回 `# TYPE emqx_connections_count gauge…`。

這裡用本機假 HTTP 伺服器（tests/conftest.py 的 `broker`，依 Accept 協商文字回應）鎖住：
- 這支工具的請求帶 `Accept: text/plain`，Prometheus 文字原樣（含截斷）回傳；
- 其他所有工具的請求仍是 `Accept: application/json`；
- 受保護路徑的規則不被繞過：回應上限 4 MiB、拒絕非 identity 壓縮、空內容、其他工具的空／畸形 200 JSON 照樣被拒絕；
- 傳輸層只接受兩個固定的 Accept 值，呼叫端塞不進任意標頭。
"""
import json
from urllib.parse import urlsplit

import pytest
from fastmcp import Client

from emqx_mcp_server.errors import EmqxApiError, emqx_request
from emqx_mcp_server.http_policy import MAX_RESPONSE_BYTES, BrokerClient, DestinationDenied
from emqx_mcp_server.server import build_server
from test_39_calls import CASES

PROM = '/api/v5/prometheus/stats'
HEAD = '# HELP emqx_connections_count Number of connections\n# TYPE emqx_connections_count gauge\nemqx_connections_count 7\n'
EXPOSITION = HEAD + ''.join(f'emqx_messages_received{{node="emqx@127.0.0.1",shard="{i}"}} {i * 7}\n' for i in range(3000))
TEXT = 'text/plain; version=0.0.4; charset=utf-8'


@pytest.fixture
def policy(tmp_path):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'tools': {'disabled': [], 'disabled_operations': {}}}))
    path.chmod(0o600)
    return path


@pytest.fixture
async def http(broker):
    client = BrokerClient(broker.url, 'synthetic-api-key', 'synthetic-api-secret', .1)
    yield client
    await client.aclose()


async def call(policy, name, args=None):
    async with Client(build_server(policy_path=policy)) as client:
        return await client.call_tool(name, args or {}, raise_on_error=False)


def accept_of(request):
    return request['headers'].get('accept'), request['headers'].get('accept-encoding')


# ------------------------------------------------------------------ 這支工具：文字


async def test_the_prometheus_tool_asks_for_text_and_returns_the_exposition_text(policy, broker):
    broker.text_routes = {PROM: EXPOSITION}
    result = await call(policy, 'emqx_prometheus_stats', {'max_bytes': 262144})
    assert not result.is_error, result
    assert [accept_of(r) for r in broker.requests] == [('text/plain', 'identity')]
    assert (broker.requests[0]['method'], urlsplit(broker.requests[0]['path']).path) == ('GET', PROM)
    assert result.structured_content == {'truncated': False, 'text': EXPOSITION}


async def test_the_exposition_text_is_cut_at_max_bytes_not_in_the_middle_of_json(policy, broker):
    broker.text_routes = {PROM: EXPOSITION}
    assert len(EXPOSITION) > 65536
    small = await call(policy, 'emqx_prometheus_stats', {'max_bytes': 1024})
    default = await call(policy, 'emqx_prometheus_stats')
    assert small.structured_content == {'truncated': True, 'text': EXPOSITION[:1024]}
    assert small.structured_content['text'].startswith('# HELP emqx_connections_count')
    assert default.structured_content == {'truncated': True, 'text': EXPOSITION[:65536]}


async def test_a_broker_that_still_answers_json_is_passed_through_as_before(policy, broker):
    """EMQX 沒有為 text/plain 準備文字時（回 JSON），行為與修正前相同：原樣回傳文字，不報錯。"""
    result = await call(policy, 'emqx_prometheus_stats')
    assert not result.is_error, result
    assert json.loads(result.structured_content['text']) == [{'connections.count': 7}]


# ------------------------------------------------------------------ 其他工具：維持 JSON


async def test_every_other_tool_keeps_asking_for_json(policy, broker):
    broker.routes = {
        '/api/v5/authorization/sources': {'sources': [{'type': 'built_in_database'}]},
        '/api/v5/publish': {'id': 'owned-id', 'reason_code': 0},
        '/api/v5/publish/bulk': [{'id': 'owned-id', 'reason_code': 0}],
        '/api/v5/mqtt/retainer/message/owned%2Ftopic': {'payload': 'b3duZWQ=', 'qos': 0},
        '/api/v5/trace/owned/log_detail': [],
    }
    broker.text_routes = {PROM: EXPOSITION}
    async with Client(build_server(policy_path=policy)) as client:
        for short, args, _, _ in CASES:
            if short == 'prometheus_stats':
                continue
            result = await client.call_tool('emqx_' + short, args, raise_on_error=False)
            assert not result.is_error, (short, result)
    assert len(broker.requests) >= len(CASES) - 1
    assert {accept_of(r) for r in broker.requests} == {('application/json', 'identity')}


# ------------------------------------------------------------------ 受保護路徑的規則沒有被繞過


@pytest.mark.parametrize('body', [b'', b'\n', b' \r\n \t'])
async def test_empty_exposition_text_is_refused_not_reported_as_success(body, policy, broker):
    broker.body, broker.content_type = body, TEXT
    result = await call(policy, 'emqx_prometheus_stats')
    assert result.is_error and 'EMQX_INVALID_RESPONSE' in result.content[0].text
    assert len(broker.calls) == 1


async def test_a_204_for_the_text_request_is_not_an_empty_success_either(policy, broker):
    broker.status, broker.body, broker.content_type = 204, b'', TEXT
    result = await call(policy, 'emqx_prometheus_stats')
    assert result.is_error and 'EMQX_INVALID_RESPONSE' in result.content[0].text


async def test_text_over_4_mib_is_refused(policy, broker):
    broker.body, broker.content_type = b'x' * (MAX_RESPONSE_BYTES + 1), TEXT
    result = await call(policy, 'emqx_prometheus_stats')
    assert result.is_error and 'EMQX_RESPONSE_TOO_LARGE' in result.content[0].text
    assert accept_of(broker.requests[0])[0] == 'text/plain'


async def test_exactly_4_mib_of_text_is_still_served_and_truncated(policy, broker):
    broker.body, broker.content_type = b'x' * MAX_RESPONSE_BYTES, TEXT
    result = await call(policy, 'emqx_prometheus_stats', {'max_bytes': 2048})
    assert not result.is_error, result
    assert result.structured_content == {'truncated': True, 'text': 'x' * 2048}


async def test_compressed_text_is_refused(policy, broker):
    broker.body, broker.content_type, broker.headers = b'compressed', TEXT, {'Content-Encoding': 'gzip'}
    result = await call(policy, 'emqx_prometheus_stats')
    assert result.is_error and 'EMQX_UNAVAILABLE' in result.content[0].text
    assert 'compressed' not in result.content[0].text


@pytest.mark.parametrize('status,fragment', [(401, '401'), (404, '404'), (500, 'EMQX_API_ERROR status=500')])
async def test_error_statuses_for_the_text_request_are_mapped_like_any_other_tool(status, fragment, policy, broker):
    broker.status = status
    broker.response = {'message': 'synthetic-api-secret synthetic-api-key'}
    result = await call(policy, 'emqx_prometheus_stats')
    assert result.is_error and fragment in result.content[0].text
    assert 'synthetic-api-' not in result.content[0].text


async def test_other_tools_still_refuse_empty_and_malformed_200_json(policy, broker):
    for body in (b'', b'<html>login</html>', b'null', b'42'):
        broker.body = body
        result = await call(policy, 'emqx_broker_stats')
        assert result.is_error and 'EMQX_INVALID_RESPONSE' in result.content[0].text, body


# ------------------------------------------------------------------ 傳輸層：只有兩個固定的 Accept 值


async def test_transport_serves_text_only_when_asked_with_the_fixed_value(broker, http):
    broker.text_routes = {PROM: EXPOSITION}
    response = await emqx_request(http, 'GET', '/prometheus/stats', accept='text/plain')
    assert response.status_code == 200 and response.text == EXPOSITION
    json_response = await emqx_request(http, 'GET', '/prometheus/stats')
    assert json.loads(json_response.text) == [{'connections.count': 7}]
    explicit = await emqx_request(http, 'GET', '/prometheus/stats', accept='application/json')
    assert json.loads(explicit.text) == [{'connections.count': 7}]
    assert [accept_of(r)[0] for r in broker.requests] == ['text/plain', 'application/json', 'application/json']
    assert {accept_of(r)[1] for r in broker.requests} == {'identity'}


@pytest.mark.parametrize('accept', ['text/html', '*/*', 'text/plain, application/json', 'application/json;q=0.1',
                                    'text/plain\r\nX-Injected: 1', '', 'TEXT/PLAIN', None, 1, ['text/plain'], {'a': 1}])
async def test_any_other_accept_value_is_refused_before_io(accept, broker, http):
    with pytest.raises(DestinationDenied):
        await http.request('GET', '/prometheus/stats', accept=accept)
    assert not broker.calls


@pytest.mark.parametrize('extra', [{'headers': {'Accept': 'text/plain'}}, {'headers': {'X-Anything': '1'}},
                                   {'cookies': {'a': 'b'}}, {'auth': ('a', 'b')}, {'follow_redirects': True}])
async def test_callers_still_cannot_pass_headers_or_other_request_options(extra, broker, http):
    with pytest.raises(DestinationDenied):
        await http.request('GET', '/nodes', **extra)
    assert not broker.calls
    with pytest.raises(EmqxApiError, match='EMQX_DESTINATION_DENIED'):
        await emqx_request(http, 'GET', '/nodes', **extra)
