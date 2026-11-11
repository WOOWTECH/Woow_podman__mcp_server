"""``/healthz`` 的 ``backend`` 欄位：子行程起得來、但後端連線沒設定（第五輪，負責人 2026-10-27）。

背景：Odoo、LiteLLM、Hermes、n8n（文件模式）沒有存後端連線時子行程照樣在跑、照樣列出工具（round3 staging：odoo 41、
litellm 40、hermes 11、n8n 7 支），``/healthz`` 的 ``mcp`` 是 ``running``，平台看不出「其實還沒接上後端、工具呼叫會失敗」。
現在宣告了 ``LaunchPolicy.backend_connection`` 的產品，``/healthz`` 多回 ``"backend": "configured"｜"missing"``，平台據此顯示
較輕的提示；``mcp`` 的三個值與判斷不變（#88），沒宣告的產品沒有這個欄位，讀不到設定時也不帶，probe 一律 200。
"""
from __future__ import annotations

import asyncio
import json
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

HOST = "paas-cs-test0002.woowtech.io"
FIELDS = (("app_url", "App 網址"), ("app_key", "App API key"))
COMPLETE = {"app_url": "https://app.example.com", "app_key": "app-key-SECRET-0123"}
_DEFAULT = object()


def _policy(backend=FIELDS, required=()):
    from mcp_admin_core.launch_policy import LaunchPolicy

    return LaunchPolicy(
        command="app-mcp",
        args=("--port", "{port}"),
        env_prefix="APP_",
        connection_keys=frozenset({"app_url", "app_key", "app_user"}),
        required_connection=required,
        backend_connection=backend,
    )


class _RunningPM:
    """子行程在跑（只給 /healthz 看狀態；不 spawn 任何東西）。"""

    is_running = True


@pytest.fixture
def healthz(tmp_config_path, monkeypatch):
    """``healthz(connection=..., policy=..., running=..., raw=...)`` → TestClient（不進 lifespan、不 spawn 子行程）。

    預設政策＝兩個後端連線欄位、沒有必要欄位；``policy=None`` 是沒有政策。``raw`` 直接寫成設定檔內容（例如壞掉的 JSON）。
    """
    import mcp_admin_core.config.store as store_mod
    import mcp_admin_core.process as process_mod
    from mcp_admin_core import create_app
    from mcp_admin_core.launch_policy import get_launch_policy, set_launch_policy

    previous = get_launch_policy()

    def _make(*, connection=None, policy=_DEFAULT, running=False, raw=None, app=None):
        if raw is not None:
            tmp_config_path.write_text(raw)
        else:
            cfg = json.loads(json.dumps(store_mod._DEFAULT_CONFIG))
            cfg["connection"] = dict(connection or {})
            tmp_config_path.write_text(json.dumps(cfg))
        monkeypatch.setenv("MCP_ADMIN_CONFIG", str(tmp_config_path))
        monkeypatch.setattr(store_mod, "_instance", None)
        monkeypatch.setattr(process_mod, "_instance", _RunningPM() if running else None)
        if app is None:
            app = create_app(title="App MCP Admin", launch_policy=_policy() if policy is _DEFAULT else policy)
        else:
            set_launch_policy(policy)  # 真的產品 app：別的測試以 create_app() 組 app 時會把全域政策換掉
        return TestClient(app, base_url=f"https://{HOST}")

    yield _make
    set_launch_policy(previous)


@pytest.mark.parametrize(
    ("connection", "running", "mcp", "backend"),
    [
        ({}, True, "running", "missing"),  # 新開的服務：子行程照樣在跑、列出工具，但沒有後端
        ({}, False, "not_running", "missing"),
        ({"app_url": "https://app.example.com"}, True, "running", "missing"),  # 只填一部分（例如平台預填了網址）
        ({"app_key": "k"}, True, "running", "missing"),
        ({"app_url": "   ", "app_key": "k"}, True, "running", "missing"),  # 只有空白＝沒填
        ({"app_url": None, "app_key": "k"}, True, "running", "missing"),
        (COMPLETE, True, "running", "configured"),
        (COMPLETE, False, "not_running", "configured"),  # 與子行程在不在跑無關
    ],
)
def test_healthz_reports_whether_the_backend_connection_is_filled_in(healthz, connection, running, mcp, backend):
    r = healthz(connection=connection, running=running).get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "mcp": mcp, "backend": backend}


@pytest.mark.parametrize(
    ("policy_kind", "connection", "running", "mcp"),
    [
        ("no_policy", {}, True, "running"),
        ("no_policy", COMPLETE, False, "not_running"),
        ("nothing_declared", {}, True, "running"),
        ("nothing_declared", {}, False, "not_running"),
        ("required_only", {}, True, "not_configured"),  # Nextcloud 這種：缺必要欄位，照 #88 報 not_configured
        ("required_only", {"app_user": "u"}, True, "running"),
        ("required_only", {"app_user": "u"}, False, "not_running"),
    ],
)
def test_products_without_backend_fields_have_no_backend_key(healthz, policy_kind, connection, running, mcp):
    """沒宣告後端連線欄位的產品，/healthz 與 #88 逐字相同：只有 status 與 mcp。"""
    policy = {
        "no_policy": None,
        "nothing_declared": _policy(backend=()),
        "required_only": _policy(backend=(), required=(("app_user", "App 帳號"),)),
    }[policy_kind]
    r = healthz(connection=connection, running=running, policy=policy).get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "mcp": mcp}


@pytest.mark.parametrize(
    ("connection", "running", "mcp", "backend"),
    [
        ({}, True, "not_configured", "missing"),  # 看門狗重啟嘗試中：缺必要欄位照舊 not_configured
        (COMPLETE, True, "not_configured", "configured"),  # 後端欄位齊全也不會讓缺必要欄位變成 running
        ({"app_user": "u"}, True, "running", "missing"),  # 後端欄位沒填也不會讓 mcp 變成 not_configured
        ({"app_user": "u", **COMPLETE}, True, "running", "configured"),
        ({"app_user": "u", **COMPLETE}, False, "not_running", "configured"),
    ],
)
def test_backend_does_not_change_the_mcp_field(healthz, connection, running, mcp, backend):
    """必要欄位與後端欄位都宣告時，兩個欄位各算各的：mcp 只看 required_connection 與子行程（#88），backend 只看後端欄位。"""
    policy = _policy(required=(("app_user", "App 帳號"),))
    r = healthz(connection=connection, running=running, policy=policy).get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "mcp": mcp, "backend": backend}


def test_backend_is_left_out_when_the_config_is_unreadable(healthz):
    """設定檔壞掉（讀不到）：不知道有沒有設定，backend 不帶；mcp 只看子行程，probe 照樣 200。"""
    r = healthz(raw="{not json", running=True).get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "mcp": "running"}


def test_backend_is_left_out_when_reading_the_config_fails(healthz, monkeypatch):
    from mcp_admin_core.config import get_config_store
    from mcp_admin_core.config.store import ConfigUnavailable

    c = healthz(connection=COMPLETE)

    async def unreadable(*args, **kwargs):
        raise ConfigUnavailable("Configuration state unavailable")

    monkeypatch.setattr(get_config_store(), "get", unreadable)  # 還沒有快照：/healthz 會去讀檔
    r = c.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "mcp": "not_running"}


def test_healthz_does_not_wait_for_a_slow_config_write(healthz, monkeypatch):
    """寫檔卡住時（例如 Longhorn／iSCSI 卡住）/healthz 照樣立刻回 200，backend 是寫入前、最後一次完成的版本，寫完才換。"""
    from mcp_admin_core.config import get_config_store

    c = healthz(running=True)
    store = get_config_store()
    release = threading.Event()
    real_write = store._write_sync

    def slow_write(data):
        if not release.wait(10):
            raise TimeoutError("test did not release the config write")
        real_write(data)

    monkeypatch.setattr(store, "_write_sync", slow_write)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=c.app), base_url=f"https://{HOST}") as client:
            before = await client.get("/healthz")  # 第一次讀會載入設定（lifespan 也一定先載入過）
            writer = asyncio.create_task(store.patch("connection", COMPLETE))  # 管理員存連線，寫檔卡住
            try:
                while not store._lock.locked():
                    await asyncio.sleep(0.01)
                started = time.monotonic()
                during = await asyncio.wait_for(client.get("/healthz"), 2)
                elapsed = time.monotonic() - started
            finally:
                release.set()
                await writer
            after = await client.get("/healthz")
        return before, during, elapsed, after

    before, during, elapsed, after = asyncio.run(scenario())
    assert before.json() == {"status": "ok", "mcp": "running", "backend": "missing"}
    assert during.status_code == 200 and elapsed < 1, elapsed
    assert during.json() == {"status": "ok", "mcp": "running", "backend": "missing"}  # 寫入前的版本
    assert after.json() == {"status": "ok", "mcp": "running", "backend": "configured"}


def test_healthz_without_a_snapshot_waits_only_briefly_then_leaves_backend_out(healthz):
    """還沒有快照又剛好碰上別人拿著鎖：最多等 PROBE_CONFIG_WAIT_SECONDS，逾時就不帶 backend、照樣 200。"""
    from mcp_admin_core import setup_status
    from mcp_admin_core.config import get_config_store

    c = healthz(connection=COMPLETE)  # 不進 lifespan：還沒有任何請求載入過設定
    store = get_config_store()

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=c.app), base_url=f"https://{HOST}") as client:
            async with store._lock:  # 例如正在寫檔
                started = time.monotonic()
                response = await asyncio.wait_for(client.get("/healthz"), 3)
                return response, time.monotonic() - started

    response, elapsed = asyncio.run(scenario())
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "mcp": "not_running"}
    assert setup_status.PROBE_CONFIG_WAIT_SECONDS <= elapsed < setup_status.PROBE_CONFIG_WAIT_SECONDS + 1, elapsed


def test_healthz_reads_the_connection_once(healthz, monkeypatch):
    """mcp 與 backend 出自同一份設定快照：/healthz 只讀一次設定。"""
    from mcp_admin_core import setup_status

    calls = []
    real = setup_status.probe_connection

    async def counting(store=None):
        calls.append(1)
        return await real(store)

    monkeypatch.setattr(setup_status, "probe_connection", counting)
    c = healthz(connection={"app_user": "u"}, running=True, policy=_policy(required=(("app_user", "App 帳號"),)))
    assert c.get("/healthz").json() == {"status": "ok", "mcp": "running", "backend": "missing"}
    assert len(calls) == 1


def test_backend_field_has_no_field_names_labels_or_values(healthz):
    """/healthz 是公開的：只有 configured／missing，沒有缺哪個欄位、標籤或任何值。"""
    r = healthz(connection={"app_url": "https://backend.internal.example:8443/x", "app_key": ""}, running=True).get("/healthz")
    assert r.json() == {"status": "ok", "mcp": "running", "backend": "missing"}
    for leaked in ("app_url", "app_key", "App 網址", "App API key", "backend.internal", "8443"):
        assert leaked not in r.text, leaked
    r = healthz(connection=COMPLETE, running=True).get("/healthz")
    assert r.json() == {"status": "ok", "mcp": "running", "backend": "configured"}
    assert COMPLETE["app_key"] not in r.text and COMPLETE["app_url"] not in r.text


# ---------------------------------------------------------------------------
# 真的產品 app（vk、opendesign 沒有自己的 app 測試，在這裡一起驗）
# ---------------------------------------------------------------------------


def _product(name):
    import importlib

    module = importlib.import_module(f"{name}_mcp_admin.main")
    return module.app, module.LAUNCH_POLICY


_FILLED = {
    "odoo": {"odoo_url": "https://odoo.example.com", "odoo_db": "prod", "odoo_username": "bot", "odoo_password": "k"},
    "litellm": {"base_url": "http://svc-x-litellm:4000", "master_key": "sk-test"},
    "hermes": {"gateway_url": "https://h.example.com/gateway", "gateway_api_key": "g", "dashboard_url": "https://h.example.com",
               "dashboard_username": "admin", "dashboard_password": "p"},
    "n8n": {"n8n_api_url": "https://n8n.example.com", "n8n_api_key": "n"},
    "vk": {"vibe_backend_url": "http://vk-host:3000"},
    "opendesign": {"od_api_base": "http://svc-x:7457"},
}


@pytest.mark.parametrize("name", sorted(_FILLED))
def test_each_product_reports_its_backend(healthz, name):
    app, policy = _product(name)
    assert healthz(connection={}, policy=policy, app=app).get("/healthz").json() == {
        "status": "ok", "mcp": "not_running", "backend": "missing"}
    assert healthz(connection=_FILLED[name], policy=policy, app=app).get("/healthz").json() == {
        "status": "ok", "mcp": "not_running", "backend": "configured"}


@pytest.mark.parametrize(("name", "connection", "mcp"), [
    ("nextcloud", {}, "not_configured"),
    ("nextcloud", {"base_url": "https://cloud.example.com", "username": "u", "app_password": "p"}, "not_running"),
    ("k8s", {}, "not_running"),
    ("k8s", {"kubeconfig": "apiVersion: v1", "in_cluster": "false"}, "not_running"),
])
def test_products_that_declare_no_backend_fields_have_no_backend_key(healthz, name, connection, mcp):
    app, policy = _product(name)
    assert healthz(connection=connection, policy=policy, app=app).get("/healthz").json() == {"status": "ok", "mcp": mcp}
