"""tool_filter 單元測試（#1384）：JSON-RPC 解析、停用判定、tools/list 過濾、SSE 逐事件改寫。"""
from __future__ import annotations

import asyncio
import json

from mcp_admin_core import tool_filter as tf

_LIST = {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "alpha"}, {"name": "beta"}]}}


def _run(coro):
    return asyncio.run(coro)


def test_normalize_disabled_accepts_list_and_csv():
    assert tf.normalize_disabled(["a", "", 3, "b"]) == {"a", "b"}
    assert tf.normalize_disabled(" a, b ,,") == {"a", "b"}
    assert tf.normalize_disabled(None) == frozenset()


def test_parse_jsonrpc_rejects_non_json():
    assert tf.parse_jsonrpc(b"") is None
    assert tf.parse_jsonrpc(b"not json") is None
    assert tf.parse_jsonrpc(b'"str"') is None
    assert tf.parse_jsonrpc(b'{"a":1}') == {"a": 1}


def test_disabled_calls_single_and_batch():
    call_b = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "beta"}}
    call_a = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "alpha"}}
    assert tf.disabled_calls(call_b, frozenset({"beta"})) == [call_b]
    assert tf.disabled_calls(call_a, frozenset({"beta"})) == []
    assert tf.disabled_calls([call_a, call_b], frozenset({"beta"})) == [call_b]
    assert tf.disabled_calls(call_b, frozenset()) == []
    # 不是 tools/call（例如 prompts/get 同名）不擋
    assert tf.disabled_calls({"method": "prompts/get", "params": {"name": "beta"}}, frozenset({"beta"})) == []


def test_disabled_error_shape():
    """停用的工具回 isError=true 的工具結果（MCP 2025-11-25 的 Tool Execution Error），文字照舊；不是 JSON-RPC error。"""
    err = tf.disabled_error({"id": 7, "method": "tools/call", "params": {"name": "beta"}})
    assert err["id"] == 7 and "error" not in err
    assert err["result"]["isError"] is True and "structuredContent" not in err["result"]
    text = err["result"]["content"][0]["text"]
    assert err["result"]["content"][0]["type"] == "text" and "beta" in text and "停用" in text


def test_rewrite_disabled_calls_renames_only_disabled():
    batch = [
        {"id": 1, "method": "tools/call", "params": {"name": "alpha"}},
        {"id": 2, "method": "tools/call", "params": {"name": "beta", "arguments": {"x": 1}}},
    ]
    out = json.loads(tf.rewrite_disabled_calls(batch, frozenset({"beta"})))
    assert out[0]["params"]["name"] == "alpha"
    assert out[1]["params"]["name"] == "beta" + tf.DISABLED_NAME_SUFFIX
    assert out[1]["params"]["arguments"] == {"x": 1}


def test_filter_json_body():
    body = json.dumps(_LIST).encode()
    out = json.loads(tf.filter_json_body(body, frozenset({"beta"})))
    assert [t["name"] for t in out["result"]["tools"]] == ["alpha"]
    # 沒有要移除的 → 原 bytes 原樣返回
    assert tf.filter_json_body(body, frozenset({"gamma"})) is body
    # 非 tools 結果原樣
    other = b'{"jsonrpc":"2.0","id":1,"result":{"content":[]}}'
    assert tf.filter_json_body(other, frozenset({"beta"})) is other


def _sse_filter(disabled):
    async def get():
        return frozenset(disabled)
    return tf.SseToolsFilter(get)


def test_sse_filter_rewrites_tools_event_split_across_chunks():
    f = _sse_filter({"beta"})
    raw = ("event: message\r\ndata: " + json.dumps(_LIST) + "\r\n\r\n").encode()

    async def go():
        out = b""
        for i in range(0, len(raw), 7):  # 任意切塊：事件跨 chunk
            out += await f.feed(raw[i:i + 7])
        return out + f.flush()

    out = _run(go())
    assert out.startswith(b"event: message\r\ndata: ") and out.endswith(b"\r\n\r\n")
    data = json.loads(out.split(b"data: ", 1)[1].strip())
    assert [t["name"] for t in data["result"]["tools"]] == ["alpha"]


def test_sse_filter_passthrough_is_byte_identical():
    f = _sse_filter({"beta"})
    raw = (b"event: endpoint\ndata: /messages/?session_id=abc\n\n"
           b": ping\n\n"
           b'event: message\ndata: {"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"tools"}]}}\n\n')

    async def go():
        return await f.feed(raw) + f.flush()

    assert _run(go()) == raw


def test_sse_filter_does_not_read_config_without_tools_event():
    calls = []

    async def get():
        calls.append(1)
        return frozenset({"beta"})

    f = tf.SseToolsFilter(get)
    _run(f.feed(b"event: endpoint\ndata: /messages/\n\n"))
    assert calls == []


def test_sse_filter_multiline_data_and_trailing_partial():
    f = _sse_filter({"beta"})
    body = json.dumps(_LIST, indent=1).splitlines()
    raw = ("event: message\n" + "".join(f"data: {ln}\n" for ln in body) + "\n").encode()

    async def go():
        out = await f.feed(raw + b"event: message\ndata: {\"partial")
        return out, f.flush()

    out, rest = _run(go())
    data = json.loads(out.split(b"data: ", 1)[1].strip())
    assert [t["name"] for t in data["result"]["tools"]] == ["alpha"]
    assert out.count(b"data:") == 1           # 多行 data 合併成一行
    assert rest == b"event: message\ndata: {\"partial"  # 串流結束時殘段原樣送出


def test_sse_filter_no_disabled_keeps_event():
    f = _sse_filter(set())
    raw = ("data: " + json.dumps(_LIST) + "\n\n").encode()
    assert _run(f.feed(raw)) == raw
