"""看門狗狀態要看得到、退避要從第一階開始（商用驗收 DATA-NMCP-004-1）。

事故：n8n MCP 251 kill -9 子行程後，第一次自動重啟等了 5 秒（梯度第二階）而不是 2 秒；
而且 Dashboard 只顯示 pid，``/api/health`` 只有 ``restart_count``，看門狗的自動重啟次數、
是否排程中、還要等幾秒只在 ``/api/settings/mcp/status`` 看得到。

5 秒的原因：手動 restart（含存設定觸發的 restart）時，``start()`` 先把 ``_intentional_stop``
清成 False，接著在 ``await create_subprocess_exec`` 讓出事件迴圈；此時 ``self._process`` 還是
舊的子行程，舊子行程的 drainer 醒來後判定「意外結束」，偷偷把 ``_consecutive_failures`` 加一
並排了一個不會真的執行的重啟。之後 120 秒內真的崩潰，就從第二階（5 秒）起跳。
"""

from __future__ import annotations

import asyncio
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import mcp_admin_core.process as process_mod
from mcp_admin_core.process import McpProcessManager


async def _set_cmd(store_env, args: list[str]) -> None:
    await store_env.patch("mcp_server", {"command": sys.executable, "args": args})


async def _wait_until(cond, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.05)):
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")


@pytest.fixture
def slow_spawn(monkeypatch):
    """讓 spawn 慢一點（真實環境 fork/exec 一樣會讓出事件迴圈），把競態窗口撐大到必現。"""
    real = asyncio.create_subprocess_exec

    async def _slow(*args, **kwargs):
        await asyncio.sleep(0.3)
        return await real(*args, **kwargs)

    monkeypatch.setattr(process_mod.asyncio, "create_subprocess_exec", _slow)


async def test_manual_restart_does_not_arm_watchdog(store_env, slow_spawn):
    await _set_cmd(store_env, ["-c", "import time; time.sleep(30)"])
    pm = McpProcessManager()
    pm.auto_restart_delays = (0.2, 7)
    assert await pm.start() is True
    try:
        assert await pm.restart() is True
        await asyncio.sleep(0.5)  # 讓舊子行程的 drainer 跑完
        assert pm._consecutive_failures == 0, "手動 restart 不應被算成一次崩潰"
        st = await pm.status()
        assert st["auto_restart_pending"] is False
        assert st["auto_restart_count"] == 0

        # 之後真的崩潰：第一次自動重啟要用梯度第一階
        pid = pm._process.pid
        pm._process.kill()
        await _wait_until(lambda: pm._auto_restart_pending)
        assert pm._next_auto_restart_delay == 0.2
        await _wait_until(lambda: pm.is_running and pm._process.pid != pid)
        assert (await pm.status())["auto_restart_count"] == 1
    finally:
        await pm.stop()


async def test_status_shape_is_stable_while_running(store_env):
    await _set_cmd(store_env, ["-c", "import time; time.sleep(30)"])
    pm = McpProcessManager()
    assert await pm.start() is True
    try:
        st = await pm.status()
        assert st["running"] is True
        assert st["auto_restart_pending"] is False
        assert st["next_auto_restart_in"] is None
    finally:
        await pm.stop()


async def test_next_auto_restart_in_counts_down(store_env):
    # next_auto_restart_in 是「還要等幾秒」，不是固定的退避值
    await _set_cmd(store_env, ["-c", "import sys; sys.exit(3)"])
    pm = McpProcessManager()
    pm.auto_restart_delays = (30,)
    try:
        assert await pm.start() is True
        await _wait_until(lambda: pm._auto_restart_pending)
        first = (await pm.status())["next_auto_restart_in"]
        await asyncio.sleep(1.2)
        second = (await pm.status())["next_auto_restart_in"]
        assert 0 < second < first <= 30
        assert first - second >= 1.0
    finally:
        await pm.stop()


def test_mcp_server_health_exposes_watchdog_running():
    from mcp_admin_core.routers.health import mcp_server_health

    m = mcp_server_health({
        "running": True, "pid": 88, "restart_count": 2, "auto_restart_count": 4,
        "auto_restart_pending": False, "next_auto_restart_in": None,
    })
    assert m == {
        "healthy": True,
        "process": "pid=88",
        "restart_count": 2,
        "auto_restart_count": 4,
        "auto_restart_pending": False,
        "next_auto_restart_in": None,
        "exit_code": None,
    }


def test_mcp_server_health_exposes_watchdog_pending():
    from mcp_admin_core.routers.health import mcp_server_health

    m = mcp_server_health({
        "running": False, "pid": None, "restart_count": 0, "auto_restart_count": 1,
        "auto_restart_pending": True, "next_auto_restart_in": 4.2, "exit_code": -9,
    })
    assert m["healthy"] is False
    assert m["process"] == "stopped"
    assert m["auto_restart_count"] == 1
    assert m["auto_restart_pending"] is True
    assert m["next_auto_restart_in"] == 4.2
    assert m["exit_code"] == -9


def test_health_router_includes_watchdog_fields(store_env):
    from mcp_admin_core.routers.health import make_health_router

    async def probe_target(conn):
        return {"healthy": True}

    async def fetch_info(conn):
        return {}

    app = FastAPI()
    app.include_router(make_health_router(app_type="x", probe_target=probe_target, fetch_info=fetch_info))
    body = TestClient(app).get("/api/health").json()
    for key in ("restart_count", "auto_restart_count", "auto_restart_pending", "next_auto_restart_in", "exit_code"):
        assert key in body["mcp_server"], key
