"""In-place guarded restore: old backups never resurrect tokens; interrupted runs converge; marker last.

The live directory starts with the current (still valid) credentials, the source is an older backup
with different (since revoked) credentials. After a successful run every MCP token is revoked, none of
either credential set is active or even present, and the marker is gone. Any interruption leaves the
marker in place (the service refuses to start) and a plain re-run converges to the same result.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from mcp_admin_core import restore_inplace
from mcp_admin_core.restore_inplace import (EXIT_FAILED, EXIT_OK, EXIT_REFUSED, EXIT_USAGE, STEPS, WORK_NAME,
                                            trusted_live_root)
from mcp_admin_core.restore_marker import MARKER_NAME

PROFILE = "1234567890abcdef12345678"
SCHEMA = Path(__file__).parent / "fixtures" / "n8n_catalog_schema.sql"
CRASH = 57

# restore_inplace 設計上拒絕以 root（euid 0）執行（_check_paths：服務以 uid 1000 執行、只讀自己擁有的檔案，
# root 擁有的輸出永遠不會啟動），這是產品的安全設計，不提供任何繞過。凡是真的執行 restore（CLI 子行程或 restore()）
# 的測試都套用這個 marker；root 下 skip，非 root 照常執行。test_running_as_root_is_refused 不套用，root 下照樣驗證拒絕。
executes_restore = pytest.mark.skipif(
    os.geteuid() == 0,
    reason="restore_inplace 設計上拒絕 root；CI 以 uid 1000 執行",
)


@pytest.fixture(autouse=True)
def n8n_runtime_fixture(tmp_path, monkeypatch):
    """The n8n mode validates the installed n8n-mcp 2.96.2 package contract (same as restore_guard tests)."""
    package = tmp_path / "n8n-package"
    (package / "dist/mcp").mkdir(parents=True)
    (package / "dist/mcp/index.js").touch()
    (package / "package.json").write_text('{"name":"n8n-mcp","version":"2.96.2"}')
    monkeypatch.setenv("N8N_MCP_PACKAGE_DIR", str(package))


def write_tree(root: Path, tag: str, product: str = "generic", journal: bool = False) -> list[str]:
    """A two-profile config volume. Returns every credential it contains."""
    root.mkdir(exist_ok=True)
    child_dir = root / "profiles" / PROFILE
    child_dir.mkdir(parents=True)
    common = {"mcp_auth_revoked": False, "connection": {"odoo_url": f"https://{tag}.example"},
              "tools": {"disabled": [f"{tag}_disabled_tool"], "disabled_operations": {},
                        "owner_defaults_20261025": {"applied": "fresh_install"}},
              "token_history": [], "proxy": {}}
    default = {**common, "admin_password": f"{tag}-admin-password-a", "admin_session_generation": f"{tag}-session-a",
               "mcp_auth_generation": f"{tag}-generation-a", "mcp_auth_token": f"{tag}-token-default",
               "mcp_server": {"port": 18001}, "profile_index": {PROFILE: {"label": "B", "port": 18002}}}
    child = {**common, "admin_password": f"{tag}-admin-password-b", "admin_session_generation": f"{tag}-session-b",
             "mcp_auth_generation": f"{tag}-generation-b", "mcp_auth_token": f"{tag}-token-profile",
             "mcp_server": {"port": 18002}}
    secrets = [default["admin_password"], child["admin_password"], default["mcp_auth_token"], child["mcp_auth_token"]]
    for i, (directory, config) in enumerate(((root, default), (child_dir, child))):
        if product == "n8n":
            bearer = f"{tag}-bearer-{i}"
            config = {**config, "mcp_server": {**config["mcp_server"], "env": {"AUTH_TOKEN": bearer}},
                      "proxy": {"bearer_token": bearer}}
            secrets.append(bearer)
            database = directory / "n8n-mcp"
            database.mkdir()
            with sqlite3.connect(database / "nodes.db") as db:
                db.executescript(SCHEMA.read_text())
                db.execute("insert into nodes(node_type,package_name,display_name) values(?,?,?)",
                           (f"n8n-{tag}-{i}", "synthetic", "Synthetic"))
                db.execute("insert into nodes_fts(rowid,node_type,display_name) "
                           "select rowid,node_type,display_name from nodes")
                db.execute("create table workflow_history(id integer primary key, marker text)")
                db.execute("insert into workflow_history(marker) values(?)", (f"{tag}-history-{i}",))
        path = directory / "config.json"
        path.write_text(json.dumps(config))
        path.chmod(0o600)
        if journal:
            from mcp_admin_core.operations import OperationJournal
            OperationJournal(path)._write({"version": 1, "key": "a" * 64, "expired_before_ms": 0, "receipts": {}})
    return secrets


def seed(tmp_path: Path, product: str = "generic") -> tuple[Path, Path, list[str], list[str]]:
    source, live = tmp_path / "backup", tmp_path / "live"
    old = write_tree(source, "backup", product)
    current = write_tree(live, "live", product, journal=True)
    # Live-only clutter that must not survive, and ext4's lost+found that must be left alone.
    (live / "kubeconfig").write_text("live-only-derived-file")
    (live / ".config.json.tmp.1.abcd").write_text("interrupted-atomic-write live-token-default")
    (live / "lost+found").mkdir()
    (live / "lost+found" / "#1234").write_text("fsck-recovered")
    return source, live, old, current


def cli(*args: str | Path, mode: str | None = None, target: str = "") -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    if mode is None:
        command = [sys.executable, "-m", "mcp_admin_core.restore_inplace", *map(str, args)]
    else:
        command = [sys.executable, "-c", INJECT, mode, target, *map(str, args)]
    return subprocess.run(command, env=env, capture_output=True, text=True, timeout=120, check=False)


# Interruption injection: "after" kills the process right after a step completes, "raise" makes the
# step fail with an exception carrying secret-looking text; the mid-* modes kill it inside a step.
INJECT = r'''
import os, sys
from pathlib import Path
from mcp_admin_core import restore_guard as g, restore_inplace as r
mode, target, argv = sys.argv[1], sys.argv[2], sys.argv[3:]
if mode in ("after", "raise"):
    def hook(name):
        if name == target:
            if mode == "after":
                os._exit(57)
            raise OSError("private detail live-token-default backup-token-default")
    r._after_step = hook
elif mode == "pause":
    def hook(name):
        if name == target:
            Path(os.environ["RESTORE_TEST_PAUSED"]).touch()
            __import__("time").sleep(120)
    r._after_step = hook
elif mode == "mid-stage":
    real, count = g._write_config, [0]
    def write(path, config):
        count[0] += 1
        if count[0] == 2:
            os._exit(57)
        real(path, config)
    g._write_config = write
elif mode == "mid-move":
    real, count = os.rename, [0]
    def rename(*args, **kwargs):
        count[0] += 1
        if count[0] == 2:
            os._exit(57)
        return real(*args, **kwargs)
    os.rename = rename
elif mode == "mid-publish":
    real, count = g._publish, [0]
    def publish(source, destination):
        count[0] += 1
        if count[0] == 3:
            os._exit(57)
        return real(source, destination)
    g._publish = publish
elif mode == "mid-discard":
    real = r.shutil.rmtree
    def rmtree(path, *args, **kwargs):
        if Path(path).name == "pre":
            batch = sorted(Path(path).iterdir())[0]
            first = sorted(batch.iterdir())[0]
            real(first) if first.is_dir() else first.unlink()
            os._exit(57)
        return real(path, *args, **kwargs)
    r.shutil.rmtree = rmtree
elif mode == "tamper":
    real = g._write_config
    def write(path, config):
        real(path, config)
        if path.parent.name != "profiles" and "profiles" in path.parts:
            value = __import__("json").loads(path.read_text())
            if target == "token":
                value["mcp_auth_token"] = "tampered-active-token"
            elif target == "revoked":
                value["mcp_auth_revoked"] = False
            elif target == "port":
                value["mcp_server"]["port"] = 18003
            path.write_text(__import__("json").dumps(value))
    g._write_config = write
    if target == "journal":
        real_journal = g._write_journal
        def journal(path, cutoff):
            if "profiles" not in path.parts:
                real_journal(path, cutoff)
        g._write_journal = journal
sys.exit(r.main(argv))
'''


def files_under(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file() and not p.is_symlink() and "lost+found" not in p.parts]


def assert_restored(live: Path, secrets: list[str], product: str = "generic") -> None:
    assert not os.path.lexists(live / MARKER_NAME)
    assert not os.path.lexists(live / WORK_NAME)
    expected = {"config.json", ".config.json.operations.json", "profiles", "lost+found"}
    if product == "n8n":
        expected.add("n8n-mcp")
    assert set(os.listdir(live)) == expected
    assert (live / "lost+found" / "#1234").read_text() == "fsck-recovered"
    bearers = []
    for i, directory in enumerate((live, live / "profiles" / PROFILE)):
        config = json.loads((directory / "config.json").read_text())
        assert config["mcp_auth_token"] == "" and config["mcp_auth_revoked"] is True
        assert config["tools"]["disabled"] == ["backup_disabled_tool"]  # content comes from the backup
        assert config["connection"] == {"odoo_url": "https://backup.example"}
        assert stat.S_IMODE((directory / "config.json").stat().st_mode) == 0o600
        assert (directory / ".config.json.operations.json").is_file()
        if product == "n8n":
            bearer = config["mcp_server"]["env"]["AUTH_TOKEN"]
            assert bearer and config["proxy"]["bearer_token"] == bearer
            bearers.append(bearer)
            with sqlite3.connect(directory / "n8n-mcp" / "nodes.db") as db:
                assert db.execute("select marker from workflow_history").fetchone() == (f"backup-history-{i}",)
    assert len(set(bearers)) == len(bearers)
    assert set(os.listdir(live / "profiles")) == {PROFILE}
    for path in files_under(live):
        data = path.read_bytes()
        for secret in secrets:
            assert secret.encode() not in data, (path, secret)


def assert_gateway_rejects(live: Path, tokens: list[str], monkeypatch) -> None:
    """The restored volume, served by the real gateway: every old token is refused."""
    import mcp_admin_core.config.store as store_mod
    from fastapi.testclient import TestClient
    from mcp_admin_core import create_app

    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(live / "config.json"))
    monkeypatch.setattr(store_mod, "_instance", None)
    client = TestClient(create_app(title="Restored"))
    for token in tokens:
        response = client.post(f"/private_{token}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert response.status_code == 403, token


def service_refuses(live: Path, monkeypatch) -> bool:
    """Would the admin process start on this volume right now?"""
    import mcp_admin_core.config.store as store_mod
    from mcp_admin_core import create_app
    from mcp_admin_core.restore_marker import RestoreInProgress
    from test_restore_marker import drive_lifespan

    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(live / "config.json"))
    monkeypatch.setattr(store_mod, "_instance", None)
    sent, error = asyncio.run(drive_lifespan(create_app(title="Interrupted")))
    return sent == ["lifespan.startup.failed"] and isinstance(error, RestoreInProgress)


@executes_restore
@pytest.mark.parametrize("product", ["generic", "n8n"])
def test_restore_revokes_backup_and_live_credentials_and_removes_marker(tmp_path, monkeypatch, product):
    source, live, old, current = seed(tmp_path, product)
    source_bytes = {p: p.read_bytes() for p in files_under(source)}
    result = cli("--product", product, source, live)
    assert result.returncode == EXIT_OK, result.stderr
    assert result.stdout.startswith("MCP in-place restore completed")
    assert_restored(live, old + current, product)
    assert {p: p.read_bytes() for p in files_under(source)} == source_bytes  # the snapshot is never modified
    for secret in old + current:
        assert secret not in result.stdout + result.stderr
    assert_gateway_rejects(live, [s for s in old + current if "-token-" in s], monkeypatch)


@executes_restore
@pytest.mark.parametrize("step", STEPS)
def test_crash_after_each_step_keeps_marker_then_rerun_converges(tmp_path, monkeypatch, step):
    source, live, old, current = seed(tmp_path)
    crashed = cli(source, live, mode="after", target=step)
    assert crashed.returncode == CRASH
    if step != "unmark":
        assert (live / MARKER_NAME).is_file()  # the marker goes last: any earlier stop keeps it
        assert service_refuses(live, monkeypatch)
    rerun = cli(source, live)
    assert rerun.returncode == EXIT_OK, rerun.stderr
    assert_restored(live, old + current)


@executes_restore
@pytest.mark.parametrize("mode", ["mid-stage", "mid-move", "mid-publish", "mid-discard"])
@pytest.mark.parametrize("product", ["generic", "n8n"])
def test_crash_inside_a_step_then_rerun_starts_over_and_converges(tmp_path, monkeypatch, mode, product):
    source, live, old, current = seed(tmp_path, product)
    crashed = cli("--product", product, source, live, mode=mode)
    assert crashed.returncode == CRASH
    assert (live / MARKER_NAME).is_file()
    assert service_refuses(live, monkeypatch)
    if mode == "mid-publish":
        # Partial output is live now; it must not be trusted, only replaced.
        assert (live / ".config.json.operations.json").exists() and not (live / "profiles").exists()
    rerun = cli("--product", product, source, live)
    assert rerun.returncode == EXIT_OK, rerun.stderr
    assert_restored(live, old + current, product)


@executes_restore
@pytest.mark.parametrize("step", STEPS[:-1])
def test_failure_at_each_step_keeps_marker_and_prints_only_fixed_text(tmp_path, step):
    source, live, old, current = seed(tmp_path)
    failed = cli(source, live, mode="raise", target=step)
    assert failed.returncode == EXIT_FAILED
    assert failed.stderr.strip().splitlines()[-1] == restore_inplace.FAILED.format(step=step)
    assert failed.stdout == ""
    assert (live / MARKER_NAME).is_file()
    assert cli(source, live).returncode == EXIT_OK
    assert_restored(live, old + current)


@executes_restore
def test_invalid_backup_fails_before_moving_live_data(tmp_path, monkeypatch):
    source, live, old, current = seed(tmp_path)
    (source / "profiles" / PROFILE / "config.json").write_text('{"mcp_auth_token":"a","mcp_auth_token":"b"}')
    before = {p: p.read_bytes() for p in files_under(live)}
    failed = cli(source, live)
    assert failed.returncode == EXIT_FAILED
    assert "step prepare" in failed.stderr
    assert (live / MARKER_NAME).is_file() and service_refuses(live, monkeypatch)
    # Nothing of the live state was moved: an operator abort (remove work dir, then marker) is still possible.
    assert {p: p.read_bytes() for p in files_under(live) if WORK_NAME not in p.parts and p.name != MARKER_NAME} == before
    for secret in old + current:
        assert secret not in failed.stdout + failed.stderr


@executes_restore
@pytest.mark.parametrize("tamper", ["token", "revoked", "port", "journal"])
def test_verification_rejects_a_bad_publication_and_keeps_old_state_for_rerun(tmp_path, tamper):
    source, live, old, current = seed(tmp_path)
    failed = cli(source, live, mode="tamper", target=tamper)
    assert failed.returncode == EXIT_FAILED
    assert failed.stderr.strip().splitlines()[-1] == restore_inplace.FAILED.format(step="verify")
    assert (live / MARKER_NAME).is_file()
    assert "tampered-active-token" not in failed.stderr
    assert cli(source, live).returncode == EXIT_OK
    assert_restored(live, old + current + ["tampered-active-token"])


@executes_restore
def test_rerun_after_success_is_a_fresh_restore(tmp_path):
    source, live, old, current = seed(tmp_path)
    assert cli(source, live).returncode == EXIT_OK
    first = json.loads((live / "config.json").read_text())
    assert cli(source, live).returncode == EXIT_OK
    assert_restored(live, old + current + [first["admin_password"]])


def test_refusals_change_nothing(tmp_path):
    source, live, old, current = seed(tmp_path)
    snapshot = {p: p.read_bytes() for p in files_under(tmp_path)}
    nested_source = live / "profiles" / PROFILE
    outer = tmp_path / "outer"
    cases = [(live, live), (nested_source, live), (source, source / "profiles"), (tmp_path / "missing", live),
             (source, tmp_path / "missing"), (source / "config.json", live)]
    link = tmp_path / "live-link"
    link.symlink_to(live)
    cases.append((source, link))
    for case_source, case_live in cases:
        result = cli(case_source, case_live)
        assert result.returncode == EXIT_REFUSED, (case_source, case_live)
        assert result.stderr.strip().splitlines()[-1] == restore_inplace.REFUSED
    assert not os.path.lexists(live / MARKER_NAME) and not os.path.lexists(live / WORK_NAME)
    live.chmod(0o777)
    assert cli(source, live).returncode == EXIT_REFUSED  # world-writable live dir
    live.chmod(0o755)
    (live / MARKER_NAME).mkdir()
    assert cli(source, live).returncode == EXIT_REFUSED  # an unexpected marker type is not ours to remove
    (live / MARKER_NAME).rmdir()
    (live / WORK_NAME).symlink_to(outer)
    assert cli(source, live).returncode == EXIT_REFUSED
    (live / WORK_NAME).unlink()
    assert {p: p.read_bytes() for p in files_under(tmp_path)} == snapshot
    assert not outer.exists()
    usage = cli("--product", "other", source, live)
    assert usage.returncode == EXIT_USAGE and usage.stderr.strip().splitlines()[-1] == "Invalid restore arguments"


def _stat(mode: int, uid: int, gid: int) -> os.stat_result:
    return os.stat_result((mode, 1, 1, 2, uid, gid, 4096, 0, 0, 0))


@pytest.mark.parametrize("info, is_mount, trusted", [
    (_stat(stat.S_IFDIR | 0o755, 1000, 1000), False, True),           # owned by us
    (_stat(stat.S_IFDIR | 0o2775, 1000, 1000), True, True),           # ours, fsGroup g+rwx
    (_stat(stat.S_IFDIR | 0o2775, 0, 1000), True, True),              # Kubernetes volume root (woow-k3s E1)
    (_stat(stat.S_IFDIR | 0o2775, 0, 1000), False, False),            # root-owned but not a volume root
    (_stat(stat.S_IFDIR | 0o2775, 0, 2000), True, False),             # another group may write
    (_stat(stat.S_IFDIR | 0o755, 0, 2000), True, False),              # root-owned volume not given our group
    (_stat(stat.S_IFDIR | 0o755, 1001, 1000), False, False),          # another user
    (_stat(stat.S_IFDIR | 0o777, 1000, 1000), False, False),          # world-writable
    (_stat(stat.S_IFDIR | 0o1777, 0, 0), True, False),                # sticky world-writable
    (_stat(stat.S_IFLNK | 0o777, 1000, 1000), False, False),          # symlink
    (_stat(stat.S_IFREG | 0o600, 1000, 1000), False, False),          # not a directory
])
def test_live_root_trust_rule(info, is_mount, trusted):
    assert trusted_live_root(info, is_mount=is_mount, euid=1000, egid=1000) is trusted


# ---- restore_guard.prepare(allow_volume_root=True): a restored Kubernetes volume mounted read-only ----------------
# woow-k3s (longhorn-delete, fsGroup 1000) volume roots are root:1000 2775 mount points with ext4's lost+found
# (k8s/e1-pvc-root.json in the evidence directory). Only that root-level directory is ignored, only for a read-only
# mount point, and only when the caller opts in; every other check is unchanged.


def _private(path: Path) -> Path:
    path.mkdir(mode=0o700)
    return path / "out"


def test_volume_root_source_ignores_only_its_own_lost_and_found(tmp_path, monkeypatch):
    from mcp_admin_core import restore_guard as guard

    source = tmp_path / "backup"
    write_tree(source, "backup")
    (source / "lost+found").mkdir()
    for allow in (False, True):  # not a read-only mount: lost+found is an unexpected entry, as before
        with pytest.raises(guard.RestoreError):
            guard.prepare(source, _private(tmp_path / f"plain-{allow}"), allow_volume_root=allow)
    monkeypatch.setattr(guard, "_read_only_mount", lambda path: Path(path) == source)
    with pytest.raises(guard.RestoreError):  # opt-in only
        guard.prepare(source, _private(tmp_path / "not-opted-in"))
    out = _private(tmp_path / "volume")
    guard.prepare(source, out, allow_volume_root=True)
    assert set(os.listdir(out)) == {"config.json", ".config.json.operations.json", "profiles"}
    assert json.loads((out / "config.json").read_text())["mcp_auth_revoked"] is True
    (source / "lost+found").rmdir()
    (source / "lost+found").symlink_to(source / "profiles")  # must be a real directory
    with pytest.raises(guard.RestoreError):
        guard.prepare(source, _private(tmp_path / "symlink"), allow_volume_root=True)
    (source / "lost+found").unlink()
    (source / "lost+found").write_text("not a directory")
    with pytest.raises(guard.RestoreError):
        guard.prepare(source, _private(tmp_path / "file"), allow_volume_root=True)
    (source / "lost+found").unlink()
    (source / "profiles" / PROFILE / "lost+found").mkdir()  # only at the volume root
    with pytest.raises(guard.RestoreError):
        guard.prepare(source, _private(tmp_path / "nested"), allow_volume_root=True)


def test_volume_root_owner_rule():
    from mcp_admin_core import restore_guard as guard

    guard._volume_root(Path("/"))  # root-owned, not world-writable: accepted as a volume root
    with pytest.raises(guard.RestoreError):
        guard._volume_root(Path("/tmp"))  # world-writable
    with pytest.raises(guard.RestoreError):
        guard._volume_root(Path("/etc/hostname"))  # not a directory


def test_read_only_mount_detection(tmp_path):
    from mcp_admin_core import restore_guard as guard

    assert guard._read_only_mount(tmp_path) is False  # not a mount point
    candidates = [Path(p) for p in ("/sys", "/proc/sys") if os.path.ismount(p)]
    readonly = [p for p in candidates if os.statvfs(p).f_flag & os.ST_RDONLY]
    if not readonly:
        pytest.skip("no read-only mount point available in this environment")
    assert guard._read_only_mount(readonly[0]) is True


@executes_restore
def test_in_place_restore_from_a_read_only_volume_root_with_lost_and_found(tmp_path, monkeypatch):
    from mcp_admin_core import restore_guard as guard

    source, live, old, current = seed(tmp_path)
    (source / "lost+found").mkdir()
    monkeypatch.setattr(guard, "_read_only_mount", lambda path: Path(path) == source)
    restore_inplace.restore(source, live)
    assert_restored(live, old + current)


@executes_restore
def test_overlapping_run_is_refused_while_another_holds_the_marker(tmp_path, monkeypatch):
    source, live, old, current = seed(tmp_path)
    paused = tmp_path / "paused"
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "RESTORE_TEST_PAUSED": str(paused)}
    first = subprocess.Popen([sys.executable, "-c", INJECT, "pause", "move_aside", str(source), str(live)],
                             env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        for _ in range(600):
            if paused.exists() or first.poll() is not None:
                break
            __import__("time").sleep(0.05)
        assert paused.exists()  # the first run moved the live entries aside and holds the marker lock
        snapshot = sorted(str(p.relative_to(live)) for p in live.rglob("*"))
        second = cli(source, live)
        assert second.returncode == EXIT_REFUSED
        assert second.stderr.strip().splitlines()[-1] == restore_inplace.BUSY
        assert sorted(str(p.relative_to(live)) for p in live.rglob("*")) == snapshot  # changed nothing
        assert service_refuses(live, monkeypatch)
    finally:
        first.kill()
        first.communicate(timeout=30)
    assert cli(source, live).returncode == EXIT_OK  # the lock dies with the process; a re-run converges
    assert_restored(live, old + current)


def test_running_as_root_is_refused(tmp_path, monkeypatch):
    source, live, _, _ = seed(tmp_path)
    monkeypatch.setattr(restore_inplace.os, "geteuid", lambda: 0)
    with pytest.raises(restore_inplace.Refused):
        restore_inplace.restore(source, live)
    assert not os.path.lexists(live / MARKER_NAME)


# F3 restore-1: the platform's reset-config leaves .config.json.reset-backup-<32 hex> (previous config.json, old
# credentials) on the config volume. A backup taken afterwards contains it.
RESET_BACKUP = ".config.json.reset-backup-" + "fedcba9876543210" * 2


@executes_restore
@pytest.mark.parametrize("product", ["generic", "n8n"])
def test_source_and_live_reset_backups_are_never_restored_and_all_tokens_die(tmp_path, monkeypatch, product):
    source, live, old, current = seed(tmp_path, product)
    # Old plaintext credentials sit inside both leftovers, as the real operator's copy of config.json would.
    stale_source = json.dumps({"admin_password": "source-reset-admin-secret", "mcp_auth_token": "source-reset-token"})
    stale_live = json.dumps({"admin_password": "live-reset-admin-secret", "mcp_auth_token": "live-reset-token"})
    (source / RESET_BACKUP).write_text(stale_source)
    (live / RESET_BACKUP).write_text(stale_live)
    result = cli("--product", product, source, live)
    assert result.returncode == EXIT_OK, result.stderr
    secrets_ = old + current + ["source-reset-admin-secret", "source-reset-token",
                                "live-reset-admin-secret", "live-reset-token"]
    assert not os.path.lexists(live / RESET_BACKUP)
    assert_restored(live, secrets_, product)  # exact entry set, no secret anywhere in live
    assert (source / RESET_BACKUP).read_text() == stale_source  # the snapshot is untouched
    for secret in secrets_:
        assert secret not in result.stdout + result.stderr
    assert_gateway_rejects(live, [s for s in secrets_ if "token" in s], monkeypatch)


@executes_restore
@pytest.mark.parametrize("step", STEPS)
def test_crash_with_reset_backups_present_then_rerun_converges(tmp_path, step):
    source, live, old, current = seed(tmp_path)
    (source / RESET_BACKUP).write_text("source-reset-admin-secret")
    (live / RESET_BACKUP).write_text("live-reset-admin-secret")
    assert cli(source, live, mode="after", target=step).returncode == CRASH
    assert cli(source, live).returncode == EXIT_OK
    assert not os.path.lexists(live / RESET_BACKUP)
    assert_restored(live, old + current + ["source-reset-admin-secret", "live-reset-admin-secret"])


@executes_restore
def test_live_only_reset_backup_is_discarded(tmp_path):
    source, live, old, current = seed(tmp_path)
    (live / RESET_BACKUP).write_text("live-reset-admin-secret")
    assert cli(source, live).returncode == EXIT_OK
    assert_restored(live, old + current + ["live-reset-admin-secret"])


@executes_restore
def test_source_reset_backup_lookalike_still_fails_before_touching_live(tmp_path):
    source, live, old, current = seed(tmp_path)
    (source / (RESET_BACKUP + ".json")).write_text("x")
    before = {p.name: p.read_bytes() for p in live.iterdir() if p.is_file()}
    result = cli(source, live)
    assert result.returncode == EXIT_FAILED
    assert {p.name for p in live.iterdir() if p.is_file()} - {MARKER_NAME} == set(before)


# 2026-11-13 多帳號實測（mp-gap-prod-restore）P1：Headscale 的管理程式每次啟動都會在設定目錄（卷根目錄）建立
# admin-sessions.sqlite3（apps/headscale/headscale_mcp_admin/admin_sessions.py：每次登入的撤銷紀錄與隨機 epoch），
# 所以 Headscale 設定卷的每一份備份都有它；guard 原本不認得這個檔，照手冊還原一律在 prepare 失敗。
# 下面的卷照正式站 Headscale 卷的實際形狀建立（沙盒證據 artifacts/mcp-security-release-20261112/
# mp-gap-prod-restore-20261113/receipts/inventory-headscale-backup.json，不在本 repo）：根目錄 config.json、
# .config.json.operations.json、admin-sessions.sqlite3、profiles/ 下三個帳號各自的 config.json 與 journal。
SESSIONS = "admin-sessions.sqlite3"
HEADSCALE_PROFILES = {"c7666c71747e64962b7eefa9": 18000, "1d7ae1b9b680b0e4e5455fee": 18001,
                      "1e39440e3dcaaf3034f09a0d": 18002}


def write_admin_sessions(path: Path, canary: str) -> None:
    """Same schema as headscale_mcp_admin.admin_sessions (random epoch + hashed revocations); *canary* is stored in it."""
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE session_state (id INTEGER PRIMARY KEY CHECK (id = 1), epoch TEXT NOT NULL)")
        db.execute("INSERT INTO session_state VALUES (1, ?)", (os.urandom(16).hex(),))
        db.execute("CREATE TABLE revoked_sessions (jti_hash TEXT PRIMARY KEY, expires_at INTEGER NOT NULL)")
        db.execute("CREATE INDEX revoked_expiry ON revoked_sessions (expires_at)")
        db.execute("INSERT INTO revoked_sessions VALUES (?, ?)", (canary, 4102444800))
    db.close()
    path.chmod(0o600)


def write_headscale_tree(root: Path, tag: str) -> list[str]:
    """A Headscale config volume: default profile + three profiles, operation journals, admin sessions database.

    Returns every MCP credential and session canary it contains (not the backend connection secrets, which a
    restore intentionally keeps, runbook step 10)."""
    from mcp_admin_core.operations import OperationJournal

    root.mkdir(exist_ok=True)
    common = {"mcp_auth_revoked": False, "admin_session_revocations": {}, "proxy": {"timeout": 86400},
              "connection": {"base_url": f"http://hs-be-{tag}.invalid:8080", "api_key": f"{tag}-headscale-api-key",
                             "enrollment_url": "", "headplane_url": "", "paas_base_url": "", "paas_api_token": "",
                             "workspace_id": "", "headscale_service_id": ""},
              "tools": {"disabled": [f"{tag}_disabled_tool"], "disabled_operations": {"headscale_users": ["delete"]},
                        "owner_defaults_20261025": {"applied": "fresh_install", "source": "existing_config"}}}
    secrets = []
    configs = [(root, 8000, {pid: {"label": f"{tag}-{i}", "port": port}
                             for i, (pid, port) in enumerate(HEADSCALE_PROFILES.items())})]
    configs += [(root / "profiles" / pid, port, {}) for pid, port in HEADSCALE_PROFILES.items()]
    for i, (directory, port, index) in enumerate(configs):
        directory.mkdir(parents=True, exist_ok=True)
        config = {**common, "admin_password": f"{tag}-admin-password-{i}",
                  "admin_session_generation": f"{tag}-session-{i}", "mcp_auth_generation": f"{tag}-generation-{i}",
                  "mcp_auth_token": f"{tag}-token-{i}",
                  "token_history": [{"action": "auto_issue", "at": "2026-11-13T04:56:36Z"},
                                    {"action": "rotate", "at": "2026-11-13T04:57:25Z"}],
                  "mcp_server": {"command": "headscale-mcp-server", "args": [], "port": port, "env": {},
                                 "env_prefix": "HEADSCALE_MCP_"},
                  "profile_index": index}
        secrets += [config["admin_password"], config["mcp_auth_token"]]
        path = directory / "config.json"
        path.write_text(json.dumps(config))
        path.chmod(0o600)
        OperationJournal(path)._write({"version": 1, "key": "b" * 64, "expired_before_ms": 0, "receipts": {}})
    canary = f"{tag}-revoked-session-canary"
    write_admin_sessions(root / SESSIONS, canary)
    return secrets + [canary]


def seed_headscale(tmp_path: Path) -> tuple[Path, Path, list[str], list[str]]:
    source, live = tmp_path / "backup", tmp_path / "live"
    old = write_headscale_tree(source, "backup")
    current = write_headscale_tree(live, "live")
    (live / "lost+found").mkdir()
    (live / "lost+found" / "#1234").write_text("fsck-recovered")
    return source, live, old, current


def assert_headscale_restored(live: Path, secrets: list[str]) -> None:
    assert not os.path.lexists(live / MARKER_NAME) and not os.path.lexists(live / WORK_NAME)
    # Exactly guard output plus lost+found: no admin-sessions.sqlite3 from the backup or from before the restore.
    # The admin process creates a new, empty one (new random epoch) when it starts on this volume.
    # (restore-operation-journals: the backup's operation journal, archived by the guard for audit.)
    published = {"config.json", ".config.json.operations.json", "restore-operation-journals"}
    assert set(os.listdir(live)) == published | {"profiles", "lost+found"}
    assert set(os.listdir(live / "profiles")) == set(HEADSCALE_PROFILES)
    for directory in (live, *(live / "profiles" / pid for pid in HEADSCALE_PROFILES)):
        assert set(os.listdir(directory)) - {"profiles", "lost+found"} == published
        config = json.loads((directory / "config.json").read_text())
        assert config["mcp_auth_token"] == "" and config["mcp_auth_revoked"] is True
        assert config["tools"]["disabled"] == ["backup_disabled_tool"]  # policy comes from the backup
        assert config["tools"]["disabled_operations"] == {"headscale_users": ["delete"]}
        assert config["connection"]["base_url"] == "http://hs-be-backup.invalid:8080"
    for path in files_under(live):
        data = path.read_bytes()
        for secret in secrets:
            assert secret.encode() not in data, (path, secret)


@executes_restore
def test_headscale_volume_restores_without_its_admin_sessions_database(tmp_path, monkeypatch):
    source, live, old, current = seed_headscale(tmp_path)
    source_bytes = {p: p.read_bytes() for p in files_under(source)}
    result = cli(source, live)
    assert result.returncode == EXIT_OK, result.stderr
    assert_headscale_restored(live, old + current)
    assert {p: p.read_bytes() for p in files_under(source)} == source_bytes  # the snapshot is never modified
    for secret in old + current:
        assert secret not in result.stdout + result.stderr
    assert_gateway_rejects(live, [s for s in old + current if "-token-" in s], monkeypatch)


@executes_restore
@pytest.mark.parametrize("companions", [("-journal",), ("-wal", "-shm"), ("-journal", "-wal", "-shm")])
def test_headscale_admin_sessions_sqlite_companions_are_never_restored(tmp_path, monkeypatch, companions):
    # A backup taken while the service ran can catch SQLite's rollback journal (or WAL/SHM). They belong to the
    # database that is not restored; published next to the new database a hot journal would even be replayed into it.
    source, live, old, current = seed_headscale(tmp_path)
    for suffix in companions:
        (source / (SESSIONS + suffix)).write_bytes(b"backup-sessions-companion-canary")
        (live / (SESSIONS + suffix)).write_bytes(b"live-sessions-companion-canary")
    result = cli(source, live)
    assert result.returncode == EXIT_OK, result.stderr
    assert_headscale_restored(live, old + current + ["backup-sessions-companion-canary", "live-sessions-companion-canary"])
    for suffix in companions:
        assert (source / (SESSIONS + suffix)).read_bytes() == b"backup-sessions-companion-canary"
    assert_gateway_rejects(live, [s for s in old + current if "-token-" in s], monkeypatch)


@executes_restore
@pytest.mark.parametrize("step", STEPS)
def test_headscale_volume_crash_after_each_step_then_rerun_converges(tmp_path, monkeypatch, step):
    source, live, old, current = seed_headscale(tmp_path)
    (source / (SESSIONS + "-journal")).write_bytes(b"backup-sessions-companion-canary")
    crashed = cli(source, live, mode="after", target=step)
    assert crashed.returncode == CRASH
    if step != "unmark":
        assert (live / MARKER_NAME).is_file()
        assert service_refuses(live, monkeypatch)
    rerun = cli(source, live)
    assert rerun.returncode == EXIT_OK, rerun.stderr
    assert_headscale_restored(live, old + current + ["backup-sessions-companion-canary"])


@executes_restore
def test_headscale_volume_with_a_reset_config_leftover_restores_neither(tmp_path, monkeypatch):
    # After runbook step 8 (platform reset-config) the operator's cleanup can leave .config.json.reset-backup-<hex>
    # next to admin-sessions.sqlite3 (seen again in the 2026-11-13 sandbox): a later backup holds both. Neither comes back.
    source, live, old, current = seed_headscale(tmp_path)
    stale = json.dumps({"admin_password": "source-reset-admin-secret", "mcp_auth_token": "source-reset-token"})
    (source / RESET_BACKUP).write_text(stale)
    (live / RESET_BACKUP).write_text(json.dumps({"admin_password": "live-reset-admin-secret"}))
    result = cli(source, live)
    assert result.returncode == EXIT_OK, result.stderr
    assert_headscale_restored(live, old + current + ["source-reset-admin-secret", "source-reset-token",
                                                     "live-reset-admin-secret"])
    assert (source / RESET_BACKUP).read_text() == stale
    assert_gateway_rejects(live, [s for s in old + current if "-token-" in s] + ["source-reset-token"], monkeypatch)
