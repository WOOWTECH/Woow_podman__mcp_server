"""F-01：emqx_list_subscriptions 的 match_topic 說明要如實描述 broker 的比對方向。

2026-11-14 EMQX 5.8.9 實測（mp-emqx-20261114 報告第 5 節 F-01、收據 A4-*-tool-matrix.json 的 readback）：
`GET /subscriptions?match_topic=X` 把 X 當成「過濾器」，去比對「已儲存的訂閱主題」，
所以 holder 訂了 `sub/a/#` 時，match_topic='sub/a/deep/x'（具體主題）查不到、match_topic='sub/a/#' 或 'sub/a/+' 查得到。
直接打 broker 結果相同，問題在工具說明寫成相反的方向（「找出過濾器會匹配這個具體主題的訂閱」），LLM 會誤答「沒人訂閱」。

本檔鎖兩件事：
1. 工具與參數的說明不再出現相反的措辭，並說清楚比對方向與替代做法；
2. 參數仍原樣轉成 GET /subscriptions 的查詢參數，回傳資料不在閘道端另外過濾（不改行為、不改資料結構）。
"""
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from fastmcp import Client

from emqx_mcp_server.server import build_server

# 修正前的說明裡「方向相反」的原文（空白已正規化）。整句鎖住，不鎖單字，免得新的正確說明被誤判。
OLD_CLAIMS = (
    'whose filter would match this concrete topic',
    "use this to answer 'who receives messages published here?'",
    'given a concrete topic, it tells you exactly which subscribers would receive a message on it',
)


def squash(text):
    return ' '.join((text or '').split())


@pytest.fixture
def policy(tmp_path):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'tools': {'disabled': [], 'disabled_operations': {}}}))
    path.chmod(0o600)
    return path


async def subscriptions_tool(policy):
    async with Client(build_server(policy_path=policy)) as client:
        return next(t for t in await client.list_tools() if t.name == 'emqx_list_subscriptions')


async def test_descriptions_no_longer_claim_the_opposite_direction(policy):
    tool = await subscriptions_tool(policy)
    param = squash(tool.input_schema['properties']['match_topic']['description'])
    whole = squash(tool.description)
    for claim in OLD_CLAIMS:
        assert claim not in param.lower() and claim not in whole.lower(), claim


async def test_match_topic_parameter_states_what_the_broker_really_does(policy):
    """比對方向（你給的字串當過濾器、去比對已儲存的訂閱主題）、不是「誰會收到」的查詢、具體主題找不到萬用字元訂閱。"""
    tool = await subscriptions_tool(policy)
    param = squash(tool.input_schema['properties']['match_topic']['description']).lower()
    assert 'filter' in param and 'stored' in param
    assert 'not a delivery lookup' in param
    assert 'concrete topic' in param and 'wildcard subscription' in param
    # 具體例子：儲存的是 'sensors/#'，用具體主題 'sensors/a/temp' 查不到。
    assert 'sensors/#' in param and 'sensors/a/temp' in param


async def test_tool_description_names_the_way_to_answer_who_receives(policy):
    """說明裡有可行的替代做法：列出訂閱（可用 clientid 縮小、翻頁），自己用 MQTT 萬用字元規則比對每一列的 topic。"""
    tool = await subscriptions_tool(policy)
    whole = squash(tool.description).lower()
    assert 'who receives messages published to topic' in whole
    assert 'list the subscriptions' in whole and 'compare' in whole
    assert "'+' = exactly one level" in whole and "'#' = all remaining levels" in whole
    assert 'never proves that nobody is subscribed' in whole


@pytest.mark.parametrize('value', [
    'sub/a/#', 'sub/a/+', 'sub/a/deep/x', 'sub/+/x', '$SYS/brokers/+/clients/#', '中文/主題', 'a b/c%d', 'x&y=z#w',
])
async def test_match_topic_is_forwarded_unchanged_as_a_query_parameter(value, policy, broker):
    async with Client(build_server(policy_path=policy)) as client:
        result = await client.call_tool('emqx_list_subscriptions', {'match_topic': value}, raise_on_error=False)
    assert not result.is_error, result
    assert len(broker.calls) == 1
    split = urlsplit(broker.calls[0][1])
    assert (broker.calls[0][0], split.path) == ('GET', '/api/v5/subscriptions')
    assert parse_qs(split.query, keep_blank_values=True)['match_topic'] == [value]


async def test_other_filters_keep_their_query_names(policy, broker):
    args = {'clientid': 'cli-a', 'topic': 'sub/a/#', 'match_topic': 'sub/a/+', 'qos': 1, 'page': 2, 'limit': 5}
    async with Client(build_server(policy_path=policy)) as client:
        result = await client.call_tool('emqx_list_subscriptions', args, raise_on_error=False)
    assert not result.is_error, result
    query = parse_qs(urlsplit(broker.calls[0][1]).query)
    assert query == {'clientid': ['cli-a'], 'topic': ['sub/a/#'], 'match_topic': ['sub/a/+'],
                     'qos': ['1'], 'page': ['2'], 'limit': ['5']}


async def test_rows_are_returned_as_the_broker_sent_them_without_client_side_matching(policy, broker):
    """閘道不自己重新比對：broker 回什麼就是什麼（即使用具體主題查、broker 回了別的列，也不刪、不補）。"""
    row = {'clientid': 'cli-a', 'topic': 'sub/a/#', 'qos': 1, 'node': 'emqx@127.0.0.1'}
    broker.routes = {'/api/v5/subscriptions': {'data': [row], 'meta': {'count': 1}}}
    async with Client(build_server(policy_path=policy)) as client:
        found = await client.call_tool('emqx_list_subscriptions', {'match_topic': 'sub/a/deep/x'}, raise_on_error=False)
        broker.routes = {'/api/v5/subscriptions': {'data': [], 'meta': {'count': 0}}}
        empty = await client.call_tool('emqx_list_subscriptions', {'match_topic': 'sub/a/deep/x'}, raise_on_error=False)
    assert found.structured_content == {'count': 1, 'total': 1, 'subscriptions': [row]}
    assert empty.structured_content == {'count': 0, 'total': 0, 'subscriptions': []}
