"""還原進行中標記（.mcp-restore-in-progress）：管理程序拒絕啟動，不讀寫設定、不發 token、不啟動子行程。

標記由 restore_inplace 在動到任何檔案前寫下、確認完成後最後才移除。檢查在最外層的 ASGI lifespan：app 自己包在 core
lifespan 外面的 seed／遷移（odoo／litellm／nextcloud 的 seed_defaults、opendesign 的 seed_migration、headscale／emqx
讀設定）也不會執行。uvicorn 收到 lifespan.startup.failed 會在建立監聽 socket 之前以 exit code 3 結束。
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from mcp_admin_core.config.store import ConfigStore, ConfigUnavailable
from mcp_admin_core.restore_marker import MARKER_NAME, RestoreInProgress, marker_path, restore_in_progress

PRODUCT_APPS = ["emqx", "headscale", "hermes", "k8s", "litellm", "n8n", "nextcloud", "odoo", "opendesign", "vk"]


def legacy_config(port: int = 18999) -> dict:
    """沒有 owner_defaults 標記、從來沒有過 token 的舊設定：正常啟動會遷移、自動發 token（設定檔一定會變）。"""
    return {"admin_password": "fixture-admin-password-0123456789", "connection": {},
            "tools": {"disabled": ["fixture_tool"], "disabled_operations": {}},
            "mcp_server": {"port": port}, "proxy": {"timeout": 86400}}


async def drive_lifespan(app) -> tuple[list[str], BaseException | None]:
    """照 ASGI lifespan 協定送 startup（成功的話再送 shutdown）；回傳 app 送出的訊息種類與它拋出的例外。

    不用 TestClient：它在 app 送出 startup.failed 之後若正常返回，會把啟動當成成功。
    """
    sent: list[dict] = []
    inbox: asyncio.Queue = asyncio.Queue()
    await inbox.put({"type": "lifespan.startup"})

    async def receive():
        return await inbox.get()

    async def send(message):
        sent.append(message)

    scope = {"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.0"}, "state": {}}
    task = asyncio.create_task(app(scope, receive, send))
    for _ in range(1000):
        if task.done() or any(m["type"].startswith("lifespan.startup.") for m in sent):
            break
        await asyncio.sleep(0.01)
    if sent and sent[0]["type"] == "lifespan.startup.complete":
        await inbox.put({"type": "lifespan.shutdown"})
    error = None
    try:
        await asyncio.wait_for(task, 20)
    except BaseException as exc:  # noqa: BLE001
        error = exc
    return [m["type"] for m in sent], error


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    import mcp_admin_core.config.store as store_mod

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(data / "config.json"))
    monkeypatch.setattr(store_mod, "_instance", None)
    return data


@pytest.fixture
def children(monkeypatch):
    """類別層級的 spy：不必先建立 store／registry（標記存在時建立 store 本身就會被拒）。

    預設的子行程管理器換成新的實例：別的測試以 monkeypatch 改過單例的 ``start`` 之後，還原時會在實例上留下原本的
    bound method，蓋過類別層級的 spy。
    """
    from mcp_admin_core import process
    from mcp_admin_core.process import McpProcessManager
    from mcp_admin_core.profiles import ProfileRegistry

    monkeypatch.setattr(process, "_instance", None)
    spies = {"start": AsyncMock(return_value=True), "stop": AsyncMock(return_value=None),
             "start_all": AsyncMock(return_value=None), "stop_all": AsyncMock(return_value=None)}
    monkeypatch.setattr(McpProcessManager, "start", spies["start"])
    monkeypatch.setattr(McpProcessManager, "stop", spies["stop"])
    monkeypatch.setattr(ProfileRegistry, "start_all", spies["start_all"])
    monkeypatch.setattr(ProfileRegistry, "stop_all", spies["stop_all"])
    return spies


def _snapshot(path: Path) -> tuple[bytes, int, int]:
    info = path.stat()
    return path.read_bytes(), info.st_mode, info.st_mtime_ns


def _write(path: Path, config: dict, mode: int = 0o640) -> None:
    path.write_text(json.dumps(config))
    path.chmod(mode)  # 不是 0600：ConfigStore 一讀就會收緊，所以 mode 沒變＝沒有被讀過


async def test_marker_refuses_startup_before_migration_token_and_children(data_dir, children, capsys):
    from mcp_admin_core import create_app

    config = data_dir / "config.json"
    _write(config, legacy_config())
    (data_dir / MARKER_NAME).write_text("restore in progress\n")
    before = _snapshot(config)
    app = create_app(title="Restore Marker Test")
    sent, error = await drive_lifespan(app)
    assert sent == ["lifespan.startup.failed"]
    assert isinstance(error, RestoreInProgress)
    assert _snapshot(config) == before  # 位元組、權限、mtime 都沒變：沒有遷移、沒有發 token、連讀都沒讀
    assert sorted(os.listdir(data_dir)) == sorted(["config.json", MARKER_NAME])
    for spy in children.values():
        spy.assert_not_awaited()
    out = capsys.readouterr().out
    assert "拒絕啟動" in out and str(data_dir / MARKER_NAME) in out  # 容器 stdout 看得到原因


async def test_marker_blocks_app_level_lifespan_wrappers_and_absent_marker_starts_normally(data_dir, children):
    """odoo／litellm／nextcloud／opendesign／headscale／emqx 都把自己的 seed／遷移包在 core lifespan 外面。"""
    from mcp_admin_core import create_app
    from mcp_admin_core.config import get_config_store

    config = data_dir / "config.json"
    _write(config, legacy_config())
    marker = data_dir / MARKER_NAME
    marker.write_text("restore in progress\n")
    app = create_app(title="Restore Marker Wrapper Test")
    core = app.router.lifespan_context
    seeded: list[str] = []

    @asynccontextmanager
    async def product_lifespan(a):
        seeded.append("seed")
        await get_config_store().put("product_seed", True)  # 與各產品的 seed_defaults 相同：會寫設定
        async with core(a):
            yield

    app.router.lifespan_context = product_lifespan
    before = _snapshot(config)
    sent, error = await drive_lifespan(app)
    assert sent == ["lifespan.startup.failed"] and isinstance(error, RestoreInProgress)
    assert seeded == [] and _snapshot(config) == before
    children["start"].assert_not_awaited()

    marker.unlink()  # 標記不在：照常啟動（證明上面的斷言抓得到變更）
    sent, error = await drive_lifespan(app)
    assert sent == ["lifespan.startup.complete", "lifespan.shutdown.complete"] and error is None
    assert seeded == ["seed"]
    after = json.loads(config.read_text())
    assert after["product_seed"] is True
    assert after["tools"]["owner_defaults_20261025"]["applied"] == "migrated"
    assert after["mcp_auth_token"] and after["token_history"][0]["action"] == "auto_issue"
    children["start"].assert_awaited()
    children["start_all"].assert_awaited()


async def test_core_lifespan_itself_refuses_when_called_directly(data_dir, children):
    from mcp_admin_core import create_app

    config = data_dir / "config.json"
    _write(config, legacy_config())
    (data_dir / MARKER_NAME).write_text("x")
    before = _snapshot(config)
    app = create_app(title="Restore Marker Direct Test")
    with pytest.raises(RestoreInProgress):
        async with app.router.lifespan_context(app):
            pass
    assert _snapshot(config) == before
    children["start"].assert_not_awaited()


@pytest.mark.parametrize("product", PRODUCT_APPS)
async def test_every_product_app_refuses_before_its_own_seed_or_migration(product, data_dir, children):
    module = importlib.import_module(f"{product}_mcp_admin.main")
    config = data_dir / "config.json"
    _write(config, legacy_config())
    (data_dir / MARKER_NAME).write_text("x")
    before = _snapshot(config)
    sent, error = await drive_lifespan(module.app)
    assert sent == ["lifespan.startup.failed"] and isinstance(error, RestoreInProgress)
    assert _snapshot(config) == before
    assert sorted(os.listdir(data_dir)) == sorted(["config.json", MARKER_NAME])
    for spy in children.values():
        spy.assert_not_awaited()


async def test_missing_config_during_restore_is_not_replaced_by_factory_defaults(data_dir, children):
    """還原到一半（舊項目已移開、新結果還沒放進來）：不可補一份 admin／admin 的出廠設定。"""
    from mcp_admin_core import create_app

    (data_dir / MARKER_NAME).write_text("x")
    sent, error = await drive_lifespan(create_app(title="Restore Marker Empty Test"))
    assert sent == ["lifespan.startup.failed"]
    assert os.listdir(data_dir) == [MARKER_NAME]


def test_config_store_never_creates_a_factory_config_during_a_restore(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    (data / MARKER_NAME).write_text("x")
    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(data / "config.json"))
    with pytest.raises(ConfigUnavailable):
        ConfigStore()
    profile = data / "profiles" / ("a" * 24)
    profile.mkdir(parents=True)
    with pytest.raises(ConfigUnavailable):  # profile 的 store 也看設定目錄的標記
        ConfigStore(profile / "config.json")
    assert sorted(p.name for p in data.rglob("*")) == sorted([MARKER_NAME, "profiles", "a" * 24])
    unrelated = tmp_path / "elsewhere" / "config.json"
    ConfigStore(unrelated)  # 設定目錄以外的 store 不受影響
    assert unrelated.exists()
    (data / MARKER_NAME).unlink()
    ConfigStore()
    assert (data / "config.json").exists()


def test_marker_detection_fails_closed(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    config = data / "config.json"
    marker = data / MARKER_NAME
    assert marker_path(config) == marker
    assert restore_in_progress(config) is False
    marker.mkdir()
    assert restore_in_progress(config) is True  # 任何種類的項目都算
    marker.rmdir()
    marker.symlink_to(tmp_path / "does-not-exist")
    assert restore_in_progress(config) is True  # 懸空 symlink 也算
    marker.unlink()
    assert restore_in_progress(tmp_path / "not-created-yet" / "config.json") is False  # 全新安裝
    monkeypatch.delenv("MCP_ADMIN_CONFIG", raising=False)
    assert marker_path() == Path("/data") / MARKER_NAME
    if os.geteuid() != 0:
        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(0)
        try:
            assert restore_in_progress(locked / "config.json") is True  # 確認不了＝當作存在
        finally:
            locked.chmod(0o700)


# ---- 真的 uvicorn（與 Dockerfile CMD 相同的啟動方式，預設 --lifespan auto） ------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _uvicorn(target: str, data: Path, port: int, app_dir: Path | None = None) -> subprocess.Popen:
    env = {**os.environ, "MCP_ADMIN_CONFIG": str(data / "config.json"), "PYTHONDONTWRITEBYTECODE": "1",
           "JWT_SECRET": "synthetic-jwt-secret-for-restore-marker-tests-0123456789"}
    command = [sys.executable, "-m", "uvicorn", target, "--host", "127.0.0.1", "--port", str(port)]
    if app_dir is not None:
        command += ["--app-dir", str(app_dir)]
    return subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _watch_until_exit(proc: subprocess.Popen, port: int, timeout: float = 60) -> bool:
    """行程結束前不停嘗試連線；回傳有沒有連上過。"""
    connected = False
    deadline = time.monotonic() + timeout
    while proc.poll() is None and time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                connected = True
        except OSError:
            pass
        time.sleep(0.02)
    return connected


def _probe_app(tmp_path: Path) -> Path:
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "restore_marker_probe_app.py").write_text(
        "from mcp_admin_core import create_app\napp = create_app(title='restore marker probe')\n")
    return app_dir


def test_uvicorn_exits_3_without_listening_and_without_touching_config(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    sentinel = tmp_path / "child-started"
    child = {"command": sys.executable, "port": _free_port(),
             "args": ["-c", "import pathlib,sys,time; pathlib.Path(sys.argv[1]).touch(); time.sleep(120)", str(sentinel)]}
    config = data / "config.json"
    _write(config, {**legacy_config(), "mcp_server": child}, mode=0o600)
    (data / MARKER_NAME).write_text("x")
    before = _snapshot(config)
    app_dir = _probe_app(tmp_path)
    port = _free_port()
    proc = _uvicorn("restore_marker_probe_app:app", data, port, app_dir)
    connected = _watch_until_exit(proc, port)
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 3, err[-2000:]
    assert not connected  # 從來沒有開始監聽：沒有任何流量
    assert "Application startup failed. Exiting." in err
    assert "拒絕啟動" in out and "拒絕啟動" in err  # 我們的 stdout 一行＋uvicorn 記下的 startup.failed 訊息
    assert _snapshot(config) == before and sorted(os.listdir(data)) == sorted(["config.json", MARKER_NAME])
    assert not sentinel.exists()

    # 對照：標記移除後同一份設定照常啟動、拉起子行程、改寫設定（證明上面的斷言抓得到）。
    (data / MARKER_NAME).unlink()
    proc = _uvicorn("restore_marker_probe_app:app", data, port, app_dir)
    try:
        deadline = time.monotonic() + 60
        status = None
        while time.monotonic() < deadline and status != 200:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as response:
                    status = response.status
            except OSError:
                time.sleep(0.1)
        assert status == 200
        deadline = time.monotonic() + 30
        while not sentinel.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert sentinel.exists()
        assert json.loads(config.read_text())["mcp_auth_token"]
    finally:
        proc.terminate()
        proc.communicate(timeout=30)


def test_uvicorn_real_product_app_refuses_before_its_seed_defaults(tmp_path):
    """odoo 的 lifespan 先跑 seed_defaults（會補寫入開關），標記存在時它也不能跑。"""
    data = tmp_path / "data"
    data.mkdir()
    config = data / "config.json"
    _write(config, legacy_config(_free_port()), mode=0o600)
    (data / MARKER_NAME).write_text("x")
    before = _snapshot(config)
    port = _free_port()
    proc = _uvicorn("odoo_mcp_admin.main:app", data, port)
    connected = _watch_until_exit(proc, port)
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 3, err[-2000:]
    assert not connected
    assert _snapshot(config) == before and sorted(os.listdir(data)) == sorted(["config.json", MARKER_NAME])
