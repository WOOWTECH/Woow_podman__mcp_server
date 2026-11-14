"""JSON-RPC -32004 的文字依實際原因區分（#145 審查的非阻擋建議）。

#145 對所有「子行程在回應途中斷線」（httpx ``ReadError``／``RemoteProtocolError``／``WriteError``）都說「MCP 子行程停止了
（會自動重新啟動）」。但連線中斷不一定是子行程停了：子行程仍在跑、只是這條連線被它或中間的什麼關掉。誤導的說明會讓人
去查一個沒有當掉的子行程。現在閘道看子行程的實際狀態（轉發的那個世代已結束、被換掉、被擋下，或 process 已結束）：

- 子行程已停止／重啟：維持原本的說明（會自動重新啟動）；
- 子行程仍在執行：說明連線中斷、子行程仍在執行。

兩種都保留「操作可能已完成也可能沒有，請先確認再重試」，錯誤碼同樣是 -32004、HTTP 502（標頭之前）或補一則錯誤事件（之後）。
"""
from __future__ import annotations

import json
import logging
import os
import signal
import time

import httpx
import pytest

from loopback_servers import RecordHandler, ThreadedUvicorn, mcp_child_process, wait_until

TOKEN = "interrupted-cause-test-token-00000"  # 合成值
_ACCEPT = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
_INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                          "clientInfo": {"name": "interrupted-cause-test", "version": "0"}}}
CALL = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "x", "arguments": {}}}

STOPPED = "MCP server process stopped"
CONNECTION = "connection to the MCP server was interrupted"


def _sse_messages(body: str) -> list[dict]:
    out = []
    for event in body.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in event.split("\n") if line.startswith("data:"))
        if data:
            out.append(json.loads(data))
    return out


class _Broken(httpx.AsyncByteStream):
    """先吐 ``chunks``，然後像連線被關掉那樣斷線；斷線的當下執行 ``before_error``。"""

    def __init__(self, *chunks: bytes, before_error=None) -> None:
        self.chunks = chunks
        self.before_error = before_error

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.before_error is not None:
            self.before_error()
        raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")

    async def aclose(self):
        pass


@pytest.fixture
def mock_gateway(make_app, monkeypatch, synthetic_child):
    """子行程是 MockTransport；``synthetic_child`` 是通過身分檢查、仍在執行的合成子行程（可以改它的 returncode）。"""
    from mcp_admin_core import proxy

    def build(handler, **config):
        monkeypatch.setattr(proxy, "_get_client",
                            lambda timeout=86400: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        return make_app({"mcp_auth_token": TOKEN,
                         "mcp_server": {"command": "", "args": [], "port": 8123, "env": {}}, **config})

    return build, synthetic_child


def _stop(child) -> None:
    """子行程在這一刻結束（管理程序的世代也跟著失效，與正式的 _drain_logs 一樣）。"""
    child._process.returncode = -9
    child._invalidate_initialize_generation()


@pytest.mark.parametrize("error", [httpx.ReadError, httpx.RemoteProtocolError, httpx.WriteError])
def test_connection_error_with_the_child_still_running_says_so(mock_gateway, error):
    build, child = mock_gateway

    def handler(request):
        raise error("connection reset", request=request)

    client = build(handler)
    started = time.monotonic()
    response = client.post(f"/private_{TOKEN}/mcp", json=CALL)
    assert response.status_code == 502
    body = response.json()
    assert body["id"] == 7 and body["error"]["code"] == -32004
    message = body["error"]["message"]
    assert CONNECTION in message and "still running" in message and "子行程仍在執行" in message
    assert STOPPED not in message and "會自動重新啟動" not in message
    assert "may or may not have completed" in message and "可能已完成也可能沒有" in message
    assert time.monotonic() - started < 3  # 等管理程序確認子行程有沒有結束只有一小段（約 0.25 秒）
    assert child.is_running  # 沒有人把子行程標成停止


@pytest.mark.parametrize("how", ["exited", "generation_replaced"])
def test_child_that_stopped_during_the_request_keeps_the_established_message(mock_gateway, how):
    build, child = mock_gateway

    def handler(request):
        if how == "exited":
            _stop(child)
        else:  # 重啟：世代失效、換上新的 process
            child._invalidate_initialize_generation()
        raise httpx.ReadError("peer closed", request=request)

    client = build(handler)
    started = time.monotonic()
    response = client.post(f"/private_{TOKEN}/mcp", json=CALL)
    assert response.status_code == 502 and response.json()["id"] == 7
    message = response.json()["error"]["message"]
    assert STOPPED in message and "restarts automatically" in message and "會自動重新啟動" in message
    assert CONNECTION not in message
    assert "may or may not have completed" in message
    assert time.monotonic() - started < 2  # 已經看得出停止：不需要等


def test_manager_noticing_the_exit_a_moment_later_is_still_reported_as_stopped(mock_gateway, monkeypatch):
    """連線先斷、管理程序隔一會兒才發現子行程結束（process 的收屍與 pipe 的 EOF 都晚幾毫秒）：等一小段再判斷。"""
    import asyncio

    from mcp_admin_core import proxy

    monkeypatch.setattr(proxy, "CHILD_EXIT_GRACE_SECONDS", 3.0)  # 寬一點：慢的 CI 上 50 毫秒後的狀態改變也來得及被看到
    build, child = mock_gateway

    def handler(request):
        asyncio.get_running_loop().call_later(0.05, _stop, child)  # 連線斷了之後 50 毫秒，管理程序才看到結束
        raise httpx.ReadError("peer closed", request=request)

    client = build(handler)
    response = client.post(f"/private_{TOKEN}/mcp", json=CALL)
    message = response.json()["error"]["message"]
    assert STOPPED in message and CONNECTION not in message


def test_batch_and_streams_get_the_same_wording(mock_gateway):
    build, child = mock_gateway

    def handler(request):
        raise httpx.ReadError("connection reset", request=request)

    client = build(handler)
    batch = client.post(f"/private_{TOKEN}/mcp", json=[
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, {"jsonrpc": "2.0", "id": 2, "method": "ping"}])
    assert batch.status_code == 502 and [m["id"] for m in batch.json()] == [1, 2]
    assert all(CONNECTION in m["error"]["message"] for m in batch.json())
    stream = client.get(f"/private_{TOKEN}/mcp")
    assert stream.status_code == 502 and stream.json()["id"] is None
    assert CONNECTION in stream.json()["error"]["message"]


def test_mid_sse_disconnect_with_the_child_still_running(mock_gateway):
    build, child = mock_gateway
    progress = b'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress","params":{}}\n\n'
    client = build(lambda request: httpx.Response(
        200, stream=_Broken(progress), headers={"content-type": "text/event-stream"}))
    response = client.post(f"/private_{TOKEN}/mcp", json=CALL)
    assert response.status_code == 200
    errors = [m for m in _sse_messages(response.text) if m.get("id") == 7]
    assert len(errors) == 1 and errors[0]["error"]["code"] == -32004
    assert CONNECTION in errors[0]["error"]["message"] and STOPPED not in errors[0]["error"]["message"]


def test_mid_sse_disconnect_when_the_child_stopped(mock_gateway):
    build, child = mock_gateway
    progress = b'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress","params":{}}\n\n'
    client = build(lambda request: httpx.Response(
        200, stream=_Broken(progress, before_error=lambda: _stop(child)),
        headers={"content-type": "text/event-stream"}))
    response = client.post(f"/private_{TOKEN}/mcp", json=CALL)
    errors = [m for m in _sse_messages(response.text) if m.get("id") == 7]
    assert len(errors) == 1 and errors[0]["error"]["code"] == -32004
    assert STOPPED in errors[0]["error"]["message"] and CONNECTION not in errors[0]["error"]["message"]


@pytest.mark.parametrize("stopped", [False, True])
def test_buffered_tools_list_disconnect_follows_the_same_rule(mock_gateway, stopped):
    build, child = mock_gateway
    client = build(lambda request: httpx.Response(
        200, stream=_Broken(b'{"jsonrpc":"2.0","id":3,"result":{"too',
                            before_error=(lambda: _stop(child)) if stopped else None),
        headers={"content-type": "application/json"}), tools={"disabled": ["hidden"]})
    response = client.post(f"/private_{TOKEN}/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    assert response.status_code == 502 and response.json()["id"] == 3
    message = response.json()["error"]["message"]
    assert (STOPPED in message) is stopped and (CONNECTION in message) is not stopped


def test_log_line_names_the_cause_without_a_traceback_or_token(mock_gateway):
    build, child = mock_gateway

    def handler(request):
        raise httpx.ReadError("connection reset", request=request)

    client = build(handler)
    with RecordHandler("mcp_admin_core", "uvicorn.error") as logs:
        client.post(f"/private_{TOKEN}/mcp", json=CALL)
    warnings = [r for r in logs.records if r.name == "mcp_admin_core.proxy" and r.levelno == logging.WARNING]
    assert len(warnings) == 1 and warnings[0].exc_info is None
    text = warnings[0].getMessage()
    assert "POST /mcp" in text and "ReadError" in text and TOKEN not in text
    assert not [r for r in logs.records if r.levelno >= logging.ERROR]


# ---------------------------------------------------------------------------
# 真的子行程（另一個 process，MCP SDK）被 kill：說「停止」
# ---------------------------------------------------------------------------


class _PolledProcess:
    """像正式的 ``asyncio.subprocess.Process``：``returncode`` 讀的是 process 當下的狀態（Popen 要 poll 才會更新）。"""

    def __init__(self, popen) -> None:
        self._popen = popen
        self.pid = popen.pid

    @property
    def returncode(self):
        return self._popen.poll()

    def send_signal(self, sig):
        self._popen.send_signal(sig)


def test_real_child_killed_mid_request_is_reported_as_stopped(tmp_config_path, monkeypatch):
    pytest.importorskip("mcp.server.mcpserver")
    import threading

    import mcp_admin_core.config.store as store_mod
    from mcp_admin_core import create_app, proxy
    from managed_proxy_fixture import attach

    monkeypatch.setattr(proxy, "_client", None)
    with mcp_child_process() as (port, child):
        attach(monkeypatch, _PolledProcess(child))
        config = json.loads(json.dumps(store_mod._DEFAULT_CONFIG))
        config.update(mcp_auth_token=TOKEN, mcp_server={"command": "", "args": [], "port": port, "env": {}})
        tmp_config_path.write_text(json.dumps(config))
        monkeypatch.setenv("MCP_ADMIN_CONFIG", str(tmp_config_path))
        monkeypatch.setattr(store_mod, "_instance", None)
        with ThreadedUvicorn(create_app(title="interrupted cause")) as admin:
            url = f"http://127.0.0.1:{admin.port}/private_{TOKEN}/mcp"
            with httpx.Client(timeout=30) as client:
                initialized = client.post(url, json=_INITIALIZE, headers=_ACCEPT)
                assert initialized.status_code == 200
                headers = {**_ACCEPT, "mcp-session-id": initialized.headers["mcp-session-id"]}
                client.post(url, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers)
                result: dict = {}

                def call():
                    try:
                        result["response"] = client.post(url, headers=headers, json={
                            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
                            "params": {"name": "slow", "arguments": {"seconds": 10}}}, timeout=30)
                    except Exception as exc:  # noqa: BLE001 - 記下來給斷言
                        result["error"] = exc

                os.kill(child.pid, signal.SIGSTOP)  # 凍結（身分仍通過），送出請求，再 kill（與 R2c 相同）
                thread = threading.Thread(target=call)
                thread.start()
                wait_until(lambda: proxy._client is not None and proxy._client._transport.usage()[0] >= 1, timeout=20)
                time.sleep(0.5)
                os.kill(child.pid, signal.SIGKILL)
                thread.join(30)
        proxy._client = None
    assert "error" not in result, result.get("error")
    response = result["response"]
    assert response.status_code == 502
    message = response.json()["error"]["message"]
    assert STOPPED in message and CONNECTION not in message
