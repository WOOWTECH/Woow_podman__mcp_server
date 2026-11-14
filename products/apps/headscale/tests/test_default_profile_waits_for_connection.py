"""Headscale 預設帳號在存連線設定之前不啟動子程式，也就不再「exit 1 → 看門狗 2、5、15 秒…重啟」（2026-11-13 mp-products）。

Headscale 的 LaunchPolicy 本來就宣告 base_url／api_key 是 required_connection（缺了 launcher 的 HeadscaleClient 就拋錯、
exit 1），額外帳號一直照這個規則等設定；預設帳號的啟動路徑原本沒看它。改成每個帳號的子程式都由管理程序同一個規則決定。
合成值、臨時檔案；不 spawn 真的子程式。
"""
import socket
from types import SimpleNamespace

from mcp_admin_core import process as process_module
from mcp_admin_core.config import store as store_module
from mcp_admin_core.configuration import configuration_status
from mcp_admin_core.launch_policy import get_launch_policy, set_launch_policy

from headscale_mcp_admin.main import LAUNCH_POLICY


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_default_profile_child_waits_for_base_url_and_api_key(tmp_path, monkeypatch, capsys):
    store = store_module.ConfigStore(tmp_path / "config.json")
    monkeypatch.setattr(store_module, "_instance", store)
    previous = get_launch_policy()
    set_launch_policy(LAUNCH_POLICY)
    spawned = []

    async def spawn(*argv, **kwargs):
        spawned.append(argv)
        return SimpleNamespace(pid=54321, returncode=None, stdout=None, send_signal=lambda sig: None)

    async def quiet(*args, **kwargs):
        return None

    monkeypatch.setattr(process_module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(process_module.McpProcessManager, "_drain_logs", quiet)
    monkeypatch.setattr(process_module.McpProcessManager, "_verify_configuration", quiet)
    pm = process_module.McpProcessManager()
    try:
        await store.patch("mcp_server", {"port": _free_port()})
        assert await pm.start() is False  # fresh install: no connection yet
        assert spawned == [] and not pm._auto_restart_pending and not pm.is_running
        status = configuration_status(store, await store.load(), pm)
        assert status["apply_state"] == "unconfigured"
        assert "base_url, api_key" in capsys.readouterr().out

        await store.patch("connection", {"base_url": "https://headscale.example"})
        assert await pm.start() is False and spawned == []  # the API key is still missing

        await store.patch("connection", {"api_key": "synthetic-headscale-api-key-0123456789"})
        assert await pm.start() is True and len(spawned) == 1
        assert "synthetic-headscale-api-key-0123456789" not in capsys.readouterr().out
    finally:
        pm._process = None
        set_launch_policy(previous)
