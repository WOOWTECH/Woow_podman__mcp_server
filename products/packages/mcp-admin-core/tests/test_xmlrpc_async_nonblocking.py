"""同步 XML-RPC 不得卡住事件迴圈（DATA-OMCP-007-1）。

實測：Odoo URL 指到黑洞位址後，``/api/health`` 的同步 ``common.version()`` 卡死事件迴圈，
``/healthz`` 逾時、kubelet liveness 殺容器（服務中斷約 2.5 分鐘）。
這裡用「接受連線但永不回應」的 socket 模擬黑洞，量測事件迴圈的最長停頓。
"""

from __future__ import annotations

import asyncio
import functools
import socket
import time

import pytest

from mcp_admin_core.util import xmlrpc_async


@pytest.fixture
def blackhole():
    """接受 TCP 連線（kernel backlog）但永遠不讀不回的伺服器。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    yield f"http://127.0.0.1:{srv.getsockname()[1]}"
    srv.close()


async def _max_loop_stall(coro, tick: float = 0.02) -> tuple[float, object]:
    """執行 coro，同時量測事件迴圈相鄰兩次 tick 的最大間隔。"""
    stalls = [0.0]
    done = asyncio.Event()

    async def ticker():
        last = time.monotonic()
        while not done.is_set():
            await asyncio.sleep(tick)
            now = time.monotonic()
            stalls[0] = max(stalls[0], now - last)
            last = now

    t = asyncio.create_task(ticker())
    try:
        result = await coro
    finally:
        done.set()
        await t
    return stalls[0], result


def test_make_proxy_has_socket_timeout(blackhole):
    proxy = xmlrpc_async.make_proxy(f"{blackhole}/xmlrpc/2/common", timeout=0.3)
    start = time.monotonic()
    with pytest.raises(OSError):  # socket.timeout 是 OSError 子類
        proxy.version()
    assert time.monotonic() - start < 3


def test_odoo_health_probe_does_not_block_event_loop(blackhole, monkeypatch):
    from odoo_mcp_admin import health_probes

    monkeypatch.setattr(health_probes, "make_proxy", functools.partial(xmlrpc_async.make_proxy, timeout=0.6))

    async def scenario():
        return await _max_loop_stall(health_probes.fetch_info({
            "odoo_url": blackhole, "odoo_db": "d", "odoo_username": "u", "odoo_password": "p",
        }))

    stall, info = asyncio.run(scenario())
    # 探測本身要等到 socket 逾時（>=0.6 s），但事件迴圈的停頓必須遠小於它
    assert stall < 0.3, f"event loop stalled {stall:.2f}s"
    assert info == {"version": None, "db_name": "d", "item_count": None}


def test_odoo_connection_test_does_not_block_event_loop(blackhole, store_env, monkeypatch):
    from odoo_mcp_admin.routers import config as cfg

    monkeypatch.setattr(cfg, "make_proxy", functools.partial(xmlrpc_async.make_proxy, timeout=0.6))

    async def scenario():
        await store_env.patch("connection", {
            "odoo_url": blackhole, "odoo_db": "d", "odoo_username": "u", "odoo_password": "p",
        })
        return await _max_loop_stall(cfg.test_connection())

    stall, resp = asyncio.run(scenario())
    assert stall < 0.3, f"event loop stalled {stall:.2f}s"
    assert resp.success is False and "Cannot reach Odoo" in resp.message


def test_run_blocking_gives_up_at_deadline():
    async def scenario():
        with pytest.raises(asyncio.TimeoutError):
            await xmlrpc_async.run_blocking(time.sleep, 1.0, deadline=0.1)

    start = time.monotonic()
    asyncio.run(scenario())
    # wait_for 在 deadline 就放棄等待（工作緒稍後自行結束）；整體不該等滿 1 秒以上太多
    assert time.monotonic() - start < 2
