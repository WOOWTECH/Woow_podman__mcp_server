"""閘道請求內容位元組預算（大請求記憶體保護；沙盒 mp-retest2 T8，P2）。

問題：排隊上限只算個數、不算位元組；已送進子行程（每帳號最多 64 個）與還在讀內容的請求完全不受限，1 GiB 的 pod 被約 4 MiB 的請求
打到 OOM、所有帳號一起重啟。修正：``request_budget``（全域＋每帳號的請求內容位元組預算），超過時立刻回與「佇列已滿」一致的忙碌回應。

這個檔案的測試：

- 預設值的推導與環境變數（單元）；
- 預算的記帳：小請求與通知不計、超過單筆上限不計、每條路徑歸還（單元，含 1,000 個混合請求後回到 0）；
- 端到端（真的 uvicorn＋create_app＋假子行程）：超過預算立刻被拒（HTTP 200＋-32003＋id＋Retry-After 5，batch 與 chunked 也是）、
  帳號隔離、通知與小請求不受影響、合法的 13.3 MiB 大請求通過、放開後預算回到 0、用戶端放棄／中斷也歸還。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging

import httpx
import pytest

from loopback_servers import RecordHandler, ThreadedUvicorn, fake_child_app, wait_until

MIB = 1024 * 1024
PROFILE_B = "b0" * 12
TOKEN_A = "budget-test-default-profile-token-0000"  # 合成值，不是真的 token
TOKEN_B = "budget-test-profile-b-token-1111111111"
ENV = ("MCP_GATEWAY_MAX_CONNECTIONS_PER_PROFILE", "MCP_GATEWAY_MAX_STREAMS_PER_PROFILE",
       "MCP_GATEWAY_POOL_TIMEOUT_SECONDS", "MCP_GATEWAY_MAX_QUEUE_PER_PROFILE",
       "MCP_GATEWAY_MAX_BUFFERED_BYTES", "MCP_GATEWAY_MAX_BUFFERED_BYTES_PER_PROFILE")

pytestmark = pytest.mark.usefixtures("synthetic_child")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    from mcp_admin_core import gateway_pool

    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(gateway_pool, "_reports", {})


def _rpc(rpc_id, method="tools/list", pad=0):
    message = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
    if pad:
        message["params"] = {"padding": "x" * pad}
    return message


def _hold(rpc_id, mib):
    """子行程會停住、直到放開的大請求（內容帶 *mib* MiB 的 padding）。"""
    return _rpc(rpc_id, "hold", pad=int(mib * MIB))


class _Gateway:
    def __init__(self, admin, children) -> None:
        self.base = f"http://127.0.0.1:{admin.port}"
        self.children = children

    @staticmethod
    def path(token: str = TOKEN_A) -> str:
        return f"/private_{token}/mcp"

    @staticmethod
    def used(profile: str | None = None) -> int:
        from mcp_admin_core import proxy

        budget = getattr(proxy, "_budget", None)
        return 0 if budget is None else budget.used(profile)

    @staticmethod
    def peak() -> int:
        from mcp_admin_core import proxy

        budget = getattr(proxy, "_budget", None)
        return 0 if budget is None else budget.peak


@pytest.fixture
def gateway(tmp_config_path, monkeypatch):
    import mcp_admin_core.config.store as store_mod
    from mcp_admin_core import create_app, proxy

    monkeypatch.setattr(proxy, "_client", None)
    monkeypatch.setattr(proxy, "_budget", None, raising=False)

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
            with ThreadedUvicorn(create_app(title="gateway budget test")) as admin:
                yield _Gateway(admin, {"default": state_a, PROFILE_B: state_b})
        proxy._client = None

    return start


def _client(gw: _Gateway, timeout: float = 60.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=gw.base, timeout=httpx.Timeout(timeout),
                             limits=httpx.Limits(max_connections=400, max_keepalive_connections=400))


def _budget_env(monkeypatch, total_mib: int, per_profile_mib: int) -> None:
    monkeypatch.setenv("MCP_GATEWAY_MAX_BUFFERED_BYTES", str(total_mib * MIB))
    monkeypatch.setenv("MCP_GATEWAY_MAX_BUFFERED_BYTES_PER_PROFILE", str(per_profile_mib * MIB))


async def _hold_n(client, gw, token, rpc_id, mib, child_key="default"):
    """送一個停在子行程裡的大請求，等到子行程真的收到。"""
    before = gw.children[child_key].held
    task = asyncio.create_task(client.post(gw.path(token), json=_hold(rpc_id, mib)))
    await asyncio.to_thread(wait_until, lambda: gw.children[child_key].held == before + 1)
    return task


def _assert_busy(response: httpx.Response, rpc_id) -> None:
    """與「佇列已滿」一致的忙碌回應：HTTP 200＋-32003＋id 原樣＋Retry-After: 5。"""
    assert response.status_code == 200, response.text[:300]
    body = response.json()
    assert body["id"] == rpc_id and body["error"]["code"] == -32003
    assert response.headers["retry-after"] == "5"


# ---------------------------------------------------------------------------
# 一、預設值與環境變數（單元）
# ---------------------------------------------------------------------------


def test_defaults_derive_from_the_cgroup_memory_limit(monkeypatch):
    """第二輪（F2、沙盒 P0）：預設值扣掉「帳號數 × 子行程記憶體」再算，見 test_gateway_budget_defaults.py 的完整場景。
    這裡只留最基本的形狀：沒有 LaunchPolicy（單一帳號、子行程 160 MiB 的預設估計）、1 GiB。"""
    from mcp_admin_core import request_budget as rb

    monkeypatch.setattr(rb, "_memory_limit_mib", lambda: 1024)
    limits = rb.BudgetLimits.from_env()
    expected = int((1024 - rb.GATEWAY_BASELINE_MIB - 1 * rb.DEFAULT_CHILD_MEMORY_MIB - rb.SAFETY_MARGIN_MIB) * MIB / rb.AMPLIFICATION)
    assert limits.total == expected
    assert 16 * MIB <= limits.per_profile == max(16 * MIB, limits.total // 2)
    # 放大倍數乘回去，加上閘道與子行程的基線，仍留得住安全邊際
    assert limits.total * rb.AMPLIFICATION + (rb.GATEWAY_BASELINE_MIB + rb.DEFAULT_CHILD_MEMORY_MIB) * MIB \
        <= (1024 - rb.SAFETY_MARGIN_MIB) * MIB + 1


def test_defaults_without_a_cgroup_limit_and_clamps(monkeypatch):
    from mcp_admin_core import request_budget as rb

    monkeypatch.setattr(rb, "_memory_limit_mib", lambda: None)
    assert rb.BudgetLimits.from_env().total == rb.FALLBACK_TOTAL_BYTES == 32 * MIB  # 放大 8.5 倍後約 272 MiB，512 MiB 的容器放得下
    monkeypatch.setattr(rb, "_memory_limit_mib", lambda: 128)  # 很小的容器：不低於單筆上限 16 MiB（每帳號至少能放行一個）
    small = rb.BudgetLimits.from_env()
    assert small.total == 16 * MIB and small.per_profile == 16 * MIB


def test_environment_overrides_and_invalid_values_warn(monkeypatch):
    from mcp_admin_core import request_budget as rb

    monkeypatch.setattr(rb, "_memory_limit_mib", lambda: 1024)
    default = rb.BudgetLimits.from_env()
    monkeypatch.setenv(rb.TOTAL_ENV, str(200 * MIB))
    monkeypatch.setenv(rb.PER_PROFILE_ENV, str(50 * MIB))
    assert rb.BudgetLimits.from_env() == rb.BudgetLimits(total=200 * MIB, per_profile=50 * MIB)
    for bad in ("abc", "1", str(1024 ** 5), "-5"):  # 不是整數、低於 16 MiB、太大、負數
        monkeypatch.setenv(rb.TOTAL_ENV, bad)
        with RecordHandler("mcp_admin_core.request_budget") as logs:
            assert rb.BudgetLimits.from_env().total == default.total
        message = " ".join(r.getMessage() for r in logs.records if r.levelno == logging.WARNING)
        assert rb.TOTAL_ENV in message and "abc" not in message  # 只記變數名稱、範圍與預設值，不回顯設定值


# ---------------------------------------------------------------------------
# 二、記帳（單元）
# ---------------------------------------------------------------------------


def _budget(total_mib=32, per_mib=20):
    from mcp_admin_core import request_budget as rb

    return rb.RequestBudget(rb.BudgetLimits(total=total_mib * MIB, per_profile=per_mib * MIB))


CAP = 16 * MIB


def test_small_requests_are_never_charged_or_refused():
    budget = _budget(16, 16)
    full = budget.open("default")
    full.grow(15 * MIB, cap=CAP)
    notification = budget.open("default")
    notification.grow(2048, cap=CAP)  # 通知與一般小呼叫
    notification.grow(64 * 1024, cap=CAP)
    assert budget.used() == 15 * MIB  # 小請求沒有計入
    notification.finish()
    full.finish()
    assert budget.used() == 0


def test_profile_and_global_limits_are_independent():
    from mcp_admin_core.request_budget import BudgetExceeded

    budget = _budget(30, 20)
    a1 = budget.open("a")
    a1.grow(12 * MIB, cap=CAP)
    a2 = budget.open("a")
    with pytest.raises(BudgetExceeded) as profile_full:
        a2.grow(9 * MIB, cap=CAP)
    assert profile_full.value.scope == "profile"
    b1 = budget.open("b")
    b1.grow(12 * MIB, cap=CAP)  # 帳號隔離：A 滿了，B 照常
    b2 = budget.open("b")
    with pytest.raises(BudgetExceeded) as total_full:
        b2.grow(8 * MIB, cap=CAP)  # 12+12+8 > 30
    assert total_full.value.scope == "global"
    assert budget.used() == 24 * MIB and budget.used("a") == 12 * MIB
    for r in (a1, a2, b1, b2):
        r.finish()
    assert budget.used() == 0 and budget.used("a") == 0


def test_every_profile_can_always_send_one_maximum_size_request():
    budget = _budget(16, 16)  # 兩個預算的下限＝單筆上限
    r = budget.open("only")
    r.grow(16 * MIB, cap=CAP)
    assert budget.used("only") == 16 * MIB
    r.finish()


def test_requests_over_the_single_request_cap_are_not_held():
    budget = _budget(32, 32)
    r = budget.open("a")
    r.grow(10 * MIB, cap=CAP)
    assert budget.used() == 10 * MIB
    r.grow(17 * MIB, cap=CAP)  # chunked 讀到超過單筆上限：內容改為丟棄，已登記的還回去
    assert budget.used() == 0
    r.grow(40 * MIB, cap=CAP)
    assert budget.used() == 0
    r.finish()
    assert budget.used() == 0


def test_reservation_is_held_until_proxy_finished_and_every_upstream_closed():
    budget = _budget()
    r = budget.open("a")
    r.grow(5 * MIB, cap=CAP)
    r.attach()  # 送往子行程
    r.finish()  # proxy 已經回應（串流還開著）
    assert budget.used() == 5 * MIB
    r.attach()  # 第二次送出（例如重試）
    r.detach()
    assert budget.used() == 5 * MIB
    r.detach()  # 串流結束
    assert budget.used() == 0
    r.detach()  # 重複歸還不會變負
    r.finish()
    assert budget.used() == 0


def test_a_refused_grow_does_not_leak_and_finish_refunds():
    from mcp_admin_core.request_budget import BudgetExceeded

    budget = _budget(32, 20)
    r = budget.open("a")
    r.grow(5 * MIB, cap=CAP)
    with pytest.raises(BudgetExceeded):
        r.grow(25 * MIB if False else 21 * MIB, cap=40 * MIB)
    r.finish()
    assert budget.used() == 0 and budget.used("a") == 0


def test_thousand_mixed_requests_return_the_budget_to_zero():
    import random

    from mcp_admin_core.request_budget import BudgetExceeded

    rng = random.Random(7)
    budget = _budget(48, 24)
    live = []
    for step in range(1000):
        r = budget.open(rng.choice("abc"))
        try:
            r.grow(rng.choice([0, 300, 70_000, 3 * MIB, 9 * MIB, 15 * MIB, 17 * MIB]), cap=CAP)
            if rng.random() < 0.5:
                r.grow(r.size + rng.choice([0, 2 * MIB, 8 * MIB]), cap=CAP)
        except BudgetExceeded:
            pass
        attached = rng.random() < 0.6
        if attached:
            r.attach()
        r.finish() if rng.random() < 0.7 else live.append(r)
        if attached and rng.random() < 0.8:
            r.detach()
        elif attached:
            live.append(r)
        if len(live) > 12:
            old = live.pop(rng.randrange(len(live)))
            old._refs and old.detach()
            old.finish()
        assert 0 <= budget.used() <= 48 * MIB
    for r in live:
        while r._refs:
            r.detach()
        r.finish()
    assert budget.used() == 0 and budget._profiles == {}


# ---------------------------------------------------------------------------
# 三、transport 的歸還時機（單元）
# ---------------------------------------------------------------------------


async def _send(transport, reservation, method="POST"):
    from mcp_admin_core import gateway_pool

    request = httpx.Request(method, "http://child/mcp", content=b"x")
    gateway_pool.GatewayHints(reservation=reservation).attach(request)
    return await transport.handle_async_request(request)


class _Stream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"data"


@pytest.mark.asyncio
async def test_transport_returns_the_budget_on_stream_close_error_and_queue_full():
    from mcp_admin_core import gateway_pool

    budget = _budget()
    limits = gateway_pool.GatewayLimits(max_connections=1, max_streams=1, pool_timeout=0.2, max_queue=0)

    class Inner(httpx.AsyncBaseTransport):
        mode = "stream"

        async def handle_async_request(self, request):
            if self.mode == "error":
                raise httpx.ReadError("boom")
            return httpx.Response(200, stream=_Stream())

    inner = Inner()
    transport = gateway_pool.ProfileLimitedTransport(inner, limits)
    held = budget.open("default")
    held.grow(5 * MIB, cap=CAP)
    response = await _send(transport, held)
    held.finish()  # proxy 已回應；串流還沒讀完
    assert budget.used() == 5 * MIB  # 回應還開著：預算與連線名額一起佔著
    # 佇列已滿（max_queue=0）：這個請求沒送出，也要還
    queued = budget.open("default")
    queued.grow(4 * MIB, cap=CAP)
    with pytest.raises(gateway_pool.GatewayQueueFull):
        await _send(transport, queued)
    queued.finish()
    assert budget.used() == 5 * MIB
    await response.aclose()  # 回應結束
    assert budget.used() == 0
    inner.mode = "error"
    failed = budget.open("default")
    failed.grow(6 * MIB, cap=CAP)
    with pytest.raises(httpx.ReadError):
        await _send(transport, failed)
    failed.finish()
    assert budget.used() == 0


# ---------------------------------------------------------------------------
# 四、端到端
# ---------------------------------------------------------------------------


def test_over_budget_request_is_refused_immediately_with_the_queue_full_style_reply(gateway, monkeypatch):
    """T8 的核心：已在子行程裡的大請求把預算用掉，下一個大請求立刻拿到忙碌回應（不排隊、不送給子行程）。"""
    _budget_env(monkeypatch, 16, 16)
    with gateway() as gw, RecordHandler("mcp_admin_core") as logs:
        async def scenario():
            async with _client(gw) as client:
                first = await _hold_n(client, gw, TOKEN_A, 1, 10)
                assert 10 * MIB <= gw.used() < 10 * MIB + 4096
                response = await asyncio.wait_for(client.post(gw.path(), json=_hold(2, 10)), timeout=10)
                _assert_busy(response, 2)
                assert 2 not in gw.children["default"].received_ids()
                # 小請求與通知不受影響（保留名額的目的）
                ok = await client.post(gw.path(), json=_rpc(3))
                assert ok.status_code == 200 and "result" in ok.json()
                note = await client.post(gw.path(), json={"jsonrpc": "2.0", "method": "notifications/initialized"})
                assert note.status_code == 202
                gw.children["default"].release_holds.set()
                assert (await first).status_code == 200
                await asyncio.to_thread(wait_until, lambda: gw.used() == 0)
        asyncio.run(scenario())
        warnings = [r.getMessage() for r in logs.records if r.levelno == logging.WARNING and "MCP_GATEWAY_MAX_BUFFERED" in r.getMessage()]
        assert len(warnings) == 1 and "xxxx" not in warnings[0] and TOKEN_A not in warnings[0]


def test_refusal_formats_for_get_batch_and_chunked(gateway, monkeypatch):
    _budget_env(monkeypatch, 16, 16)
    with gateway() as gw:
        async def scenario():
            async with _client(gw) as client:
                first = await _hold_n(client, gw, TOKEN_A, 1, 10)
                batch = await client.post(gw.path(), json=[_hold(10, 5), _hold(11, 5)])
                assert batch.status_code == 503 and batch.headers["retry-after"] == "5"
                assert batch.json()["error"]["code"] == -32003

                async def chunks():
                    for _ in range(8):  # 沒有 Content-Length（chunked）：邊讀邊計，超過就停止
                        yield (b"x" * MIB)
                chunked = await client.post(gw.path(), content=chunks(), headers={"content-type": "application/json"})
                assert chunked.status_code in (200, 503) and chunked.json()["error"]["code"] == -32003
                gw.children["default"].release_holds.set()
                await first
                await asyncio.to_thread(wait_until, lambda: gw.used() == 0)
        asyncio.run(scenario())


def test_chunked_request_within_budget_is_forwarded_and_budget_returns(gateway, monkeypatch):
    _budget_env(monkeypatch, 16, 16)
    with gateway() as gw:
        async def scenario():
            async with _client(gw) as client:
                body = json.dumps(_rpc(5, "tools/list", pad=3 * MIB)).encode()

                async def chunks():
                    for i in range(0, len(body), MIB):
                        yield body[i:i + MIB]
                response = await client.post(gw.path(), content=chunks(), headers={"content-type": "application/json"})
                assert response.status_code == 200 and "result" in response.json()
                await asyncio.to_thread(wait_until, lambda: gw.used() == 0)
                assert gw.peak() >= 3 * MIB  # 讀的時候有登記
        asyncio.run(scenario())


def test_one_profile_filling_its_share_does_not_affect_another(gateway, monkeypatch):
    _budget_env(monkeypatch, 32, 16)
    with gateway() as gw:
        async def scenario():
            async with _client(gw) as client:
                a1 = await _hold_n(client, gw, TOKEN_A, 1, 10)
                _assert_busy(await client.post(gw.path(TOKEN_A), json=_hold(2, 10)), 2)  # A 的份額滿了
                b1 = await _hold_n(client, gw, TOKEN_B, 3, 10, PROFILE_B)  # B 照常通過
                assert 10 * MIB <= gw.used("default") < 11 * MIB and 10 * MIB <= gw.used(PROFILE_B) < 11 * MIB
                # 全域 32 MiB：B 再要 10 MiB 時全域剩 12，仍可；兩邊都再要就被拒
                for state in gw.children.values():
                    state.release_holds.set()
                await asyncio.gather(a1, b1)
                await asyncio.to_thread(wait_until, lambda: gw.used() == 0)
        asyncio.run(scenario())


def test_global_budget_is_shared_by_all_profiles(gateway, monkeypatch):
    _budget_env(monkeypatch, 16, 16)
    with gateway() as gw:
        async def scenario():
            async with _client(gw) as client:
                a = await _hold_n(client, gw, TOKEN_A, 1, 10)
                _assert_busy(await client.post(gw.path(TOKEN_B), json=_hold(2, 10)), 2)  # B 自己沒用，但全域用完了
                gw.children["default"].release_holds.set()
                await a
        asyncio.run(scenario())


def test_legitimate_13_mib_upload_passes_with_default_budget(gateway, monkeypatch):
    from mcp_admin_core import request_budget as rb

    monkeypatch.setattr(rb, "_memory_limit_mib", lambda: 1024)
    with gateway() as gw:
        async def scenario():
            async with _client(gw) as client:
                response = await client.post(gw.path(), json=_rpc(9, "tools/call", pad=int(13.3 * MIB)))
                assert response.status_code == 200 and "result" in response.json()
                await asyncio.to_thread(wait_until, lambda: gw.used() == 0)
        asyncio.run(scenario())


def test_flood_of_large_requests_never_holds_more_than_the_budget(gateway, monkeypatch):
    """直接送進子行程的大請求（不必排隊）也受限：40 個 8 MiB 同時送，登記量任何時刻都不超過預算。"""
    _budget_env(monkeypatch, 32, 32)
    with gateway() as gw:
        async def scenario():
            async with _client(gw) as client:
                tasks = [asyncio.create_task(client.post(gw.path(), json=_hold(100 + i, 8))) for i in range(40)]
                await asyncio.to_thread(wait_until, lambda: gw.children["default"].held >= 1)
                await asyncio.sleep(1.5)
                gw.children["default"].release_holds.set()
                results = await asyncio.gather(*tasks)
                admitted = [r for r in results if "result" in r.json()]
                refused = [r for r in results if r.json().get("error", {}).get("code") == -32003]
                assert len(admitted) + len(refused) == 40 and refused
                assert len(admitted) >= 3  # 預算 32 MiB／約 8 MiB
                assert gw.peak() <= 32 * MIB
                await asyncio.to_thread(wait_until, lambda: gw.used() == 0)
        asyncio.run(scenario())


def test_budget_returns_after_abandoned_queue_client_disconnect_and_refusals(gateway, monkeypatch):
    monkeypatch.setenv("MCP_GATEWAY_MAX_CONNECTIONS_PER_PROFILE", "2")
    monkeypatch.setenv("MCP_GATEWAY_MAX_STREAMS_PER_PROFILE", "1")
    monkeypatch.setenv("MCP_GATEWAY_POOL_TIMEOUT_SECONDS", "30")
    _budget_env(monkeypatch, 64, 64)
    with gateway() as gw:
        async def scenario():
            async with _client(gw) as client:
                holds = [asyncio.create_task(client.post(gw.path(), json=_hold(i, 1))) for i in range(2)]
                await asyncio.to_thread(wait_until, lambda: gw.children["default"].held == 2)
                # 排隊中的大請求：用戶端放棄
                with pytest.raises(httpx.TimeoutException):
                    await client.post(gw.path(), json=_hold(50, 8), timeout=0.5)
                await asyncio.to_thread(wait_until, lambda: 2 * MIB <= gw.used() < 3 * MIB, 5)
                # 內容傳到一半用戶端就斷線
                async def broken():
                    yield b"x" * MIB
                    raise RuntimeError("client dies")
                with pytest.raises(Exception):
                    await client.post(gw.path(), content=broken(), headers={"content-type": "application/json"})
                gw.children["default"].release_holds.set()
                await asyncio.gather(*holds)
                await asyncio.to_thread(wait_until, lambda: gw.used() == 0, 5)
        asyncio.run(scenario())


def test_proxy_wrapper_returns_the_budget_when_the_request_raises(monkeypatch):
    from mcp_admin_core import proxy, request_budget as rb

    budget = _budget()
    monkeypatch.setattr(proxy, "_budget", budget, raising=False)

    async def boom(token, path, request, reservation):
        reservation.grow(5 * MIB, cap=CAP)
        assert budget.used() == 5 * MIB
        raise RuntimeError("boom")

    monkeypatch.setattr(proxy, "_mcp_proxy", boom, raising=False)
    with pytest.raises(RuntimeError):
        asyncio.run(proxy.mcp_proxy("t", "mcp", None))
    assert budget.used() == 0
