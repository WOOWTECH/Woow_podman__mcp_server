#!/usr/bin/env python3
"""Invalidate all GUI cookies after a volume is restored from a backup."""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import sys
import tempfile


def rotate(path: Path) -> None:
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or not isinstance(data.get("admin_password"), str):
        raise ValueError("invalid restored MCP configuration")
    data["admin_session_generation"] = secrets.token_urlsafe(24)
    info = path.stat()
    fd, temporary = tempfile.mkstemp(prefix=".restore-auth-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        os.fchown(fd, info.st_uid, info.st_gid)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: rotate-restored-auth.py CONFIG_JSON")
    rotate(Path(sys.argv[1]))
