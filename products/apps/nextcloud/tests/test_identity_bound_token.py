"""連線身分綁定 MCP token：換使用者或網址時，舊的 MCP 網址在同一次原子寫入被撤銷並換新（2026-11-14 實測 F-3）。

修正前：B 帳號的 ``username`` 由 bob 改成 carol（換 carol 的 app password）並套用，B 的**同一個 MCP token** 立刻讀到 carol 的
資料；token 歷史沒有紀錄；``restart:false`` 時只存檔、子行程繼續用舊身分直到重啟。Odoo 早就有「token 永遠綁一個後端身分」
（``LaunchPolicy.identity_connection_keys`` ＋ ``save_bound_connection``），Nextcloud 比照：``base_url``、``username`` 是身分，
``app_password`` 不是（只換密碼不換 token）。

這個檔案用**真的 ConfigStore、真的 McpProcessManager、真的 admin app（含認證、profile 選取、If-Match）**，只把子行程換成
一支假的 ``nextcloud-mcp-server``（``FAKE_CHILD``：在 ``--port`` 上回答自己的身分，並把 start／stop 與設定檔寫入的先後順序記
在事件檔裡）。所以「舊的 MCP 網址之後 403、新網址讀到新帳號」「先停舊子行程、再寫入新連線與新 token」「寫入失敗時舊 token 與
舊子行程完好」都是實際發生的事，不是 mock 的回傳值。不連任何外部網路。
"""
from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import logging
import socket
import sys
import textwrap
import time
from pathlib import Path

import httpx
import pytest
from management_client import async_revision_headers
from mcp_admin_core import capabilities
from mcp_admin_core import process as process_mod
from mcp_admin_core.config import store as store_mod
from mcp_admin_core.connection_apply import SSRF_BLOCKED
from mcp_admin_core.launch_policy import get_launch_policy, set_launch_policy
from mcp_admin_core.mcp_access import AccessChanged, AccessLease
from mcp_admin_core.profiles import get_profile_registry

from nextcloud_mcp_admin import log_redaction
from nextcloud_mcp_admin.main import LAUNCH_POLICY, app
from nextcloud_mcp_admin.routers import config as config_router

from .fake_nextcloud import BASE_URL  # 有子路徑的網址（https://cloud.example.com/nc），正規化測試用得到

OLD_URL = BASE_URL  # https://cloud.example.com/nc
NEW_URL = "https://cloud-two.example.com/nc"
OLD_USER = "owner-bob-7f3a"
NEW_USER = "owner-carol-91c2"
OLD_PW = "Bob-App-Password-0001"
NEW_PW = "Carol-App-Password-0002"
OLD_TOKEN = "owner-old-token-0001"
OLD = {"base_url": OLD_URL, "username": OLD_USER, "app_password": OLD_PW}
#: 這個檔案裡不可以出現在 token 歷史、回應與 log 的值。
SECRET_VALUES = (OLD_USER, NEW_USER, OLD_PW, NEW_PW, "cloud-two.example.com", "cloud.example.com")

# 假的 nextcloud-mcp-server：綁 --port、回答「我是用哪個身分啟動的」，並把 start／stop 記進事件檔（FAKE_CHILD_EVENTS）。
FAKE_CHILD = textwrap.dedent('''
    import json, os, signal, sys, time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    port = int(sys.argv[sys.argv.index("--port") + 1])
    events = os.environ["FAKE_CHILD_EVENTS"]
    user = os.environ.get("NEXTCLOUD_MCP_USERNAME")
    base_url = os.environ.get("NEXTCLOUD_MCP_BASE_URL")

    def record(event, **extra):
        with open(events, "a") as fh:
            fh.write(json.dumps({"event": event, "pid": os.getpid(), "port": port, "t": time.time(), **extra}) + "\\n")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"user": user, "base_url": base_url, "pid": os.getpid()}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        do_POST = do_GET
        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)

    def stop(*_):
        record("stop")
        os._exit(0)

    signal.signal(signal.SIGTERM, stop)
    record("start", user=user, base_url=base_url)
    server.serve_forever()
''')


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _free_port_pair() -> int:
    """連續兩個目前沒人用的埠：額外帳號的子行程埠從 ``profiles.FIRST_PROFILE_PORT`` 起依序分配，固定的 18000 在同一台機器上可能被別的行程佔著。"""
    for _ in range(100):
        with socket.socket() as first:
            first.bind(("127.0.0.1", 0))
            base = first.getsockname()[1]
            if base >= 65534:
                continue
            with socket.socket() as second:
                try:
                    second.bind(("127.0.0.1", base + 1))
                except OSError:
                    continue
                return base
    raise RuntimeError("找不到連續兩個空閒埠")


def payload(**over) -> dict:
    """Connection 頁送的三欄＋restart；app_password 留空＝沿用既有值。"""
    return {"base_url": OLD_URL, "username": OLD_USER, "app_password": "", "restart": True, **over}


class Stack:
    """真的 admin app＋ConfigStore＋McpProcessManager，子行程是 ``FAKE_CHILD``。"""

    def __init__(self, client, store, path, events_path, port):
        self.client = client
        self.store = store
        self.path = path
        self.events_path = events_path
        self.port = port
        self.fail_write_when = None
        self.profile_ids: list[str] = []  # 建立過的額外帳號：結束時要清掉它們在 log 遮罩登記的密碼（全域狀態）

    # -- 讀 -------------------------------------------------------------------
    def config(self, path: Path | None = None) -> dict:
        return json.loads((path or self.path).read_text())

    def events(self) -> list[dict]:
        return [json.loads(line) for line in self.events_path.read_text().splitlines() if line.strip()]

    @property
    def pm(self):
        return process_mod.get_default_process_manager()

    def pid(self, pm=None) -> int | None:
        return (pm or self.pm).child_pid

    async def wait_routable(self, pm=None, port: int | None = None, timeout: float = 15.0) -> None:
        """子行程已經 listen 且通過管理程序的 OS 身分檢查：閘道才會轉發（blocker 3）。"""
        pm, port = pm or self.pm, port or self.port
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pm.is_running and pm.forward_generation(port) is not None:
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"fake child on {port} never became routable; events={self.events()}")

    async def wait_event(self, kind: str, timeout: float = 10.0, **match) -> dict:
        """等假子行程把某個事件寫進事件檔（spawn 回來時它還沒來得及寫）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = only(self.events(), kind, **match)
            if found:
                return found[-1]
            await asyncio.sleep(0.05)
        raise AssertionError(f"等不到 {kind} {match}；實際：{[(e['event'], e.get('user')) for e in self.events()]}")

    # -- 寫 -------------------------------------------------------------------
    async def put(self, route="/api/config/connection", *, headers=None, read_path="/api/config", **body):
        headers = await async_revision_headers(self.client, headers, read_path=read_path)
        return await self.client.put(route, json=body, headers=headers)

    async def shared_put(self, body: dict, *, headers=None):
        return await self.put("/api/settings/connection", headers=headers, read_path="/api/settings", **body)

    # -- 用 token 連 ----------------------------------------------------------
    async def probe(self, token: str) -> httpx.Response:
        return await self.client.get(f"/private_{token}/whoami")

    async def whoami(self, token: str) -> dict:
        reply = await self.probe(token)
        assert reply.status_code == 200, (reply.status_code, reply.text)
        return reply.json()


@pytest.fixture(autouse=True)
def _clear_secrets():
    log_redaction.set_secrets(None)
    yield
    log_redaction.set_secrets(None)


@pytest.fixture
async def make_stack(tmp_path, monkeypatch):
    """工廠：``await make_stack(connection=…, token=…, revoked=…, start=…)``。測試結束時停掉所有子行程、還原全域政策。"""
    previous_policy = get_launch_policy()
    started: list = []

    async def ready(_snapshot):  # 子行程是假的，不會說 MCP：就緒探測直接放行（OS 身分檢查照舊真的跑）
        return []

    monkeypatch.setattr(capabilities, "discover_tools", ready)
    monkeypatch.setattr("mcp_admin_core.profiles._memory_limit_mib", lambda: 1024)  # 不依賴測試機器的 cgroup

    async def _make(connection=None, *, token: str | None = OLD_TOKEN, revoked: bool = False, start: bool = True):
        script = tmp_path / "fake_nextcloud_child.py"
        script.write_text(FAKE_CHILD)
        events_path = tmp_path / "child-events.jsonl"
        events_path.touch()
        monkeypatch.setenv("FAKE_CHILD_EVENTS", str(events_path))
        port = _free_port()
        cfg = copy.deepcopy(store_mod._DEFAULT_CONFIG)
        cfg["connection"] = copy.deepcopy(OLD if connection is None else connection)
        cfg["mcp_server"] = {**cfg["mcp_server"], "port": port, "env_prefix": "NEXTCLOUD_MCP_"}
        path = tmp_path / "config.json"
        path.write_text(json.dumps(cfg))
        monkeypatch.setenv("MCP_ADMIN_CONFIG", str(path))
        monkeypatch.setattr(store_mod, "_instance", None)
        monkeypatch.setattr(process_mod, "_instance", None)
        store = store_mod.get_config_store()
        if token is not None:
            await store.change_mcp_token(token, action="set")
        if revoked:
            await store.change_mcp_token(None, action="revoke")

        # 與正式組裝的 LAUNCH_POLICY 逐欄相同（身分欄位、必填欄位、正規化…），只把子行程換成假的。
        set_launch_policy(dataclasses.replace(LAUNCH_POLICY, command=sys.executable,
                                              args=(str(script), "--port", "{port}")))
        stack = Stack(None, store, path, events_path, port)

        real_write = store_mod.ConfigStore._write_sync

        def recording_write(self, data):
            conn = data.get("connection") or {}
            with open(events_path, "a") as fh:  # 先記下「這一刻要寫」，再決定成功或失敗
                fh.write(json.dumps({"event": "config_write", "t": time.time(), "profile": Path(self.path).parent.name,
                                     "user": conn.get("username"), "base_url": conn.get("base_url"),
                                     "token": data.get("mcp_auth_token")}) + "\n")
            if stack.fail_write_when is not None and stack.fail_write_when(self, data):
                raise OSError("simulated disk failure")
            return real_write(self, data)

        monkeypatch.setattr(store_mod.ConfigStore, "_write_sync", recording_write)

        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
        login = await client.post("/api/auth/login", json={"password": "admin"})
        assert login.status_code == 200, login.text
        client.headers["Authorization"] = "Bearer " + login.json()["token"]
        stack.client = client
        started.append(stack)
        if start:
            assert await stack.pm.start() is True
            await stack.wait_routable()
        return stack

    yield _make

    registry = get_profile_registry()
    for stack in started:
        await stack.client.aclose()
        for profile_id in stack.profile_ids:  # 成功的存檔登記了該帳號的 app password；不清掉會讓 test_log_redaction 的計數多出來
            log_redaction.remove_profile_secrets(profile_id)
    for profile_id, manager in [*registry._managers.items(), ("default", process_mod.get_default_process_manager())]:
        try:
            with registry.activate(profile_id):
                await manager.stop()
        except Exception:  # noqa: BLE001 — 收尾不能讓一個壞掉的 manager 擋住其他子行程的清理
            pass
    set_launch_policy(previous_policy)


def only(events: list[dict], kind: str, **match) -> list[dict]:
    return [e for e in events if e["event"] == kind and all(e.get(k) == v for k, v in match.items())]


def index_of(events: list[dict], kind: str, **match) -> int:
    for position, event in enumerate(events):
        if event["event"] == kind and all(event.get(k) == v for k, v in match.items()):
            return position
    raise AssertionError(f"沒有 {kind} {match} 這個事件；實際：{[(e['event'], e.get('user')) for e in events]}")


# ---------------------------------------------------------------------------
# 宣告：身分欄位是 base_url、username；app_password 不是
# ---------------------------------------------------------------------------


def test_launch_policy_declares_base_url_and_username_as_the_identity():
    assert LAUNCH_POLICY.identity_connection_keys == frozenset({"base_url", "username"})
    assert LAUNCH_POLICY.identity_connection_keys <= LAUNCH_POLICY.connection_keys  # 核心也會擋「不是連線鍵的身分欄位」
    assert "app_password" not in LAUNCH_POLICY.identity_connection_keys  # 只換密碼不換 token
    assert config_router.IDENTITY_KEYS == LAUNCH_POLICY.identity_connection_keys
    # 共用的 PUT /api/settings/connection 用同一套正規化，兩條路徑比的是同一種寫法
    assert LAUNCH_POLICY.normalize_connection is config_router.normalize_connection_update


def test_update_response_model_carries_token_rotated():
    field = config_router.ConnectionUpdateResponse.model_fields["token_rotated"]
    assert field.default is False
    assert config_router.ConnectionUpdateResponse(success=True, message="x").token_rotated is False


# ---------------------------------------------------------------------------
# 換使用者／網址：舊網址撤銷、新網址可用、子行程一定重啟（restart:false 也一樣）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("restart", [True, False], ids=["restart-true", "restart-false"])
@pytest.mark.parametrize("changed", [{"username": NEW_USER, "app_password": NEW_PW}, {"base_url": NEW_URL},
                                     {"base_url": NEW_URL, "username": NEW_USER, "app_password": NEW_PW}],
                         ids=["username", "base_url", "both"])
async def test_identity_change_revokes_the_old_url_and_issues_a_new_one(make_stack, changed, restart):
    s = await make_stack()
    assert (await s.whoami(OLD_TOKEN))["user"] == OLD_USER
    old_pid = s.pid()
    lease = await AccessLease.acquire(s.store, OLD_TOKEN)  # 舊 token 的一條進行中的 session／串流
    before = s.config()

    reply = await s.put(**payload(restart=restart, **changed))

    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert body["token_rotated"] is True and body["saved"] is True
    # restart:false 也要重啟（舊身分不可以繼續跑）。apply_state 與 message 在子行程探測確認之前會被核心改寫成通用文字，
    # 所以這裡看不會被改寫的 restarted；提示文字用假 PM 的 test_the_response_message_… 驗證。
    assert body["status"] == "ok" and body["restarted"] is True

    after = s.config()
    new_token = after["mcp_auth_token"]
    assert new_token and new_token != OLD_TOKEN and after["mcp_auth_revoked"] is False
    assert new_token not in reply.text and OLD_TOKEN not in reply.text  # 回應只說換了，不給 token
    assert after["mcp_auth_generation"] != before["mcp_auth_generation"]
    history = after["token_history"]
    assert len(history) == len(before["token_history"]) + 1 and history[0]["action"] == "backend_identity_change"
    dumped = json.dumps(history)
    assert OLD_TOKEN not in dumped and not [v for v in SECRET_VALUES if v in dumped]  # 歷史只有遮罩過的前一個 token
    assert after["connection"]["base_url"] == changed.get("base_url", OLD_URL)
    assert after["connection"]["username"] == changed.get("username", OLD_USER)

    # 舊網址：403；新網址：讀到的是新身分（修正前是舊 token 直接讀到新使用者的資料）
    assert (await s.probe(OLD_TOKEN)).status_code == 403
    await s.wait_routable()
    seen = await s.whoami(new_token)
    assert seen["user"] == changed.get("username", OLD_USER) and seen["base_url"] == changed.get("base_url", OLD_URL)
    assert s.pid() not in (None, old_pid) and seen["pid"] == s.pid()
    with pytest.raises(AccessChanged):  # 舊 session 被中止
        lease.check()


@pytest.mark.parametrize("restart", [True, False], ids=["restart-true", "restart-false"])
async def test_changing_only_the_app_password_keeps_the_token(make_stack, restart):
    s = await make_stack()
    old_pid = s.pid()
    before = s.config()
    reply = await s.put(**payload(app_password=NEW_PW, restart=restart))
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert body["token_rotated"] is False and "Tokens" not in body["message"]
    after = s.config()
    assert after["connection"]["app_password"] == NEW_PW
    assert after["mcp_auth_token"] == OLD_TOKEN and after["mcp_auth_generation"] == before["mcp_auth_generation"]
    assert after["token_history"] == before["token_history"]
    assert (await s.probe(OLD_TOKEN)).status_code != 403
    if restart:  # 既有行為不退步：要求套用才重啟
        assert body["restarted"] is True and s.pid() != old_pid
    else:
        assert body["apply_state"] == "not_requested" and body["restarted"] is False and s.pid() == old_pid
        assert only(s.events(), "stop") == []


# ---------------------------------------------------------------------------
# base_url 用「正規化後」的值比：寫法不同不算換身分
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spelling", [
    "https://cloud.example.com/nc/",                                  # 尾斜線
    "https://cloud.example.com/nc//",                                 # 多個尾斜線
    "  https://cloud.example.com/nc  ",                               # 前後空白
    "HTTPS://Cloud.Example.COM/nc",                                   # 協定與主機名大小寫
    "https://cloud.example.com/nc/index.php/apps/files/",             # 誤貼的頁面路徑會被 normalize 切掉
    "https://cloud.example.com/nc/remote.php/dav/files/owner/",
    "https://cloud.example.com:443/nc",                               # 預設埠
])
async def test_a_different_spelling_of_the_same_url_is_not_an_identity_change(make_stack, spelling):
    s = await make_stack()
    before = s.config()
    reply = await s.put(**payload(base_url=spelling, restart=False))
    assert reply.status_code == 200, reply.text
    assert reply.json()["token_rotated"] is False
    after = s.config()
    assert after["mcp_auth_token"] == OLD_TOKEN and after["token_history"] == before["token_history"]
    assert after["connection"]["base_url"] == OLD_URL  # 存下來的仍是原來那一個寫法
    assert (await s.probe(OLD_TOKEN)).status_code != 403


@pytest.mark.parametrize("different", [
    "http://cloud.example.com/nc",             # 協定
    "https://cloud.example.com:8443/nc",       # 埠
    "https://cloud.example.com",               # 子路徑不同（可能是另一個 Nextcloud）
    "https://cloud.example.com/NC",            # 路徑大小寫：伺服器可能分大小寫，視為不同（寧可多換一次，不漏換）
    "https://other.example.com/nc",            # 主機
])
async def test_a_really_different_url_is_an_identity_change(make_stack, different):
    s = await make_stack()
    reply = await s.put(**payload(base_url=different, restart=False))
    assert reply.status_code == 200, reply.text
    assert reply.json()["token_rotated"] is True
    assert s.config()["mcp_auth_token"] != OLD_TOKEN
    assert (await s.probe(OLD_TOKEN)).status_code == 403


async def test_a_legacy_unnormalized_stored_url_is_not_rotated_by_resaving_the_form(make_stack):
    """舊存檔（共用路徑以前原樣存、或當時的寫法）還留著大寫與尾斜線時，Connection 頁把它原樣送回來也不算換身分。"""
    legacy = "https://Cloud.Example.com/nc/"
    s = await make_stack({**OLD, "base_url": legacy})
    reply = await s.put(**payload(base_url=legacy, app_password=NEW_PW, restart=False))
    assert reply.status_code == 200, reply.text
    assert reply.json()["token_rotated"] is False
    after = s.config()
    assert after["mcp_auth_token"] == OLD_TOKEN and after["connection"]["base_url"] == legacy
    assert after["connection"]["app_password"] == NEW_PW


# ---------------------------------------------------------------------------
# 第一次設定、已撤銷、沒有 token：不換
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("initial", [
    {},                                                                        # 全新
    {"base_url": OLD_URL, "username": "", "app_password": ""},                 # PaaS seed：只有網址
    {"base_url": "", "username": OLD_USER, "app_password": ""},                # 只有帳號
    {"base_url": OLD_URL, "username": "   ", "app_password": ""},              # 只有空白
], ids=["empty", "seed-url-only", "user-only", "blank-user"])
async def test_first_time_setup_keeps_the_issued_url(make_stack, initial):
    s = await make_stack(initial, start=False)
    reply = await s.put(base_url=OLD_URL, username=OLD_USER, app_password=OLD_PW, restart=True)
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert body["token_rotated"] is False and "Tokens" not in body["message"]
    assert body["restarted"] is True  # 原本沒在跑，現在啟動
    after = s.config()
    assert after["mcp_auth_token"] == OLD_TOKEN
    assert "backend_identity_change" not in json.dumps(after["token_history"])
    await s.wait_routable()
    assert (await s.whoami(OLD_TOKEN))["user"] == OLD_USER


@pytest.mark.parametrize("state", ["revoked", "never-issued"])
async def test_a_revoked_or_missing_token_is_not_replaced_by_an_identity_change(make_stack, state):
    s = await make_stack(token=OLD_TOKEN if state == "revoked" else None, revoked=state == "revoked")
    before = s.config()
    assert not before["mcp_auth_token"] and before["mcp_auth_revoked"] is (state == "revoked")
    reply = await s.put(**payload(username=NEW_USER, app_password=NEW_PW, restart=False))
    assert reply.status_code == 200, reply.text
    assert reply.json()["token_rotated"] is False
    after = s.config()
    assert after["connection"]["username"] == NEW_USER
    assert after["mcp_auth_token"] == "" and after["mcp_auth_revoked"] is before["mcp_auth_revoked"]
    assert after["token_history"] == before["token_history"]  # 核心不替撤銷過的設定偷偷發 token
    assert (await s.probe(OLD_TOKEN)).status_code == 403


async def test_clearing_the_identity_rotates_and_filling_it_back_does_not(make_stack):
    """清空使用者也是「換掉身分」：不然可以先清空、再填別人的帳號，舊網址就切到新身分。清空之後再填，前一個身分已經不完整，不再換。"""
    s = await make_stack()
    cleared = await s.put(**payload(username="", restart=True))
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["token_rotated"] is True
    token_after_clear = s.config()["mcp_auth_token"]
    assert token_after_clear != OLD_TOKEN and (await s.probe(OLD_TOKEN)).status_code == 403
    filled = await s.put(**payload(username=NEW_USER, app_password=NEW_PW, restart=True))
    assert filled.status_code == 200, filled.text
    assert filled.json()["token_rotated"] is False
    assert s.config()["mcp_auth_token"] == token_after_clear
    await s.wait_routable()
    assert (await s.whoami(token_after_clear))["user"] == NEW_USER


# ---------------------------------------------------------------------------
# 原子性與順序：先停舊子行程，再一次寫入新連線＋新 token
# ---------------------------------------------------------------------------


async def test_the_old_child_is_stopped_before_the_new_identity_becomes_durable(make_stack):
    s = await make_stack()
    old_pid = s.pid()
    seen = len(s.events())
    reply = await s.put(**payload(username=NEW_USER, app_password=NEW_PW, restart=False))
    assert reply.status_code == 200, reply.text
    await s.wait_event("start", user=NEW_USER)
    events = s.events()[seen:]
    kinds = [(e["event"], e.get("user")) for e in events]
    stop = index_of(events, "stop", pid=old_pid)
    write = index_of(events, "config_write", user=NEW_USER)
    start = index_of(events, "start", user=NEW_USER)
    # 舊子行程先死（不再能用舊身分回答任何請求），新連線與新 token 在同一次寫入，最後才用新身分啟動
    assert stop < write < start, kinds
    assert events[write]["token"] != OLD_TOKEN
    assert len(only(events, "config_write", user=NEW_USER)) == 1  # 連線與 token 不是兩次寫入
    assert only(events, "stop") == [events[stop]]


async def test_a_failed_write_leaves_the_old_token_and_the_old_child_intact(make_stack):
    s = await make_stack()
    old_pid = s.pid()
    before = s.path.read_bytes()
    seen = len(s.events())
    s.fail_write_when = lambda _store, data: (data.get("connection") or {}).get("username") == NEW_USER
    reply = await s.put(**payload(username=NEW_USER, app_password=NEW_PW, restart=True))
    assert reply.status_code == 500 and reply.json()["detail"]["code"] == "config_save_failed", reply.text
    assert not [v for v in SECRET_VALUES if v in reply.text]
    s.fail_write_when = None
    assert s.path.read_bytes() == before  # 設定檔一個位元組都沒變：舊 token、舊連線、舊歷史
    assert (await s.probe(OLD_TOKEN)).status_code != 403
    # 舊子行程在寫入前被停掉；寫入失敗後用舊身分重新拉起來（不是新身分）
    events = s.events()[seen:]
    assert only(events, "stop", pid=old_pid) and only(events, "config_write", user=NEW_USER)
    assert only(events, "start", user=NEW_USER) == []
    await s.wait_routable()
    assert s.pm.is_running and s.pid() != old_pid
    assert (await s.whoami(OLD_TOKEN))["user"] == OLD_USER
    assert only(s.events()[seen:], "start")[-1]["user"] == OLD_USER


# ---------------------------------------------------------------------------
# 額外帳號（X-MCP-Profile）：只換自己的 token，不影響預設帳號與其他帳號
# ---------------------------------------------------------------------------


async def _make_profile(s: Stack, label: str, user: str) -> dict:
    created = await s.client.post("/api/profiles", json={"label": label})
    assert created.status_code == 201, created.text
    profile_id = created.json()["id"]
    s.profile_ids.append(profile_id)
    header = {"X-MCP-Profile": profile_id}
    profile_file = s.path.parent / "profiles" / profile_id / "config.json"
    # 新帳號第一次設定：沒有完整的前一個身分，不換 token
    token = s.config(profile_file)["mcp_auth_token"]
    first = await s.put(headers=header, base_url=OLD_URL, username=user, app_password=OLD_PW, restart=True)
    assert first.status_code == 200, first.text
    assert first.json()["token_rotated"] is False and s.config(profile_file)["mcp_auth_token"] == token
    manager = get_profile_registry().manager_for(profile_id)
    port = s.config(profile_file)["mcp_server"]["port"]
    await s.wait_routable(manager, port)
    return {"id": profile_id, "header": header, "file": profile_file, "token": token, "manager": manager, "port": port,
            "user": user}


async def test_an_extra_account_rotates_only_its_own_token(make_stack, monkeypatch):
    monkeypatch.setattr("mcp_admin_core.profiles.FIRST_PROFILE_PORT", _free_port_pair())
    s = await make_stack()
    b = await _make_profile(s, "帳號 B", "owner-b-user-11")
    c = await _make_profile(s, "帳號 C", "owner-c-user-22")
    default_pid, c_pid, b_pid = s.pid(), s.pid(c["manager"]), s.pid(b["manager"])
    default_before, c_before = s.config(), s.config(c["file"])
    assert (await s.whoami(b["token"]))["user"] == "owner-b-user-11"

    reply = await s.put(headers=b["header"], base_url=OLD_URL, username=NEW_USER, app_password=NEW_PW, restart=False)
    assert reply.status_code == 200, reply.text
    assert reply.json()["token_rotated"] is True

    b_after = s.config(b["file"])
    assert b_after["mcp_auth_token"] != b["token"] and b_after["token_history"][0]["action"] == "backend_identity_change"
    assert (await s.probe(b["token"])).status_code == 403
    await s.wait_routable(b["manager"], b["port"])
    assert (await s.whoami(b_after["mcp_auth_token"]))["user"] == NEW_USER
    assert s.pid(b["manager"]) not in (None, b_pid)  # B 的子行程重啟了（restart:false 也一樣）
    # 預設帳號與 C：token、設定檔、子行程都沒動
    assert s.config() == default_before and s.config(c["file"]) == c_before
    assert s.pid() == default_pid and s.pid(c["manager"]) == c_pid
    assert (await s.whoami(OLD_TOKEN))["user"] == OLD_USER
    assert (await s.whoami(c["token"]))["user"] == "owner-c-user-22"


# ---------------------------------------------------------------------------
# 共用的 PUT /api/settings/connection：同樣換 token
# ---------------------------------------------------------------------------


async def test_the_shared_settings_route_rotates_the_token_too(make_stack):
    s = await make_stack()
    old_pid = s.pid()
    seen = len(s.events())
    reply = await s.shared_put({"username": NEW_USER, "app_password": NEW_PW})
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert body["token_rotated"] is True
    after = s.config()
    assert after["mcp_auth_token"] != OLD_TOKEN and after["token_history"][0]["action"] == "backend_identity_change"
    assert (await s.probe(OLD_TOKEN)).status_code == 403
    await s.wait_routable()
    assert (await s.whoami(after["mcp_auth_token"]))["user"] == NEW_USER
    events = s.events()[seen:]
    assert index_of(events, "stop", pid=old_pid) < index_of(events, "config_write", user=NEW_USER)


async def test_the_shared_route_keeps_the_token_for_a_password_or_a_same_url_spelling(make_stack):
    s = await make_stack()
    before = s.config()
    for body in ({"app_password": NEW_PW}, {"base_url": "https://CLOUD.example.com/nc/"},
                 {"base_url": "https://cloud.example.com/nc/index.php/apps/files"}):
        reply = await s.shared_put(body)
        assert reply.status_code == 200, (body, reply.text)
        assert reply.json()["token_rotated"] is False, body
    after = s.config()
    assert after["mcp_auth_token"] == OLD_TOKEN and after["token_history"] == before["token_history"]
    assert after["connection"] == {**OLD, "app_password": NEW_PW}  # 網址寫法不同：存的是正規化後的同一個值


async def test_the_shared_route_validates_the_url_like_the_connection_page(make_stack):
    s = await make_stack()
    before = s.path.read_bytes()
    for bad in ("cloud.example.com", "ftp://cloud.example.com", "https://alice:pw@cloud.example.com",
                "https://cloud.example.com/?x=1"):
        reply = await s.shared_put({"base_url": bad})
        assert reply.status_code == 400, (bad, reply.text)
        assert "alice:pw" not in reply.text
    assert s.path.read_bytes() == before


# ---------------------------------------------------------------------------
# 既有行為不退步：If-Match、SSRF（#152）
# ---------------------------------------------------------------------------


async def test_a_stale_or_missing_revision_does_nothing(make_stack):
    s = await make_stack()
    before, seen = s.path.read_bytes(), len(s.events())
    stale = await s.client.put("/api/config/connection", json=payload(username=NEW_USER, app_password=NEW_PW),
                               headers={"If-Match": '"stale-revision"'})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "config_conflict"
    missing = await s.client.put("/api/config/connection", json=payload(username=NEW_USER, app_password=NEW_PW))
    assert missing.status_code == 428 and missing.json()["detail"]["code"] == "config_revision_required"
    assert s.path.read_bytes() == before and s.events()[seen:] == []  # 沒換 token、沒停子行程、沒寫檔
    assert (await s.whoami(OLD_TOKEN))["user"] == OLD_USER


@pytest.mark.parametrize("route", ["/api/config/connection", "/api/settings/connection"])
async def test_a_blocked_url_stores_nothing_and_does_not_rotate(make_stack, route):
    s = await make_stack()
    before, seen = s.path.read_bytes(), len(s.events())
    body = (payload(base_url="http://169.254.169.254", username=NEW_USER, app_password=NEW_PW) if route.startswith("/api/config")
            else {"base_url": "http://169.254.169.254", "username": NEW_USER})
    reply = await s.put(route, read_path="/api/config" if route.startswith("/api/config") else "/api/settings", **body)
    assert reply.status_code == 400 and reply.json()["detail"]["code"] == SSRF_BLOCKED, reply.text
    assert s.path.read_bytes() == before and s.events()[seen:] == []
    assert (await s.whoami(OLD_TOKEN))["user"] == OLD_USER


# ---------------------------------------------------------------------------
# 回應訊息：假 PM（沒有 _attempted_revision，核心不會把 message 改寫成「尚未確認套用」）
# ---------------------------------------------------------------------------


class _RecordingPM:
    """只記呼叫順序的 PM 替身：真的 ConfigStore＋真的 router／核心 save_bound_connection，子行程不存在。"""

    def __init__(self, running: bool = True, start_ok: bool = True) -> None:
        self.is_running, self.start_ok, self.calls = running, start_ok, []

    async def stop(self):
        self.calls.append("stop")
        self.is_running = False

    async def start(self):
        self.calls.append("start")
        self.is_running = True
        return True

    async def start_or_restart(self):
        self.calls.append("start_or_restart")
        self.is_running = self.start_ok
        return self.start_ok


@pytest.fixture
def recording_pm_client(make_client, monkeypatch):
    import asyncio

    def _make(pm, *, connection=None):
        monkeypatch.setattr(config_router, "get_process_manager", lambda: pm)
        monkeypatch.setattr("mcp_admin_core.routers.settings.get_process_manager", lambda: pm)
        client = make_client(connection=dict(OLD if connection is None else connection), app="main")
        set_launch_policy(LAUNCH_POLICY)  # 別的測試以 create_app() 組 app 時會把全域政策換掉
        asyncio.run(store_mod.get_config_store().change_mcp_token(OLD_TOKEN, action="set"))
        return client

    previous = get_launch_policy()
    yield _make
    set_launch_policy(previous)


def test_the_response_message_says_the_old_url_was_revoked_and_the_child_restarted(recording_pm_client):
    from management_client import revision_headers

    pm = _RecordingPM(running=True)
    client = recording_pm_client(pm)
    reply = client.put("/api/config/connection", json=payload(username=NEW_USER, app_password=NEW_PW, restart=False),
                       headers=revision_headers(client, None, read_path="/api/config"))
    body = reply.json()
    assert reply.status_code == 200 and body["token_rotated"] is True and body["status"] == "ok"
    assert pm.calls == ["stop", "start_or_restart"]  # restart:false 也要換掉舊子行程：核心先停、router 再起
    message = body["message"]
    assert "restarted" in message and "已重啟" in message and "原本未執行" not in message
    assert "Account changed: old MCP URL revoked. Copy the new URL from Tokens." in message  # 與 Odoo 相同的英文句
    assert "舊的 MCP 網址已撤銷" in message and "Tokens 頁" in message  # 中英文並列
    assert not [v for v in (*SECRET_VALUES, OLD_TOKEN, store_mod.get_config_store().peek("mcp_auth_token")) if v in reply.text]
    assert set(body) >= {"saved", "token_rotated", "status", "apply_state", "success", "message", "restarted"}


def test_a_failed_restart_after_a_rotation_still_tells_the_admin_the_url_changed(recording_pm_client):
    from management_client import revision_headers

    pm = _RecordingPM(running=True, start_ok=False)
    client = recording_pm_client(pm)
    reply = client.put("/api/config/connection", json=payload(username=NEW_USER, app_password=NEW_PW),
                       headers=revision_headers(client, None, read_path="/api/config"))
    body = reply.json()
    assert reply.status_code == 200 and body["token_rotated"] is True
    assert body["status"] == "partial" and body["apply_state"] == "failed" and body["saved"] is True
    assert "啟動失敗" in body["message"] and "舊的 MCP 網址已撤銷" in body["message"]


def test_a_password_only_save_adds_no_notice_and_keeps_the_restart_flag_semantics(recording_pm_client):
    from management_client import revision_headers

    pm = _RecordingPM(running=True)
    client = recording_pm_client(pm)
    reply = client.put("/api/config/connection", json=payload(app_password=NEW_PW, restart=False),
                       headers=revision_headers(client, None, read_path="/api/config"))
    body = reply.json()
    assert reply.status_code == 200 and body["token_rotated"] is False and "Tokens" not in body["message"]
    assert pm.calls == [] and body["apply_state"] == "not_requested"  # 沒要求套用、也沒換身分：不碰子行程
    reply = client.put("/api/config/connection", json=payload(app_password=NEW_PW),
                       headers=revision_headers(client, None, read_path="/api/config"))
    assert reply.json()["token_rotated"] is False and pm.calls == ["start_or_restart"]


# ---------------------------------------------------------------------------
# log 與回應不洩漏身分
# ---------------------------------------------------------------------------


async def test_the_identity_change_log_line_has_no_values(make_stack, caplog):
    s = await make_stack()
    caplog.set_level(logging.DEBUG)
    reply = await s.put(**payload(base_url=NEW_URL, username=NEW_USER, app_password=NEW_PW, restart=True))
    assert reply.status_code == 200 and reply.json()["token_rotated"] is True
    new_token = s.config()["mcp_auth_token"]
    text = caplog.text
    assert "Nextcloud connection identity changed; MCP token rotated" in text
    leaked = [v for v in (*SECRET_VALUES, OLD_TOKEN, new_token) if v in text]
    assert not leaked, f"log 不該出現這些值：{leaked}"
    assert "username=" not in text and "base_url=" not in text
