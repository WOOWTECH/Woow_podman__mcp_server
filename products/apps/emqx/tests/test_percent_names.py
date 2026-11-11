"""名稱含一般 '%' 的 client id、topic 可以送到 broker（'%' 編成 '%25'），規則與 HA add-on 相同。

清單照搬 HA add-on 的 tests/test_backend_policy_python.py（Woow_ha_mcp_addons #70）：允許的名稱、
再解碼才會變成路徑結構或其他伺服器會另外解讀的寫法、怪輸入。設定的 base URL 照舊不能含 '%'。
"""
from urllib.parse import quote

import pytest
from fastmcp import Client
from emqx_mcp_server.http_policy import BrokerClient, DestinationDenied, normalize_base_url, path_value
from emqx_mcp_server.server import build_server


def _encoded(name):
    """A name as the tools send it: every character outside the unreserved set percent-encoded once."""
    return quote(name, safe='')


def _layers(count):
    """'%41' encoded `count` more times: after the one decode every server performs, it still changes on `count` more."""
    value = '%41'
    for _ in range(count):
        value = quote(value, safe='')
    return value


NAMES = ['100%-off.txt', '100% done', '%', '50%25.txt', '%E6%AF%9B', 'a%zz', 'a%2eb',
         '毛利率35%.xlsx', '%%%', '1.5%', 'report%20final.pdf', '%18 KDV.xlsx', '%10 zam.xlsx',
         '%05 indirim.pdf', '%100 Pamuk Tişört.jpg', 'frame_%04d.png', 'out%1d.mp4',
         '50%descuento.pdf', '100%Beef.jpg', '%80 indirim.pdf']

REFUSED = [
    # a dot segment, '/', '\', '?', '#', NUL or line break that only a further decode produces, however deeply nested
    '/clients/' + _encoded('%2e%2e') + '/secret', '/clients/' + _encoded('%2E%2e') + '/secret', '/clients/' + _encoded('%2e'),
    '/clients/' + _encoded('a%2fb'), '/clients/' + _encoded('a%2Fb'), '/clients/' + _encoded('a%5cb'),
    '/clients/' + _encoded('a%3fb'), '/clients/' + _encoded('a%23b'), '/clients/a%3Fb' + _encoded('%3F'),
    '/clients/' + _encoded('a%00b'), '/clients/' + _encoded('a%0ab'), '/clients/' + _encoded('a%0db'),
    '/clients/' + _encoded('a%C2%85b'), '/clients/' + _encoded('a%E2%80%A8b'),
    '/clients/' + _encoded(_encoded('%2e%2e')) + '/x', '/clients/' + _encoded(_encoded('a%2fb')), '/clients/' + _layers(9),
    # a '%' on the wire that does not start a %XX escape (httpx sends it as it is)
    '/clients/100%-off.txt', '/clients/a%2', '/clients/%%32%65%%32%65/x', '/clients/.%2g./x', '/clients/%u002e%u002e/secret',
    # escapes Python's unquote leaves alone but other decoders read: %uXXXX (IIS, .NET, JavaScript's unescape) at any
    # layer, Unicode decimal or fullwidth hex digits (Java's URLDecoder, int()), Jetty's '>' (14)
    '/clients/' + _encoded('%u002e%u002e') + '/secret', '/clients/' + _encoded('%U002F'),
    '/clients/' + _encoded(_encoded('%u005c')), '/clients/' + _encoded('%u００２ｅ'),
    '/clients/' + _encoded('%２e%２e') + '/secret', '/clients/' + _encoded('%٢e'), '/clients/' + _encoded('%2ｅ'),
    '/clients/' + _encoded('a%２fb'), '/clients/' + _encoded('a%５cb'), '/clients/' + _encoded('a%３fb'),
    '/clients/' + _encoded('a%０d%０ab'), '/clients/%252%3E%252%3E/x',
    # an invalid escape nginx drops into a dot segment ('.%.', '.%2..'); overlong UTF-8 after a further decode
    '/clients/.%25./x', '/clients/.%252../x', '/clients/%25c0%25ae%25c0%25ae/secret', '/clients/%25e0%2580%25ae',
    '/clients/%25c0%25af', '/clients/%25c1%259c']

ODD = ['/api/v5/clients/%25\ud800', '/api/v5/clients/\udcff%2541', '/api/v5/clients/%25%00%0A', '/api/v5/clients/' + '%25' * 20000,
       '/api/v5/clients/' + '%25.' * 20000, '/api/v5/clients/' + '%252.' * 20000]


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


@pytest.mark.parametrize('name', NAMES)
async def test_a_name_with_a_literal_percent_reaches_the_broker_encoded_once(name, broker, http):
    await http.request('GET', '/clients/' + _encoded(name))
    assert [call[1] for call in broker.calls] == ['/api/v5/clients/' + _encoded(name)]


@pytest.mark.parametrize('path', REFUSED)
async def test_what_a_further_decode_could_misread_is_refused_before_io(path, broker, http):
    with pytest.raises(DestinationDenied):
        await http.request('GET', path)
    assert not broker.calls


@pytest.mark.parametrize('path', ODD)
def test_odd_request_paths_are_refused_or_kept_never_an_error(path):
    try:
        path_value(path, literal_percent=True)
    except DestinationDenied:
        pass


def test_eight_further_decodes_are_the_limit():
    assert path_value('/api/v5/clients/' + _layers(8), literal_percent=True) == '/api/v5/clients/' + _layers(7)
    with pytest.raises(DestinationDenied):
        path_value('/api/v5/clients/' + _layers(9), literal_percent=True)


@pytest.mark.parametrize('url', ['http://broker.example.test/a%25b', 'http://broker.example.test/%25'])
def test_the_configured_base_url_keeps_refusing_percent(url):
    with pytest.raises(DestinationDenied):
        normalize_base_url(url)


async def test_tools_serve_a_client_id_with_a_literal_percent(broker, policy):
    """emqx_get_client('50%') 送 /clients/50%25（以前拒絕）；名稱本身是 'a%2fb' 的照樣在送出前拒絕。"""
    async with Client(build_server(policy_path=policy)) as client:
        result = await client.call_tool('emqx_get_client', {'clientid': '50%'}, raise_on_error=False)
        assert not result.is_error, result
        refused = await client.call_tool('emqx_get_client', {'clientid': 'a%2fb'}, raise_on_error=False)
    assert refused.is_error and 'EMQX_DESTINATION_DENIED' in refused.content[0].text
    assert [call[1] for call in broker.calls] == ['/api/v5/clients/50%25']
