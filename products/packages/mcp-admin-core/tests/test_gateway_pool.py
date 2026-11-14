"""閘道到子行程的連線：每個帳號各自的連線／串流上限，等名額有期限（2026-11-13 多帳號實測 QA-1，P2）。

證據（mp-qa-20261113 receipts/q3-s2-pool-95-5.json）：帳號 A 用自己的 token 開了 100 條 GET 串流後，經閘道的請求全部
等到 25 秒逾時（A、B、預設帳號都是），直接打 B 的子行程卻正常、/healthz 一直 200；開 95 條時一切正常。原因是
proxy._get_client() 只有一個 httpx.AsyncClient（預設最多 100 條連線），而 Timeout(86400, connect=10) 讓等連線池也是 86400 秒。

端到端測試用真的 uvicorn（admin 與假子行程都在背景 thread）＋真的 socket，經過 create_app 的完整 middleware 與
ProfileContextMiddleware 選帳號，與正式站相同；單元測試直接測 ProfileLimitedTransport。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import Counter

import httpx
import pytest

from loopback_servers import RecordHandler, ThreadedUvicorn, fake_child_app, mcp_child_process, wait_until

PROFILE_B = "b0" * 12  # 24 個小寫十六進位字元（profiles._PROFILE_ID）
TOKEN_A = "pool-test-default-profile-token-000000"  # 合成值，不是真的 token
TOKEN_B = "pool-test-profile-b-token-111111111111"
POOL_ENV = ("MCP_GATEWAY_MAX_CONNECTIONS_PER_PROFILE", "MCP_GATEWAY_MAX_STREAMS_PER_PROFILE",
            "MCP_GATEWAY_POOL_TIMEOUT_SECONDS")

# Blocker 3: these tests own their upstream; stand in for the OS listener check (conftest.py).
pytestmark = pytest.mark.usefixtures("synthetic_child")


@pytest.fixture(autouse=True)
def _default_limits(monkeypatch):
    from mcp_admin_core import gateway_pool

    for name in POOL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(gateway_pool, "_reports", {})  # 每 60 秒一行的紀錄狀態不跨測試


def _rpc(rpc_id, method="tools/list"):
    return {"jsonrpc": "2.0", "id": rpc_id, "method": method}


class _Gateway:
    def __init__(self, admin: ThreadedUvicorn, children: dict) -> None:
        self.base = f"http://127.0.0.1:{admin.port}"
        self.children = children

    @staticmethod
    def path(token: str) -> str:
        return f"/private_{token}/mcp"

    @staticmethod
    def usage(profile: str = "default") -> tuple[int, int]:
        from mcp_admin_core import proxy

        client = proxy._client
        return (0, 0) if client is None else client._transport.usage(profile)


@pytest.fixture
def two_profile_gateway(tmp_config_path, monkeypatch):
    """預設帳號（TOKEN_A）＋額外帳號 B（TOKEN_B），各自一個假子行程；回傳 context manager factory。"""
    import mcp_admin_core.config.store as store_mod
    from mcp_admin_core import create_app, proxy

    monkeypatch.setattr(proxy, "_client", None)  # 共用 client 綁在 admin 的事件迴圈上；測完還原

    @contextlib.contextmanager
    def start():
        app_a, state_a = fake_child_app()
        app_b, state_b = fake_child_app()
        with ThreadedUvicorn(app_a) as child_a, ThreadedUvicorn(app_b) as child_b:
            base = json.loads(json.dumps(store_mod._DEFAULT_CONFIG))
            launch = {"command": "", "args": [], "env": {}}
            tmp_config_path.write_text(json.dumps({
                **base, "mcp_auth_token": TOKEN_A, "mcp_server": {**launch, "port": child_a.port},
                "profile_index": {PROFILE_B: {"label": "B", "port": child_b.port}},
            }))
            profile_dir = tmp_config_path.parent / "profiles" / PROFILE_B
            profile_dir.mkdir(parents=True)
            (profile_dir / "config.json").write_text(json.dumps({
                **base, "mcp_auth_token": TOKEN_B, "mcp_server": {**launch, "port": child_b.port},
            }))
            monkeypatch.setenv("MCP_ADMIN_CONFIG", str(tmp_config_path))
            monkeypatch.setattr(store_mod, "_instance", None)
            with ThreadedUvicorn(create_app(title="gateway pool test")) as admin:
                yield _Gateway(admin, {"default": state_a, PROFILE_B: state_b})
        proxy._client = None

    return start


def _client(gateway: _Gateway) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=gateway.base, timeout=httpx.Timeout(30.0),
                             limits=httpx.Limits(max_connections=400, max_keepalive_connections=400))


async def _open_streams(client: httpx.AsyncClient, token: str, count: int) -> list[httpx.Response]:
    async def one():
        return await client.send(client.build_request("GET", _Gateway.path(token)), stream=True)
    return list(await asyncio.gather(*(one() for _ in range(count))))


async def _close(responses) -> None:
    await asyncio.gather(*(response.aclose() for response in responses))


def test_one_profile_holding_100_streams_does_not_block_other_profiles(two_profile_gateway):
    """QA-1 重現：A 開 100 條 GET 串流。以前 B（以及 A 自己）之後的請求都卡到逾時；現在 A 超過 32 條的串流馬上拿到 429，
    B 與 A 的 POST 照常回應。"""
    with two_profile_gateway() as gateway:
        async def scenario():
            async with _client(gateway) as client:
                streams = await _open_streams(client, TOKEN_A, 100)
                try:
                    statuses = Counter(response.status_code for response in streams)
                    # 其他帳號、與 A 自己的 POST 都要在幾秒內回應（以前等到用戶端逾時）
                    b = await asyncio.wait_for(client.post(_Gateway.path(TOKEN_B), json=_rpc(1)), 5)
                    a = await asyncio.wait_for(client.post(_Gateway.path(TOKEN_A), json=_rpc(2)), 5)
                    refused = [response for response in streams if response.status_code == 429]
                    for response in refused:
                        await response.aread()
                    return statuses, a, b, refused
                finally:
                    await _close(streams)

        statuses, a, b, refused = asyncio.run(scenario())
        assert (b.status_code, b.json()["id"]) == (200, 1)
        assert (a.status_code, a.json()["id"]) == (200, 2)
        assert statuses == Counter({200: 32, 429: 68})
        body = refused[0].json()
        assert body["id"] is None and body["error"]["code"] == -32003
        assert "32 open event streams" in body["error"]["message"] and "32 條事件串流" in body["error"]["message"]
        assert refused[0].headers["retry-after"] == "30"
        assert TOKEN_A not in refused[0].text
        # 關掉串流後名額全部歸還
        wait_until(lambda: gateway.children["default"].open_streams == 0)
        wait_until(lambda: _Gateway.usage("default") == (0, 0))


def test_streams_spread_over_profiles_no_longer_exhaust_the_shared_pool(two_profile_gateway, monkeypatch):
    """QA-1 的另一半：同一台累計 100 條串流（A、B 各 50）。以前共用池滿了，兩個帳號之後的請求都卡住。"""
    monkeypatch.setenv("MCP_GATEWAY_MAX_STREAMS_PER_PROFILE", "60")
    with two_profile_gateway() as gateway:
        async def scenario():
            async with _client(gateway) as client:
                streams = (await _open_streams(client, TOKEN_A, 50)) + (await _open_streams(client, TOKEN_B, 50))
                try:
                    statuses = Counter(response.status_code for response in streams)
                    a = await asyncio.wait_for(client.post(_Gateway.path(TOKEN_A), json=_rpc(3)), 5)
                    b = await asyncio.wait_for(client.post(_Gateway.path(TOKEN_B), json=_rpc(4)), 5)
                    return statuses, a, b
                finally:
                    await _close(streams)

        statuses, a, b = asyncio.run(scenario())
        assert statuses == Counter({200: 100})
        assert (a.status_code, b.status_code) == (200, 200)


def test_full_profile_gets_a_clean_busy_error_and_others_are_unaffected(two_profile_gateway, monkeypatch):
    """A 的連線名額全被佔住：A 的新請求最多等 pool_timeout 秒，拿到帶 id 的 JSON-RPC 錯誤（或 503＋Retry-After），
    不是無限等；同一時間 B 照常。"""
    monkeypatch.setenv("MCP_GATEWAY_MAX_CONNECTIONS_PER_PROFILE", "4")
    monkeypatch.setenv("MCP_GATEWAY_MAX_STREAMS_PER_PROFILE", "2")
    monkeypatch.setenv("MCP_GATEWAY_POOL_TIMEOUT_SECONDS", "0.5")
    with two_profile_gateway() as gateway, RecordHandler("mcp_admin_core", "uvicorn.error") as logs:
        async def scenario():
            async with _client(gateway) as client:
                holds = [asyncio.create_task(client.post(_Gateway.path(TOKEN_A), json=_rpc(100 + i, "hold")))
                         for i in range(4)]
                await asyncio.to_thread(wait_until, lambda: gateway.children["default"].held == 4)
                loop = asyncio.get_running_loop()
                started = loop.time()
                busy = await asyncio.wait_for(client.post(_Gateway.path(TOKEN_A), json=_rpc(9)), 5)
                waited = loop.time() - started
                notification = await client.post(_Gateway.path(TOKEN_A),
                                                 json={"jsonrpc": "2.0", "method": "notifications/initialized"})
                stream = await client.get(_Gateway.path(TOKEN_A))
                b = await asyncio.wait_for(client.post(_Gateway.path(TOKEN_B), json=_rpc(10)), 5)
                gateway.children["default"].release_holds.set()
                assert [r.status_code for r in await asyncio.gather(*holds)] == [200] * 4
                await asyncio.to_thread(wait_until, lambda: gateway.children["default"].held == 0)
                await asyncio.to_thread(wait_until, lambda: _Gateway.usage("default") == (0, 0))
                after = await asyncio.wait_for(client.post(_Gateway.path(TOKEN_A), json=_rpc(11)), 5)
                return busy, waited, notification, stream, b, after

        busy, waited, notification, stream, b, after = asyncio.run(scenario())
    assert busy.status_code == 200 and busy.headers["retry-after"] == "5"
    error = busy.json()
    assert error["id"] == 9 and error["error"]["code"] == -32003
    assert "4 gateway connections" in error["error"]["message"] and "4 條連線" in error["error"]["message"]
    assert 0.4 <= waited < 3
    # 沒有 id 可帶的 GET 串流：503＋Retry-After
    assert stream.status_code == 503 and stream.headers["retry-after"] == "5"
    # 通知（沒有 id 的 POST）：2026-11-13 起走另外保留的名額，照樣送到子行程、回 202，不是 503——MCP Python SDK 1.x 對通知 POST 的
    # 5xx 會結束整個 session（#145 審查建議；見 test_gateway_queue.py）
    assert notification.status_code == 202 and notification.content == b""
    assert (b.status_code, b.json()["id"]) == (200, 10)
    assert (after.status_code, after.json()["id"]) == (200, 11)
    # 一行 WARNING（不含 token、每個帳號每 60 秒最多一行），沒有 ERROR／traceback
    warnings = [r for r in logs.records if r.name == "mcp_admin_core.gateway_pool"]
    assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING
    assert "profile=default" in warnings[0].getMessage() and TOKEN_A not in warnings[0].getMessage()
    assert not [r for r in logs.records if r.levelno >= logging.ERROR or r.exc_info]


def test_real_mcp_client_keeps_one_stream_and_is_unaffected(tmp_config_path, monkeypatch):
    """真的 MCP 用戶端（MCP Python SDK 2.x，與 Claude Code 一樣在 initialized 之後一直開著一條 GET 串流）經閘道連到
    真的 MCP 子行程：預設上限下只用掉 1 條串流，工具呼叫（含並行）照常；把串流上限設成 1 時，第二個用戶端的 GET 拿到
    429，但它的請求（POST）照常完成。"""
    pytest.importorskip("mcp.server.mcpserver")
    import anyio
    import mcp_admin_core.config.store as store_mod
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp_admin_core import create_app, proxy
    from managed_proxy_fixture import attach

    monkeypatch.setattr(proxy, "_client", None)
    with mcp_child_process() as (port, child):
        attach(monkeypatch, child)
        config = json.loads(json.dumps(store_mod._DEFAULT_CONFIG))
        config.update(mcp_auth_token=TOKEN_A, mcp_server={"command": "", "args": [], "port": port, "env": {}})
        tmp_config_path.write_text(json.dumps(config))
        monkeypatch.setenv("MCP_ADMIN_CONFIG", str(tmp_config_path))
        monkeypatch.setattr(store_mod, "_instance", None)

        async def session_calls(url: str, calls: int):
            with anyio.fail_after(60):
                async with streamable_http_client(url) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        await anyio.to_thread.run_sync(lambda: wait_until(lambda: _Gateway.usage()[1] >= 1))
                        names = [tool.name for tool in (await session.list_tools()).tools]
                        results = []

                        async def call(i):
                            results.append(await session.call_tool("slow", {"seconds": 0.05}))

                        async with anyio.create_task_group() as tg:
                            for i in range(calls):
                                tg.start_soon(call, i)
                        return names, results, _Gateway.usage()

        with ThreadedUvicorn(create_app(title="real client")) as admin:
            url = f"http://127.0.0.1:{admin.port}/private_{TOKEN_A}/mcp"
            names, results, usage = anyio.run(session_calls, url, 8)
            assert "slow" in names
            assert len(results) == 8 and all(not r.is_error for r in results)
            assert usage[1] == 1  # 只有那一條 GET 串流
            wait_until(lambda: _Gateway.usage() == (0, 0))

        proxy._client = None
        monkeypatch.setenv("MCP_GATEWAY_MAX_STREAMS_PER_PROFILE", "1")
        with ThreadedUvicorn(create_app(title="real client, one stream")) as admin:
            url = f"http://127.0.0.1:{admin.port}/private_{TOKEN_A}/mcp"

            async def two_clients():
                with anyio.fail_after(60):
                    async with streamable_http_client(url) as (read, write):
                        async with ClientSession(read, write) as first:
                            await first.initialize()
                            await anyio.to_thread.run_sync(lambda: wait_until(lambda: _Gateway.usage()[1] == 1))
                            async with httpx.AsyncClient() as raw:
                                refused = await raw.get(url)
                            # 第二個用戶端的 GET 被拒（429），但請求照常
                            names, results, _ = await session_calls(url, 2)
                            first_result = await first.call_tool("slow", {"seconds": 0.01})
                            return refused, names, results, first_result

            refused, names, results, first_result = anyio.run(two_clients)
            assert refused.status_code == 429
            assert len(results) == 2 and all(not r.is_error for r in results)
            assert not first_result.is_error
        proxy._client = None


@pytest.mark.parametrize(("path", "status"), [("mcp", 200), ("messages/?session_id=s1", 503)])
def test_busy_answer_keeps_streamable_sessions_and_legacy_sse_status(make_app, monkeypatch, path, status):
    """等名額逾時：Streamable HTTP 有 id 時回 200＋帶 id 的錯誤（session 不中斷）；legacy SSE 的 /messages/ 回應走 GET
    串流、POST 只看狀態碼，所以照樣 503（與請求內容上限 request_body 的做法相同）。"""
    from mcp_admin_core import gateway_pool, proxy

    limits = gateway_pool.GatewayLimits(4, 2, 0.5)

    class _Busy:
        def build_request(self, *, method, url, headers, content):
            return httpx.Request(method, url)

        async def send(self, request, *, stream):
            raise gateway_pool.GatewayBusy(limits, request=request)

    monkeypatch.setattr(proxy, "_get_client", lambda timeout=86400: _Busy())
    client = make_app({"mcp_auth_token": TOKEN_A, "mcp_server": {"command": "", "args": [], "port": 8123, "env": {}}})
    response = client.post(f"/private_{TOKEN_A}/{path}", json=_rpc(5))
    assert response.status_code == status and response.headers["retry-after"] == "5"
    assert response.json()["id"] == 5 and response.json()["error"]["code"] == -32003


# ---------------------------------------------------------------------------
# ProfileLimitedTransport 單元測試
# ---------------------------------------------------------------------------


class _Profile:
    def __init__(self) -> None:
        self.current = "default"

    def __call__(self) -> str:
        return self.current


def _transport(handler, *, connections=2, streams=1, timeout=0.2):
    from mcp_admin_core import gateway_pool

    profile = _Profile()
    limits = gateway_pool.GatewayLimits(max_connections=connections, max_streams=streams, pool_timeout=timeout)
    transport = gateway_pool.ProfileLimitedTransport(httpx.MockTransport(handler), limits, profile=profile)
    return transport, profile


class _Body(httpx.AsyncByteStream):
    """串流的回應內容（與真的 AsyncHTTPTransport 一樣，關閉之前一直佔著連線）。"""

    async def __aiter__(self):
        yield b'{"ok": true}'


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, stream=_Body(), headers={"content-type": "application/json"})


async def test_already_read_response_returns_its_slot_at_once():
    """內容已經整個讀完的回應（例如 MockTransport 的 json=…）沒有佔著連線：名額馬上歸還。"""
    transport, _ = _transport(lambda request: httpx.Response(200, json={"ok": True}), connections=1, timeout=0.2)
    async with httpx.AsyncClient(transport=transport) as client:
        first = await client.send(client.build_request("POST", "http://child/mcp"), stream=True)
        assert transport.usage("default") == (0, 0)
        assert (await client.post("http://child/mcp")).status_code == 200
        await first.aclose()
    assert not transport._slots


async def test_full_profile_waits_at_most_pool_timeout_and_other_profiles_proceed():
    from mcp_admin_core.gateway_pool import GatewayBusy

    transport, profile = _transport(_ok, connections=2, timeout=0.2)
    async with httpx.AsyncClient(transport=transport) as client:
        held = [await client.send(client.build_request("POST", "http://child/mcp"), stream=True) for _ in range(2)]
        assert transport.usage("default") == (2, 0)
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(GatewayBusy) as busy:
            await client.post("http://child/mcp")
        assert 0.15 <= loop.time() - started < 2
        assert isinstance(busy.value, httpx.PoolTimeout) and busy.value.limits.max_connections == 2
        profile.current = "c" * 24
        assert (await client.post("http://child/mcp")).status_code == 200  # 別的帳號不受影響
        profile.current = "default"
        await held[0].aclose()
        assert transport.usage("default") == (1, 0)
        assert (await client.post("http://child/mcp")).status_code == 200  # 歸還後就能用
        await held[1].aclose()
    assert transport.usage("default") == (0, 0) and not transport._slots


async def test_waiting_request_gets_the_slot_when_one_is_released():
    transport, _ = _transport(_ok, connections=1, timeout=5)
    async with httpx.AsyncClient(transport=transport) as client:
        held = await client.send(client.build_request("POST", "http://child/mcp"), stream=True)
        waiting = asyncio.create_task(client.post("http://child/mcp"))
        await asyncio.sleep(0.05)
        assert not waiting.done()
        await held.aclose()
        assert (await asyncio.wait_for(waiting, 2)).status_code == 200
    assert not transport._slots


async def test_stream_limit_refuses_immediately_and_posts_still_work():
    from mcp_admin_core.gateway_pool import StreamLimitReached

    transport, _ = _transport(_ok, connections=3, streams=1, timeout=5)
    async with httpx.AsyncClient(transport=transport) as client:
        stream = await client.send(client.build_request("GET", "http://child/mcp"), stream=True)
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(StreamLimitReached):
            await client.get("http://child/mcp")
        assert loop.time() - started < 1  # 不等名額
        assert transport.usage("default") == (1, 1)
        assert (await client.post("http://child/mcp")).status_code == 200
        await stream.aclose()
        assert (await client.get("http://child/mcp")).status_code == 200
    assert not transport._slots


async def test_slots_are_returned_on_transport_errors_and_cancellation():
    from mcp_admin_core.gateway_pool import GatewayBusy

    gate = asyncio.Event()

    async def handler(request):
        if request.url.path == "/broken":
            raise httpx.ReadError("child died", request=request)
        await gate.wait()
        return _ok(request)

    transport, _ = _transport(handler, connections=1, streams=1, timeout=0.2)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.ReadError):
            await client.post("http://child/broken")
        assert transport.usage("default") == (0, 0)
        pending = asyncio.create_task(client.get("http://child/slow"))
        await asyncio.sleep(0.05)
        assert transport.usage("default") == (1, 1)
        with pytest.raises(GatewayBusy):  # 名額被 pending 佔住
            await client.post("http://child/mcp")
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert transport.usage("default") == (0, 0) and not transport._slots
        gate.set()
        assert (await client.post("http://child/mcp")).status_code == 200


def test_limits_from_environment(monkeypatch, caplog):
    from mcp_admin_core import gateway_pool
    from mcp_admin_core.profiles import MAX_EXTRA_PROFILES

    limits = gateway_pool.GatewayLimits.from_env()
    assert (limits.max_connections, limits.max_streams, limits.pool_timeout) == (64, 32, 10.0)
    assert limits.max_streams < limits.max_connections
    assert limits.shared_pool_size >= limits.max_connections * (MAX_EXTRA_PROFILES + 1)

    monkeypatch.setenv("MCP_GATEWAY_MAX_CONNECTIONS_PER_PROFILE", "16")
    monkeypatch.setenv("MCP_GATEWAY_MAX_STREAMS_PER_PROFILE", "8")
    monkeypatch.setenv("MCP_GATEWAY_POOL_TIMEOUT_SECONDS", "2.5")
    assert gateway_pool.GatewayLimits.from_env() == gateway_pool.GatewayLimits(16, 8, 2.5)

    caplog.set_level(logging.WARNING, logger="mcp_admin_core.gateway_pool")
    monkeypatch.setenv("MCP_GATEWAY_MAX_STREAMS_PER_PROFILE", "16")  # 不小於連線上限：POST 會沒有名額
    monkeypatch.setenv("MCP_GATEWAY_POOL_TIMEOUT_SECONDS", "nan")
    assert gateway_pool.GatewayLimits.from_env() == gateway_pool.GatewayLimits(16, 15, 10.0)
    monkeypatch.setenv("MCP_GATEWAY_MAX_CONNECTIONS_PER_PROFILE", "0")
    monkeypatch.setenv("MCP_GATEWAY_MAX_STREAMS_PER_PROFILE", "x")
    monkeypatch.setenv("MCP_GATEWAY_POOL_TIMEOUT_SECONDS", "-1")
    assert gateway_pool.GatewayLimits.from_env() == gateway_pool.GatewayLimits(64, 32, 10.0)
    assert len([r for r in caplog.records if r.name == "mcp_admin_core.gateway_pool"]) >= 4


def test_gateway_client_uses_bounded_pool_wait():
    from mcp_admin_core import gateway_pool

    limits = gateway_pool.GatewayLimits(8, 4, 3.0)
    client = gateway_pool.new_client(86400, limits)
    try:
        assert client.timeout.pool == 3.0 and client.timeout.read == 86400 and client.timeout.connect == 10.0
        assert isinstance(client._transport, gateway_pool.ProfileLimitedTransport)
        assert client._transport.limits == limits
    finally:
        asyncio.run(client.aclose())
