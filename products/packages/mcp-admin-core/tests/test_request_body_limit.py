"""請求內容上限 16 MiB（mcp_admin_core/request_body.py）：SDK transport、DualTransportApp 與 admin 閘道。

mcp 1.30.0（2026-10-16 安全下限）的 transport 預設 4 MiB、超過就在處理前回 413 純文字；Nextcloud upload_file
（base64，上限 10 MiB）因此超過約 3 MiB 就失敗。真的子行程端到端見 test_child_request_body_limit.py。
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import uuid

import httpx
import pytest
from starlette.requests import Request
from starlette.testclient import TestClient

from mcp_admin_core import request_body
from mcp_admin_core.request_body import DRAIN_BYTES, MAX_REQUEST_BODY_BYTES

MiB = 1024 * 1024
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}


def sdk_request(size: int, rpc_id=7, method="tools/call") -> bytes:
    """MCP Python／TS SDK 的寫法（method、params 在前，jsonrpc、id 在最後），整個請求剛好 *size* 位元組。"""
    shell = {"method": method, "params": {"name": "upload_file", "arguments": {"content_base64": ""}},
             "jsonrpc": "2.0", "id": rpc_id}
    empty = len(json.dumps(shell, separators=(",", ":")))
    shell["params"]["arguments"]["content_base64"] = "A" * (size - empty)
    body = json.dumps(shell, separators=(",", ":")).encode()
    assert len(body) == size
    return body


def ping(size: int, rpc_id=2) -> bytes:
    """任何 MCP server 都接受的 ping，以 params._meta 補到剛好 *size* 位元組（不需要後端）。"""
    shell = {"jsonrpc": "2.0", "id": rpc_id, "method": "ping", "params": {"_meta": {"padding": ""}}}
    empty = len(json.dumps(shell, separators=(",", ":")))
    shell["params"]["_meta"]["padding"] = "A" * (size - empty)
    body = json.dumps(shell, separators=(",", ":")).encode()
    assert len(body) == size
    return body


def test_ceiling_covers_the_largest_product_input():
    """16 MiB 要裝得下 Nextcloud upload_file 的產品上限（10 MiB 檔案的 base64＋JSON 外殼）。"""
    from nextcloud_mcp_server.settings import Settings

    max_upload = Settings.model_fields["max_upload_bytes"].default
    assert max_upload == 10 * MiB
    base64_len = -(-max_upload // 3) * 4
    body = sdk_request(base64_len + 200)  # 外殼（path、id…）遠小於 200 bytes 的餘裕
    assert MAX_REQUEST_BODY_BYTES == 16 * MiB
    assert 4 * MiB < len(body) < MAX_REQUEST_BODY_BYTES
    assert DRAIN_BYTES > 100 * 1000 * 1000  # 經 Cloudflare（100 MB）進來的請求都讀得到結尾的 id


# ---------------------------------------------------------------------------
# install()：fastmcp 子行程（litellm、nextcloud；fastmcp 4.x，#78）的 SDK 預設值。改的是全域類別，所以在子行程裡驗。
# ---------------------------------------------------------------------------

INSTALL_PROBE = r"""
import json, uuid
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager as Manager
from mcp_admin_core import request_body
out = {"before": Manager(app=object()).max_request_body_size}
original = Manager.__init__
request_body.install()
request_body.install()  # 冪等：不會包兩層
out["single_wrap"] = Manager.__init__.__wrapped__ is original
out["default"] = Manager(app=object()).max_request_body_size
out["stateless"] = Manager(app=object(), stateless=True).max_request_body_size
out["explicit"] = Manager(app=object(), max_request_body_size=1024).max_request_body_size
out["positional"] = Manager(object(), None, False, False, None, None, 1800.0, 2048).max_request_body_size
out["session_idle"] = Manager(app=object()).session_idle_timeout  # PaaS 維持 SDK 預設 30 分鐘
from fastmcp.server.http import FastMCPStreamableHTTPSessionManager
out["fastmcp"] = FastMCPStreamableHTTPSessionManager(app=object()).max_request_body_size

# SSE：沒有 session 的 POST，未超過上限時走到 session 查詢（404），超過才是 413。
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient
def sse_status(transport, size):
    app = Starlette(routes=[Mount("/messages/", app=transport.handle_post_message)])
    with TestClient(app) as client:
        return client.post("/messages/?session_id=" + uuid.uuid4().hex, content=b"x" * size,
                           headers={"content-type": "application/json"}).status_code
out["sse_default"] = [sse_status(SseServerTransport("/messages/"), n) for n in (5 * 2**20, 16 * 2**20 + 1)]
out["sse_explicit"] = sse_status(SseServerTransport("/messages/", max_request_body_size=1024), 2048)
print(json.dumps(out))
"""


def test_install_makes_the_ceiling_the_sdk_default_only_where_callers_pass_nothing():
    done = subprocess.run([sys.executable, "-c", INSTALL_PROBE], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    out = json.loads(done.stdout.splitlines()[-1])
    assert out == {
        "before": 4 * MiB,  # MCP SDK（1.30.0 起，2.x 相同）的預設：沒有 install 的行程就是這個上限
        "single_wrap": True,
        "default": MAX_REQUEST_BODY_BYTES,
        "stateless": MAX_REQUEST_BODY_BYTES,
        "explicit": 1024,
        "positional": 2048,
        "session_idle": 1800.0,
        "fastmcp": MAX_REQUEST_BODY_BYTES,  # fastmcp 4.1.3（3.4.11 亦同）建 session manager 時不傳這個參數
        "sse_default": [404, 413],
        "sse_explicit": 413,
    }


def test_apply_to_run_passes_the_ceiling_to_mcpserver_http_transports():
    """odoo（mcp 2.x 的 MCPServer）：上限只能從 run() 傳入，apply_to_run 補在沒指定的 HTTP transport 上。"""
    import inspect

    from mcp.server.mcpserver import MCPServer

    from mcp_admin_core.request_body import apply_to_run

    # 前提：SDK 2.x 的兩個 HTTP transport 都從 run() 接這個參數（SDK 改了這裡就會先紅）
    for runner in (MCPServer.run_streamable_http_async, MCPServer.run_sse_async):
        assert "max_request_body_size" in inspect.signature(runner).parameters

    calls = []

    class Server:
        def run(self, transport="stdio", **kwargs):
            calls.append((transport, kwargs))

    server = Server()
    apply_to_run(server)
    apply_to_run(server)  # 冪等：不會包兩層
    server.run(transport="streamable-http", host="127.0.0.1", port=8000)
    server.run("sse")
    server.run(transport="stdio")
    server.run(transport="streamable-http", max_request_body_size=1024)
    assert calls == [
        ("streamable-http", {"host": "127.0.0.1", "port": 8000, "max_request_body_size": MAX_REQUEST_BODY_BYTES}),
        ("sse", {"max_request_body_size": MAX_REQUEST_BODY_BYTES}),
        ("stdio", {}),
        ("streamable-http", {"max_request_body_size": 1024}),
    ]


# ---------------------------------------------------------------------------
# DualTransportApp（hermes、opendesign）：兩個 transport 都明確設上限
# ---------------------------------------------------------------------------


def _fake_server():
    from mcp.server.lowlevel import Server

    return Server("fake")


@pytest.mark.parametrize("stateless", [False, True])
def test_dual_transport_streamable_http_accepts_up_to_the_ceiling(stateless):
    from mcp_admin_core.mcp_sse_wrapper import DualTransportApp

    with TestClient(DualTransportApp(_fake_server(), stateless=stateless)) as client:
        init = client.post("/mcp", json=INIT, headers=HEADERS)
        assert init.status_code == 200
        headers = {**HEADERS, "mcp-protocol-version": "2025-06-18"}
        if not stateless:
            headers["mcp-session-id"] = init.headers["mcp-session-id"]
            client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers)
        for size in (5 * MiB, MAX_REQUEST_BODY_BYTES):
            reply = client.post("/mcp", content=ping(size), headers=headers)
            assert reply.status_code == 200, (size, reply.text[:200])
            assert '"id":2,"result":{}' in reply.text
        refused = client.post("/mcp", content=ping(MAX_REQUEST_BODY_BYTES + 1), headers=headers)
        assert refused.status_code == 413


def test_dual_transport_sse_messages_accept_up_to_the_ceiling():
    from mcp_admin_core.mcp_sse_wrapper import DualTransportApp

    with TestClient(DualTransportApp(_fake_server())) as client:
        def post(size):
            return client.post(f"/messages/?session_id={uuid.uuid4().hex}", content=ping(size), headers=HEADERS)

        # 沒有這個 session：未超過上限時走到 session 查詢（404），超過才在讀取時回 413。
        assert post(5 * MiB).status_code == 404
        assert post(MAX_REQUEST_BODY_BYTES).status_code == 404
        assert post(MAX_REQUEST_BODY_BYTES + 1).status_code == 413


def test_dual_transport_explicit_limit_is_honoured():
    from mcp_admin_core.mcp_sse_wrapper import DualTransportApp

    with TestClient(DualTransportApp(_fake_server(), stateless=True, max_request_body_size=MiB)) as client:
        assert client.post("/mcp", content=ping(MiB + 1), headers=HEADERS).status_code == 413
        assert client.post(f"/messages/?session_id={uuid.uuid4().hex}", content=ping(MiB + 1),
                           headers=HEADERS).status_code == 413


# ---------------------------------------------------------------------------
# admin 閘道（proxy.py）：n8n 以外的產品
# ---------------------------------------------------------------------------


@pytest.fixture
def gateway(make_app, monkeypatch):
    """非 n8n 產品的 admin（mcp_request_limits=None）；子行程以 MockTransport 代替，可設定回應。"""
    from mcp_admin_core import proxy

    forwarded: list[httpx.Request] = []
    child = {"status": 200}

    def upstream(request: httpx.Request) -> httpx.Response:
        request.read()
        forwarded.append(request)
        if child["status"] == 413:
            return httpx.Response(413, text="Request body too large")  # mcp SDK 的 RequestBodyLimitMiddleware
        if request.method == "GET":
            return httpx.Response(200, content=b"event: endpoint\ndata: /messages/?session_id=s\n\n",
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 7, "result": {"content": []}})

    monkeypatch.setattr(proxy, "_get_client",
                        lambda timeout=86400: httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    client = make_app({"mcp_auth_token": "secret"})
    assert client.app.state.mcp_request_limits is None
    return client, forwarded, child


def _assert_readable(error: dict) -> None:
    assert error["code"] == -32602
    assert "16 MiB" in error["message"] and "上限" in error["message"]


@pytest.mark.parametrize("size", [5 * MiB, MAX_REQUEST_BODY_BYTES])
def test_gateway_forwards_bodies_up_to_the_ceiling_unchanged(gateway, size):
    client, forwarded, _ = gateway
    body = sdk_request(size)
    response = client.post("/private_secret/mcp", content=body, headers=HEADERS)
    assert response.status_code == 200
    assert len(forwarded) == 1 and forwarded[0].content == body


@pytest.mark.parametrize("size", [MAX_REQUEST_BODY_BYTES + 1, 20 * MiB])
def test_gateway_refuses_larger_requests_with_a_readable_error_for_that_call(gateway, size):
    """超過上限：不轉發；Streamable HTTP 回 200＋帶請求 id 的 JSON-RPC 錯誤（client 只這次呼叫失敗）。"""
    client, forwarded, _ = gateway
    response = client.post("/private_secret/mcp", content=sdk_request(size, rpc_id=42), headers=HEADERS)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    reply = response.json()
    assert reply["jsonrpc"] == "2.0" and reply["id"] == 42 and "result" not in reply
    _assert_readable(reply["error"])
    assert not forwarded


def test_gateway_finds_the_id_at_the_start_too(gateway):
    client, forwarded, _ = gateway
    body = b'{"jsonrpc":"2.0","id":"req-1","method":"tools/call","params":{"x":"' + b"A" * (17 * MiB) + b'"}}'
    reply = client.post("/private_secret/mcp", content=body, headers=HEADERS)
    assert reply.status_code == 200 and reply.json()["id"] == "req-1"
    assert not forwarded


# 通知沒有 id；batch、參數裡的 "id"、不是 JSON 都不猜：413＋id 為 null（TS SDK 會把內容放進錯誤訊息）
UNKNOWN_ID_BODIES = {
    "notification": b'{"method":"notifications/message","params":{"x":"%s"},"jsonrpc":"2.0"}',
    "batch": b'[{"method":"tools/call","params":{"x":"%s"},"jsonrpc":"2.0","id":1}]',
    "nested-id": b'{"method":"tools/call","params":{"x":"%s","jsonrpc":"2.0","id":5}}',
    "not-json": b"%s",
}


@pytest.mark.parametrize("kind", sorted(UNKNOWN_ID_BODIES))
def test_gateway_refusal_without_a_known_id_is_a_413_with_a_json_rpc_error(gateway, kind):
    client, forwarded, _ = gateway
    body = UNKNOWN_ID_BODIES[kind] % (b"A" * (17 * MiB))
    response = client.post("/private_secret/mcp", content=body, headers=HEADERS)
    assert response.status_code == 413
    assert response.json()["id"] is None
    _assert_readable(response.json()["error"])
    assert not forwarded


def test_gateway_refuses_legacy_sse_posts_with_413(gateway):
    """legacy SSE 的 POST 只看狀態碼（回應走 GET 串流）：413＋可讀的 JSON-RPC 錯誤（帶得到 id 就帶）。"""
    client, forwarded, _ = gateway
    response = client.post("/private_secret/messages/?session_id=s", content=sdk_request(17 * MiB, rpc_id=9),
                           headers=HEADERS)
    assert response.status_code == 413
    assert response.json()["id"] == 9
    _assert_readable(response.json()["error"])
    assert not forwarded


async def _drive(app, *, path, headers, chunks):
    """直接以 ASGI 送出分塊請求；回 (HTTP 狀態, JSON 內容, 實際被讀走的分塊數)。"""
    consumed = []
    completed = asyncio.Event()
    queue = list(chunks)

    async def receive():
        await asyncio.sleep(0)
        if queue:
            consumed.append(True)
            chunk = queue.pop(0)
            return {"type": "http.request", "body": chunk, "more_body": bool(queue)}
        await completed.wait()
        return {"type": "http.disconnect"}

    sent = []

    async def send(event):
        sent.append(event)
        if event["type"] == "http.response.body" and not event.get("more_body", False):
            completed.set()

    raw_path, _, query = path.partition("?")
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
             "scheme": "http", "path": raw_path, "raw_path": raw_path.encode(), "query_string": query.encode(),
             "headers": headers, "client": ("127.0.0.1", 1), "server": ("testserver", 80)}
    await asyncio.wait_for(app(scope, receive, send), timeout=30)
    status = next(e for e in sent if e["type"] == "http.response.start")["status"]
    body = b"".join(e.get("body", b"") for e in sent if e["type"] == "http.response.body")
    return status, json.loads(body), len(consumed)


def test_gateway_refuses_an_absurd_declared_length_without_reading(gateway):
    # 不送任何分塊：閘道若嘗試讀取，receive() 會一直等到回應送完——也就是卡到逾時。
    client, forwarded, _ = gateway
    headers = [(b"content-length", str(DRAIN_BYTES + 1).encode()), (b"content-type", b"application/json")]
    status, reply, consumed = asyncio.run(_drive(client.app, path="/private_secret/mcp", headers=headers,
                                                 chunks=[]))
    assert status == 413 and reply["id"] is None
    _assert_readable(reply["error"])
    assert consumed == 0 and not forwarded


def test_chunked_oversize_is_read_to_the_end_and_answered_with_the_id(gateway):
    """沒有 Content-Length 的分塊上傳：超過上限後只留頭尾（不整包進記憶體）繼續讀，讀到結尾找到 id。"""
    client, forwarded, _ = gateway
    body = sdk_request(20 * MiB, rpc_id=77)
    chunks = [body[i:i + MiB] for i in range(0, len(body), MiB)]
    status, reply, consumed = asyncio.run(_drive(client.app, path="/private_secret/mcp",
                                                 headers=[(b"transfer-encoding", b"chunked")], chunks=chunks))
    assert status == 200 and reply["id"] == 77
    _assert_readable(reply["error"])
    assert consumed == len(chunks) and not forwarded


def test_gateway_stops_draining_after_drain_bytes(gateway, monkeypatch):
    client, forwarded, _ = gateway
    monkeypatch.setattr(request_body, "DRAIN_BYTES", 17 * MiB)
    body = sdk_request(20 * MiB, rpc_id=5)
    chunks = [body[i:i + MiB] for i in range(0, len(body), MiB)]
    status, reply, consumed = asyncio.run(_drive(client.app, path="/private_secret/mcp",
                                                 headers=[(b"transfer-encoding", b"chunked")], chunks=chunks))
    # id 在結尾、還沒讀到就停：413＋id 為 null；只讀到超過 DRAIN_BYTES 的那一塊（第 18 塊）為止
    assert status == 413 and reply["id"] is None
    _assert_readable(reply["error"])
    assert consumed == 18 and not forwarded


def test_auth_happens_before_reading_any_body(gateway, monkeypatch):
    client, forwarded, _ = gateway

    async def forbidden(self):
        raise AssertionError("unauthenticated body must not be consumed")
        yield b""  # async iterator

    monkeypatch.setattr(Request, "stream", forbidden)
    response = client.post("/private_wrong/mcp", content=b"x",
                           headers={"content-length": str(MAX_REQUEST_BODY_BYTES + 1)})
    assert response.status_code == 403
    assert not forwarded


# ---------------------------------------------------------------------------
# 子行程自己回 413（例如上限較低的非 Python 子行程）：閘道換成可讀的 JSON-RPC 錯誤
# ---------------------------------------------------------------------------


def test_child_413_becomes_a_json_rpc_error_for_that_call(gateway):
    client, forwarded, child = gateway
    child["status"] = 413
    response = client.post("/private_secret/mcp", content=sdk_request(MiB, rpc_id=3), headers=HEADERS)
    assert response.status_code == 200
    reply = response.json()
    assert reply["id"] == 3 and reply["error"]["code"] == -32602
    assert "too large" in reply["error"]["message"] and "過大" in reply["error"]["message"]
    assert "Request body too large" not in response.text  # 子行程的原始內容不轉出
    assert len(forwarded) == 1


def test_child_413_for_a_batch_answers_every_request(gateway):
    client, _, child = gateway
    child["status"] = 413
    batch = [{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "a"}},
             {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}},
             {"jsonrpc": "2.0", "id": "b", "method": "tools/list"}]
    response = client.post("/private_secret/mcp", json=batch, headers=HEADERS)
    assert response.status_code == 200
    assert [r["id"] for r in response.json()] == [1, "b"]


@pytest.mark.parametrize(("path", "rpc", "expected_id"), [
    ("messages/?session_id=s", {"jsonrpc": "2.0", "id": 4, "method": "tools/call"}, 4),
    ("mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"}, None),
])
def test_child_413_without_a_reply_channel_stays_413_but_readable(gateway, path, rpc, expected_id):
    client, _, child = gateway
    child["status"] = 413
    response = client.post("/private_secret/" + path, json=rpc, headers=HEADERS)
    assert response.status_code == 413
    assert response.json()["id"] == expected_id
    assert "過大" in response.json()["error"]["message"]


def test_n8n_keeps_its_own_4_mib_limit(gateway):
    """n8n 的 McpRequestLimits（4 MiB＋元素／深度，另案審過）不受共用上限影響。"""
    from mcp_admin_core.request_limits import McpRequestLimits

    client, forwarded, _ = gateway
    client.app.state.mcp_request_limits = McpRequestLimits(4 * MiB, 10_000)
    response = client.post("/private_secret/mcp", content=sdk_request(5 * MiB), headers=HEADERS)
    assert response.status_code == 413
    assert "4194304 bytes" in response.json()["error"]["message"]
    assert not forwarded
