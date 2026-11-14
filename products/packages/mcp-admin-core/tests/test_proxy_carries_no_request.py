"""``proxy._carries_no_request``：哪些 POST 內容可以走通知的保留名額（獨立審查 N5）。

這是整個保留通道的守門條件：把「其實是請求」的內容判成通知，它就改用每帳號另外保留的名額，繞過一般名額的連線上限與排隊上限。
所以判斷必須保守，只有同時符合下面幾點才走保留名額，其他一律走一般名額（看不懂的內容也是）：

- 方法是 POST（GET 是串流，DELETE 是結束 session）；
- 內容解析得出來，而且是一個物件，或「非空」的物件陣列；
- 其中沒有任何請求（有 id、而且有字串 method）；
- 每一則都是通知（有字串 method），或用戶端對伺服器請求的回應（有 id、有 result／error、沒有 method）。

這個檔案把「必須走一般名額」的負向案例做成參數化測試（審查者的變異 px03：不限 POST、px04：空 batch 也算、px05：method 不是字串也算，
以前都沒有測試會紅），並釘住少數「走保留名額」的案例，讓邊界的決定有出處。內容一律是合成資料。
"""
from __future__ import annotations

import json

import pytest

from mcp_admin_core import proxy, tool_filter


def _json(value) -> bytes:
    return json.dumps(value).encode()


_CALL = '"method":"tools/call","params":{"name":"x","arguments":{}}'
_NOTIFICATION = {"jsonrpc": "2.0", "method": "notifications/initialized"}
_REQUEST = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "x", "arguments": {}}}
_RESPONSE = {"jsonrpc": "2.0", "id": "server-req-1", "result": {"roots": []}}


def _classify(method: str, body: bytes) -> bool:
    """與閘道相同：只有 POST 才解析內容（GET、DELETE 傳 None），再問 ``_carries_no_request``。"""
    rpc = tool_filter.parse_jsonrpc(body) if method == "POST" else None
    return proxy._carries_no_request(method, rpc)


# 必須走一般名額（回傳 False）。每一筆都是「如果被判成通知，就能用保留名額跑一個請求」的內容，或是看不懂的內容。
MUST_USE_THE_ORDINARY_LANE = {
    # --- 有請求 id：0、空字串、false 都是 id（只有 null 與沒有 id 不是）
    "request": _json(_REQUEST),
    "request id 0": f'{{"jsonrpc":"2.0","id":0,{_CALL}}}'.encode(),
    "request id empty string": f'{{"jsonrpc":"2.0","id":"",{_CALL}}}'.encode(),
    "request id false": f'{{"jsonrpc":"2.0","id":false,{_CALL}}}'.encode(),
    "request id float": f'{{"jsonrpc":"2.0","id":1.5,{_CALL}}}'.encode(),
    "request id NaN": f'{{"jsonrpc":"2.0","id":NaN,{_CALL}}}'.encode(),
    "request id object": f'{{"jsonrpc":"2.0","id":{{"a":1}},{_CALL}}}'.encode(),
    "request id list": f'{{"jsonrpc":"2.0","id":[1],{_CALL}}}'.encode(),
    "request that also carries a result": f'{{"jsonrpc":"2.0","id":1,"result":{{}},{_CALL}}}'.encode(),
    # --- batch：只要有一則是請求就是一般請求（混合、通知加請求、回應加請求）
    "batch of a notification and a request": _json([_NOTIFICATION, _REQUEST]),
    "batch of a response and a request": _json([_RESPONSE, {"jsonrpc": "2.0", "id": 2, "method": "ping"}]),
    "batch of a request only": _json([_REQUEST]),
    # --- 重複鍵：Python（與 MCP SDK）取最後一個
    "duplicate id, the last one is a real id": f'{{"jsonrpc":"2.0","id":null,"id":8,{_CALL}}}'.encode(),
    "duplicate method, a request id with a notification method last":
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call","method":"notifications/initialized"}',
    "duplicate method, a request id with a notification method first":
        b'{"jsonrpc":"2.0","id":1,"method":"notifications/initialized","method":"tools/call"}',
    # --- 用編碼或跳脫把請求藏起來：解析得出來就是請求
    "request with a UTF-8 BOM": b"\xef\xbb\xbf" + _json(_REQUEST),
    "request in UTF-16": _json(_REQUEST).decode().encode("utf-16"),
    "request in UTF-16 without a BOM": _json(_REQUEST).decode().encode("utf-16-le"),
    "request in UTF-32": _json(_REQUEST).decode().encode("utf-32"),
    "request with an escaped key name for id": b'{"jsonrpc":"2.0","\\u0069d":1,"method":"tools/call"}',
    "request with an escaped key name for method": b'{"jsonrpc":"2.0","id":1,"\\u006dethod":"tools/call"}',
    # --- method 缺失或不是字串
    "method is a number": b'{"jsonrpc":"2.0","method":5}',
    "method is null": b'{"jsonrpc":"2.0","method":null}',
    "method is a list": b'{"jsonrpc":"2.0","method":["notifications/initialized"]}',
    "method is a number with an id": b'{"jsonrpc":"2.0","id":1,"method":5}',
    "no method, no id, no result": b'{"jsonrpc":"2.0"}',
    "an id alone": b'{"jsonrpc":"2.0","id":1}',
    "empty object": b"{}",
    "response without an id": b'{"jsonrpc":"2.0","result":{}}',
    # --- batch 看不懂
    "empty batch": b"[]",
    "batch of numbers": b"[1,2]",
    "batch with a notification and a number": _json([_NOTIFICATION, 5]),
    "nested batch": _json([[_REQUEST]]),
    "batch with an unreadable member": _json([_NOTIFICATION, {}]),
    # --- 解析不出來、或不是物件／陣列
    "empty body": b"",
    "not json": b"hello",
    "truncated json": b'{"jsonrpc":"2.0","method":"notifications/init',
    "trailing garbage": _json(_NOTIFICATION) + b" garbage",
    "json string": b'"notifications/initialized"',
    "json number": b"1",
    "json null": b"null",
    "json true": b"true",
}

# 走保留名額（回傳 True）：邊界的決定。
USES_THE_RESERVED_LANE = {
    "notification": _json(_NOTIFICATION),
    "notification with params": b'{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":1000}}',
    "batch of notifications": _json([_NOTIFICATION, {"jsonrpc": "2.0", "method": "notifications/cancelled"}]),
    "response with a result": _json(_RESPONSE),
    "response with an error": b'{"jsonrpc":"2.0","id":1,"error":{"code":-32601,"message":"x"}}',
    "batch of a notification and a response": _json([_NOTIFICATION, _RESPONSE]),
    # id 是 null（或沒有 id）的 tools/call 不是請求：JSON-RPC 通知沒有 id，MCP Python SDK 2.3.0 的子行程對這類內容回 202、
    # 不執行工具（2026-11-14 審查 N5 以真的子行程實測；TS SDK 1.31.0 的 schema 解析不接受 id:null）。k8s（Go）與 vk（Rust）的
    # 子行程沒有驗證過。最後一個 id 才算：{"id":7,"id":null} 與 SDK 一樣取最後一個。
    "tools/call with id null": f'{{"jsonrpc":"2.0","id":null,{_CALL}}}'.encode(),
    "tools/call without an id": f'{{"jsonrpc":"2.0",{_CALL}}}'.encode(),
    "duplicate id, the last one is null": f'{{"jsonrpc":"2.0","id":7,"id":null,{_CALL}}}'.encode(),
}


@pytest.mark.parametrize("body", MUST_USE_THE_ORDINARY_LANE.values(), ids=MUST_USE_THE_ORDINARY_LANE.keys())
def test_anything_that_could_be_a_request_or_cannot_be_read_uses_the_ordinary_lane(body):
    assert _classify("POST", body) is False


@pytest.mark.parametrize("body", USES_THE_RESERVED_LANE.values(), ids=USES_THE_RESERVED_LANE.keys())
def test_only_notifications_and_responses_use_the_reserved_lane(body):
    assert _classify("POST", body) is True


@pytest.mark.parametrize("method", ["GET", "DELETE", "PUT", "OPTIONS", "get"])
def test_only_post_can_use_the_reserved_lane(method):
    """GET 是串流（佔名額的時間以小時計）、DELETE 結束 session：不是通知，就算內容看起來是通知也不能用保留名額。"""
    assert proxy._carries_no_request("POST", _NOTIFICATION) is True  # 對照：同樣的內容用 POST 就走保留名額
    assert proxy._carries_no_request(method, _NOTIFICATION) is False
    assert proxy._carries_no_request(method, [_NOTIFICATION]) is False
    assert proxy._carries_no_request(method, None) is False
    assert _classify(method, _json(_NOTIFICATION)) is False


def test_nothing_parsed_means_the_ordinary_lane():
    assert proxy._carries_no_request("POST", None) is False
    assert proxy._carries_no_request("POST", []) is False
    assert proxy._carries_no_request("POST", {}) is False
