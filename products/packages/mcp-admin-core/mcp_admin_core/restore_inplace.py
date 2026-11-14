"""In-place guarded restore of an MCP config volume; every restored MCP token is revoked.

    python -m mcp_admin_core.restore_inplace [--product generic|n8n] <source-snapshot-dir> <live-data-dir>

Intended to run as a Kubernetes Job (uid/gid 1000, the service's own image) while the service is
scaled to 0. ``source`` is the restored backup volume, mounted read-only; ``live`` is the service
volume root (the parent of ``MCP_ADMIN_CONFIG``). The caller MUST keep every replica stopped until
this command exits 0; it cannot see or stop other pods.

Steps (each re-run starts over from the source and never trusts partial output):

1. ``marker``: create ``<live>/.mcp-restore-in-progress`` (fsync file and directory). While it
   exists the service refuses to start (:mod:`restore_marker`). The run holds an exclusive
   ``flock`` on it until it exits, so an overlapping run (a stuck Job pod and its replacement on
   the same node) is refused instead of interleaving with this one.
2. ``workdir``: private ``<live>/.mcp-restore-inplace`` (0700). Everything in it except ``pre``
   (left by an interrupted run) is discarded.
3. ``prepare``: :func:`restore_guard.prepare` from the source into ``<work>/out``.
4. ``move_aside``: every live entry except the marker, the work dir and ``lost+found`` is renamed
   into a new batch directory under ``<work>/pre``.
5. ``publish``: every entry of ``<work>/out`` is renamed into the live dir (no replacement).
6. ``sync``: fsync the live and work directories.
7. ``verify``: the live dir holds exactly the published entries; every profile has an empty MCP
   token and ``mcp_auth_revoked=true``; ports are unique; expected files exist; no credential of
   the source is active.
8. ``discard_live``: remove ``<work>/pre`` (it holds the previous, still valid tokens).
9. ``remove_workdir`` and ``unmark``: remove the work dir, then the marker last; fsync.

Output is limited to fixed messages. Exit codes: 0 done; 1 failed (marker kept, service must stay
down, fix and re-run); 2 invalid arguments; 3 refused before changing anything (unsafe paths,
running as root, or another restore holding the marker).
"""
from __future__ import annotations

import fcntl
import os
import secrets
import shutil
import stat
import sys
import time
from pathlib import Path

from . import restore_guard as guard
from .profiles import _validated_index
from .restore_marker import MARKER_NAME

WORK_NAME = ".mcp-restore-inplace"
OUT_NAME = "out"
PRE_NAME = "pre"
LOST_FOUND = "lost+found"
#: Live entries that are never moved aside or published over.
PRESERVED = frozenset({MARKER_NAME, WORK_NAME, LOST_FOUND})
MARKER_TEXT = b"MCP configuration restore in progress; the MCP service must not start.\n"

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_REFUSED = 0, 1, 2, 3
STEPS = ("marker", "workdir", "prepare", "move_aside", "publish", "sync", "verify",
         "discard_live", "remove_workdir", "unmark")

DONE = ("MCP in-place restore completed: every MCP token is revoked and the admin password was replaced. "
        "Reset the admin password out-of-band and issue new MCP tokens before reopening traffic.")
REFUSED = "MCP in-place restore refused: unsafe or unsupported paths; this run changed nothing."
BUSY = "MCP in-place restore refused: another restore holds the marker; this run changed nothing."
FAILED = ("MCP in-place restore failed at step {step}; the restore marker is kept, keep the service "
          "scaled to 0 and re-run (the restore starts over from the source).")


class Refused(Exception):
    """Unsafe preconditions; this run has not changed anything."""


class Busy(Refused):
    """Another restore run holds the marker lock."""


class StepFailed(Exception):
    def __init__(self, step: str) -> None:
        super().__init__(step)
        self.step = step


def _after_step(name: str) -> None:
    """Interruption hook for tests; a no-op otherwise."""


def trusted_live_root(info: os.stat_result, *, is_mount: bool, euid: int, egid: int) -> bool:
    """The live dir must be controlled only by this uid, root and (via fsGroup) this gid.

    A Kubernetes volume root is created root-owned by mkfs; fsGroup only sets our group and
    g+rwx/setgid. Accept that shape for a mount point; refuse other owners or world-writable dirs.
    """
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o002:
        return False
    if info.st_mode & 0o020 and info.st_gid != egid:
        return False
    if info.st_uid == euid:
        return True
    return info.st_uid == 0 and is_mount and info.st_gid == egid


def _check_paths(source: Path, live: Path) -> tuple[Path, Path]:
    if os.geteuid() == 0:
        # The service runs as uid 1000 and only reads files it owns; root-owned output never starts.
        raise Refused()
    source, live = Path(os.path.abspath(source)), Path(os.path.abspath(live))
    try:
        source_info, live_info = source.lstat(), live.lstat()
    except OSError as exc:
        raise Refused() from exc
    if not stat.S_ISDIR(source_info.st_mode):
        raise Refused()
    if not trusted_live_root(live_info, is_mount=os.path.ismount(live), euid=os.geteuid(), egid=os.getegid()):
        raise Refused()
    if (source_info.st_dev, source_info.st_ino) == (live_info.st_dev, live_info.st_ino):
        raise Refused()
    real_source, real_live = Path(os.path.realpath(source)), Path(os.path.realpath(live))
    if real_source == real_live or real_live in real_source.parents or real_source in real_live.parents:
        raise Refused()
    for name in (MARKER_NAME, WORK_NAME):
        try:
            info = (live / name).lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise Refused() from exc
        expected = stat.S_ISREG if name == MARKER_NAME else stat.S_ISDIR
        if not expected(info.st_mode) or info.st_uid != os.geteuid():
            raise Refused()
    return source, live


def _remove(path: Path) -> None:
    if stat.S_ISDIR(path.lstat().st_mode):
        shutil.rmtree(path)  # fd-based on Linux; never follows symlinks
    else:
        os.unlink(path)


def _private_dir(path: Path) -> None:
    """Create or re-tighten a 0700 directory owned by us (a remount with fsGroup widens it)."""
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(fd).st_uid != os.geteuid():
            raise guard.RestoreError()
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)


def _lock_marker(live: Path, marker: Path) -> int:
    """Create (fsync file and directory) or open the marker and hold an exclusive lock on it.

    Returns the locked descriptor; :class:`Busy` when another run holds it. A run that is just
    finishing may unlink the marker after we opened it: only the inode still at the path counts.
    """
    for _ in range(8):
        try:
            fd = os.open(marker, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            created = True
        except FileExistsError:
            fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            created = False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise Busy() from None
        locked, current = os.fstat(fd), None
        try:
            current = marker.lstat()
        except FileNotFoundError:
            pass
        if (current is None or not stat.S_ISREG(locked.st_mode)
                or (current.st_dev, current.st_ino) != (locked.st_dev, locked.st_ino)):
            os.close(fd)
            continue
        if created:
            os.write(fd, MARKER_TEXT)
            os.fsync(fd)
            guard._fsync_dir(live)
        return fd
    raise Busy()


def _prepare_workdir(live: Path, work: Path) -> None:
    created = not os.path.lexists(work)
    _private_dir(work)
    if created:
        guard._fsync_dir(live)
    # Never trust output of an interrupted run; only the moved-aside live entries are kept.
    for entry in sorted(os.listdir(work)):
        if entry != PRE_NAME:
            _remove(work / entry)
    guard._fsync_dir(work)


def _move_aside(live: Path, work: Path) -> None:
    pre = work / PRE_NAME
    _private_dir(pre)
    batch = pre / f"{time.time_ns()}-{secrets.token_hex(4)}"
    os.mkdir(batch, 0o700)
    guard._fsync_dir(pre)
    for name in sorted(os.listdir(live)):
        if name not in PRESERVED:
            os.rename(live / name, batch / name)
    guard._fsync_dir(batch)
    guard._fsync_dir(live)


def _publish(live: Path, work: Path) -> frozenset[str]:
    out = work / OUT_NAME
    names = sorted(os.listdir(out))
    for name in names:
        if name in PRESERVED:
            raise guard.RestoreError()
        guard._publish(out / name, live / name)  # renameat2(RENAME_NOREPLACE)
    return frozenset(names)


def _configs(root_dir: Path, product: str) -> list[tuple[Path, dict]]:
    root = guard._read_regular(root_dir / "config.json", product=product)
    index = _validated_index(root)  # profile ids/labels/ports valid, ports unique and != root port
    configs = [(root_dir, root)]
    profiles = root_dir / "profiles"
    if index:
        guard._directory(profiles)
        if set(os.listdir(profiles)) != set(index):
            raise guard.RestoreError()
    for profile_id, item in index.items():
        directory = profiles / profile_id
        guard._directory(directory)
        config = guard._read_regular(directory / "config.json", product=product)
        if config["mcp_server"]["port"] != item["port"] or config.get("profile_index"):
            raise guard.RestoreError()
        configs.append((directory, config))
    return configs


def _credentials(config: dict) -> set[str]:
    server = config.get("mcp_server") if isinstance(config.get("mcp_server"), dict) else {}
    env = server.get("env") if isinstance(server.get("env"), dict) else {}
    proxy = config.get("proxy") if isinstance(config.get("proxy"), dict) else {}
    values = (config.get("mcp_auth_token"), config.get("admin_password"),
              proxy.get("bearer_token"), env.get("AUTH_TOKEN"))
    return {value for value in values if isinstance(value, str) and value}


def _regular(path: Path) -> None:
    if not stat.S_ISREG(path.lstat().st_mode):
        raise guard.RestoreError()


def _verify(live: Path, source: Path, product: str, published: frozenset[str]) -> None:
    if set(os.listdir(live)) - PRESERVED != set(published) or "config.json" not in published:
        raise guard.RestoreError()
    configs = _configs(live, product)
    ports = [config["mcp_server"]["port"] for _, config in configs]
    if len(set(ports)) != len(ports):
        raise guard.RestoreError()
    for directory, config in configs:
        if config.get("mcp_auth_token") != "" or config.get("mcp_auth_revoked") is not True:
            raise guard.RestoreError()
        if not config.get("mcp_auth_generation") or not config.get("admin_session_generation"):
            raise guard.RestoreError()
        _regular(directory / "config.json")
        _regular(directory / ".config.json.operations.json")
        if product == "n8n":
            _regular(directory / "n8n-mcp" / "nodes.db")
    # Independent check: nothing that authenticated against the backup authenticates now.
    old: set[str] = set()
    for _, config in _configs(source, product):
        old |= _credentials(config)
    if any(_credentials(config) & old for _, config in configs):
        raise guard.RestoreError()


def restore(source: Path, live: Path, *, product: str = "generic") -> None:
    if product not in ("generic", "n8n"):
        raise Refused()
    source, live = _check_paths(source, live)
    marker, work = live / MARKER_NAME, live / WORK_NAME
    step = STEPS[0]
    lock = None
    try:
        lock = _lock_marker(live, marker)
        _after_step(step)
        step = "workdir"
        _prepare_workdir(live, work)
        _after_step(step)
        step = "prepare"
        guard.prepare(source, work / OUT_NAME, product=product, allow_volume_root=True)
        _after_step(step)
        step = "move_aside"
        _move_aside(live, work)
        _after_step(step)
        step = "publish"
        published = _publish(live, work)
        _after_step(step)
        step = "sync"
        guard._fsync_dir(live)
        guard._fsync_dir(work / OUT_NAME)
        _after_step(step)
        step = "verify"
        _verify(live, source, product, published)
        _after_step(step)
        step = "discard_live"
        shutil.rmtree(work / PRE_NAME)
        guard._fsync_dir(work)
        _after_step(step)
        step = "remove_workdir"
        shutil.rmtree(work)
        guard._fsync_dir(live)
        _after_step(step)
        step = "unmark"
        os.unlink(marker)  # still locked: an overlapping run that opened it sees the inode is gone
        guard._fsync_dir(live)
        _after_step(step)
    except (Busy, StepFailed):
        raise
    except Exception as exc:  # noqa: BLE001 — no detail may leave this process
        raise StepFailed(step) from exc
    finally:
        if lock is not None:
            os.close(lock)


def main(argv=None) -> int:
    parser = guard._Parser(description="Restore an MCP config volume in place; all MCP tokens are revoked")
    parser.add_argument("--product", choices=("generic", "n8n"), default="generic")
    parser.add_argument("source", type=Path)
    parser.add_argument("live", type=Path)
    args = parser.parse_args(argv)
    try:
        restore(args.source, args.live, product=args.product)
    except Busy:
        print(BUSY, file=sys.stderr)
        return EXIT_REFUSED
    except Refused:
        print(REFUSED, file=sys.stderr)
        return EXIT_REFUSED
    except StepFailed as failure:
        # Fixed text only: paths, parsed JSON and exception text may contain credentials.
        print(FAILED.format(step=failure.step), file=sys.stderr)
        return EXIT_FAILED
    print(DONE)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
