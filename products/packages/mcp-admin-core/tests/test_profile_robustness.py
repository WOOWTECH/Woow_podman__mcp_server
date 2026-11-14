"""多帳號實測（2026-11-13，mp-odoo-iso／mp-odoo-res／mp-n8n／mp-products）找到的管理與啟動問題。

- 手動設定的 token 已由另一個帳號使用：回 409 ``token_in_use``（原因清楚、不回顯 token），不留「結果不明」的收據；
- 兩個帳號設定檔的 token 重複：照舊 503、不導到任一帳號，另記一行（限流）不含 token 的 log；
- 帳號索引出現重複 port：只停衝突的帳號（fail closed）並記錄，其他帳號照常，整台不再起不來；
- 探測逾時（child_probe_failed）與晚驗證成功、子程式當掉與退避：log 帶帳號 id、port、PID；
- 記憶體不夠時建立帳號回 409 說明是記憶體與需要多少，被停用的帳號記 log、/api/profiles 標出原因；
- 必要連線沒填（LaunchPolicy.required_connection）時預設帳號也不啟動子程式，不再「exit → 退避重啟」無限循環。

全部用合成 token、臨時檔案與 loopback 臨時 port；log 斷言一律確認 token 原文不出現。
"""
from __future__ import annotations

import asyncio
import logging

import pytest
from fastapi import HTTPException

from mcp_admin_core.config.store import ConfigUnavailable
from mcp_admin_core.profiles import ProfileRegistry, get_profile_registry
from test_child_identity_live import (  # noqa: F401  (peers is a fixture)
    LINUX, TOKEN, free_port, gateway, peers, stop, wait_until)

PROCESS_LOGGER = "mcp_admin_core.process"


@pytest.fixture(autouse=True)
def _one_gib_test_container(monkeypatch):
    # CI runner cgroup limits vary; the capacity cases set their own value.
    monkeypatch.setattr("mcp_admin_core.profiles._memory_limit_mib", lambda: 1024)


def _admin(client):
    login = client.post("/api/auth/login", json={"password": "admin"})
    assert login.status_code == 200
    return {"Authorization": "Bearer " + login.json()["token"]}


def _lines(out: str, needle: str) -> list[str]:
    return [line for line in out.splitlines() if needle in line]


class _Child:
    """Spawned-child stand-in: no OS process, so no identity check (tests that need one use a real child)."""
    returncode = None
    pid = 43210
    stdout = None

    def send_signal(self, sig):
        self.returncode = 0

    async def wait(self):
        return 0


def _fake_spawns(monkeypatch) -> list[tuple]:
    import mcp_admin_core.process as process

    spawned: list[tuple] = []

    async def spawn(*cmd, **kwargs):
        spawned.append(cmd)
        return _Child()

    async def quiet_drain(self, proc):
        return None

    async def quiet_probe(self, proc, snapshot, revision, late=False):
        return None

    monkeypatch.setattr(process.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(process.McpProcessManager, "_drain_logs", quiet_drain)
    monkeypatch.setattr(process.McpProcessManager, "_verify_configuration", quiet_probe)
    return spawned


# ---- 1. token 撞號：回真正原因 -------------------------------------------------------------------------------------


def test_manual_token_used_by_another_profile_is_refused_with_its_reason(make_app):
    from management_client import revision_headers

    client = make_app({"mcp_server": {"command": "", "args": [], "port": 59999, "env": {}}})
    auth = _admin(client)
    first = client.post("/api/profiles", headers=auth, json={"label": "first"}).json()["id"]
    second = client.post("/api/profiles", headers=auth, json={"label": "second"}).json()["id"]
    registry = get_profile_registry()
    first_token = asyncio.run(registry.store_for(first).get("mcp_auth_token"))
    second_token = asyncio.run(registry.store_for(second).get("mcp_auth_token"))
    selected = {**auth, "X-MCP-Profile": second}
    headers = revision_headers(client, selected, read_path="/api/tokens")

    refused = client.put("/api/tokens", json={"token": first_token}, headers=headers)
    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert detail["code"] == "token_in_use"
    assert "another profile" in detail["message"] and "另一個帳號" in detail["message"]
    assert first_token not in refused.text and second_token not in refused.text
    # The outcome is known (nothing changed): no receipt is left in the "unknown" state, and the same
    # Idempotency-Key gets the same explicit answer instead of 409 operation_outcome_unknown.
    receipt = client.get(f"/api/operations/{headers['Idempotency-Key']}", headers=selected)
    assert receipt.status_code == 404 and receipt.json()["detail"]["code"] == "operation_not_found"
    replay = client.put("/api/tokens", json={"token": first_token}, headers=headers)
    assert replay.status_code == 409 and replay.json()["detail"]["code"] == "token_in_use"
    assert asyncio.run(registry.store_for(second).get("mcp_auth_token")) == second_token
    assert asyncio.run(registry.resolve_token(first_token)) == first

    # Internal callers (no journal) get the same refusal.
    with pytest.raises(HTTPException) as direct:
        asyncio.run(registry.assign_token(second, first_token))
    assert direct.value.status_code == 409 and direct.value.detail["code"] == "token_in_use"


def test_refusal_inside_a_journaled_operation_drops_only_its_own_receipt(make_app):
    """Other failures of a journaled operation keep reporting operation_outcome_unknown (unchanged)."""
    from management_client import revision_headers

    import mcp_admin_core.config.store as store_mod

    client = make_app({"mcp_server": {"command": "", "args": [], "port": 59999, "env": {}}})
    auth = _admin(client)
    headers = revision_headers(client, auth, read_path="/api/tokens")
    store = store_mod.get_config_store()
    original = store.change_mcp_token

    async def broken(value, *, action):
        raise RuntimeError("synthetic failure after the intent was recorded")

    store.change_mcp_token = broken
    try:
        failed = client.put("/api/tokens", json={"token": "synthetic-unused-token-0123456789"}, headers=headers)
    finally:
        store.change_mcp_token = original
    assert failed.status_code == 409 and failed.json()["detail"]["code"] == "operation_outcome_unknown"
    receipt = client.get(f"/api/operations/{headers['Idempotency-Key']}", headers=auth)
    assert receipt.status_code == 200 and receipt.json()["operation"]["state"] == "unknown"


# ---- 2. 兩個帳號設定檔的 token 重複：503 照舊，加一行 log -----------------------------------------------------------


def test_token_duplicated_in_two_profile_files_is_refused_and_logged_once_without_the_token(make_app, capsys):
    import mcp_admin_core.profiles as profiles

    profiles._ambiguous_token_logged.clear()
    client = make_app({"mcp_auth_token": "synthetic-default-token-0123456789",
                       "mcp_server": {"command": "", "args": [], "port": 59999, "env": {}}})
    auth = _admin(client)
    first = client.post("/api/profiles", headers=auth, json={"label": "first"}).json()["id"]
    second = client.post("/api/profiles", headers=auth, json={"label": "second"}).json()["id"]
    registry = get_profile_registry()
    shared = asyncio.run(registry.store_for(first).get("mcp_auth_token"))
    # Hand edit or bad restore: the second profile's own file now holds the first profile's token.
    asyncio.run(registry.store_for(second).change_mcp_token(shared, action="set"))
    capsys.readouterr()

    for _ in range(3):
        refused = client.get(f"/private_{shared}/health")
        assert refused.status_code == 503 and refused.json()["code"] == "state_unavailable"
    out = capsys.readouterr().out
    lines = _lines(out, "more than one profile")
    assert len(lines) == 1, out  # rate limited: one line, not one per request
    assert "WARNING" in lines[0] and first in lines[0] and second in lines[0]
    assert shared not in out
    # Other profiles keep resolving; rotating one of the two repairs the URL.
    assert asyncio.run(registry.resolve_token("synthetic-default-token-0123456789")) == "default"
    asyncio.run(registry.store_for(second).change_mcp_token("synthetic-repaired-token-0123456789", action="set"))
    assert asyncio.run(registry.resolve_token(shared)) == first


# ---- 3. 帳號索引重複 port：只停衝突的帳號 --------------------------------------------------------------------------


def _three_profiles_with_duplicate_index_port(make_app):
    client = make_app({"mcp_auth_token": "synthetic-default-token-0123456789",
                       "mcp_server": {"command": "", "args": [], "port": 59999, "env": {}}})
    auth = _admin(client)
    ids = [client.post("/api/profiles", headers=auth, json={"label": f"person-{i}"}).json()["id"] for i in range(3)]
    registry = get_profile_registry()
    tokens = [asyncio.run(registry.store_for(pid).get("mcp_auth_token")) for pid in ids]
    # Hand edit or bad restore of /data/config.json: the third profile is assigned the second profile's port
    # (mp-odoo-res R6b; the third profile's own config.json still has its old port).
    index = asyncio.run(registry.default_store.get("profile_index"))
    ports = [index[pid]["port"] for pid in ids]
    index[ids[2]]["port"] = ports[1]
    asyncio.run(registry.default_store.put("profile_index", index))
    return client, auth, registry, ids, tokens, ports


def test_duplicate_index_port_keeps_the_service_and_other_profiles_available(make_app):
    from mcp_admin_core.profiles import _validated_index

    client, auth, registry, ids, tokens, ports = _three_profiles_with_duplicate_index_port(make_app)
    listing = client.get("/api/profiles", headers=auth)
    assert listing.status_code == 200, listing.text
    reasons = {row["id"]: row["disabled_reason"] for row in listing.json()["profiles"]}
    assert reasons == {"default": None, ids[0]: None, ids[1]: "port_conflict", ids[2]: "port_conflict"}
    for pid, token in zip(ids, tokens):
        assert asyncio.run(registry.resolve_token(token)) == pid  # each URL still selects only its own profile
    assert asyncio.run(registry.resolve_token("synthetic-default-token-0123456789")) == "default"
    # Restore validation stays strict: such an index is never accepted from a backup.
    with pytest.raises(ConfigUnavailable):
        _validated_index(asyncio.run(registry.default_store.load()))
    # The administrator can still repair it from the API, e.g. by deleting one of the two.
    assert client.delete(f"/api/profiles/{ids[2]}", headers=auth).status_code == 204
    rows = client.get("/api/profiles", headers=auth).json()["profiles"]
    assert {row["id"]: row["disabled_reason"] for row in rows} == {"default": None, ids[0]: None, ids[1]: None}


def test_duplicate_index_port_starts_every_other_profile_and_logs_the_conflict(make_app, monkeypatch, capsys):
    import mcp_admin_core.process as process
    import mcp_admin_core.profiles as profiles
    from mcp_admin_core.launch_policy import LaunchPolicy, set_launch_policy
    from mcp_admin_core.process import get_default_process_manager

    client, auth, registry, ids, tokens, ports = _three_profiles_with_duplicate_index_port(make_app)
    set_launch_policy(LaunchPolicy(command="synthetic-child", args=("--port", "{port}")))
    spawned = _fake_spawns(monkeypatch)
    monkeypatch.setattr(process, "_instance", None)  # a fresh default manager, as in a restarted pod
    reloaded = ProfileRegistry(registry.default_store)  # what a restarted pod (lifespan start_all) sees
    monkeypatch.setattr(profiles, "_registry", reloaded)
    capsys.readouterr()

    async def startup():
        default = get_default_process_manager()
        await default.start()
        await reloaded.start_all()
        states = {pid: (reloaded.manager_for(pid).is_running, reloaded.manager_for(pid)._configuration_error)
                  for pid in ids}
        default_running = default.is_running
        await reloaded.stop_all()
        await default.stop()
        return default_running, states

    default_running, states = asyncio.run(startup())
    assert default_running  # the whole service no longer fails startup (was: lifespan exit 3, CrashLoopBackOff)
    assert sorted(cmd[-1] for cmd in spawned) == sorted(["59999", str(ports[0])])
    assert states[ids[0]] == (True, None)
    assert states[ids[1]] == (False, "child_port_conflict")
    assert states[ids[2]] == (False, "child_port_conflict")
    out = capsys.readouterr().out
    index_lines = _lines(out, "Profile index assigns")
    assert len(index_lines) == 1 and "ERROR" in index_lines[0], out
    assert str(ports[1]) in index_lines[0] and ids[1] in index_lines[0] and ids[2] in index_lines[0]
    refusing = _lines(out, "Refusing to start the MCP child")
    assert {pid for pid in ids if any(f"profile={pid}" in line for line in refusing)} == {ids[1], ids[2]}
    for token in (*tokens, "synthetic-default-token-0123456789"):
        assert token not in out


def test_restore_validation_is_unchanged_and_runtime_relaxes_only_port_uniqueness():
    """Differential check against the e0a80d6 validator (verbatim below) on random snapshots, malformed ones included.

    Restore (restore_guard／restore_inplace) keeps exactly the old strict outcome, exception type included; the
    runtime form rejects every structural error the old one rejected and additionally accepts only port collisions.
    """
    import random
    import re

    from mcp_admin_core.profiles import MAX_EXTRA_PROFILES, _index_entries, _validated_index

    profile_id_re = re.compile(r"[a-f0-9]{24}\Z")

    def e0a80d6_validated_index(snapshot):
        index = snapshot.get("profile_index", {})
        if not isinstance(index, dict) or len(index) > MAX_EXTRA_PROFILES:
            raise ConfigUnavailable("Profile index unavailable")
        ports = {int((snapshot.get("mcp_server") or {}).get("port", 8000))}
        labels = {"default"}
        for profile_id, item in index.items():
            if (not isinstance(profile_id, str) or not profile_id_re.fullmatch(profile_id)
                    or not isinstance(item, dict) or set(item) != {"label", "port"}
                    or not isinstance(item["label"], str) or not item["label"].strip()
                    or len(item["label"]) > 80 or not isinstance(item["port"], int)
                    or isinstance(item["port"], bool) or not 1024 <= item["port"] <= 65535
                    or item["port"] in ports or item["label"].casefold() in labels):
                raise ConfigUnavailable("Profile index unavailable")
            ports.add(item["port"])
            labels.add(item["label"].casefold())
        return index

    def outcome(validate, snapshot):
        try:
            return ("ok", validate(snapshot))
        except Exception as exc:  # noqa: BLE001  the exception type is part of the contract here
            return (type(exc).__name__,)

    rng = random.Random(20261113)
    ids = ["%024x" % rng.getrandbits(96) for _ in range(6)] + ["../x", "A" * 24, 7]
    ports = [8000, 18000, 18001, 18002, 1023, 65536, True, "18000", None]
    labels = ["a", "A", "b", "default", " ", "", "x" * 81, 5, "ß", "SS"]
    relaxed = 0
    for _ in range(3000):
        index = {}
        for _ in range(rng.randint(0, 7)):
            index[rng.choice(ids)] = rng.choice([{"label": rng.choice(labels), "port": rng.choice(ports)},
                                                 {"label": "a"}, "entry", {"label": "z", "port": 18003, "x": 1}])
        snapshot = {"profile_index": rng.choice([index, index, index, [], None])}
        if rng.random() < 0.8:
            snapshot["mcp_server"] = rng.choice([{"port": rng.choice([8000, 18000, "18001", None, "junk"])}, {}, None])
        old = outcome(e0a80d6_validated_index, snapshot)
        assert outcome(_validated_index, snapshot) == old, snapshot
        runtime = outcome(_index_entries, snapshot)
        if old[0] == "ok":
            assert runtime == old, snapshot
        elif runtime[0] == "ok":
            # Accepted at runtime only because of a port collision or a malformed default port (read with
            # coerce_port at runtime); never because of a malformed entry.
            relaxed += 1
            entries = snapshot["profile_index"].values()
            assert all(isinstance(item, dict) and set(item) == {"label", "port"} for item in entries), snapshot
    assert relaxed > 0


def test_index_entry_on_the_default_port_blocks_only_that_profile(make_app):
    from mcp_admin_core.process import McpProcessManager

    client = make_app({"mcp_server": {"command": "", "args": [], "port": 59999, "env": {}}})
    auth = _admin(client)
    extra = client.post("/api/profiles", headers=auth, json={"label": "extra"}).json()["id"]
    registry = get_profile_registry()
    index = asyncio.run(registry.default_store.get("profile_index"))
    index[extra]["port"] = 59999
    asyncio.run(registry.default_store.put("profile_index", index))
    rows = {row["id"]: row for row in client.get("/api/profiles", headers=auth).json()["profiles"]}
    assert rows["default"]["disabled_reason"] is None and rows[extra]["disabled_reason"] == "port_conflict"
    probe = McpProcessManager()
    assert asyncio.run(registry.port_conflict("default", 59999, probe)) is None
    assert asyncio.run(registry.port_conflict(extra, 59999, probe)) is not None


# ---- 4／5. 探測逾時、晚驗證成功、當掉與退避的 log 帶帳號、port、PID ---------------------------------------------------


@pytest.mark.skipif(not LINUX, reason="needs /proc to see who owns the listener")
async def test_probe_timeout_and_late_verification_are_logged_with_profile_port_and_pid(
        make_app, monkeypatch, peers, tmp_path, capsys):
    from test_child_late_verify import GATED, fast, late_waiting

    port = free_port()
    gate = tmp_path / "gate"
    args, _ready, _log = peers.args(port, "late-log-synthetic")
    app, pm = gateway(make_app, monkeypatch, port, ["-c", GATED, str(gate), *args])
    fast(pm)
    try:
        assert await pm.start()
        pid = pm._process.pid
        assert await late_waiting(pm)
        out = capsys.readouterr().out
        timeout = _lines(out, "child_probe_failed")
        assert len(timeout) == 1 and "WARNING" in timeout[0], out
        assert "profile=default" in timeout[0] and f"port={port}" in timeout[0] and f"pid={pid}" in timeout[0]
        gate.write_text("go")
        assert await wait_until(
            lambda: pm._applied_revision is not None and pm._applied_revision == pm._attempted_revision, 15)
        out = capsys.readouterr().out
        late = _lines(out, "late verification")
        assert len(late) == 1 and "INFO" in late[0], out
        assert "profile=default" in late[0] and f"port={port}" in late[0] and f"pid={pid}" in late[0]
        assert TOKEN not in out
    finally:
        await stop(pm)


class _FakeStdout:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class _CrashedChild:
    pid = 4321
    returncode = -9
    stdout = _FakeStdout()

    async def wait(self):
        return -9


async def test_crash_and_backoff_lines_name_the_profile_port_and_pid(store_env, monkeypatch, capsys):
    from mcp_admin_core.process import McpProcessManager

    profile_id = "ab" * 12
    pm = McpProcessManager(profile_id=profile_id)
    pm.auto_restart_delays = (0,)
    child = _CrashedChild()
    pm._process, pm._running, pm._active_launch, pm._launch_port = child, True, ("synthetic-child", 18003), 18003
    attempts = []

    async def fake_start():
        attempts.append(1)
        return True

    monkeypatch.setattr(pm, "start", fake_start)
    capsys.readouterr()
    await pm._drain_logs(child)
    assert await wait_until(lambda: attempts and not pm._auto_restart_pending, 5)
    out = capsys.readouterr().out
    exited = _lines(out, "exited unexpectedly with code -9")
    scheduled = _lines(out, "auto-restart scheduled in 0s (attempt 1)")
    restarting = _lines(out, "auto-restarting (auto_restart_count=1)")
    assert len(exited) == len(scheduled) == len(restarting) == 1, out
    for line in exited + scheduled + restarting:
        assert f"profile={profile_id}" in line and "port=18003" in line
    assert "pid=4321" in exited[0]


async def test_default_manager_and_registry_managers_carry_their_profile_id(make_app):
    from mcp_admin_core.process import McpProcessManager, get_default_process_manager

    make_app({"mcp_server": {"command": "", "args": [], "port": 59999, "env": {}}})
    assert McpProcessManager().profile_id == "default"
    assert get_default_process_manager().profile_id == "default"
    assert get_profile_registry().manager_for("cd" * 12).profile_id == "cd" * 12


# ---- 6. 容量：建立被拒說明記憶體與需要多少、停用的帳號記 log、清單標出原因 ----------------------------------------------


def test_create_refused_for_memory_says_so_and_how_much_is_needed(make_app, monkeypatch):
    import mcp_admin_core.profiles as profiles
    from mcp_admin_core.launch_policy import LaunchPolicy, set_launch_policy

    client = make_app({"mcp_server": {"command": "", "args": [], "port": 59999, "env": {}}})
    # n8n's declared budget (apps/n8n main.py): two additional profiles, 300 MiB per child.
    set_launch_policy(LaunchPolicy(command="synthetic-child", max_extra_profiles=2, profile_child_memory_mib=300))
    auth = _admin(client)
    assert client.post("/api/profiles", headers=auth, json={"label": "first"}).status_code == 201
    refused = client.post("/api/profiles", headers=auth, json={"label": "second"})
    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert detail["code"] == "profile_memory_insufficient"
    assert detail["memory_limit_mib"] == 1024 and detail["required_memory_mib"] == 1156 and detail["limit"] == 1
    assert "1024 MiB" in detail["message"] and "1156 MiB" in detail["message"] and "記憶體" in detail["message"]

    monkeypatch.setattr(profiles, "_memory_limit_mib", lambda: 1156)
    assert client.post("/api/profiles", headers=auth, json={"label": "second"}).status_code == 201
    over = client.post("/api/profiles", headers=auth, json={"label": "third"})
    assert over.status_code == 409
    assert over.json()["detail"]["code"] == "profile_limit_reached" and over.json()["detail"]["limit"] == 2


def test_memory_disabled_profile_is_listed_with_its_reason_and_logged_when_not_started(make_app, monkeypatch, capsys):
    import mcp_admin_core.profiles as profiles
    from mcp_admin_core.launch_policy import LaunchPolicy, set_launch_policy

    client = make_app({"mcp_server": {"command": "", "args": [], "port": 59999, "env": {}}})
    set_launch_policy(LaunchPolicy(command="synthetic-child", max_extra_profiles=5, profile_child_memory_mib=100))
    auth = _admin(client)
    first = client.post("/api/profiles", headers=auth, json={"label": "first"}).json()["id"]
    second = client.post("/api/profiles", headers=auth, json={"label": "second"}).json()["id"]
    registry = get_profile_registry()
    second_token = asyncio.run(registry.store_for(second).get("mcp_auth_token"))

    # The memory limit was lowered (for example a helm rollback): 512 MiB runs the default plus one profile.
    monkeypatch.setattr(profiles, "_memory_limit_mib", lambda: 512)
    rows = {row["id"]: row for row in client.get("/api/profiles", headers=auth).json()["profiles"]}
    assert rows["default"]["disabled_reason"] is None and rows[first]["disabled_reason"] is None
    assert rows[second]["disabled_reason"] == "memory_capacity"
    assert rows[second]["required_memory_mib"] == 556
    assert "required_memory_mib" not in rows[first]
    capsys.readouterr()
    with registry.activate(second):
        assert asyncio.run(registry.manager_for(second).start()) is False
    out = capsys.readouterr().out
    lines = _lines(out, f"profile={second}")
    assert len(lines) == 1 and "WARNING" in lines[0], out
    assert "memory" in lines[0] and "512 MiB" in lines[0] and "556 MiB" in lines[0]
    assert second_token not in out


# ---- 7. 必要連線沒填時不啟動、不重啟 -------------------------------------------------------------------------------


async def test_default_profile_without_required_connection_is_not_spawned_or_restarted(store_env, monkeypatch, capsys):
    from mcp_admin_core.configuration import configuration_status
    from mcp_admin_core.launch_policy import LaunchPolicy, set_launch_policy
    from mcp_admin_core.process import McpProcessManager

    await store_env.patch("mcp_server", {"port": free_port()})
    set_launch_policy(LaunchPolicy(
        command="synthetic-child", args=("--port", "{port}"), env_prefix="SYNTHETIC_",
        connection_keys=frozenset({"base_url", "api_key"}),
        required_connection=(("base_url", "API 位址"), ("api_key", "API 金鑰"))))
    spawned = _fake_spawns(monkeypatch)
    pm = McpProcessManager()
    capsys.readouterr()
    assert await pm.start() is False
    assert spawned == [] and not pm._auto_restart_pending and not pm.is_running
    status = configuration_status(store_env, await store_env.load(), pm)
    assert status["apply_state"] == "unconfigured" and status["last_error_code"] is None
    out = capsys.readouterr().out
    waiting = _lines(out, "required connection")
    assert len(waiting) == 1 and "base_url" in waiting[0] and "api_key" in waiting[0] and "profile=default" in waiting[0]

    # Saving only one field is still not enough; saving both starts the child once.
    await store_env.patch("connection", {"base_url": "https://backend.example"})
    assert await pm.start() is False and spawned == []
    await store_env.patch("connection", {"api_key": "synthetic-backend-key-0123456789"})
    assert await pm.start() is True
    assert len(spawned) == 1
    assert "synthetic-backend-key-0123456789" not in capsys.readouterr().out
    await pm.stop()


def test_stdout_handlers_are_installed_once():
    from mcp_admin_core.owner_defaults import _StdoutHandler

    for name in ("mcp_admin_core.profiles", PROCESS_LOGGER):
        handlers = [h for h in logging.getLogger(name).handlers if isinstance(h, _StdoutHandler)]
        assert len(handlers) == 1, name
