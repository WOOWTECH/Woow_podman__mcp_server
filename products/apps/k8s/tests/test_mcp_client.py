"""mcp_client（raw httpx JSON-RPC）的契約測試（Test 路徑一的協定層）。

不引入 mcp SDK，用 httpx 完成 Streamable HTTP 三往返：initialize → 擷取
``Mcp-Session-Id`` → notifications/initialized → tools/call；並解析 JSON 與
SSE-framed（``data: `` 行）兩種回應。timeout / 連線失敗翻成可分辨的自訂例外，
讓呼叫端把「叢集 API 沒回」與「子行程 HTTP listener 沒就緒」分層誠實表述。
"""
from __future__ import annotations

import json

import httpx
import pytest

from k8s_mcp_admin import mcp_client
from k8s_mcp_admin.mcp_client import (
    McpTimeout,
    McpUnavailable,
    call_tool_via_mcp,
)


def _req_method(request: httpx.Request) -> str:
    try:
        return json.loads(request.content).get("method", "")
    except ValueError:
        return ""


def _ok_result_response(text: str = "namespace list...") -> httpx.Response:
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

    result = await call_tool_via_mcp(
        8000, "namespaces_list", {}, transport=httpx.MockTransport(handler)
    )

    assert result.ok is True
    # initialize 之後的每個請求都帶回擷取到的 session id
    after_init = [h for h in seen_headers if h["method"] != "initialize"]
    assert after_init  # 確實有後續請求
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
        # tools/call 回 SSE-framed
        payload = {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {"content": [{"type": "text", "text": "sse-ok"}], "isError": False},
        }
        sse = f"event: message\ndata: {json.dumps(payload)}\n\n"
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=sse)

    result = await call_tool_via_mcp(
        8000, "namespaces_list", {}, transport=httpx.MockTransport(handler)
    )

    assert result.ok is True
    assert "sse-ok" in json.dumps(result.payload)


async def test_tools_call_result_extracted():
    def handler(request: httpx.Request) -> httpx.Response:
        if _req_method(request) == "initialize":
            return httpx.Response(200, headers={"Mcp-Session-Id": "s"}, json={"jsonrpc": "2.0", "id": 1, "result": {}})
        return _ok_result_response("default\nkube-system")

    result = await call_tool_via_mcp(
        8000, "namespaces_list", {}, transport=httpx.MockTransport(handler)
    )

    assert result.ok is True
    assert result.payload is not None


# ---------------------------------------------------------------------------
# 錯誤分類
# ---------------------------------------------------------------------------


async def test_tool_error_forbidden_detected():
    def handler(request: httpx.Request) -> httpx.Response:
        if _req_method(request) == "initialize":
            return httpx.Response(200, headers={"Mcp-Session-Id": "s"}, json={"jsonrpc": "2.0", "id": 1, "result": {}})
        body = {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "content": [{"type": "text", "text": 'namespaces is forbidden: User "x" cannot list'}],
                "isError": True,
            },
        }
        return httpx.Response(200, json=body)

    result = await call_tool_via_mcp(
        8000, "namespaces_list", {}, transport=httpx.MockTransport(handler)
    )

    assert result.ok is False
    assert result.kind == "rbac"


async def test_tool_error_generic_is_tool_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if _req_method(request) == "initialize":
            return httpx.Response(200, headers={"Mcp-Session-Id": "s"}, json={"jsonrpc": "2.0", "id": 1, "result": {}})
        body = {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {"content": [{"type": "text", "text": "some other failure"}], "isError": True},
        }
        return httpx.Response(200, json=body)

    result = await call_tool_via_mcp(
        8000, "namespaces_list", {}, transport=httpx.MockTransport(handler)
    )

    assert result.ok is False
    assert result.kind == "tool_error"


# ---------------------------------------------------------------------------
# 可分辨的傳輸層例外
# ---------------------------------------------------------------------------


async def test_timeout_raises_distinct():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    with pytest.raises(McpTimeout):
        await call_tool_via_mcp(8000, "namespaces_list", {}, transport=httpx.MockTransport(handler))


async def test_connect_error_raises_distinct():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(McpUnavailable):
        await call_tool_via_mcp(8000, "namespaces_list", {}, transport=httpx.MockTransport(handler))
