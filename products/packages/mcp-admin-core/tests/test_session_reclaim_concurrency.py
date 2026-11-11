"""閘道回收 session 的併發情境（第二輪對抗式審查 F1：名額在 DELETE 與重試之間被別的 initialize 搶走）。

假上游照 n8n-mcp 2.96.2 ``dist/http-server-single-session.js`` 的名額／429／DELETE／404 行為，handler 是 async，
請求可以真的重疊（asyncio）。``on_delete_freed`` 在 DELETE 釋放名額之後、回應之前執行，用來讓另一個 initialize
剛好插進「DELETE 與重試之間」。改寫自審查的 scripts/test_zz_review_adversarial.py。
"""
from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import pytest

from mcp_admin_core.request_limits import McpRequestLimits
from mcp_admin_core.session_reclaim import IDLE_FLOOR_SECONDS as FLOOR
from mcp_admin_core.session_reclaim import SessionReclaimer

pytest.importorskip("n8n_mcp_admin")
from n8n_mcp_admin import child_env  # noqa: E402

TOKEN = "secret-token"
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
HEADERS = {"accept": "application/json, text/event-stream"}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class AsyncFakeN8n:
    def __init__(self, cap: int = 2) -> None:
        self.cap = cap
        self.sessions: set[str] = set()
        self.log: list[str] = []
        self.on_delete_freed = None  # async callback：名額釋放之後、DELETE 回應之前
        self.stream_ends: dict[str, asyncio.Event] = {}  # GET 串流：session 被移除時結束（同上游 removeSession）
        self.call_release = asyncio.Event()  # tools/call 卡住，直到 set
        self.call_release.set()

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        sid = request.headers.get("mcp-session-id")
        if request.method == "DELETE":
            self.log.append("DELETE")
            if sid in self.sessions:
                self.sessions.discard(sid)
                if sid in self.stream_ends:
                    self.stream_ends[sid].set()
                if self.on_delete_freed is not None:
                    callback, self.on_delete_freed = self.on_delete_freed, None
                    await callback()
                return httpx.Response(204)
            return httpx.Response(404, json={"jsonrpc": "2.0", "error": {"code": -32001, "message": "Session not found"},
                                             "id": None})
        if request.method == "GET":
            if sid not in self.sessions:
                return httpx.Response(400, json={"error": "SSE transport uses /sse endpoint"})
            end = self.stream_ends.setdefault(sid, asyncio.Event())

            async def stream():
                yield b": keepalive\n\n"
                await end.wait()

            self.log.append("GET open")
            return httpx.Response(200, headers={"content-type": "text/event-stream", "mcp-session-id": sid},
                                  content=stream())
        body = json.loads(request.content or b"null")
        if isinstance(body, dict) and body.get("method") == "tools/call":
            await self.call_release.wait()
        if isinstance(body, dict) and body.get("method") == "initialize":
            await asyncio.sleep(0)
            if len(self.sessions) >= self.cap:
                self.log.append("INIT 429")
                return httpx.Response(429, json={"jsonrpc": "2.0", "error": {"code": -32000, "message": (
                    f"Session limit reached ({self.cap}). Please wait for existing sessions to expire.")},
                    "id": body.get("id")})
            new = uuid.uuid4().hex
            self.sessions.add(new)
            self.log.append("INIT 200")
            payload = {"jsonrpc": "2.0", "id": body.get("id"), "result": {"protocolVersion": "2025-06-18"}}
            return httpx.Response(200, headers={"mcp-session-id": new, "content-type": "text/event-stream"},
                                  content=b"event: message\ndata: " + json.dumps(payload).encode() + b"\n\n")
        if sid not in self.sessions:
            return httpx.Response(404, json={"jsonrpc": "2.0", "error": {
                "code": -32000, "message": "Session not found or expired"}, "id": body.get("id") if body else None})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body.get("id"), "result": {"tools": []}})


@pytest.fixture
def gw(make_app, monkeypatch):
    from mcp_admin_core import proxy

    upstream = AsyncFakeN8n()
    clock = Clock()
    monkeypatch.setattr(proxy, "_get_client",
                        lambda timeout=86400: httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    client = make_app({"mcp_auth_token": TOKEN, "proxy": {"timeout": 30, "bearer_token": "child-bearer"},
                       "connection": {"mcp_session_timeout": "900"}})
    client.app.state.mcp_request_limits = McpRequestLimits(4 * 1024 * 1024, 10_000)
    reclaimer = SessionReclaimer(is_session_limit=child_env.is_session_limit_reply,
                                 session_lifetime=child_env.upstream_session_lifetime_seconds,
                                 clock=clock, label="n8n")
    client.app.state.session_reclaimer = reclaimer
    return client.app, upstream, clock, reclaimer


def _ac(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw")


async def _init(ac):
    return await ac.post(f"/private_{TOKEN}/mcp", json=INIT, headers=HEADERS)


async def _open(ac, count, clock):
    sessions = []
    for _ in range(count):
        sessions.append((await _init(ac)).headers["mcp-session-id"])
        clock.advance(1)
    return sessions


async def test_two_concurrent_initializes_with_two_idle_sessions_both_get_a_slot(gw):
    app, upstream, clock, _ = gw
    async with _ac(app) as ac:
        old = await _open(ac, 2, clock)
        clock.advance(FLOOR + 20)
        replies = await asyncio.gather(_init(ac), _init(ac))
    assert [r.status_code for r in replies] == [200, 200]
    assert upstream.log.count("DELETE") == 2 and not set(old) & upstream.sessions


async def test_two_concurrent_initializes_with_one_idle_session_end_only_that_one(gw):
    app, upstream, clock, reclaimer = gw
    async with _ac(app) as ac:
        a, b = await _open(ac, 2, clock)
        finish_b = reclaimer.begin(b, None)  # 例如 B 有一個還沒回完的 tools/call
        clock.advance(FLOOR + 20)
        replies = await asyncio.gather(_init(ac), _init(ac))
        finish_b()
    assert sorted(r.status_code for r in replies) == [200, 429]
    assert b in upstream.sessions and a not in upstream.sessions
    assert upstream.log.count("DELETE") == 1


async def _with_thief(app, upstream, ac):
    """讓一個新的 initialize 剛好插進「DELETE 釋放名額之後、閘道重試之前」（它的第一次嘗試不經過回收鎖）。"""
    stolen = {}

    async def thief():
        stolen["reply"] = await _init(ac)

    async def freed():
        stolen["task"] = asyncio.ensure_future(thief())
        for _ in range(50):  # 等小偷的 initialize 真的拿到名額
            await asyncio.sleep(0.01)
            if "INIT 200" in upstream.log[-1:]:
                break

    upstream.on_delete_freed = freed
    reply = await _init(ac)
    await stolen["task"]
    return reply, stolen["reply"]


async def test_slot_stolen_between_delete_and_retry_reclaims_the_next_idle_session(gw):
    app, upstream, clock, _ = gw
    async with _ac(app) as ac:
        a, b = await _open(ac, 2, clock)
        clock.advance(FLOOR + 20)
        reply, thief = await _with_thief(app, upstream, ac)
    assert thief.status_code == 200
    assert reply.status_code == 200, reply.text  # 修正前：回收者結束了 A 卻仍拿到 429
    assert a not in upstream.sessions and b not in upstream.sessions
    assert upstream.log.count("DELETE") == 2


async def test_slot_stolen_and_nothing_else_idle_still_answers_429(gw):
    app, upstream, clock, reclaimer = gw
    async with _ac(app) as ac:
        a, b = await _open(ac, 2, clock)
        finish_b = reclaimer.begin(b, None)
        clock.advance(FLOOR + 20)
        reply, thief = await _with_thief(app, upstream, ac)
        finish_b()
    assert (reply.status_code, thief.status_code) == (429, 200)
    assert reply.json()["error"]["message"].startswith("Session limit reached (")
    assert upstream.log.count("DELETE") == 1 and b in upstream.sessions


async def _until(predicate, timeout=5.0):
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


async def test_an_open_get_stream_does_not_protect_an_idle_session(gw):
    """真的 Claude Code 連上後一直開著 GET 串流：串流不算使用中（同上游只看 POST），閒置超過下限照樣回收；
    回收時上游結束這個 session，串流跟著結束。"""
    app, upstream, clock, reclaimer = gw
    async with _ac(app) as ac:
        a, b = await _open(ac, 2, clock)
        stream_a = asyncio.ensure_future(ac.get(f"/private_{TOKEN}/mcp",
                                                headers={"accept": "text/event-stream", "mcp-session-id": a}))
        await _until(lambda: "GET open" in upstream.log)
        assert reclaimer.tracked()[a]["in_flight"] == 0  # 串流開著也不算進行中
        clock.advance(FLOOR + 20)
        await ac.post(f"/private_{TOKEN}/mcp", json={"jsonrpc": "2.0", "id": 5, "method": "tools/list"},
                      headers={**HEADERS, "mcp-session-id": b})  # B 剛用過
        reply = await _init(ac)
        streamed = await asyncio.wait_for(stream_a, timeout=5)
    assert reply.status_code == 200, reply.text
    assert a not in upstream.sessions and b in upstream.sessions  # 結束的是開著串流、但閒置最久的 A
    assert streamed.status_code == 200  # 串流隨上游移除 session 而結束（用戶端下一次 POST 會拿到 404）


async def test_a_tool_call_in_progress_still_protects_its_session(gw):
    """還沒回完的 POST（例如很久的 tools/call）照樣算使用中：不回收，改回收另一個閒置的。"""
    app, upstream, clock, reclaimer = gw
    async with _ac(app) as ac:
        a, b = await _open(ac, 2, clock)
        clock.advance(FLOOR + 20)
        upstream.call_release.clear()
        call_a = asyncio.ensure_future(ac.post(
            f"/private_{TOKEN}/mcp", json={"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                                           "params": {"name": "n8n_list_workflows", "arguments": {}}},
            headers={**HEADERS, "mcp-session-id": a}))
        await _until(lambda: reclaimer.tracked()[a]["in_flight"] == 1)
        clock.advance(FLOOR + 20)  # A 的呼叫還在跑：A、B 都閒置超過下限，但 A 在使用中
        reply = await _init(ac)
        upstream.call_release.set()
        await asyncio.wait_for(call_a, timeout=5)
    assert reply.status_code == 200, reply.text
    assert a in upstream.sessions and b not in upstream.sessions
