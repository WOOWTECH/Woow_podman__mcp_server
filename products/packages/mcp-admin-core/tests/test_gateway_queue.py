"""閘道排隊（2026-11-13 補測 P2「用戶端放棄的寫入仍會被執行」＋ #145 審查的非阻擋建議）。

證據（mp-gap-n8n-pool-20261113 Q6）：卡住期間送出的 create，用戶端 25 秒逾時放棄；放開後 1～2 秒仍寫進 Odoo（3/3），
用戶端重試就會重複寫入。#145 把等名額限制在 10 秒內，但排隊中的請求仍沒有人看用戶端還在不在：用戶端在這 10 秒內關閉
連線（HTTP 層逾時、斷線）時，名額一空出來請求照樣送給子行程。

這個檔案的測試：

- 排隊期間監看用戶端斷線，斷線就丟掉、絕不送給子行程（端到端：真的 uvicorn＋socket＋假子行程；再加 transport 單元測試）；
- 排隊數有上限（每個佇列各自計數），超過立刻拒絕並記 WARNING；
- 沒有 id 的通知（與用戶端對伺服器請求的回應）另有保留名額，不會被一般請求的排隊擠到 503（MCP Python SDK 1.x 用戶端收到
  通知 POST 的 5xx 會結束整個 session、TS SDK 的 connect() 會失敗）；
- 啟動中的 initialize 在排隊時被自己的期限切斷，與一般名額滿相同：帶 id 的 -32003＋WARNING，不是「子行程還沒好」的 503。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from types import SimpleNamespace

import httpx
import pytest

from loopback_servers import RecordHandler, ThreadedUvicorn, fake_child_app, wait_until

TOKEN = "queue-test-default-profile-token-00000"  # 合成值，不是真的 token
POOL_ENV = ("MCP_GATEWAY_MAX_CONNECTIONS_PER_PROFILE", "MCP_GATEWAY_MAX_STREAMS_PER_PROFILE",
            "MCP_GATEWAY_POOL_TIMEOUT_SECONDS", "MCP_GATEWAY_MAX_QUEUE_PER_PROFILE")

# Blocker 3: these tests own their upstream; stand in for the OS listener check (conftest.py).
pytestmark = pytest.mark.usefixtures("synthetic_child")


@pytest.fixture(autouse=True)
def _default_limits(monkeypatch):
    from mcp_admin_core import gateway_pool

    for name in POOL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(gateway_pool, "_reports", {})  # 每 60 秒一行的紀錄狀態不跨測試


def _rpc(rpc_id, method="tools/list", **params):
    message = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
    if params:
        message["params"] = params
    return message


def _create(rpc_id):
    """一個會改資料的工具呼叫（「用戶端放棄的寫入」）。"""
    return _rpc(rpc_id, "tools/call", name="create_partner", arguments={"name": "SYNTHETIC-GHOST-WRITE"})


class _Gateway:
    def __init__(self, admin: ThreadedUvicorn, child) -> None:
        self.url = f"http://127.0.0.1:{admin.port}/private_{TOKEN}/mcp"
        self.child = child

    @staticmethod
    def usage(profile: str = "default") -> tuple[int, int]:
        from mcp_admin_core import proxy

        client = proxy._client
        return (0, 0) if client is None else client._transport.usage(profile)

    @staticmethod
    def waiting(profile: str = "default") -> int:
        from mcp_admin_core import proxy

        client = proxy._client
        slots = None if client is None else client._transport._slots.get(profile)
        return 0 if slots is None else slots.waiting


@pytest.fixture
def gateway(tmp_config_path, monkeypatch):
    """預設帳號一個、假子行程一個，真的 uvicorn（create_app 的完整 middleware，與正式站相同）。"""
    import mcp_admin_core.config.store as store_mod
    from mcp_admin_core import create_app, proxy

    monkeypatch.setattr(proxy, "_client", None)  # 共用 client 綁在 admin 的事件迴圈上；測完還原

    @contextlib.contextmanager
    def start():
        child_app, child = fake_child_app()
        with ThreadedUvicorn(child_app) as child_server:
            base = json.loads(json.dumps(store_mod._DEFAULT_CONFIG))
            tmp_config_path.write_text(json.dumps({
                **base, "mcp_auth_token": TOKEN,
                "mcp_server": {"command": "", "args": [], "port": child_server.port, "env": {}},
            }))
            monkeypatch.setenv("MCP_ADMIN_CONFIG", str(tmp_config_path))
            monkeypatch.setattr(store_mod, "_instance", None)
            with ThreadedUvicorn(create_app(title="gateway queue test")) as admin:
                yield _Gateway(admin, child)
        proxy._client = None

    return start


def _client(timeout: float = 30.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout),
                             limits=httpx.Limits(max_connections=200, max_keepalive_connections=200))


async def _fill_connections(client: httpx.AsyncClient, gw: _Gateway, count: int) -> list[asyncio.Task]:
    """送 *count* 個會被子行程佔住的請求，等到閘道真的把它們都轉發出去。"""
    holds = [asyncio.create_task(client.post(gw.url, json=_rpc(1000 + i, "hold"))) for i in range(count)]
    await asyncio.to_thread(wait_until, lambda: gw.child.held == count)
    return holds


# ---------------------------------------------------------------------------
# 一、排隊中用戶端放棄：絕不送給子行程（P2）
# ---------------------------------------------------------------------------


def test_request_abandoned_while_queued_never_reaches_the_child(gateway, monkeypatch):
    """Q6 的重現：名額被佔滿，送出一個寫入，用戶端在排隊時逾時放棄；名額空出來之後，子行程一次都不會收到那個寫入。

    對照：同一時間仍連著的用戶端的請求，名額空出後照常送給子行程（排隊本身沒有壞）。"""
    monkeypatch.setenv("MCP_GATEWAY_MAX_CONNECTIONS_PER_PROFILE", "2")
    monkeypatch.setenv("MCP_GATEWAY_MAX_STREAMS_PER_PROFILE", "1")
    monkeypatch.setenv("MCP_GATEWAY_POOL_TIMEOUT_SECONDS", "30")  # 比測試久：用戶端先放棄，不是閘道先逾時
    with gateway() as gw, RecordHandler("mcp_admin_core", "uvicorn.error") as logs:
        async def scenario():
            async with _client() as client:
                holds = await _fill_connections(client, gw, 2)
                patient = asyncio.create_task(client.post(gw.url, json=_create(88)))
                await asyncio.to_thread(wait_until, lambda: gw.waiting() == 1)
                with pytest.raises(httpx.TimeoutException):  # 用戶端排了 0.5 秒就放棄（與 Q6 的 25 秒逾時同一種情況）
                    await client.post(gw.url, json=_create(77), timeout=0.5)
                # 放棄的那個被閘道丟出排隊、不佔位置；耐心等的那個還在排
                await asyncio.to_thread(wait_until, lambda: gw.waiting() == 1, 3)
                assert 77 not in gw.child.received_ids()
                gw.child.release_holds.set()
                results = await asyncio.gather(*holds)
                patient_response = await asyncio.wait_for(patient, 10)
                await asyncio.to_thread(wait_until, lambda: gw.child.held == 0 and gw.usage() == (0, 0))
                return results, patient_response

        results, patient_response = asyncio.run(scenario())
        received = gw.child.received_ids()
    assert [r.status_code for r in results] == [200, 200]
    assert patient_response.status_code == 200 and patient_response.json()["id"] == 88
    assert 77 not in received, "用戶端已放棄的寫入不可以到達子行程"
    assert 88 in received
    assert not [r for r in logs.records if r.levelno >= logging.ERROR or r.exc_info]
    # 一行 WARNING（限流）：有請求在排隊時被用戶端放棄，不含 token
    notes = [r for r in logs.records if r.name == "mcp_admin_core.gateway_pool" and r.levelno == logging.WARNING]
    assert len(notes) == 1 and "profile=default" in notes[0].getMessage() and TOKEN not in notes[0].getMessage()


def test_client_leaving_the_queue_is_noticed_at_once_and_returns_nothing_it_did_not_take(gateway, monkeypatch):
    """用戶端一走，閘道就把它從排隊移除（不是等到 pool_timeout 才發現），連線名額與串流名額的記帳都不亂。"""
    monkeypatch.setenv("MCP_GATEWAY_MAX_CONNECTIONS_PER_PROFILE", "2")
    monkeypatch.setenv("MCP_GATEWAY_MAX_STREAMS_PER_PROFILE", "1")
    monkeypatch.setenv("MCP_GATEWAY_POOL_TIMEOUT_SECONDS", "60")
    with gateway() as gw:
        async def scenario():
            async with _client() as client:
                holds = await _fill_connections(client, gw, 2)
                leavers = [asyncio.create_task(client.post(gw.url, json=_create(200 + i), timeout=0.3))
                           for i in range(5)]
                stream = asyncio.create_task(client.get(gw.url, timeout=0.3))  # 排隊中的 GET 串流也一樣
                await asyncio.gather(*leavers, stream, return_exceptions=True)
                started = time.monotonic()
                await asyncio.to_thread(wait_until, lambda: gw.waiting() == 0, 30)
                gone_after = time.monotonic() - started
                connections_while_held = gw.usage()[0]
                gw.child.release_holds.set()
                await asyncio.gather(*holds)
                await asyncio.to_thread(wait_until, lambda: gw.usage() == (0, 0))
                after = await asyncio.wait_for(client.post(gw.url, json=_rpc(9)), 30)  # 名額都還在、沒有被吃掉（pool_timeout 是 60 秒，30 秒仍分得出來）
                return gone_after, connections_while_held, after

        gone_after, connections_while_held, after = asyncio.run(scenario())
        reached = [i for i in gw.child.received_ids() if isinstance(i, int) and 200 <= i < 205]
    assert reached == [], "放棄的請求不可以到達子行程"
    assert gone_after < 30 and connections_while_held == 2  # 小於 60 秒的 pool_timeout：不是等逾時才發現（上限放寬到 30 秒，慢的 CI 也不會誤判）
    assert after.status_code == 200 and after.json()["id"] == 9


# ---------------------------------------------------------------------------
# 二、每帳號排隊數上限
# ---------------------------------------------------------------------------


def test_queue_is_capped_per_profile_and_the_overflow_gets_a_clean_busy_answer_with_a_warning(gateway, monkeypatch):
    """排隊數沒有上限＝排隊時請求本體（最多 16 MiB 一筆）全在記憶體。超過上限的請求立刻回 -32003（有 id 時 HTTP 200）＋
    Retry-After，不等、不排隊，記一行 WARNING（每帳號每 60 秒一行）。"""
    monkeypatch.setenv("MCP_GATEWAY_MAX_CONNECTIONS_PER_PROFILE", "2")
    monkeypatch.setenv("MCP_GATEWAY_MAX_STREAMS_PER_PROFILE", "1")
    monkeypatch.setenv("MCP_GATEWAY_POOL_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("MCP_GATEWAY_MAX_QUEUE_PER_PROFILE", "3")
    with gateway() as gw, RecordHandler("mcp_admin_core", "uvicorn.error") as logs:
        async def scenario():
            async with _client() as client:
                holds = await _fill_connections(client, gw, 2)
                queued = [asyncio.create_task(client.post(gw.url, json=_rpc(300 + i))) for i in range(3)]
                await asyncio.to_thread(wait_until, lambda: gw.waiting() == 3)
                loop = asyncio.get_running_loop()
                started = loop.time()
                refused = [await asyncio.wait_for(client.post(gw.url, json=_create(400 + i)), 5) for i in range(2)]
                no_id = await asyncio.wait_for(client.get(gw.url), 5)  # 沒有 id（GET）：503＋Retry-After
                elapsed = loop.time() - started
                assert gw.waiting() == 3  # 被拒的沒有排進去
                gw.child.release_holds.set()
                served = await asyncio.gather(*queued)  # 已經排隊的照常被服務
                await asyncio.gather(*holds)
                await asyncio.to_thread(wait_until, lambda: gw.usage() == (0, 0) and gw.waiting() == 0)
                after = await asyncio.wait_for(client.post(gw.url, json=_rpc(9)), 5)
                return refused, no_id, elapsed, served, after

        refused, no_id, elapsed, served, after = asyncio.run(scenario())
        received = gw.child.received_ids()
    assert elapsed < 3, "超過上限要立刻拒絕，不是等 30 秒的 pool_timeout"
    for index, response in enumerate(refused):
        assert response.status_code == 200 and response.headers["retry-after"] == "5"
        body = response.json()
        assert body["id"] == 400 + index and body["error"]["code"] == -32003
        assert "3 requests" in body["error"]["message"] and "3 個請求" in body["error"]["message"]
        assert TOKEN not in response.text
    assert no_id.status_code == 503 and no_id.headers["retry-after"] == "5" and no_id.json()["id"] is None
    assert [r.status_code for r in served] == [200, 200, 200]
    assert after.status_code == 200
    assert not [i for i in received if i in (400, 401)], "被拒的請求不可以到達子行程"
    warnings = [r for r in logs.records if r.name == "mcp_admin_core.gateway_pool" and r.levelno == logging.WARNING]
    queue_warnings = [r for r in warnings if "MCP_GATEWAY_MAX_QUEUE_PER_PROFILE" in r.getMessage()]
    assert len(queue_warnings) == 1, [r.getMessage() for r in warnings]  # 限流：3 次拒絕只記一行
    message = queue_warnings[0].getMessage()
    assert "profile=default" in message and "3" in message and TOKEN not in message
    assert not [r for r in logs.records if r.levelno >= logging.ERROR or r.exc_info]


def test_zero_queue_means_no_waiting_at_all(gateway, monkeypatch):
    monkeypatch.setenv("MCP_GATEWAY_MAX_CONNECTIONS_PER_PROFILE", "2")
    monkeypatch.setenv("MCP_GATEWAY_MAX_STREAMS_PER_PROFILE", "1")
    monkeypatch.setenv("MCP_GATEWAY_POOL_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("MCP_GATEWAY_MAX_QUEUE_PER_PROFILE", "0")
    with gateway() as gw:
        async def scenario():
            async with _client() as client:
                holds = await _fill_connections(client, gw, 2)
                loop = asyncio.get_running_loop()
                started = loop.time()
                refused = await asyncio.wait_for(client.post(gw.url, json=_rpc(5)), 5)
                elapsed = loop.time() - started
                gw.child.release_holds.set()
                await asyncio.gather(*holds)
                return refused, elapsed

        refused, elapsed = asyncio.run(scenario())
    assert elapsed < 3 and refused.json()["error"]["code"] == -32003


def test_limits_from_environment_include_the_queue(monkeypatch, caplog):
    from mcp_admin_core import gateway_pool

    limits = gateway_pool.GatewayLimits.from_env()
    assert (limits.max_connections, limits.max_streams, limits.pool_timeout) == (64, 32, 10.0)  # #145 的預設值不變
    assert limits.max_queue == gateway_pool.DEFAULT_MAX_QUEUE == 32

    monkeypatch.setenv("MCP_GATEWAY_MAX_QUEUE_PER_PROFILE", "5")
    assert gateway_pool.GatewayLimits.from_env().max_queue == 5
    monkeypatch.setenv("MCP_GATEWAY_MAX_QUEUE_PER_PROFILE", "0")
    assert gateway_pool.GatewayLimits.from_env().max_queue == 0

    caplog.set_level(logging.WARNING, logger="mcp_admin_core.gateway_pool")
    for bad in ("-1", "x", "1.5", "99999"):
        monkeypatch.setenv("MCP_GATEWAY_MAX_QUEUE_PER_PROFILE", bad)
        assert gateway_pool.GatewayLimits.from_env().max_queue == 32
    messages = [r.getMessage() for r in caplog.records if r.name == "mcp_admin_core.gateway_pool"]
    assert len(messages) == 4 and all("MCP_GATEWAY_MAX_QUEUE_PER_PROFILE" in m for m in messages)
    assert not any(bad in m for m in messages for bad in ("99999", "1.5"))  # 不回顯設定的值


# ---------------------------------------------------------------------------
# 三、通知與回應：保留名額，不被一般請求的排隊擠成 503
# ---------------------------------------------------------------------------


def test_notifications_get_through_when_every_ordinary_slot_is_busy(gateway, monkeypatch):
    """沒有 id 的通知（notifications/initialized、cancelled…）與用戶端對伺服器請求的回應，各 SDK 對它們的 5xx 反應都很糟：
    MCP Python SDK 1.x 結束整個 session、TS SDK 的 connect() 失敗、Python SDK 2.x 靜默忽略（通知掉了）。把它們靜默丟掉
    （202 不轉發）更糟：cancelled 掉了，用戶端放棄的寫入照樣跑完；對伺服器請求的回應掉了，子行程那個請求一直等。
    所以一般名額滿時，通知走另外保留的名額，照樣送給子行程、回 202。"""
    monkeypatch.setenv("MCP_GATEWAY_MAX_CONNECTIONS_PER_PROFILE", "2")
    monkeypatch.setenv("MCP_GATEWAY_MAX_STREAMS_PER_PROFILE", "1")
    monkeypatch.setenv("MCP_GATEWAY_POOL_TIMEOUT_SECONDS", "0.5")
    with gateway() as gw:
        async def scenario():
            async with _client() as client:
                holds = await _fill_connections(client, gw, 2)
                ordinary = await asyncio.wait_for(client.post(gw.url, json=_rpc(9)), 5)  # 一般請求：照舊 -32003
                initialized = await asyncio.wait_for(client.post(
                    gw.url, json={"jsonrpc": "2.0", "method": "notifications/initialized"}), 5)
                cancelled = await asyncio.wait_for(client.post(
                    gw.url, json={"jsonrpc": "2.0", "method": "notifications/cancelled",
                                  "params": {"requestId": 1000}}), 5)
                reply = await asyncio.wait_for(client.post(
                    gw.url, json={"jsonrpc": "2.0", "id": "server-req-1", "result": {"roots": []}}), 5)
                batch = await asyncio.wait_for(client.post(gw.url, json=[
                    {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progressToken": "t"}},
                    {"jsonrpc": "2.0", "method": "notifications/roots/list_changed"}]), 5)
                # 事件，不是時間：四個通知都回了 202 的此刻，兩個一般名額還被佔著（它們沒有等一般名額），而通知已經到了子行程
                served_while_held = (gw.child.held, [m.get("method") for m in list(gw.child.received)])
                mixed = await asyncio.wait_for(client.post(gw.url, json=[  # 有一個請求：一般請求，名額滿就是 -32003
                    {"jsonrpc": "2.0", "method": "notifications/initialized"}, _rpc(10)]), 5)
                junk = await asyncio.wait_for(client.post(gw.url, json={}), 5)  # 不是通知：不能用保留名額
                gw.child.release_holds.set()
                await asyncio.gather(*holds)
                return ordinary, initialized, cancelled, reply, batch, served_while_held, mixed, junk

        ordinary, initialized, cancelled, reply, batch, served_while_held, mixed, junk = asyncio.run(scenario())
        received = gw.child.received
    assert ordinary.status_code == 200 and ordinary.json()["error"]["code"] == -32003
    for response in (initialized, cancelled, reply, batch):
        assert response.status_code == 202 and response.content == b""
    held_then, methods_then = served_while_held
    assert held_then == 2, "通知回 202 的時候，兩個一般名額應該還被佔著（通知走保留名額，沒有等一般名額）"
    assert {"notifications/initialized", "notifications/cancelled", "notifications/progress",
            "notifications/roots/list_changed"} <= set(methods_then), "通知在一般名額還被佔著的時候就該到達子行程"
    methods = [m.get("method") for m in received]
    assert "notifications/initialized" in methods and "notifications/cancelled" in methods
    assert any(m.get("id") == "server-req-1" for m in received)  # 回應也送到了
    assert methods.count("notifications/progress") == 1 and methods.count("notifications/roots/list_changed") == 1
    assert mixed.status_code == 200 and [m["id"] for m in mixed.json()] == [10]
    assert mixed.json()[0]["error"]["code"] == -32003
    assert junk.status_code in (200, 503) and "error" in junk.json()  # 擋在名額外面（逾時後的 -32003／503）
    assert junk.json()["error"]["code"] == -32003


def test_get_streams_never_use_the_notification_reserve(gateway, monkeypatch):
    """GET（串流）沒有 id，但不是通知：保留名額只給「POST 而且裡面沒有任何請求」的內容。"""
    monkeypatch.setenv("MCP_GATEWAY_MAX_CONNECTIONS_PER_PROFILE", "2")
    monkeypatch.setenv("MCP_GATEWAY_MAX_STREAMS_PER_PROFILE", "1")
    monkeypatch.setenv("MCP_GATEWAY_POOL_TIMEOUT_SECONDS", "0.5")
    with gateway() as gw:
        async def scenario():
            async with _client() as client:
                holds = await _fill_connections(client, gw, 2)
                stream = await asyncio.wait_for(client.get(gw.url), 5)
                gw.child.release_holds.set()
                await asyncio.gather(*holds)
                return stream

        stream = asyncio.run(scenario())
    assert stream.status_code == 503 and stream.headers["retry-after"] == "5"


# ---------------------------------------------------------------------------
# 四、ProfileLimitedTransport 單元測試
# ---------------------------------------------------------------------------


class _Profile:
    def __init__(self) -> None:
        self.current = "default"

    def __call__(self) -> str:
        return self.current


class _Body(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'{"ok": true}'


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, stream=_Body(), headers={"content-type": "application/json"})


def _transport(handler, *, connections=1, streams=1, timeout=5.0, queue=32, reserve=1):
    from mcp_admin_core import gateway_pool

    profile = _Profile()
    limits = gateway_pool.GatewayLimits(max_connections=connections, max_streams=streams, pool_timeout=timeout,
                                        max_queue=queue, notification_reserve=reserve)
    transport = gateway_pool.ProfileLimitedTransport(httpx.MockTransport(handler), limits, profile=profile)
    return transport, profile


def _with_hints(client: httpx.AsyncClient, method="POST", **hints) -> httpx.Request:
    from mcp_admin_core import gateway_pool

    request = client.build_request(method, "http://child/mcp", headers={"x-case": hints.pop("case", "")})
    gateway_pool.GatewayHints(**hints).attach(request)
    return request


async def test_queued_request_is_dropped_when_its_client_goes_away_and_is_never_sent():
    from mcp_admin_core.gateway_pool import ClientAbandoned

    sent: list[str] = []

    def handler(request):
        sent.append(request.headers["x-case"])
        return _ok(request)

    transport, _ = _transport(handler, connections=1, timeout=30)
    gone = asyncio.Event()
    async with httpx.AsyncClient(transport=transport) as client:
        held = await client.send(_with_hints(client, case="held"), stream=True)
        queued = asyncio.create_task(client.send(_with_hints(client, case="doomed", gone=gone.wait), stream=True))
        await asyncio.sleep(0.05)
        assert transport.waiting() == 1 and sent == ["held"]
        gone.set()
        with pytest.raises(ClientAbandoned):
            await asyncio.wait_for(queued, _PROMPT)
        assert transport.waiting() == 0 and transport.usage() == (1, 0)
        await held.aclose()
        await asyncio.sleep(0.05)
        assert sent == ["held"], "放棄的請求在名額空出後也沒有送出"
        assert transport.usage() == (0, 0) and not transport._slots
        assert (await client.post("http://child/mcp", headers={"x-case": "later"})).status_code == 200
        assert sent == ["held", "later"]  # 名額沒有被放棄的請求吃掉


async def test_slot_granted_in_the_same_instant_the_client_leaves_is_given_back_not_used():
    """名額剛好在用戶端離開的同一刻輪到它：不送出，名額還回去。

    另有一個請求（anchor）一直佔著另一個名額：帳號的記帳不會在中間歸零被整個丟掉重建（那會把漏掉的 semaphore token 一起
    丟掉，測試就看不出來）——漏掉一個名額，下面「立刻拿得到空位」的檢查就會排隊逾時。"""
    from mcp_admin_core.gateway_pool import ClientAbandoned

    sent: list[str] = []

    def handler(request):
        sent.append(request.headers["x-case"])
        return _ok(request)

    transport, _ = _transport(handler, connections=2, timeout=5)
    slots = _audited_slots(transport)  # 換成會記帳的 semaphore：「沒漏還」不夠，還要「沒還過頭」（審查 R3，變異 r03）
    gone = asyncio.Event()
    async with httpx.AsyncClient(transport=transport) as client:
        anchor = await client.send(_with_hints(client, case="anchor"), stream=True)
        held = await client.send(_with_hints(client, case="held"), stream=True)
        queued = asyncio.create_task(client.send(_with_hints(client, case="doomed", gone=gone.wait), stream=True))
        await asyncio.sleep(0.05)
        await held.aclose()  # 名額空出來（等待的人被叫醒）……
        gone.set()  # ……同一刻用戶端也走了
        with pytest.raises(ClientAbandoned):
            await asyncio.wait_for(queued, _PROMPT)
        await asyncio.sleep(0.05)
        await _settle()
        assert sent == ["anchor", "held"] and transport.usage() == (1, 0) and transport.waiting() == 0
        audit = slots.main.semaphore
        # 放棄的 acquire 拿到的許可只還一次：只剩 anchor 還借著一個，沒有還過頭（還過頭＝每帳號連線上限被悄悄放大）
        assert (audit.held, audit.over_released, audit.pending) == (1, 0, 0), "放棄的 acquire 的許可被還了不只一次"
        later = await asyncio.wait_for(client.send(_with_hints(client, case="later"), stream=True), _PROMPT)  # 立刻拿得到空位
        assert sent == ["anchor", "held", "later"] and transport.usage() == (2, 0)
        await later.aclose()
        await anchor.aclose()
        await _settle()
    assert transport.usage() == (0, 0) and not transport._slots
    assert (audit.held, audit.over_released, audit.pending) == (0, 0, 0) and not audit.locked()


async def test_cancelling_a_queued_request_returns_nothing_and_leaks_no_slot():
    transport, _ = _transport(_ok, connections=2, timeout=5)
    slots = _audited_slots(transport)  # 同上：也檢查沒有還過頭（審查 R3）
    gone = asyncio.Event()
    async with httpx.AsyncClient(transport=transport) as client:
        anchor = await client.send(_with_hints(client, case="anchor"), stream=True)  # 讓帳號的記帳一直存在（見上一個測試）
        held = await client.send(_with_hints(client, case="held"), stream=True)
        for _ in range(3):  # 反覆在不同時機取消：排隊中
            queued = asyncio.create_task(client.send(_with_hints(client, gone=gone.wait), stream=True))
            await asyncio.sleep(0.02)
            queued.cancel()
            with pytest.raises(asyncio.CancelledError):
                await queued
        assert transport.waiting() == 0 and transport.usage() == (2, 0)
        racing = asyncio.create_task(client.send(_with_hints(client, gone=gone.wait), stream=True))
        await asyncio.sleep(0.02)
        await held.aclose()  # 叫醒排隊的人，同一刻取消它：剛被叫醒的名額要還回去
        racing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await racing
        await asyncio.sleep(0.05)
        await _settle()
        assert transport.usage() == (1, 0) and transport.waiting() == 0
        audit = slots.main.semaphore
        assert (audit.held, audit.over_released, audit.pending) == (1, 0, 0), "剛被叫醒又被取消的名額被還了不只一次"
        later = await asyncio.wait_for(client.send(_with_hints(client), stream=True), _PROMPT)  # 立刻拿得到空位
        assert transport.usage() == (2, 0)
        await later.aclose()
        await anchor.aclose()
        await _settle()
    assert transport.usage() == (0, 0) and not transport._slots
    assert (audit.held, audit.over_released, audit.pending) == (0, 0, 0) and not audit.locked()


async def test_a_broken_disconnect_watcher_does_not_look_like_a_disconnect():
    """watcher 自己出錯（例如 ASGI receive 拋例外）不能被當成用戶端斷線，也不能讓排隊提早結束。"""
    transport, _ = _transport(_ok, connections=1, timeout=30)

    async def broken():
        raise RuntimeError("receive() went wrong")

    async with httpx.AsyncClient(transport=transport) as client:
        held = await client.send(_with_hints(client, case="held"), stream=True)
        queued = asyncio.create_task(client.send(_with_hints(client, gone=broken), stream=True))
        await asyncio.sleep(0.1)
        assert not queued.done() and transport.waiting() == 1
        await held.aclose()
        response = await asyncio.wait_for(queued, _PROMPT)
        assert response.status_code == 200
        await response.aclose()
    assert transport.usage() == (0, 0)


async def test_request_without_hints_behaves_exactly_as_before():
    from mcp_admin_core.gateway_pool import GatewayBusy

    transport, _ = _transport(_ok, connections=1, timeout=0.2)
    async with httpx.AsyncClient(transport=transport) as client:
        held = await client.send(client.build_request("POST", "http://child/mcp"), stream=True)
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(GatewayBusy):
            await client.post("http://child/mcp")
        assert 0.15 <= loop.time() - started < 2
        await held.aclose()
    assert not transport._slots


async def test_queue_cap_refuses_the_overflow_at_once_and_other_profiles_are_unaffected(caplog):
    from mcp_admin_core.gateway_pool import GatewayBusy, GatewayQueueFull

    caplog.set_level(logging.WARNING, logger="mcp_admin_core.gateway_pool")
    transport, profile = _transport(_ok, connections=1, timeout=30, queue=2)
    async with httpx.AsyncClient(transport=transport) as client:
        held = await client.send(client.build_request("POST", "http://child/mcp"), stream=True)
        waiters = [asyncio.create_task(client.post("http://child/mcp")) for _ in range(2)]
        await asyncio.sleep(0.05)
        assert transport.waiting() == 2
        loop = asyncio.get_running_loop()
        started = loop.time()
        for _ in range(3):
            with pytest.raises(GatewayQueueFull) as refused:
                await client.post("http://child/mcp")
        assert loop.time() - started < 1  # 不等
        assert isinstance(refused.value, GatewayBusy) and refused.value.limits.max_queue == 2
        assert transport.waiting() == 2
        profile.current = "c" * 24
        assert (await client.post("http://child/mcp")).status_code == 200  # 別的帳號有自己的名額與排隊
        profile.current = "default"
        await held.aclose()
        assert [(await w).status_code for w in waiters] == [200, 200]
        assert (await client.post("http://child/mcp")).status_code == 200  # 排隊空了，又能排
    messages = [r.getMessage() for r in caplog.records if r.name == "mcp_admin_core.gateway_pool"]
    assert len(messages) == 1 and "MCP_GATEWAY_MAX_QUEUE_PER_PROFILE" in messages[0]  # 3 次拒絕、一行 WARNING
    assert not transport._slots


async def test_notification_lane_is_separate_from_the_ordinary_slots():
    from mcp_admin_core.gateway_pool import GatewayBusy

    transport, _ = _transport(_ok, connections=1, timeout=0.3, reserve=1)
    async with httpx.AsyncClient(transport=transport) as client:
        held = await client.send(client.build_request("POST", "http://child/mcp"), stream=True)
        with pytest.raises(GatewayBusy):  # 一般請求：名額滿
            await client.post("http://child/mcp")
        # GET 不是通知：就算標了 notification 也不用保留名額（保留名額此時是空的：用了就會拿到）
        with pytest.raises(GatewayBusy):
            await client.send(_with_hints(client, method="GET", notification=True), stream=True)
        assert transport.usage() == (1, 0)
        note = await client.send(_with_hints(client, notification=True), stream=True)  # 通知：保留名額
        assert note.status_code == 200 and transport.usage() == (2, 0)
        started = asyncio.get_running_loop().time()
        with pytest.raises(GatewayBusy):  # 保留名額也用滿：照一般規則排隊、逾時
            await client.send(_with_hints(client, notification=True), stream=True)
        assert asyncio.get_running_loop().time() - started >= 0.25
        await note.aclose()
        await held.aclose()
    assert transport.usage() == (0, 0) and not transport._slots


@pytest.mark.parametrize("seed", range(int(os.environ.get("GATEWAY_STRESS_SEEDS", "6"))))
async def test_slot_accounting_survives_random_cancellation_disconnects_timeouts_and_overflow(seed):
    """壓力：許多請求同時搶少少的名額，各自在隨機的時間點被取消、用戶端斷線、逾時、被排隊上限拒絕或正常完成。全部結束之後
    沒有任何名額、排隊或串流的記帳殘留，而且兩組名額都還拿得到滿額（沒有漏掉的 semaphore）。
    預設跑 6 個種子；``GATEWAY_STRESS_SEEDS=300`` 可以拉高（改 gateway_pool 的取消／歸還邏輯時請跑一次，約 1 分鐘）。"""
    import random

    from mcp_admin_core.gateway_pool import ClientAbandoned, GatewayBusy

    rng = random.Random(seed)
    connections, reserve = 3, 2

    async def handler(request):
        await asyncio.sleep(rng.random() * 0.02)
        return _ok(request)

    transport, _ = _transport(handler, connections=connections, streams=2, timeout=0.05, queue=4, reserve=reserve)
    slots = _audited_slots(transport)  # 兩組名額都換成會記帳的 semaphore：結束後不只記帳歸零，許可也沒有還過頭或漏還（審查 R3）
    outcomes: list[str] = []

    async with httpx.AsyncClient(transport=transport) as client:
        async def one(index: int) -> None:
            gone = asyncio.Event()
            notification = rng.random() < 0.3
            method = "GET" if rng.random() < 0.2 and not notification else "POST"
            request = _with_hints(client, method=method, notification=notification,
                                  gone=gone.wait if rng.random() < 0.8 else None)
            work = asyncio.create_task(client.send(request, stream=True))
            action = rng.choice(["finish", "finish", "leave", "cancel"])
            await asyncio.sleep(rng.random() * 0.06)
            if action == "leave":
                gone.set()
            elif action == "cancel":
                work.cancel()
            try:
                response = await work
            except ClientAbandoned:
                outcomes.append("abandoned")
            except GatewayBusy:
                outcomes.append("busy")  # 排隊已滿或等名額逾時
            except asyncio.CancelledError:
                outcomes.append("cancelled")
            except httpx.TransportError:
                outcomes.append("stream_limit")
            else:
                await asyncio.sleep(rng.random() * 0.03)
                await response.aclose()
                outcomes.append("served")

        # anchor：整個過程一直佔著一個一般名額，帳號的記帳不會在中間歸零被丟掉重建（那會連漏掉的 semaphore token 一起丟掉）
        anchor = await client.send(client.build_request("POST", "http://child/mcp"), stream=True)
        await asyncio.gather(*(one(i) for i in range(120)))
        await asyncio.sleep(0.05)  # callbacks of abandoned acquire tasks
        assert transport.usage() == (1, 0) and transport.waiting() == 0, outcomes
        assert "served" in outcomes and {"abandoned", "busy"} & set(outcomes), set(outcomes)
        # 兩組名額都還拿得到剩下的滿額：沒有漏掉的 semaphore token
        held = [await asyncio.wait_for(client.send(client.build_request("POST", "http://child/mcp"), stream=True), _PROMPT)
                for _ in range(connections - 1)]
        notes = [await asyncio.wait_for(client.send(_with_hints(client, notification=True), stream=True), _PROMPT)
                 for _ in range(reserve)]
        assert transport.waiting() == 0 and transport.usage() == (connections + reserve, 0)
        for response in held + notes + [anchor]:
            await response.aclose()
        await _settle()
    assert transport.usage() == (0, 0) and not transport._slots
    for lane in (slots.main, slots.notify):
        audit = lane.semaphore
        assert (audit.held, audit.over_released, audit.pending) == (0, 0, 0), (seed, audit.held, audit.over_released)
        assert not audit.locked()


def test_shared_pool_has_room_for_every_profiles_slots_and_reserve():
    from mcp_admin_core import gateway_pool
    from mcp_admin_core.profiles import MAX_EXTRA_PROFILES

    limits = gateway_pool.GatewayLimits.from_env()
    per_profile = limits.max_connections + limits.notification_reserve
    assert limits.notification_reserve == gateway_pool.NOTIFICATION_RESERVE
    assert limits.shared_pool_size >= per_profile * (MAX_EXTRA_PROFILES + 1)


# ---------------------------------------------------------------------------
# 五、啟動中的 initialize 遇到名額滿：與一般名額滿相同（帶 id 的 -32003＋WARNING）
# ---------------------------------------------------------------------------

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "queue-test", "version": "1"}}}


@pytest.mark.parametrize("queue", [None, "0"])
async def test_initialize_waiting_for_a_slot_during_startup_gets_the_normal_busy_answer(make_app, monkeypatch, queue):
    """initialize 在子行程剛啟動時有自己的 10 秒期限（等探測＋送出）。它在排隊等名額時被這個期限切斷，以前回
    「503 MCP child is not ready」而且沒有任何 log；現在與一般名額滿一致：HTTP 200＋帶 id 的 -32003＋Retry-After＋WARNING
    （queue="0"：排隊上限 0 的情況，立刻拒絕，也是同一種回答）。"""
    from mcp_admin_core import gateway_pool, proxy
    from mcp_admin_core.process import McpProcessManager

    monkeypatch.setattr(gateway_pool, "_reports", {})
    monkeypatch.setattr(proxy, "INITIALIZE_READY_TIMEOUT", 5.0)  # 夠寬：慢的 CI（數十倍）也來得及先排進名額後面，再被這個期限切斷
    limits = gateway_pool.GatewayLimits(max_connections=2, max_streams=1, pool_timeout=30.0,
                                        max_queue=32 if queue is None else int(queue))
    release = asyncio.Event()
    entered = []

    async def upstream(request):
        entered.append(json.loads(request.content or b"{}").get("method"))
        if entered[-1] == "hold":
            await release.wait()
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

    client = httpx.AsyncClient(transport=gateway_pool.ProfileLimitedTransport(httpx.MockTransport(upstream), limits))
    monkeypatch.setattr(proxy, "_get_client", lambda *args: client)

    ready = asyncio.Event()
    pm = McpProcessManager()
    pm._process = SimpleNamespace(pid=4242, returncode=None)
    pm._running = True
    pm._attempted_revision = "synthetic-revision"
    pm._verifying_configuration = True
    pm._peer_verified = (pm._initialize_generation_changed, 59999)

    async def verify():
        await ready.wait()
        pm._applied_revision = pm._attempted_revision
        pm._verifying_configuration = False

    probe = pm._configuration_task = asyncio.create_task(verify())
    monkeypatch.setattr(proxy, "get_process_manager", lambda: pm)
    app = make_app({"mcp_auth_token": TOKEN, "mcp_server": {"command": "", "args": [], "port": 59999, "env": {}}}).app
    path = f"/private_{TOKEN}/mcp"
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            holds = [asyncio.create_task(c.post(path, json={"jsonrpc": "2.0", "id": 50 + i, "method": "hold"}))
                     for i in range(2)]
            for _ in range(200):
                if entered.count("hold") == 2:
                    break
                await asyncio.sleep(0.01)
            assert entered.count("hold") == 2
            with RecordHandler("mcp_admin_core") as logs:
                starting = asyncio.create_task(c.post(path, json=INIT))  # 探測還在跑：initialize 先等子行程就緒
                await asyncio.sleep(0.1)
                ready.set()  # 探測完成：initialize 往下送，在名額這裡排隊（或被排隊上限立刻拒絕）
                response = await asyncio.wait_for(starting, _PROMPT)
            release.set()
            await asyncio.gather(*holds)
    finally:
        ready.set()
        release.set()
        await probe
        await client.aclose()
    assert "initialize" not in entered, "沒有名額，initialize 不可以送給子行程"
    assert response.status_code == 200, response.text
    assert response.headers["retry-after"] == "5"
    body = response.json()
    assert body["id"] == 1 and body["error"]["code"] == -32003
    assert "not ready" not in response.text and TOKEN not in response.text
    warnings = [r for r in logs.records if r.name == "mcp_admin_core.gateway_pool" and r.levelno == logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in logs.records]
    assert "profile=default" in warnings[0].getMessage() and TOKEN not in warnings[0].getMessage()


# ---------------------------------------------------------------------------
# 六、名額記帳的補強測試（獨立審查 N2／N3／N4／N8／N9）
#
# 審查者對 gateway_pool 做了 57 個變異，其中這幾個以前沒有任何測試守著：排隊後拿到名額的請求提早還名額（gp16）、共用池算式
# 與保留名額數字（gp12、gp22）、排隊上限改成每帳號合計（gp23）、同一個回應關兩次（gp15）、逾時後不取消排隊的 acquire（gp17）、
# 排隊上限環境變數的上界（gp19）。下面每個測試都對應其中一個，並以該變異確認會紅。
# 這一節的測試不用固定的 sleep 或時間門檻：等「某件事發生」一律用 _until（有寬鬆上限的輪詢）或事件。
# ---------------------------------------------------------------------------


async def _until(predicate, *, timeout: float = 30.0) -> bool:
    """讓出事件迴圈，直到 *predicate* 成立；回傳在 *timeout*（寬鬆的上限，不是時間門檻）內有沒有成立。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            return False
        await asyncio.sleep(0.001)
    return True


async def _settle(iterations: int = 20) -> None:
    """讓事件迴圈把已排好的 callback（放棄的 acquire 的歸還、被取消的 task 的收尾）跑完；數的是迴圈的輪數，不是時間。"""
    for _ in range(iterations):
        await asyncio.sleep(0)


class _AuditedSemaphore(asyncio.Semaphore):
    """會記帳的 ``asyncio.Semaphore``，只在測試裡換進去（production 的 semaphore 維持 ``asyncio.Semaphore``，不用 BoundedSemaphore）。

    - ``held``：acquire 成功、還沒 release 的許可數；``over_released``：release 時 ``held`` 已經是 0，也就是還了沒借過的許可
      （名額膨脹。BoundedSemaphore 會在這裡拋 ValueError，但拋在 done callback 或 finally 裡只會被吞掉、或蓋掉原本的例外）；
    - ``pending``：還在等的 acquire 數（等名額逾時之後若不取消排隊的 acquire，它會一直留在這裡＝孤兒 waiter）。
    """

    def __init__(self, value: int) -> None:
        super().__init__(value)
        self.held = 0
        self.over_released = 0
        self.pending = 0

    async def acquire(self) -> bool:
        self.pending += 1
        try:
            await super().acquire()
        finally:
            self.pending -= 1
        self.held += 1
        return True

    def release(self) -> None:
        if self.held == 0:
            self.over_released += 1
        else:
            self.held -= 1
        super().release()


def _audited_slots(transport, profile_id: str = "default"):
    """第一個請求之前，把帳號的兩組名額換成會記帳的 semaphore（回傳 ``_Slots``：全部歸還、帳號的記帳被丟掉之後仍可檢查）。"""
    from mcp_admin_core import gateway_pool

    slots = gateway_pool._Slots(transport.limits)
    slots.main.semaphore = _AuditedSemaphore(transport.limits.max_connections)
    slots.notify.semaphore = _AuditedSemaphore(transport.limits.notification_reserve)
    transport._slots[profile_id] = slots
    return slots


#: 「這個請求應該馬上有結果」的等待上限：正常時是幾個事件迴圈的輪數；兩組名額被改成共用、排隊上限被拿掉之類的退步，
#: 請求會一直排隊，沒有這個上限就要等到 pool_timeout（這一節的測試設 300 秒）才失敗。
_PROMPT = 30.0


async def _fill_both_lanes(client: httpx.AsyncClient, connections: int, reserve: int) -> list[httpx.Response]:
    """一般名額與通知保留名額各自佔滿（回應都不關）。"""
    held = [await asyncio.wait_for(client.send(_with_hints(client), stream=True), _PROMPT) for _ in range(connections)]
    held += [await asyncio.wait_for(client.send(_with_hints(client, notification=True), stream=True), _PROMPT)
             for _ in range(reserve)]
    return held


@pytest.mark.parametrize("notification", [False, True], ids=["ordinary-lane", "notification-lane"])
async def test_a_request_that_waited_for_a_slot_keeps_holding_it_until_its_response_closes(notification):
    """N2（變異 gp16）：排隊後才拿到名額的請求必須真的佔著名額，直到它的回應關閉。

    被改壞（拿到名額就立刻把許可還回去）時，每個「排隊後才拿到」的請求都不佔名額，回應關閉時 release() 又還一次：許可數
    永久膨脹，每帳號連線上限形同虛設。只比對 usage()（連線計數）看不出來，要看 semaphore 本身：此時再來的請求必須排隊，
    全部歸還之後許可數正好回到上限、沒有還過頭。"""
    transport, _ = _transport(_ok, connections=1, timeout=300, reserve=1)
    slots = _audited_slots(transport)
    lane = slots.notify if notification else slots.main
    async with httpx.AsyncClient(transport=transport) as client:
        holder = await asyncio.wait_for(client.send(_with_hints(client, notification=notification), stream=True),
                                        _PROMPT)
        assert lane.semaphore.locked() and transport.waiting() == 0
        waiter = asyncio.create_task(client.send(_with_hints(client, notification=notification), stream=True))
        assert await _until(lambda: transport.waiting() == 1 or waiter.done())
        assert not waiter.done() and transport.waiting() == 1  # 名額被佔滿：排隊
        await holder.aclose()  # 名額空出來，排隊的那個拿到
        granted = await asyncio.wait_for(waiter, 30)
        await _settle()
        assert (transport.waiting(), lane.connections) == (0, 1)
        assert lane.semaphore.locked(), "排隊後拿到名額的請求沒有佔著名額（許可被提早還回去了）"
        newcomer = asyncio.create_task(client.send(_with_hints(client, notification=notification), stream=True))
        assert await _until(lambda: transport.waiting() == 1 or newcomer.done())
        assert not newcomer.done() and transport.waiting() == 1, "名額已被佔著，此時再來的請求必須排隊"
        await granted.aclose()  # 歸還：排隊的 newcomer 拿到
        last = await asyncio.wait_for(newcomer, 30)
        await last.aclose()
        await _settle()
    audit = lane.semaphore
    assert (audit.held, audit.over_released, audit.pending) == (0, 0, 0), "許可還過頭、漏還或有孤兒 waiter"
    assert not audit.locked(), "全部歸還之後許可數沒有回到上限"
    assert (lane.connections, transport.waiting(), transport.usage()) == (0, 0, (0, 0))


@pytest.mark.parametrize("connections", [2, 64, 1024])
def test_shared_pool_size_is_pinned_to_the_documented_formula(connections):
    """N3（變異 gp12、gp22）：共用池總上限＝（連線上限＋通知保留名額）×（額外帳號上限＋2），保留名額是 8。

    以前只斷言「放得下 6 個帳號」（``>= per_profile × (MAX_EXTRA_PROFILES + 1)``）：算式忘了加保留名額（448）或保留名額改成 9
    都照樣通過。文件（docs/mcp-gateway-limits.md、CLAUDE.md）寫的是 8 與 504；改這些數字時要連文件一起改。"""
    from mcp_admin_core import gateway_pool
    from mcp_admin_core.profiles import MAX_EXTRA_PROFILES

    assert gateway_pool.NOTIFICATION_RESERVE == 8
    limits = gateway_pool.GatewayLimits(max_connections=connections, max_streams=1)
    assert limits.notification_reserve == gateway_pool.NOTIFICATION_RESERVE
    assert limits.shared_pool_size == (connections + gateway_pool.NOTIFICATION_RESERVE) * (MAX_EXTRA_PROFILES + 2)
    if connections == 64:  # 預設值：文件寫的 504（MAX_EXTRA_PROFILES 是 5）
        assert (MAX_EXTRA_PROFILES, limits.shared_pool_size) == (5, 504)
    assert gateway_pool.GatewayLimits.from_env().shared_pool_size == 504


async def test_queue_limit_counts_each_lane_on_its_own():
    """N4（變異 gp23）：排隊上限是「每個 lane（一般／通知保留）各自」的上限，不是每帳號合計。

    合計計數會讓一般排隊已滿時，連通知也被拒絕，正好破壞保留通道的目的（通知要真的送到子行程，見 GatewayHints）。代價是每帳號
    最多 2 × max_queue 個請求在排隊，文件與 PR 說明已經寫明（排隊中的請求本體最壞 2 × 32 × 16 MiB）。"""
    from mcp_admin_core.gateway_pool import GatewayQueueFull

    transport, _ = _transport(_ok, connections=1, timeout=300, queue=2, reserve=1)
    async with httpx.AsyncClient(transport=transport) as client:
        held = await _fill_both_lanes(client, 1, 1)
        waiters: list[asyncio.Task] = []
        try:
            waiters += [asyncio.create_task(client.send(_with_hints(client), stream=True)) for _ in range(2)]
            assert await _until(lambda: transport.waiting() == 2 or any(w.done() for w in waiters))
            assert transport.waiting() == 2
            with pytest.raises(GatewayQueueFull):  # 一般 lane 的排隊已滿
                await asyncio.wait_for(client.send(_with_hints(client), stream=True), _PROMPT)
            # 通知有自己的排隊：不受一般 lane 已滿的影響
            notes = [asyncio.create_task(client.send(_with_hints(client, notification=True), stream=True)) for _ in range(2)]
            waiters += notes
            assert await _until(lambda: transport.waiting() == 4 or any(n.done() for n in notes))
            assert transport.waiting() == 4 and not any(n.done() for n in notes), "通知被一般 lane 的排隊數擠掉了"
            with pytest.raises(GatewayQueueFull):  # 保留 lane 自己的排隊也滿了
                await asyncio.wait_for(client.send(_with_hints(client, notification=True), stream=True), _PROMPT)
            assert transport.waiting() == 4  # 每帳號最多 2 × max_queue
        finally:
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
            for response in held:
                await response.aclose()
        await _settle()
    assert transport.usage() == (0, 0) and transport.waiting() == 0


@pytest.mark.parametrize("notification", [False, True], ids=["ordinary-lane", "notification-lane"])
async def test_busy_message_names_the_limit_of_the_lane_that_timed_out(notification, caplog):
    """N8：等名額逾時的說明（回應與 WARNING）寫「各 lane 自己的上限」。以前通知保留 lane 逾時也寫「All 64 gateway connections」，
    但保留 lane 只有 8 條。兩組刻意用不同的數字（一般 9、保留 3），寫錯了就看得出來。"""
    from mcp_admin_core import proxy
    from mcp_admin_core.gateway_pool import GatewayBusy

    caplog.set_level(logging.WARNING, logger="mcp_admin_core.gateway_pool")
    transport, _ = _transport(_ok, connections=9, timeout=0.05, reserve=3)  # timeout 只是讓逾時發生，沒有任何時間門檻
    async with httpx.AsyncClient(transport=transport) as client:
        held = await _fill_both_lanes(client, 9, 3)
        try:
            with pytest.raises(GatewayBusy) as busy:
                await client.send(_with_hints(client, notification=notification), stream=True)
        finally:
            for response in held:
                await response.aclose()
    message = proxy._busy_message(busy.value)
    warnings = [r.getMessage() for r in caplog.records if r.name == "mcp_admin_core.gateway_pool"]
    assert len(warnings) == 1 and "profile=default" in warnings[0]
    # 完整文字（不是只比對前半）：保留數是固定值、不能用環境變數調整，所以結尾寫「子行程可能卡住了」、不指向任何環境變數
    # （審查 R4，變異 r16 把結尾換成 MCP_GATEWAY_MAX_CONNECTIONS_PER_PROFILE）；一般 lane 才指向調整連線上限的環境變數
    assert warnings[0] == ("MCP 閘道 profile=default 這個帳號保留給通知的 3 條連線都在使用中，等了 0.05 秒仍沒有空出來，請求沒有送出"
                           "（子行程可能卡住了）" if notification else
                           "MCP 閘道 profile=default 這個帳號送往子行程的 9 條連線都在使用中，等了 0.05 秒仍沒有空出來，請求沒有送出"
                           "（MCP_GATEWAY_MAX_CONNECTIONS_PER_PROFILE）")
    if notification:
        assert "All 3 gateway connections reserved for notifications of this MCP URL are in use" in message
        assert "保留給通知、送往 MCP 子行程的 3 條連線都在使用中" in message
        assert "保留給通知的 3 條連線都在使用中" in warnings[0]
        assert "9" not in message and "9" not in warnings[0]
    else:
        assert "All 9 gateway connections of this MCP URL are in use" in message
        assert "送往 MCP 子行程的 9 條連線都在使用中" in message
        assert "reserved" not in message and "保留" not in message
        assert "9 條連線都在使用中" in warnings[0] and "保留" not in warnings[0]
        assert "3" not in message.replace("0.05", "")


@pytest.mark.parametrize("notification", [False, True], ids=["ordinary-lane", "notification-lane"])
async def test_queue_full_message_names_the_limit_of_the_lane_that_is_full(notification):
    """N8：排隊已滿的說明同樣寫各 lane 自己的連線數（一般 9、保留 3）。"""
    from mcp_admin_core import proxy
    from mcp_admin_core.gateway_pool import GatewayQueueFull

    transport, _ = _transport(_ok, connections=9, timeout=300, queue=2, reserve=3)
    async with httpx.AsyncClient(transport=transport) as client:
        held = await _fill_both_lanes(client, 9, 3)
        waiters = [asyncio.create_task(client.send(_with_hints(client, notification=notification), stream=True))
                   for _ in range(2)]
        try:
            assert await _until(lambda: transport.waiting() == 2 or any(w.done() for w in waiters))
            assert transport.waiting() == 2
            with pytest.raises(GatewayQueueFull) as full:
                await asyncio.wait_for(client.send(_with_hints(client, notification=notification), stream=True),
                                       _PROMPT)
        finally:
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
            for response in held:
                await response.aclose()
    message = proxy._busy_message(full.value)
    if notification:
        assert "2 requests of this MCP URL are already waiting for one of its 3 gateway connections reserved for " \
               "notifications" in message
        assert "已經有 2 個請求在排隊等 3 條保留給通知、送往 MCP 子行程的連線" in message
        assert "9" not in message
    else:
        assert "2 requests of this MCP URL are already waiting for one of its 9 gateway connections," in message
        assert "已經有 2 個請求在排隊等 9 條送往 MCP 子行程的連線" in message
        assert "reserved" not in message and "保留" not in message and "3" not in message


async def test_closing_the_same_response_twice_returns_the_slot_only_once():
    """N9（變異 gp15）：同一個回應的 stream 關兩次，名額只還一次。目前沒有呼叫者會關兩次，這是防禦：第二次若再還一次，
    會把別的請求（這裡的 anchor）佔著的名額也還掉。"""
    transport, _ = _transport(_ok, connections=2, timeout=300, reserve=1)
    slots = _audited_slots(transport)
    async with httpx.AsyncClient(transport=transport) as client:
        anchor = await client.send(_with_hints(client), stream=True)  # 另一個請求一直佔著一個名額：帳號的記帳不會歸零被丟掉
        doomed = await client.send(_with_hints(client), stream=True)
        assert transport.usage() == (2, 0)
        await doomed.stream.aclose()
        await doomed.stream.aclose()  # 第二次：什麼都不該發生
        assert transport.usage() == (1, 0), "同一個回應關兩次，名額被還了兩次"
        await anchor.aclose()
        await _settle()
    audit = slots.main.semaphore
    assert (audit.held, audit.over_released, audit.pending) == (0, 0, 0) and not audit.locked()


async def test_requests_that_timed_out_leave_no_orphan_waiter_on_the_semaphore():
    """N9（變異 gp17）：等名額逾時之後要取消排隊的 acquire。不取消的話它留在 semaphore 的等待佇列裡當孤兒：拿到許可後雖然會被
    _hand_back 還回去（所以不漏名額），但孤兒佔 FIFO 順位、不受 max_queue 管，名額被長時間佔滿時，孤兒數量隨逾時的請求線性成長。"""
    from mcp_admin_core.gateway_pool import GatewayBusy

    transport, _ = _transport(_ok, connections=1, timeout=0.05, reserve=1)  # timeout 只是讓逾時發生
    slots = _audited_slots(transport)
    async with httpx.AsyncClient(transport=transport) as client:
        holder = await client.send(_with_hints(client), stream=True)
        for _ in range(5):
            with pytest.raises(GatewayBusy):
                await client.send(_with_hints(client), stream=True)
        settled = await _until(lambda: slots.main.semaphore.pending == 0, timeout=10)
        assert settled, f"逾時的請求在 semaphore 的等待佇列留下 {slots.main.semaphore.pending} 個孤兒 waiter"
        assert transport.waiting() == 0
        await holder.aclose()
        await _settle()
    audit = slots.main.semaphore
    assert (audit.held, audit.over_released, audit.pending) == (0, 0, 0) and not audit.locked()


def test_queue_limit_environment_variable_accepts_its_ceiling_and_rejects_one_more(monkeypatch, caplog):
    """N9（變異 gp19）：``MCP_GATEWAY_MAX_QUEUE_PER_PROFILE`` 的上界是 1024：1024 合法、1025 退回預設並記一行 WARNING（不回顯值）。"""
    from mcp_admin_core import gateway_pool

    caplog.set_level(logging.WARNING, logger="mcp_admin_core.gateway_pool")
    monkeypatch.setenv("MCP_GATEWAY_MAX_QUEUE_PER_PROFILE", "1024")
    assert gateway_pool.GatewayLimits.from_env().max_queue == 1024
    assert not [r for r in caplog.records if r.name == "mcp_admin_core.gateway_pool"]  # 上界本身合法，不記 WARNING
    monkeypatch.setenv("MCP_GATEWAY_MAX_QUEUE_PER_PROFILE", "1025")
    assert gateway_pool.GatewayLimits.from_env().max_queue == gateway_pool.DEFAULT_MAX_QUEUE == 32
    messages = [r.getMessage() for r in caplog.records if r.name == "mcp_admin_core.gateway_pool"]
    assert len(messages) == 1 and "MCP_GATEWAY_MAX_QUEUE_PER_PROFILE" in messages[0] and "1025" not in messages[0]
