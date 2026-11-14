"""DATA-OMCP-007-3：``tools/call`` 的閘道期限要涵蓋 SSE 回應本體，不能只等回應標頭。

Streamable HTTP 的 MCP 子行程（MCP SDK 的 FastMCP／MCPServer 預設 ``json_response=False``，正式的 odoo-mcp 也是）
收到 ``tools/call`` 後**立刻**送出 ``200 text/event-stream`` 標頭，工具結果之後才以 SSE 事件送達。
只對 ``client.send(..., stream=True)``（拿到標頭就返回）套 ``asyncio.wait_for`` 的期限因此永遠不會觸發，
慢工具照樣跑到 Cloudflare 的 120 秒上限、回 524。

這裡用**真的子行程**（``_streamable_slow_server.py``，MCPServer＋uvicorn）經過 proxy 驗證，
不用 stub 讓 ``send()`` 睡覺（那種測試在真實協定下抓不到這個缺陷）。
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("mcp.server.mcpserver")

_SERVER = Path(__file__).with_name("_streamable_slow_server.py")
_ACCEPT = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def mcp_child():
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(_SERVER), str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("test MCP server did not start")
                time.sleep(0.1)
        yield port, proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:  # 同步工具還卡著事件迴圈時 SIGTERM 不會馬上生效
            proc.kill()
            proc.wait(timeout=5)


def _sse_messages(body: str) -> list[dict]:
    out = []
    for event in body.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(ln[5:].lstrip() for ln in event.split("\n") if ln.startswith("data:"))
        if data:
            out.append(json.loads(data))
    return out


def _fresh_upstream_client(proxy_mod) -> None:
    """proxy 的共用 httpx client 綁在建立它的事件迴圈上；不以 context manager 用的 TestClient
    每個請求各開一個迴圈（正式環境只有一個），所以每次請求前丟掉舊的。"""
    proxy_mod._client = None


@pytest.fixture
def proxied(make_app, mcp_child, monkeypatch):
    """經 proxy 完成 MCP initialize 握手，回傳 (client, headers, proxy_mod)。"""
    import mcp_admin_core.proxy as proxy_mod
    from managed_proxy_fixture import attach

    port, proc = mcp_child
    attach(monkeypatch, proc)

    monkeypatch.setattr(proxy_mod, "_client", None)  # 測完還原，別把綁在已關閉迴圈的 client 留給別的測試
    c = make_app(
        {"mcp_auth_token": "secret", "mcp_server": {"command": "", "args": [], "port": port, "env": {}}}
    )
    _fresh_upstream_client(proxy_mod)
    r = c.post(
        "/private_secret/mcp",
        headers=_ACCEPT,
        json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "deadline-test", "version": "0"}},
        },
    )
    assert r.status_code == 200, r.text
    headers = dict(_ACCEPT)
    sid = r.headers.get("mcp-session-id")
    if sid:
        headers["mcp-session-id"] = sid
    _fresh_upstream_client(proxy_mod)
    r = c.post("/private_secret/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code in (200, 202), r.text
    return c, headers, proxy_mod


def _call(c, headers, tool: str, seconds: float, rpc_id: int = 7):
    import mcp_admin_core.proxy as proxy_mod

    _fresh_upstream_client(proxy_mod)
    t0 = time.monotonic()
    r = c.post(
        "/private_secret/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": rpc_id, "method": "tools/call",
              "params": {"name": tool, "arguments": {"seconds": seconds}}},
    )
    return r, time.monotonic() - t0


def test_child_really_streams_tool_results_as_sse(proxied):
    """前提：子行程對 tools/call 回 text/event-stream（標頭先到、結果後到）。"""
    c, headers, proxy_mod = proxied
    r, _ = _call(c, headers, "slow", 0.1)
    assert r.headers["content-type"].startswith("text/event-stream")


@pytest.mark.parametrize("tool", ["slow", "slow_blocking"])
def test_tool_deadline_covers_the_sse_body(proxied, monkeypatch, tool):
    """工具跑 8 秒、期限 1 秒 → proxy 在期限到時就回 JSON-RPC 錯誤並結束串流，不是等 8 秒拿到結果。"""
    c, headers, proxy_mod = proxied
    monkeypatch.setattr(proxy_mod, "TOOL_CALL_TIMEOUT", 1.0)
    r, elapsed = _call(c, headers, tool, 8)
    assert r.status_code == 200
    assert elapsed < 5, f"gateway deadline did not fire: response took {elapsed:.1f}s"
    msgs = _sse_messages(r.text)
    reply = [m for m in msgs if m.get("id") == 7]
    assert len(reply) == 1, msgs
    assert "result" not in reply[0]
    assert reply[0]["error"]["code"] == proxy_mod.TOOL_TIMEOUT_ERROR_CODE
    assert "datas" in reply[0]["error"]["message"]


def test_tool_finishing_before_deadline_passes_through_unchanged(proxied, monkeypatch):
    c, headers, proxy_mod = proxied
    monkeypatch.setattr(proxy_mod, "TOOL_CALL_TIMEOUT", 5.0)
    r, elapsed = _call(c, headers, "slow", 0.3)
    assert r.status_code == 200
    msgs = _sse_messages(r.text)
    reply = [m for m in msgs if m.get("id") == 7]
    assert len(reply) == 1, msgs
    assert "error" not in reply[0]
    assert reply[0]["result"]["content"][0]["text"] == "done after 0.3"


def test_deadline_disabled_with_zero(proxied, monkeypatch):
    """``MCP_TOOL_CALL_TIMEOUT=0`` 關閉期限：慢工具照常回結果。"""
    c, headers, proxy_mod = proxied
    monkeypatch.setattr(proxy_mod, "TOOL_CALL_TIMEOUT", 0.0)
    r, elapsed = _call(c, headers, "slow", 1.2)
    reply = [m for m in _sse_messages(r.text) if m.get("id") == 7]
    assert reply and reply[0]["result"]["content"][0]["text"] == "done after 1.2"
