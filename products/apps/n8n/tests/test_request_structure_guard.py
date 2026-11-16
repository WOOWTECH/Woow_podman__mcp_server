"""n8n：產品自己宣告的 McpRequestLimits 不放寬，並且同樣擋住整個請求的結構放大（閘道請求內容位元組預算第二輪 P1-A）。"""
from __future__ import annotations

import pytest
from mcp_admin_core.request_limits import DEFAULT_MAX_TOTAL_ELEMENTS, RequestLimitError

from n8n_mcp_admin.main import app


def _call(arguments: bytes) -> bytes:
    return b'{"jsonrpc":"2.0","id":9,"method":"tools/call","params":{"name":"t","arguments":' + arguments + b"}}"


def test_declared_limits_are_unchanged_and_shared_with_the_structure_guard():
    limits = app.state.mcp_request_limits
    assert (limits.max_bytes, limits.max_argument_elements, limits.max_depth) == (4 * 1024 * 1024, 10_000, 64)
    assert app.state.mcp_structure_limits is limits
    assert limits.max_total_elements == DEFAULT_MAX_TOTAL_ELEMENTS  # 預設的整個請求元素上限只會更嚴，不放寬任何既有值


def test_structure_amplification_outside_the_arguments_is_refused():
    """既有的元素上限只算工具參數；放在別處（params 的其他欄位）的結構放大也要擋。"""
    limits = app.state.mcp_request_limits
    body = b'{"jsonrpc":"2.0","id":9,"method":"tools/list","params":{"x":[' + b"[]," * DEFAULT_MAX_TOTAL_ELEMENTS + b"[]]}}"
    if len(body) > limits.max_bytes:
        body = b'{"jsonrpc":"2.0","id":9,"method":"tools/list","params":{"x":[' + b"[]," * (limits.max_bytes // 4) + b"[]]}}"
    with pytest.raises(RequestLimitError):
        limits.parse_and_check(body)


def test_legitimate_call_is_parsed():
    assert app.state.mcp_request_limits.parse_and_check(_call(b'{"items":[1,2,3]}'))["id"] == 9


def test_utf16_and_utf32_bodies_are_refused_before_any_parsing():
    """第三輪 P1-A：UTF-16／32 讓結構掃描失準、json.loads 卻看得懂（_check_depth 的 utf-8 解碼對 ``[\\x00`` 也成功）。"""
    limits = app.state.mcp_request_limits
    text = '{"jsonrpc":"2.0","id":9,"method":"tools/list","params":{"x":["∀",' + ",".join(["[]"] * 50) + "]}}"
    for encoding in ("utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be"):
        with pytest.raises(RequestLimitError) as caught:
            limits.parse_and_check(text.encode(encoding))
        assert "UTF-8" in caught.value.payload["error"]["message"], encoding


def test_valid_utf8_cjk_and_emoji_is_parsed():
    limits = app.state.mcp_request_limits
    body = _call('{"text":"你好，こんにちは 😀🎉"}'.encode())
    assert limits.parse_and_check(body)["id"] == 9


def test_structure_refusal_has_the_same_format_as_the_other_products():
    """第三輪 F7：認得出 id 就 HTTP 200＋帶該 id 的 -32602（MCP SDK 對非 2xx 會讓整個 session 失敗），認不出才 413。"""
    limits = app.state.mcp_request_limits
    too_deep = _call(b"[" * (limits.max_depth + 1) + b"]" * (limits.max_depth + 1))
    with pytest.raises(RequestLimitError) as caught:
        limits.parse_and_check(too_deep)
    assert caught.value.status == 200 and caught.value.payload["id"] == 9 and caught.value.payload["error"]["code"] == -32602
    with pytest.raises(RequestLimitError) as legacy:
        limits.parse_and_check(too_deep, legacy_sse=True)
    assert legacy.value.status == 413
    with pytest.raises(RequestLimitError) as no_id:
        limits.parse_and_check(b'{"jsonrpc":"2.0","method":"notifications/x","params":' + b"[" * (limits.max_depth + 1) + b"]" * (limits.max_depth + 1) + b"}")
    assert no_id.value.status == 413 and no_id.value.payload["id"] is None
