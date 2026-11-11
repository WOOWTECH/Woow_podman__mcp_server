"""路徑參數：'.'、'..'、空字串在送出前就拒絕，其餘編碼成單一路徑段。

quote() 不會編碼點號，httpx 組網址時又會把 '.'、'..' 段解掉：解除封鎖 '..' 原本會變成 DELETE /banned（清除全部封鎖），
刪除使用者 '..' 會刪掉整個驗證器。
"""
from urllib.parse import urlsplit

import pytest
from fastmcp import Client

from emqx_mcp_server.server import build_server

REFUSED = "must not be empty, '.' or '..'."
CASES = [
    ('emqx_unban', {'subject_type': 'clientid', 'who': '..'}, 'who'),
    ('emqx_manage_authn_users', {'operation': 'delete', 'user_id': '..'}, 'user_id'),
    ('emqx_manage_authz_rules', {'operation': 'delete', 'subject_type': 'username', 'subject': '..'}, 'subject'),
    ('emqx_node_detail', {'node': '..'}, 'node'),
    ('emqx_get_client', {'clientid': '.'}, 'clientid'),
    ('emqx_kick_client', {'clientid': ''}, 'clientid'),
    ('emqx_delete_trace', {'name': '..'}, 'name'),
    ('emqx_toggle_rule', {'rule_id': '..', 'enable': True}, 'rule_id'),
    ('emqx_delete_retained', {'topic': '..'}, 'topic'),
]


@pytest.mark.parametrize('name,args,field', CASES, ids=[c[0] + ':' + c[2] for c in CASES])
async def test_dot_segments_are_refused_before_any_request(name, args, field, broker, tmp_path):
    policy = tmp_path / 'config.json'
    policy.write_text('{"tools":{"disabled":[]}}')
    policy.chmod(0o600)
    broker.routes = {'/api/v5/authorization/sources': {'sources': [{'type': 'built_in_database'}]}}
    async with Client(build_server(policy_path=policy)) as client:
        result = await client.call_tool(name, args, raise_on_error=False)
    assert result.is_error
    assert f'{field} {REFUSED}' in result.content[0].text
    assert [call for call in broker.calls if not call[1].endswith('/authorization/sources')] == []


async def test_other_values_are_one_encoded_segment(broker, tmp_path):
    policy = tmp_path / 'config.json'
    policy.write_text('{"tools":{"disabled":[]}}')
    policy.chmod(0o600)
    async with Client(build_server(policy_path=policy)) as client:
        for name, args in [('emqx_unban', {'subject_type': 'clientid', 'who': 'owned/x'}),
                           ('emqx_get_client', {'clientid': 'a/b?c#d'}),
                           ('emqx_node_detail', {'node': 'emqx@owned'})]:
            result = await client.call_tool(name, args, raise_on_error=False)
            assert not result.is_error, (name, result)
    assert [(c[0], urlsplit(c[1]).path) for c in broker.calls] == [
        ('DELETE', '/api/v5/banned/clientid/owned%2Fx'),
        ('GET', '/api/v5/clients/a%2Fb%3Fc%23d'),
        ('GET', '/api/v5/nodes/emqx@owned'),  # 節點名稱的 '@' 照以前原樣送出
    ]


async def test_a_dot_topic_is_read_through_the_listing_scan(broker, tmp_path):
    """'..' 是合法的 MQTT topic，但路徑段裝不下（httpx 會把它解掉），所以讀取直接改走清單掃描。"""
    policy = tmp_path / 'config.json'
    policy.write_text('{"tools":{"disabled":[]}}')
    policy.chmod(0o600)
    async with Client(build_server(policy_path=policy)) as client:
        result = await client.call_tool('emqx_get_retained', {'topic': '..'}, raise_on_error=False)
    assert not result.is_error, result
    assert [(c[0], urlsplit(c[1]).path) for c in broker.calls] == [('GET', '/api/v5/mqtt/retainer/messages')]
