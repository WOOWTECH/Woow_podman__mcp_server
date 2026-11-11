"""n8n HTTP 邊界：SDK 接觸輸入前拒絕；其他產品沒有這些界線（只有共用的 16 MiB 請求內容上限，見 test_request_body_limit.py）。"""
import json

import httpx
import pytest
from starlette.requests import Request

from mcp_admin_core.request_limits import McpRequestLimits

LIMITS = McpRequestLimits(4 * 1024 * 1024, 10_000)


def call(arguments, rpc_id=7):
    return {"jsonrpc": "2.0", "id": rpc_id, "method": "tools/call",
            "params": {"name": "validate_workflow", "arguments": arguments}}


@pytest.fixture
def gateway(make_app, monkeypatch):
    from mcp_admin_core import proxy

    forwarded = []

    def upstream(request):
        forwarded.append(request)
        if request.method == "GET":
            return httpx.Response(200, content=b"event: endpoint\ndata: /messages/?session_id=test\n\n",
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 7, "result": {"tools": []}})

    monkeypatch.setattr(proxy, "_get_client", lambda timeout=86400: httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    client = make_app({"mcp_auth_token": "secret"})
    client.app.state.mcp_request_limits = LIMITS
    return client, forwarded


@pytest.mark.parametrize("path", ["mcp", "messages", "messages/", "sse", "other/path"])
@pytest.mark.parametrize("batch", [False, True])
def test_elements_denied_before_any_forward(gateway, path, batch):
    client, forwarded = gateway
    rpc = call({"items": [0] * 10_000})
    if batch:
        rpc = [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, rpc]
    response = client.post("/private_secret/" + path, json=rpc)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == -32602
    assert response.json()["id"] == 7
    assert not forwarded


def test_exact_element_boundary_and_catalog_forward(gateway):
    client, forwarded = gateway
    for rpc in [call({"items": [0] * 9_999}),
                {"jsonrpc": "2.0", "id": 8, "method": "tools/list"},
                {"jsonrpc": "2.0", "method": "notifications/initialized"}]:
        assert client.post("/private_secret/mcp", json=rpc).status_code == 200
    assert len(forwarded) == 3


def test_batch_shares_one_budget(gateway):
    client, forwarded = gateway
    rpc = [call({"items": [0] * 4_999}, 1), call({"items": [0] * 5_000}, 2)]
    assert client.post("/private_secret/mcp", json=rpc).status_code == 413
    assert not forwarded


def test_object_members_and_nested_elements_share_budget(gateway):
    client, forwarded = gateway
    args = {str(i): {"inner": [0, 1]} for i in range(2_501)}
    assert client.post("/private_secret/mcp", json=call(args)).status_code == 413
    assert not forwarded


def test_stringified_n8n_arguments_cannot_bypass_budget(gateway):
    client, forwarded = gateway
    for args in [{"nodes": json.dumps([0] * 10_000)},
                 {"output": json.dumps({"nodes": [0] * 10_000})}]:
        assert client.post("/private_secret/mcp", json=call(args)).status_code == 413
    assert not forwarded


def test_body_boundary(gateway):
    client, forwarded = gateway
    raw = json.dumps(call({"text": ""})).encode()
    body = raw.replace(b'"text": ""', b'"text": "' + b'x' * (LIMITS.max_bytes - len(raw)) + b'"')
    assert len(body) == LIMITS.max_bytes
    assert client.post("/private_secret/mcp", content=body).status_code == 200
    assert len(forwarded) == 1
    assert client.post("/private_secret/mcp", content=body + b' ').status_code == 413
    assert len(forwarded) == 1


@pytest.mark.parametrize("declared_length", [None, b"1"])
def test_chunked_oversize_stops_reading_and_never_forwards(gateway, declared_length):
    import asyncio

    client, forwarded = gateway
    consumed = []

    # ASGI receive 的分塊本身沒有 Content-Length，驗證不依賴可偽造的宣告長度。
    async def exercise():
        completed = asyncio.Event()

        async def receive():
            # 真實 ASGI receive 會讓出執行權；有限 body 後等回應完成才通知斷線，
            # 避免 middleware 的 disconnect listener 忙迴圈或提早取消回應。
            await asyncio.sleep(0)
            if len(consumed) < 2:
                consumed.append(True)
                return {"type": "http.request", "body": b"x" * (LIMITS.max_bytes // 2 + 1),
                        "more_body": len(consumed) < 2}
            await completed.wait()
            return {"type": "http.disconnect"}

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": "POST", "scheme": "http", "path": "/private_secret/messages/",
                 "raw_path": b"/private_secret/messages/", "query_string": b"session_id=test",
                 "headers": [(b"transfer-encoding", b"chunked")],
                 "client": ("127.0.0.1", 1), "server": ("testserver", 80)}
        if declared_length is not None:
            scope["headers"].append((b"content-length", declared_length))
        sent = []

        async def send(event):
            sent.append(event)
            if event["type"] == "http.response.body" and not event.get("more_body", False):
                completed.set()

        await asyncio.wait_for(client.app(scope, receive, send), timeout=5)
        assert next(e for e in sent if e["type"] == "http.response.start")["status"] == 413

    asyncio.run(exercise())
    assert len(consumed) == 2
    assert not forwarded


def test_auth_happens_before_body_or_size_checks(gateway, monkeypatch):
    client, forwarded = gateway

    async def forbidden_read(self):
        raise AssertionError("unauthenticated body must not be consumed")
        yield b""  # async iterator

    monkeypatch.setattr(Request, "stream", forbidden_read)
    response = client.post("/private_wrong/mcp", content=b"x",
                           headers={"content-length": str(LIMITS.max_bytes + 1)})
    assert response.status_code == 403
    assert not forwarded


def test_compressed_body_is_rejected_before_decompression(gateway):
    client, forwarded = gateway
    assert client.post("/private_secret/mcp", content=b"small", headers={"content-encoding": "gzip"}).status_code == 415
    assert not forwarded


def test_deep_json_does_not_reach_child(gateway):
    client, forwarded = gateway
    response = client.post("/private_secret/mcp", content=b"[" * 2_000 + b"0" + b"]" * 2_000)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == -32602
    assert not forwarded


def test_encoded_depth_error_preserves_rpc_id_before_forward(gateway):
    client, forwarded = gateway
    encoded = "[" * (LIMITS.max_depth + 1) + "0" + "]" * (LIMITS.max_depth + 1)
    response = client.post("/private_secret/mcp", json=call({"nodes": encoded}, "encoded-depth"))
    assert response.status_code == 413
    assert response.json()["error"]["code"] == -32602
    assert response.json()["id"] == "encoded-depth"
    assert not forwarded


def test_legacy_sse_and_normal_workflow_still_forward(gateway):
    client, forwarded = gateway
    response = client.get("/private_secret/sse")
    assert response.status_code == 200
    assert "/private_secret/messages/?session_id=test" in response.text
    workflow = {"workflow": {"nodes": [{"name": "Start", "type": "n8n-nodes-base.manualTrigger",
                                        "parameters": {}, "position": [0, 0]}], "connections": {}}}
    assert client.post("/private_secret/messages/?session_id=test", json=call(workflow)).status_code == 200
    assert len(forwarded) == 2


def test_other_products_have_no_n8n_limits(gateway):
    # 沒有 n8n 的元素／深度界線與 4 MiB；共用的 16 MiB 請求內容上限見 test_request_body_limit.py。
    client, forwarded = gateway
    client.app.state.mcp_request_limits = None
    assert client.post("/private_secret/mcp", json=call({"items": [0] * 10_000})).status_code == 200
    assert client.post("/private_secret/mcp", json=call({"text": "x" * LIMITS.max_bytes})).status_code == 200
    assert len(forwarded) == 2


def test_depth_scanner_ignores_brackets_and_escaped_quotes_in_strings(gateway):
    client, forwarded = gateway
    args = {"text": 'test \\" ' + '[' * 1_000 + ']' * 1_000}
    assert client.post("/private_secret/mcp", json=call(args)).status_code == 200
    assert len(forwarded) == 1


def test_n8n_app_enables_limits():
    from n8n_mcp_admin.main import app
    assert app.state.mcp_request_limits == LIMITS
