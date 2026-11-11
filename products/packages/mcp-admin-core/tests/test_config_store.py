"""ConfigStore 行為回歸測試 —— 預設值、get/put/patch、persistence、deep-merge、reload。"""

from __future__ import annotations

import json
import os
import stat
import asyncio
import threading

import pytest

from mcp_admin_core.config.store import ConfigStore


async def test_defaults_seeded_on_first_load(fresh_store):
    cfg = await fresh_store.load()
    assert cfg["admin_password"] == "admin"
    assert cfg["mcp_auth_token"] == ""
    assert cfg["mcp_server"]["port"] == 8000
    assert cfg["proxy"]["timeout"] == 86400
    assert cfg["tools"]["disabled"] == []


async def test_ensure_file_created_on_init(fresh_store):
    assert fresh_store.path.exists()
    data = json.loads(fresh_store.path.read_text())
    assert data["admin_password"] == "admin"


def _policy_with_fresh_env(fresh_env):
    from mcp_admin_core.launch_policy import LaunchPolicy

    return LaunchPolicy(command="x", env_keys=frozenset({"A_SWITCH", "B_SWITCH"}), fresh_env=fresh_env)


def test_new_file_gets_app_fresh_env(tmp_config_path):
    """全新安裝（本行程建立 config.json）：mcp_server.env 帶 app 的 LaunchPolicy.fresh_env，其他預設不變。"""
    from mcp_admin_core.launch_policy import set_launch_policy

    set_launch_policy(_policy_with_fresh_env({"A_SWITCH": "1", "B_SWITCH": "0"}))
    ConfigStore(path=tmp_config_path)
    data = json.loads(tmp_config_path.read_text())
    assert data["mcp_server"]["env"] == {"A_SWITCH": "1", "B_SWITCH": "0"}
    assert data["tools"]["disabled"] == [] and data["tools"]["disabled_operations"] == {}
    # 全新安裝直接寫上 2026-10-25 全開標記（不需遷移）
    assert data["tools"]["owner_defaults_20261025"]["applied"] == "fresh_install"
    assert data["tools"]["owner_defaults_20261025"]["source"] == "new_config"  # 這個行程新建的設定
    assert data["admin_password"] == "admin"


def test_fresh_env_never_touches_existing_file(tmp_config_path):
    """既有 config.json（含部署 seed 建立的）不經過 fresh_env：連缺鍵也不補，原檔逐位元組不變。"""
    from mcp_admin_core.launch_policy import set_launch_policy

    set_launch_policy(_policy_with_fresh_env({"A_SWITCH": "1"}))
    tmp_config_path.write_text(json.dumps({"mcp_server": {"env": {}}}))
    before = tmp_config_path.read_bytes()
    ConfigStore(path=tmp_config_path)
    assert tmp_config_path.read_bytes() == before


def test_fresh_env_does_not_leak_into_shared_defaults(tmp_path):
    from mcp_admin_core.config import store as store_mod
    from mcp_admin_core.launch_policy import set_launch_policy

    set_launch_policy(_policy_with_fresh_env({"A_SWITCH": "1"}))
    ConfigStore(path=tmp_path / "a.json")
    assert store_mod._DEFAULT_CONFIG["mcp_server"]["env"] == {}
    set_launch_policy(None)
    ConfigStore(path=tmp_path / "b.json")
    assert json.loads((tmp_path / "b.json").read_text())["mcp_server"]["env"] == {}


def test_fresh_env_must_be_declared_env_keys():
    from mcp_admin_core.launch_policy import LaunchPolicy

    with pytest.raises(ValueError, match="fresh_env keys must be a subset of env_keys"):
        LaunchPolicy(command="x", env_keys=frozenset({"A_SWITCH"}), fresh_env={"OTHER": "1"})
    with pytest.raises(ValueError, match="fresh_env values must be strings"):
        LaunchPolicy(command="x", env_keys=frozenset({"A_SWITCH"}), fresh_env={"A_SWITCH": 1})
    # 不參與 hash（dict 欄位），政策物件仍可 hash
    hash(LaunchPolicy(command="x", env_keys=frozenset({"A_SWITCH"}), fresh_env={"A_SWITCH": "1"}))


async def test_put_and_get_roundtrip(fresh_store):
    await fresh_store.put("admin_password", "s3cret")
    assert await fresh_store.get("admin_password") == "s3cret"


async def test_get_returns_default_for_missing_key(fresh_store):
    assert await fresh_store.get("does_not_exist", "fallback") == "fallback"


async def test_patch_merges_into_dict_value(fresh_store):
    merged = await fresh_store.patch("connection", {"odoo_url": "http://x"})
    assert merged["odoo_url"] == "http://x"
    cfg = await fresh_store.load()
    assert cfg["connection"]["odoo_url"] == "http://x"


async def test_bound_connection_rotation_is_atomic_and_preserves_password_changes(fresh_store, monkeypatch):
    identity = frozenset({"odoo_url", "odoo_db", "odoo_username"})
    await fresh_store.patch("connection", {"odoo_url": "https://odoo.example", "odoo_db": "shared",
                                          "odoo_username": "alice", "odoo_password": "old-key"})
    await fresh_store.change_mcp_token("alice-token", action="set")
    before = await fresh_store.load()
    merged, rotated = await fresh_store.patch_connection_bound(
        {"odoo_password": "new-key"}, identity_keys=identity, make_token=lambda: "new-token")
    assert not rotated and merged["odoo_password"] == "new-key"
    assert await fresh_store.get("mcp_auth_token") == "alice-token"

    observed_before_stop = []
    async def before_rotate():
        observed_before_stop.append(json.loads(fresh_store.path.read_text())["mcp_auth_token"])
    merged, rotated = await fresh_store.patch_connection_bound(
        {"odoo_username": "bob"}, identity_keys=identity,
        make_token=lambda: "bob-token", before_rotate=before_rotate)
    after = await fresh_store.load()
    assert observed_before_stop == ["alice-token"]
    assert rotated and merged["odoo_username"] == "bob"
    assert after["mcp_auth_token"] == "bob-token"
    assert after["mcp_auth_generation"] != before["mcp_auth_generation"]
    assert after["token_history"][0]["action"] == "backend_identity_change"

    durable = fresh_store.path.read_bytes()
    def fail_write(_data):
        raise OSError("simulated disk failure")
    monkeypatch.setattr(fresh_store, "_write_sync", fail_write)
    with pytest.raises(OSError, match="simulated disk failure"):
        await fresh_store.patch_connection_bound(
            {"odoo_username": "charlie"}, identity_keys=identity, make_token=lambda: "charlie-token")
    assert fresh_store.path.read_bytes() == durable
    assert (await fresh_store.load())["mcp_auth_token"] == "bob-token"


async def test_initial_bound_connection_setup_keeps_issued_url(fresh_store):
    identity = frozenset({"odoo_url", "odoo_db", "odoo_username"})
    await fresh_store.change_mcp_token("initial-token", action="set")
    _, rotated = await fresh_store.patch_connection_bound(
        {"odoo_url": "https://odoo.example", "odoo_db": "shared", "odoo_username": "alice"},
        identity_keys=identity, make_token=lambda: "new-token")
    assert not rotated
    assert await fresh_store.get("mcp_auth_token") == "initial-token"


async def test_failed_account_rebind_restores_old_child_and_url(fresh_store, monkeypatch):
    from mcp_admin_core.connection_apply import save_bound_connection

    class Child:
        is_running = True
        stops = 0
        starts = 0

        async def stop(self):
            self.stops += 1
            self.is_running = False

        async def start(self):
            self.starts += 1
            self.is_running = True
            return True

    await fresh_store.patch("connection", {"odoo_url": "https://odoo.example", "odoo_db": "shared",
                                          "odoo_username": "alice", "odoo_password": "key"})
    await fresh_store.change_mcp_token("alice-token", action="set")
    child = Child()
    def fail_write(_data):
        raise OSError("simulated disk failure")
    monkeypatch.setattr(fresh_store, "_write_sync", fail_write)
    with pytest.raises(OSError, match="simulated disk failure"):
        await save_bound_connection(fresh_store, child, {"odoo_username": "bob"},
            identity_keys=frozenset({"odoo_url", "odoo_db", "odoo_username"}),
            make_token=lambda: "bob-token")
    assert child.is_running and (child.stops, child.starts) == (1, 1)
    assert (await fresh_store.load())["connection"]["odoo_username"] == "alice"
    assert await fresh_store.get("mcp_auth_token") == "alice-token"


async def test_patch_resets_non_dict_value(fresh_store):
    await fresh_store.put("connection", "not-a-dict")
    merged = await fresh_store.patch("connection", {"a": 1})
    assert merged == {"a": 1}


async def test_persistence_across_instances(tmp_config_path):
    s1 = ConfigStore(path=tmp_config_path)
    await s1.put("mcp_auth_token", "tok-123")
    s2 = ConfigStore(path=tmp_config_path)
    assert await s2.get("mcp_auth_token") == "tok-123"


async def test_deep_merge_keeps_default_subkeys(tmp_config_path):
    # 只寫入部分 mcp_server 子鍵，load() 應補回預設子鍵（deep merge）
    tmp_config_path.write_text(json.dumps({"mcp_server": {"command": "foo"}}))
    s = ConfigStore(path=tmp_config_path)
    cfg = await s.load()
    assert cfg["mcp_server"]["command"] == "foo"
    assert cfg["mcp_server"]["port"] == 8000
    assert cfg["mcp_server"]["env"] == {}


async def test_config_file_mode_0600(fresh_store):
    # 寫入後 config.json 檔案權限必須是 0600（kubeconfig 全文落 PVC 的權限底線）
    await fresh_store.put("connection", {"a": "b"})
    mode = stat.S_IMODE(os.stat(fresh_store.path).st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


async def test_default_config_created_mode_0600(tmp_config_path):
    # 新路徑首次 load（觸發建預設檔）後，預設檔權限同樣是 0600
    store = ConfigStore(path=tmp_config_path)
    await store.load()
    mode = stat.S_IMODE(os.stat(tmp_config_path).st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


async def test_reload_picks_up_external_change(fresh_store):
    await fresh_store.load()  # prime cache
    data = json.loads(fresh_store.path.read_text())
    data["admin_password"] = "changed-on-disk"
    fresh_store.path.write_text(json.dumps(data))
    # reload 前仍為 cache 值
    assert await fresh_store.get("admin_password") == "admin"
    await fresh_store.reload()
    assert await fresh_store.get("admin_password") == "changed-on-disk"


async def test_generation_initialization_persists_and_is_stable(fresh_store):
    first, second = await asyncio.gather(fresh_store.get_admin_auth(), fresh_store.get_admin_auth())
    assert first == second
    assert first[0] == "admin"
    assert isinstance(first[1], str) and first[1]
    reloaded = ConfigStore(path=fresh_store.path)
    assert await reloaded.get_admin_auth() == first


async def test_load_and_patch_results_cannot_mutate_live_config(fresh_store):
    auth = await fresh_store.get_admin_auth()
    snapshot = await fresh_store.load()
    snapshot["admin_password"] = "snapshot-password"
    snapshot["admin_session_generation"] = "snapshot-generation"
    snapshot["proxy"]["timeout"] = 1
    patched = await fresh_store.patch("connection", {"nested": {"marker": "original"}})
    patched["nested"]["marker"] = "modified"
    assert await fresh_store.get_admin_auth() == auth
    assert (await fresh_store.get("proxy"))["timeout"] == 86400
    assert (await fresh_store.get("connection"))["nested"]["marker"] == "original"


async def test_unrelated_concurrent_writes_preserve_password_generation(fresh_store):
    before = await fresh_store.get_admin_auth()
    await asyncio.gather(
        fresh_store.put("admin_password", "changed-password"),
        fresh_store.put("mcp_auth_token", "local-opaque-token"),
        fresh_store.patch("connection", {"fixture": "owned"}),
    )
    password, generation = await fresh_store.get_admin_auth()
    assert password == "changed-password"
    assert generation != before[1]
    assert await fresh_store.get("mcp_auth_token") == "local-opaque-token"
    assert (await fresh_store.get("connection"))["fixture"] == "owned"
    persisted = json.loads(fresh_store.path.read_text())
    assert (persisted["admin_password"], persisted["admin_session_generation"]) == (password, generation)


async def test_concurrent_password_writes_leave_coherent_last_committed_auth(fresh_store):
    original = await fresh_store.get_admin_auth()
    await asyncio.gather(
        fresh_store.put("admin_password", "first-password"),
        fresh_store.put("admin_password", "second-password"),
    )
    current = await fresh_store.get_admin_auth()
    assert current[0] in ("first-password", "second-password")
    assert current[1] != original[1]
    persisted = json.loads(fresh_store.path.read_text())
    assert (persisted["admin_password"], persisted["admin_session_generation"]) == current
    assert await ConfigStore(path=fresh_store.path).get_admin_auth() == current


async def test_failed_atomic_write_keeps_cache_and_file(fresh_store, monkeypatch):
    auth_before = await fresh_store.get_admin_auth()
    disk_before = fresh_store.path.read_bytes()

    def disk_failure(data):
        raise OSError("local fixture disk failure")

    with monkeypatch.context() as m:
        m.setattr(fresh_store, "_write_sync", disk_failure)
        with pytest.raises(OSError, match="local fixture disk failure"):
            await fresh_store.put("admin_password", "changed-password")
    assert await fresh_store.get_admin_auth() == auth_before
    assert fresh_store.path.read_bytes() == disk_before


async def test_full_save_cannot_restore_an_earlier_generation(fresh_store):
    original = await fresh_store.get_admin_auth()
    old_snapshot = await fresh_store.load()
    await fresh_store.put("admin_password", "changed-password")
    intermediate = await fresh_store.get_admin_auth()
    # Full replacement may intentionally restore the password, but must mint a new generation.
    await fresh_store.save(old_snapshot)
    current = await fresh_store.get_admin_auth()
    assert current[0] == original[0]
    assert current[1] not in (original[1], intermediate[1])


async def test_full_save_same_password_keeps_current_generation(fresh_store):
    current = await fresh_store.get_admin_auth()
    snapshot = await fresh_store.load()
    snapshot["admin_session_generation"] = "older-generation"
    snapshot["proxy"]["timeout"] = 42
    await fresh_store.save(snapshot)
    assert await fresh_store.get_admin_auth() == current
    assert (await fresh_store.get("proxy"))["timeout"] == 42


@pytest.mark.parametrize("cancel_count", [1, 2])
async def test_cancelled_disk_write_keeps_lock_until_cache_and_file_match(
    fresh_store, monkeypatch, cancel_count,
):
    """取消等待者不讓未結束的 writer 覆蓋下一次已提交的密碼。"""
    before = await fresh_store.get_admin_auth()
    started, release = threading.Event(), threading.Event()
    write = fresh_store._write_sync

    def held_write(data):
        if data["admin_password"] == before[0] and data["proxy"]["timeout"] == 42:
            started.set()
            if not release.wait(5):
                raise TimeoutError("controlled writer was not released")
        write(data)

    monkeypatch.setattr(fresh_store, "_write_sync", held_write)
    cancelled = asyncio.create_task(fresh_store.patch("proxy", {"timeout": 42}))
    password = None
    try:
        assert await asyncio.to_thread(started.wait, 2)
        cancelled.cancel()
        await asyncio.sleep(0.02)
        password = asyncio.create_task(fresh_store.put("admin_password", "changed-password"))
        await asyncio.sleep(0.02)
        if cancel_count == 2:
            cancelled.cancel()
            await asyncio.sleep(0.02)
        remained_serialized = not cancelled.done() and not password.done()
    finally:
        release.set()
        outcomes = await asyncio.gather(
            cancelled, *([password] if password is not None else []), return_exceptions=True,
        )
    assert remained_serialized, "cancellation released the lock while its disk writer was active"
    assert isinstance(outcomes[0], asyncio.CancelledError)
    assert outcomes[1] is None
    current = await fresh_store.get_admin_auth()
    assert current[0] == "changed-password" and current[1] != before[1]
    persisted = json.loads(fresh_store.path.read_text())
    assert (persisted["admin_password"], persisted["admin_session_generation"]) == current
    assert await ConfigStore(path=fresh_store.path).get_admin_auth() == current
    assert (await fresh_store.get("proxy"))["timeout"] == 42


async def test_peek_reads_the_last_committed_snapshot_without_waiting_for_the_lock(fresh_store, monkeypatch):
    """peek（給 /healthz 這種不能等鎖的讀者）：不拿鎖、不讀檔；還沒載入過拋 LookupError；寫檔進行中時是寫入前的版本、
    寫完才換；回傳的是複本，改它不會動到設定。"""
    with pytest.raises(LookupError):
        fresh_store.peek("connection")
    await fresh_store.patch("connection", {"base_url": "https://before.example.com"})
    started, release = threading.Event(), threading.Event()
    write = fresh_store._write_sync

    def held_write(data):
        started.set()
        if not release.wait(5):
            raise TimeoutError("controlled writer was not released")
        write(data)

    monkeypatch.setattr(fresh_store, "_write_sync", held_write)
    writer = asyncio.create_task(fresh_store.patch("connection", {"base_url": "https://after.example.com"}))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        assert fresh_store._lock.locked()
        assert fresh_store.peek("connection")["base_url"] == "https://before.example.com"
    finally:
        release.set()
        await writer
    assert fresh_store.peek("connection")["base_url"] == "https://after.example.com"
    fresh_store.peek("connection")["base_url"] = "mutated"
    assert fresh_store.peek("connection")["base_url"] == "https://after.example.com"
    assert (await fresh_store.get("connection"))["base_url"] == "https://after.example.com"
    assert fresh_store.peek("no_such_key", {"default": 1}) == {"default": 1}
