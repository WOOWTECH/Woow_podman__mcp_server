"""McpProcessManager 回歸測試 —— singleton、status 形狀、無 command 時不啟動。"""

from __future__ import annotations

from mcp_admin_core import get_process_manager
from mcp_admin_core.process import McpProcessManager


def test_process_manager_singleton_identity():
    assert get_process_manager() is get_process_manager()


async def test_status_when_not_started(store_env):
    pm = McpProcessManager()
    st = await pm.status()
    assert st["running"] is False
    assert st["pid"] is None
    assert "command" in st
    assert st["port"] == 8000


async def test_start_returns_false_without_command(store_env):
    # 預設 mcp_server.command 為空字串 → 不應啟動子行程
    pm = McpProcessManager()
    started = await pm.start()
    assert started is False
    assert pm.is_running is False


from mcp_admin_core.process import _build_subprocess_env


def test_build_subprocess_env_applies_prefix():
    env = _build_subprocess_env(
        {"gateway_api_key": "k", "gateway_url": "u"},
        {"env_prefix": "HERMES_"},
    )
    assert env["HERMES_GATEWAY_API_KEY"] == "k"
    assert env["HERMES_GATEWAY_URL"] == "u"
    assert "GATEWAY_API_KEY" not in env


def test_build_subprocess_env_no_prefix_by_default():
    env = _build_subprocess_env({"gateway_url": "u"}, {})
    assert env["GATEWAY_URL"] == "u"
    assert "HERMES_GATEWAY_URL" not in env


def test_build_subprocess_env_overrides_win():
    env = _build_subprocess_env(
        {"gateway_url": "from_conn"},
        {"env_prefix": "", "env": {"GATEWAY_URL": "override"}},
    )
    assert env["GATEWAY_URL"] == "override"



# ---------------------------------------------------------------------------
# #1350：start_or_restart + exit 後保留最後輸出
# ---------------------------------------------------------------------------

import asyncio
import sys


async def _wait_exit(pm: McpProcessManager, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.05)):
        if not pm.is_running and pm._process is not None and pm._process.returncode is not None:
            # 等 drainer 把 pipe 內剩餘輸出讀完
            await asyncio.sleep(0.3)
            return
        await asyncio.sleep(0.05)
    raise AssertionError("subprocess did not exit in time")


async def _set_cmd(store_env, args: list[str]) -> None:
    await store_env.patch("mcp_server", {"command": sys.executable, "args": args})


async def test_start_or_restart_starts_exited_process_and_keeps_last_output(store_env):
    # 模擬 odoo-manage 開機缺 ODOO_URL：印錯誤後 exit 1
    await _set_cmd(store_env, ["-c", "import sys; print('ODOO_URL not set'); sys.exit(1)"])
    pm = McpProcessManager()
    assert await pm.start() is True
    await _wait_exit(pm)

    st = await pm.status()
    assert st["running"] is False
    assert st["exit_code"] == 1
    assert "ODOO_URL not set" in st["last_output"]

    # 存設定後：子行程未運行 → start_or_restart 走 start（restart_count 不增）
    await _set_cmd(store_env, ["-c", "import time; print('ready'); time.sleep(30)"])
    assert await pm.start_or_restart() is True
    assert pm.is_running is True
    assert (await pm.status())["restart_count"] == 0
    try:
        # 運行中 → start_or_restart 走 restart（restart_count +1）
        assert await pm.start_or_restart() is True
        assert pm.is_running is True
        assert (await pm.status())["restart_count"] == 1
    finally:
        await pm.stop()


async def test_last_output_is_bounded(store_env):
    from mcp_admin_core.process import LAST_OUTPUT_LINES

    await _set_cmd(store_env, ["-c", "for i in range(100): print(f'line{i}')"])
    pm = McpProcessManager()
    assert await pm.start() is True
    await _wait_exit(pm)
    out = (await pm.status())["last_output"]
    assert len(out) == LAST_OUTPUT_LINES
    assert out[-1] == "line99"


# ---------------------------------------------------------------------------
# 看門狗：子行程意外結束時自動重啟（容器 liveness 只看 /healthz，子行程死掉 pod 仍健康）
# ---------------------------------------------------------------------------


async def _wait_until(cond, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.05)):
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")


async def test_unexpected_exit_is_auto_restarted(store_env):
    # 子行程被 OOM／SIGKILL 之類殺掉：看門狗要把它拉回來
    await _set_cmd(store_env, ["-c", "import time; print('up'); time.sleep(30)"])
    pm = McpProcessManager()
    pm.auto_restart_delays = (0.1,)
    assert await pm.start() is True
    first_pid = pm._process.pid
    try:
        pm._process.kill()
        await _wait_until(lambda: pm.is_running and pm._process.pid != first_pid)
        st = await pm.status()
        assert st["running"] is True
        assert st["auto_restart_count"] == 1
        assert st["restart_count"] == 0  # 自動重啟不算手動 restart
    finally:
        await pm.stop()


async def test_intentional_stop_is_not_auto_restarted(store_env):
    await _set_cmd(store_env, ["-c", "import time; time.sleep(30)"])
    pm = McpProcessManager()
    pm.auto_restart_delays = (0.05,)
    assert await pm.start() is True
    await pm.stop()
    await asyncio.sleep(0.4)
    st = await pm.status()
    assert st["running"] is False
    assert st["auto_restart_count"] == 0
    assert st["auto_restart_pending"] is False


async def test_manual_restart_does_not_double_spawn(store_env):
    await _set_cmd(store_env, ["-c", "import time; time.sleep(30)"])
    pm = McpProcessManager()
    pm.auto_restart_delays = (0.05,)
    assert await pm.start() is True
    try:
        assert await pm.restart() is True
        pid = pm._process.pid
        await asyncio.sleep(0.4)
        # 舊子行程結束是 restart 造成的：不應再被看門狗拉起第二個
        assert pm.is_running and pm._process.pid == pid
        assert (await pm.status())["auto_restart_count"] == 0
    finally:
        await pm.stop()


async def test_crash_loop_backs_off_and_stop_cancels_pending(store_env):
    # 設定不完整、一啟動就 exit：退避逐次拉長；stop() 取消已排程的重啟
    await _set_cmd(store_env, ["-c", "import sys; print('ODOO_URL not set'); sys.exit(1)"])
    pm = McpProcessManager()
    pm.auto_restart_delays = (0.05, 0.1, 30)
    try:
        assert await pm.start() is True
        # 等到「第 3 次排程（30 秒）」真的排上：第 2 次重啟的 start() 還在跑時 pending 也是 True、
        # 但 next_auto_restart_in 已清成 None，只看 pending 會偶發失敗（負載高時）。
        await _wait_until(
            lambda: pm._auto_restart_count >= 2 and pm._auto_restart_pending and pm._next_auto_restart_delay == 30
        )
        st = await pm.status()
        assert st["running"] is False
        assert st["auto_restart_pending"] is True
        # next_auto_restart_in 是倒數秒數（DATA-NMCP-004-1），剛排上時接近 30
        assert 29 < st["next_auto_restart_in"] <= 30
        assert "ODOO_URL not set" in st["last_output"]
    finally:
        await pm.stop()
    st = await pm.status()
    assert st["auto_restart_pending"] is False
    count = st["auto_restart_count"]
    await asyncio.sleep(0.3)
    assert (await pm.status())["auto_restart_count"] == count
