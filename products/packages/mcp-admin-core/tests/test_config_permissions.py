"""既有 PVC 重新掛載後的權限收緊：跑真正 ConfigStore，不改設定內容。"""

import os
import stat

import pytest

from mcp_admin_core.config.store import ConfigStore, ConfigUnavailable


@pytest.mark.parametrize("mounted_mode", [0o660, 0o640, 0o644])
async def test_restart_repairs_existing_config_without_rewriting(tmp_path, mounted_mode):
    path = tmp_path / "config.json"
    store = ConfigStore(path)
    await store.change_mcp_token("synthetic-test-token", action="set")
    await store.patch("connection", {"fixture": "synthetic-only"})
    before = path.read_bytes()
    path.chmod(mounted_mode)
    metadata = path.stat()

    restarted = ConfigStore(path)
    loaded = await restarted.load()

    after = path.stat()
    assert stat.S_IMODE(after.st_mode) == 0o600
    assert (after.st_ino, after.st_uid, after.st_gid, after.st_mtime_ns) == (
        metadata.st_ino, metadata.st_uid, metadata.st_gid, metadata.st_mtime_ns,
    )
    assert path.read_bytes() == before
    assert loaded == await store.load()
    await restarted.patch("proxy", {"timeout": 42})
    saved = path.read_bytes()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert (await ConfigStore(path).load())["proxy"]["timeout"] == 42
    assert path.read_bytes() == saved


@pytest.mark.parametrize("kind", ["symlink", "dangling", "hardlink", "directory", "fifo"])
async def test_unsafe_existing_file_fails_closed_without_touching_target(tmp_path, kind):
    path = tmp_path / "config.json"
    target = tmp_path / "target.json"
    target.write_text('{"admin_password":"synthetic-only"}')
    target.chmod(0o660)
    before = target.read_bytes()
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "dangling":
        path.symlink_to(tmp_path / "missing")
    elif kind == "hardlink":
        os.link(target, path)
    elif kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    store = ConfigStore(path)
    with pytest.raises(ConfigUnavailable, match="Configuration state unavailable"):
        await store.load()
    assert store._cache is None
    assert target.read_bytes() == before
    assert stat.S_IMODE(target.stat().st_mode) == 0o660
    assert not (tmp_path / "missing").exists()


async def test_other_owner_is_refused_before_chmod(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    store = ConfigStore(path)
    path.chmod(0o660)
    before = path.read_bytes()
    monkeypatch.setattr(os, "geteuid", lambda: path.stat().st_uid + 1)
    with pytest.raises(ConfigUnavailable):
        await store.load()
    assert path.read_bytes() == before
    assert stat.S_IMODE(path.stat().st_mode) == 0o660


async def test_permission_repair_failure_does_not_load_or_seed(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    store = ConfigStore(path)
    path.chmod(0o660)
    before = path.read_bytes()

    def denied(*args):
        raise PermissionError("synthetic permission denial")

    monkeypatch.setattr(os, "fchmod", denied)
    with pytest.raises(ConfigUnavailable):
        await store.load()
    assert store._cache is None
    assert path.read_bytes() == before
    assert stat.S_IMODE(path.stat().st_mode) == 0o660


async def test_private_file_needs_no_chmod_on_read(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    store = ConfigStore(path)

    def forbidden(*args):
        raise AssertionError("already-private file must not need chmod")

    monkeypatch.setattr(os, "fchmod", forbidden)
    assert (await store.load())["connection"] == {}


async def test_replaced_path_is_refused_before_fd_permission_change(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    store = ConfigStore(path)
    path.chmod(0o660)
    original = tmp_path / "original.json"
    real_open = os.open

    def racing_open(name, flags, *args, **kwargs):
        fd = real_open(name, flags, *args, **kwargs)
        if os.fspath(name) == str(path):
            path.rename(original)
            path.write_text("{}")
            path.chmod(0o660)
        return fd

    monkeypatch.setattr(os, "open", racing_open)
    with pytest.raises(ConfigUnavailable):
        await store.load()
    assert stat.S_IMODE(original.stat().st_mode) == 0o660
    assert stat.S_IMODE(path.stat().st_mode) == 0o660


async def test_atomic_save_never_chmods_a_replacement_symlink(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    store = ConfigStore(path)
    await store.load()
    outside = tmp_path / "outside.json"
    outside.write_text("unrelated synthetic file")
    outside.chmod(0o644)
    real_replace = os.replace

    def racing_replace(src, dst):
        real_replace(src, dst)
        path.unlink()
        path.symlink_to(outside)

    monkeypatch.setattr(os, "replace", racing_replace)
    await store.patch("proxy", {"timeout": 42})
    assert stat.S_IMODE(outside.stat().st_mode) == 0o644
    assert outside.read_text() == "unrelated synthetic file"


async def test_seed_and_save_are_private_before_any_bytes_are_written(tmp_path, monkeypatch):
    real_fdopen = os.fdopen
    modes = []

    class CheckedWriter:
        def __init__(self, fh):
            self.fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.fh.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.fh, name)

        def write(self, text):
            modes.append(stat.S_IMODE(os.fstat(self.fh.fileno()).st_mode))
            return self.fh.write(text)

    def checked_fdopen(fd, *args, **kwargs):
        fh = real_fdopen(fd, *args, **kwargs)
        if args and args[0] == "w":
            return CheckedWriter(fh)
        return fh

    monkeypatch.setattr(os, "fdopen", checked_fdopen)
    previous_umask = os.umask(0o777)
    try:
        store = ConfigStore(tmp_path / "config.json")
        await store.patch("proxy", {"timeout": 42})
    finally:
        os.umask(previous_umask)
    assert modes == [0o600, 0o600]
