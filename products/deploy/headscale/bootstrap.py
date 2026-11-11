"""Quadlet entry point: seed only first-boot credentials, then run the PaaS app."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

import uvicorn


def seed_config(path: Path, password: str) -> bool:
    """Create a private config atomically; existing state always wins."""
    try:
        path.lstat()
    except FileNotFoundError:
        pass
    else:
        return False
    if not password or password == "admin" or len(password) < 12:
        raise ValueError("admin password must be a unique value of at least 12 characters")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".config-seed-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"admin_password": password}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return True
    finally:
        os.unlink(temporary)


def main() -> None:
    path = Path(os.environ.get("MCP_ADMIN_CONFIG", "/data/config.json"))
    password = os.environ.pop("ADMIN_PASSWORD", "")
    seed_config(path, password)
    uvicorn.run("headscale_mcp_admin.main:app", host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
