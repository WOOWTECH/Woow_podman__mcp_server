"""litellm：沒有自己宣告 McpRequestLimits 的產品也要有結構守門（深度與元素個數；閘道請求內容位元組預算第二輪 P1-A）。

位元組預算只算 body 的位元組；16 MiB 的 ``[[],[],…]`` 解析後多出數百 MiB。這個產品的真實 app（``litellm_mcp_admin.main.app``）必須帶著
create_app 的預設結構上限，proxy 在 json.loads 之前拒收過深或元素過多的請求，合法的大呼叫（一個 13.3 MiB 的字串、數千筆紀錄）照常放行。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from mcp_admin_core import proxy
from mcp_admin_core.request_limits import DEFAULT_MAX_DEPTH, DEFAULT_MAX_TOTAL_ELEMENTS, RequestLimitError

from litellm_mcp_admin.main import app

POST = SimpleNamespace(method="POST")


def _call(arguments: bytes) -> bytes:
    return b'{"jsonrpc":"2.0","id":9,"method":"tools/call","params":{"name":"t","arguments":' + arguments + b"}}"


def test_app_has_the_default_structure_limits():
    assert app.state.mcp_request_limits is None  # 這個產品沒有自己的 McpRequestLimits
    guard = app.state.mcp_structure_limits
    assert (guard.max_depth, guard.max_total_elements) == (DEFAULT_MAX_DEPTH, DEFAULT_MAX_TOTAL_ELEMENTS)


def test_too_deep_request_is_refused():
    guard = app.state.mcp_structure_limits
    levels = DEFAULT_MAX_DEPTH + 1
    with pytest.raises(RequestLimitError) as caught:
        proxy._parse_unlimited(POST, _call(b"[" * levels + b"]" * levels), guard, legacy_sse=False)
    assert caught.value.payload["error"]["code"] == -32602 and caught.value.payload["id"] == 9


def test_too_many_elements_are_refused():
    guard = app.state.mcp_structure_limits
    body = _call(b"[" + b"[]," * DEFAULT_MAX_TOTAL_ELEMENTS + b"[]]")
    with pytest.raises(RequestLimitError) as caught:
        proxy._parse_unlimited(POST, body, guard, legacy_sse=False)
    assert caught.value.payload["id"] == 9 and caught.value.status == 200


def test_legitimate_large_calls_are_parsed():
    guard = app.state.mcp_structure_limits
    big_string = json.dumps({"content": "A" * (13 * 1024 * 1024)}).encode()
    records = json.dumps({"args": [[{f"f{i}": i for i in range(30)}] * 3000]}).encode()
    for arguments in (big_string, records):
        assert proxy._parse_unlimited(POST, _call(arguments), guard, legacy_sse=False)["id"] == 9


def test_utf16_and_utf32_bodies_are_refused_but_utf8_cjk_and_emoji_pass():
    """第三輪 P1-A：UTF-16／32 讓結構掃描失準、json.loads 卻看得懂，守門前先拒收；合法 UTF-8（中文、日文、emoji）照常解析。"""
    guard = app.state.mcp_structure_limits
    text = _call(b'{"x":["\xe2\x88\x80",[],[],[]]}').decode()
    for encoding in ("utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be"):
        with pytest.raises(RequestLimitError) as caught:
            proxy._parse_unlimited(POST, text.encode(encoding), guard, legacy_sse=False)
        assert "UTF-8" in caught.value.payload["error"]["message"], encoding
    ok = _call('{"text":"你好，こんにちは 😀🎉"}'.encode())
    assert proxy._parse_unlimited(POST, ok, guard, legacy_sse=False)["id"] == 9
