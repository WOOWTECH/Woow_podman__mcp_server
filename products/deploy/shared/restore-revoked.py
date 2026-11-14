#!/usr/bin/env python3
"""Write the shared core's fully revoked copy of a backup into an empty volume.

usage: restore-revoked.py PRODUCT SOURCE_DIR TARGET_DIR

Runs inside the installed product image (restore-volume.sh starts it) so the
validation is the core's own mcp_admin_core.restore_guard: every profile's MCP
token ends revoked, old operation receipts expire and profiles are checked
against the index. The GUI password becomes the installed podman secret
(ADMIN_PASSWORD) instead of restore_guard's random value, so the operator can
sign in and issue new tokens. Nothing is printed except a fixed failure line.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile

STAGED = ".restore-revoked"


def _write_private(path: Path, data: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".restore-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def restore(product: str, source: Path, target: Path, password: str) -> None:
    from mcp_admin_core.restore_guard import prepare

    if not password or password == "admin" or len(password) < 12:
        raise ValueError("installed admin password secret is missing or too short")
    if any(target.iterdir()):
        raise ValueError("target volume is not empty")
    staged = target / STAGED
    # restore_guard publishes atomically next to its stage, so stage inside the target volume.
    prepare(source, staged, product="n8n" if product == "n8n" else "generic")
    for entry in staged.iterdir():
        os.rename(entry, target / entry.name)
    staged.rmdir()
    config = target / "config.json"
    data = json.loads(config.read_text(encoding="utf-8"))
    data["admin_password"] = password
    _write_private(config, data)
    _fsync_dir(target)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: restore-revoked.py PRODUCT SOURCE_DIR TARGET_DIR", file=sys.stderr)
        return 2
    password = os.environ.pop("ADMIN_PASSWORD", "")
    try:
        restore(argv[0], Path(argv[1]), Path(argv[2]), password)
    except Exception:  # noqa: BLE001 - paths and parsed JSON may hold credentials; never echo them
        print("restore preparation failed; the service stays stopped", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
