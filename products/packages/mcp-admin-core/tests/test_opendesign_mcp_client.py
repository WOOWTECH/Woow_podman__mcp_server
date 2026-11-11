"""opendesign mcp_client（raw httpx JSON-RPC）契約測試（Test 路徑一的協定層）。

不引入 mcp SDK，用 httpx 對子行程（supergateway 橋出的 ``/mcp`` Streamable HTTP）完成
三往返：initialize → 擷取 ``Mcp-Session-Id`` → notifications/initialized → tools/call；
並解析 JSON 與 SSE-framed（``data: `` 行）兩種回應。timeout / 連線失敗翻成可分辨的
自訂例外，讓呼叫端把「daemon 沒回」與「supergateway HTTP 尚未就緒」分層誠實表述。

形比照 apps/k8s/tests/test_mcp_client.py，但工具改為 ``health``（opendesign 的 System
探針；無 RBAC 概念，故不測 forbidden 分類）。
"""
from __future__ import annotations

import json

import httpx
import pytest

from opendesign_mcp_admin import mcp_client
from opendesign_mcp_admin.mcp_client import (
    McpTimeout,
    McpUnavailable,
    call_tool_via_mcp,
)


def _req_method(request: httpx.Request) -> str:
    try:
        return json.loads(request.content).get("method", "")
    except ValueError:
        return ""


def _ok_result_response(text: str = "ok") -> httpx.Response:
    body = {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {"content": [{"type": "text", "text": text}], "isError": False},
    }
    return httpx.Response(200, json=body)


# ---------------------------------------------------------------------------
# session id 擷取與往返
# ---------------------------------------------------------------------------


async def test_initialize_captures_session_id():
    seen_headers: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = _req_method(request)
        seen_headers.append({"method": method, "session": request.headers.get("mcp-session-id")})
        if method == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "abc", "Content-Type": "application/json"},
                json={"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}},
            )
        return _ok_result_response()

    result = await call_tool_via_mcp(8000, "health", {}, transport=httpx.MockTransport(handler))

    assert result.ok is True
    after_init = [h for h in seen_headers if h["method"] != "initialize"]
    assert after_init  # 確有後續請求
    assert all(h["session"] == "abc" for h in after_init)


async def test_parse_sse_framed_response():
    def handler(request: httpx.Request) -> httpx.Response:
        method = _req_method(request)
        if method == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "s1", "Content-Type": "application/json"},
                json={"jsonrpc": "2.0", "id": 1, "result": {}},
            )
        payload = {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {"content": [{"type": "text", "text": "sse-ok"}], "isError": False},
        }
        sse = f"event: message\ndata: {json.dumps(payload)}\n\n"
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=sse)

    result = await call_tool_via_mcp(8000, "health", {}, transport=httpx.MockTransport(handler))

    assert result.ok is True
    assert "sse-ok" in json.dumps(result.payload)


async def test_tools_call_result_extracted():
    def handler(request: httpx.Request) -> httpx.Response:
        if _req_method(request) == "initialize":
            return httpx.Response(200, headers={"Mcp-Session-Id": "s"}, json={"jsonrpc": "2.0", "id": 1, "result": {}})
        return _ok_result_response('{"status":"ok"}')

    result = await call_tool_via_mcp(8000, "health", {}, transport=httpx.MockTransport(handler))

    assert result.ok is True
    assert result.payload is not None


# ---------------------------------------------------------------------------
# 錯誤分類（opendesign health 無 RBAC 維度，isError 一律 tool_error）
# ---------------------------------------------------------------------------


async def test_tool_error_is_tool_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if _req_method(request) == "initialize":
            return httpx.Response(200, headers={"Mcp-Session-Id": "s"}, json={"jsonrpc": "2.0", "id": 1, "result": {}})
        body = {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {"content": [{"type": "text", "text": "daemon returned 500"}], "isError": True},
        }
        return httpx.Response(200, json=body)

    result = await call_tool_via_mcp(8000, "health", {}, transport=httpx.MockTransport(handler))

    assert result.ok is False
    assert result.kind == "tool_error"
    assert "500" in result.message


async def test_jsonrpc_error_is_tool_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if _req_method(request) == "initialize":
            return httpx.Response(200, headers={"Mcp-Session-Id": "s"}, json={"jsonrpc": "2.0", "id": 1, "result": {}})
        body = {"jsonrpc": "2.0", "id": 2, "error": {"code": -32601, "message": "method not found"}}
        return httpx.Response(200, json=body)

    result = await call_tool_via_mcp(8000, "health", {}, transport=httpx.MockTransport(handler))

    assert result.ok is False
    assert result.kind == "tool_error"
    assert "method not found" in result.message


# ---------------------------------------------------------------------------
# 可分辨的傳輸層例外
# ---------------------------------------------------------------------------


async def test_timeout_raises_distinct():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    with pytest.raises(McpTimeout):
        await call_tool_via_mcp(8000, "health", {}, transport=httpx.MockTransport(handler))


async def test_connect_error_raises_distinct():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(McpUnavailable):
        await call_tool_via_mcp(8000, "health", {}, transport=httpx.MockTransport(handler))


async def test_malformed_json_raises_base_error():
    # HTTP 200 但 body 非合法 JSON（supergateway 剛起、尚未就緒）→ 基礎 McpClientError，
    # 既非 timeout 也非 unavailable，呼叫端在該分層誠實回報「無法解析」。
    def handler(request: httpx.Request) -> httpx.Response:
        if _req_method(request) == "initialize":
            return httpx.Response(200, headers={"Mcp-Session-Id": "s"}, json={"jsonrpc": "2.0", "id": 1, "result": {}})
        return httpx.Response(200, headers={"Content-Type": "application/json"}, content="<html>not json</html>")

    with pytest.raises(mcp_client.McpClientError):
        await call_tool_via_mcp(8000, "health", {}, transport=httpx.MockTransport(handler))
