"""子行程在處理請求途中當掉：那筆請求拿到乾淨的錯誤，stdout 不再有一大段 traceback（P3-b）。

證據（mp-odoo-res-20261113 receipts/r2c-inflight-request-at-crash-P2.json，r2c_inflight.py）：把帳號的子行程 SIGSTOP（仍是
通過身分檢查的世代，閘道照樣轉發）、送一個工具呼叫、再 SIGKILL：2/2 回 HTTP 500 ``Internal Server Error``，容器 stdout
多一段約 163 行的「Exception in ASGI application」，最後是 ``httpx.ReadError``——proxy 只接住 ConnectError。

現在：回應標頭之前斷線 → 502＋JSON-RPC 錯誤（帶得出 id 就帶）；標頭已送出（SSE）之後斷線 → 補一則 JSON-RPC 錯誤事件後
正常結束；兩種都只記一行 warning（不含 token、不帶 traceback）。
"""
from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time

import httpx
import pytest

from loopback_servers import RecordHandler, ThreadedUvicorn, mcp_child_process, wait_until

TOKEN = "child-interrupted-test-token-00000000"  # 合成值
_ACCEPT = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
_INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                          "clientInfo": {"name": "child-interrupted-test", "version": "0"}}}


def _sse_messages(body: str) -> list[dict]:
    out = []
    for event in body.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in event.split("\n") if line.startswith("data:"))
        if data:
            out.append(json.loads(data))
    return out


def _wait_until_forwarded() -> None:
    """等閘道真的把請求送給子行程（閘道自己的連線用量），而不是只靠 sleep。"""
    from mcp_admin_core import proxy

    wait_until(lambda: proxy._client is not None and proxy._client._transport.usage()[0] >= 1, timeout=20)
    time.sleep(0.5)  # 讓請求內容寫進 socket（SSE 的情況：讓回應標頭先回到閘道）


def _no_traceback(logs: RecordHandler) -> None:
    assert not [r for r in logs.records if "Exception in ASGI application" in r.getMessage()]
    assert not [r for r in logs.records if r.exc_info or r.levelno >= logging.ERROR]


@pytest.fixture
def real_child_gateway(tmp_config_path, monkeypatch):
    """真的 MCP 子行程（另一個 process）＋真的 admin（uvicorn），回傳 (gateway 網址, 子行程 Popen, session headers)。"""
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
        with ThreadedUvicorn(create_app(title="child interrupted")) as admin:
            url = f"http://127.0.0.1:{admin.port}/private_{TOKEN}/mcp"
            with httpx.Client(timeout=30) as client:
                initialized = client.post(url, json=_INITIALIZE, headers=_ACCEPT)
                assert initialized.status_code == 200
                headers = {**_ACCEPT, "mcp-session-id": initialized.headers["mcp-session-id"]}
                client.post(url, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers)
            yield url, child, headers
        proxy._client = None


def _call_in_thread(url, headers, seconds):
    result = {}

    def run():
        try:
            with httpx.Client(timeout=30) as client:
                result["response"] = client.post(url, headers=headers, json={
                    "jsonrpc": "2.0", "id": 7, "method": "tools/call",
                    "params": {"name": "slow", "arguments": {"seconds": seconds}}})
        except Exception as exc:  # noqa: BLE001 - 記下來給斷言
            result["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    return thread, result


def test_child_killed_before_answering_gets_502_jsonrpc_error_not_500(real_child_gateway):
    """R2c 的做法：子行程凍結（SIGSTOP）時送出工具呼叫，再 SIGKILL。"""
    url, child, headers = real_child_gateway
    with RecordHandler("uvicorn.error", "mcp_admin_core") as logs:
        os.kill(child.pid, signal.SIGSTOP)
        thread, result = _call_in_thread(url, headers, 0.1)
        _wait_until_forwarded()
        os.kill(child.pid, signal.SIGKILL)
        thread.join(30)
    response = result["response"]
    assert response.status_code == 502, response.text
    body = response.json()
    assert body["id"] == 7 and body["error"]["code"] == -32004
    assert "may or may not have completed" in body["error"]["message"]
    assert TOKEN not in response.text and "127.0.0.1" not in response.text
    _no_traceback(logs)
    warnings = [r for r in logs.records if r.name == "mcp_admin_core.proxy" and r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "POST /mcp" in warnings[0].getMessage() and TOKEN not in warnings[0].getMessage()


def test_child_killed_while_streaming_ends_sse_with_jsonrpc_error(real_child_gateway):
    """工具執行中（SSE 標頭已送出）子行程被 kill：補一則同 id 的 JSON-RPC 錯誤事件後正常結束。"""
    url, child, headers = real_child_gateway
    with RecordHandler("uvicorn.error", "mcp_admin_core") as logs:
        thread, result = _call_in_thread(url, headers, 10)
        _wait_until_forwarded()
        os.kill(child.pid, signal.SIGKILL)
        thread.join(30)
    assert "error" not in result, result.get("error")
    response = result["response"]
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    replies = [m for m in _sse_messages(response.text) if m.get("id") == 7]
    assert len(replies) == 1 and replies[0]["error"]["code"] == -32004
    _no_traceback(logs)
    warnings = [r for r in logs.records if r.name == "mcp_admin_core.proxy" and r.levelno == logging.WARNING]
    assert len(warnings) == 1 and TOKEN not in warnings[0].getMessage()


# ---------------------------------------------------------------------------
# 各種斷線位置（MockTransport 的子行程）
# ---------------------------------------------------------------------------

pytestmark_mock = pytest.mark.usefixtures("synthetic_child")


class _Broken(httpx.AsyncByteStream):
    """先吐 ``chunks``，然後像子行程被 kill 那樣斷線。"""

    def __init__(self, *chunks: bytes, error=httpx.RemoteProtocolError) -> None:
        self.chunks = chunks
        self.error = error
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        raise self.error("peer closed connection without sending complete message body")

    async def aclose(self):
        self.closed = True


def _gateway(make_app, monkeypatch, handler, **config):
    from mcp_admin_core import proxy

    monkeypatch.setattr(proxy, "_get_client",
                        lambda timeout=86400: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return make_app({"mcp_auth_token": TOKEN, "mcp_server": {"command": "", "args": [], "port": 8123, "env": {}},
                     **config})


@pytestmark_mock
@pytest.mark.parametrize("error", [httpx.ReadError, httpx.RemoteProtocolError, httpx.WriteError])
def test_disconnect_before_headers_answers_502_with_each_request_id(make_app, monkeypatch, error):
    def child(request):
        raise error("child went away", request=request)

    client = _gateway(make_app, monkeypatch, child)
    single = client.post(f"/private_{TOKEN}/mcp", json={"jsonrpc": "2.0", "id": "a", "method": "tools/call",
                                                         "params": {"name": "x", "arguments": {}}})
    assert single.status_code == 502 and single.json()["id"] == "a"
    batch = client.post(f"/private_{TOKEN}/mcp", json=[
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        {"jsonrpc": "2.0", "method": "notifications/progress"},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
    ])
    assert batch.status_code == 502 and [m["id"] for m in batch.json()] == [1, 2]
    stream = client.get(f"/private_{TOKEN}/mcp")
    assert stream.status_code == 502 and stream.json()["id"] is None
    assert stream.json()["error"]["code"] == -32004


@pytestmark_mock
def test_disconnect_mid_sse_keeps_delivered_events_and_appends_error_for_pending_ids(make_app, monkeypatch):
    progress = b'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress","params":{}}\n\n'
    answered = b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n\n'
    stream = _Broken(progress, answered, b'event: message\ndata: {"jsonrpc":"2.0","id":2,"res')

    client = _gateway(make_app, monkeypatch, lambda request: httpx.Response(
        200, stream=stream, headers={"content-type": "text/event-stream"}))
    response = client.post(f"/private_{TOKEN}/mcp", json=[
        {"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])
    assert response.status_code == 200
    messages = _sse_messages(response.text)
    assert messages[0]["method"] == "notifications/progress"
    assert messages[1] == {"jsonrpc": "2.0", "id": 1, "result": {}}
    # id 1 已經回過，不重複；id 2 的半個事件丟掉、改成錯誤事件
    assert [m["id"] for m in messages[2:]] == [2] and messages[2]["error"]["code"] == -32004
    assert '"id":2,"res' not in response.text
    assert stream.closed


@pytestmark_mock
def test_disconnect_before_any_json_byte_answers_jsonrpc_error_body(make_app, monkeypatch):
    client = _gateway(make_app, monkeypatch, lambda request: httpx.Response(
        200, stream=_Broken(), headers={"content-type": "application/json"}))
    response = client.post(f"/private_{TOKEN}/mcp", json={"jsonrpc": "2.0", "id": 5, "method": "tools/list"})
    assert response.status_code == 200
    assert response.json() == {"jsonrpc": "2.0", "id": 5, "error": {
        "code": -32004, "message": response.json()["error"]["message"]}}


@pytestmark_mock
def test_disconnect_while_reading_buffered_tools_list_answers_502(make_app, monkeypatch):
    """停用了工具時 tools/list 的 JSON 回應整包讀完再過濾：讀到一半斷線也是 502＋JSON-RPC 錯誤。"""
    client = _gateway(make_app, monkeypatch, lambda request: httpx.Response(
        200, stream=_Broken(b'{"jsonrpc":"2.0","id":3,"result":{"too'), headers={"content-type": "application/json"}),
        tools={"disabled": ["hidden"]})
    response = client.post(f"/private_{TOKEN}/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    assert response.status_code == 502
    assert response.json()["id"] == 3 and response.json()["error"]["code"] == -32004


@pytestmark_mock
def test_legacy_sse_post_reply_gets_no_jsonrpc_tail(make_app, monkeypatch):
    """legacy SSE 的 POST /messages/ 回應走 GET 串流：它自己的回應內容被切斷時不補 JSON-RPC 錯誤（用戶端只看狀態碼）。"""
    client = _gateway(make_app, monkeypatch, lambda request: httpx.Response(
        202, stream=_Broken(), headers={"content-type": "text/plain"}))
    response = client.post(f"/private_{TOKEN}/messages/?session_id=s1",
                           json={"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
    assert response.status_code == 202 and response.content == b""
