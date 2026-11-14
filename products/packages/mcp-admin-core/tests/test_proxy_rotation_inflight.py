"""負載中換 token：進行中的請求要嘛正常完成、要嘛拿到乾淨的 403 或 JSON-RPC 錯誤，不再有 traceback 或 500（P3-a）。

證據（mp-odoo-iso-20261113）：receipts/s12b-repro-rotation-traceback.json——重負載下輪替 token，24 段 89 行的
「Exception in ASGI application」，最後一行都是 ``starlette.requests.ClientDisconnect``（mcp_access.py
``stream_response`` 拋出），用戶端 24 次 ConnectionError；沒有輪替的對照組 0 段。s12-extra-cross-write-and-churn.json——
負載中刪帳號時 1 次 HTTP 500，旁邊也是同一個 ClientDisconnect。舊 token 的新請求一律 403（不能因為修正而放行）。

真的 admin（uvicorn，含 create_app 全部 middleware）＋真的 MCP 子行程（另一個 process），輪替走管理 API，與實測相同。
"""
from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

from loopback_servers import RecordHandler, ThreadedUvicorn, mcp_child_process
from management_client import async_revision_headers

TOKEN = "rotation-inflight-test-token-00000000"  # 合成值
_ACCEPT = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
_INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                          "clientInfo": {"name": "rotation-test", "version": "0"}}}


def _sse_messages(body: str) -> list[dict]:
    out = []
    for event in body.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in event.split("\n") if line.startswith("data:"))
        if data:
            out.append(json.loads(data))
    return out


def _no_traceback(logs: RecordHandler) -> None:
    assert not [r for r in logs.records if "Exception in ASGI application" in r.getMessage()]
    assert not [r for r in logs.records if r.exc_info or r.levelno >= logging.ERROR]


@pytest.fixture
def gateway(tmp_config_path, monkeypatch):
    pytest.importorskip("mcp.server.mcpserver")
    import mcp_admin_core.config.store as store_mod
    from mcp_admin_core import create_app, proxy
    from managed_proxy_fixture import attach

    monkeypatch.setattr(proxy, "_client", None)
    with mcp_child_process() as (port, child):
        attach(monkeypatch, child)
        config = json.loads(json.dumps(store_mod._DEFAULT_CONFIG))
        config.update(mcp_auth_token=TOKEN, mcp_server={"command": "", "args": [], "port": port, "env": {}})
        tmp_config_path.write_text(json.dumps(config))
        monkeypatch.setenv("MCP_ADMIN_CONFIG", str(tmp_config_path))
        monkeypatch.setattr(store_mod, "_instance", None)
        with ThreadedUvicorn(create_app(title="rotation in flight")) as admin:
            yield f"http://127.0.0.1:{admin.port}"
        proxy._client = None


async def _session(client: httpx.AsyncClient, token: str) -> dict:
    url = f"/private_{token}/mcp"
    initialized = await client.post(url, json=_INITIALIZE, headers=_ACCEPT)
    assert initialized.status_code == 200
    headers = {**_ACCEPT, "mcp-session-id": initialized.headers["mcp-session-id"]}
    await client.post(url, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers)
    return headers


async def _rotate(client: httpx.AsyncClient) -> str:
    login = await client.post("/api/auth/login", json={"password": "admin"})
    auth = {"Authorization": "Bearer " + login.json()["token"]}
    response = await client.post("/api/tokens/rotate",
                                 headers=await async_revision_headers(client, auth, read_path="/api/tokens"))
    assert response.status_code == 200, response.text
    return response.json()["token"]


def _call(rpc_id: int, seconds: float) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "method": "tools/call",
            "params": {"name": "slow", "arguments": {"seconds": seconds}}}


def test_rotation_during_a_tool_call_ends_its_stream_with_a_jsonrpc_error(gateway):
    async def scenario():
        async with httpx.AsyncClient(base_url=gateway, timeout=30) as client:
            headers = await _session(client, TOKEN)
            request = client.build_request("POST", f"/private_{TOKEN}/mcp", json=_call(7, 10), headers=headers)
            response = await client.send(request, stream=True)  # 標頭已送出（SSE），工具還在跑
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            new_token = await _rotate(client)
            body = await asyncio.wait_for(response.aread(), 5)  # 不必等工具跑完
            stale = await client.post(f"/private_{TOKEN}/mcp", json=_INITIALIZE, headers=_ACCEPT)
            fresh = await client.post(f"/private_{new_token}/mcp", json=_INITIALIZE, headers=_ACCEPT)
            return body.decode(), stale, fresh, new_token

    with RecordHandler("uvicorn.error", "mcp_admin_core") as logs:
        body, stale, fresh, new_token = asyncio.run(scenario())
    replies = [m for m in _sse_messages(body) if m.get("id") == 7]
    assert len(replies) == 1 and "result" not in replies[0]
    assert replies[0]["error"]["code"] == -32005
    assert "rotated or revoked" in replies[0]["error"]["message"]
    assert stale.status_code == 403  # 舊 token 的新請求照樣擋
    assert fresh.status_code == 200
    assert TOKEN not in body and new_token not in body
    _no_traceback(logs)


def test_rotation_under_load_never_gives_500_or_a_traceback(gateway):
    """S12b 的形狀：12 個用戶端各自不停呼叫工具，中途輪替。每一筆不是正常完成（結果）、就是乾淨的 JSON-RPC 錯誤或 403；
    輪替完成之後才送出的請求一律 403。"""
    outcomes: list[tuple[str, float, float]] = []
    rotated = {}

    async def worker(client: httpx.AsyncClient, number: int, stop: asyncio.Event):
        headers = await _session(client, TOKEN)
        rpc_id = number * 10_000
        loop = asyncio.get_running_loop()
        while not stop.is_set():
            rpc_id += 1
            sent = loop.time()
            try:
                response = await client.post(f"/private_{TOKEN}/mcp", json=_call(rpc_id, 0.02), headers=headers)
            except httpx.HTTPError as exc:
                outcomes.append((f"exception:{type(exc).__name__}", sent, loop.time()))
                continue
            if response.status_code == 403:
                kind = "403"
            elif response.status_code == 200:
                replies = [m for m in _sse_messages(response.text) if m.get("id") == rpc_id]
                if any("result" in m for m in replies):
                    # 結果那一段剛交給外層、同一刻 token 換掉時，閘道無法確定它已送出，會再補一則同 id 的錯誤
                    # （寧可多一則、不要讓用戶端一直等；SDK 把已完成的 id 的第二則回應當成未知 id 忽略）。
                    kind = "result"
                elif replies and all(m.get("error", {}).get("code") == -32005 for m in replies):
                    kind = "jsonrpc-error"
                else:
                    kind = f"200-without-reply:{len(replies)}"
            else:
                kind = f"http-{response.status_code}"
            outcomes.append((kind, sent, loop.time()))
            if kind == "403":
                await asyncio.sleep(0.01)

    async def scenario():
        limits = httpx.Limits(max_connections=64, max_keepalive_connections=64)
        async with httpx.AsyncClient(base_url=gateway, timeout=30, limits=limits) as client:
            stop = asyncio.Event()
            workers = [asyncio.create_task(worker(client, n, stop)) for n in range(12)]
            await asyncio.sleep(1.0)
            loop = asyncio.get_running_loop()
            rotated["started"] = loop.time()
            await _rotate(client)
            rotated["done"] = loop.time()
            await asyncio.sleep(1.0)
            stop.set()
            await asyncio.gather(*workers)

    with RecordHandler("uvicorn.error", "mcp_admin_core") as logs:
        asyncio.run(scenario())
    kinds = {kind for kind, _, _ in outcomes}
    assert kinds <= {"result", "jsonrpc-error", "403"}, sorted(kinds)
    assert "result" in kinds and "403" in kinds
    # 輪替完成之後才送出的請求：沒有一筆拿到結果
    assert not [o for o in outcomes if o[0] == "result" and o[1] > rotated["done"]]
    _no_traceback(logs)


# ---------------------------------------------------------------------------
# LeasedStreamingResponse／LeasedResponse 本身
# ---------------------------------------------------------------------------


def _lease(changed=False):
    from mcp_admin_core.mcp_access import AccessLease

    event = asyncio.Event()
    if changed:
        event.set()
    return AccessLease(event)


class _Upstream:
    def __init__(self):
        self.closed = False

    async def aclose(self):
        self.closed = True


async def test_lease_changed_before_headers_answers_403_not_500():
    """S12 的 500：token 在回應標頭送出之前就換掉了。以前拋 ClientDisconnect（uvicorn 回 500＋traceback）。"""
    from mcp_admin_core.mcp_access import LeasedResponse, LeasedStreamingResponse

    async def body():
        yield b"NEVER"

    for response in (
        LeasedStreamingResponse(body(), lease=_lease(changed=True), upstream=_Upstream(), media_type="text/event-stream"),
        LeasedResponse(b'{"jsonrpc":"2.0","id":1,"result":{}}', lease=_lease(changed=True), media_type="application/json"),
    ):
        sent = []

        async def send(message):
            sent.append(message)

        async def receive():
            await asyncio.Event().wait()

        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 403
        assert json.loads(sent[1]["body"]) == {"detail": "MCP access changed; reconnect using the current shared token"}
        assert sent[1].get("more_body", False) is False
        assert b"NEVER" not in b"".join(m.get("body", b"") for m in sent)


async def test_lease_changed_mid_stream_sends_the_gateway_tail_then_ends():
    from mcp_admin_core.mcp_access import LeasedStreamingResponse

    lease = _lease()
    upstream = _Upstream()
    resume = asyncio.Event()

    async def body():
        yield b"event: message\ndata: first\n\n"
        await resume.wait()
        yield b"event: message\ndata: NEVER_AFTER_ROTATION\n\n"

    response = LeasedStreamingResponse(body(), lease=lease, upstream=upstream, media_type="text/event-stream",
                                       interrupted=lambda: b"event: message\ndata: gateway-error\n\n")
    sent = []

    async def send(message):
        sent.append(message)
        if message.get("body") == b"event: message\ndata: first\n\n":
            lease.changed.set()  # 第一段送出之後換 token

    async def receive():
        await asyncio.Event().wait()

    await asyncio.wait_for(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send), 5)
    bodies = [m.get("body", b"") for m in sent if m["type"] == "http.response.body"]
    assert bodies == [b"event: message\ndata: first\n\n", b"event: message\ndata: gateway-error\n\n", b""]
    assert sent[-1].get("more_body", False) is False
    assert upstream.closed


async def test_buffered_response_cut_after_headers_ends_with_jsonrpc_error_body():
    """tools/list 的過濾後 JSON（LeasedResponse）：標頭送出、本體還沒交出去時換 token → 本體換成 JSON-RPC 錯誤。
    回應不帶 Content-Length，所以可以這樣結束。"""
    from mcp_admin_core.mcp_access import LeasedResponse

    lease = _lease()
    response = LeasedResponse(b'{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}', lease=lease,
                              media_type="application/json",
                              interrupted=lambda: b'{"jsonrpc":"2.0","id":1,"error":{"code":-32005}}')
    assert b"content-length" not in dict(response.raw_headers)
    sent = []
    first_body = True

    async def send(message):
        nonlocal first_body
        if message["type"] == "http.response.body" and first_body:
            first_body = False
            lease.changed.set()
            await asyncio.Event().wait()  # 被背壓卡住，直到換 token 取消
        sent.append(message)

    async def receive():
        await asyncio.Event().wait()

    await asyncio.wait_for(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send), 5)
    assert sent[0]["status"] == 200
    assert [m.get("body") for m in sent[1:]] == [b'{"jsonrpc":"2.0","id":1,"error":{"code":-32005}}', b""]


async def test_unreachable_client_does_not_hang_the_gateway(monkeypatch):
    """用戶端完全不讀（背壓）時，閘道最多等 INTERRUPTED_FINISH_TIMEOUT 就放棄，不拋例外。"""
    from mcp_admin_core import mcp_access

    monkeypatch.setattr(mcp_access, "INTERRUPTED_FINISH_TIMEOUT", 0.2)
    lease = _lease()
    upstream = _Upstream()

    async def body():
        yield b"event: message\ndata: first\n\n"
        await asyncio.Event().wait()

    response = mcp_access.LeasedStreamingResponse(body(), lease=lease, upstream=upstream,
                                                  media_type="text/event-stream", interrupted=lambda: b"tail")

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            lease.changed.set()
            await asyncio.Event().wait()

    async def receive():
        await asyncio.Event().wait()

    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.wait_for(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send), 5)
    assert loop.time() - started < 2 and upstream.closed


def test_request_ids_and_tail_shapes():
    from mcp_admin_core import proxy

    rpc = [{"jsonrpc": "2.0", "id": 1, "method": "a"}, {"jsonrpc": "2.0", "method": "note"},
           {"jsonrpc": "2.0", "id": "x", "method": "b"}, {"jsonrpc": "2.0", "id": 9, "result": {}}]
    assert proxy._request_ids(rpc) == [1, "x"]
    assert proxy._request_ids(None) == []
    sse = proxy._Interruption(rpc, sse=True)
    sse.delivered(b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n\n')
    assert [m["id"] for m in _sse_messages(sse.tail(-32005, "m").decode())] == ["x"]
    # 很大的回應事件：只看開頭／結尾（Python SDK 與 TS SDK 的欄位順序）
    big = "x" * (70 * 1024)
    python_order = f'data: {{"jsonrpc":"2.0","id":"x","result":{{"text":"{big}"}}}}\n\n'.encode()
    ts_order = f'data: {{"result":{{"text":"{big}"}},"jsonrpc":"2.0","id":1}}\n\n'.encode()
    assert proxy._answered_ids(python_order) == ['"x"'] and proxy._answered_ids(ts_order) == ["1"]
    as_json = proxy._Interruption({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, sse=False)
    assert json.loads(as_json.tail(-32005, "m"))["id"] == 3
    as_json.delivered(b"{")
    assert as_json.tail(-32005, "m") == b""  # 送了一部分就不能再接 JSON
    assert proxy._Interruption(None, sse=True).tail(-32005, "m") == b""  # GET 串流沒有 id
