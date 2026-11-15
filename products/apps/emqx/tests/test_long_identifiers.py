"""F-03：超長（≥65,536 字元）或無法編碼的識別字要變成固定的錯誤碼，不能讓例外一路冒到 FastMCP。

2026-11-14 實測：`emqx_get_client`、`emqx_get_retained` 傳 400,000 個字元的 clientid／topic 時，httpx 組網址拋
`httpx.InvalidURL: URL too long`；它不是 `httpx.HTTPError`，`emqx_request` 沒接住，用戶端只看到 FastMCP 的通用遮罩錯誤
「Error calling tool 'emqx_get_client'」，子行程的 stdout（容器 log、管理頁 Logs）留下含函式庫路徑的 rich traceback。
同一個缺口還有：識別字含單獨的 surrogate（'\\ud800'）時 `quote()`／httpx 拋 `UnicodeEncodeError`（路徑、查詢參數、JSON 內容都會）。

修正後：這兩種具體例外一律變成既有的固定公開錯誤碼 `EMQX_DESTINATION_DENIED`（與其他「送出前就拒絕」同一個），
錯誤文字不含引數內容，也不再有 traceback；正常長度（編碼後仍小於網址元件上限）的識別字行為不變。
"""
import json
import logging
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from urllib.parse import quote, urlsplit

import pytest
from fastmcp import Client

from emqx_mcp_server.errors import EmqxApiError, emqx_request
from emqx_mcp_server.http_policy import BrokerClient, DestinationDenied
from emqx_mcp_server.server import build_server
from emqx_mcp_server.tools._common import path_segment

CODE = 'EMQX_DESTINATION_DENIED'
LONG = 'a' * 400_000

# （工具, 引數, 欄位）：會進網址路徑的參數各一個，再加兩個進查詢字串的參數（同一個 InvalidURL 來源）。
LONG_CASES = [
    ('emqx_get_client', {'clientid': LONG}, 'clientid'),
    ('emqx_client_subscriptions', {'clientid': LONG}, 'clientid'),
    ('emqx_kick_client', {'clientid': LONG}, 'clientid'),
    ('emqx_client_subscribe', {'clientid': LONG, 'topic': 'owned/topic'}, 'clientid'),
    ('emqx_client_unsubscribe', {'clientid': LONG, 'topic': 'owned/topic'}, 'clientid'),
    ('emqx_get_retained', {'topic': LONG}, 'topic'),
    ('emqx_delete_retained', {'topic': LONG}, 'topic'),
    ('emqx_node_detail', {'node': LONG}, 'node'),
    ('emqx_get_trace_log', {'name': LONG}, 'name'),
    ('emqx_delete_trace', {'name': LONG}, 'name'),
    ('emqx_get_rule_metrics', {'rule_id': LONG}, 'rule_id'),
    ('emqx_toggle_rule', {'rule_id': LONG, 'enable': True}, 'rule_id'),
    ('emqx_unban', {'subject_type': 'clientid', 'who': LONG}, 'who'),
    ('emqx_manage_authn_users', {'operation': 'delete', 'user_id': LONG}, 'user_id'),
    ('emqx_manage_authn_users', {'operation': 'read', 'authenticator_id': LONG}, 'authenticator_id'),
    ('emqx_manage_authz_rules', {'operation': 'delete', 'subject_type': 'username', 'subject': LONG}, 'subject'),
    ('emqx_list_clients', {'username': LONG}, 'username(query)'),
    ('emqx_list_subscriptions', {'match_topic': LONG}, 'match_topic(query)'),
]
IDS = [f'{tool}:{field}' for tool, _, field in LONG_CASES]


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


class Records(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def with_traceback(self):
        return [r for r in self.records if r.exc_info or r.exc_text or r.stack_info
                or 'Traceback' in self.format(r)]


@contextmanager
def captured_logs():
    """容器 stdout 的內容就是這些 logger 輸出的：根 logger 與 fastmcp（它有自己的 handler，不一定往上傳）。"""
    handler, seen = Records(), []
    for logger in (logging.getLogger(), logging.getLogger('fastmcp')):
        logger.addHandler(handler)
        seen.append(logger)
    try:
        yield handler
    finally:
        for logger in seen:
            logger.removeHandler(handler)


def real_calls(broker):
    return [c for c in broker.calls if not c[1].endswith('/authorization/sources')]


# ------------------------------------------------------------------ 透過 MCP：固定錯誤碼、不含引數、沒有 traceback


@pytest.mark.parametrize('tool,args,field', LONG_CASES, ids=IDS)
async def test_an_overlong_identifier_gets_the_fixed_code_and_never_leaves_the_process(tool, args, field, policy, broker):
    broker.routes = {'/api/v5/authorization/sources': {'sources': [{'type': 'built_in_database'}]}}
    with captured_logs() as logs:
        async with Client(build_server(policy_path=policy)) as client:
            result = await client.call_tool(tool, args, raise_on_error=False)
    text = result.content[0].text
    assert result.is_error
    assert CODE in text, text
    assert 'aaaaaaaaaaaaaaaa' not in text and len(text) < 300  # 不回顯引數，也不是通用遮罩錯誤
    assert 'URL too long' not in text and 'httpx' not in text.lower()
    assert real_calls(broker) == []  # 沒有任何請求送到 broker
    assert logs.with_traceback() == [], [logs.format(r) for r in logs.with_traceback()]


@pytest.mark.parametrize('value', ['%' * 400_000, '毛' * 100_000, '/' * 400_000, '\n' * 70_000],
                         ids=['percent', 'cjk', 'slash', 'newline'])
async def test_the_fixed_code_does_not_depend_on_what_the_long_value_is_made_of(value, policy, broker):
    """'%' 編碼後變三倍長、CJK 字元約九倍長、控制字元也一樣：都以固定錯誤碼收場，沒有請求送出。"""
    async with Client(build_server(policy_path=policy)) as client:
        result = await client.call_tool('emqx_get_client', {'clientid': value}, raise_on_error=False)
    assert result.is_error and CODE in result.content[0].text
    assert broker.calls == []


# ------------------------------------------------------------------ 子行程的 stdout：真的子行程、真的 log 輸出


async def test_the_real_child_prints_no_traceback_to_its_stdout_for_an_overlong_identifier(tmp_path, broker):
    policy = tmp_path / 'config.json'
    policy.write_text('{"tools":{"disabled":[]}}')
    policy.chmod(0o600)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = {**os.environ, 'MCP_ADMIN_CONFIG': str(policy), 'WEBHOOK_SECURITY_MODE': 'moderate',
           'EMQX_MCP_BASE_URL': broker.url, 'EMQX_MCP_API_KEY': 'synthetic-api-key',
           'EMQX_MCP_API_SECRET': 'synthetic-api-secret', 'PYTHONDONTWRITEBYTECODE': '1'}
    proc = None
    with (tmp_path / 'child.log').open('w+') as log:
        try:
            proc = subprocess.Popen([sys.executable, '-m', 'emqx_mcp_admin.launcher', '--port', str(port)],
                                    env=env, stdout=log, stderr=log)
            for _ in range(200):
                assert proc.poll() is None, 'child exited early'
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=.1):
                        break
                except OSError:
                    time.sleep(.1)
            else:
                raise AssertionError('child did not start')
            async with Client(f'http://127.0.0.1:{port}/mcp') as client:
                for tool, args in [('emqx_get_client', {'clientid': LONG}), ('emqx_get_retained', {'topic': LONG})]:
                    result = await client.call_tool(tool, args, raise_on_error=False)
                    assert result.is_error and CODE in result.content[0].text, result
                assert not (await client.call_tool('emqx_get_client', {'clientid': 'owned'})).is_error
        finally:
            if proc is not None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        log.seek(0)
        output = log.read()
    assert 'Traceback' not in output, output[-2000:]
    assert 'URL too long' not in output and 'site-packages' not in output
    assert 'aaaaaaaaaaaaaaaa' not in output
    assert [c[1] for c in broker.calls] == ['/api/v5/clients/owned']  # 只有那個正常的請求到了 broker


# ------------------------------------------------------------------ 單獨的 surrogate：無法編碼，同樣是固定錯誤碼


def test_a_path_value_that_cannot_be_encoded_is_refused_with_the_fixed_code():
    with pytest.raises(EmqxApiError) as refused:
        path_segment('a\ud800b', 'clientid')
    assert str(refused.value) == CODE
    assert 'ud800' not in str(refused.value).lower() and 'clientid' not in str(refused.value)


@pytest.mark.parametrize('kwargs', [{'params': {'match_topic': 'a\ud800'}}, {'json': {'topic': 'a\ud800', 'payload': 'x'}},
                                    {'params': {'a': 'x' * 70_000}}], ids=['query-surrogate', 'json-surrogate', 'query-too-long'])
async def test_a_request_that_cannot_be_built_is_denied_before_any_io(kwargs, broker, http):
    method = 'POST' if 'json' in kwargs else 'GET'
    path = '/publish' if 'json' in kwargs else '/subscriptions'
    with pytest.raises(DestinationDenied):
        await http.request(method, path, **kwargs)
    with pytest.raises(EmqxApiError) as refused:
        await emqx_request(http, method, path, **kwargs)
    assert str(refused.value) == CODE
    assert not broker.calls


async def test_a_path_httpx_would_refuse_is_denied_before_the_path_analysis_does_any_work(broker, http, monkeypatch):
    """'%' 填滿的 400,000 字元，編碼後 120 萬字元：先擋掉，不讓 path_value 對它做十幾輪線性比對。"""
    import emqx_mcp_server.http_policy as module

    def analysed(*args, **kwargs):
        raise AssertionError('path_value ran on a path that cannot be sent anyway')
    monkeypatch.setattr(module, 'path_value', analysed)
    with pytest.raises(DestinationDenied):
        await http.request('GET', '/clients/' + path_segment('%' * 400_000, 'clientid'))
    assert not broker.calls


async def test_a_lone_surrogate_in_a_tool_argument_gets_the_fixed_code(policy, broker):
    """用戶端經 JSON 送來單獨的 surrogate：路徑欄位、查詢欄位、內容欄位都是固定錯誤碼，沒有請求送出。"""
    async with Client(build_server(policy_path=policy)) as client:
        for tool, args in [('emqx_get_client', {'clientid': 'a\ud800b'}),
                           ('emqx_list_subscriptions', {'match_topic': 'a\ud800'}),
                           ('emqx_client_subscribe', {'clientid': 'owned', 'topic': 'a\ud800'})]:
            result = await client.call_tool(tool, args, raise_on_error=False)
            assert result.is_error and CODE in result.content[0].text, (tool, result)
    assert broker.calls == []


# ------------------------------------------------------------------ 正常長度不變


@pytest.mark.parametrize('value', ['a' * 1000, 'a' * 64_000, '毛' * 5000, 'a/b?c#d' * 3000])
async def test_a_long_but_valid_identifier_still_reaches_the_broker_unchanged(value, policy, broker):
    async with Client(build_server(policy_path=policy)) as client:
        result = await client.call_tool('emqx_get_client', {'clientid': value}, raise_on_error=False)
        match = await client.call_tool('emqx_list_subscriptions', {'match_topic': value[:60_000]}, raise_on_error=False)
    assert not result.is_error, result.content[0].text[:200]
    assert not match.is_error, match.content[0].text[:200]
    assert urlsplit(broker.calls[0][1]).path == '/api/v5/clients/' + quote(value, safe='')


async def test_the_limit_is_on_the_encoded_length_so_a_cjk_value_stops_earlier(policy, broker):
    """'毛' 每個字編成 9 個字元：5,000 個（45,000）可以，8,000 個（72,000）超過 65,535 就是固定錯誤碼。"""
    async with Client(build_server(policy_path=policy)) as client:
        fits = await client.call_tool('emqx_get_client', {'clientid': '毛' * 5000}, raise_on_error=False)
        over = await client.call_tool('emqx_get_client', {'clientid': '毛' * 8000}, raise_on_error=False)
    assert not fits.is_error
    assert over.is_error and CODE in over.content[0].text
    assert len(broker.calls) == 1
