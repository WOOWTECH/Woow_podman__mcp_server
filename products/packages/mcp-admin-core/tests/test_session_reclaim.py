"""名額已滿時由閘道回收上游閒置的 MCP session（mcp_admin_core.session_reclaim＋proxy；n8n 的判斷式）。

假上游照 n8n-mcp 2.96.2 ``dist/http-server-single-session.js`` 的行為：initialize 在名額內回 200＋``mcp-session-id``，
名額滿時回 429＋-32000 ``Session limit reached (N)…``；``DELETE /mcp`` 帶 id 立刻釋放名額回 204，不存在回 404；
不認得的 id 回 404。真的上游（Node 26＋n8n-mcp 2.96.2）的端到端驗證見第二輪證據 F1-e2e-*.json。
"""
from __future__ import annotations

import json
import logging
import uuid
from types import SimpleNamespace

import httpx
import pytest

from mcp_admin_core.process import McpProcessManager
from managed_proxy_fixture import trust_listener
from mcp_admin_core.request_limits import McpRequestLimits
from mcp_admin_core.session_reclaim import IDLE_FLOOR_SECONDS as FLOOR
from mcp_admin_core.session_reclaim import SessionReclaimer, session_tag

pytest.importorskip("n8n_mcp_admin")
from n8n_mcp_admin import child_env  # noqa: E402

TOKEN = "secret-token"
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
HEADERS = {"accept": "application/json, text/event-stream"}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeN8nMcp:
    """上游 SingleSessionHTTPServer 的 session 行為（名額、429、DELETE 釋放、404）。"""

    def __init__(self, cap: int = 2) -> None:
        self.cap = cap
        self.sessions: set[str] = set()
        self.deletes: list[tuple[str | None, str | None]] = []  # (session id, authorization)
        self.initializes = 0
        self.free_on_delete = True
        self.limit_text = None  # 覆寫 429 的文字（例如上游的認證限流）
        self.delete_status = None  # 覆寫 DELETE 的回應（例如上游的認證限流 429、500）

    def __call__(self, request: httpx.Request) -> httpx.Response:
        sid = request.headers.get("mcp-session-id")
        if request.method == "DELETE":
            self.deletes.append((sid, request.headers.get("authorization")))
            if self.delete_status is not None:
                return httpx.Response(self.delete_status, json={"jsonrpc": "2.0", "error": {
                    "code": -32000, "message": "Too many authentication attempts"}, "id": None})
            if sid in self.sessions:
                if self.free_on_delete:
                    self.sessions.discard(sid)
                return httpx.Response(204)
            return httpx.Response(404, json={"jsonrpc": "2.0", "error": {"code": -32001, "message": "Session not found"},
                                             "id": None})
        body = json.loads(request.content or b"null")
        if isinstance(body, dict) and body.get("method") == "initialize":
            self.initializes += 1
            if self.limit_text is not None or len(self.sessions) >= self.cap:
                message = self.limit_text or (f"Session limit reached ({self.cap}). "
                                              "Please wait for existing sessions to expire.")
                return httpx.Response(429, json={"jsonrpc": "2.0", "error": {"code": -32000, "message": message},
                                                 "id": body.get("id")})
            new = uuid.uuid4().hex
            self.sessions.add(new)
            payload = {"jsonrpc": "2.0", "id": body.get("id"), "result": {"protocolVersion": "2025-06-18"}}
            return httpx.Response(200, headers={"mcp-session-id": new, "content-type": "text/event-stream"},
                                  content=b"event: message\ndata: " + json.dumps(payload).encode() + b"\n\n")
        if sid not in self.sessions:
            return httpx.Response(404, json={"jsonrpc": "2.0", "error": {
                "code": -32000, "message": "Session not found or expired"}, "id": body.get("id") if body else None})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body.get("id"), "result": {"tools": []}})


@pytest.fixture
def gateway(make_app, monkeypatch):
    """n8n 形狀的閘道（4 MiB 界線＋n8n 的「名額已滿」判斷式），上游是 FakeN8nMcp，時鐘可控。"""
    from mcp_admin_core import proxy

    # Exercise the fenced prototype; production defaults to 429 passthrough.
    monkeypatch.setattr(proxy, '_AUTOMATIC_RECLAIM_EXPERIMENTAL', True)
    upstream = FakeN8nMcp()
    clock = Clock()
    pm = McpProcessManager()
    pm._process = SimpleNamespace(pid=101, returncode=None)
    pm._running = True
    trust_listener(pm)  # blocker 3: synthetic stand-in for the OS listener check
    monkeypatch.setattr(proxy, 'get_process_manager', lambda: pm)
    monkeypatch.setattr(proxy, "_get_client",
                        lambda timeout=86400: httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    client = make_app({"mcp_auth_token": TOKEN, "proxy": {"timeout": 30, "bearer_token": "child-bearer"},
                       "connection": {"mcp_session_timeout": "900"}})
    client.app.state.mcp_request_limits = McpRequestLimits(4 * 1024 * 1024, 10_000)
    reclaimer = SessionReclaimer(is_session_limit=child_env.is_session_limit_reply,
                                 session_lifetime=child_env.upstream_session_lifetime_seconds,
                                 clock=clock, label="n8n")
    client.app.state.session_reclaimer = reclaimer
    return client, upstream, clock, reclaimer


def initialize(client, token=TOKEN):
    return client.post(f"/private_{token}/mcp", json=INIT, headers=HEADERS)


def use(client, sid):
    return client.post(f"/private_{TOKEN}/mcp", json=LIST, headers={**HEADERS, "mcp-session-id": sid})


def test_reclaims_least_recently_used_idle_session_then_retries(gateway, caplog):
    client, upstream, clock, reclaimer = gateway
    a = initialize(client).headers["mcp-session-id"]
    clock.advance(5)
    b = initialize(client).headers["mcp-session-id"]
    clock.advance(FLOOR + 1)
    caplog.set_level(logging.INFO, logger="mcp_admin_core.session_reclaim")
    reply = initialize(client)
    assert reply.status_code == 200, reply.text
    c = reply.headers["mcp-session-id"]
    assert upstream.deletes == [(a, "Bearer child-bearer")]  # 最久沒活動的 A；DELETE 帶 proxy 的 bearer
    assert upstream.initializes == 4  # A、B、C 的第一次（429）、重試
    assert use(client, a).status_code == 404  # 被回收的用戶端依 MCP 規範重新 initialize
    assert use(client, b).status_code == 200 and use(client, c).status_code == 200
    assert set(reclaimer.tracked()) == {b, c}
    lines = [r.getMessage() for r in caplog.records if r.name == "mcp_admin_core.session_reclaim"]
    assert any(session_tag(a) in line and f"閒置 {int(FLOOR) + 6} 秒" in line for line in lines), lines
    assert not any(a in line or TOKEN in line or "child-bearer" in line for line in lines)


def test_recently_used_sessions_are_not_reclaimed(gateway):
    client, upstream, clock, _ = gateway
    a = initialize(client).headers["mcp-session-id"]
    b = initialize(client).headers["mcp-session-id"]
    clock.advance(FLOOR - 1)
    assert use(client, a).status_code == 200
    reply = initialize(client)
    assert reply.status_code == 429
    assert reply.json()["error"] == {"code": -32000,
                                     "message": "Session limit reached (2). Please wait for existing sessions to expire."}
    assert upstream.deletes == [] and upstream.sessions == {a, b}


def test_activity_moves_a_session_to_the_back(gateway):
    client, upstream, clock, _ = gateway
    a = initialize(client).headers["mcp-session-id"]
    b = initialize(client).headers["mcp-session-id"]
    clock.advance(FLOOR + 1)
    assert use(client, a).status_code == 200  # A 剛用過
    clock.advance(FLOOR + 1)
    assert initialize(client).status_code == 200
    assert [sid for sid, _ in upstream.deletes] == [b]


def test_wrong_token_never_triggers_reclaim(gateway):
    client, upstream, clock, _ = gateway
    initialize(client)
    initialize(client)
    clock.advance(600)
    assert initialize(client, token="wrong-token").status_code == 403
    assert upstream.deletes == [] and upstream.initializes == 2


def test_other_429_is_passed_through_without_reclaim(gateway):
    client, upstream, clock, _ = gateway
    initialize(client)
    clock.advance(600)
    upstream.limit_text = "Too many authentication attempts"
    reply = initialize(client)
    assert reply.status_code == 429
    assert reply.json()["error"]["message"] == "Too many authentication attempts"
    assert upstream.deletes == []


def test_freed_slot_taken_by_someone_else_reclaims_the_next_idle_session(gateway):
    """重試又拿到「名額已滿」（剛釋放的名額被沒經過回收鎖的 initialize 先用掉）：再回收下一個閒置的，直到沒有可回收的。"""
    client, upstream, clock, _ = gateway
    a = initialize(client).headers["mcp-session-id"]
    clock.advance(1)
    b = initialize(client).headers["mcp-session-id"]
    clock.advance(FLOOR + 1)
    upstream.free_on_delete = False  # 模擬每次釋放的名額都被別人先拿走
    reply = initialize(client)
    assert reply.status_code == 429
    assert reply.json()["error"]["message"].startswith("Session limit reached (")
    assert [sid for sid, _ in upstream.deletes] == [a, b]  # 兩個閒置的都試過，之後沒有可回收的
    assert upstream.initializes == 5  # A、B、第一次、兩次重試


def test_one_initialize_sends_at_most_max_delete_attempts(gateway):
    client, upstream, clock, reclaimer = gateway
    upstream.cap = 8
    for _ in range(8):
        initialize(client)
        clock.advance(1)
    clock.advance(FLOOR + 1)
    upstream.free_on_delete = False
    assert initialize(client).status_code == 429
    assert len(upstream.deletes) == reclaimer.max_delete_attempts == 5


def test_reclaims_per_minute_are_capped(gateway, caplog):
    """不停重連的用戶端（每次都 initialize、從不 DELETE）一分鐘最多只能踢掉 10 個閒置的 session。"""
    client, upstream, clock, reclaimer = gateway
    upstream.cap = 14
    old = [initialize(client).headers["mcp-session-id"] for _ in range(14)]
    clock.advance(FLOOR + 1)
    caplog.set_level(logging.INFO, logger="mcp_admin_core.session_reclaim")
    statuses = []
    for _ in range(12):
        statuses.append(initialize(client).status_code)
        clock.advance(1)
    assert statuses == [200] * 10 + [429, 429]
    assert len(upstream.deletes) == 10 and set(sid for sid, _ in upstream.deletes) <= set(old)
    limited = [r for r in caplog.records if "上限" in r.getMessage()]
    assert len(limited) == 1, "一個時間窗只記一行"
    clock.advance(60)  # 時間窗過了：又可以回收
    assert initialize(client).status_code == 200
    assert len(upstream.deletes) == 11


def test_client_delete_and_upstream_404_forget_the_session(gateway):
    client, upstream, clock, reclaimer = gateway
    a = initialize(client).headers["mcp-session-id"]
    b = initialize(client).headers["mcp-session-id"]
    assert client.request("DELETE", f"/private_{TOKEN}/mcp", headers={**HEADERS, "mcp-session-id": a}).status_code == 204
    upstream.sessions.discard(b)  # 上游自己逾時清掉
    assert use(client, b).status_code == 404
    assert reclaimer.tracked() == {}


def test_unknown_session_ids_from_clients_are_not_tracked(gateway):
    client, upstream, clock, reclaimer = gateway
    assert use(client, "made-up-by-client").status_code == 404
    assert reclaimer.tracked() == {}


def test_child_restart_drops_previous_records(gateway, monkeypatch):
    from mcp_admin_core import proxy
    client, upstream, clock, reclaimer = gateway
    initialize(client)
    initialize(client)
    pm = proxy.get_process_manager()
    pm._invalidate_initialize_generation()
    pm._process = SimpleNamespace(pid=202, returncode=None)
    import asyncio
    pm._initialize_generation_changed = asyncio.Event()
    clock.advance(600)
    assert initialize(client).status_code == 429
    assert upstream.deletes == [] and reclaimer.tracked() == {}


# ---- SessionReclaimer 本身 ----------------------------------------------------------------------


def _reclaimer(clock, **kwargs):
    return SessionReclaimer(is_session_limit=child_env.is_session_limit_reply, clock=clock, **kwargs)


def test_in_flight_requests_and_streams_keep_a_session_in_use():
    clock = Clock()
    r = _reclaimer(clock)
    r.created("a", None)
    r.created("b", None)
    finish = r.begin("a", None)  # 例如一個還沒回完的 tools/call（GET 的 SSE 串流 proxy 不算，見併發測試）
    clock.advance(3600)
    assert [sid for sid, _ in r.idle_candidates(None)] == ["b"]
    finish()
    finish()  # 只生效一次
    assert r.tracked()["a"]["in_flight"] == 0
    clock.advance(FLOOR + 1)
    assert [sid for sid, _ in r.idle_candidates(None)] == ["b", "a"]


def test_records_past_upstream_lifetime_are_dropped_before_reclaim():
    clock = Clock()
    r = _reclaimer(clock)
    r.created("expired", None)
    clock.advance(1300)
    r.created("live", None)
    clock.advance(FLOOR + 100)
    # 逾時 900 秒（15 分鐘）＋清掃 300 秒：閒置超過 1200 秒的上游一定已經清掉
    lifetime = child_env.upstream_session_lifetime_seconds({"mcp_session_timeout": "900"})
    assert lifetime == 1200
    assert [sid for sid, _ in r.idle_candidates(None, lifetime)] == ["live"]
    assert set(r.tracked()) == {"live"}


async def test_reclaim_skips_sessions_the_upstream_already_removed():
    clock = Clock()
    r = _reclaimer(clock)
    for sid in ("gone-1", "gone-2", "live"):
        r.created(sid, None)
        clock.advance(1)
    clock.advance(FLOOR + 20)
    sent = []

    async def delete(sid):
        sent.append(sid)
        return 204 if sid == "live" else 404

    assert await r.reclaim(delete, None) is True
    assert sent == ["gone-1", "gone-2", "live"]
    assert r.tracked() == {}


async def test_reclaim_gives_up_after_bounded_attempts_and_on_refusal():
    clock = Clock()
    r = _reclaimer(clock, max_delete_attempts=2)
    for sid in ("x1", "x2", "x3"):
        r.created(sid, None)
    clock.advance(FLOOR + 20)
    sent = []

    async def missing(sid):
        sent.append(sid)
        return 404

    assert await r.reclaim(missing, None) is False
    assert len(sent) == 2

    r.created("y", None)
    clock.advance(FLOOR + 20)

    async def refused(sid):
        return 401

    assert await r.reclaim(refused, None) is False
    # 上游拒絕結束的 x3（最久沒活動、第一個被送 DELETE）還在、還占名額：紀錄要留著
    assert set(r.tracked()) == {"x3", "y"} and r.tracked()["x3"]["cooldown_seconds"] > 0


def test_tracking_table_is_bounded():
    clock = Clock()
    r = _reclaimer(clock, max_tracked=3)
    for index in range(5):
        r.created(f"s{index}", None)
        clock.advance(1)
    assert set(r.tracked()) == {"s2", "s3", "s4"}


def test_n8n_session_limit_reply_is_recognised_exactly():
    limit = json.dumps({"jsonrpc": "2.0", "error": {
        "code": -32000, "message": "Session limit reached (20). Please wait for existing sessions to expire."}, "id": 1})
    assert child_env.is_session_limit_reply(429, limit.encode())
    assert not child_env.is_session_limit_reply(503, limit.encode())
    auth = json.dumps({"jsonrpc": "2.0", "error": {"code": -32000, "message": "Too many authentication attempts"},
                       "id": None})
    assert not child_env.is_session_limit_reply(429, auth.encode())
    assert not child_env.is_session_limit_reply(429, b"<html>busy</html>")


# ---- DELETE 沒有釋放名額：保留紀錄、冷卻（第二輪審查 F1：DELETE 回 429 後紀錄被丟掉、上游仍占名額）--------


@pytest.mark.parametrize("refusal", [429, 401, 500])
def test_refused_delete_keeps_the_record_and_cools_down(gateway, refusal):
    client, upstream, clock, reclaimer = gateway
    a = initialize(client).headers["mcp-session-id"]
    clock.advance(5)
    b = initialize(client).headers["mcp-session-id"]
    clock.advance(FLOOR + 1)
    upstream.delete_status = refusal  # 例如上游的認證限流（127.0.0.1 共用額度）
    assert initialize(client).status_code == 429
    assert [sid for sid, _ in upstream.deletes] == [a]
    assert upstream.sessions == {a, b}  # 上游沒有結束 A：A 還占名額
    tracked = reclaimer.tracked()
    assert set(tracked) == {a, b} and tracked[a]["cooldown_seconds"] == pytest.approx(60)
    # 冷卻中：不再對 A 送 DELETE（B 也閒置了 61 秒，換它）
    upstream.delete_status = None
    assert initialize(client).status_code == 200
    assert [sid for sid, _ in upstream.deletes] == [a, b]
    assert a in upstream.sessions and a in reclaimer.tracked()


def test_cooled_down_session_is_reclaimed_later(gateway):
    client, upstream, clock, reclaimer = gateway
    a = initialize(client).headers["mcp-session-id"]
    clock.advance(1)
    b = initialize(client).headers["mcp-session-id"]
    assert use(client, b).status_code == 200  # B 一直在用
    clock.advance(FLOOR + 1)
    assert use(client, b).status_code == 200
    upstream.delete_status = 429
    assert initialize(client).status_code == 429
    upstream.delete_status = None
    clock.advance(30)
    assert initialize(client).status_code == 429, "A 還在冷卻、B 剛用過：沒有可回收的"
    clock.advance(31)
    reply = initialize(client)
    assert reply.status_code == 200, reply.text
    assert [sid for sid, _ in upstream.deletes] == [a, a]
    assert a not in upstream.sessions and a not in reclaimer.tracked()


async def test_delete_that_cannot_be_sent_keeps_the_record():
    clock = Clock()
    r = _reclaimer(clock)
    r.created("a", None)
    clock.advance(FLOOR + 20)

    async def broken(sid):
        raise httpx.ConnectError("child is restarting")

    assert await r.reclaim(broken, None) is False
    assert "a" in r.tracked() and r.idle_candidates(None) == []
    clock.advance(61)
    assert [sid for sid, _ in r.idle_candidates(None)] == ["a"]
