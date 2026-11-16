"""閘道請求內容位元組預算第二輪（審查 F4、F7、變異缺口 N1／N2／N4／N5；沙盒 P3）。

- P2-C：預算在啟動時建立，不合法的環境變數的 WARNING 在啟動時就進容器 stdout 與 Logs 頁（不等第一個請求）；
- P2-D：已登記預算、卻不送內容（或停在一半）的連線，連續一段時間沒有新位元組就放棄並歸還預算；持續上傳不受影響；
- P3-F：legacy SSE ``/messages/`` 被拒回 503（不是 200）、被拒時用戶端中斷、``_report`` 節流、chunked 超量時找回 id。
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import time

import httpx
import pytest

from loopback_servers import RecordHandler, wait_until
from test_gateway_request_budget import (  # noqa: F401  (gateway 是 fixture)
    MIB, PROFILE_B, TOKEN_A, TOKEN_B, _assert_busy, _budget_env, _client, _hold, _hold_n, _rpc, _clean, gateway,
)

pytestmark = pytest.mark.usefixtures("synthetic_child")


# ---------------------------------------------------------------------------
# P2-C：啟動時建立預算、WARNING 進 stdout
# ---------------------------------------------------------------------------


def test_budget_is_built_at_app_creation_and_invalid_env_warns_on_stdout_and_logs_page(monkeypatch, capsys, tmp_config_path):
    import mcp_admin_core.config.store as store_mod
    from mcp_admin_core import create_app, proxy, request_budget as rb

    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(tmp_config_path))
    monkeypatch.setattr(store_mod, "_instance", None)
    monkeypatch.setattr(proxy, "_budget", None, raising=False)
    monkeypatch.setenv(rb.TOTAL_ENV, "not-a-number-SECRET")
    monkeypatch.setenv(rb.PER_PROFILE_ENV, "1")
    with RecordHandler("mcp_admin_core") as logs:  # Logs 頁的 handler 掛在 mcp_admin_core，request_budget 的紀錄往上傳
        create_app(title="startup budget")
        assert proxy._budget is not None  # 還沒有任何請求，預算已經建好
        logged = [r.getMessage() for r in logs.records if r.levelno == logging.WARNING]
    out = capsys.readouterr().out
    for text in (" ".join(logged), out):
        assert rb.TOTAL_ENV in text and rb.PER_PROFILE_ENV in text
        assert "SECRET" not in text and "not-a-number" not in text  # 只有變數名、範圍與預設值，不回顯設定值
    assert "[mcp_admin_core.request_budget]" in out  # 容器 stdout（kubectl logs）看得到


def test_request_budget_logger_has_a_stdout_handler():
    from mcp_admin_core import request_budget as rb
    from mcp_admin_core.owner_defaults import _StdoutHandler

    assert any(isinstance(h, _StdoutHandler) for h in logging.getLogger(rb.__name__).handlers)


def test_invalid_idle_env_warns_without_echoing_the_value(monkeypatch):
    from mcp_admin_core import request_budget as rb

    for bad in ("0", "-3", "abc", "nan", "99999"):
        monkeypatch.setenv(rb.BODY_IDLE_ENV, bad)
        with RecordHandler("mcp_admin_core") as logs:
            assert rb.BudgetLimits.from_env().body_idle_seconds == rb.DEFAULT_BODY_IDLE_SECONDS
        text = " ".join(logs.messages())
        assert rb.BODY_IDLE_ENV in text
        if bad.isalpha():
            assert bad not in text  # 只有變數名、範圍與預設值，不回顯設定值
    monkeypatch.setenv(rb.BODY_IDLE_ENV, "7.5")
    assert rb.BudgetLimits.from_env().body_idle_seconds == 7.5


# ---------------------------------------------------------------------------
# P2-D：已登記預算、不送內容的連線
# ---------------------------------------------------------------------------


def _raw_post(gw, path: str, declared: int, token_path: str | None = None) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", gw.base.rsplit(":", 1)[1] and int(gw.base.rsplit(":", 1)[1])))
    head = (f"POST {path} HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nAccept: application/json, text/event-stream\r\n"
            f"Content-Length: {declared}\r\n\r\n")
    sock.sendall(head.encode())
    return sock


def test_idle_registered_connection_is_dropped_and_returns_the_budget(gateway, monkeypatch):
    _budget_env(monkeypatch, 16, 16)
    monkeypatch.setenv("MCP_GATEWAY_BODY_IDLE_SECONDS", "1")
    with gateway() as gw:
        sock = _raw_post(gw, gw.path(), 15 * MIB)  # 宣告 15 MiB，之後什麼都不送
        try:
            wait_until(lambda: gw.used() >= 15 * MIB, 5)  # 預先登記
            sock.sendall(b'{"jsonrpc":"2.0","id":1,')  # 送一點點之後停住
            wait_until(lambda: gw.used() == 0, 6)  # 1 秒沒有新位元組：放棄並歸還
            sock.settimeout(5)
            reply = sock.recv(65536)
            assert reply.startswith(b"HTTP/1.1 408") and b"-32003" in reply and b"retry-after: 5" in reply.lower()
        finally:
            sock.close()

        async def scenario():
            async with _client(gw) as client:  # 預算已空：同帳號的合法大請求馬上通過
                ok = await client.post(gw.path(), json=_rpc(2, "tools/call", pad=5 * MIB))
                assert ok.status_code == 200 and "result" in ok.json()
        asyncio.run(scenario())


def test_slow_but_steady_upload_is_not_cut_off(gateway, monkeypatch):
    _budget_env(monkeypatch, 32, 32)
    monkeypatch.setenv("MCP_GATEWAY_BODY_IDLE_SECONDS", "1")
    with gateway() as gw:
        body = json.dumps(_rpc(3, "tools/call", pad=2 * MIB)).encode()

        async def slow():
            for i in range(0, len(body), len(body) // 4 + 1):  # 4 段，每段間隔 0.6 秒（小於 1 秒的閒置上限，總時間超過它）
                yield body[i:i + len(body) // 4 + 1]
                await asyncio.sleep(0.6)

        async def scenario():
            async with _client(gw) as client:
                response = await client.post(gw.path(), content=slow(), headers={"content-type": "application/json"})
                assert response.status_code == 200 and "result" in response.json()
                await asyncio.to_thread(wait_until, lambda: gw.used() == 0)
        asyncio.run(scenario())


def test_small_requests_are_not_subject_to_the_idle_timeout_and_budget(gateway, monkeypatch):
    _budget_env(monkeypatch, 16, 16)
    monkeypatch.setenv("MCP_GATEWAY_BODY_IDLE_SECONDS", "1")
    with gateway() as gw:
        payload = json.dumps(_rpc(4)).encode()
        sock = _raw_post(gw, gw.path(), len(payload))
        try:
            time.sleep(1.8)  # 宣告的內容很小：沒有登記、也不套閒置逾時，晚一點送完照常處理
            assert gw.used() == 0
            sock.sendall(payload)
            sock.settimeout(5)
            assert sock.recv(65536).startswith(b"HTTP/1.1 200")
        finally:
            sock.close()


# ---------------------------------------------------------------------------
# P3-F：變異缺口
# ---------------------------------------------------------------------------


def test_legacy_sse_messages_refusal_is_503_not_200(gateway, monkeypatch):
    """N1：legacy SSE 的 ``/messages/`` POST 的回應走 GET 串流，POST 只看狀態碼，所以被預算拒絕時是 503（不是 200＋-32003）。"""
    _budget_env(monkeypatch, 16, 16)
    with gateway() as gw:
        async def scenario():
            async with _client(gw) as client:
                first = await _hold_n(client, gw, TOKEN_A, 1, 10)
                path = f"/private_{TOKEN_A}/messages/?session_id=abc"
                refused = await client.post(path, json=_hold(2, 10))
                assert refused.status_code == 503 and refused.headers["retry-after"] == "5"
                assert refused.json()["error"]["code"] == -32003
                streamable = await client.post(gw.path(), json=_hold(3, 10))
                assert streamable.status_code == 200  # 對照：Streamable HTTP 且認得 id 才是 200
                gw.children["default"].release_holds.set()
                await first
        asyncio.run(scenario())


class _FakeRequest:
    """``_refuse_over_budget`` 用到的最小 Request：方法、讀內容的 stream。"""

    def __init__(self, chunks=(), error: Exception | None = None, method: str = "POST") -> None:
        self.method = method
        self._chunks, self._error = list(chunks), error

    async def stream(self):
        for chunk in self._chunks:
            yield chunk
        if self._error is not None:
            raise self._error


def _exceeded(scope="global"):
    from mcp_admin_core.request_budget import BudgetExceeded

    return BudgetExceeded(scope, 16 * MIB)


def test_client_disconnect_while_draining_a_refused_request_is_not_an_error():
    """N2：被拒絕的請求在丟棄內容時用戶端中斷（``ClientDisconnect``）：回 499，不拋例外、不當成伺服器錯誤。"""
    from starlette.requests import ClientDisconnect
    from mcp_admin_core import proxy

    request = _FakeRequest([b"x" * 1000], error=ClientDisconnect())
    response = asyncio.run(proxy._refuse_over_budget(request, _exceeded(), declared=5000, legacy_sse=False, profile_id="default"))
    assert response.status_code == proxy.CLIENT_CLOSED_REQUEST


def test_refusal_report_is_throttled_per_profile_with_a_suppressed_count(monkeypatch):
    """N4：連續拒絕每個帳號每 60 秒只記一行 WARNING，之後的那一行帶 suppressed 數。"""
    from mcp_admin_core import gateway_pool, proxy

    monkeypatch.setattr(gateway_pool, "_reports", {})
    now = [1000.0]
    monkeypatch.setattr(gateway_pool.time, "monotonic", lambda: now[0])

    def refuse(profile):
        request = _FakeRequest([b'{"jsonrpc":"2.0","id":1,"method":"x"}'])
        return asyncio.run(proxy._refuse_over_budget(request, _exceeded(), declared=40, legacy_sse=False, profile_id=profile))

    def lines():
        return [m for m in logs.messages() if "請求內容位元組預算" in m]

    with RecordHandler("mcp_admin_core.gateway_pool") as logs:
        for _ in range(3):
            refuse("default")
        assert len(lines()) == 1 and "suppressed" not in lines()[0]
        refuse(PROFILE_B)  # 另一個帳號有自己的節流
        assert len(lines()) == 2
        now[0] += gateway_pool.REPORT_INTERVAL_SECONDS + 1
        refuse("default")
        assert len(lines()) == 3 and "suppressed=2" in lines()[2]


def test_chunked_request_refused_mid_stream_still_gets_its_id_back(gateway, monkeypatch):
    """N5：chunked（沒有 Content-Length）在讀到一半超過預算：開頭的 id 照樣找回來，回 200＋該 id 的 -32003（不是 id 為 null 的 503）。"""
    _budget_env(monkeypatch, 16, 16)
    with gateway() as gw:
        async def scenario():
            async with _client(gw) as client:
                first = await _hold_n(client, gw, TOKEN_A, 1, 10)

                async def chunks():
                    yield b'{"jsonrpc":"2.0","id":77,"method":"tools/call","params":{"padding":"' + b"x" * (MIB - 80)
                    for _ in range(8):
                        yield b"x" * MIB
                response = await client.post(gw.path(), content=chunks(), headers={"content-type": "application/json"})
                _assert_busy(response, 77)
                gw.children["default"].release_holds.set()
                await first
                await asyncio.to_thread(wait_until, lambda: gw.used() == 0)
        asyncio.run(scenario())
